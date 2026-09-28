//! Incremental Wilder ATR (average true range), fed completed bars.
//!
//! ```text
//! TR  = max(high - low, |high - prev_close|, |low - prev_close|)
//! ATR = SMA(TR, period)                      // seed
//! ATR = (ATR * (period - 1) + TR) / period   // Wilder smoothing
//! ```

/// O(1)-per-bar Wilder ATR. `None` until `period` true ranges are seeded.
#[derive(Debug, Clone)]
pub struct Atr {
    period: usize,
    prev_close: Option<f64>,
    value: Option<f64>,
    seed_sum: f64,
    seed_count: usize,
}

impl Atr {
    /// `period` == 0 clamps to 1.
    pub fn new(period: usize) -> Self {
        Self {
            period: period.max(1),
            prev_close: None,
            value: None,
            seed_sum: 0.0,
            seed_count: 0,
        }
    }

    /// Feed one completed bar and return the ATR once warm; invalid bars change nothing.
    /// The first bar's true range is `high - low`.
    pub fn on_bar(&mut self, high: f64, low: f64, close: f64) -> Option<f64> {
        if !high.is_finite() || !low.is_finite() || !close.is_finite() || high < low {
            return self.value;
        }
        let tr = match self.prev_close {
            Some(prev) => (high - low)
                .max((high - prev).abs())
                .max((low - prev).abs()),
            None => high - low,
        };
        self.prev_close = Some(close);

        match self.value {
            Some(atr) => {
                let n = self.period as f64;
                self.value = Some((atr * (n - 1.0) + tr) / n);
            }
            None => {
                self.seed_sum += tr;
                self.seed_count += 1;
                if self.seed_count >= self.period {
                    self.value = Some(self.seed_sum / self.period as f64);
                }
            }
        }
        self.value
    }

    pub fn value(&self) -> Option<f64> {
        self.value
    }

    pub fn is_warm(&self) -> bool {
        self.value.is_some()
    }

    /// Session start: back to cold.
    pub fn reset(&mut self) {
        self.prev_close = None;
        self.value = None;
        self.seed_sum = 0.0;
        self.seed_count = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_sequence_parity() {
        // period=3. TRs: bar1 h-l=2; bar2 max(3,|103-101|=2,|100-101|=1)=3;
        // bar3 max(4,|105-102|=3,|101-102|=1)=4 → seed ATR=(2+3+4)/3=3.0.
        // bar4 TR=max(5,|108-104|=4,|103-104|=1)=5 → (3*2+5)/3=11/3.
        let mut atr = Atr::new(3);
        assert_eq!(atr.on_bar(102.0, 100.0, 101.0), None);
        assert_eq!(atr.on_bar(103.0, 100.0, 102.0), None);
        assert_eq!(atr.on_bar(105.0, 101.0, 104.0), Some(3.0));
        let v = atr.on_bar(108.0, 103.0, 107.0).expect("warm");
        assert!((v - 11.0 / 3.0).abs() < 1e-12);
    }

    #[test]
    fn warmup_none_until_period_bars() {
        let mut atr = Atr::new(4);
        for i in 0..3 {
            assert_eq!(atr.on_bar(101.0, 100.0, 100.5), None, "bar {i}");
            assert!(!atr.is_warm());
        }
        assert!(atr.on_bar(101.0, 100.0, 100.5).is_some());
        assert!(atr.is_warm());
    }

    #[test]
    fn gap_bar_uses_prev_close_distance() {
        // prev_close=100; gap bar h=112 l=111 → h-l=1 but |h-prev|=12 → TR=12.
        let mut atr = Atr::new(2);
        atr.on_bar(101.0, 99.0, 100.0); // TR=2
        let v = atr.on_bar(112.0, 111.0, 111.5).expect("warm"); // TR=12
        assert!((v - 7.0).abs() < 1e-12); // seed SMA = (2+12)/2
    }

    #[test]
    fn invalid_bars_ignored_state_unchanged() {
        let mut atr = Atr::new(2);
        atr.on_bar(101.0, 99.0, 100.0);
        atr.on_bar(102.0, 100.0, 101.0);
        let before = atr.value();
        assert_eq!(atr.on_bar(f64::NAN, 99.0, 100.0), before);
        assert_eq!(atr.on_bar(101.0, f64::NEG_INFINITY, 100.0), before);
        assert_eq!(atr.on_bar(101.0, 99.0, f64::INFINITY), before);
        assert_eq!(atr.on_bar(99.0, 101.0, 100.0), before); // high < low
        assert_eq!(atr.value(), before);
        // Next valid bar continues from unpoisoned state: prev_close=101,
        // TR=max(2,|103-101|,|101-101|)=2 → (before*1 + 2)/2.
        let v = atr.on_bar(103.0, 101.0, 102.0).expect("warm");
        assert!((v - (before.unwrap() + 2.0) / 2.0).abs() < 1e-12);
    }

    #[test]
    fn reset_and_zero_period_clamp() {
        let mut atr = Atr::new(0); // clamps to 1 → warm after first bar
        assert_eq!(atr.on_bar(102.0, 100.0, 101.0), Some(2.0));
        atr.reset();
        assert!(!atr.is_warm());
        assert_eq!(atr.value(), None);
        // First bar after reset has no prev_close again.
        assert_eq!(atr.on_bar(105.0, 100.0, 104.0), Some(5.0));
    }
}
