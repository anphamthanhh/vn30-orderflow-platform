//! Property tests for `orderflow_engine::spread::SpreadTracker`.
//!
//! Covers finite readings, `Crossed` exactly when the spread is negative, agreement with an
//! independent shadow model, and non-finite input read as 0.0.

use orderflow_engine::spread::{SpreadRegime, SpreadTracker};
use proptest::prelude::*;
use std::collections::VecDeque;

const TOL: f64 = 1e-6;

fn finite_spread() -> impl Strategy<Value = f64> {
    -2.0f64..10.0
}

fn any_spread() -> impl Strategy<Value = f64> {
    prop_oneof![
        5 => -2.0f64..10.0,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
        1 => Just(f64::NEG_INFINITY),
    ]
}

struct ShadowSpread {
    window: usize,
    values: VecDeque<f64>,
}

impl ShadowSpread {
    fn new(window: usize) -> Self {
        Self {
            window: window.max(1),
            values: VecDeque::new(),
        }
    }

    fn update(&mut self, spread: f64) -> (f64, f64, f64) {
        let spread = if spread.is_finite() { spread } else { 0.0 };
        self.values.push_back(spread);
        if self.values.len() > self.window {
            self.values.pop_front();
        }
        let n = self.values.len() as f64;
        let mean = self.values.iter().sum::<f64>() / n;
        let variance = self.values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n;
        let stddev = variance.max(0.0).sqrt();
        let zscore = if stddev > 1e-9 {
            (spread - mean) / stddev
        } else {
            0.0
        };
        (mean, stddev, zscore)
    }
}

proptest! {
    #[test]
    fn regime_crossed_iff_negative(
        window in 1usize..30,
        threshold in 0.5f64..4.0,
        spreads in prop::collection::vec(finite_spread(), 0..100),
    ) {
        let mut tracker = SpreadTracker::new(window, threshold);
        for s in spreads {
            let reading = tracker.update(s);
            prop_assert!(reading.current.is_finite());
            prop_assert!(reading.rolling_mean.is_finite());
            prop_assert!(reading.rolling_stddev.is_finite());
            prop_assert!(reading.zscore.is_finite());
            if s < 0.0 {
                prop_assert_eq!(reading.regime, SpreadRegime::Crossed);
            } else {
                prop_assert_ne!(reading.regime, SpreadRegime::Crossed);
            }
        }
    }

    #[test]
    fn matches_independent_shadow_model(
        window in 1usize..20,
        threshold in 0.5f64..4.0,
        spreads in prop::collection::vec(finite_spread(), 0..100),
    ) {
        let mut tracker = SpreadTracker::new(window, threshold);
        let mut shadow = ShadowSpread::new(window);
        for s in spreads {
            let reading = tracker.update(s);
            let (mean, stddev, zscore) = shadow.update(s);
            prop_assert!((reading.rolling_mean - mean).abs() <= TOL, "mean diverged");
            prop_assert!((reading.rolling_stddev - stddev).abs() <= TOL, "stddev diverged");
            prop_assert!((reading.zscore - zscore).abs() <= TOL, "zscore diverged");
        }
    }

    #[test]
    fn garbage_inputs_sanitize_to_zero(spreads in prop::collection::vec(any_spread(), 0..30)) {
        let mut tracker = SpreadTracker::new(10, 2.0);
        for s in spreads {
            let reading = tracker.update(s);
            if !s.is_finite() {
                prop_assert_eq!(reading.current, 0.0);
            }
        }
    }
}
