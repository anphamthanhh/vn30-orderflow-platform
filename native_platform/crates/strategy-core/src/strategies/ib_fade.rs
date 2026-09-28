//! Initial-balance failure fade (bar-driven), after Dalton, *Mind Over Markets*.
//!
//! After the IB seals, a bar beyond an IB extreme arms a watch; a close back inside the IB, at
//! once or within `fail_confirm_bars`, fades the failed breakout toward the IB midpoint.

use orderflow_engine::initial_balance::InitialBalance;

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
pub struct IbFadeConfig {
    pub bar_seconds: i64,
    /// Initial-balance window in minutes.
    pub ib_min: i64,
    /// Bars a breakout may stay armed before it is deemed accepted (no fade).
    pub fail_confirm_bars: u32,
    /// Adverse stop in points from entry.
    pub stop_points: f64,
    /// Max bars to hold before a time exit.
    pub hold_bars: u32,
    /// No fresh entry past this many minutes from the segment open.
    pub entry_window_min: i64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for IbFadeConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 300, // M5
            ib_min: 60,
            fail_confirm_bars: 3,
            stop_points: 4.0,
            hold_bars: 12,
            entry_window_min: 150,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct IbFade {
    cfg: IbFadeConfig,
    ib: InitialBalance,
    /// Session key `(ict_day, segment)`.
    session: Option<(i64, u8)>,
    entered_this_session: bool,
    /// Armed breakout `(direction_of_breakout, bars_since_armed)`.
    armed: Option<(Dir, u32)>,
    bars_held: u32,
}

impl IbFade {
    #[must_use]
    pub fn new(cfg: IbFadeConfig) -> Self {
        Self {
            ib: InitialBalance::new(cfg.ib_min),
            session: None,
            entered_this_session: false,
            armed: None,
            bars_held: 0,
            cfg,
        }
    }

    /// Fade entry opposite a failed breakout; `None` when short is disabled.
    fn fade_enter(&self, breakout: Dir) -> Option<BtDecision> {
        match breakout {
            // failed up-breakout: short back into value
            Dir::Long if self.cfg.enable_short => Some(BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "ib_fade_short".into(),
            }),
            Dir::Long => None,
            // failed down-breakout: long back into value
            Dir::Short => Some(BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "ib_fade_long".into(),
            }),
        }
    }
}

impl Strategy for IbFade {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        let day = ict_day(bar.ts_close);
        let pos = session_pos(bar.ts_close);
        let key = pos.map(|(seg, _)| (day, seg));
        if key != self.session {
            self.session = key;
            self.entered_this_session = false;
            self.armed = None;
            if pos.is_some() {
                self.ib.start_session();
            }
        }
        if let Some((_, minute)) = pos {
            self.ib.observe(minute, bar.high, bar.low);
        }

        // open position: IB-mid target, stop or time exit
        if let Some(p) = ctx.position {
            self.bars_held = self.bars_held.saturating_add(1);
            if let Some((ib_high, ib_low)) = self.ib.range() {
                let mid = 0.5 * (ib_high + ib_low);
                let target_hit = match p.dir {
                    Dir::Long => bar.close >= mid,
                    Dir::Short => bar.close <= mid,
                };
                if target_hit {
                    return BtDecision::Exit {
                        reason: "ib_fade_target".into(),
                    };
                }
            }
            let stop_hit = match p.dir {
                Dir::Long => bar.close <= p.entry_price - self.cfg.stop_points,
                Dir::Short => bar.close >= p.entry_price + self.cfg.stop_points,
            };
            if stop_hit {
                return BtDecision::Exit {
                    reason: "ib_fade_stop".into(),
                };
            }
            if self.bars_held >= self.cfg.hold_bars {
                return BtDecision::Exit {
                    reason: "ib_fade_time".into(),
                };
            }
            return BtDecision::Hold;
        }
        self.bars_held = 0;

        // flat: fade only in session, inside the entry window, after the IB seals, once per session
        if self.entered_this_session {
            return BtDecision::Hold;
        }
        let Some((_, minute)) = pos else {
            return BtDecision::Hold;
        };
        if minute > self.cfg.entry_window_min {
            self.armed = None;
            return BtDecision::Hold;
        }
        let Some((ib_high, ib_low)) = self.ib.range() else {
            return BtDecision::Hold;
        };

        match self.armed {
            None => {
                if bar.high > ib_high {
                    // immediate rejection: wick above, close back inside
                    if bar.close < ib_high {
                        if let Some(dec) = self.fade_enter(Dir::Long) {
                            self.entered_this_session = true;
                            return dec;
                        }
                    } else {
                        self.armed = Some((Dir::Long, 0));
                    }
                } else if bar.low < ib_low {
                    if bar.close > ib_low {
                        self.entered_this_session = true;
                        return self.fade_enter(Dir::Short).unwrap();
                    }
                    self.armed = Some((Dir::Short, 0));
                }
                BtDecision::Hold
            }
            Some((bdir, age)) => {
                let age = age + 1;
                if age > self.cfg.fail_confirm_bars {
                    self.armed = None; // breakout held: accepted, no fade
                    return BtDecision::Hold;
                }
                let failed = match bdir {
                    Dir::Long => bar.close < ib_high, // up-breakout re-entered IB
                    Dir::Short => bar.close > ib_low, // down-breakout re-entered IB
                };
                if failed {
                    if let Some(dec) = self.fade_enter(bdir) {
                        self.armed = None;
                        self.entered_this_session = true;
                        return dec;
                    }
                    self.armed = None; // shorts disabled: drop the arm
                    return BtDecision::Hold;
                }
                self.armed = Some((bdir, age));
                BtDecision::Hold
            }
        }
    }

    fn name(&self) -> &'static str {
        "ib_fade"
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
    fn bar_at(ts: i64, o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar {
            ts_open: ts - 60,
            ts_close: ts,
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 10.0,
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
    fn cfg() -> IbFadeConfig {
        IbFadeConfig {
            bar_seconds: 60,
            ib_min: 5,
            fail_confirm_bars: 3,
            stop_points: 4.0,
            hold_bars: 12,
            entry_window_min: 150,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn immediate_up_break_failure_fades_short() {
        let mut s = IbFade::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(&bar_at(ts, 100.0, 101.0, 100.0, 100.5), &ctx(None, ts));
        }
        // 09:05 seals the IB at 100-101; 09:06 wicks to 102 and closes back inside
        let ts = ict(9, 5);
        s.on_bar(&bar_at(ts, 100.5, 101.0, 100.0, 100.8), &ctx(None, ts));
        let ts = ict(9, 6);
        match s.on_bar(&bar_at(ts, 100.8, 102.0, 100.7, 100.8), &ctx(None, ts)) {
            BtDecision::Enter {
                direction: Dir::Short,
                ..
            } => {}
            other => panic!("expected Enter Short, got {other:?}"),
        }
    }

    #[test]
    fn delayed_down_break_failure_fades_long() {
        let mut s = IbFade::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(&bar_at(ts, 100.0, 101.0, 100.0, 100.5), &ctx(None, ts));
        }
        let ts = ict(9, 5);
        s.on_bar(&bar_at(ts, 100.5, 101.0, 100.0, 100.5), &ctx(None, ts));
        // 09:06 closes below the IB low: armed, no entry
        let ts = ict(9, 6);
        assert!(matches!(
            s.on_bar(&bar_at(ts, 100.2, 100.3, 99.0, 99.5), &ctx(None, ts)),
            BtDecision::Hold
        ));
        // 09:07 closes back inside within the fail window: fade long
        let ts = ict(9, 7);
        match s.on_bar(&bar_at(ts, 99.5, 100.5, 99.4, 100.4), &ctx(None, ts)) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected Enter Long, got {other:?}"),
        }
    }

    #[test]
    fn accepted_breakout_no_fade() {
        let mut s = IbFade::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(&bar_at(ts, 100.0, 101.0, 100.0, 100.5), &ctx(None, ts));
        }
        let ts = ict(9, 5);
        s.on_bar(&bar_at(ts, 100.5, 101.0, 100.0, 100.5), &ctx(None, ts));
        // keeps closing above the IB high for longer than fail_confirm_bars
        let mut last = BtDecision::Hold;
        for m in 6..11 {
            let ts = ict(9, m);
            last = s.on_bar(&bar_at(ts, 101.5, 102.5, 101.4, 102.0), &ctx(None, ts));
        }
        assert!(matches!(last, BtDecision::Hold), "got {last:?}");
    }

    #[test]
    fn target_exit_at_ib_mid() {
        let mut s = IbFade::new(cfg());
        for m in 0..5 {
            let ts = ict(9, m);
            s.on_bar(&bar_at(ts, 100.0, 101.0, 100.0, 100.5), &ctx(None, ts));
        }
        // short from 101; a close at the IB mid (100.5) hits the target
        let short = Some(PositionView {
            dir: Dir::Short,
            qty: 1,
            entry_price: 101.0,
        });
        let ts = ict(9, 6);
        assert!(matches!(
            s.on_bar(&bar_at(ts, 100.9, 101.0, 100.4, 100.5), &ctx(short, ts)),
            BtDecision::Exit { reason } if reason == "ib_fade_target"
        ));
    }
}
