//! Cumulative volume delta (CVD): the running sum of tick deltas since the session start.
//!
//! Also hosts the bar-based CVD exhaustion `DivergenceDetector`, which mirrors the Python
//! research formulas (see `docs/knowledge/05-order-flow-features-and-parity.md`).

use std::collections::VecDeque;

/// A single point on the CVD curve.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CvdPoint {
    pub timestamp: i64,
    pub value: f64,
}

/// Running cumulative delta with a bounded `(timestamp, value)` history for charting.
#[derive(Debug, Clone)]
pub struct CvdTracker {
    pub cumulative_delta: f64,
    pub history: VecDeque<CvdPoint>,
    max_points: usize,
    /// Base value carried across sessions by `rollover` (0 after `reset`).
    pub initial_value: f64,
    /// Cached min and max of the history, so rendering avoids an O(n) scan.
    pub min_value: f64,
    pub max_value: f64,
}

impl CvdTracker {
    pub fn new(max_points: usize) -> Self {
        Self {
            cumulative_delta: 0.0,
            history: VecDeque::with_capacity(max_points.min(100)),
            max_points,
            initial_value: 0.0,
            min_value: 0.0,
            max_value: 0.0,
        }
    }

    /// Add a tick delta and return the new CVD; a non-finite delta is ignored.
    pub fn update(&mut self, delta: f64, timestamp: i64) -> f64 {
        if !delta.is_finite() {
            return self.current_value();
        }
        self.cumulative_delta += delta;
        let value = self.initial_value + self.cumulative_delta;
        self.history.push_back(CvdPoint { timestamp, value });
        if self.history.len() == 1 {
            self.min_value = value;
            self.max_value = value;
        } else {
            if value < self.min_value {
                self.min_value = value;
            }
            if value > self.max_value {
                self.max_value = value;
            }
        }
        if self.history.len() > self.max_points {
            if let Some(evicted) = self.history.pop_front() {
                // recompute the bounds only when an extremum was evicted
                if (evicted.value - self.min_value).abs() < f64::EPSILON
                    || (evicted.value - self.max_value).abs() < f64::EPSILON
                {
                    self.recompute_bounds();
                }
            }
        }
        value
    }

    fn recompute_bounds(&mut self) {
        let Some(first) = self.history.front() else {
            self.min_value = 0.0;
            self.max_value = 0.0;
            return;
        };
        let first = first.value;
        let (min, max) = self.history.iter().fold((first, first), |(mn, mx), p| {
            (mn.min(p.value), mx.max(p.value))
        });
        self.min_value = min;
        self.max_value = max;
    }

    /// Reset CVD to zero and clear history.
    pub fn reset(&mut self) {
        self.cumulative_delta = 0.0;
        self.history.clear();
        self.initial_value = 0.0;
        self.min_value = 0.0;
        self.max_value = 0.0;
    }

    /// Start a new session segment: keep the current value as the base, clear the history.
    pub fn rollover(&mut self) {
        self.initial_value += self.cumulative_delta;
        self.cumulative_delta = 0.0;
        self.history.clear();
        self.min_value = 0.0;
        self.max_value = 0.0;
    }

    /// Number of history points stored.
    pub fn len(&self) -> usize {
        self.history.len()
    }

    pub fn is_empty(&self) -> bool {
        self.history.is_empty()
    }

    /// Current total CVD value.
    pub fn current_value(&self) -> f64 {
        self.initial_value + self.cumulative_delta
    }
}

impl Default for CvdTracker {
    fn default() -> Self {
        Self::new(10_000)
    }
}

/// Direction of a CVD exhaustion divergence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DivergenceType {
    /// Price at rolling high while CVD collapsed from its rolling high.
    BearishExhaustion,
    /// Price at rolling low while CVD recovered from its rolling low.
    BullishExhaustion,
}

/// A detected CVD exhaustion divergence on a completed bar.
#[derive(Debug, Clone, Copy)]
pub struct CvdDivergence {
    pub divergence_type: DivergenceType,
    /// Relative CVD collapse or recovery, capped at 1.0.
    pub strength: f64,
    /// Bar close at divergence.
    pub price: f64,
    /// CVD at divergence.
    pub cvd: f64,
    pub timestamp: i64,
}

/// One completed price and CVD bar, as consumed by [`DivergenceDetector`].
#[derive(Debug, Clone, Copy)]
pub struct DivBar {
    pub timestamp: i64,
    pub close: f64,
    pub high: f64,
    pub low: f64,
    pub cvd: f64,
}

impl DivBar {
    fn is_valid(&self) -> bool {
        self.close.is_finite()
            && self.high.is_finite()
            && self.low.is_finite()
            && self.cvd.is_finite()
            && self.high >= self.low
    }
}

/// Bar-based CVD exhaustion detector, mirroring the Python research formulas.
///
/// The window is the last `lookback` bars including the current one (pandas
/// `rolling(window=lookback)`); detection starts after `lookback + 2` valid bars.
#[derive(Debug, Clone)]
pub struct DivergenceDetector {
    /// Last `lookback` bars, current included.
    bars: VecDeque<DivBar>,
    /// Total valid bars ever fed (for warmup gating).
    bars_seen: usize,
    lookback: usize,
    cvd_threshold: f64,
    min_strength: f64,
}

impl DivergenceDetector {
    /// `lookback` is at least 1; a non-finite threshold falls back to 0.3.
    pub fn new(lookback: usize, cvd_threshold: f64, min_strength: f64) -> Self {
        Self {
            bars: VecDeque::with_capacity(lookback.max(1)),
            bars_seen: 0,
            lookback: lookback.max(1),
            cvd_threshold: if cvd_threshold.is_finite() {
                cvd_threshold
            } else {
                0.3
            },
            min_strength: if min_strength.is_finite() {
                min_strength
            } else {
                0.3
            },
        }
    }

    /// Feed one completed bar; returns a divergence if detected. Invalid bars are ignored.
    pub fn on_bar(&mut self, bar: DivBar) -> Option<CvdDivergence> {
        if !bar.is_valid() {
            return None;
        }
        self.bars.push_back(bar);
        if self.bars.len() > self.lookback {
            self.bars.pop_front();
        }
        self.bars_seen += 1;
        // the Python loop starts at row lookback + 1, i.e. after lookback + 2 bars
        if self.bars_seen < self.lookback + 2 {
            return None;
        }

        let mut max_close = f64::NEG_INFINITY;
        let mut min_close = f64::INFINITY;
        let mut max_cvd = f64::NEG_INFINITY;
        let mut min_cvd = f64::INFINITY;
        for b in &self.bars {
            max_close = max_close.max(b.close);
            min_close = min_close.min(b.close);
            max_cvd = max_cvd.max(b.cvd);
            min_cvd = min_cvd.min(b.cvd);
        }

        // bearish: price at the rolling high, CVD down > threshold from a positive rolling high
        if bar.close >= max_close * 0.999
            && bar.cvd <= max_cvd * (1.0 - self.cvd_threshold)
            && max_cvd > 0.0
        {
            let strength = ((max_cvd - bar.cvd).abs() / max_cvd.abs().max(1.0)).min(1.0);
            if strength >= self.min_strength {
                return Some(CvdDivergence {
                    divergence_type: DivergenceType::BearishExhaustion,
                    strength,
                    price: bar.close,
                    cvd: bar.cvd,
                    timestamp: bar.timestamp,
                });
            }
            return None;
        }

        // bullish: price at the rolling low, CVD up > threshold from a negative rolling low
        if bar.close <= min_close * 1.001
            && bar.cvd >= min_cvd * (1.0 + self.cvd_threshold)
            && min_cvd < 0.0
        {
            let strength = ((bar.cvd - min_cvd).abs() / min_cvd.abs().max(1.0)).min(1.0);
            if strength >= self.min_strength {
                return Some(CvdDivergence {
                    divergence_type: DivergenceType::BullishExhaustion,
                    strength,
                    price: bar.close,
                    cvd: bar.cvd,
                    timestamp: bar.timestamp,
                });
            }
        }
        None
    }

    /// Session start: clear all state.
    pub fn reset(&mut self) {
        self.bars.clear();
        self.bars_seen = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn update_adds_delta_to_cumulative() {
        let mut cvd = CvdTracker::new(100);
        assert_eq!(cvd.update(10.0, 1000), 10.0);
        assert_eq!(cvd.update(-5.0, 1001), 5.0);
        assert_eq!(cvd.cumulative_delta, 5.0);
    }

    #[test]
    fn history_bounded_by_max_points() {
        let mut cvd = CvdTracker::new(3);
        cvd.update(1.0, 0);
        cvd.update(2.0, 1);
        cvd.update(3.0, 2);
        assert_eq!(cvd.history.len(), 3);
        cvd.update(4.0, 3);
        assert_eq!(cvd.history.len(), 3);
        assert_eq!(cvd.history[0].value, 3.0); // oldest evicted, cum=1+2
        assert_eq!(cvd.history[2].value, 10.0); // 1+2+3+4
    }

    #[test]
    fn reset_clears_everything() {
        let mut cvd = CvdTracker::new(100);
        cvd.update(10.0, 0);
        cvd.update(5.0, 1);
        cvd.reset();
        assert_eq!(cvd.cumulative_delta, 0.0);
        assert!(cvd.history.is_empty());
        assert_eq!(cvd.initial_value, 0.0);
    }

    #[test]
    fn rollover_preserves_value_across_sessions() {
        let mut cvd = CvdTracker::new(100);
        cvd.update(10.0, 0);
        cvd.update(5.0, 1);
        assert_eq!(cvd.current_value(), 15.0);
        assert_eq!(cvd.history.len(), 2);

        cvd.rollover();
        assert_eq!(cvd.cumulative_delta, 0.0);
        assert_eq!(cvd.initial_value, 15.0);
        assert!(cvd.history.is_empty());
        assert_eq!(cvd.current_value(), 15.0);

        cvd.update(-3.0, 100);
        assert_eq!(cvd.current_value(), 12.0);
    }

    #[test]
    fn empty_tracker_has_zero_len() {
        let cvd = CvdTracker::new(100);
        assert!(cvd.is_empty());
        assert_eq!(cvd.len(), 0);
    }

    #[test]
    fn default_max_points_is_10000() {
        let cvd = CvdTracker::default();
        assert_eq!(cvd.max_points, 10_000);
    }

    fn bar(ts: i64, close: f64, cvd: f64) -> DivBar {
        DivBar {
            timestamp: ts,
            close,
            high: close,
            low: close,
            cvd,
        }
    }

    #[test]
    fn bearish_exhaustion_detected() {
        // lookback=3: window = last 3 bars incl current. At bar 5:
        // max_close=104 (close==max), max_cvd=58, cvd=30 <= 58*0.7=40.6,
        // strength=|58-30|/58=0.483 >= 0.3.
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        assert!(det.on_bar(bar(1, 100.0, 50.0)).is_none());
        assert!(det.on_bar(bar(2, 101.0, 60.0)).is_none());
        assert!(det.on_bar(bar(3, 102.0, 55.0)).is_none());
        assert!(det.on_bar(bar(4, 103.0, 58.0)).is_none());
        let div = det.on_bar(bar(5, 104.0, 30.0)).expect("divergence");
        assert_eq!(div.divergence_type, DivergenceType::BearishExhaustion);
        assert!((div.strength - (58.0 - 30.0) / 58.0).abs() < 1e-12);
        assert_eq!(div.price, 104.0);
        assert_eq!(div.cvd, 30.0);
        assert_eq!(div.timestamp, 5);
    }

    #[test]
    fn bullish_exhaustion_detected() {
        // Mirror of bearish: min_cvd=-58, cvd=-30 >= -58*1.3=-75.4,
        // strength=|-30-(-58)|/58=0.483.
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        det.on_bar(bar(1, 100.0, -50.0));
        det.on_bar(bar(2, 99.0, -60.0));
        det.on_bar(bar(3, 98.0, -55.0));
        det.on_bar(bar(4, 97.0, -58.0));
        let div = det.on_bar(bar(5, 96.0, -30.0)).expect("divergence");
        assert_eq!(div.divergence_type, DivergenceType::BullishExhaustion);
        assert!((div.strength - 28.0 / 58.0).abs() < 1e-12);
    }

    #[test]
    fn aligned_price_and_cvd_no_divergence() {
        // price and CVD both make higher highs
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        det.on_bar(bar(1, 100.0, 10.0));
        det.on_bar(bar(2, 101.0, 20.0));
        det.on_bar(bar(3, 102.0, 30.0));
        det.on_bar(bar(4, 103.0, 40.0));
        assert!(det.on_bar(bar(5, 104.0, 50.0)).is_none());
    }

    #[test]
    fn warmup_returns_none() {
        // Same shape as bearish case but shifted so the divergence bar is
        // the 4th bar (< lookback+2 = 5) → still warming up.
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        assert!(det.on_bar(bar(1, 101.0, 60.0)).is_none());
        assert!(det.on_bar(bar(2, 102.0, 55.0)).is_none());
        assert!(det.on_bar(bar(3, 103.0, 58.0)).is_none());
        assert!(det.on_bar(bar(4, 104.0, 30.0)).is_none());
    }

    #[test]
    fn strength_capped_at_one() {
        // max_cvd=0.5 → denominator max(0.5,1)=1; collapse to -5 →
        // raw strength 5.5, capped to 1.0.
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        det.on_bar(bar(1, 100.0, 0.2));
        det.on_bar(bar(2, 100.5, 0.3));
        det.on_bar(bar(3, 101.0, 0.4));
        det.on_bar(bar(4, 101.5, 0.5));
        let div = det.on_bar(bar(5, 102.0, -5.0)).expect("divergence");
        assert_eq!(div.strength, 1.0);
    }

    #[test]
    fn below_min_strength_suppressed() {
        // Collapse of ~31% passes cvd_threshold=0.3 barely but
        // min_strength=0.5 suppresses it.
        let mut det = DivergenceDetector::new(3, 0.3, 0.5);
        det.on_bar(bar(1, 100.0, 90.0));
        det.on_bar(bar(2, 101.0, 95.0));
        det.on_bar(bar(3, 102.0, 100.0));
        det.on_bar(bar(4, 103.0, 98.0));
        assert!(det.on_bar(bar(5, 104.0, 69.0)).is_none());
    }

    #[test]
    fn invalid_bars_ignored() {
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        det.on_bar(bar(1, 100.0, 50.0));
        det.on_bar(bar(2, 101.0, 60.0));
        det.on_bar(bar(3, 102.0, 55.0));
        det.on_bar(bar(4, 103.0, 58.0));
        // non-finite and inverted bars neither count toward warmup nor enter the window
        assert!(det.on_bar(bar(90, f64::NAN, 10.0)).is_none());
        assert!(det.on_bar(bar(91, 104.0, f64::INFINITY)).is_none());
        assert!(det
            .on_bar(DivBar {
                timestamp: 92,
                close: 104.0,
                high: 100.0,
                low: 105.0,
                cvd: 30.0,
            })
            .is_none());
        // the valid divergence bar still fires as in the clean case
        let div = det.on_bar(bar(5, 104.0, 30.0)).expect("divergence");
        assert_eq!(div.divergence_type, DivergenceType::BearishExhaustion);
    }

    #[test]
    fn detector_reset_restarts_warmup() {
        let mut det = DivergenceDetector::new(3, 0.3, 0.3);
        det.on_bar(bar(1, 100.0, 50.0));
        det.on_bar(bar(2, 101.0, 60.0));
        det.on_bar(bar(3, 102.0, 55.0));
        det.on_bar(bar(4, 103.0, 58.0));
        det.reset();
        // Post-reset the would-be divergence bar is bar #1 → warmup.
        assert!(det.on_bar(bar(5, 104.0, 30.0)).is_none());
    }
}
