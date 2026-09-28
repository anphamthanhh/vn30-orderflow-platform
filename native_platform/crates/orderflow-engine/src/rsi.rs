//! Incremental Wilder RSI (relative strength index), fed completed bar closes.
//!
//! ```text
//! gain/loss = max(±Δclose, 0)
//! avg_gain  = SMA(gain, period)                    // seed
//! avg_gain  = (avg_gain*(period-1) + gain) / period // Wilder smoothing
//! RS  = avg_gain / avg_loss
//! RSI = 100 - 100/(1+RS)     (RSI = 100 when avg_loss == 0)
//! ```

/// O(1)-per-bar Wilder RSI. `None` until `period` price deltas are seeded.
#[derive(Debug, Clone)]
pub struct Rsi {
    period: usize,
    prev_close: Option<f64>,
    avg_gain: Option<f64>,
    avg_loss: Option<f64>,
    seed_gain: f64,
    seed_loss: f64,
    seed_count: usize,
    value: Option<f64>,
}

impl Rsi {
    /// `period` == 0 clamps to 1.
    pub fn new(period: usize) -> Self {
        Self {
            period: period.max(1),
            prev_close: None,
            avg_gain: None,
            avg_loss: None,
            seed_gain: 0.0,
            seed_loss: 0.0,
            seed_count: 0,
            value: None,
        }
    }

    /// Feed one bar close; returns the RSI in `[0, 100]` once warm. Non-finite input is ignored.
    pub fn on_close(&mut self, close: f64) -> Option<f64> {
        if !close.is_finite() {
            return self.value;
        }
        let prev = match self.prev_close {
            Some(p) => p,
            None => {
                self.prev_close = Some(close);
                return None;
            }
        };
        let delta = close - prev;
        self.prev_close = Some(close);
        let gain = delta.max(0.0);
        let loss = (-delta).max(0.0);

        match (self.avg_gain, self.avg_loss) {
            (Some(ag), Some(al)) => {
                let n = self.period as f64;
                let ag = (ag * (n - 1.0) + gain) / n;
                let al = (al * (n - 1.0) + loss) / n;
                self.avg_gain = Some(ag);
                self.avg_loss = Some(al);
                self.value = Some(Self::rsi(ag, al));
            }
            _ => {
                self.seed_gain += gain;
                self.seed_loss += loss;
                self.seed_count += 1;
                if self.seed_count >= self.period {
                    let n = self.period as f64;
                    let ag = self.seed_gain / n;
                    let al = self.seed_loss / n;
                    self.avg_gain = Some(ag);
                    self.avg_loss = Some(al);
                    self.value = Some(Self::rsi(ag, al));
                }
            }
        }
        self.value
    }

    fn rsi(avg_gain: f64, avg_loss: f64) -> f64 {
        if avg_loss == 0.0 {
            // no losses: 100, or 50 when price did not move
            return if avg_gain == 0.0 { 50.0 } else { 100.0 };
        }
        let rs = avg_gain / avg_loss;
        100.0 - 100.0 / (1.0 + rs)
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
        self.avg_gain = None;
        self.avg_loss = None;
        self.seed_gain = 0.0;
        self.seed_loss = 0.0;
        self.seed_count = 0;
        self.value = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn warmup_none_until_period_deltas() {
        // period=3 needs 1 seed close + 3 deltas = 4 closes before warm.
        let mut rsi = Rsi::new(3);
        assert_eq!(rsi.on_close(100.0), None); // seed prev
        assert_eq!(rsi.on_close(101.0), None); // delta 1
        assert_eq!(rsi.on_close(102.0), None); // delta 2
        assert!(rsi.on_close(103.0).is_some()); // delta 3 → warm
        assert!(rsi.is_warm());
    }

    #[test]
    fn all_gains_is_hundred() {
        let mut rsi = Rsi::new(3);
        for c in [100.0, 101.0, 102.0, 103.0] {
            rsi.on_close(c);
        }
        assert_eq!(rsi.value(), Some(100.0));
    }

    #[test]
    fn all_losses_is_zero() {
        let mut rsi = Rsi::new(3);
        for c in [100.0, 99.0, 98.0, 97.0] {
            rsi.on_close(c);
        }
        assert_eq!(rsi.value(), Some(0.0));
    }

    #[test]
    fn balanced_moves_near_fifty() {
        // Equal up/down magnitude alternating → RSI hovers around 50.
        let mut rsi = Rsi::new(4);
        for c in [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0] {
            rsi.on_close(c);
        }
        let v = rsi.value().expect("warm");
        assert!((v - 50.0).abs() < 20.0, "rsi={v}");
    }

    #[test]
    fn known_wilder_value() {
        // period=2. closes 100,101,100.5. seed deltas: +1, -0.5 →
        // avg_gain=0.5, avg_loss=0.25 → RS=2 → RSI=100-100/3=66.6667.
        let mut rsi = Rsi::new(2);
        rsi.on_close(100.0);
        rsi.on_close(101.0);
        let v = rsi.on_close(100.5).expect("warm");
        assert!((v - (100.0 - 100.0 / 3.0)).abs() < 1e-9, "rsi={v}");
    }

    #[test]
    fn non_finite_ignored() {
        let mut rsi = Rsi::new(2);
        rsi.on_close(100.0);
        rsi.on_close(101.0);
        let before = rsi.on_close(102.0);
        assert_eq!(rsi.on_close(f64::NAN), before);
    }
}
