//! Property tests for value-area invariants on synthetic VN30F1M profiles (0.1 tick grid).
//!
//! POC inside `[VAL, VAH]`; value area of at least 70 % and minimal; sorted rows; single-price
//! collapse; determinism; volume conservation; ClickHouse-equal bucketing; fixed-width bucket ids.

use chrono::{DateTime, Duration, FixedOffset};
use market_profile_engine::{
    build_session_profile, build_tpo_profile_from_ticks, EngineTick, ProfileBuildConfig,
    ProfileSummary, ProfileType, TpoProfileRow, ValueAreaConfig, VolumeProfileRow,
    DEFAULT_VALUE_AREA_PCT,
};
use market_types::{EventTimestamp, Price, Volume};
use proptest::prelude::*;

const SYMBOL: &str = "VN30F1M";
/// VN30F1M tick size is 0.1 points.
const TICK_SIZE: f64 = 0.1;

fn tick_size() -> Price {
    Price::from_f64(TICK_SIZE).expect("tick size")
}

fn profile_config() -> ProfileBuildConfig {
    ProfileBuildConfig {
        price_grouping: 1,
        tick_size: tick_size(),
        tpo_bucket_minutes: 30,
    }
}

fn value_area_config() -> ValueAreaConfig {
    ValueAreaConfig {
        value_area_pct: DEFAULT_VALUE_AREA_PCT,
    }
}

fn session_base() -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("base datetime")
}

/// Tick at `price` with `volume`, `minute_offset` minutes into the morning session.
fn tick_at(price: f64, volume: i64, minute_offset: i64) -> EngineTick {
    let datetime = session_base() + Duration::minutes(minute_offset);
    EngineTick {
        timestamp: EventTimestamp(datetime.timestamp()),
        datetime,
        price: Price::from_f64(price).expect("finite price"),
        volume: Volume::new(volume).expect("non-negative volume"),
        side: None,
        last_trade_side: None,
    }
}

/// Grid price for a tick-offset in [0, 5000]: 900.0 + offset * 0.1.
fn grid_price(offset: i64) -> f64 {
    900.0 + offset as f64 * TICK_SIZE
}

fn volume_summary(ticks: &[EngineTick]) -> ProfileSummary<VolumeProfileRow> {
    let from = session_base();
    let to = from + Duration::minutes(150);
    build_session_profile(
        SYMBOL,
        from,
        to,
        ticks,
        ProfileType::Volume,
        profile_config(),
        value_area_config(),
    )
    .expect("session profile")
    .volume_profile
    .expect("volume profile requested")
}

fn tpo_summary(ticks: &[EngineTick]) -> ProfileSummary<TpoProfileRow> {
    build_tpo_profile_from_ticks(
        ticks,
        tick_size(),
        1,
        DEFAULT_VALUE_AREA_PCT,
        profile_config().tpo_bucket_minutes,
    )
    .expect("tpo profile")
}

/// Synthetic profile levels: (tick-offset, volume, minute-offset).
fn arb_levels() -> impl Strategy<Value = Vec<(i64, i64, i64)>> {
    proptest::collection::vec((0i64..=5000, 1i64..10_000, 0i64..150), 1..40)
}

fn ticks_from_levels(levels: &[(i64, i64, i64)]) -> Vec<EngineTick> {
    levels
        .iter()
        .map(|(offset, volume, minute)| tick_at(grid_price(*offset), *volume, *minute))
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    #[test]
    fn prop_volume_poc_within_value_area(levels in arb_levels()) {
        let summary = volume_summary(&ticks_from_levels(&levels));
        let poc = summary.poc.expect("poc");
        let vah = summary.vah.expect("vah");
        let val = summary.val.expect("val");
        prop_assert!(val <= poc, "POC {poc:?} below VAL {val:?}");
        prop_assert!(poc <= vah, "POC {poc:?} above VAH {vah:?}");
    }

    #[test]
    fn prop_volume_rows_sorted_and_va_ordered(levels in arb_levels()) {
        let summary = volume_summary(&ticks_from_levels(&levels));
        prop_assert!(summary.vah >= summary.val, "VAH below VAL");
        for pair in summary.rows.windows(2) {
            prop_assert!(pair[0].price < pair[1].price, "rows not strictly sorted");
        }
    }

    /// Minimality: removing the larger edge row takes the value area back below the target.
    #[test]
    fn prop_value_area_covers_threshold_and_is_minimal(levels in arb_levels()) {
        let summary = volume_summary(&ticks_from_levels(&levels));
        let vah = summary.vah.expect("vah");
        let val = summary.val.expect("val");
        let total: i64 = summary.rows.iter().map(|row| row.volume.0).sum();
        let target = total as f64 * DEFAULT_VALUE_AREA_PCT;
        let va_rows: Vec<&VolumeProfileRow> = summary
            .rows
            .iter()
            .filter(|row| row.price >= val && row.price <= vah)
            .collect();
        let va_volume: i64 = va_rows.iter().map(|row| row.volume.0).sum();
        prop_assert!(
            va_volume as f64 + 1e-6 >= target,
            "VA volume {va_volume} below target {target}"
        );
        if val == vah {
            // the POC row alone reached the target
            prop_assert_eq!(va_rows.len(), 1);
        } else {
            let low_edge = va_rows.first().expect("va low edge").volume.0;
            let high_edge = va_rows.last().expect("va high edge").volume.0;
            let max_edge = low_edge.max(high_edge);
            // the last-added level is an edge, so its volume is at most max_edge
            prop_assert!(
                ((va_volume - max_edge) as f64) < target + 1e-6,
                "VA not minimal: {va_volume} - {max_edge} still >= {target}"
            );
        }
    }

    #[test]
    fn prop_single_price_profile_collapses(
        offset in 0i64..=5000,
        volume in 1i64..10_000,
        minute in 0i64..150,
    ) {
        let summary = volume_summary(&[tick_at(grid_price(offset), volume, minute)]);
        let expected = Price::from_f64(grid_price(offset)).expect("grid price");
        prop_assert_eq!(summary.poc, Some(expected));
        prop_assert_eq!(summary.vah, Some(expected));
        prop_assert_eq!(summary.val, Some(expected));
        prop_assert_eq!(summary.rows.len(), 1);
    }

    #[test]
    fn prop_session_profile_is_deterministic(levels in arb_levels()) {
        let ticks = ticks_from_levels(&levels);
        let from = session_base();
        let to = from + Duration::minutes(150);
        let build = || {
            build_session_profile(
                SYMBOL,
                from,
                to,
                &ticks,
                ProfileType::Both,
                profile_config(),
                value_area_config(),
            )
            .expect("session profile")
        };
        prop_assert_eq!(build(), build());
    }

    #[test]
    fn prop_total_volume_preserved(levels in arb_levels()) {
        let summary = volume_summary(&ticks_from_levels(&levels));
        let input_total: i64 = levels.iter().map(|(_, volume, _)| *volume).sum();
        let row_total: i64 = summary.rows.iter().map(|row| row.volume.0).sum();
        prop_assert_eq!(input_total, row_total);
    }

    #[test]
    fn prop_tpo_poc_within_value_area(levels in arb_levels()) {
        let summary = tpo_summary(&ticks_from_levels(&levels));
        let poc = summary.poc.expect("poc");
        let vah = summary.vah.expect("vah");
        let val = summary.val.expect("val");
        prop_assert!(val <= poc && poc <= vah, "TPO POC outside VA");
    }

    /// Bucket ids are 12-digit strings whose string order is their numeric order.
    #[test]
    fn prop_tpo_bucket_ids_zero_padded_and_sorted(levels in arb_levels()) {
        let summary = tpo_summary(&ticks_from_levels(&levels));
        for row in &summary.rows {
            prop_assert_eq!(row.tpo_count, row.bucket_ids.len());
            for id in &row.bucket_ids {
                prop_assert_eq!(id.len(), 12, "bucket id not 12-wide: {}", id);
                prop_assert!(id.bytes().all(|b| b.is_ascii_digit()), "non-digit id {id}");
            }
            let numeric: Vec<u64> = row
                .bucket_ids
                .iter()
                .map(|id| id.parse::<u64>().expect("numeric id"))
                .collect();
            let mut sorted = numeric.clone();
            sorted.sort_unstable();
            prop_assert_eq!(numeric, sorted, "lexicographic order != numeric order");
        }
    }

    /// On 0.1-tick multiples with up to 1e-9 noise, the bucket equals ClickHouse `round(price, 1)`.
    #[test]
    fn prop_bucket_price_matches_clickhouse_round(
        offset in 0i64..=5000,
        noise in -1i32..=1,
    ) {
        let exact = grid_price(offset);
        let noisy = exact + f64::from(noise) * 1e-9;
        let summary = volume_summary(&[tick_at(noisy, 10, 5)]);
        let ch_key = (exact * 10.0).round() / 10.0;
        let bucket = summary.rows[0].price.as_f64();
        prop_assert!(
            (bucket - ch_key).abs() < 1e-9,
            "bucket {bucket} != CH key {ch_key} for price {noisy}"
        );
    }
}
