//! Property tests for `orderflow_engine::vpin::VpinCalculator` through its public API.
//!
//! Covers the [0, 1] range, threshold-consistent and monotonic regimes, volume conservation
//! across buckets, agreement with an O(n) shadow model, and rejection of invalid input.

use orderflow_engine::vpin::{VpinCalculator, VpinRegime};
use proptest::prelude::*;
use std::collections::VecDeque;

const TOL: f64 = 1e-9;

/// Finite trade volume component (contracts).
fn finite_vol() -> impl Strategy<Value = f64> {
    0.0f64..200.0
}

/// Volume component mixing finite values with invalid ones.
fn any_vol() -> impl Strategy<Value = f64> {
    prop_oneof![
        5 => 0.0f64..200.0,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
        1 => Just(f64::NEG_INFINITY),
        1 => -100.0f64..0.0,
    ]
}

fn regime_rank(r: VpinRegime) -> u8 {
    match r {
        VpinRegime::Normal => 0,
        VpinRegime::Elevated => 1,
        VpinRegime::Toxic => 2,
    }
}

/// O(n) reference model that recomputes the mean each step, exposing any running-sum drift.
struct ShadowVpin {
    bucket_size: f64,
    n_buckets: usize,
    buy: f64,
    sell: f64,
    window: VecDeque<f64>,
}

impl ShadowVpin {
    fn new(bucket_size: f64, n_buckets: usize) -> Self {
        Self {
            bucket_size,
            n_buckets,
            buy: 0.0,
            sell: 0.0,
            window: VecDeque::new(),
        }
    }

    fn sanitize(v: f64) -> f64 {
        if v.is_finite() && v > 0.0 {
            v
        } else {
            0.0
        }
    }

    fn add(&mut self, buy_vol: f64, sell_vol: f64) -> (f64, bool) {
        self.buy += Self::sanitize(buy_vol);
        self.sell += Self::sanitize(sell_vol);
        let mut completed = false;
        if self.buy + self.sell >= self.bucket_size {
            let abs_delta =
                ((self.buy - self.sell).abs() / self.bucket_size.max(1.0)).clamp(0.0, 1.0);
            self.window.push_back(abs_delta);
            if self.window.len() > self.n_buckets {
                self.window.pop_front();
            }
            self.buy = 0.0;
            self.sell = 0.0;
            completed = true;
        }
        (self.vpin(), completed)
    }

    fn vpin(&self) -> f64 {
        if self.window.is_empty() {
            0.0
        } else {
            self.window.iter().sum::<f64>() / self.window.len() as f64
        }
    }
}

proptest! {
    /// VPIN stays finite and within [0, 1] after every trade, including invalid inputs.
    #[test]
    fn vpin_always_in_unit_interval(
        bucket_size in 1.0f64..100.0,
        n_buckets in 1usize..30,
        trades in prop::collection::vec((any_vol(), any_vol()), 0..300),
    ) {
        let mut vpin = VpinCalculator::new(bucket_size, n_buckets);
        for (buy, sell) in trades {
            let (v, _) = vpin.add_trade(buy, sell);
            prop_assert!(v.is_finite(), "VPIN not finite: {v}");
            prop_assert!((0.0..=1.0).contains(&v), "VPIN {v} outside [0,1]");
            prop_assert!(vpin.current_vpin().is_finite());
        }
    }

    /// The engine matches the shadow model at every step: VPIN (within 1e-9), completion flag
    /// and window length.
    #[test]
    fn engine_matches_independent_shadow_model(
        bucket_size in 1.0f64..100.0,
        n_buckets in 1usize..20,
        trades in prop::collection::vec((finite_vol(), finite_vol()), 0..300),
    ) {
        let mut engine = VpinCalculator::new(bucket_size, n_buckets);
        let mut shadow = ShadowVpin::new(bucket_size, n_buckets);
        for (i, (buy, sell)) in trades.iter().enumerate() {
            let (ev, ec) = engine.add_trade(*buy, *sell);
            let (sv, sc) = shadow.add(*buy, *sell);
            prop_assert_eq!(ec, sc, "completed flag diverged at trade {}", i);
            prop_assert!(
                (ev - sv).abs() <= TOL,
                "VPIN diverged at trade {}: engine {} shadow {}",
                i, ev, sv
            );
            prop_assert_eq!(engine.completed_buckets(), shadow.window.len());
        }
    }

    /// Each completed bucket holds at least `bucket_size`, the remainder stays below it, and
    /// bucketed volume plus remainder equals the total input.
    #[test]
    fn bucket_rotation_conserves_volume(
        bucket_size in 1.0f64..100.0,
        n_buckets in 1usize..20,
        trades in prop::collection::vec((finite_vol(), finite_vol()), 0..300),
    ) {
        let mut vpin = VpinCalculator::new(bucket_size, n_buckets);
        let mut acc = 0.0f64; // sanitized volume since last completed bucket
        let mut bucketed = 0.0f64; // total volume locked into completed buckets
        let mut total = 0.0f64;
        for (buy, sell) in &trades {
            let vol = buy + sell; // finite_vol() is already >= 0 and finite
            total += vol;
            acc += vol;
            let (_, completed) = vpin.add_trade(*buy, *sell);
            if completed {
                prop_assert!(
                    acc >= bucket_size - TOL,
                    "bucket closed with only {acc} < bucket_size {bucket_size}"
                );
                bucketed += acc;
                acc = 0.0;
            } else {
                prop_assert!(
                    acc < bucket_size + TOL,
                    "accumulated {acc} >= bucket_size {bucket_size} without completion"
                );
            }
        }
        prop_assert!(
            (bucketed + acc - total).abs() <= TOL * (1.0 + total.abs()),
            "volume not conserved: bucketed {bucketed} + partial {acc} != total {total}"
        );
    }

    /// Regimes follow the strict `>` thresholds (0.5, 0.7) and stay Normal until warm.
    #[test]
    fn regime_consistent_with_thresholds(
        bucket_size in 1.0f64..50.0,
        n_buckets in 1usize..40,
        trades in prop::collection::vec((finite_vol(), finite_vol()), 0..300),
    ) {
        let mut vpin = VpinCalculator::new(bucket_size, n_buckets);
        for (buy, sell) in trades {
            vpin.add_trade(buy, sell);
            let v = vpin.current_vpin();
            match vpin.regime() {
                VpinRegime::Toxic => {
                    prop_assert!(vpin.is_warm(), "Toxic while cold");
                    prop_assert!(v > 0.7 - TOL, "Toxic with VPIN {v} <= 0.7");
                }
                VpinRegime::Elevated => {
                    prop_assert!(vpin.is_warm(), "Elevated while cold");
                    prop_assert!(v > 0.5 - TOL && v <= 0.7 + TOL, "Elevated with VPIN {v}");
                }
                VpinRegime::Normal => {
                    prop_assert!(!vpin.is_warm() || v <= 0.5 + TOL, "Normal with VPIN {v}");
                }
            }
        }
    }

    /// A higher per-bucket imbalance never maps to a lower regime.
    #[test]
    fn regime_monotonic_in_imbalance(r1 in 0.0f64..=1.0, r2 in 0.0f64..=1.0) {
        let (lo, hi) = if r1 <= r2 { (r1, r2) } else { (r2, r1) };
        let build = |ratio: f64| {
            let mut v = VpinCalculator::new(10.0, 5); // warm at 5 buckets
            for _ in 0..5 {
                // Each trade fills exactly one bucket with abs_delta == ratio.
                v.add_trade(5.0 + 5.0 * ratio, 5.0 - 5.0 * ratio);
            }
            v
        };
        let (v_lo, v_hi) = (build(lo), build(hi));
        prop_assert!(v_lo.is_warm() && v_hi.is_warm());
        prop_assert!(
            regime_rank(v_lo.regime()) <= regime_rank(v_hi.regime()),
            "imbalance {} -> {:?} but higher imbalance {} -> {:?}",
            lo, v_lo.regime(), hi, v_hi.regime()
        );
    }

    /// NaN, infinite and negative volumes count as 0: no state change and no bucket completion.
    #[test]
    fn garbage_inputs_never_mutate_state(
        warmup in prop::collection::vec((finite_vol(), finite_vol()), 0..60),
        garbage in prop::collection::vec(
            prop_oneof![
                Just((f64::NAN, f64::NAN)),
                Just((f64::INFINITY, -1.0)),
                Just((f64::NEG_INFINITY, f64::INFINITY)),
                Just((-50.0, -0.001)),
            ],
            1..8,
        ),
    ) {
        let mut vpin = VpinCalculator::new(10.0, 5);
        for (buy, sell) in warmup {
            vpin.add_trade(buy, sell);
        }
        let before_vpin = vpin.current_vpin();
        let before_buckets = vpin.completed_buckets();
        let before_regime = vpin.regime();
        for (buy, sell) in garbage {
            let (v, completed) = vpin.add_trade(buy, sell);
            prop_assert!(!completed, "garbage trade completed a bucket");
            prop_assert!((v - before_vpin).abs() <= TOL);
            prop_assert_eq!(vpin.completed_buckets(), before_buckets);
            prop_assert_eq!(vpin.regime(), before_regime);
        }
    }
}

/// With `bucket_size < 1` the divisor floors at 1, so a one-sided bucket reports `bucket_size`.
#[test]
fn bucket_size_below_one_caps_vpin_at_bucket_size() {
    let mut vpin = VpinCalculator::new(0.5, 5);
    let (_, completed) = vpin.add_trade(0.5, 0.0); // 100% buy bucket
    assert!(completed);
    assert!(
        (vpin.current_vpin() - 0.5).abs() <= TOL,
        "expected VPIN 0.5 (|0.5-0|/max(0.5,1.0)), got {}",
        vpin.current_vpin()
    );
}

/// `reset()` returns to a cold state (no buckets, VPIN 0, Normal) that re-warms cleanly.
#[test]
fn reset_returns_to_cold_and_rewarm_works() {
    let mut vpin = VpinCalculator::new(10.0, 5);
    for _ in 0..6 {
        vpin.add_trade(10.0, 0.0);
    }
    assert!(vpin.is_warm());
    assert_eq!(vpin.regime(), VpinRegime::Toxic);

    vpin.reset();
    assert_eq!(vpin.completed_buckets(), 0);
    assert_eq!(vpin.current_vpin(), 0.0);
    assert!(!vpin.is_warm());
    assert_eq!(vpin.regime(), VpinRegime::Normal);

    for _ in 0..5 {
        vpin.add_trade(5.0, 5.0);
    }
    assert!(vpin.is_warm());
    assert_eq!(vpin.regime(), VpinRegime::Normal);
    assert!(vpin.current_vpin().abs() <= TOL);
}
