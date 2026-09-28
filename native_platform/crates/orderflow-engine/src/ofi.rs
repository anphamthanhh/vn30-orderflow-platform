//! Multi-level order flow imbalance (OFI; Cont, Kukanov and Stoikov, 2014).
//!
//! Extends the best-quote `ofi_events()` in `historical/research_lab/lab.py` to N levels.
//! Per level `i`, the price-conditioned event term:
//!
//! ```text
//! e_i = 1{Pb_i(t)>=Pb_i(t-1)}·Qb_i(t) − 1{Pb_i(t)<=Pb_i(t-1)}·Qb_i(t-1)
//!     − 1{Pa_i(t)<=Pa_i(t-1)}·Qa_i(t) + 1{Pa_i(t)>=Pa_i(t-1)}·Qa_i(t-1)
//! ```
//!
//! One update contributes `Σ w_i · e_i` with `w_i = e^(−β·i)`, the weighting of
//! [`crate::obi::WeightedObi`].

/// Decay applied to the cumulative value on every update.
const CUMULATIVE_DECAY: f64 = 0.99;

#[derive(Debug, Clone)]
pub struct MultiLevelOfi {
    levels: usize,
    decay_beta: f64,
    prev_bids: Vec<(f64, f64)>,
    prev_asks: Vec<(f64, f64)>,
    has_prev: bool,
    cumulative: f64,
}

impl MultiLevelOfi {
    pub fn new(levels: usize, decay_beta: f64) -> Self {
        Self {
            levels: levels.max(1),
            decay_beta,
            prev_bids: Vec::new(),
            prev_asks: Vec::new(),
            has_prev: false,
            cumulative: 0.0,
        }
    }

    /// Return this update's weighted OFI (0.0 on the first call) and add it to the cumulative.
    ///
    /// `bids` and `asks` are `(price, volume)` pairs, best first, matched by level index.
    pub fn update(&mut self, bids: &[(f64, f64)], asks: &[(f64, f64)]) -> f64 {
        let cur_bids: Vec<(f64, f64)> = bids.iter().take(self.levels).copied().collect();
        let cur_asks: Vec<(f64, f64)> = asks.iter().take(self.levels).copied().collect();

        let raw = if !self.has_prev {
            0.0
        } else {
            let n = self
                .levels
                .min(self.prev_bids.len())
                .min(self.prev_asks.len())
                .min(cur_bids.len())
                .min(cur_asks.len());
            let mut total = 0.0;
            for i in 0..n {
                let w = (-self.decay_beta * i as f64).exp();
                let (pb0, qb0) = self.prev_bids[i];
                let (pb1, qb1) = cur_bids[i];
                let (pa0, qa0) = self.prev_asks[i];
                let (pa1, qa1) = cur_asks[i];
                total += w * (bid_event(pb0, qb0, pb1, qb1) + ask_event(pa0, qa0, pa1, qa1));
            }
            total
        };

        self.prev_bids = cur_bids;
        self.prev_asks = cur_asks;
        self.has_prev = true;

        self.cumulative = (self.cumulative + raw) * CUMULATIVE_DECAY;
        raw
    }

    /// Decayed running sum of per-update values; the value to threshold or display.
    pub fn cumulative(&self) -> f64 {
        self.cumulative
    }

    /// Session start: forget prior levels and cumulative pressure.
    pub fn reset(&mut self) {
        self.prev_bids.clear();
        self.prev_asks.clear();
        self.has_prev = false;
        self.cumulative = 0.0;
    }
}

/// Bid-side CKS event term; 0 if either price is non-finite, invalid sizes count as 0.
fn bid_event(pb0: f64, qb0: f64, pb1: f64, qb1: f64) -> f64 {
    if !pb0.is_finite() || !pb1.is_finite() {
        return 0.0;
    }
    let (qb0, qb1) = (sanitize_qty(qb0), sanitize_qty(qb1));
    let up = if pb1 >= pb0 { qb1 } else { 0.0 };
    let down = if pb1 <= pb0 { qb0 } else { 0.0 };
    up - down
}

/// Ask-side CKS event term; an improving (lower) ask counts as bearish, as in `ofi_events()`.
fn ask_event(pa0: f64, qa0: f64, pa1: f64, qa1: f64) -> f64 {
    if !pa0.is_finite() || !pa1.is_finite() {
        return 0.0;
    }
    let (qa0, qa1) = (sanitize_qty(qa0), sanitize_qty(qa1));
    let down = if pa1 <= pa0 { qa1 } else { 0.0 };
    let up = if pa1 >= pa0 { qa0 } else { 0.0 };
    up - down
}

fn sanitize_qty(v: f64) -> f64 {
    if v.is_finite() && v >= 0.0 {
        v
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_update_returns_zero_no_predecessor() {
        let mut ofi = MultiLevelOfi::new(3, 0.3);
        let v = ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        assert_eq!(v, 0.0);
        assert_eq!(ofi.cumulative(), 0.0);
    }

    #[test]
    fn bid_size_increase_at_same_price_is_bullish() {
        let mut ofi = MultiLevelOfi::new(1, 0.3);
        ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        // Bid grew from 10 -> 20 at the same price, ask unchanged.
        let v = ofi.update(&[(1300.0, 20.0)], &[(1300.1, 10.0)]);
        assert!(v > 0.0, "expected bullish OFI, got {v}");
    }

    #[test]
    fn ask_size_increase_at_same_price_is_bearish() {
        let mut ofi = MultiLevelOfi::new(1, 0.3);
        ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        let v = ofi.update(&[(1300.0, 10.0)], &[(1300.1, 20.0)]);
        assert!(v < 0.0, "expected bearish OFI, got {v}");
    }

    #[test]
    fn bid_price_drop_treats_old_level_as_departed() {
        // Best bid moves down a tick: old bid level departs entirely
        // (contributes -q_old), independent of the new level's size.
        let mut ofi = MultiLevelOfi::new(1, 0.3);
        ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        let v = ofi.update(&[(1299.9, 5.0)], &[(1300.1, 10.0)]);
        assert!(v < 0.0, "departing bid level should be bearish, got {v}");
    }

    #[test]
    fn nan_price_zeroes_that_side_without_poisoning_state() {
        let mut ofi = MultiLevelOfi::new(1, 0.3);
        ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        let v = ofi.update(&[(f64::NAN, 999.0)], &[(1300.1, 10.0)]);
        assert!(v.is_finite());
        assert_eq!(v, 0.0); // bid side zeroed, ask side unchanged -> 0
    }

    #[test]
    fn reset_returns_to_cold() {
        let mut ofi = MultiLevelOfi::new(2, 0.3);
        ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        ofi.update(&[(1300.0, 50.0)], &[(1300.1, 5.0)]);
        assert_ne!(ofi.cumulative(), 0.0);
        ofi.reset();
        assert_eq!(ofi.cumulative(), 0.0);
        let v = ofi.update(&[(1300.0, 10.0)], &[(1300.1, 10.0)]);
        assert_eq!(v, 0.0); // cold again, no predecessor
    }

    #[test]
    fn empty_book_never_panics_and_stays_finite() {
        let mut ofi = MultiLevelOfi::new(5, 0.3);
        assert_eq!(ofi.update(&[], &[]), 0.0);
        let v = ofi.update(&[], &[]);
        assert!(v.is_finite());
    }
}
