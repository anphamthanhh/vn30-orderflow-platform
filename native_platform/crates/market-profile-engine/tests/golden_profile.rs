//! Golden tests on hand-made profiles in `tests/fixtures/*.json`, compared at 0.1-tick precision.
//!
//! POC ties go to the level nearest the mid-range, then to the higher price; the value area grows
//! one level per step, taking the lower side on equal weight.

use chrono::{DateTime, Duration, FixedOffset};
use market_profile_engine::{
    build_session_profile, build_tpo_profile_from_ticks, EngineTick, ProfileBuildConfig,
    ProfileSummary, ProfileType, ValueAreaConfig, VolumeProfileRow,
};
use market_types::{EventTimestamp, Price, Volume};
use serde::Deserialize;

const SYMBOL: &str = "VN30F1M";
/// VN30F1M tick size is 0.1 points.
const TICK_SIZE: f64 = 0.1;

#[derive(Debug, Deserialize)]
struct Fixture {
    value_area_pct: f64,
    levels: Vec<Level>,
    expected: Expected,
}

#[derive(Debug, Deserialize)]
struct Level {
    price: f64,
    volume: i64,
}

#[derive(Debug, Deserialize)]
struct Expected {
    poc: f64,
    vah: f64,
    val: f64,
    va_volume: i64,
}

fn load_fixture(name: &str) -> Fixture {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read fixture {path}: {error}"));
    serde_json::from_str(&raw).unwrap_or_else(|error| panic!("parse fixture {path}: {error}"))
}

/// Round to the 0.1 tick before comparing (cross-OS determinism).
fn round_tick(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn assert_price_eq(actual: Option<Price>, expected: f64, label: &str) {
    let actual = actual.unwrap_or_else(|| panic!("{label} missing"));
    let actual = round_tick(actual.as_f64());
    let expected = round_tick(expected);
    assert!(
        (actual - expected).abs() < 1e-9,
        "{label}: engine {actual} != golden {expected}"
    );
}

fn session_base() -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("base datetime")
}

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

fn volume_summary_for(fixture: &Fixture) -> ProfileSummary<VolumeProfileRow> {
    let ticks: Vec<EngineTick> = fixture
        .levels
        .iter()
        .map(|level| tick_at(level.price, level.volume, 5))
        .collect();
    let from = session_base();
    let to = from + Duration::minutes(150);
    build_session_profile(
        SYMBOL,
        from,
        to,
        &ticks,
        ProfileType::Volume,
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: Price::from_f64(TICK_SIZE).expect("tick size"),
            tpo_bucket_minutes: 30,
        },
        ValueAreaConfig {
            value_area_pct: fixture.value_area_pct,
        },
    )
    .expect("session profile")
    .volume_profile
    .expect("volume profile requested")
}

fn assert_matches_fixture(fixture: &Fixture, summary: &ProfileSummary<VolumeProfileRow>) {
    assert_price_eq(summary.poc, fixture.expected.poc, "POC");
    assert_price_eq(summary.vah, fixture.expected.vah, "VAH");
    assert_price_eq(summary.val, fixture.expected.val, "VAL");
    // one row per distinct fixture level: no phantom rows
    assert_eq!(summary.rows.len(), fixture.levels.len(), "row count");
    let vah = summary.vah.expect("vah");
    let val = summary.val.expect("val");
    let va_volume: i64 = summary
        .rows
        .iter()
        .filter(|row| row.price >= val && row.price <= vah)
        .map(|row| row.volume.0)
        .sum();
    assert_eq!(va_volume, fixture.expected.va_volume, "VA volume");
}

/// Equal volume at every level of 1300.0-1300.8: the POC is the mid-range level, 1300.4.
#[test]
fn golden_all_tied_poc_is_closest_to_midrange() {
    let fixture = load_fixture("all_tied.json");
    let summary = volume_summary_for(&fixture);
    assert_matches_fixture(&fixture, &summary);
}

/// An even level count puts the mid-range between two levels; the higher one wins.
#[test]
fn golden_all_tied_even_levels_tie_resolves_to_higher_price() {
    let fixture = load_fixture("all_tied_even.json");
    let summary = volume_summary_for(&fixture);
    assert_matches_fixture(&fixture, &summary);
}

/// A unique maximum wins outright; the value area grows toward the second peak.
#[test]
fn golden_bimodal_profile() {
    let fixture = load_fixture("bimodal.json");
    let summary = volume_summary_for(&fixture);
    assert_matches_fixture(&fixture, &summary);
}

/// Untraded levels inside the range get no rows; tie-break and value area use traded levels.
#[test]
fn golden_gap_session_has_no_phantom_rows() {
    let fixture = load_fixture("gap_session.json");
    let summary = volume_summary_for(&fixture);
    assert_matches_fixture(&fixture, &summary);
    let gap_prices = [1310.2, 1310.3, 1310.4];
    for gap in gap_prices {
        let gap = Price::from_f64(gap).expect("gap price");
        assert!(
            summary.rows.iter().all(|row| row.price != gap),
            "phantom row at gap price {gap:?}"
        );
    }
}

/// Of two tied levels away from the mid-range, the nearer wins even if it is not the lower one.
#[test]
fn golden_two_level_tie_off_midrange() {
    let fixture = load_fixture("two_level_tie_off_midrange.json");
    let summary = volume_summary_for(&fixture);
    assert_matches_fixture(&fixture, &summary);
}

/// TPO path: one print per level in one 30-minute bucket ties every level, as in `all_tied.json`.
#[test]
fn golden_all_tied_tpo_path_matches_volume_expectations() {
    let fixture = load_fixture("all_tied.json");
    let ticks: Vec<EngineTick> = fixture
        .levels
        .iter()
        .map(|level| tick_at(level.price, level.volume, 5))
        .collect();
    let summary = build_tpo_profile_from_ticks(
        &ticks,
        Price::from_f64(TICK_SIZE).expect("tick size"),
        1,
        fixture.value_area_pct,
        30,
    )
    .expect("tpo profile");
    assert!(
        summary.rows.iter().all(|row| row.tpo_count == 1),
        "expected an all-tied TPO profile"
    );
    assert_price_eq(summary.poc, fixture.expected.poc, "TPO POC");
    assert_price_eq(summary.vah, fixture.expected.vah, "TPO VAH");
    assert_price_eq(summary.val, fixture.expected.val, "TPO VAL");
}

/// Bucket ids keep a fixed width and time order across the epoch / 1800 digit rollover.
#[test]
fn golden_tpo_bucket_ids_survive_epoch_digit_rollover() {
    let before = tick_at(1300.0, 10, 0);
    let mut after = tick_at(1300.0, 10, 0);
    after.datetime = DateTime::parse_from_rfc3339("2027-02-02T09:00:00+07:00").expect("after");
    after.timestamp = EventTimestamp(after.datetime.timestamp());
    let mut early = before.clone();
    early.datetime = DateTime::parse_from_rfc3339("2026-12-31T09:00:00+07:00").expect("early");
    early.timestamp = EventTimestamp(early.datetime.timestamp());

    let summary = build_tpo_profile_from_ticks(
        &[early, after],
        Price::from_f64(TICK_SIZE).expect("tick size"),
        1,
        0.7,
        30,
    )
    .expect("tpo profile");
    assert_eq!(summary.rows.len(), 1);
    let ids = &summary.rows[0].bucket_ids;
    assert_eq!(ids.len(), 2);
    assert!(
        ids.iter().all(|id| id.len() == 12),
        "ids not 12-wide: {ids:?}"
    );
    assert!(ids[0] < ids[1], "lexicographic order broke: {ids:?}");
    let numeric: Vec<u64> = ids.iter().map(|id| id.parse().expect("numeric")).collect();
    assert!(
        numeric[0] < numeric[1],
        "chronological order broke: {numeric:?}"
    );
}
