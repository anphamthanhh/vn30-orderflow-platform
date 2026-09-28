//! Volume delta per tick and per time bar.
//!
//! Tick delta = buy volume − sell volume by aggressor side; bar delta sums it over the bar.

/// Result of a single tick delta update.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TickDelta {
    pub buy_volume: f64,
    pub sell_volume: f64,
    pub net_delta: f64,
}

/// Tracks delta per tick and aggregates into time bars.
#[derive(Debug, Clone)]
pub struct DeltaCalculator {
    /// Accumulated delta in the current bar.
    pub bar_delta: f64,
    /// Total buy volume in the current bar.
    pub bar_buy_vol: f64,
    /// Total sell volume in the current bar.
    pub bar_sell_vol: f64,
    /// Bar interval in seconds.
    bar_seconds: i64,
    /// Timestamp of the last bar reset.
    last_bar_ts: i64,
}

impl DeltaCalculator {
    pub fn new(bar_seconds: i64) -> Self {
        Self {
            bar_delta: 0.0,
            bar_buy_vol: 0.0,
            bar_sell_vol: 0.0,
            bar_seconds,
            last_bar_ts: 0,
        }
    }

    /// Return the tick's delta, starting a new bar first when `timestamp` crosses a boundary.
    /// Unknown sides and invalid volumes contribute 0.
    pub fn on_tick(&mut self, volume: f64, side: &str, timestamp: i64) -> TickDelta {
        let volume = if volume.is_finite() && volume >= 0.0 {
            volume
        } else {
            0.0
        };
        let (buy_vol, sell_vol) = match side {
            "B" | "BUY" | "Buy" | "buy" => (volume, 0.0),
            "S" | "SELL" | "Sell" | "sell" => (0.0, volume),
            _ => (0.0, 0.0),
        };
        let net = buy_vol - sell_vol;

        if self.bar_seconds > 0 && timestamp > 0 {
            let bar_index = timestamp / self.bar_seconds;
            let last_bar = self.last_bar_ts / self.bar_seconds;
            if bar_index != last_bar {
                self.reset_bar(timestamp);
            }
        }

        self.bar_delta += net;
        self.bar_buy_vol += buy_vol;
        self.bar_sell_vol += sell_vol;
        self.last_bar_ts = timestamp;

        TickDelta {
            buy_volume: buy_vol,
            sell_volume: sell_vol,
            net_delta: net,
        }
    }

    /// Reset bar accumulators.
    pub fn reset_bar(&mut self, timestamp: i64) {
        self.bar_delta = 0.0;
        self.bar_buy_vol = 0.0;
        self.bar_sell_vol = 0.0;
        self.last_bar_ts = timestamp;
    }

    /// Total volume in the current bar.
    pub fn bar_total_vol(&self) -> f64 {
        self.bar_buy_vol + self.bar_sell_vol
    }
}

impl Default for DeltaCalculator {
    fn default() -> Self {
        Self::new(60) // 1-minute bars by default
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn buy_tick_adds_to_delta() {
        let mut calc = DeltaCalculator::new(60);
        let result = calc.on_tick(10.0, "B", 1000);
        assert_eq!(result.net_delta, 10.0);
        assert_eq!(result.buy_volume, 10.0);
        assert_eq!(result.sell_volume, 0.0);
        assert_eq!(calc.bar_delta, 10.0);
    }

    #[test]
    fn sell_tick_subtracts_from_delta() {
        let mut calc = DeltaCalculator::new(60);
        let result = calc.on_tick(15.0, "S", 1000);
        assert_eq!(result.net_delta, -15.0);
        assert_eq!(calc.bar_delta, -15.0);
    }

    #[test]
    fn unknown_side_produces_zero_delta() {
        let mut calc = DeltaCalculator::new(60);
        let result = calc.on_tick(10.0, "X", 1000);
        assert_eq!(result.net_delta, 0.0);
        assert_eq!(result.buy_volume, 0.0);
        assert_eq!(result.sell_volume, 0.0);
        assert_eq!(calc.bar_delta, 0.0);
    }

    #[test]
    fn bar_resets_on_timestamp_boundary() {
        let mut calc = DeltaCalculator::new(60);
        calc.on_tick(10.0, "B", 0); // bar 0, timestamp 0
        assert_eq!(calc.bar_delta, 10.0);

        calc.on_tick(5.0, "B", 30); // still bar 0 (30/60 = 0)
        assert_eq!(calc.bar_delta, 15.0);

        calc.on_tick(3.0, "S", 61); // bar 1 now (61/60 = 1), auto-reset
        assert_eq!(calc.bar_delta, -3.0);
        assert_eq!(calc.bar_buy_vol, 0.0);
        assert_eq!(calc.bar_sell_vol, 3.0);
    }

    #[test]
    fn manual_bar_reset_clears_accumulators() {
        let mut calc = DeltaCalculator::new(60);
        calc.on_tick(10.0, "B", 0);
        calc.on_tick(5.0, "S", 1);
        assert_eq!(calc.bar_delta, 5.0);

        calc.reset_bar(100);
        assert_eq!(calc.bar_delta, 0.0);
        assert_eq!(calc.bar_buy_vol, 0.0);
        assert_eq!(calc.bar_sell_vol, 0.0);
    }

    #[test]
    fn default_bar_interval_is_60_seconds() {
        let calc = DeltaCalculator::default();
        assert_eq!(calc.bar_seconds, 60);
    }

    #[test]
    fn bar_total_volume_matches_buy_plus_sell() {
        let mut calc = DeltaCalculator::new(60);
        calc.on_tick(10.0, "B", 0);
        calc.on_tick(20.0, "S", 1);
        calc.on_tick(5.0, "B", 2);
        assert_eq!(calc.bar_total_vol(), 35.0);
    }
}
