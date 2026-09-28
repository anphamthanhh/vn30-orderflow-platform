//! Rolling simple moving average of bar volume, with a volume-spike test.
//!
//! Used to confirm a breakout by a volume surge (e.g. at least 2× the trailing average).

use std::collections::VecDeque;

/// O(1)-amortised trailing volume SMA over the last `period` bars.
#[derive(Debug, Clone)]
pub struct RollingVolumeMa {
    period: usize,
    buf: VecDeque<f64>,
    sum: f64,
}

impl RollingVolumeMa {
    /// `period` == 0 clamps to 1.
    pub fn new(period: usize) -> Self {
        let period = period.max(1);
        Self {
            period,
            buf: VecDeque::with_capacity(period),
            sum: 0.0,
        }
    }

    /// Add a bar's volume; returns the SMA once the window is full. Invalid input is ignored.
    pub fn on_volume(&mut self, volume: f64) -> Option<f64> {
        if !volume.is_finite() || volume < 0.0 {
            return self.value();
        }
        self.buf.push_back(volume);
        self.sum += volume;
        if self.buf.len() > self.period {
            if let Some(old) = self.buf.pop_front() {
                self.sum -= old;
            }
        }
        self.value()
    }

    /// The trailing SMA, or `None` until `period` bars are seen.
    pub fn value(&self) -> Option<f64> {
        if self.buf.len() >= self.period {
            Some(self.sum / self.period as f64)
        } else {
            None
        }
    }

    pub fn is_warm(&self) -> bool {
        self.buf.len() >= self.period
    }

    /// True when `current >= mult * SMA`; always false while cold.
    pub fn is_spike(&self, current: f64, mult: f64) -> bool {
        match self.value() {
            Some(ma) if ma > 0.0 && current.is_finite() => current >= mult * ma,
            _ => false,
        }
    }

    pub fn reset(&mut self) {
        self.buf.clear();
        self.sum = 0.0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cold_until_full_window() {
        let mut m = RollingVolumeMa::new(3);
        assert_eq!(m.on_volume(10.0), None);
        assert_eq!(m.on_volume(20.0), None);
        assert_eq!(m.on_volume(30.0), Some(20.0));
        assert!(m.is_warm());
    }

    #[test]
    fn rolls_off_oldest() {
        let mut m = RollingVolumeMa::new(3);
        m.on_volume(10.0);
        m.on_volume(20.0);
        m.on_volume(30.0); // ma=20
        assert_eq!(m.on_volume(60.0), Some((20.0 + 30.0 + 60.0) / 3.0)); // 10 rolled off
    }

    #[test]
    fn spike_detection() {
        let mut m = RollingVolumeMa::new(3);
        m.on_volume(10.0);
        m.on_volume(10.0);
        m.on_volume(10.0); // ma=10
        assert!(m.is_spike(20.0, 2.0)); // 20 >= 2*10
        assert!(!m.is_spike(19.0, 2.0)); // 19 < 20
        assert!(!m.is_spike(f64::NAN, 2.0));
    }

    #[test]
    fn spike_false_while_cold() {
        let m = RollingVolumeMa::new(3);
        assert!(!m.is_spike(1000.0, 2.0));
    }

    #[test]
    fn negative_and_nonfinite_ignored() {
        let mut m = RollingVolumeMa::new(2);
        m.on_volume(10.0);
        let before = m.on_volume(20.0);
        assert_eq!(m.on_volume(-5.0), before);
        assert_eq!(m.on_volume(f64::INFINITY), before);
    }
}
