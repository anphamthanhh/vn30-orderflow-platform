//! Anchored VWAP: the volume-weighted mean price since a caller-chosen anchor.
//!
//! The caller re-anchors with `reset` (e.g. at the session open); the value is `None` until a
//! tick with positive volume arrives.

/// Incremental anchored VWAP = Σ(price·volume) / Σ(volume) since the last anchor.
#[derive(Debug, Clone, Default)]
pub struct AnchoredVwap {
    sum_pv: f64,
    sum_v: f64,
}

impl AnchoredVwap {
    pub fn new() -> Self {
        Self::default()
    }

    /// Re-anchor by clearing the accumulation.
    pub fn reset(&mut self) {
        self.sum_pv = 0.0;
        self.sum_v = 0.0;
    }

    /// Add a tick or bar and return the VWAP; non-finite input or volume <= 0 is ignored.
    pub fn update(&mut self, price: f64, volume: f64) -> Option<f64> {
        if price.is_finite() && volume.is_finite() && volume > 0.0 {
            self.sum_pv += price * volume;
            self.sum_v += volume;
        }
        self.value()
    }

    pub fn value(&self) -> Option<f64> {
        if self.sum_v > 0.0 {
            Some(self.sum_pv / self.sum_v)
        } else {
            None
        }
    }

    pub fn is_warm(&self) -> bool {
        self.sum_v > 0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_until_first_sample() {
        let mut v = AnchoredVwap::new();
        assert_eq!(v.value(), None);
        assert_eq!(v.update(100.0, 0.0), None); // zero volume ignored
        assert_eq!(v.update(100.0, 10.0), Some(100.0));
    }

    #[test]
    fn volume_weighted_mean() {
        let mut v = AnchoredVwap::new();
        v.update(100.0, 10.0); // pv=1000
        let out = v.update(110.0, 30.0).unwrap(); // pv=1000+3300=4300, vol=40
        assert!((out - 4300.0 / 40.0).abs() < 1e-12); // 107.5 (skewed to heavier 110)
    }

    #[test]
    fn reset_reanchors() {
        let mut v = AnchoredVwap::new();
        v.update(100.0, 10.0);
        v.reset();
        assert_eq!(v.value(), None);
        assert_eq!(v.update(200.0, 5.0), Some(200.0));
    }

    #[test]
    fn nonfinite_and_negative_ignored() {
        let mut v = AnchoredVwap::new();
        v.update(100.0, 10.0);
        let before = v.value();
        assert_eq!(v.update(f64::NAN, 10.0), before);
        assert_eq!(v.update(100.0, -5.0), before);
        assert_eq!(v.update(100.0, f64::INFINITY), before);
    }
}
