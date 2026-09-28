//! VPIN: flow toxicity over volume-synchronized buckets (Easley, López de Prado and O'Hara, 2011).
//!
//! ```text
//! VPIN = Σ|BuyVol_b - SellVol_b| / (n × bucket_size)
//! ```
//!
//! A rolling window, O(1) per trade. It agrees with the batch formulation (whole session,
//! adaptive bucket size) at the regime level, not bit-for-bit.

use std::collections::VecDeque;

/// Flow-toxicity regime from the current VPIN; thresholds are strict `>` comparisons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VpinRegime {
    /// VPIN <= elevated threshold (or not enough buckets yet): trade normally.
    Normal,
    /// VPIN in (elevated, toxic]: reduce size / require confluence.
    Elevated,
    /// VPIN > toxic threshold: no trade.
    Toxic,
}

/// O(1) incremental VPIN over volume-synchronized buckets.
#[derive(Debug, Clone)]
pub struct VpinCalculator {
    /// Volume per bucket (e.g. 500 contracts).
    bucket_size: f64,
    /// Rolling window length in completed buckets.
    n_buckets: usize,
    toxic_threshold: f64,
    elevated_threshold: f64,
    current_bucket_buy: f64,
    current_bucket_sell: f64,
    /// `|buy-sell| / bucket_size` per completed bucket, clamped to [0,1].
    bucket_abs_deltas: VecDeque<f64>,
    /// Running sum of `bucket_abs_deltas` → O(1) `current_vpin`.
    sum_abs_deltas: f64,
}

impl VpinCalculator {
    /// A non-positive or non-finite `bucket_size` becomes 1.0 and `n_buckets` is at least 1.
    /// Thresholds are 0.7 (toxic) and 0.5 (elevated).
    pub fn new(bucket_size: f64, n_buckets: usize) -> Self {
        let bucket_size = if bucket_size.is_finite() && bucket_size > 0.0 {
            bucket_size
        } else {
            1.0
        };
        Self {
            bucket_size,
            n_buckets: n_buckets.max(1),
            toxic_threshold: 0.7,
            elevated_threshold: 0.5,
            current_bucket_buy: 0.0,
            current_bucket_sell: 0.0,
            bucket_abs_deltas: VecDeque::with_capacity(n_buckets.max(1)),
            sum_abs_deltas: 0.0,
        }
    }

    /// Add one trade's buy/sell volumes; returns `(current_vpin, bucket_completed)`.
    ///
    /// Non-finite or negative volumes count as 0. A bucket closes once it reaches
    /// `bucket_size`, and any overflow stays in the closed bucket.
    pub fn add_trade(&mut self, buy_vol: f64, sell_vol: f64) -> (f64, bool) {
        let buy = if buy_vol.is_finite() && buy_vol > 0.0 {
            buy_vol
        } else {
            0.0
        };
        let sell = if sell_vol.is_finite() && sell_vol > 0.0 {
            sell_vol
        } else {
            0.0
        };
        self.current_bucket_buy += buy;
        self.current_bucket_sell += sell;

        let mut completed = false;
        if self.current_bucket_buy + self.current_bucket_sell >= self.bucket_size {
            let abs_delta = ((self.current_bucket_buy - self.current_bucket_sell).abs()
                / self.bucket_size.max(1.0))
            .clamp(0.0, 1.0);
            self.bucket_abs_deltas.push_back(abs_delta);
            self.sum_abs_deltas += abs_delta;
            if self.bucket_abs_deltas.len() > self.n_buckets {
                if let Some(evicted) = self.bucket_abs_deltas.pop_front() {
                    self.sum_abs_deltas -= evicted;
                }
            }
            self.current_bucket_buy = 0.0;
            self.current_bucket_sell = 0.0;
            completed = true;
        }
        (self.current_vpin(), completed)
    }

    /// Add a completed bar's buy/sell volumes; same semantics as [`Self::add_trade`].
    pub fn add_bar(&mut self, buy_vol: f64, sell_vol: f64) -> (f64, bool) {
        self.add_trade(buy_vol, sell_vol)
    }

    /// Mean |buy-sell|/bucket_size over completed buckets; 0.0 when none.
    pub fn current_vpin(&self) -> f64 {
        if self.bucket_abs_deltas.is_empty() {
            return 0.0;
        }
        // Guard against negative drift from float subtraction on eviction.
        (self.sum_abs_deltas / self.bucket_abs_deltas.len() as f64).clamp(0.0, 1.0)
    }

    /// Warm once `max(n_buckets / 5, 5)` buckets have completed.
    pub fn is_warm(&self) -> bool {
        self.bucket_abs_deltas.len() >= (self.n_buckets / 5).max(5)
    }

    /// [`VpinRegime::Normal`] until warm; then strict `>` thresholds.
    pub fn regime(&self) -> VpinRegime {
        if !self.is_warm() {
            return VpinRegime::Normal;
        }
        let vpin = self.current_vpin();
        if vpin > self.toxic_threshold {
            VpinRegime::Toxic
        } else if vpin > self.elevated_threshold {
            VpinRegime::Elevated
        } else {
            VpinRegime::Normal
        }
    }

    /// Completed buckets currently in the rolling window.
    pub fn completed_buckets(&self) -> usize {
        self.bucket_abs_deltas.len()
    }

    /// Session start: drop all buckets and the partial accumulator.
    pub fn reset(&mut self) {
        self.current_bucket_buy = 0.0;
        self.current_bucket_sell = 0.0;
        self.bucket_abs_deltas.clear();
        self.sum_abs_deltas = 0.0;
    }
}

impl Default for VpinCalculator {
    /// VN30F1M defaults: 500-contract buckets, 50-bucket window.
    fn default() -> Self {
        Self::new(500.0, 50)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pure_buy_stream_converges_to_one() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        for _ in 0..10 {
            vpin.add_trade(10.0, 0.0);
        }
        assert!((vpin.current_vpin() - 1.0).abs() < 1e-12);
        assert!(vpin.is_warm());
        assert_eq!(vpin.regime(), VpinRegime::Toxic);
    }

    #[test]
    fn balanced_stream_converges_to_zero() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        for _ in 0..10 {
            vpin.add_trade(5.0, 5.0);
        }
        assert_eq!(vpin.current_vpin(), 0.0);
        assert_eq!(vpin.regime(), VpinRegime::Normal);
    }

    #[test]
    fn bucket_rotates_at_exact_bucket_size() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        let (v, completed) = vpin.add_trade(4.0, 4.0);
        assert!(!completed);
        assert_eq!(v, 0.0); // no completed bucket yet
        let (_, completed) = vpin.add_trade(1.0, 1.0); // total 10 == bucket_size
        assert!(completed);
        assert_eq!(vpin.completed_buckets(), 1);
    }

    #[test]
    fn rolling_window_evicts_oldest_and_sum_stays_consistent() {
        let mut vpin = VpinCalculator::new(10.0, 3);
        // 3 imbalanced buckets: abs_delta = 1.0 each.
        for _ in 0..3 {
            vpin.add_trade(10.0, 0.0);
        }
        assert!((vpin.current_vpin() - 1.0).abs() < 1e-12);
        // 3 balanced buckets fully evict the imbalanced ones.
        for _ in 0..3 {
            vpin.add_trade(5.0, 5.0);
        }
        assert_eq!(vpin.completed_buckets(), 3);
        assert_eq!(vpin.current_vpin(), 0.0);
        // Long-run consistency: after 3*n more buckets sum still matches mean.
        for i in 0..9 {
            let (buy, sell) = if i % 2 == 0 { (10.0, 0.0) } else { (0.0, 10.0) };
            vpin.add_trade(buy, sell);
        }
        assert!((vpin.current_vpin() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn oversized_trade_clamps_abs_delta() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        // Single 100% buy trade of 30 volume closes one bucket with
        // |30-0|/10 = 3.0 → clamped to 1.0.
        let (_, completed) = vpin.add_trade(30.0, 0.0);
        assert!(completed);
        assert!((vpin.current_vpin() - 1.0).abs() < 1e-12);
        assert_eq!(vpin.completed_buckets(), 1);
    }

    #[test]
    fn nan_inf_negative_inputs_treated_as_zero() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        vpin.add_trade(f64::NAN, f64::INFINITY);
        vpin.add_trade(-5.0, f64::NEG_INFINITY);
        assert_eq!(vpin.completed_buckets(), 0);
        assert_eq!(vpin.current_vpin(), 0.0);
        // Sane input still works afterwards.
        let (_, completed) = vpin.add_trade(10.0, 0.0);
        assert!(completed);
    }

    #[test]
    fn regime_normal_before_warm_even_if_toxic_values() {
        let mut vpin = VpinCalculator::new(10.0, 50); // warm at 10 buckets
        for _ in 0..9 {
            vpin.add_trade(10.0, 0.0);
        }
        assert!((vpin.current_vpin() - 1.0).abs() < 1e-12);
        assert!(!vpin.is_warm());
        assert_eq!(vpin.regime(), VpinRegime::Normal);
        vpin.add_trade(10.0, 0.0); // 10th bucket → warm
        assert_eq!(vpin.regime(), VpinRegime::Toxic);
    }

    #[test]
    fn regime_thresholds_are_strict() {
        // abs_delta 0.5 per bucket: buy 7.5 / sell 2.5 → |5|/10 = 0.5.
        let mut vpin = VpinCalculator::new(10.0, 5);
        for _ in 0..5 {
            vpin.add_trade(7.5, 2.5);
        }
        assert!((vpin.current_vpin() - 0.5).abs() < 1e-12);
        assert_eq!(vpin.regime(), VpinRegime::Normal); // 0.5 is not > 0.5

        // abs_delta 0.7: buy 8.5 / sell 1.5 → |7|/10 = 0.7.
        let mut vpin = VpinCalculator::new(10.0, 5);
        for _ in 0..5 {
            vpin.add_trade(8.5, 1.5);
        }
        assert!((vpin.current_vpin() - 0.7).abs() < 1e-12);
        assert_eq!(vpin.regime(), VpinRegime::Elevated); // 0.7 is not > 0.7
    }

    #[test]
    fn reset_returns_to_cold() {
        let mut vpin = VpinCalculator::new(10.0, 5);
        for _ in 0..6 {
            vpin.add_trade(10.0, 0.0);
        }
        assert!(vpin.is_warm());
        vpin.reset();
        assert_eq!(vpin.completed_buckets(), 0);
        assert_eq!(vpin.current_vpin(), 0.0);
        assert!(!vpin.is_warm());
        assert_eq!(vpin.regime(), VpinRegime::Normal);
    }

    #[test]
    fn degenerate_construction_clamps() {
        let mut vpin = VpinCalculator::new(-3.0, 0); // → bucket_size 1.0, n_buckets 1
        let (_, completed) = vpin.add_trade(1.0, 0.0);
        assert!(completed);
        let vpin2 = VpinCalculator::new(f64::NAN, 5);
        assert_eq!(vpin2.current_vpin(), 0.0);
    }
}
