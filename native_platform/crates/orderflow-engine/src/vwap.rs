//! Session VWAP with standard-deviation bands and anchoring.

/// Incremental VWAP with ±1σ/2σ/3σ bands; resets at 09:00 ICT each trading day.
///
/// [`VwapCalculator::anchor`] restarts accumulation from the current tick; until the next tick
/// arrives, [`VwapCalculator::snapshot`] keeps reporting the pre-anchor value.
#[derive(Debug, Clone)]
pub struct VwapCalculator {
    cumulative_pv: f64,    // Σ(price × volume) since last reset
    cumulative_vol: f64,   // Σ(volume) since last reset
    cumulative_pv_sq: f64, // Σ(price² × volume) for variance
    /// VWAP at the last anchor, reported only until a post-anchor tick arrives.
    anchor_baseline: f64,
    /// UTC timestamp of last trading day start (midnight), used for daily auto-reset.
    last_trading_day_utc: i64,
}

impl Default for VwapCalculator {
    fn default() -> Self {
        Self {
            cumulative_pv: 0.0,
            cumulative_vol: 0.0,
            cumulative_pv_sq: 0.0,
            anchor_baseline: 0.0,
            last_trading_day_utc: 0,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct VwapSnapshot {
    pub value: f64,
    pub upper_1: f64,
    pub lower_1: f64,
    pub upper_2: f64,
    pub lower_2: f64,
    pub upper_3: f64,
    pub lower_3: f64,
    pub total_volume: f64,
}

impl VwapCalculator {
    const ICT_OFFSET: i64 = 7 * 3600; // UTC+7
    const MARKET_OPEN_SECS: i64 = 9 * 3600; // 09:00 ICT

    pub fn new() -> Self {
        Self::default()
    }

    /// Add a tick, resetting first on a new trading day. Volume is floored at 1.0 so that
    /// price-only feeds still accumulate.
    pub fn update(&mut self, price: f64, volume: f64, timestamp: i64) {
        let vol = volume.max(1.0);

        let trading_day = trading_day_utc(timestamp);
        if self.last_trading_day_utc != 0 && trading_day != self.last_trading_day_utc {
            self.reset();
        }
        self.last_trading_day_utc = trading_day;

        self.cumulative_pv += price * vol;
        self.cumulative_pv_sq += price * price * vol;
        self.cumulative_vol += vol;
    }

    /// Current VWAP value with ±1σ, ±2σ, and ±3σ bands.
    pub fn snapshot(&self) -> VwapSnapshot {
        if self.cumulative_vol <= 0.0 {
            if self.anchor_baseline > 0.0 {
                return VwapSnapshot {
                    value: self.anchor_baseline,
                    ..Default::default()
                };
            }
            return VwapSnapshot::default();
        }
        // the anchor baseline only fills the empty case above; it is not an offset
        let vwap = self.cumulative_pv / self.cumulative_vol;
        let variance = (self.cumulative_pv_sq / self.cumulative_vol)
            - ((self.cumulative_pv / self.cumulative_vol).powi(2));
        let std_dev = variance.max(0.0).sqrt();

        VwapSnapshot {
            value: vwap,
            upper_1: vwap + std_dev,
            lower_1: vwap - std_dev,
            upper_2: vwap + std_dev * 2.0,
            lower_2: vwap - std_dev * 2.0,
            upper_3: vwap + std_dev * 3.0,
            lower_3: vwap - std_dev * 3.0,
            total_volume: self.cumulative_vol,
        }
    }

    /// Reset for new session (daily at 09:00 ICT).
    pub fn reset(&mut self) {
        self.cumulative_pv = 0.0;
        self.cumulative_vol = 0.0;
        self.cumulative_pv_sq = 0.0;
        self.anchor_baseline = 0.0;
    }

    /// Restart accumulation from now; the current value is reported until the next tick.
    pub fn anchor(&mut self) {
        let snap = self.snapshot();
        self.anchor_baseline = if snap.value > 0.0 { snap.value } else { 0.0 };
        self.cumulative_pv = 0.0;
        self.cumulative_vol = 0.0;
        self.cumulative_pv_sq = 0.0;
    }

    /// Whether VWAP has enough data to be meaningful.
    pub fn has_data(&self) -> bool {
        self.cumulative_vol > 0.0 || self.anchor_baseline > 0.0
    }

    /// Expose accumulators for cache serialization.
    pub fn accumulators(&self) -> (f64, f64, f64, f64, i64) {
        (
            self.cumulative_pv,
            self.cumulative_vol,
            self.cumulative_pv_sq,
            self.anchor_baseline,
            self.last_trading_day_utc,
        )
    }

    /// Restore accumulators from cache.
    pub fn restore(&mut self, cpv: f64, cvol: f64, cpvsq: f64, baseline: f64, last_day: i64) {
        self.cumulative_pv = cpv;
        self.cumulative_vol = cvol;
        self.cumulative_pv_sq = cpvsq;
        self.anchor_baseline = baseline;
        self.last_trading_day_utc = last_day;
    }
}

/// UTC timestamp of the ICT midnight that starts the trading day of `ts`.
/// Ticks before 09:00 ICT belong to the previous trading day.
fn trading_day_utc(ts: i64) -> i64 {
    let ts_ict = ts + VwapCalculator::ICT_OFFSET;
    let secs_in_day = ts_ict % 86400;
    if secs_in_day < VwapCalculator::MARKET_OPEN_SECS {
        (ts_ict - secs_in_day - 86400) / 86400 * 86400 - VwapCalculator::ICT_OFFSET
    } else {
        (ts_ict - secs_in_day) / 86400 * 86400 - VwapCalculator::ICT_OFFSET
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vwap_basic_calculation() {
        let mut calc = VwapCalculator::default();
        calc.update(100.0, 10.0, vn30f_open_ts(2026, 5, 19)); // pv=1000, vol=10
        calc.update(110.0, 20.0, vn30f_open_ts(2026, 5, 19) + 60); // pv=3200, vol=30

        let snap = calc.snapshot();
        assert!((snap.value - 106.67).abs() < 0.01);
        assert_eq!(snap.total_volume, 30.0);
    }

    #[test]
    fn zero_volume_uses_floor_of_one() {
        let mut calc = VwapCalculator::default();
        calc.update(100.0, 0.0, vn30f_open_ts(2026, 5, 19));
        calc.update(110.0, 10.0, vn30f_open_ts(2026, 5, 19) + 60);

        // vol: 1 + 10 = 11, pv: 100*1 + 110*10 = 1200, VWAP = 1200/11 ≈ 109.09
        let snap = calc.snapshot();
        assert!((snap.value - 109.09).abs() < 0.01);
    }

    #[test]
    fn vwap_no_data_returns_default() {
        let calc = VwapCalculator::default();
        let snap = calc.snapshot();
        assert_eq!(snap.value, 0.0);
        assert!(!calc.has_data());
    }

    #[test]
    fn vwap_reset_clears_state() {
        let mut calc = VwapCalculator::default();
        calc.update(100.0, 10.0, vn30f_open_ts(2026, 5, 19));
        calc.reset();

        let snap = calc.snapshot();
        assert_eq!(snap.value, 0.0);
        assert!(!calc.has_data());
    }

    #[test]
    fn vwap_bands_symmetrical() {
        let mut calc = VwapCalculator::default();
        let base = vn30f_open_ts(2026, 5, 19);
        for i in 0..100 {
            calc.update(100.0 + i as f64 * 0.1, 10.0, base + i as i64 * 60);
        }
        let snap = calc.snapshot();
        assert!(snap.upper_1 > snap.value);
        assert!(snap.lower_1 < snap.value);
        assert!(snap.upper_2 > snap.upper_1);
        assert!(snap.lower_2 < snap.lower_1);
        assert!(snap.upper_3 > snap.upper_2);
        assert!(snap.lower_3 < snap.lower_2);
    }

    #[test]
    fn vwap_auto_reset_on_new_trading_day() {
        let mut calc = VwapCalculator::default();
        let day1 = vn30f_open_ts(2026, 5, 19);
        calc.update(100.0, 10.0, day1);
        assert!((calc.snapshot().value - 100.0).abs() < 0.01);

        // Next trading day at 09:00 ICT
        let day2 = vn30f_open_ts(2026, 5, 20);
        calc.update(110.0, 10.0, day2);
        // Should have reset — only day2 volume counts
        assert!((calc.snapshot().value - 110.0).abs() < 0.01);
    }

    #[test]
    fn vwap_no_auto_reset_same_day() {
        let mut calc = VwapCalculator::default();
        let day1 = vn30f_open_ts(2026, 5, 19);
        calc.update(100.0, 5.0, day1); // 09:00
        calc.update(102.0, 5.0, day1 + 3600); // 10:00 — same day
        assert!((calc.snapshot().value - 101.0).abs() < 0.01);
    }

    #[test]
    fn vwap_anchor_preserves_baseline() {
        let mut calc = VwapCalculator::default();
        calc.update(100.0, 10.0, vn30f_open_ts(2026, 5, 19));
        let before = calc.snapshot().value;
        assert!(before > 0.0);

        calc.anchor();
        // After anchor, has_data should still be true (baseline preserved)
        assert!(calc.has_data());
        // Value should still show the baseline
        assert!((calc.snapshot().value - before).abs() < 0.01);

        // New tick after anchor: the anchored VWAP is that tick alone.
        calc.update(110.0, 10.0, vn30f_open_ts(2026, 5, 19) + 120);
        let after = calc.snapshot().value;
        assert!((after - 110.0).abs() < 1e-9, "after {after}");
    }

    /// The pre-anchor baseline is never added to the post-anchor VWAP.
    #[test]
    fn anchored_vwap_counts_only_ticks_since_the_anchor() {
        let open = vn30f_open_ts(2026, 5, 19);
        let mut calc = VwapCalculator::default();
        calc.update(100.0, 10.0, open);
        calc.anchor();

        calc.update(110.0, 10.0, open + 120);
        let snap = calc.snapshot();
        assert!((snap.value - 110.0).abs() < 1e-9, "value {}", snap.value);

        // (110 * 10 + 120 * 30) / 40 = 117.5, and the bands stay centred on it.
        calc.update(120.0, 30.0, open + 180);
        let snap = calc.snapshot();
        assert!((snap.value - 117.5).abs() < 1e-9, "value {}", snap.value);
        assert!(((snap.upper_1 - snap.value) - (snap.value - snap.lower_1)).abs() < 1e-9);
        assert_eq!(snap.total_volume, 40.0);
    }

    #[test]
    fn vwap_pre_market_belongs_to_previous_day() {
        let mut calc = VwapCalculator::default();
        let day1_close = vn30f_open_ts(2026, 5, 19) + 5 * 3600 + 45 * 60; // ~14:45
        calc.update(100.0, 10.0, day1_close);
        assert!((calc.snapshot().value - 100.0).abs() < 0.01);

        // 08:00 ICT next day — should still be previous trading day
        let next_day_8am = vn30f_midnight_utc(2026, 5, 20) + 8 * 3600;
        calc.update(105.0, 10.0, next_day_8am);
        // Same trading day, so VWAP = (100*10 + 105*10) / 20 = 102.5
        assert!((calc.snapshot().value - 102.5).abs() < 0.01);
    }

    #[test]
    fn trading_day_boundary_at_0900() {
        // 08:59:59 ICT
        let before = vn30f_midnight_utc(2026, 5, 19) + 8 * 3600 + 59 * 60 + 59;
        // 09:00:00 ICT
        let at_open = vn30f_open_ts(2026, 5, 19);

        assert_ne!(trading_day_utc(before), trading_day_utc(at_open));
    }

    fn vn30f_open_ts(y: i32, m: u32, d: u32) -> i64 {
        vn30f_midnight_utc(y, m, d) + 9 * 3600 // 09:00 ICT
    }

    fn vn30f_midnight_utc(y: i32, m: u32, d: u32) -> i64 {
        let dt = chrono::NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap();
        // NaiveDate is in ICT; midnight ICT = 17:00 UTC previous day
        dt.and_utc().timestamp() - 7 * 3600
    }
}
