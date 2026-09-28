//! Spread regime tracker: classifies each bid-ask spread as normal, wide or crossed.
//!
//! Scores a raw spread (as computed by `market-dom-engine`) against a rolling window.

use std::collections::VecDeque;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpreadRegime {
    /// Spread < 0: the book is crossed; flagged even before warm-up.
    Crossed,
    /// z-score of the current spread vs. the rolling window >= threshold.
    Wide,
    Normal,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SpreadReading {
    pub current: f64,
    pub rolling_mean: f64,
    pub rolling_stddev: f64,
    pub zscore: f64,
    pub regime: SpreadRegime,
}

/// Rolling mean, standard deviation and z-score of the spread over a fixed window.
///
/// Recomputed from the window on each update (O(window)) rather than as `E[x^2] - E[x]^2`,
/// which cancels catastrophically when the spread is nearly constant.
#[derive(Debug, Clone)]
pub struct SpreadTracker {
    window: usize,
    wide_threshold_stddev: f64,
    history: VecDeque<f64>,
}

impl SpreadTracker {
    /// `window` is at least 1; `wide_threshold_stddev` is the z-score at which a spread is `Wide`.
    pub fn new(window: usize, wide_threshold_stddev: f64) -> Self {
        let window = window.max(1);
        Self {
            window,
            wide_threshold_stddev,
            history: VecDeque::with_capacity(window),
        }
    }

    /// Add one spread in points (non-finite becomes 0.0) and return its reading.
    pub fn update(&mut self, spread: f64) -> SpreadReading {
        let spread = if spread.is_finite() { spread } else { 0.0 };
        self.history.push_back(spread);
        if self.history.len() > self.window {
            self.history.pop_front();
        }
        let n = self.history.len() as f64;
        let mean = self.history.iter().sum::<f64>() / n;
        let variance = self.history.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n;
        let stddev = variance.max(0.0).sqrt();
        let zscore = if stddev > 1e-9 {
            (spread - mean) / stddev
        } else {
            0.0
        };

        let min_warm = self.window.min(5);
        let is_warm = self.history.len() >= min_warm;
        let regime = if spread < 0.0 {
            SpreadRegime::Crossed
        } else if is_warm && zscore >= self.wide_threshold_stddev {
            SpreadRegime::Wide
        } else {
            SpreadRegime::Normal
        };

        SpreadReading {
            current: spread,
            rolling_mean: mean,
            rolling_stddev: stddev,
            zscore,
            regime,
        }
    }

    /// Session start: drop the rolling window.
    pub fn reset(&mut self) {
        self.history.clear();
    }
}

impl Default for SpreadTracker {
    /// 50-sample window, 2.0-stddev "wide" threshold.
    fn default() -> Self {
        Self::new(50, 2.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn negative_spread_is_always_crossed() {
        let mut tracker = SpreadTracker::new(10, 2.0);
        for _ in 0..3 {
            tracker.update(0.1); // build up a normal window
        }
        let reading = tracker.update(-0.5);
        assert_eq!(reading.regime, SpreadRegime::Crossed);
        assert_eq!(reading.current, -0.5);
    }

    #[test]
    fn constant_spread_never_flags_wide() {
        let mut tracker = SpreadTracker::new(20, 2.0);
        let mut last = SpreadReading {
            current: 0.0,
            rolling_mean: 0.0,
            rolling_stddev: 0.0,
            zscore: 0.0,
            regime: SpreadRegime::Normal,
        };
        for _ in 0..20 {
            last = tracker.update(0.1);
        }
        assert_eq!(last.regime, SpreadRegime::Normal);
        assert!(last.zscore.abs() < 1e-9);
    }

    #[test]
    fn sudden_widening_flags_wide_once_warm() {
        let mut tracker = SpreadTracker::new(20, 2.0);
        for _ in 0..10 {
            tracker.update(0.1);
        }
        let reading = tracker.update(5.0); // sharp widening
        assert_eq!(reading.regime, SpreadRegime::Wide, "{reading:?}");
        assert!(reading.zscore > 2.0);
    }

    #[test]
    fn cold_start_never_flags_wide_before_min_warm() {
        let mut tracker = SpreadTracker::new(20, 2.0);
        // First sample: stddev == 0 -> zscore forced to 0 -> Normal.
        let reading = tracker.update(0.1);
        assert_eq!(reading.regime, SpreadRegime::Normal);
    }

    #[test]
    fn nan_inf_spread_sanitized_to_zero() {
        let mut tracker = SpreadTracker::new(10, 2.0);
        let reading = tracker.update(f64::NAN);
        assert_eq!(reading.current, 0.0);
        let reading = tracker.update(f64::INFINITY);
        assert_eq!(reading.current, 0.0);
    }

    #[test]
    fn rolling_window_evicts_oldest() {
        let mut tracker = SpreadTracker::new(3, 2.0);
        tracker.update(1.0);
        tracker.update(1.0);
        tracker.update(1.0);
        let reading = tracker.update(1.0); // window now [1,1,1], oldest evicted
        assert!((reading.rolling_mean - 1.0).abs() < 1e-9);
        assert!(reading.rolling_stddev.abs() < 1e-9);
    }

    #[test]
    fn reset_returns_to_cold() {
        let mut tracker = SpreadTracker::new(10, 2.0);
        for _ in 0..10 {
            tracker.update(3.0);
        }
        tracker.reset();
        let reading = tracker.update(0.1);
        assert_eq!(reading.rolling_mean, 0.1);
        assert_eq!(reading.regime, SpreadRegime::Normal);
    }
}
