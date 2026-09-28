use super::*;
use chrono::DateTime;
use proptest::prelude::*;

/// Golden value-area cases, shared with the Python mirror in `shared/quant_core/profile.py`.
#[test]
fn value_area_matches_the_cross_stack_golden_fixture() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/value_area_golden.json"
    );
    let raw =
        std::fs::read_to_string(path).unwrap_or_else(|e| panic!("read golden fixture {path}: {e}"));
    let doc: serde_json::Value = serde_json::from_str(&raw).expect("parse golden fixture");
    let cases = doc["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "fixture must carry cases");

    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let pct = case["value_area_pct"].as_f64().expect("value_area_pct");
        let rows: Vec<(Price, i64)> = case["rows"]
            .as_array()
            .expect("rows array")
            .iter()
            .map(|row| {
                let price = Price::from_f64(row[0].as_f64().expect("price")).expect("price");
                (price, row[1].as_i64().expect("weight"))
            })
            .collect();

        let (poc, vah, val) =
            value_area_from_pairs(&rows, pct).unwrap_or_else(|e| panic!("case {name}: {e:?}"));
        let expected = &case["expected"];
        assert_eq!(
            poc.as_f64(),
            expected["poc"].as_f64().expect("poc"),
            "case {name}: POC"
        );
        assert_eq!(
            val.as_f64(),
            expected["val"].as_f64().expect("val"),
            "case {name}: VAL"
        );
        assert_eq!(
            vah.as_f64(),
            expected["vah"].as_f64().expect("vah"),
            "case {name}: VAH"
        );
    }
}

/// The vendored fixture must equal `shared/quant_core/testdata/value_area_golden.json` if present.
#[test]
fn vendored_golden_fixture_matches_the_python_copy_when_present() {
    let dir = env!("CARGO_MANIFEST_DIR");
    let python_copy = format!("{dir}/../../../shared/quant_core/testdata/value_area_golden.json");
    let Ok(theirs) = std::fs::read_to_string(&python_copy) else {
        eprintln!("skipped: {python_copy} is absent (workspace used on its own)");
        return;
    };
    let ours = std::fs::read_to_string(format!("{dir}/tests/fixtures/value_area_golden.json"))
        .expect("read the vendored golden fixture");
    assert_eq!(
        ours.replace("\r\n", "\n"),
        theirs.replace("\r\n", "\n"),
        "the vendored golden fixture has drifted from {python_copy}"
    );
}

/// Aggregated rows have no time dimension, so single prints and excess need the supplied IB.
#[test]
fn aggregated_path_yields_single_prints_and_excess_when_the_ib_is_supplied() {
    fn tpo(price: f64, count: u32) -> market_data_core::AggregatedTpoRow {
        market_data_core::AggregatedTpoRow {
            price_level: Price::from_f64(price).expect("price"),
            tpo_count: count,
        }
    }
    fn vol(price: f64, volume: i64) -> market_data_core::AggregatedVolumeRow {
        market_data_core::AggregatedVolumeRow {
            price: Price::from_f64(price).expect("price"),
            total_volume: Volume::new(volume).expect("volume"),
            buy_volume: Volume::new(volume).expect("volume"),
            sell_volume: Volume::new(0).expect("volume"),
        }
    }

    // a balanced middle with a thin one-bracket tail above the IB: 1251.5 is a single print and
    // 1252.0 the excess high
    let tpo_rows = vec![
        tpo(1249.0, 1),
        tpo(1249.5, 4),
        tpo(1250.0, 9),
        tpo(1250.5, 6),
        tpo(1251.0, 3),
        tpo(1251.5, 1),
        tpo(1252.0, 1),
    ];
    let volume_rows = vec![
        vol(1249.0, 5),
        vol(1249.5, 40),
        vol(1250.0, 90),
        vol(1250.5, 60),
        vol(1251.0, 30),
        vol(1251.5, 5),
        vol(1252.0, 5),
    ];
    let (from, to) = from_to("2026-08-20T09:00:00+07:00", "2026-08-20T14:45:00+07:00");

    let make = |ib: Option<(f64, f64)>| market_data_core::AggregatedProfileData {
        volume_rows: volume_rows.clone(),
        tpo_rows: tpo_rows.clone(),
        session_high: Price::from_f64(1252.0).ok(),
        session_low: Price::from_f64(1249.0).ok(),
        session_open: Price::from_f64(1250.0).ok(),
        ib_high: ib.and_then(|(high, _)| Price::from_f64(high).ok()),
        ib_low: ib.and_then(|(_, low)| Price::from_f64(low).ok()),
    };

    // --- with an IB: the references are computed ---
    let with_ib = build_session_profile_from_aggregated(
        "VN30F1M",
        from,
        to,
        &make(Some((1251.0, 1249.5))),
        ProfileType::Both,
        profile_config(),
        value_area_config(),
    )
    .expect("profile with ib");

    let refs = with_ib.auction_references.expect("auction references");
    assert_eq!(refs.ib_high, Price::from_f64(1251.0).ok());
    assert_eq!(refs.ib_low, Price::from_f64(1249.5).ok());

    let advanced = with_ib.advanced_references.expect("advanced references");
    assert!(advanced.ib_available, "an IB was supplied");
    assert!(
        advanced
            .single_print_prices
            .contains(&Price::from_f64(1251.5).expect("price")),
        "the one-bracket level above the IB is a single print: {:?}",
        advanced.single_print_prices
    );
    assert!(
        !advanced.excess_high.is_empty(),
        "the thin tail above the IB is excess: {:?}",
        advanced.excess_high
    );

    // --- without an IB: empty and marked as not computed ---
    let no_ib = build_session_profile_from_aggregated(
        "VN30F1M",
        from,
        to,
        &make(None),
        ProfileType::Both,
        profile_config(),
        value_area_config(),
    )
    .expect("profile without ib");
    let advanced = no_ib.advanced_references.expect("advanced references");
    assert!(
        !advanced.ib_available,
        "no IB was supplied — callers must be able to tell this apart from 'none found'"
    );
    assert!(advanced.single_print_prices.is_empty());
    assert!(advanced.excess_high.is_empty() && advanced.excess_low.is_empty());
    // poor highs and lows do not depend on the IB
    assert!(advanced.poor_high.is_some() || advanced.poor_low.is_some());
}

fn profile_config() -> ProfileBuildConfig {
    ProfileBuildConfig {
        price_grouping: 1,
        tick_size: Price::from_f64(0.5).expect("tick size"),
        tpo_bucket_minutes: 30,
    }
}

fn value_area_config() -> ValueAreaConfig {
    ValueAreaConfig {
        value_area_pct: DEFAULT_VALUE_AREA_PCT,
    }
}

fn from_to(from: &str, to: &str) -> (DateTime<FixedOffset>, DateTime<FixedOffset>) {
    (
        DateTime::parse_from_rfc3339(from).expect("from"),
        DateTime::parse_from_rfc3339(to).expect("to"),
    )
}

fn engine_tick(datetime: &str, price: f64, volume: i64) -> EngineTick {
    let datetime = DateTime::parse_from_rfc3339(datetime).expect("valid iso datetime");
    EngineTick {
        timestamp: EventTimestamp(datetime.timestamp()),
        datetime,
        price: Price::from_f64(price).expect("finite price"),
        volume: Volume::new(volume).expect("non-negative volume"),
        side: None,
        last_trade_side: None,
    }
}

fn replay_sample_ticks() -> Vec<EngineTick> {
    vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:15:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T09:45:00+07:00", 1201.0, 12),
    ]
}

fn replay_multi_session_ticks() -> Vec<EngineTick> {
    vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:15:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T13:30:00+07:00", 1201.0, 12),
        engine_tick("2026-04-18T09:00:00+07:00", 1201.5, 11),
        engine_tick("2026-04-18T09:10:00+07:00", 1202.0, 18),
    ]
}

#[test]
fn bucket_price_rounds_to_nearest_group() {
    let bucket = bucket_price(
        Price::from_f64(1200.1).expect("finite"),
        Price::from_f64(0.5).expect("finite"),
        1,
    )
    .expect("bucketed");
    assert_eq!(bucket.as_f64(), 1200.0);
}

#[test]
fn session_profile_builds_volume_and_tpo() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:20:00+07:00", 1201.0, 12),
        engine_tick("2026-04-17T10:10:00+07:00", 1199.5, 8),
        engine_tick("2026-04-17T13:15:00+07:00", 1202.0, 20),
    ];
    let profile = build_session_profile(
        "VN30F1M",
        DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("from"),
        DateTime::parse_from_rfc3339("2026-04-17T14:45:00+07:00").expect("to"),
        &ticks,
        ProfileType::Both,
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: Price::from_f64(0.5).expect("tick size"),
            tpo_bucket_minutes: 30,
        },
        ValueAreaConfig {
            value_area_pct: DEFAULT_VALUE_AREA_PCT,
        },
    )
    .expect("profile");
    assert_eq!(
        profile
            .volume_profile
            .as_ref()
            .and_then(|p| p.poc)
            .map(Price::as_f64),
        Some(1202.0)
    );
    // all rows tie at tpo_count 1: the POC is the price nearest the mid-range
    assert_eq!(
        profile
            .tpo_profile
            .as_ref()
            .and_then(|p| p.poc)
            .map(Price::as_f64),
        Some(1201.0)
    );
    assert!(profile.auction_references.is_some());
}

#[test]
fn replay_range_orders_ticks_deterministically() {
    let mut ticks = vec![
        engine_tick("2026-04-17T09:15:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:45:00+07:00", 1201.0, 12),
    ];
    ticks[0].timestamp = EventTimestamp(2);
    ticks[1].timestamp = EventTimestamp(1);
    ticks[2].timestamp = EventTimestamp(3);
    let replay = build_replay_range(
        "VN30F1M",
        DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("from"),
        DateTime::parse_from_rfc3339("2026-04-17T09:45:00+07:00").expect("to"),
        &ticks,
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: Price::from_f64(0.5).expect("tick size"),
            tpo_bucket_minutes: 30,
        },
        ValueAreaConfig {
            value_area_pct: DEFAULT_VALUE_AREA_PCT,
        },
    )
    .expect("replay");
    assert_eq!(replay.ticks.len(), 3);
    assert_eq!(replay.ticks[0].price.as_f64(), 1200.0);
    assert_eq!(replay.ticks[2].price.as_f64(), 1201.0);
}

#[test]
fn volume_profile_preserves_total_volume() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:01:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T09:02:00+07:00", 1200.0, 20),
    ];
    let profile = build_volume_profile(
        &ticks,
        Price::from_f64(0.5).expect("tick size"),
        1,
        DEFAULT_VALUE_AREA_PCT,
    )
    .expect("volume profile");
    let total_output: i64 = profile.rows.iter().map(|row| row.volume.0).sum();
    assert_eq!(total_output, 45);
}

#[test]
fn tpo_profile_prices_stay_ordered() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1201.0, 1),
        engine_tick("2026-04-17T09:05:00+07:00", 1199.5, 1),
        engine_tick("2026-04-17T09:35:00+07:00", 1200.0, 1),
    ];
    let profile = build_tpo_profile_from_ticks(
        &ticks,
        Price::from_f64(0.5).expect("tick size"),
        1,
        DEFAULT_VALUE_AREA_PCT,
        30,
    )
    .expect("tpo profile");
    let ordered = profile
        .rows
        .iter()
        .map(|row| row.price.0)
        .collect::<Vec<_>>();
    assert_eq!(ordered, vec![11_995_000, 12_000_000, 12_010_000]);
}

#[test]
fn replay_range_is_deterministic() {
    let mut ticks = vec![
        engine_tick("2026-04-17T09:15:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:45:00+07:00", 1201.0, 12),
    ];
    ticks[0].timestamp = EventTimestamp(2);
    ticks[1].timestamp = EventTimestamp(1);
    ticks[2].timestamp = EventTimestamp(3);
    let from = DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("from");
    let to = DateTime::parse_from_rfc3339("2026-04-17T09:45:00+07:00").expect("to");
    let left = build_replay_range(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("left");
    let right = build_replay_range(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("right");
    assert_eq!(left, right);
}

#[test]
fn session_profile_matches_base_auction_reference_oracle() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:20:00+07:00", 1201.0, 12),
        engine_tick("2026-04-17T10:10:00+07:00", 1199.5, 8),
        engine_tick("2026-04-17T13:15:00+07:00", 1202.0, 20),
    ];
    let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-17T14:45:00+07:00");
    let profile = build_session_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        ProfileType::Both,
        profile_config(),
        value_area_config(),
    )
    .expect("profile");

    let refs = profile.auction_references.expect("auction refs");
    assert_eq!(refs.session_open.as_f64(), 1200.0);
    assert_eq!(refs.session_high.as_f64(), 1202.0);
    assert_eq!(refs.session_low.as_f64(), 1199.5);
    assert_eq!(refs.ib_high.map(Price::as_f64), Some(1201.0));
    assert_eq!(refs.ib_low.map(Price::as_f64), Some(1200.0));
    // all TPO counts tie: the POC is the price nearest the mid-range (1200.75)
    assert_eq!(refs.tpo_poc.map(Price::as_f64), Some(1201.0));
    assert_eq!(refs.volume_poc.map(Price::as_f64), Some(1202.0));
}

#[test]
fn context_profile_carries_auction_references_per_session() {
    let ticks = vec![
        engine_tick("2026-04-16T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-16T09:20:00+07:00", 1201.0, 12),
        engine_tick("2026-04-16T10:10:00+07:00", 1199.5, 8),
        engine_tick("2026-04-16T13:15:00+07:00", 1202.0, 20),
        engine_tick("2026-04-17T09:00:00+07:00", 1203.0, 15),
        engine_tick("2026-04-17T09:20:00+07:00", 1204.0, 18),
        engine_tick("2026-04-17T10:10:00+07:00", 1202.5, 12),
        engine_tick("2026-04-17T13:15:00+07:00", 1204.5, 25),
    ];
    let profile = build_context_profile(
        "VN30F1M",
        &ticks,
        2,
        ContextMode::Standard,
        0,
        profile_config(),
        value_area_config(),
    )
    .expect("context");

    let first = profile.sessions[0]
        .auction_references
        .as_ref()
        .expect("first auction refs");
    assert_eq!(first.session_open.as_f64(), 1200.0);
    assert_eq!(first.session_high.as_f64(), 1202.0);
    assert_eq!(first.session_low.as_f64(), 1199.5);
    assert_eq!(first.ib_high.map(Price::as_f64), Some(1201.0));
    assert_eq!(first.ib_low.map(Price::as_f64), Some(1200.0));
    // all TPO counts tie: the price nearest the mid-range wins
    assert_eq!(first.tpo_poc.map(Price::as_f64), Some(1201.0));
    assert_eq!(first.volume_poc.map(Price::as_f64), Some(1202.0));

    let second = profile.sessions[1]
        .auction_references
        .as_ref()
        .expect("second auction refs");
    assert_eq!(second.session_open.as_f64(), 1203.0);
    assert_eq!(second.session_high.as_f64(), 1204.5);
    assert_eq!(second.session_low.as_f64(), 1202.5);
    assert_eq!(second.ib_high.map(Price::as_f64), Some(1204.0));
    assert_eq!(second.ib_low.map(Price::as_f64), Some(1203.0));
    // 1203.0 and 1204.0 are equidistant from the mid-range: the higher price wins
    assert_eq!(second.tpo_poc.map(Price::as_f64), Some(1204.0));
    assert_eq!(second.volume_poc.map(Price::as_f64), Some(1204.5));
}

#[test]
fn session_profile_matches_advanced_reference_oracle() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1198.5, 2),
        engine_tick("2026-04-17T09:10:00+07:00", 1198.5, 2),
        engine_tick("2026-04-17T09:20:00+07:00", 1199.0, 4),
        engine_tick("2026-04-17T09:40:00+07:00", 1200.0, 6),
        engine_tick("2026-04-17T10:00:00+07:00", 1200.0, 6),
        engine_tick("2026-04-17T10:20:00+07:00", 1201.0, 4),
        engine_tick("2026-04-17T10:40:00+07:00", 1201.0, 4),
        engine_tick("2026-04-17T11:00:00+07:00", 1201.5, 1),
        engine_tick("2026-04-17T13:15:00+07:00", 1202.0, 5),
        engine_tick("2026-04-17T13:30:00+07:00", 1202.0, 5),
        engine_tick("2026-04-17T14:00:00+07:00", 1203.0, 2),
        engine_tick("2026-04-17T14:20:00+07:00", 1203.0, 2),
    ];
    let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-17T14:45:00+07:00");
    let profile = build_session_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        ProfileType::Both,
        profile_config(),
        value_area_config(),
    )
    .expect("profile");

    let refs = profile.advanced_references.expect("advanced refs");
    assert_eq!(refs.poor_high.map(Price::as_f64), Some(1203.0));
    assert_eq!(refs.poor_low.map(Price::as_f64), Some(1198.5));
    assert_eq!(
        refs.single_print_prices
            .iter()
            .map(|price| price.as_f64())
            .collect::<Vec<_>>(),
        vec![1201.5]
    );
    assert_eq!(
        refs.excess_high
            .iter()
            .map(|price| price.as_f64())
            .collect::<Vec<_>>(),
        vec![1203.0]
    );
    assert_eq!(
        refs.excess_low
            .iter()
            .map(|price| price.as_f64())
            .collect::<Vec<_>>(),
        vec![1198.5]
    );
}

#[test]
fn context_profile_uses_shared_price_axis_for_visible_sessions() {
    let ticks = vec![
        engine_tick("2026-04-16T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-16T10:00:00+07:00", 1200.5, 12),
        engine_tick("2026-04-17T09:00:00+07:00", 1201.0, 14),
        engine_tick("2026-04-17T10:00:00+07:00", 1201.5, 16),
        engine_tick("2026-04-18T09:00:00+07:00", 1202.0, 18),
        engine_tick("2026-04-18T10:00:00+07:00", 1202.5, 20),
    ];
    let profile = build_context_profile(
        "VN30F1M",
        &ticks,
        3,
        ContextMode::Standard,
        0,
        profile_config(),
        value_area_config(),
    )
    .expect("context");

    assert_eq!(profile.sessions.len(), 3);
    let shared = &profile.sessions[0].shared_price_levels;
    assert!(!shared.is_empty());
    assert_eq!(shared, &profile.shared_price_levels);
    assert!(shared.windows(2).all(|window| window[0] <= window[1]));
    assert!(profile
        .sessions
        .iter()
        .all(|session| session.shared_price_levels == *shared));
    assert!(profile.sessions[0].session_key < profile.sessions[2].session_key);
}

#[test]
fn composite_profile_has_deterministic_applied_range_and_interval() {
    let ticks = vec![
        engine_tick("2026-04-17T09:15:00+07:00", 1251.0, 10),
        engine_tick("2026-04-17T09:20:00+07:00", 1251.5, 12),
    ];
    let (from, to) = from_to("2026-04-17T09:15:00+07:00", "2026-04-17T09:20:00+07:00");
    let profile = build_composite_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("composite");

    assert_eq!(profile.from_iso, "2026-04-17T09:15:00+07:00");
    assert_eq!(profile.to_iso, "2026-04-17T09:20:00+07:00");
    assert_eq!(profile.overview_interval, "M5");
}

#[test]
fn replay_range_matches_canonical_custom_range_oracle() {
    let profile = build_replay_range(
        "VN30F1M",
        DateTime::parse_from_rfc3339("2026-04-17T09:00:00+07:00").expect("from"),
        DateTime::parse_from_rfc3339("2026-04-17T09:45:00+07:00").expect("to"),
        &replay_sample_ticks(),
        profile_config(),
        value_area_config(),
    )
    .expect("replay range");

    assert_eq!(profile.from_iso, "2026-04-17T09:00:00+07:00");
    assert_eq!(profile.to_iso, "2026-04-17T09:45:00+07:00");
    assert_eq!(profile.ticks.len(), 3);
    assert_eq!(profile.ticks[0].price.as_f64(), 1200.0);
    assert_eq!(profile.ticks[1].price.as_f64(), 1200.5);
    assert_eq!(profile.ticks[2].price.as_f64(), 1201.0);
}

#[test]
fn replay_final_matches_session_profile_for_single_session_range() {
    let ticks = replay_sample_ticks();
    let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-17T09:45:00+07:00");
    let historical = build_session_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        ProfileType::Both,
        profile_config(),
        value_area_config(),
    )
    .expect("historical");
    let replay_final = build_replay_final_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("replay final");

    match replay_final {
        EitherProfile::Session(session) => {
            assert_eq!(session.volume_profile, historical.volume_profile);
            assert_eq!(session.tpo_profile, historical.tpo_profile);
            assert_eq!(session.auction_references, historical.auction_references);
            assert_eq!(session.advanced_references, historical.advanced_references);
        }
        EitherProfile::Composite(_) => panic!("expected session replay profile"),
    }
}

#[test]
fn replay_final_matches_composite_profile_for_multi_session_range() {
    let ticks = replay_multi_session_ticks();
    let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-18T09:10:00+07:00");
    let historical = build_composite_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("historical");
    let replay_final = build_replay_final_profile(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("replay final");

    match replay_final {
        EitherProfile::Composite(composite) => {
            assert_eq!(composite.volume_profile, historical.volume_profile);
            assert_eq!(composite.market_profile, historical.market_profile);
            assert_eq!(composite.auction_references, historical.auction_references);
            assert_eq!(
                composite.advanced_references,
                historical.advanced_references
            );
        }
        EitherProfile::Session(_) => panic!("expected composite replay profile"),
    }
}

#[test]
fn bucket_price_is_deterministic_for_same_input() {
    let input = Price::from_f64(1201.24).expect("price");
    let left = bucket_price(
        input,
        profile_config().tick_size,
        profile_config().price_grouping,
    )
    .expect("left bucket");
    let right = bucket_price(
        input,
        profile_config().tick_size,
        profile_config().price_grouping,
    )
    .expect("right bucket");
    assert_eq!(left, right);
}

#[test]
fn volume_profile_handles_zero_volume_without_panic() {
    let ticks = vec![
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 0),
        engine_tick("2026-04-17T09:01:00+07:00", 1200.5, 0),
    ];
    let profile = build_volume_profile(
        &ticks,
        profile_config().tick_size,
        profile_config().price_grouping,
        value_area_config().value_area_pct,
    )
    .expect("zero volume profile");
    assert!(profile.rows.is_empty());
    assert!(profile.poc.is_none());
    assert!(profile.vah.is_none());
    assert!(profile.val.is_none());
}

#[test]
fn replay_range_filters_and_orders_same_input_consistently() {
    let ticks = vec![
        engine_tick("2026-04-17T09:45:00+07:00", 1201.0, 12),
        engine_tick("2026-04-17T08:59:59+07:00", 1199.5, 5),
        engine_tick("2026-04-17T09:15:00+07:00", 1200.5, 15),
        engine_tick("2026-04-17T09:00:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:46:00+07:00", 1201.5, 8),
    ];
    let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-17T09:45:00+07:00");
    let replay = build_replay_range(
        "VN30F1M",
        from,
        to,
        &ticks,
        profile_config(),
        value_area_config(),
    )
    .expect("replay range");
    assert_eq!(replay.ticks.len(), 3);
    assert_eq!(
        replay
            .ticks
            .iter()
            .map(|tick| tick.price.as_f64())
            .collect::<Vec<_>>(),
        vec![1200.0, 1200.5, 1201.0]
    );
}

// --- tie rules and edge cases ---

#[test]
fn poc_tie_resolves_to_price_closest_to_midrange() {
    // range 1200-1204, mid 1202: of the tied 1200.5 and 1202.5, the nearer 1202.5 wins
    let rows = vec![
        (Price::from_f64(1200.0).unwrap(), 1),
        (Price::from_f64(1200.5).unwrap(), 10),
        (Price::from_f64(1201.0).unwrap(), 2),
        (Price::from_f64(1202.5).unwrap(), 10),
        (Price::from_f64(1204.0).unwrap(), 1),
    ];
    let (poc, _, _) = value_area_from_pairs(&rows, 0.7).expect("value area");
    assert_eq!(poc.as_f64(), 1202.5);
}

#[test]
fn poc_tie_equidistant_from_midrange_resolves_to_higher_price() {
    // range 1200-1204, mid 1202: 1201.0 and 1203.0 are equidistant, so 1203.0 wins
    let rows = vec![
        (Price::from_f64(1200.0).unwrap(), 1),
        (Price::from_f64(1201.0).unwrap(), 10),
        (Price::from_f64(1203.0).unwrap(), 10),
        (Price::from_f64(1204.0).unwrap(), 1),
    ];
    let (poc, _, _) = value_area_from_pairs(&rows, 0.7).expect("value area");
    assert_eq!(poc.as_f64(), 1203.0);
}

#[test]
fn value_area_stops_when_target_reached_exactly() {
    // the POC holds 50 of 100 at pct 0.5, so the value area is that one row
    let rows = vec![
        (Price::from_f64(1200.0).unwrap(), 25),
        (Price::from_f64(1200.5).unwrap(), 50),
        (Price::from_f64(1201.0).unwrap(), 25),
    ];
    let (poc, vah, val) = value_area_from_pairs(&rows, 0.5).expect("value area");
    assert_eq!(poc.as_f64(), 1200.5);
    assert_eq!(vah.as_f64(), 1200.5);
    assert_eq!(val.as_f64(), 1200.5);
}

#[test]
fn value_area_single_row_profile_is_degenerate_but_valid() {
    let rows = vec![(Price::from_f64(1200.0).unwrap(), 7)];
    let (poc, vah, val) = value_area_from_pairs(&rows, 0.7).expect("value area");
    assert_eq!(poc.as_f64(), 1200.0);
    assert_eq!(vah.as_f64(), 1200.0);
    assert_eq!(val.as_f64(), 1200.0);
}

#[test]
fn raw_and_aggregated_paths_agree_on_poc_vah_val() {
    // the same market as raw ticks and as aggregated rows gives the same POC, VAH and VAL
    let ticks = vec![
        engine_tick("2026-04-17T09:05:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T09:10:00+07:00", 1200.5, 40),
        engine_tick("2026-04-17T09:40:00+07:00", 1200.5, 10),
        engine_tick("2026-04-17T09:41:00+07:00", 1201.0, 30),
        engine_tick("2026-04-17T10:10:00+07:00", 1201.5, 10),
    ];
    let raw =
        build_volume_profile(&ticks, Price::from_f64(0.1).unwrap(), 1, 0.7).expect("raw profile");
    let volume_rows = vec![
        market_data_core::AggregatedVolumeRow {
            price: Price::from_f64(1200.0).unwrap(),
            total_volume: Volume::new(10).unwrap(),
            buy_volume: Volume::new(0).unwrap(),
            sell_volume: Volume::new(0).unwrap(),
        },
        market_data_core::AggregatedVolumeRow {
            price: Price::from_f64(1200.5).unwrap(),
            total_volume: Volume::new(50).unwrap(),
            buy_volume: Volume::new(0).unwrap(),
            sell_volume: Volume::new(0).unwrap(),
        },
        market_data_core::AggregatedVolumeRow {
            price: Price::from_f64(1201.0).unwrap(),
            total_volume: Volume::new(30).unwrap(),
            buy_volume: Volume::new(0).unwrap(),
            sell_volume: Volume::new(0).unwrap(),
        },
        market_data_core::AggregatedVolumeRow {
            price: Price::from_f64(1201.5).unwrap(),
            total_volume: Volume::new(10).unwrap(),
            buy_volume: Volume::new(0).unwrap(),
            sell_volume: Volume::new(0).unwrap(),
        },
    ];
    let agg = build_volume_profile_from_rows(&volume_rows, Price::from_f64(0.1).unwrap(), 1, 0.7)
        .expect("aggregated profile");
    assert_eq!(raw.poc, agg.poc);
    assert_eq!(raw.vah, agg.vah);
    assert_eq!(raw.val, agg.val);
}

#[test]
fn tpo_bucket_ids_sort_chronologically_across_digit_rollover() {
    // epoch / 1800 gains a digit between these two times; padding keeps string order
    let before = DateTime::parse_from_rfc3339("2026-12-31T09:00:00+07:00").unwrap();
    let after = DateTime::parse_from_rfc3339("2027-02-02T09:00:00+07:00").unwrap();
    let id_before = tpo_bucket_id(before, 1800);
    let id_after = tpo_bucket_id(after, 1800);
    assert!(id_before < id_after, "{id_before} !< {id_after}");
    assert_eq!(id_before.len(), id_after.len());
}

#[test]
fn aggregated_synthetic_bucket_ids_sort_numerically_past_ten() {
    let rows = vec![market_data_core::AggregatedTpoRow {
        price_level: Price::from_f64(1200.0).unwrap(),
        tpo_count: 12,
    }];
    let profile = build_tpo_profile_from_aggregated(&rows, Price::from_f64(0.1).unwrap(), 1, 0.7)
        .expect("aggregated tpo");
    let ids = &profile.rows[0].bucket_ids;
    let mut sorted = ids.clone();
    sorted.sort();
    assert_eq!(&sorted, ids, "synthetic ids must already be sort-stable");
}

#[test]
fn bucket_price_matches_clickhouse_round_on_tick_multiples() {
    // ClickHouse rounds half to even, Rust half away from zero; prices are 0.1-tick multiples,
    // so no .05 midpoint occurs and the two agree even with float error
    let tick = Price::from_f64(0.1).unwrap();
    for i in 0..2000i64 {
        let exact = 1900.0 + (i as f64) * 0.1;
        let noisy = Price::from_f64(exact + 1e-9).unwrap();
        let bucket = bucket_price(noisy, tick, 1).expect("bucket");
        let ch_key = (exact * 10.0).round() / 10.0;
        assert_eq!(bucket.as_f64(), ch_key, "price {exact}");
    }
}

#[test]
fn lunch_gap_ticks_are_rejected_by_initial_balance_guard() {
    // builders do not filter by time; the IB path rejects out-of-session input
    let ticks = vec![
        engine_tick("2026-04-17T09:05:00+07:00", 1200.0, 10),
        engine_tick("2026-04-17T12:00:00+07:00", 1200.5, 10),
    ];
    let result = calculate_initial_balance(&ticks);
    assert!(matches!(result, Err(ProfileError::OutsideTradingHours)));
}

proptest! {
    #[test]
    fn bucket_price_is_stable_across_repeated_calls(
        raw_price in -20_000.0f64..20_000.0f64,
        raw_tick_size in 0.1f64..10.0f64,
        grouping in 1i32..8i32,
    ) {
        let price = Price::from_f64(raw_price).expect("finite price");
        let tick_size = Price::from_f64(raw_tick_size).expect("finite tick size");
        let left = bucket_price(price, tick_size, grouping).expect("left bucket");
        let right = bucket_price(price, tick_size, grouping).expect("right bucket");
        prop_assert_eq!(left, right);
    }

    #[test]
    fn volume_profile_preserves_total_volume_under_positive_input(
        prices in prop::collection::vec(-5_000.0f64..5_000.0f64, 1..20),
        volumes in prop::collection::vec(0i64..5_000i64, 1..20),
    ) {
        let len = prices.len().min(volumes.len());
        let ticks = prices
            .into_iter()
            .zip(volumes)
            .take(len)
            .enumerate()
            .map(|(idx, (price, volume))| {
                engine_tick(
                    &format!("2026-04-17T09:{:02}:00+07:00", idx % 60),
                    price,
                    volume,
                )
            })
            .collect::<Vec<_>>();
        let profile = build_volume_profile(
            &ticks,
            profile_config().tick_size,
            profile_config().price_grouping,
            value_area_config().value_area_pct,
        ).expect("volume profile");
        let input_total: i64 = ticks.iter().map(|tick| tick.volume.0).sum();
        let output_total: i64 = profile.rows.iter().map(|row| row.volume.0).sum();
        prop_assert_eq!(input_total, output_total);
    }

    #[test]
    fn tpo_profile_prices_remain_sorted_after_bucketing(
        prices in prop::collection::vec(-5_000.0f64..5_000.0f64, 1..20),
    ) {
        let ticks = prices
            .into_iter()
            .enumerate()
            .map(|(idx, price)| {
                engine_tick(
                    &format!("2026-04-17T09:{:02}:00+07:00", idx % 60),
                    price,
                    1,
                )
            })
            .collect::<Vec<_>>();
        let profile = build_tpo_profile_from_ticks(
            &ticks,
            profile_config().tick_size,
            profile_config().price_grouping,
            value_area_config().value_area_pct,
            profile_config().tpo_bucket_minutes,
        ).expect("tpo profile");
        let ordered = profile.rows.iter().map(|row| row.price.0).collect::<Vec<_>>();
        prop_assert!(ordered.windows(2).all(|window| window[0] <= window[1]));
    }

    #[test]
    fn replay_range_is_deterministic_under_unsorted_input(
        prices in prop::collection::vec(-5_000.0f64..5_000.0f64, 3..12),
        volumes in prop::collection::vec(0i64..5_000i64, 3..12),
    ) {
        let len = prices.len().min(volumes.len());
        let mut ticks = prices
            .into_iter()
            .zip(volumes)
            .take(len)
            .enumerate()
            .map(|(idx, (price, volume))| {
                let minute = (len - 1 - idx) % 45;
                let mut tick = engine_tick(
                    &format!("2026-04-17T09:{:02}:00+07:00", minute),
                    price,
                    volume,
                );
                tick.timestamp = EventTimestamp((len - idx) as i64);
                tick
            })
            .collect::<Vec<_>>();
        ticks.reverse();
        let (from, to) = from_to("2026-04-17T09:00:00+07:00", "2026-04-17T09:44:00+07:00");
        let left = build_replay_range(
            "VN30F1M",
            from,
            to,
            &ticks,
            profile_config(),
            value_area_config(),
        ).expect("left replay");
        let right = build_replay_range(
            "VN30F1M",
            from,
            to,
            &ticks,
            profile_config(),
            value_area_config(),
        ).expect("right replay");
        prop_assert_eq!(left, right);
    }
}
