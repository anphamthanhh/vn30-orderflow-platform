//! Initial-balance breakout with volume confirmation (bar-driven), after Kaufman ch. 5.
//!
//! A close beyond the IB on volume ≥ `vol_spike_mult` × its average enters with the break, if on
//! the same side of the prior-day close (a proxy for the source's value-area gate).

use orderflow_engine::initial_balance::InitialBalance;
use orderflow_engine::volume_ma::RollingVolumeMa;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

/// VN30F1M session position `(segment, minute_since_open)`; `None` off-session.
fn session_pos(ts: i64) -> Option<(u8, i64)> {
    let tod = (ts + 7 * 3600).rem_euclid(86_400);
    if (32_400..41_400).contains(&tod) {
        Some((0, (tod - 32_400) / 60))
    } else if (46_800..53_100).contains(&tod) {
        Some((1, (tod - 46_800) / 60))
    } else {
        None
    }
}

fn ict_day(ts: i64) -> i64 {
    (ts + 7 * 3600).div_euclid(86_400)
}

#[derive(Clone, Copy, Debug)]
pub struct SrlcvConfig {
    pub bar_seconds: i64,
    /// Initial-balance window in minutes (published 15).
    pub ib_min: i64,
    /// Volume-spike multiple vs trailing MA (published 2.0).
    pub vol_spike_mult: f64,
    /// Volume MA lookback in bars (published 20).
    pub vol_ma_period: usize,
    /// Max bars to hold before a time exit (published 4).
    pub hold_bars: u32,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for SrlcvConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 60, // M1
            ib_min: 15,
            vol_spike_mult: 2.0,
            vol_ma_period: 20,
            hold_bars: 4,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct Srlcv {
    cfg: SrlcvConfig,
    ib: InitialBalance,
    vol: RollingVolumeMa,
    /// Session key `(ict_day, segment)`.
    session: Option<(i64, u8)>,
    /// One entry attempt per session.
    entered_this_session: bool,
    /// Bars held since the current position opened.
    bars_held: u32,
    /// Prior-day close (regime reference for the VA-gate proxy).
    prior_day_close: Option<f64>,
    /// Current ICT day and its latest close, rolled into `prior_day_close` at the day change.
    cur_day: Option<i64>,
    cur_day_close: Option<f64>,
}

impl Srlcv {
    #[must_use]
    pub fn new(cfg: SrlcvConfig) -> Self {
        Self {
            ib: InitialBalance::new(cfg.ib_min),
            vol: RollingVolumeMa::new(cfg.vol_ma_period),
            session: None,
            entered_this_session: false,
            bars_held: 0,
            prior_day_close: None,
            cur_day: None,
            cur_day_close: None,
            cfg,
        }
    }
}

impl Strategy for Srlcv {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        // the spike compares this bar with the average of earlier bars, so read the MA first
        let prev_vol_ma = self.vol.value();
        self.vol.on_volume(bar.volume);

        // day rollover: the last close becomes the prior-day reference
        let day = ict_day(bar.ts_close);
        if self.cur_day != Some(day) {
            if let Some(c) = self.cur_day_close {
                self.prior_day_close = Some(c);
            }
            self.cur_day = Some(day);
        }
        self.cur_day_close = Some(bar.close);

        // session position and IB accumulation
        let pos = session_pos(bar.ts_close);
        let key = pos.map(|(seg, _)| (day, seg));
        if key != self.session {
            self.session = key;
            self.entered_this_session = false;
            if pos.is_some() {
                self.ib.start_session();
            }
        }
        if let Some((_, minute)) = pos {
            self.ib.observe(minute, bar.high, bar.low);
        }

        // open position: exit at the opposite IB boundary or on time
        if ctx.position.is_some() {
            self.bars_held = self.bars_held.saturating_add(1);
            if let Some((ib_high, ib_low)) = self.ib.range() {
                let dir = ctx.position.map(|p| p.dir);
                let boundary_hit = match dir {
                    Some(Dir::Long) => bar.close <= ib_low,
                    Some(Dir::Short) => bar.close >= ib_high,
                    None => false,
                };
                if boundary_hit {
                    return BtDecision::Exit {
                        reason: "srlcv_opposite_boundary".into(),
                    };
                }
            }
            if self.bars_held >= self.cfg.hold_bars {
                return BtDecision::Exit {
                    reason: "srlcv_time".into(),
                };
            }
            return BtDecision::Hold;
        }
        self.bars_held = 0;

        // flat: breakout entry once the IB seals, on a volume spike, on the regime side
        if self.entered_this_session {
            return BtDecision::Hold;
        }
        let Some((ib_high, ib_low)) = self.ib.range() else {
            return BtDecision::Hold;
        };
        if pos.is_none() {
            return BtDecision::Hold; // only enter in-session
        }
        let spike = match prev_vol_ma {
            Some(ma) if ma > 0.0 => bar.volume >= self.cfg.vol_spike_mult * ma,
            _ => false,
        };
        if !spike {
            return BtDecision::Hold;
        }
        let above_regime = self.prior_day_close.is_none_or(|p| bar.close > p);
        let below_regime = self.prior_day_close.is_none_or(|p| bar.close < p);

        if bar.close > ib_high && above_regime {
            self.entered_this_session = true;
            BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "srlcv_breakout_long".into(),
            }
        } else if bar.close < ib_low && below_regime && self.cfg.enable_short {
            self.entered_this_session = true;
            BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "srlcv_breakout_short".into(),
            }
        } else {
            BtDecision::Hold
        }
    }

    fn name(&self) -> &'static str {
        "srlcv"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    const DAY0_UTC_MIDNIGHT: i64 = 1_767_571_200; // a Monday, 00:00 UTC
    fn ict(hh: i64, mm: i64) -> i64 {
        DAY0_UTC_MIDNIGHT + (hh - 7) * 3600 + mm * 60
    }
    fn bar_at(ts: i64, o: f64, h: f64, l: f64, c: f64, v: f64) -> Bar {
        Bar {
            ts_open: ts - 60,
            ts_close: ts,
            open: o,
            high: h,
            low: l,
            close: c,
            volume: v,
        }
    }
    fn ctx(pos: Option<PositionView>, ts: i64) -> MarketCtx {
        MarketCtx {
            price: 0.0,
            volume: 0.0,
            side: None,
            timestamp: ts,
            position: pos,
        }
    }
    fn cfg() -> SrlcvConfig {
        SrlcvConfig {
            bar_seconds: 60,
            ib_min: 5,
            vol_spike_mult: 2.0,
            vol_ma_period: 3,
            hold_bars: 4,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn volume_breakout_enters_long() {
        let mut s = Srlcv::new(cfg());
        // IB window 09:00-09:04: volume 10, range 100-101
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(
                &bar_at(ts, 100.0, 101.0, 100.0, 100.5, 10.0),
                &ctx(None, ts),
            );
        }
        // 09:05 seals the IB; the volume MA stays at 10
        let ts = ict(9, 5);
        s.on_bar(
            &bar_at(ts, 100.5, 101.0, 100.0, 100.8, 10.0),
            &ctx(None, ts),
        );
        // 09:06 breaks above the IB high on 3× the average volume
        let ts = ict(9, 6);
        match s.on_bar(
            &bar_at(ts, 100.8, 102.0, 100.7, 101.8, 30.0),
            &ctx(None, ts),
        ) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected Enter Long, got {other:?}"),
        }
    }

    #[test]
    fn no_entry_without_volume_spike() {
        let mut s = Srlcv::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(
                &bar_at(ts, 100.0, 101.0, 100.0, 100.5, 10.0),
                &ctx(None, ts),
            );
        }
        let ts = ict(9, 5);
        s.on_bar(
            &bar_at(ts, 100.5, 101.0, 100.0, 100.8, 10.0),
            &ctx(None, ts),
        );
        // breaks above the IB high on flat volume: no entry
        let ts = ict(9, 6);
        assert!(matches!(
            s.on_bar(
                &bar_at(ts, 100.8, 102.0, 100.7, 101.8, 10.0),
                &ctx(None, ts)
            ),
            BtDecision::Hold
        ));
    }

    #[test]
    fn time_exit_after_hold_bars() {
        let mut s = Srlcv::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(
                &bar_at(ts, 100.0, 101.0, 100.0, 100.5, 10.0),
                &ctx(None, ts),
            );
        }
        let long = Some(PositionView {
            dir: Dir::Long,
            qty: 1,
            entry_price: 101.5,
        });
        // 4 bars inside the IB range: time exit on the 4th
        let mut last = BtDecision::Hold;
        for m in 6..10 {
            let ts = ict(9, m);
            last = s.on_bar(
                &bar_at(ts, 100.6, 100.9, 100.6, 100.8, 10.0),
                &ctx(long, ts),
            );
        }
        assert!(matches!(last, BtDecision::Exit { .. }), "got {last:?}");
    }
}
