//! Session initial balance (IB): the high/low range of the first N minutes of a session.
//!
//! Session-agnostic: the caller calls `start_session()` and passes the minutes since the open
//! with each bar. `range()` is `Some` only after the IB window closes (look-ahead-free).

/// Rolling first-N-minute high/low range per session.
#[derive(Debug, Clone)]
pub struct InitialBalance {
    window_min: i64,
    high: f64,
    low: f64,
    seen: bool,
    complete: bool,
}

impl InitialBalance {
    /// `window_min` is the IB length in minutes (e.g. 15 or 30). Clamps to >= 1.
    pub fn new(window_min: i64) -> Self {
        Self {
            window_min: window_min.max(1),
            high: f64::NEG_INFINITY,
            low: f64::INFINITY,
            seen: false,
            complete: false,
        }
    }

    /// Begin a fresh session — clears the accumulated IB.
    pub fn start_session(&mut self) {
        self.high = f64::NEG_INFINITY;
        self.low = f64::INFINITY;
        self.seen = false;
        self.complete = false;
    }

    /// Feed a completed bar at `minute_since_open` (0-based); bars in the window extend the
    /// range and the first bar after it seals the IB. Invalid bars are ignored.
    pub fn observe(&mut self, minute_since_open: i64, high: f64, low: f64) {
        if !high.is_finite() || !low.is_finite() || high < low {
            return;
        }
        if minute_since_open < self.window_min {
            self.high = self.high.max(high);
            self.low = self.low.min(low);
            self.seen = true;
        } else if self.seen {
            self.complete = true;
        }
    }

    /// The sealed IB `(high, low)` — `None` until the window has closed with at
    /// least one bar observed.
    pub fn range(&self) -> Option<(f64, f64)> {
        if self.complete {
            Some((self.high, self.low))
        } else {
            None
        }
    }

    pub fn is_complete(&self) -> bool {
        self.complete
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_until_window_closes() {
        let mut ib = InitialBalance::new(15);
        ib.start_session();
        ib.observe(0, 101.0, 100.0);
        ib.observe(5, 102.0, 100.5);
        ib.observe(14, 101.5, 99.5);
        assert_eq!(ib.range(), None, "still inside window");
        // First bar at/after minute 15 seals it.
        ib.observe(15, 103.0, 102.0);
        assert_eq!(ib.range(), Some((102.0, 99.5)));
    }

    #[test]
    fn bars_after_window_do_not_extend_range() {
        let mut ib = InitialBalance::new(15);
        ib.start_session();
        ib.observe(0, 101.0, 100.0);
        ib.observe(20, 200.0, 50.0); // seals; does not extend
        assert_eq!(ib.range(), Some((101.0, 100.0)));
        ib.observe(30, 300.0, 10.0); // ignored for range
        assert_eq!(ib.range(), Some((101.0, 100.0)));
    }

    #[test]
    fn start_session_resets() {
        let mut ib = InitialBalance::new(15);
        ib.start_session();
        ib.observe(0, 101.0, 100.0);
        ib.observe(15, 101.0, 100.0);
        assert!(ib.is_complete());
        ib.start_session();
        assert_eq!(ib.range(), None);
        assert!(!ib.is_complete());
    }

    #[test]
    fn invalid_bars_ignored() {
        let mut ib = InitialBalance::new(15);
        ib.start_session();
        ib.observe(0, f64::NAN, 100.0);
        ib.observe(1, 99.0, 101.0); // high < low
        assert!(!ib.seen);
        ib.observe(2, 101.0, 100.0);
        ib.observe(15, 101.0, 100.0);
        assert_eq!(ib.range(), Some((101.0, 100.0)));
    }
}
