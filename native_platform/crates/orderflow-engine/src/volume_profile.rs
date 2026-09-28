//! Incremental volume-at-price profile (POC and value area) that a strategy updates itself.
//!
//! Rules match `market-profile-engine` and `shared/quant_core/profile.py`, checked against one
//! golden fixture: POC ties go to the level nearest the mid-range, then the higher price; the
//! value area grows one contiguous level per step, and equal-weight ties take the lower side.

use std::collections::{BTreeMap, VecDeque};

pub const DEFAULT_VALUE_AREA_PCT: f64 = 0.70;

/// Volume accumulated per price bucket, with canonical POC / value area.
#[derive(Debug, Clone)]
pub struct VolumeProfile {
    bucket_points: f64,
    value_area_pct: f64,
    buckets: BTreeMap<i64, f64>,
    total: f64,
}

impl VolumeProfile {
    /// `bucket_points` is the price granularity in points (non-positive becomes 0.1);
    /// `value_area_pct` is clamped into `(0, 1]`.
    #[must_use]
    pub fn new(bucket_points: f64, value_area_pct: f64) -> Self {
        Self {
            bucket_points: if bucket_points > 0.0 {
                bucket_points
            } else {
                0.1
            },
            value_area_pct: value_area_pct.clamp(f64::MIN_POSITIVE, 1.0),
            buckets: BTreeMap::new(),
            total: 0.0,
        }
    }

    /// Clear the profile; call at a session boundary.
    pub fn reset(&mut self) {
        self.buckets.clear();
        self.total = 0.0;
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.buckets.is_empty()
    }

    #[must_use]
    pub fn total_volume(&self) -> f64 {
        self.total
    }

    #[must_use]
    pub fn bucket_points(&self) -> f64 {
        self.bucket_points
    }

    fn bucket_of(&self, price: f64) -> i64 {
        (price / self.bucket_points).round() as i64
    }

    /// Add one trade; non-finite or non-positive input is ignored.
    pub fn observe_tick(&mut self, price: f64, volume: f64) {
        if !price.is_finite() || !volume.is_finite() || volume <= 0.0 {
            return;
        }
        let bucket = self.bucket_of(price);
        *self.buckets.entry(bucket).or_insert(0.0) += volume;
        self.total += volume;
    }

    /// Add one completed bar, spreading its volume uniformly across the buckets in its range.
    pub fn observe_bar(&mut self, high: f64, low: f64, volume: f64) {
        if !high.is_finite() || !low.is_finite() || !volume.is_finite() {
            return;
        }
        if volume <= 0.0 || high < low {
            return;
        }
        let (lo_bucket, hi_bucket) = (self.bucket_of(low), self.bucket_of(high));
        let span = (hi_bucket - lo_bucket + 1).max(1);
        let share = volume / span as f64;
        for bucket in lo_bucket..=hi_bucket {
            *self.buckets.entry(bucket).or_insert(0.0) += share;
        }
        self.total += volume;
    }

    /// Point of control — the price bucket holding the most volume.
    #[must_use]
    pub fn poc(&self) -> Option<f64> {
        self.value_area().map(|(poc, _, _)| poc)
    }

    /// `(poc, val, vah)` in prices, or `None` for an empty profile.
    #[must_use]
    pub fn value_area(&self) -> Option<(f64, f64, f64)> {
        let rows: Vec<(i64, f64)> = self.buckets.iter().map(|(k, v)| (*k, *v)).collect();
        let (poc, val, vah) = value_area_from_buckets(&rows, self.value_area_pct)?;
        Some((
            poc as f64 * self.bucket_points,
            val as f64 * self.bucket_points,
            vah as f64 * self.bucket_points,
        ))
    }
}

/// Value area `(poc, val, vah)` in buckets over ascending `(bucket, weight)` rows.
///
/// Shared by both profile types; mirrors `market-profile-engine`'s `value_area_from_pairs`.
#[must_use]
pub fn value_area_from_buckets(
    rows: &[(i64, f64)],
    value_area_pct: f64,
) -> Option<(i64, i64, i64)> {
    if rows.is_empty() {
        return None;
    }
    let total: f64 = rows.iter().map(|(_, weight)| *weight).sum();
    if total <= 0.0 {
        return None;
    }

    // POC: max weight; ties -> nearest the mid-range; still tied -> higher bucket.
    let two_mid = rows[0].0 + rows[rows.len() - 1].0;
    let midrange_distance = |bucket: i64| (2 * bucket - two_mid).abs();
    let mut poc_index = 0usize;
    for index in 1..rows.len() {
        let better = rows[index].1 > rows[poc_index].1
            || (rows[index].1 == rows[poc_index].1
                && (midrange_distance(rows[index].0) < midrange_distance(rows[poc_index].0)
                    || (midrange_distance(rows[index].0) == midrange_distance(rows[poc_index].0)
                        && rows[index].0 > rows[poc_index].0)));
        if better {
            poc_index = index;
        }
    }

    let target = total * value_area_pct;
    let mut covered = rows[poc_index].1;
    let mut left = poc_index.checked_sub(1);
    let mut right = poc_index + 1;
    let (mut lo_index, mut hi_index) = (poc_index, poc_index);

    while covered < target && (left.is_some() || right < rows.len()) {
        let left_weight = left.map(|index| rows[index].1).unwrap_or(-1.0);
        let right_weight = if right < rows.len() {
            rows[right].1
        } else {
            -1.0
        };
        // strict `>`: an equal-weight tie takes the lower side, as in the engine
        if right_weight > left_weight {
            covered += rows[right].1;
            hi_index = right;
            right += 1;
        } else if let Some(index) = left {
            covered += rows[index].1;
            lo_index = index;
            left = index.checked_sub(1);
        } else if right < rows.len() {
            covered += rows[right].1;
            hi_index = right;
            right += 1;
        }
    }

    Some((rows[poc_index].0, rows[lo_index].0, rows[hi_index].0))
}

/// Volume profile over the last `window` bars.
///
/// Each bar's per-bucket contribution is kept so that eviction subtracts it exactly.
#[derive(Debug, Clone)]
pub struct RollingVolumeProfile {
    window: usize,
    bucket_points: f64,
    value_area_pct: f64,
    contributions: VecDeque<Vec<(i64, f64)>>,
    buckets: BTreeMap<i64, f64>,
}

impl RollingVolumeProfile {
    #[must_use]
    pub fn new(window: usize, bucket_points: f64, value_area_pct: f64) -> Self {
        Self {
            window: window.max(1),
            bucket_points: if bucket_points > 0.0 {
                bucket_points
            } else {
                0.1
            },
            value_area_pct: value_area_pct.clamp(f64::MIN_POSITIVE, 1.0),
            contributions: VecDeque::new(),
            buckets: BTreeMap::new(),
        }
    }

    pub fn reset(&mut self) {
        self.contributions.clear();
        self.buckets.clear();
    }

    /// Number of bars currently in the window.
    #[must_use]
    pub fn len(&self) -> usize {
        self.contributions.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.contributions.is_empty()
    }

    /// True once the window holds `window` bars; a partial profile should not be acted on.
    #[must_use]
    pub fn is_full(&self) -> bool {
        self.contributions.len() >= self.window
    }

    /// Add a completed bar as in [`VolumeProfile::observe_bar`], evicting the oldest when full.
    pub fn push_bar(&mut self, high: f64, low: f64, volume: f64) {
        if !high.is_finite() || !low.is_finite() || !volume.is_finite() {
            return;
        }
        if volume <= 0.0 || high < low {
            return;
        }
        let bucket_of = |price: f64| (price / self.bucket_points).round() as i64;
        let (lo_bucket, hi_bucket) = (bucket_of(low), bucket_of(high));
        let span = (hi_bucket - lo_bucket + 1).max(1);
        let share = volume / span as f64;

        let mut contribution = Vec::with_capacity(span as usize);
        for bucket in lo_bucket..=hi_bucket {
            *self.buckets.entry(bucket).or_insert(0.0) += share;
            contribution.push((bucket, share));
        }
        self.contributions.push_back(contribution);

        while self.contributions.len() > self.window {
            if let Some(old) = self.contributions.pop_front() {
                for (bucket, share) in old {
                    if let Some(entry) = self.buckets.get_mut(&bucket) {
                        *entry -= share;
                        // drop float residue so it cannot become the POC
                        if *entry <= 1e-9 {
                            self.buckets.remove(&bucket);
                        }
                    }
                }
            }
        }
    }

    #[must_use]
    pub fn poc(&self) -> Option<f64> {
        self.value_area().map(|(poc, _, _)| poc)
    }

    #[must_use]
    pub fn value_area(&self) -> Option<(f64, f64, f64)> {
        let rows: Vec<(i64, f64)> = self.buckets.iter().map(|(k, v)| (*k, *v)).collect();
        let (poc, val, vah) = value_area_from_buckets(&rows, self.value_area_pct)?;
        Some((
            poc as f64 * self.bucket_points,
            val as f64 * self.bucket_points,
            vah as f64 * self.bucket_points,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const EPS: f64 = 1e-9;

    #[test]
    fn empty_profile_has_no_references() {
        let profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        assert!(profile.is_empty());
        assert!(profile.poc().is_none());
        assert!(profile.value_area().is_none());
    }

    #[test]
    fn poc_follows_volume_not_the_last_price() {
        // most volume trades at 1250.0 while the last prints move up to 1256.0
        let mut profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        for _ in 0..50 {
            profile.observe_tick(1250.0, 100.0);
        }
        for price in [1254.0, 1255.0, 1256.0] {
            profile.observe_tick(price, 1.0);
        }
        assert!((profile.poc().expect("poc") - 1250.0).abs() < EPS);
    }

    #[test]
    fn bar_volume_is_spread_across_its_range() {
        let mut profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        profile.observe_bar(1250.2, 1250.0, 30.0); // 3 buckets -> 10 each
        assert!((profile.total_volume() - 30.0).abs() < EPS);
        // A tick concentrated at the top makes 1250.2 the POC.
        profile.observe_tick(1250.2, 5.0);
        assert!((profile.poc().expect("poc") - 1250.2).abs() < EPS);
    }

    #[test]
    fn rejects_non_finite_and_non_positive_input() {
        let mut profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        profile.observe_tick(f64::NAN, 10.0);
        profile.observe_tick(1250.0, -5.0);
        profile.observe_tick(1250.0, 0.0);
        profile.observe_bar(1249.0, 1250.0, 10.0); // high < low
        profile.observe_bar(f64::INFINITY, 1250.0, 10.0);
        assert!(profile.is_empty());
    }

    #[test]
    fn reset_clears_the_session() {
        let mut profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        profile.observe_tick(1250.0, 10.0);
        profile.reset();
        assert!(profile.is_empty() && profile.total_volume() == 0.0);
    }

    #[test]
    fn value_area_is_contiguous_and_brackets_the_poc() {
        let mut profile = VolumeProfile::new(0.1, DEFAULT_VALUE_AREA_PCT);
        for (price, volume) in [
            (1249.8, 5.0),
            (1249.9, 40.0),
            (1250.0, 90.0),
            (1250.1, 60.0),
            (1250.2, 30.0),
        ] {
            profile.observe_tick(price, volume);
        }
        let (poc, val, vah) = profile.value_area().expect("value area");
        assert!(val <= poc && poc <= vah);
        assert!((poc - 1250.0).abs() < EPS);
        assert!(vah - val > 0.0);
    }

    #[test]
    fn rolling_window_evicts_exactly() {
        let mut rolling = RollingVolumeProfile::new(3, 0.1, DEFAULT_VALUE_AREA_PCT);
        assert!(!rolling.is_full());
        for _ in 0..3 {
            rolling.push_bar(1250.0, 1250.0, 10.0);
        }
        assert!(rolling.is_full());
        assert!((rolling.poc().expect("poc") - 1250.0).abs() < EPS);

        // Push three bars far away; the original level must be fully evicted.
        for _ in 0..3 {
            rolling.push_bar(1260.0, 1260.0, 10.0);
        }
        assert_eq!(rolling.len(), 3);
        assert!((rolling.poc().expect("poc") - 1260.0).abs() < EPS);
        assert!(
            !rolling.buckets.contains_key(&12500),
            "evicted bucket must be removed, not left as float dust"
        );
    }

    /// Parity with `market-profile-engine` and the Python mirror through the shared golden fixture.
    #[test]
    fn value_area_matches_the_cross_stack_golden_fixture() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../market-profile-engine/tests/fixtures/value_area_golden.json"
        );
        let raw = std::fs::read_to_string(path)
            .unwrap_or_else(|e| panic!("read golden fixture {path}: {e}"));
        let doc: serde_json::Value = serde_json::from_str(&raw).expect("parse golden fixture");

        for case in doc["cases"].as_array().expect("cases") {
            let name = case["name"].as_str().expect("name");
            let pct = case["value_area_pct"].as_f64().expect("pct");
            // Fixture prices sit on a 0.1 grid; scale to integer buckets.
            let rows: Vec<(i64, f64)> = case["rows"]
                .as_array()
                .expect("rows")
                .iter()
                .map(|row| {
                    (
                        (row[0].as_f64().expect("price") * 10.0).round() as i64,
                        row[1].as_f64().expect("weight"),
                    )
                })
                .collect();

            let (poc, val, vah) =
                value_area_from_buckets(&rows, pct).unwrap_or_else(|| panic!("case {name}"));
            let expected = &case["expected"];
            assert_eq!(
                poc,
                (expected["poc"].as_f64().expect("poc") * 10.0).round() as i64,
                "case {name}: POC"
            );
            assert_eq!(
                val,
                (expected["val"].as_f64().expect("val") * 10.0).round() as i64,
                "case {name}: VAL"
            );
            assert_eq!(
                vah,
                (expected["vah"].as_f64().expect("vah") * 10.0).round() as i64,
                "case {name}: VAH"
            );
        }
    }
}
