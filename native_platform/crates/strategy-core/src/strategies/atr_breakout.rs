//! Session volatility breakout (bar-driven), after Kaufman, *Trading Systems and Methods*, ch. 12.
//!
//! Early in a session, enter on the first bar whose body exceeds `atr_mult × ATR(period)` (current
//! ATR, not the source's ATR-expansion term); exit on an ATR target, an ATR stop or a time stop.

use orderflow_engine::atr::Atr;

use crate::strategy::{Bar, BtDecision, Dir, Fill, MarketCtx, Strategy};

/// `(segment, minutes_since_open)`: segment 0 is 09:00–11:30 ICT, 1 is 13:00–14:45; else `None`.
fn session_pos(ts: i64) -> Option<(u8, i64)> {
    let tod = (ts + 7 * 3600).rem_euclid(86_400);
    if (32_400..41_400).contains(&tod) {
        Some((0, (tod - 32_400) / 60)) // 09:00–11:30
    } else if (46_800..53_100).contains(&tod) {
        Some((1, (tod - 46_800) / 60)) // 13:00–14:45
    } else {
        None
    }
}

/// ICT calendar day index.
fn ict_day(ts: i64) -> i64 {
    (ts + 7 * 3600).div_euclid(86_400)
}

/// Parameters; the defaults are the source's published values.
#[derive(Clone, Copy, Debug)]
pub struct AtrBreakoutConfig {
    /// Bar cadence in seconds (M1 = 60).
    pub bar_seconds: i64,
    /// ATR lookback (published 20).
    pub period: usize,
    /// Body-expansion trigger in ATR units (published 1.5).
    pub atr_mult: f64,
    /// Take-profit distance in ATR units (published 2.0).
    pub target_mult: f64,
    /// Stop distance in ATR units (published 1.5).
    pub stop_mult: f64,
    /// Time stop in minutes after entry (published 45).
    pub time_stop_min: i64,
    /// Entry window: only the first N minutes of a session (published 30).
    pub entry_window_min: i64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for AtrBreakoutConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 60, // M1
            period: 20,
            atr_mult: 1.5,
            target_mult: 2.0,
            stop_mult: 1.5,
            time_stop_min: 45,
            entry_window_min: 30,
            enable_short: true,
            qty: 1,
        }
    }
}

/// Entry ATR and time, which `PositionView` lacks; set on the entry fill, cleared on exit.
#[derive(Clone, Copy, Debug)]
struct Active {
    dir: Dir,
    entry_price: f64,
    entry_atr: f64,
    entry_ts: i64,
}

pub struct AtrBreakout {
    cfg: AtrBreakoutConfig,
    atr: Atr,
    /// Current session key `(ict_day, segment)`; detects session rollover.
    session: Option<(i64, u8)>,
    /// One entry per session.
    entered_this_session: bool,
    /// Last warm ATR, recorded as the entry ATR on the entry fill.
    last_atr: Option<f64>,
    active: Option<Active>,
}

impl AtrBreakout {
    #[must_use]
    pub fn new(cfg: AtrBreakoutConfig) -> Self {
        Self {
            atr: Atr::new(cfg.period),
            session: None,
            entered_this_session: false,
            last_atr: None,
            active: None,
            cfg,
        }
    }

    /// Exit for the open position on this bar, or `None` to hold.
    fn exit_decision(&self, bar: &Bar) -> Option<BtDecision> {
        let a = self.active?;
        let target = self.cfg.target_mult * a.entry_atr;
        let stop = self.cfg.stop_mult * a.entry_atr;
        let time_up = (bar.ts_close - a.entry_ts) >= self.cfg.time_stop_min * 60;
        let reason: Option<&'static str> = match a.dir {
            Dir::Long => {
                if bar.close >= a.entry_price + target {
                    Some("atr_breakout_tp")
                } else if bar.close <= a.entry_price - stop {
                    Some("atr_breakout_stop")
                } else if time_up {
                    Some("atr_breakout_time")
                } else {
                    None
                }
            }
            Dir::Short => {
                if bar.close <= a.entry_price - target {
                    Some("atr_breakout_tp")
                } else if bar.close >= a.entry_price + stop {
                    Some("atr_breakout_stop")
                } else if time_up {
                    Some("atr_breakout_time")
                } else {
                    None
                }
            }
        };
        reason.map(|r| BtDecision::Exit { reason: r.into() })
    }
}

impl Strategy for AtrBreakout {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        // ATR runs across sessions, so it warms up once
        let atr = self.atr.on_bar(bar.high, bar.low, bar.close);
        if let Some(a) = atr {
            self.last_atr = Some(a);
        }

        // session rollover resets the entry gate
        let pos = session_pos(bar.ts_close);
        let key = pos.map(|(seg, _)| (ict_day(bar.ts_close), seg));
        if key != self.session {
            self.session = key;
            self.entered_this_session = false;
        }

        // exits take priority over entries
        if ctx.position.is_some() {
            if let Some(exit) = self.exit_decision(bar) {
                return exit;
            }
            return BtDecision::Hold;
        }

        // flat: breakout entry inside the session entry window
        let Some(atr) = atr else {
            return BtDecision::Hold; // ATR cold
        };
        let Some((_, minute)) = pos else {
            return BtDecision::Hold; // outside session
        };
        if self.entered_this_session || minute >= self.cfg.entry_window_min {
            return BtDecision::Hold;
        }

        let body = bar.close - bar.open;
        let trigger = self.cfg.atr_mult * atr;
        if body > trigger {
            self.entered_this_session = true;
            BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "atr_breakout_long".into(),
            }
        } else if body < -trigger && self.cfg.enable_short {
            self.entered_this_session = true;
            BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "atr_breakout_short".into(),
            }
        } else {
            BtDecision::Hold
        }
    }

    fn on_fill(&mut self, fill: &Fill) {
        match fill.action {
            crate::strategy::Action::Enter => {
                self.active = Some(Active {
                    dir: fill.dir,
                    entry_price: fill.price,
                    entry_atr: self.last_atr.unwrap_or(0.0),
                    entry_ts: fill.ts,
                });
            }
            crate::strategy::Action::Exit => {
                self.active = None;
            }
        }
    }

    fn name(&self) -> &'static str {
        "atr_breakout"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::{Action, PositionView};

    // a Monday, 00:00 UTC
    const DAY0_UTC_MIDNIGHT: i64 = 1_767_571_200;
    /// Unix seconds for an ICT hour and minute on day 0.
    fn ict(hh: i64, mm: i64) -> i64 {
        // ICT is UTC+7
        DAY0_UTC_MIDNIGHT + (hh - 7) * 3600 + mm * 60
    }

    fn bar_at(ts: i64, open: f64, high: f64, low: f64, close: f64) -> Bar {
        Bar {
            ts_open: ts - 60,
            ts_close: ts,
            open,
            high,
            low,
            close,
            volume: 1.0,
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

    fn cfg() -> AtrBreakoutConfig {
        AtrBreakoutConfig {
            bar_seconds: 60,
            period: 3,
            atr_mult: 1.0,
            target_mult: 2.0,
            stop_mult: 1.5,
            time_stop_min: 45,
            entry_window_min: 30,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn session_pos_boundaries() {
        assert_eq!(session_pos(ict(9, 0)), Some((0, 0)));
        assert_eq!(session_pos(ict(9, 29)), Some((0, 29)));
        assert_eq!(session_pos(ict(11, 29)), Some((0, 149)));
        assert_eq!(session_pos(ict(11, 30)), None); // session closed
        assert_eq!(session_pos(ict(13, 0)), Some((1, 0)));
        assert_eq!(session_pos(ict(12, 0)), None); // lunch
    }

    #[test]
    fn breakout_enters_long_in_window() {
        let mut s = AtrBreakout::new(cfg());
        // warm the ATR on 3 pre-session bars of range 1.0
        for i in 0..3 {
            let ts = ict(8, 40) + i * 60;
            assert!(matches!(
                s.on_bar(&bar_at(ts, 100.0, 100.5, 99.5, 100.0), &ctx(None, ts)),
                BtDecision::Hold
            ));
        }
        // 09:05 bar with a body of +2, above 1 × ATR
        let ts = ict(9, 5);
        match s.on_bar(&bar_at(ts, 100.0, 102.5, 99.8, 102.0), &ctx(None, ts)) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected Enter Long, got {other:?}"),
        }
    }

    #[test]
    fn no_entry_outside_window() {
        let mut s = AtrBreakout::new(cfg());
        for i in 0..3 {
            let ts = ict(8, 40) + i * 60;
            s.on_bar(&bar_at(ts, 100.0, 100.5, 99.5, 100.0), &ctx(None, ts));
        }
        // 09:45 is past the 30-minute window
        let ts = ict(9, 45);
        assert!(matches!(
            s.on_bar(&bar_at(ts, 100.0, 103.0, 99.0, 102.5), &ctx(None, ts)),
            BtDecision::Hold
        ));
    }

    #[test]
    fn target_exit() {
        let mut s = AtrBreakout::new(cfg());
        for i in 0..3 {
            let ts = ict(8, 40) + i * 60;
            s.on_bar(&bar_at(ts, 100.0, 100.5, 99.5, 100.0), &ctx(None, ts));
        }
        let ts = ict(9, 5);
        s.on_bar(&bar_at(ts, 100.0, 102.5, 99.8, 102.0), &ctx(None, ts));
        // confirm the entry fill at 102
        s.on_fill(&Fill {
            ts,
            dir: Dir::Long,
            action: Action::Enter,
            qty: 1,
            price: 102.0,
            fee_vnd: 0.0,
        });
        let long = Some(PositionView {
            dir: Dir::Long,
            qty: 1,
            entry_price: 102.0,
        });
        // entry ATR ≈ 1.567 after the wide bar, so the target is ≈ 105.13; a close at 105.5 hits it
        let ts2 = ict(9, 10);
        match s.on_bar(&bar_at(ts2, 103.0, 105.6, 102.5, 105.5), &ctx(long, ts2)) {
            BtDecision::Exit { reason } => assert_eq!(reason, "atr_breakout_tp"),
            other => panic!("expected TP exit, got {other:?}"),
        }
    }

    #[test]
    fn time_stop_exit() {
        let mut s = AtrBreakout::new(cfg());
        for i in 0..3 {
            let ts = ict(8, 40) + i * 60;
            s.on_bar(&bar_at(ts, 100.0, 100.5, 99.5, 100.0), &ctx(None, ts));
        }
        let ts = ict(9, 5);
        s.on_bar(&bar_at(ts, 100.0, 102.5, 99.8, 102.0), &ctx(None, ts));
        s.on_fill(&Fill {
            ts,
            dir: Dir::Long,
            action: Action::Enter,
            qty: 1,
            price: 102.0,
            fee_vnd: 0.0,
        });
        let long = Some(PositionView {
            dir: Dir::Long,
            qty: 1,
            entry_price: 102.0,
        });
        // 46 minutes later with a flat price: time stop
        let ts2 = ts + 46 * 60;
        match s.on_bar(&bar_at(ts2, 102.0, 102.2, 101.8, 102.1), &ctx(long, ts2)) {
            BtDecision::Exit { reason } => assert_eq!(reason, "atr_breakout_time"),
            other => panic!("expected time exit, got {other:?}"),
        }
    }
}
