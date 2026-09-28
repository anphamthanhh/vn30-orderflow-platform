//! Stateful triggers that turn engine state into transition signals, one per change.

use orderflow_engine::cvd::{CvdDivergence, DivBar, DivergenceDetector, DivergenceType};
use orderflow_engine::vpin::{VpinCalculator, VpinRegime};

use super::evaluator::SessionBias;
use super::registry::{Direction, SignalId};

/// Read-only engine state passed to triggers on each tick; CVD arrives with the completed bar.
pub struct TriggerContext<'a> {
    pub timestamp: i64,
    /// Last trade price.
    pub price: f64,
    pub vpin: &'a VpinCalculator,
    pub bias: Option<&'a SessionBias>,
    /// `Some` only on the tick that completed a bar.
    pub bar_completed: Option<&'a DivBar>,
}

/// One emitted signal, ready for serialization.
#[derive(Debug, Clone)]
pub struct SignalResult {
    pub id: SignalId,
    pub direction: Direction,
    pub strength: f64,
    pub price: f64,
    pub timestamp: i64,
    /// Informational target (80% Rule: opposite VA edge).
    pub target: Option<f64>,
    /// Advisory stop level (trailing stop events).
    pub stop: Option<f64>,
    pub note: String,
}

/// A strategy trigger: evaluates engine state, emits transition signals.
pub trait TriggerContract: Send {
    fn evaluate(&mut self, ctx: &TriggerContext) -> Vec<SignalResult>;
    /// Day-start reset (trading-day boundary).
    fn reset(&mut self);
}

/// Tier 1: emits `VpinToxic` / `VpinElevated` once per regime change, nothing on return to Normal.
pub struct VpinFilterTrigger {
    last_regime: VpinRegime,
}

impl VpinFilterTrigger {
    pub fn new() -> Self {
        Self {
            last_regime: VpinRegime::Normal,
        }
    }
}

impl Default for VpinFilterTrigger {
    fn default() -> Self {
        Self::new()
    }
}

impl TriggerContract for VpinFilterTrigger {
    fn evaluate(&mut self, ctx: &TriggerContext) -> Vec<SignalResult> {
        let regime = ctx.vpin.regime();
        if regime == self.last_regime {
            return Vec::new();
        }
        self.last_regime = regime;
        let id = match regime {
            VpinRegime::Toxic => SignalId::VpinToxic,
            VpinRegime::Elevated => SignalId::VpinElevated,
            VpinRegime::Normal => return Vec::new(),
        };
        vec![SignalResult {
            id,
            direction: Direction::Neutral,
            strength: ctx.vpin.current_vpin(),
            price: ctx.price,
            timestamp: ctx.timestamp,
            target: None,
            stop: None,
            note: format!("vpin={:.3}", ctx.vpin.current_vpin()),
        }]
    }

    fn reset(&mut self) {
        self.last_regime = VpinRegime::Normal;
    }
}

/// Tier 3: CVD exhaustion divergence on completed bars, after a 10-bar strategy warmup.
pub struct CvdExhaustionTrigger {
    detector: DivergenceDetector,
    bars_fed: usize,
    min_warmup_bars: usize,
}

impl CvdExhaustionTrigger {
    pub fn new(lookback: usize, cvd_threshold: f64, min_strength: f64) -> Self {
        Self {
            detector: DivergenceDetector::new(lookback, cvd_threshold, min_strength),
            bars_fed: 0,
            min_warmup_bars: 10,
        }
    }

    fn to_signal(div: &CvdDivergence) -> SignalResult {
        let (id, direction) = match div.divergence_type {
            DivergenceType::BullishExhaustion => {
                (SignalId::CvdBullishExhaustion, Direction::Bullish)
            }
            DivergenceType::BearishExhaustion => {
                (SignalId::CvdBearishExhaustion, Direction::Bearish)
            }
        };
        SignalResult {
            id,
            direction,
            strength: div.strength,
            price: div.price,
            timestamp: div.timestamp,
            target: None,
            stop: None,
            note: format!("cvd={:.1}", div.cvd),
        }
    }
}

impl TriggerContract for CvdExhaustionTrigger {
    fn evaluate(&mut self, ctx: &TriggerContext) -> Vec<SignalResult> {
        let Some(bar) = ctx.bar_completed else {
            return Vec::new();
        };
        let divergence = self.detector.on_bar(*bar);
        self.bars_fed += 1;
        if self.bars_fed < self.min_warmup_bars {
            return Vec::new();
        }
        divergence
            .as_ref()
            .map(|d| vec![Self::to_signal(d)])
            .unwrap_or_default()
    }

    fn reset(&mut self) {
        self.detector.reset();
        self.bars_fed = 0;
    }
}

/// Tier 2: fires once when the 80% Rule session bias is set (the rule itself is
/// `market_profile_engine::eighty_percent::check_eighty_percent_rule`).
pub struct EightyPercentTrigger {
    fired: bool,
}

impl EightyPercentTrigger {
    pub fn new() -> Self {
        Self { fired: false }
    }
}

impl Default for EightyPercentTrigger {
    fn default() -> Self {
        Self::new()
    }
}

impl TriggerContract for EightyPercentTrigger {
    fn evaluate(&mut self, ctx: &TriggerContext) -> Vec<SignalResult> {
        if self.fired {
            return Vec::new();
        }
        let Some(bias) = ctx.bias else {
            return Vec::new();
        };
        let id = match bias.direction {
            Direction::Bullish => SignalId::EightyPercentBullish,
            Direction::Bearish => SignalId::EightyPercentBearish,
            Direction::Neutral => return Vec::new(),
        };
        self.fired = true;
        vec![SignalResult {
            id,
            direction: bias.direction,
            strength: bias.confidence,
            price: ctx.price,
            timestamp: ctx.timestamp,
            target: Some(bias.target),
            // position stops come from `TrailingStopManager`
            stop: None,
            note: format!("va=[{:.1},{:.1}]", bias.prior_val, bias.prior_vah),
        }]
    }

    fn reset(&mut self) {
        self.fired = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx<'a>(
        vpin: &'a VpinCalculator,
        bias: Option<&'a SessionBias>,
        bar: Option<&'a DivBar>,
        ts: i64,
    ) -> TriggerContext<'a> {
        TriggerContext {
            timestamp: ts,
            price: 1300.0,
            vpin,
            bias,
            bar_completed: bar,
        }
    }

    #[test]
    fn vpin_trigger_emits_once_per_crossing() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        let mut trig = VpinFilterTrigger::new();
        // Drive to warm+toxic (pure buy).
        for _ in 0..6 {
            vpin.add_trade(10.0, 0.0);
        }
        let s = trig.evaluate(&ctx(&vpin, None, None, 1));
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].id, SignalId::VpinToxic);
        // Same regime → no repeat.
        assert!(trig.evaluate(&ctx(&vpin, None, None, 2)).is_empty());
        // Balanced flow drags VPIN down to Normal → transition emits nothing.
        for _ in 0..5 {
            vpin.add_trade(5.0, 5.0);
        }
        assert!(trig.evaluate(&ctx(&vpin, None, None, 3)).is_empty());
        // Back up to toxic → emits again.
        for _ in 0..5 {
            vpin.add_trade(10.0, 0.0);
        }
        let s = trig.evaluate(&ctx(&vpin, None, None, 4));
        assert_eq!(s.len(), 1);
    }

    #[test]
    fn cvd_trigger_respects_strategy_warmup() {
        let vpin = VpinCalculator::default();
        // lookback=3: detector alone would fire at bar 5, but strategy
        // warmup requires 10 bars.
        let mut trig = CvdExhaustionTrigger::new(3, 0.3, 0.3);
        let mk = |ts: i64, close: f64, c: f64| DivBar {
            timestamp: ts,
            close,
            high: close,
            low: close,
            cvd: c,
        };
        // 9 benign rising bars (aligned price/cvd — no divergence anyway).
        for i in 0..9 {
            let bar = mk(i, 100.0 + i as f64, 10.0 * i as f64);
            assert!(trig.evaluate(&ctx(&vpin, None, Some(&bar), i)).is_empty());
        }
        // bar 10: new price high while CVD collapses; divergence and warmup both met
        let bar = mk(9, 110.0, 20.0);
        let s = trig.evaluate(&ctx(&vpin, None, Some(&bar), 9));
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].id, SignalId::CvdBearishExhaustion);
        assert_eq!(s[0].direction, Direction::Bearish);
    }

    #[test]
    fn eighty_trigger_fires_once() {
        let vpin = VpinCalculator::default();
        let mut trig = EightyPercentTrigger::new();
        assert!(trig.evaluate(&ctx(&vpin, None, None, 1)).is_empty());
        let bias = SessionBias {
            direction: Direction::Bearish,
            prior_vah: 1305.0,
            prior_val: 1295.0,
            target: 1295.0,
            confidence: 0.8,
            computed_at: 100,
        };
        let s = trig.evaluate(&ctx(&vpin, Some(&bias), None, 2));
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].id, SignalId::EightyPercentBearish);
        assert_eq!(s[0].target, Some(1295.0));
        // Fired → silent afterwards.
        assert!(trig.evaluate(&ctx(&vpin, Some(&bias), None, 3)).is_empty());
        // Reset re-arms.
        trig.reset();
        assert_eq!(trig.evaluate(&ctx(&vpin, Some(&bias), None, 4)).len(), 1);
    }
}
