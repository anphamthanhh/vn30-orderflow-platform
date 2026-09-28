//! Order book imbalance (OBI; Cont, Stoikov and Talreja, 2010) with exponential depth weights.
//!
//! ```text
//! OBI = (Σ w·bid − Σ w·ask) / Σ w·(bid+ask),  w_i = e^(−β·i)
//! ```
//!
//! Bounded to `[-1, 1]`: the numerator is a difference of the two non-negative sums that make
//! up the denominator.

/// Exponentially-decayed order-book imbalance over N depth levels.
#[derive(Debug, Clone, Copy)]
pub struct WeightedObi {
    pub depth_levels: usize,
    pub decay_beta: f64,
}

impl WeightedObi {
    pub fn new(depth_levels: usize, decay_beta: f64) -> Self {
        Self {
            depth_levels,
            decay_beta,
        }
    }

    /// Return `(obi, weighted_bid, weighted_ask)`, or `None` without overlapping depth or volume.
    ///
    /// `bids` and `asks` are `(price, volume)` pairs, best first, matched by index; invalid
    /// volumes count as 0.
    pub fn compute(&self, bids: &[(f64, f64)], asks: &[(f64, f64)]) -> Option<(f64, f64, f64)> {
        let n = self.depth_levels.min(bids.len()).min(asks.len());
        if n == 0 {
            return None;
        }
        let (mut w_bid, mut w_ask, mut w_total) = (0.0, 0.0, 0.0);
        for i in 0..n {
            let w = (-self.decay_beta * i as f64).exp();
            let bv = sanitize_vol(bids[i].1);
            let av = sanitize_vol(asks[i].1);
            w_bid += w * bv;
            w_ask += w * av;
            w_total += w * (bv + av);
        }
        if w_total <= 0.0 {
            return None;
        }
        Some(((w_bid - w_ask) / w_total, w_bid, w_ask))
    }
}

fn sanitize_vol(v: f64) -> f64 {
    if v.is_finite() && v > 0.0 {
        v
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_input_returns_none() {
        let obi = WeightedObi::new(5, 0.3);
        assert!(obi.compute(&[], &[]).is_none());
    }

    #[test]
    fn zero_total_volume_returns_none() {
        let obi = WeightedObi::new(5, 0.3);
        assert!(obi.compute(&[(1300.0, 0.0)], &[(1300.1, 0.0)]).is_none());
    }

    #[test]
    fn balanced_book_is_zero() {
        let obi = WeightedObi::new(3, 0.3);
        let bids = vec![(1300.0, 10.0), (1299.9, 10.0), (1299.8, 10.0)];
        let asks = vec![(1300.1, 10.0), (1300.2, 10.0), (1300.3, 10.0)];
        let (obi_val, _, _) = obi.compute(&bids, &asks).unwrap();
        assert!(obi_val.abs() < 1e-12);
    }

    #[test]
    fn all_bid_no_ask_saturates_to_one() {
        let obi = WeightedObi::new(3, 0.3);
        let bids = vec![(1300.0, 100.0)];
        let asks = vec![(1300.1, 0.0)];
        let (obi_val, w_bid, w_ask) = obi.compute(&bids, &asks).unwrap();
        assert!((obi_val - 1.0).abs() < 1e-12);
        assert!(w_bid > 0.0);
        assert_eq!(w_ask, 0.0);
    }

    #[test]
    fn negative_and_nan_volumes_sanitized_to_zero() {
        let obi = WeightedObi::new(2, 0.3);
        let bids = vec![(1300.0, f64::NAN), (1299.9, -5.0)];
        let asks = vec![(1300.1, 10.0), (1300.2, 10.0)];
        let (obi_val, w_bid, _) = obi.compute(&bids, &asks).unwrap();
        assert_eq!(w_bid, 0.0);
        assert!((obi_val + 1.0).abs() < 1e-12); // all-ask -> -1
    }
}
