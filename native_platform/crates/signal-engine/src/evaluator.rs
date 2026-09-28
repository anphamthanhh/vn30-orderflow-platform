//! `SignalEvaluator`: the three-tier SIG-001 decision engine.
//!
//! ```text
//! TIER 1  VPIN filter    (hot,  per tick — volume buckets)
//! TIER 2  80% Rule bias  (cold, set once after 09:30 ICT via set_bias)
//! TIER 3  CVD exhaustion (hot,  per completed 15m bar)
//! Exits   adaptive trailing stop (chandelier ATR, advisory)
//! ```
//!
//! A synchronous state machine: `on_tick` is deterministic and testable without an async runtime.

use orderflow_engine::atr::Atr;
use orderflow_engine::cvd::{CvdTracker, DivBar};
use orderflow_engine::delta::DeltaCalculator;
use orderflow_engine::vpin::{VpinCalculator, VpinRegime};

use super::registry::{Direction, SignalId};
use super::trailing::{AdvisoryPosition, StopEvent, TrailingConfig, TrailingStopManager};
use super::triggers::{
    CvdExhaustionTrigger, EightyPercentTrigger, SignalResult, TriggerContext, TriggerContract,
    VpinFilterTrigger,
};

/// Entry gating mode; `backtest-engine`'s `Sig001Strategy` reads the same setting.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum EntryMode {
    /// All three tiers: VPIN Toxic veto, required 80% Rule bias, half size when Elevated.
    #[default]
    Sig001Gated,
    /// Enter on any confirmed CVD exhaustion divergence while flat, with no gates.
    PureCvd,
}

/// Pipeline configuration (VN30F1M defaults).
#[derive(Debug, Clone, Copy)]
pub struct SignalConfig {
    /// VPIN volume bucket size in contracts.
    pub bucket_size: f64,
    /// VPIN rolling window in buckets.
    pub n_buckets: usize,
    /// Divergence lookback window in bars.
    pub lookback: usize,
    /// Minimum CVD collapse/recovery fraction.
    pub cvd_threshold: f64,
    /// Minimum divergence strength to emit.
    pub min_strength: f64,
    /// Bar length in seconds for divergence and ATR (default 15 min).
    pub bar_seconds: i64,
    /// CVD history cap (charting).
    pub max_cvd_points: usize,
    pub trailing: TrailingConfig,
    /// Entry gating mode (default `Sig001Gated`).
    pub entry_mode: EntryMode,
    /// Contracts per entry before `size_multiplier`; shared with `Sig001Strategy`.
    pub base_qty: u32,
}

impl Default for SignalConfig {
    fn default() -> Self {
        Self {
            bucket_size: 500.0,
            n_buckets: 50,
            lookback: 3,
            cvd_threshold: 0.3,
            min_strength: 0.3,
            bar_seconds: 900,
            max_cvd_points: 10_000,
            trailing: TrailingConfig::default(),
            entry_mode: EntryMode::Sig001Gated,
            base_qty: 2,
        }
    }
}

/// 80% Rule session bias, computed by the cold path after 09:30 ICT.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SessionBias {
    pub direction: Direction,
    pub prior_vah: f64,
    pub prior_val: f64,
    /// Opposite value-area edge.
    pub target: f64,
    pub confidence: f64,
    pub computed_at: i64,
}

/// Per-tick decision; advisory only, nothing is executed.
#[derive(Debug, Clone, PartialEq)]
pub enum TradingDecision {
    None,
    /// Tier 1 veto.
    Blocked {
        reason: &'static str,
    },
    /// All three tiers aligned; trailing stop opened.
    Enter {
        direction: Direction,
        strength: f64,
        price: f64,
        initial_stop: f64,
        size_multiplier: f64,
    },
    /// Trailing stop hit or force exit.
    Exit {
        reason: &'static str,
        price: f64,
        stop: f64,
    },
}

/// Session boundary kinds (VN30F1M: 09:00 day start, 13:00 lunch return).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionResetKind {
    /// 09:00 ICT: force-exit the advisory position and reset all state.
    DayStart,
    /// 13:00 ICT: CVD rollover only; VPIN, bias, position and ATR persist.
    LunchRollover,
}

/// Evaluator state snapshot for late subscribers.
#[derive(Debug, Clone)]
pub struct SignalStateSnapshot {
    pub vpin: f64,
    pub regime: VpinRegime,
    /// Server-side CVD reading, for diagnostics and tests.
    #[allow(dead_code)]
    pub cvd: f64,
    pub bias: Option<SessionBias>,
    pub position: Option<AdvisoryPosition>,
    /// Most recent divergence, for diagnostics and tests.
    #[allow(dead_code)]
    pub last_divergence: Option<SignalResult>,
}

/// Accumulates ticks into fixed-window bars floored to `bar_seconds` (pandas `resample` alignment).
#[derive(Debug, Clone)]
struct BarBuilder {
    bar_seconds: i64,
    current: Option<BarAccum>,
}

#[derive(Debug, Clone, Copy)]
struct BarAccum {
    bar_index: i64,
    high: f64,
    low: f64,
    close: f64,
    cvd: f64,
}

impl BarBuilder {
    fn new(bar_seconds: i64) -> Self {
        Self {
            bar_seconds: bar_seconds.max(1),
            current: None,
        }
    }

    /// Feed one tick; returns the completed bar when this tick opens a new window.
    fn on_tick(&mut self, price: f64, cvd: f64, timestamp: i64) -> Option<DivBar> {
        let bar_index = timestamp / self.bar_seconds;
        match self.current.as_mut() {
            Some(acc) if acc.bar_index == bar_index => {
                acc.high = acc.high.max(price);
                acc.low = acc.low.min(price);
                acc.close = price;
                acc.cvd = cvd;
                None
            }
            Some(acc) => {
                let completed = DivBar {
                    timestamp: acc.bar_index * self.bar_seconds,
                    close: acc.close,
                    high: acc.high,
                    low: acc.low,
                    cvd: acc.cvd,
                };
                *acc = BarAccum {
                    bar_index,
                    high: price,
                    low: price,
                    close: price,
                    cvd,
                };
                Some(completed)
            }
            None => {
                self.current = Some(BarAccum {
                    bar_index,
                    high: price,
                    low: price,
                    close: price,
                    cvd,
                });
                None
            }
        }
    }

    fn reset(&mut self) {
        self.current = None;
    }
}

/// Three-tier signal evaluator for one symbol; owns all engine state.
pub struct SignalEvaluator {
    delta: DeltaCalculator,
    cvd: CvdTracker,
    vpin: VpinCalculator,
    atr: Atr,
    trailing: TrailingStopManager,
    triggers: Vec<Box<dyn TriggerContract>>,
    bias: Option<SessionBias>,
    bar_builder: BarBuilder,
    last_price: f64,
    last_divergence: Option<SignalResult>,
    entry_mode: EntryMode,
}

impl SignalEvaluator {
    pub fn new(cfg: SignalConfig) -> Self {
        Self {
            delta: DeltaCalculator::new(cfg.bar_seconds),
            cvd: CvdTracker::new(cfg.max_cvd_points),
            vpin: VpinCalculator::new(cfg.bucket_size, cfg.n_buckets),
            atr: Atr::new(cfg.trailing.atr_period),
            trailing: TrailingStopManager::new(cfg.trailing),
            triggers: vec![
                Box::new(VpinFilterTrigger::new()),
                Box::new(EightyPercentTrigger::new()),
                Box::new(CvdExhaustionTrigger::new(
                    cfg.lookback,
                    cfg.cvd_threshold,
                    cfg.min_strength,
                )),
            ],
            bias: None,
            bar_builder: BarBuilder::new(cfg.bar_seconds),
            last_price: 0.0,
            last_divergence: None,
            entry_mode: cfg.entry_mode,
        }
    }

    /// Open a trailing-stop position and return the matching `Enter` decision.
    fn open_entry(
        &mut self,
        direction: Direction,
        strength: f64,
        price: f64,
        timestamp: i64,
        regime: VpinRegime,
        size_multiplier: f64,
    ) -> Option<TradingDecision> {
        self.trailing
            .open(direction, price, timestamp, self.atr.value(), regime);
        self.trailing.position().map(|pos| TradingDecision::Enter {
            direction,
            strength,
            price,
            initial_stop: pos.stop,
            size_multiplier,
        })
    }

    /// Evaluate one tick: engines, triggers, then exits before entries. O(1) amortized.
    pub fn on_tick(
        &mut self,
        price: f64,
        volume: f64,
        side: &str,
        timestamp: i64,
    ) -> (Vec<SignalResult>, TradingDecision) {
        if !price.is_finite() {
            return (Vec::new(), TradingDecision::None);
        }
        self.last_price = price;

        // 1. Engines (delta normalizes the side and guards against NaN volume).
        let tick_delta = self.delta.on_tick(volume, side, timestamp);
        self.cvd.update(tick_delta.net_delta, timestamp);
        self.vpin
            .add_trade(tick_delta.buy_volume, tick_delta.sell_volume);
        let completed = self
            .bar_builder
            .on_tick(price, self.cvd.current_value(), timestamp);
        if let Some(bar) = &completed {
            // the same completed bar also feeds the divergence trigger below
            self.atr.on_bar(bar.high, bar.low, bar.close);
        }
        let regime = self.vpin.regime();

        // 2. Triggers (transition signals only).
        let mut signals = Vec::new();
        {
            let ctx = TriggerContext {
                timestamp,
                price,
                vpin: &self.vpin,
                bias: self.bias.as_ref(),
                bar_completed: completed.as_ref(),
            };
            for trigger in self.triggers.iter_mut() {
                signals.extend(trigger.evaluate(&ctx));
            }
        }
        if let Some(div) = signals.iter().find(|s| {
            matches!(
                s.id,
                SignalId::CvdBullishExhaustion | SignalId::CvdBearishExhaustion
            )
        }) {
            self.last_divergence = Some(div.clone());
        }

        // 3. Position management; an exit takes precedence over an entry.
        let position_direction = self.trailing.position().map(|p| p.direction);
        match self
            .trailing
            .on_tick(price, timestamp, self.atr.value(), regime)
        {
            StopEvent::Updated { stop } => signals.push(SignalResult {
                id: SignalId::TrailingStopUpdate,
                direction: position_direction.unwrap_or(Direction::Neutral),
                strength: 0.0,
                price,
                timestamp,
                target: None,
                stop: Some(stop),
                note: String::new(),
            }),
            StopEvent::Hit { stop, exit_price } => {
                signals.push(SignalResult {
                    id: SignalId::TrailingStopExit,
                    direction: position_direction.unwrap_or(Direction::Neutral),
                    strength: 0.0,
                    price: exit_price,
                    timestamp,
                    target: None,
                    stop: Some(stop),
                    note: "trailing_stop".to_string(),
                });
                return (
                    signals,
                    TradingDecision::Exit {
                        reason: "trailing_stop",
                        price: exit_price,
                        stop,
                    },
                );
            }
            StopEvent::None => {}
        }

        // 4. Entry, per `EntryMode`; under Sig001Gated no bias means no entry.
        let new_divergence = signals
            .iter()
            .find(|s| {
                matches!(
                    s.id,
                    SignalId::CvdBullishExhaustion | SignalId::CvdBearishExhaustion
                )
            })
            .cloned();
        match self.entry_mode {
            EntryMode::Sig001Gated => {
                if regime == VpinRegime::Toxic {
                    return (
                        signals,
                        TradingDecision::Blocked {
                            reason: "vpin_toxic",
                        },
                    );
                }
                if let (Some(div), Some(bias)) = (new_divergence, self.bias) {
                    if div.direction == bias.direction && self.trailing.position().is_none() {
                        let size_multiplier = if regime == VpinRegime::Elevated {
                            0.5
                        } else {
                            1.0
                        };
                        if let Some(decision) = self.open_entry(
                            div.direction,
                            div.strength,
                            price,
                            timestamp,
                            regime,
                            size_multiplier,
                        ) {
                            return (signals, decision);
                        }
                    }
                }
            }
            EntryMode::PureCvd => {
                if let Some(div) = new_divergence {
                    if div.direction != Direction::Neutral && self.trailing.position().is_none() {
                        if let Some(decision) = self.open_entry(
                            div.direction,
                            div.strength,
                            price,
                            timestamp,
                            regime,
                            1.0,
                        ) {
                            return (signals, decision);
                        }
                    }
                }
            }
        }
        (signals, TradingDecision::None)
    }

    /// Set the 80% Rule bias (cold path, once per day after 09:30 ICT) and fire its trigger.
    ///
    /// Ignored if a bias is already set for the session or `bias` is neutral.
    pub fn set_bias(&mut self, bias: SessionBias) -> Vec<SignalResult> {
        if self.bias.is_some() || bias.direction == Direction::Neutral {
            return Vec::new();
        }
        let timestamp = bias.computed_at;
        self.bias = Some(bias);
        let mut signals = Vec::new();
        {
            let ctx = TriggerContext {
                timestamp,
                price: self.last_price,
                vpin: &self.vpin,
                bias: self.bias.as_ref(),
                bar_completed: None,
            };
            for trigger in self.triggers.iter_mut() {
                signals.extend(trigger.evaluate(&ctx));
            }
        }
        signals
    }

    /// Handle a session boundary; returns the force-exit signal when `DayStart` closes a position.
    pub fn session_reset(&mut self, kind: SessionResetKind) -> Vec<SignalResult> {
        match kind {
            SessionResetKind::DayStart => {
                let mut signals = Vec::new();
                if let StopEvent::Hit { stop, exit_price } =
                    self.trailing.force_exit(self.last_price)
                {
                    signals.push(SignalResult {
                        id: SignalId::TrailingStopExit,
                        direction: Direction::Neutral,
                        strength: 0.0,
                        price: exit_price,
                        timestamp: 0,
                        target: None,
                        stop: Some(stop),
                        note: "day_start_force_exit".to_string(),
                    });
                }
                self.delta.reset_bar(0);
                self.cvd.reset();
                self.vpin.reset();
                self.atr.reset();
                self.bar_builder.reset();
                self.bias = None;
                self.last_divergence = None;
                for trigger in self.triggers.iter_mut() {
                    trigger.reset();
                }
                signals
            }
            SessionResetKind::LunchRollover => {
                self.cvd.rollover();
                Vec::new()
            }
        }
    }

    /// State snapshot for late subscribers.
    pub fn state(&self) -> SignalStateSnapshot {
        SignalStateSnapshot {
            vpin: self.vpin.current_vpin(),
            regime: self.vpin.regime(),
            cvd: self.cvd.current_value(),
            bias: self.bias,
            position: self.trailing.position().copied(),
            last_divergence: self.last_divergence.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 60 s bars, 10-contract VPIN buckets, 5-bucket VPIN window.
    fn test_cfg() -> SignalConfig {
        SignalConfig {
            bucket_size: 10.0,
            n_buckets: 5,
            bar_seconds: 60,
            ..SignalConfig::default()
        }
    }

    fn bearish_bias() -> SessionBias {
        SessionBias {
            direction: Direction::Bearish,
            prior_vah: 1305.0,
            prior_val: 1295.0,
            target: 1295.0,
            confidence: 0.8,
            computed_at: 100,
        }
    }

    /// Feed bar `i` (`[i*60, i*60+59]`) as a buy tick then a sell tick.
    fn feed_bar(
        ev: &mut SignalEvaluator,
        i: i64,
        price: f64,
        buy: f64,
        sell: f64,
    ) -> Vec<TradingDecision> {
        let ts = i * 60;
        let (_, d1) = ev.on_tick(price, buy, "B", ts);
        let (_, d2) = ev.on_tick(price, sell, "S", ts + 1);
        vec![d1, d2]
    }

    /// Rising price, mild buy excess (+2/bar, VPIN 0.2) for `n` bars.
    fn feed_uptrend(ev: &mut SignalEvaluator, n: i64) {
        for i in 0..n {
            feed_bar(ev, i, 100.0 + i as f64, 6.0, 4.0);
        }
    }

    #[test]
    fn toxic_vpin_blocks_entry() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        let mut toxic_signals = 0;
        let mut last = TradingDecision::None;
        // Pure one-sided buy flow → every bucket abs_delta 1.0 → toxic.
        for i in 0..8 {
            let (sigs, d) = ev.on_tick(1300.0, 10.0, "B", i);
            toxic_signals += sigs.iter().filter(|s| s.id == SignalId::VpinToxic).count();
            last = d;
        }
        assert_eq!(toxic_signals, 1, "VpinToxic emitted once per crossing");
        assert_eq!(
            last,
            TradingDecision::Blocked {
                reason: "vpin_toxic"
            }
        );
        assert_eq!(ev.state().regime, VpinRegime::Toxic);
    }

    /// Bearish divergence at bar 11: a new price high while CVD falls more than 30 %.
    fn feed_divergence_script(ev: &mut SignalEvaluator) -> (Vec<SignalResult>, TradingDecision) {
        // bar 11: sell excess 8 takes CVD 22 -> 14 (<= 22*0.7 = 15.4), strength 8/22 = 0.36
        feed_uptrend(ev, 11); // bars 0..10, cvd +2/bar → 22
        feed_bar(ev, 11, 111.0, 1.0, 9.0);
        // First tick of bar 12 completes bar 11 → divergence evaluated.
        ev.on_tick(111.0, 1.0, "B", 12 * 60)
    }

    #[test]
    fn divergence_with_matching_bias_enters_with_trailing_stop() {
        let mut ev = SignalEvaluator::new(test_cfg());
        let bias_sigs = ev.set_bias(bearish_bias());
        assert_eq!(bias_sigs.len(), 1);
        assert_eq!(bias_sigs[0].id, SignalId::EightyPercentBearish);

        let (sigs, decision) = feed_divergence_script(&mut ev);
        assert!(sigs.iter().any(|s| s.id == SignalId::CvdBearishExhaustion));
        match decision {
            TradingDecision::Enter {
                direction,
                price,
                initial_stop,
                size_multiplier,
                ..
            } => {
                assert_eq!(direction, Direction::Bearish);
                assert_eq!(price, 111.0);
                assert!(initial_stop > price, "short stop above entry");
                assert_eq!(size_multiplier, 1.0);
            }
            other => panic!("expected Enter, got {other:?}"),
        }
        let pos = ev.state().position.expect("advisory position open");
        assert_eq!(pos.direction, Direction::Bearish);
        assert_eq!(pos.entry_price, 111.0);
    }

    #[test]
    fn divergence_without_bias_no_entry() {
        let mut ev = SignalEvaluator::new(test_cfg());
        let (sigs, decision) = feed_divergence_script(&mut ev);
        assert!(sigs.iter().any(|s| s.id == SignalId::CvdBearishExhaustion));
        assert_eq!(decision, TradingDecision::None);
        assert!(ev.state().position.is_none());
    }

    #[test]
    fn divergence_with_opposing_bias_no_entry() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(SessionBias {
            direction: Direction::Bullish,
            target: 1305.0,
            ..bearish_bias()
        });
        let (_, decision) = feed_divergence_script(&mut ev);
        assert_eq!(decision, TradingDecision::None);
        assert!(ev.state().position.is_none());
    }

    #[test]
    fn default_config_is_sig001_gated_with_base_qty_2() {
        let cfg = SignalConfig::default();
        assert_eq!(cfg.entry_mode, EntryMode::Sig001Gated);
        assert_eq!(cfg.base_qty, 2);
    }

    #[test]
    fn pure_cvd_enters_on_divergence_without_bias() {
        // the same script yields no entry under Sig001Gated (divergence_without_bias_no_entry)
        let cfg = SignalConfig {
            entry_mode: EntryMode::PureCvd,
            ..test_cfg()
        };
        let mut ev = SignalEvaluator::new(cfg);
        let (sigs, decision) = feed_divergence_script(&mut ev);
        assert!(sigs.iter().any(|s| s.id == SignalId::CvdBearishExhaustion));
        match decision {
            TradingDecision::Enter {
                direction,
                price,
                initial_stop,
                size_multiplier,
                ..
            } => {
                assert_eq!(direction, Direction::Bearish);
                assert_eq!(price, 111.0);
                assert!(initial_stop > price, "short stop above entry");
                assert_eq!(size_multiplier, 1.0, "PureCvd = full size (no haircut)");
            }
            other => panic!("PureCvd should Enter without bias, got {other:?}"),
        }
        assert!(ev.state().position.is_some());
    }

    #[test]
    fn elevated_regime_halves_size() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        // Buy excess 0.6/bucket → VPIN 0.6 (Elevated), cvd +6/bar → 66.
        for i in 0..11 {
            feed_bar(&mut ev, i, 100.0 + i as f64, 8.0, 2.0);
        }
        // Divergence bar: sell 25 → cvd 41 <= 66*0.7=46.2, strength 0.379.
        // VPIN window: 0.6,0.6,0.6,0.6,1.0 → 0.68 still Elevated.
        let ts = 11 * 60;
        ev.on_tick(111.0, 25.0, "S", ts);
        let (_, decision) = ev.on_tick(111.0, 1.0, "B", 12 * 60);
        match decision {
            TradingDecision::Enter {
                size_multiplier, ..
            } => assert_eq!(size_multiplier, 0.5),
            other => panic!("expected Enter, got {other:?}"),
        }
    }

    #[test]
    fn full_lifecycle_trail_then_exit() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        let (_, decision) = feed_divergence_script(&mut ev);
        let initial_stop = match decision {
            TradingDecision::Enter { initial_stop, .. } => initial_stop,
            other => panic!("expected Enter, got {other:?}"),
        };

        // Favorable move down → trailing stop ratchets down (short).
        let mut stops = Vec::new();
        for (j, price) in [108.0, 105.0, 102.0].iter().enumerate() {
            let ts = (13 + j as i64) * 60;
            let (sigs, d) = ev.on_tick(*price, 2.0, "S", ts);
            assert!(
                !matches!(d, TradingDecision::Exit { .. }),
                "no exit during favorable move"
            );
            stops.extend(
                sigs.iter()
                    .filter(|s| s.id == SignalId::TrailingStopUpdate)
                    .filter_map(|s| s.stop),
            );
        }
        assert!(!stops.is_empty(), "trailing stop updates emitted");
        assert!(
            stops.windows(2).all(|w| w[1] <= w[0]),
            "short stops monotonically non-increasing: {stops:?}"
        );
        let last_stop = *stops.last().unwrap();
        assert!(last_stop < initial_stop);

        // Adverse rally through the stop → exit.
        let (sigs, d) = ev.on_tick(last_stop + 0.1, 2.0, "B", 17 * 60);
        match d {
            TradingDecision::Exit { reason, stop, .. } => {
                assert_eq!(reason, "trailing_stop");
                assert_eq!(stop, last_stop);
            }
            other => panic!("expected Exit, got {other:?}"),
        }
        assert!(sigs.iter().any(|s| s.id == SignalId::TrailingStopExit));
        assert!(ev.state().position.is_none(), "position cleared after hit");
    }

    #[test]
    fn no_second_entry_while_position_open() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        let (_, decision) = feed_divergence_script(&mut ev);
        assert!(matches!(decision, TradingDecision::Enter { .. }));
        let entry_ts = ev.state().position.unwrap().entry_ts;

        // another divergence-shaped sequence that stays below the short stop
        for i in 12..15 {
            feed_bar(&mut ev, i, 111.0, 1.0, 9.0);
        }
        let (_, d) = ev.on_tick(111.0, 1.0, "B", 15 * 60);
        assert!(
            !matches!(d, TradingDecision::Enter { .. }),
            "single advisory position only, got {d:?}"
        );
        assert_eq!(ev.state().position.unwrap().entry_ts, entry_ts);
    }

    #[test]
    fn lunch_rollover_preserves_bias_vpin_position_rolls_cvd() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        let (_, decision) = feed_divergence_script(&mut ev);
        assert!(matches!(decision, TradingDecision::Enter { .. }));
        let before = ev.state();

        let signals = ev.session_reset(SessionResetKind::LunchRollover);
        assert!(signals.is_empty());
        let after = ev.state();
        assert_eq!(after.bias, before.bias);
        assert!(after.position.is_some());
        assert_eq!(after.vpin, before.vpin);
        // CVD value carries across lunch (rollover), history restarts.
        assert_eq!(after.cvd, before.cvd);
    }

    #[test]
    fn day_start_force_exits_and_resets() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        let (_, decision) = feed_divergence_script(&mut ev);
        assert!(matches!(decision, TradingDecision::Enter { .. }));

        let signals = ev.session_reset(SessionResetKind::DayStart);
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].id, SignalId::TrailingStopExit);
        assert_eq!(signals[0].note, "day_start_force_exit");
        let state = ev.state();
        assert!(state.position.is_none());
        assert!(state.bias.is_none());
        assert_eq!(state.cvd, 0.0);
        assert_eq!(state.vpin, 0.0);
        assert_eq!(state.regime, VpinRegime::Normal);
        // Bias can be set again next day (EightyPercent trigger re-armed).
        let sigs = ev.set_bias(bearish_bias());
        assert_eq!(sigs.len(), 1);
    }

    #[test]
    fn nan_tick_leaves_state_untouched() {
        let mut ev = SignalEvaluator::new(test_cfg());
        ev.set_bias(bearish_bias());
        feed_uptrend(&mut ev, 3);
        let before = ev.state();
        let (sigs, d) = ev.on_tick(f64::NAN, 5.0, "B", 500);
        assert!(sigs.is_empty());
        assert_eq!(d, TradingDecision::None);
        let after = ev.state();
        assert_eq!(after.cvd, before.cvd);
        assert_eq!(after.vpin, before.vpin);
        // NaN volume is neutralized by the delta guard.
        let (_, d) = ev.on_tick(103.0, f64::NAN, "B", 501);
        assert_eq!(d, TradingDecision::None);
        assert_eq!(ev.state().cvd, before.cvd);
    }

    #[test]
    fn bar_builder_boundaries_match_epoch_floor() {
        let mut bb = BarBuilder::new(900);
        // 09:00:00 ICT, epoch 1783296000 (a multiple of 900)
        let t0 = 1_783_296_000_i64;
        assert!(bb.on_tick(100.0, 1.0, t0).is_none());
        assert!(bb.on_tick(101.0, 2.0, t0 + 899).is_none());
        // 09:15:00 opens a new window → completed bar [09:00, 09:15).
        let bar = bb.on_tick(102.0, 3.0, t0 + 900).expect("completed bar");
        assert_eq!(bar.timestamp, t0);
        assert_eq!(bar.high, 101.0);
        assert_eq!(bar.low, 100.0);
        assert_eq!(bar.close, 101.0);
        assert_eq!(bar.cvd, 2.0);
    }
}
