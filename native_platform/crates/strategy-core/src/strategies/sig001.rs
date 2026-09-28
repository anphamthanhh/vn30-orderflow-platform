//! SIG-001 as a [`Strategy`]: wraps `signal_engine::SignalEvaluator` and maps its decisions.
//!
//! `DayStart` resets on the first tick of each ICT day, `LunchRollover` at 13:00, and the 80% Rule
//! bias comes from a precomputed per-day map at 09:30; `Sig001Gated` never enters without a bias.

use std::collections::HashMap;

use market_types::TickSide;
use signal_engine::evaluator::{
    SessionBias, SessionResetKind, SignalConfig, SignalEvaluator, TradingDecision,
};
use signal_engine::registry::{Direction, SignalId};

use crate::strategy::{BtDecision, Dir, MarketCtx, Strategy};

const ICT_OFFSET: i64 = 7 * 3600;
const SOD_0930: i64 = 34_200; // 09:30 ICT: bias injection
const SOD_1300: i64 = 46_800; // 13:00 ICT: lunch rollover

fn ict_day(ts: i64) -> i64 {
    (ts + ICT_OFFSET).div_euclid(86_400)
}

fn ict_sod(ts: i64) -> i64 {
    (ts + ICT_OFFSET).rem_euclid(86_400)
}

fn side_str(side: Option<TickSide>) -> &'static str {
    match side {
        Some(TickSide::Buy) => "B",
        Some(TickSide::Sell) => "S",
        None => "",
    }
}

fn dir_to_bt(d: Direction) -> Option<Dir> {
    match d {
        Direction::Bullish => Some(Dir::Long),
        Direction::Bearish => Some(Dir::Short),
        Direction::Neutral => None,
    }
}

/// Backtest/live wrapper over the SIG-001 evaluator.
pub struct Sig001Strategy {
    eval: SignalEvaluator,
    /// Contracts per entry from `SignalConfig::base_qty`, scaled by the VPIN size multiplier
    /// (1.0 normal, 0.5 elevated) and floored at one.
    base_qty: u32,
    /// Precomputed 80% Rule bias per ICT day index.
    bias_by_day: HashMap<i64, SessionBias>,
    cur_day: Option<i64>,
    bias_set_for_day: Option<i64>,
    lunch_done_for_day: Option<i64>,
}

impl Sig001Strategy {
    #[must_use]
    pub fn new(cfg: SignalConfig) -> Self {
        let base_qty = cfg.base_qty.max(1);
        Self {
            eval: SignalEvaluator::new(cfg),
            base_qty,
            bias_by_day: HashMap::new(),
            cur_day: None,
            bias_set_for_day: None,
            lunch_done_for_day: None,
        }
    }

    /// Set the precomputed bias per ICT day index.
    #[must_use]
    pub fn with_bias(mut self, bias_by_day: HashMap<i64, SessionBias>) -> Self {
        self.bias_by_day = bias_by_day;
        self
    }

    fn qty_from_multiplier(&self, mult: f64) -> u32 {
        ((self.base_qty as f64 * mult).floor() as u32).max(1)
    }

    fn map_decision(&self, d: TradingDecision) -> BtDecision {
        match d {
            TradingDecision::Enter {
                direction,
                size_multiplier,
                strength,
                ..
            } => match dir_to_bt(direction) {
                Some(dir) => BtDecision::Enter {
                    direction: dir,
                    qty: self.qty_from_multiplier(size_multiplier),
                    reason: format!("sig001_enter s{strength:.2}").into(),
                },
                None => BtDecision::Hold,
            },
            TradingDecision::Exit { reason, .. } => BtDecision::Exit {
                reason: reason.into(),
            },
            TradingDecision::None | TradingDecision::Blocked { .. } => BtDecision::Hold,
        }
    }

    /// Apply the session resets and bias for `ts` before the evaluator sees the tick.
    ///
    /// Returns the forced exit reported by a day-boundary reset; the caller must emit it so the
    /// engine closes its position too.
    fn apply_session_inputs(&mut self, ts: i64) -> Option<BtDecision> {
        let day = ict_day(ts);
        let sod = ict_sod(ts);
        let mut forced_exit = None;

        // new ICT day: the DayStart reset force-exits an open position
        if self.cur_day != Some(day) {
            let signals = self.eval.session_reset(SessionResetKind::DayStart);
            if signals
                .iter()
                .any(|s| s.note == "day_start_force_exit" || s.id == SignalId::TrailingStopExit)
            {
                forced_exit = Some(BtDecision::Exit {
                    reason: "sig001_day_start_force_exit".into(),
                });
            }
            self.cur_day = Some(day);
            self.bias_set_for_day = None;
            self.lunch_done_for_day = None;
        }

        // 09:30 ICT: inject the day's bias once, if the map has one
        if sod >= SOD_0930 && self.bias_set_for_day != Some(day) {
            if let Some(bias) = self.bias_by_day.get(&day).copied() {
                self.eval.set_bias(bias);
            }
            self.bias_set_for_day = Some(day);
        }

        // 13:00 ICT: LunchRollover once; rolls CVD, keeps bias and position
        if sod >= SOD_1300 && self.lunch_done_for_day != Some(day) {
            let signals = self.eval.session_reset(SessionResetKind::LunchRollover);
            debug_assert!(
                !signals.iter().any(|s| s.id == SignalId::TrailingStopExit),
                "LunchRollover must not force-exit: position persists across lunch"
            );
            self.lunch_done_for_day = Some(day);
        }

        forced_exit
    }
}

impl Strategy for Sig001Strategy {
    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        // a day-boundary force-exit takes precedence, keeping the engine in step with the evaluator
        if let Some(exit) = self.apply_session_inputs(ctx.timestamp) {
            // feed the tick so indicator state advances; only the decision is replaced
            let (_signals, discarded) =
                self.eval
                    .on_tick(ctx.price, ctx.volume, side_str(ctx.side), ctx.timestamp);
            // the reset cleared the bar builder, so no divergence and hence no entry fires here
            debug_assert!(
                !matches!(discarded, TradingDecision::Enter { .. }),
                "an entry on the DayStart tick would be dropped: the engine is \
                 being told to Exit on this tick and a decision is atomic"
            );
            return exit;
        }
        let (_signals, decision) =
            self.eval
                .on_tick(ctx.price, ctx.volume, side_str(ctx.side), ctx.timestamp);
        self.map_decision(decision)
    }

    fn name(&self) -> &'static str {
        "sig001"
    }
}

#[cfg(test)]
mod tests {
    // driver-dependent tests live in `backtest-engine/tests/strategy_integration.rs`
    use super::*;

    #[test]
    fn qty_scales_with_regime_multiplier() {
        let s = Sig001Strategy::new(SignalConfig::default());
        assert_eq!(s.qty_from_multiplier(1.0), 2); // normal
        assert_eq!(s.qty_from_multiplier(0.5), 1); // elevated half-size
    }

    /// Open a real position through a CVD divergence, cross the ICT day boundary, expect `Exit`.
    #[test]
    fn day_start_force_exit_is_emitted_to_the_engine() {
        use signal_engine::evaluator::EntryMode;

        // PureCvd needs no bias; M1 bars keep the fixture to 13 ticks
        let mut s = Sig001Strategy::new(SignalConfig {
            bar_seconds: 60,
            entry_mode: EntryMode::PureCvd,
            ..SignalConfig::default()
        });

        // one tick per bar: ticks 1-11 climb on buying, tick 12 holds the high on heavy selling
        // (CVD 1100 -> 100), a bearish exhaustion; tick 13 completes that bar, past the 10-bar
        // warm-up, and the entry fires
        let base = 7_200; // 09:00 ICT, ICT day 0
        let mut entered = false;
        for k in 1..=13i64 {
            let (price, side, volume) = if k <= 11 {
                (1_000.0 + k as f64, TickSide::Buy, 100.0)
            } else {
                (1_011.0, TickSide::Sell, if k == 12 { 1_000.0 } else { 1.0 })
            };
            let ctx = MarketCtx {
                price,
                volume,
                side: Some(side),
                timestamp: base + k * 60,
                position: None,
            };
            if let BtDecision::Enter { direction, .. } = s.on_tick(&ctx) {
                assert!(
                    matches!(direction, Dir::Short),
                    "exhaustion at a high fades short"
                );
                entered = true;
            }
        }
        assert!(
            entered,
            "fixture failed to open a position, so it proves nothing"
        );
        assert!(
            s.eval.state().position.is_some(),
            "the evaluator must be holding the advisory position we are about to force out"
        );

        // cross into the next ICT day with the position open
        let next_day = MarketCtx {
            price: 1_011.0,
            volume: 1.0,
            side: Some(TickSide::Buy),
            timestamp: base + 86_400,
            position: Some(crate::strategy::PositionView {
                dir: Dir::Short,
                qty: 2,
                entry_price: 1_011.0,
            }),
        };
        match s.on_tick(&next_day) {
            BtDecision::Exit { reason } => assert!(
                reason.contains("day_start"),
                "the exit must name its cause, got {reason:?}"
            ),
            other => panic!(
                "a day boundary with an open position must emit Exit, got {other:?} \
                 — this is the defect: the engine is left holding a position it can \
                 never close"
            ),
        }
        assert!(
            s.eval.state().position.is_none(),
            "the evaluator must be flat after the force-exit"
        );
        assert_eq!(s.cur_day, Some(ict_day(base + 86_400)));
    }

    #[test]
    fn ict_helpers_align_to_session() {
        // unix 7200 is 09:00 ICT (02:00 UTC)
        assert_eq!(ict_sod(7_200), 32_400);
        assert_eq!(ict_sod(21_600), SOD_1300);
        assert_eq!(ict_sod(9_000), SOD_0930);
    }

    #[test]
    fn map_decision_translates_evaluator_output() {
        let s = Sig001Strategy::new(SignalConfig::default());
        // bearish at half size: Short, qty floor(2 × 0.5) = 1
        let enter = TradingDecision::Enter {
            direction: Direction::Bearish,
            strength: 0.6,
            price: 1250.0,
            initial_stop: 1252.0,
            size_multiplier: 0.5,
        };
        match s.map_decision(enter) {
            BtDecision::Enter { direction, qty, .. } => {
                assert!(matches!(direction, Dir::Short));
                assert_eq!(qty, 1);
            }
            other => panic!("expected Enter, got {other:?}"),
        }
        // Exit passes the reason through
        match s.map_decision(TradingDecision::Exit {
            reason: "trailing_stop",
            price: 1249.0,
            stop: 1250.0,
        }) {
            BtDecision::Exit { reason } => assert_eq!(reason.as_ref(), "trailing_stop"),
            other => panic!("expected Exit, got {other:?}"),
        }
        // Blocked maps to Hold
        assert!(matches!(
            s.map_decision(TradingDecision::Blocked {
                reason: "vpin_toxic"
            }),
            BtDecision::Hold
        ));
    }

    #[test]
    fn day_change_triggers_reset() {
        let mut s = Sig001Strategy::new(SignalConfig::default());
        s.apply_session_inputs(32_400); // ICT day 0
        assert_eq!(s.cur_day, Some(ict_day(32_400)));
        let next_day = 32_400 + 86_400;
        s.apply_session_inputs(next_day);
        assert_eq!(s.cur_day, Some(ict_day(next_day)));
        assert_ne!(ict_day(32_400), ict_day(next_day));
    }
}
