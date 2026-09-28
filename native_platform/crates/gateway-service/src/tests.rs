use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;

use super::*;
use async_trait::async_trait;
use market_data_core::{
    AggregatedProfileData, AggregatedProfileProvider, AggregatedProfileQuery, AggregatedTpoRow,
    AggregatedVolumeRow, HistoricalBarProvider, HistoricalTickProvider, ReplayRangeData,
    ReplayRangeLoadResult, ReplayRangeMeta, ReplayRangeProvider, ReplaySequenceMode,
    ReplayTickData,
};
use market_profile_engine::{
    build_session_profile, ProfileBuildConfig, ProfileType, ValueAreaConfig,
};

#[derive(Debug, Clone)]
struct TestHistoricalBars;

#[async_trait]
impl HistoricalBarProvider for TestHistoricalBars {
    async fn load_bars(
        &self,
        _query: HistoricalBarQuery,
    ) -> Result<Vec<OhlcBar>, GatewayDataError> {
        Ok(Vec::new())
    }
}

#[derive(Debug, Clone)]
struct TestHistoricalTicks;

#[async_trait]
impl HistoricalTickProvider for TestHistoricalTicks {
    async fn load_ticks(&self, _query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError> {
        Ok(vec![Tick {
            symbol: Symbol::new("VN30F1M").expect("symbol"),
            timestamp: EventTimestamp::new(1_776_391_200).expect("timestamp"),
            price: market_types::Price::from_f64(1251.4).expect("price"),
            volume: market_types::Volume::new(12).expect("volume"),
            side: Some(TickSide::Buy),
        }])
    }
}

#[derive(Debug, Clone)]
struct TestReplayRanges {
    has_value: bool,
}

#[async_trait]
impl ReplayRangeProvider for TestReplayRanges {
    async fn load_replay_range(
        &self,
        _query: ReplayRangeQuery,
    ) -> Result<ReplayRangeLoadResult, GatewayDataError> {
        if !self.has_value {
            return Ok(ReplayRangeLoadResult::NotFound);
        }

        Ok(ReplayRangeLoadResult::Found(ReplayRangeData {
            meta: ReplayRangeMeta {
                session_key: SessionKey::new("2026-04-23-am").expect("session key"),
                from_timestamp: EventTimestamp::new(1).expect("from"),
                to_timestamp: EventTimestamp::new(2).expect("to"),
                tick_count: 1,
                sequence_mode: ReplaySequenceMode::SourceAuthoritative,
            },
            ticks: vec![ReplayTickData {
                tick: Tick {
                    symbol: Symbol::new("VN30F1M").expect("symbol"),
                    timestamp: EventTimestamp::new(1).expect("timestamp"),
                    price: market_types::Price::from_f64(1251.4).expect("price"),
                    volume: market_types::Volume::new(12).expect("volume"),
                    side: Some(TickSide::Buy),
                },
                event_sequence: Some(42),
            }],
        }))
    }
}

#[tokio::test]
async fn historical_bar_request_returns_data_payload() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_historical_bars(SymbolRangeRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: Some(10),
            to_timestamp: Some(20),
            resolution: None,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::HistoricalBars(response) => {
            assert_eq!(response.symbol, "VN30F1M");
            assert!(response.bars.is_empty());
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn replay_range_request_returns_error_when_not_found() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_replay_range(ReplayRangeRequest {
            symbol: "VN30F1M".to_owned(),
            replay_key: "session-replay".to_owned(),
            from_timestamp: 10,
            to_timestamp: 20,
        })
        .await
        .expect("payload");

    assert!(matches!(payload, OutboundPayload::Error(_)));
}

#[tokio::test]
async fn replay_range_request_returns_replay_payload_when_found() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_replay_range(ReplayRangeRequest {
            symbol: "VN30F1M".to_owned(),
            replay_key: "session-replay".to_owned(),
            from_timestamp: 10,
            to_timestamp: 20,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::ReplayRange(response) => {
            assert_eq!(response.symbol, "VN30F1M");
            assert_eq!(response.range.from, "1");
            assert_eq!(response.range.to, "2");
            assert_eq!(response.ticks.len(), 1);
            assert_eq!(response.ticks[0].timestamp, Some(1));
            assert_eq!(response.ticks[0].price, 1251.4);
            assert_eq!(response.ticks[0].side.as_deref(), Some("B"));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn replay_range_request_rejects_reversed_range() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let error = service
        .handle_replay_range(ReplayRangeRequest {
            symbol: "VN30F1M".to_owned(),
            replay_key: "session-replay".to_owned(),
            from_timestamp: 20,
            to_timestamp: 10,
        })
        .await
        .expect_err("reversed range should fail");

    assert!(matches!(error, GatewayServiceError::InvalidRange));
}

#[tokio::test]
async fn invalid_symbol_is_rejected_before_provider_call() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let error = service
        .handle_historical_bars(SymbolRangeRequest {
            symbol: "".to_owned(),
            from_timestamp: None,
            to_timestamp: None,
            resolution: None,
        })
        .await
        .expect_err("invalid symbol should fail");

    assert!(matches!(error, GatewayServiceError::InvalidSymbol));
}

#[tokio::test]
async fn session_profile_request_returns_session_profile_payload() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_391_200,
            profile_type: "both".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

/// Tick provider that panics if queried, proving the backfill path was taken.
#[derive(Debug, Clone)]
struct PanicIfQueriedTicks;

#[async_trait]
impl HistoricalTickProvider for PanicIfQueriedTicks {
    async fn load_ticks(&self, _query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError> {
        panic!("live tick provider queried — backfill cache fast path was not taken");
    }
}

fn backfill_tick(ts: i64, price: f64) -> Tick {
    Tick {
        symbol: Symbol::new("VN30F1M").expect("symbol"),
        timestamp: EventTimestamp::new(ts).expect("timestamp"),
        price: market_types::Price::from_f64(price).expect("price"),
        volume: market_types::Volume::new(5).expect("volume"),
        side: Some(TickSide::Buy),
    }
}

#[tokio::test]
async fn session_profile_uses_backfill_cache_when_request_range_is_covered() {
    let backfill_from = EventTimestamp::new(1_776_391_000).expect("from");
    let backfill_to = EventTimestamp::new(1_776_392_000).expect("to");
    let ticks = Arc::new(vec![
        backfill_tick(1_776_391_100, 1250.0),
        backfill_tick(1_776_391_900, 1252.0),
        // outside the request range: must be filtered out
        backfill_tick(1_776_391_999, 9999.0),
    ]);

    let service = GatewayService::new(
        TestHistoricalBars,
        PanicIfQueriedTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    )
    .with_backfill_ticks(backfill_from, backfill_to, ticks);

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_100,
            to_timestamp: 1_776_391_900,
            profile_type: "both".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");
            let source = response.source.expect("source meta");
            assert_eq!(source.source_name, "backfill_cache");
            assert_eq!(source.latest_event_timestamp, Some(1_776_391_900));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn session_profile_falls_back_to_live_query_when_request_exceeds_backfill_range() {
    let backfill_from = EventTimestamp::new(1_776_391_100).expect("from");
    let backfill_to = EventTimestamp::new(1_776_391_200).expect("to");
    let ticks = Arc::new(vec![backfill_tick(1_776_391_150, 1250.0)]);

    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    )
    .with_backfill_ticks(backfill_from, backfill_to, ticks);

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_100,
            // past `backfill_to`
            to_timestamp: 1_776_391_500,
            profile_type: "both".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");
            let source = response.source.expect("source meta");
            assert_eq!(source.source_name, "historical_tick_provider");
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn session_profile_without_backfill_cache_uses_live_query_as_before() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_391_200,
            profile_type: "both".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            let source = response.source.expect("source meta");
            assert_eq!(source.source_name, "historical_tick_provider");
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn composite_profile_request_returns_composite_profile_payload() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_391_200,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::CompositeProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn context_profile_request_returns_context_profile_payload() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_context_profile(ContextProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_391_200,
            session_count: 1,
            context_mode: "standard".to_owned(),
            offset: 0,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::ContextProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");
            assert_eq!(response.context_mode, "standard");
            assert_eq!(response.sessions.len(), 1);
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn composite_profile_request_rejects_reversed_range() {
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: true },
        None::<TestAggregatedProfile>,
    );

    let error = service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 20,
            to_timestamp: 10,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect_err("reversed range should fail");

    assert!(matches!(error, GatewayServiceError::InvalidRange));
}

fn python_replay_sample_ticks() -> Vec<Tick> {
    vec![
        Tick {
            symbol: Symbol::new("VN30F1M").expect("symbol"),
            timestamp: EventTimestamp::new(1_776_561_600).expect("timestamp"),
            price: market_types::Price::from_f64(1200.0).expect("price"),
            volume: market_types::Volume::new(10).expect("volume"),
            side: Some(TickSide::Buy),
        },
        Tick {
            symbol: Symbol::new("VN30F1M").expect("symbol"),
            timestamp: EventTimestamp::new(1_776_562_500).expect("timestamp"),
            price: market_types::Price::from_f64(1200.5).expect("price"),
            volume: market_types::Volume::new(15).expect("volume"),
            side: Some(TickSide::Buy),
        },
        Tick {
            symbol: Symbol::new("VN30F1M").expect("symbol"),
            timestamp: EventTimestamp::new(1_776_564_300).expect("timestamp"),
            price: market_types::Price::from_f64(1201.0).expect("price"),
            volume: market_types::Volume::new(12).expect("volume"),
            side: Some(TickSide::Buy),
        },
    ]
}

#[test]
fn session_replay_converges_to_historical_profile_python_fixture_oracle() {
    let symbol = Symbol::new("VN30F1M").expect("symbol");
    let ticks = python_replay_sample_ticks();
    let replay_data = ReplayRangeData {
        meta: ReplayRangeMeta {
            session_key: SessionKey::new("2026-04-17-am").expect("session key"),
            from_timestamp: ticks[0].timestamp,
            to_timestamp: ticks[2].timestamp,
            tick_count: ticks.len(),
            sequence_mode: ReplaySequenceMode::SourceAuthoritative,
        },
        ticks: ticks
            .clone()
            .into_iter()
            .enumerate()
            .map(|(index, tick)| ReplayTickData {
                tick,
                event_sequence: Some((index as u64) + 1),
            })
            .collect(),
    };

    let historical_engine_ticks = ticks
        .iter()
        .cloned()
        .map(engine_tick_from_tick)
        .collect::<Result<Vec<_>, _>>()
        .expect("historical engine ticks");
    let historical_profile = build_session_profile(
        symbol.as_str(),
        extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(ticks[0].timestamp),
        })
        .expect("from"),
        extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(ticks[2].timestamp),
        })
        .expect("to"),
        &historical_engine_ticks,
        ProfileType::Both,
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: market_types::Price::from_f64(0.5).expect("tick size"),
            tpo_bucket_minutes: 30,
        },
        ValueAreaConfig {
            value_area_pct: 0.7,
        },
    )
    .expect("historical profile");
    let historical_view = build_session_profile_view(&historical_profile);

    let replay_view = replay_range_view(&symbol, &replay_data);
    let replay_ticks = replay_view
        .ticks
        .iter()
        .map(|tick| Tick {
            symbol: symbol.clone(),
            timestamp: EventTimestamp::new(tick.timestamp.expect("timestamp")).expect("timestamp"),
            price: market_types::Price::from_f64(tick.price).expect("price"),
            volume: market_types::Volume::new(tick.volume as i64).expect("volume"),
            side: tick.side.as_deref().map(|side| {
                if side == "B" {
                    TickSide::Buy
                } else {
                    TickSide::Sell
                }
            }),
        })
        .collect::<Vec<_>>();
    let replay_engine_ticks = replay_ticks
        .into_iter()
        .map(engine_tick_from_tick)
        .collect::<Result<Vec<_>, _>>()
        .expect("replay engine ticks");
    let replay_profile = build_session_profile(
        symbol.as_str(),
        extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(ticks[0].timestamp),
        })
        .expect("from"),
        extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(ticks[2].timestamp),
        })
        .expect("to"),
        &replay_engine_ticks,
        ProfileType::Both,
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: market_types::Price::from_f64(0.5).expect("tick size"),
            tpo_bucket_minutes: 30,
        },
        ValueAreaConfig {
            value_area_pct: 0.7,
        },
    )
    .expect("replay final profile");
    let replay_final_view = build_session_profile_view(&replay_profile);

    assert_eq!(
        replay_final_view.volume_profile,
        historical_view.volume_profile
    );
    assert_eq!(replay_final_view.tpo_profile, historical_view.tpo_profile);
    assert_eq!(
        replay_final_view.auction_references,
        historical_view.auction_references
    );
    assert_eq!(
        replay_final_view.advanced_references,
        historical_view.advanced_references
    );
}

#[derive(Debug, Clone, Default)]
struct TestAggregatedProfile {
    volume_rows: Vec<AggregatedVolumeRow>,
    tpo_rows: Vec<AggregatedTpoRow>,
    session_high: Option<market_types::Price>,
    session_low: Option<market_types::Price>,
    session_open: Option<market_types::Price>,
    // aggregated rows have no time dimension, so the provider supplies the IB
    ib_high: Option<market_types::Price>,
    ib_low: Option<market_types::Price>,
}

#[async_trait]
impl AggregatedProfileProvider for TestAggregatedProfile {
    async fn load_aggregated_profile(
        &self,
        _query: AggregatedProfileQuery,
    ) -> Result<AggregatedProfileData, GatewayDataError> {
        Ok(AggregatedProfileData {
            volume_rows: self.volume_rows.clone(),
            tpo_rows: self.tpo_rows.clone(),
            session_high: self.session_high,
            session_low: self.session_low,
            session_open: self.session_open,
            ib_high: self.ib_high,
            ib_low: self.ib_low,
        })
    }

    async fn load_overview_bars(
        &self,
        _query: AggregatedProfileQuery,
    ) -> Result<Vec<market_types::OhlcBar>, GatewayDataError> {
        Ok(Vec::new())
    }
}

#[tokio::test]
async fn session_profile_pre_agg_path_matches_raw_tick_path() {
    let timestamp = EventTimestamp::new(1_776_391_200).expect("timestamp");
    let price = market_types::Price::from_f64(1251.4).expect("price");
    let volume = market_types::Volume::new(12).expect("volume");

    // raw tick path
    let raw_service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );

    let raw_payload = raw_service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: timestamp.0,
            to_timestamp: timestamp.0,
            profile_type: "volume".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("raw payload");

    // pre-aggregated path with the same single buy tick
    let agg_provider = TestAggregatedProfile {
        volume_rows: vec![AggregatedVolumeRow {
            price,
            total_volume: volume,
            buy_volume: volume,
            sell_volume: market_types::Volume::new(0).expect("zero"),
        }],
        tpo_rows: Vec::new(),
        session_high: None,
        session_low: None,
        session_open: None,
        ..Default::default()
    };
    let agg_service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let agg_payload = agg_service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: timestamp.0,
            to_timestamp: timestamp.0,
            profile_type: "volume".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("agg payload");

    match (raw_payload, agg_payload) {
        (OutboundPayload::SessionProfile(raw), OutboundPayload::SessionProfile(agg)) => {
            assert_eq!(raw.symbol, agg.symbol);
            assert_eq!(raw.profile_type, agg.profile_type);
            assert_eq!(raw.price_grouping, agg.price_grouping);

            let raw_vol = raw.volume_profile.expect("raw volume profile");
            let agg_vol = agg.volume_profile.expect("agg volume profile");
            assert_eq!(raw_vol.poc, agg_vol.poc, "POC mismatch");
            assert_eq!(raw_vol.vah, agg_vol.vah, "VAH mismatch");
            assert_eq!(raw_vol.val, agg_vol.val, "VAL mismatch");
            assert_eq!(raw_vol.rows.len(), agg_vol.rows.len(), "row count mismatch");
        }
        _ => panic!("unexpected payload types"),
    }
}

#[tokio::test]
async fn session_profile_response_exposes_profile_source_metadata() {
    let timestamp = EventTimestamp::new(1_776_391_200).expect("timestamp");
    let price = market_types::Price::from_f64(1251.4).expect("price");
    let volume = market_types::Volume::new(12).expect("volume");
    let agg_provider = TestAggregatedProfile {
        volume_rows: vec![AggregatedVolumeRow {
            price,
            total_volume: volume,
            buy_volume: volume,
            sell_volume: market_types::Volume::new(0).expect("zero"),
        }],
        tpo_rows: Vec::new(),
        session_high: None,
        session_low: None,
        session_open: None,
        ..Default::default()
    };
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: timestamp.0,
            to_timestamp: timestamp.0,
            profile_type: "volume".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            let source = response.source.expect("source metadata");
            assert_eq!(source.source_mode, "pre_agg");
            assert_eq!(source.source_name, "aggregated_profile_provider");
            // pre-aggregated rows carry no tick timestamp; freshness must not use `to_timestamp`
            assert_eq!(source.latest_event_timestamp, None);
            assert_eq!(source.freshness_age_ms, None);
            assert_eq!(source.row_count, 1);
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn raw_session_profile_exposes_budget_degraded_reason() {
    let timestamp = EventTimestamp::new(1_776_391_200).expect("timestamp");
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );

    let payload = service
        .handle_session_profile(SessionProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: timestamp.0,
            to_timestamp: timestamp.0,
            profile_type: "volume".to_owned(),
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::SessionProfile(response) => {
            let source = response.source.expect("source metadata");
            assert_eq!(source.source_mode, "raw");
            let degraded_reason = source.degraded_reason.expect("degraded reason");
            assert!(degraded_reason.contains("raw_profile_fallback"));
            assert!(degraded_reason.contains("stale_freshness"));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn composite_profile_marks_missing_aggregate_as_degraded() {
    let timestamp = EventTimestamp::new(1_776_391_200).expect("timestamp");
    let agg_provider = TestAggregatedProfile {
        volume_rows: Vec::new(),
        tpo_rows: Vec::new(),
        session_high: None,
        session_low: None,
        session_open: None,
        ..Default::default()
    };
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let payload = service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: timestamp.0,
            to_timestamp: timestamp.0,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::CompositeProfile(response) => {
            let source = response.source.expect("source metadata");
            assert_eq!(source.source_mode, "pre_agg");
            assert_eq!(source.row_count, 0);
            let degraded_reason = source.degraded_reason.expect("degraded reason");
            assert!(degraded_reason.contains("missing_aggregate"));
            assert!(degraded_reason.contains("stale_freshness"));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[test]
fn profile_budget_state_combines_row_budget_and_stale_freshness() {
    let stale_timestamp = EventTimestamp::new(1).expect("timestamp");
    let source = profile_source_meta(
        "pre_agg",
        "aggregated_profile_provider",
        "unknown",
        Some(stale_timestamp),
        PROFILE_ROW_BUDGET + 1,
        None,
    );

    let reason = source.degraded_reason.expect("degraded reason");
    assert!(reason.contains("profile_row_budget_exceeded"));
    assert!(reason.contains("stale_freshness"));
}

// --- helpers ---

fn make_tick(timestamp: i64, price: f64, volume: i64, side: TickSide) -> Tick {
    Tick {
        symbol: Symbol::new("VN30F1M").expect("symbol"),
        timestamp: EventTimestamp::new(timestamp).expect("timestamp"),
        price: market_types::Price::from_f64(price).expect("price"),
        volume: market_types::Volume::new(volume).expect("volume"),
        side: Some(side),
    }
}

fn make_agg_vol_row(price: f64, total: i64, buy: i64, sell: i64) -> AggregatedVolumeRow {
    AggregatedVolumeRow {
        price: market_types::Price::from_f64(price).expect("price"),
        total_volume: market_types::Volume::new(total).expect("volume"),
        buy_volume: market_types::Volume::new(buy).expect("volume"),
        sell_volume: market_types::Volume::new(sell).expect("volume"),
    }
}

fn make_agg_tpo_row(price: f64, tpo_count: u32) -> AggregatedTpoRow {
    AggregatedTpoRow {
        price_level: market_types::Price::from_f64(price).expect("price"),
        tpo_count,
    }
}

#[derive(Debug, Clone)]
struct TestRichHistoricalTicks;

#[async_trait]
impl HistoricalTickProvider for TestRichHistoricalTicks {
    async fn load_ticks(&self, _query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError> {
        Ok(vec![
            make_tick(1_776_391_200, 1251.2, 15, TickSide::Buy),
            make_tick(1_776_391_300, 1251.4, 12, TickSide::Buy),
            make_tick(1_776_391_400, 1251.6, 8, TickSide::Sell),
        ])
    }
}

#[tokio::test]
async fn composite_profile_pre_agg_matches_raw_tick_path() {
    let from_timestamp = 1_776_391_200_i64;
    let to_timestamp = 1_776_391_400_i64;

    // raw tick path
    let raw_service = GatewayService::new(
        TestHistoricalBars,
        TestRichHistoricalTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );

    let raw_payload = raw_service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp,
            to_timestamp,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("raw payload");

    // pre-aggregated path with the same data
    let agg_provider = TestAggregatedProfile {
        volume_rows: vec![
            make_agg_vol_row(1251.2, 15, 15, 0),
            make_agg_vol_row(1251.4, 12, 12, 0),
            make_agg_vol_row(1251.6, 8, 0, 8),
        ],
        tpo_rows: vec![
            make_agg_tpo_row(1251.2, 1),
            make_agg_tpo_row(1251.4, 1),
            make_agg_tpo_row(1251.6, 1),
        ],
        session_high: Some(market_types::Price::from_f64(1251.6).expect("price")),
        session_low: Some(market_types::Price::from_f64(1251.2).expect("price")),
        session_open: Some(market_types::Price::from_f64(1251.2).expect("price")),
        ..Default::default()
    };
    let agg_service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let agg_payload = agg_service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp,
            to_timestamp,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("agg payload");

    match (raw_payload, agg_payload) {
        (OutboundPayload::CompositeProfile(raw), OutboundPayload::CompositeProfile(agg)) => {
            assert_eq!(raw.symbol, agg.symbol, "symbol mismatch");

            assert_eq!(
                raw.volume_profile.poc, agg.volume_profile.poc,
                "volume POC mismatch"
            );
            assert_eq!(
                raw.volume_profile.vah, agg.volume_profile.vah,
                "volume VAH mismatch"
            );
            assert_eq!(
                raw.volume_profile.val, agg.volume_profile.val,
                "volume VAL mismatch"
            );
            assert_eq!(
                raw.volume_profile.rows.len(),
                agg.volume_profile.rows.len(),
                "volume row count mismatch"
            );

            assert_eq!(
                raw.market_profile.poc, agg.market_profile.poc,
                "market POC mismatch"
            );
            assert_eq!(
                raw.market_profile.vah, agg.market_profile.vah,
                "market VAH mismatch"
            );
            assert_eq!(
                raw.market_profile.val, agg.market_profile.val,
                "market VAL mismatch"
            );
            assert_eq!(
                raw.market_profile.rows.len(),
                agg.market_profile.rows.len(),
                "market row count mismatch"
            );

            let raw_source = raw.source.expect("raw source");
            assert_eq!(raw_source.source_mode, "raw");

            let agg_source = agg.source.expect("agg source");
            assert_eq!(agg_source.source_mode, "pre_agg");
        }
        _ => panic!("unexpected payload types"),
    }
}

#[tokio::test]
async fn composite_profile_aggregate_tpo_validation() {
    let agg_provider = TestAggregatedProfile {
        volume_rows: vec![
            make_agg_vol_row(1251.0, 10, 10, 0),
            make_agg_vol_row(1251.2, 12, 12, 0),
            make_agg_vol_row(1251.4, 36, 36, 0),
            make_agg_vol_row(1251.6, 8, 0, 8),
        ],
        tpo_rows: vec![
            make_agg_tpo_row(1251.0, 1),
            make_agg_tpo_row(1251.2, 1),
            make_agg_tpo_row(1251.4, 3),
            make_agg_tpo_row(1251.6, 1),
        ],
        session_high: Some(market_types::Price::from_f64(1251.6).expect("price")),
        session_low: Some(market_types::Price::from_f64(1251.0).expect("price")),
        session_open: Some(market_types::Price::from_f64(1251.2).expect("price")),
        ..Default::default()
    };

    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let payload = service
        .handle_composite_profile(CompositeProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_391_400,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("payload");

    match payload {
        OutboundPayload::CompositeProfile(response) => {
            assert_eq!(response.symbol, "VN30F1M");

            assert_eq!(response.market_profile.rows.len(), 4, "expected 4 TPO rows");

            // the highest tpo_count (3) is at 1251.4
            let poc = response.market_profile.poc.expect("market POC");
            assert!(
                (poc - 1251.4).abs() < f64::EPSILON,
                "market POC should be at 1251.4, got {poc}"
            );

            let vah = response.market_profile.vah.expect("market VAH");
            let val = response.market_profile.val.expect("market VAL");
            assert!(
                val <= poc && poc <= vah,
                "market VAL={val} <= POC={poc} <= VAH={vah}"
            );

            assert_eq!(
                response.volume_profile.rows.len(),
                4,
                "expected 4 volume rows"
            );
            let vol_poc = response.volume_profile.poc.expect("volume POC");
            assert!(
                (vol_poc - 1251.4).abs() < f64::EPSILON,
                "volume POC should be at 1251.4, got {vol_poc}"
            );

            let auctions = response.auction_references.expect("auction references");
            assert!((auctions.session_high.expect("high") - 1251.6).abs() < f64::EPSILON);
            assert!((auctions.session_low.expect("low") - 1251.0).abs() < f64::EPSILON);
            assert!((auctions.session_open.expect("open") - 1251.2).abs() < f64::EPSILON);

            let source = response.source.expect("source metadata");
            assert_eq!(source.source_mode, "pre_agg");
            assert_eq!(source.row_count, 8); // 4 volume + 4 TPO rows
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn context_profile_pre_agg_matches_raw_tick_path() {
    let from_timestamp = 1_776_391_200_i64;
    let to_timestamp = 1_776_391_400_i64;

    // raw tick path
    let raw_service = GatewayService::new(
        TestHistoricalBars,
        TestRichHistoricalTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );

    let raw_payload = raw_service
        .handle_context_profile(ContextProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp,
            to_timestamp,
            session_count: 1,
            context_mode: "standard".to_owned(),
            offset: 0,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("raw payload");

    // pre-aggregated path with the same data
    let agg_provider = TestAggregatedProfile {
        volume_rows: vec![
            make_agg_vol_row(1251.2, 15, 15, 0),
            make_agg_vol_row(1251.4, 12, 12, 0),
            make_agg_vol_row(1251.6, 8, 0, 8),
        ],
        tpo_rows: vec![
            make_agg_tpo_row(1251.2, 1),
            make_agg_tpo_row(1251.4, 1),
            make_agg_tpo_row(1251.6, 1),
        ],
        session_high: Some(market_types::Price::from_f64(1251.6).expect("price")),
        session_low: Some(market_types::Price::from_f64(1251.2).expect("price")),
        session_open: Some(market_types::Price::from_f64(1251.2).expect("price")),
        ..Default::default()
    };
    let agg_service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(agg_provider),
    );

    let agg_payload = agg_service
        .handle_context_profile(ContextProfileRequest {
            symbol: "VN30F1M".to_owned(),
            from_timestamp,
            to_timestamp,
            session_count: 1,
            context_mode: "standard".to_owned(),
            offset: 0,
            price_grouping: 1,
            tpo_bucket_minutes: 30,
            value_area_pct: 0.7,
        })
        .await
        .expect("agg payload");

    match (raw_payload, agg_payload) {
        (OutboundPayload::ContextProfile(raw), OutboundPayload::ContextProfile(agg)) => {
            assert_eq!(raw.symbol, agg.symbol, "symbol mismatch");
            assert_eq!(raw.context_mode, agg.context_mode, "context mode mismatch");
            assert_eq!(
                raw.sessions.len(),
                agg.sessions.len(),
                "session count mismatch"
            );
            assert_eq!(raw.sessions.len(), 1, "expected 1 session");

            let raw_session = &raw.sessions[0];
            let agg_session = &agg.sessions[0];

            assert_eq!(
                raw_session.volume_profile.poc, agg_session.volume_profile.poc,
                "context volume POC mismatch"
            );
            assert_eq!(
                raw_session.volume_profile.vah, agg_session.volume_profile.vah,
                "context volume VAH mismatch"
            );
            assert_eq!(
                raw_session.volume_profile.val, agg_session.volume_profile.val,
                "context volume VAL mismatch"
            );
            assert_eq!(
                raw_session.volume_profile.rows.len(),
                agg_session.volume_profile.rows.len(),
                "context volume row count mismatch"
            );

            assert_eq!(
                raw_session.market_profile.poc, agg_session.market_profile.poc,
                "context market POC mismatch"
            );
            assert_eq!(
                raw_session.market_profile.vah, agg_session.market_profile.vah,
                "context market VAH mismatch"
            );
            assert_eq!(
                raw_session.market_profile.val, agg_session.market_profile.val,
                "context market VAL mismatch"
            );
            assert_eq!(
                raw_session.market_profile.rows.len(),
                agg_session.market_profile.rows.len(),
                "context market row count mismatch"
            );

            let raw_source = raw.source.expect("raw source");
            assert_eq!(raw_source.source_mode, "raw");

            let agg_source = agg.source.expect("agg source");
            assert_eq!(agg_source.source_mode, "pre_agg");
        }
        _ => panic!("unexpected payload types"),
    }
}

// --- SIG-001 session bias (80% Rule) ---

/// A Friday, 09:00 ICT.
const BIAS_PRIOR_DAY_START: i64 = 1_776_391_200;
/// The following Monday, 09:00 ICT: lookback days 1 and 2 are the weekend, day 3 the Friday.
const BIAS_DAY_START: i64 = BIAS_PRIOR_DAY_START + 3 * 86_400;

fn bias_tick(ts: i64, price: f64, volume: i64) -> Tick {
    Tick {
        symbol: Symbol::new("VN30F1M").expect("symbol"),
        timestamp: EventTimestamp::new(ts).expect("timestamp"),
        price: market_types::Price::from_f64(price).expect("price"),
        volume: market_types::Volume::new(volume).expect("volume"),
        side: Some(TickSide::Buy),
    }
}

/// Serves prior-day ticks clustered at 1300 and scripted early blocks for `BIAS_DAY_START`.
#[derive(Debug, Clone)]
struct BiasScriptTicks {
    block_ticks: Vec<Tick>,
}

#[async_trait]
impl HistoricalTickProvider for BiasScriptTicks {
    async fn load_ticks(&self, query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError> {
        let from = query.from_timestamp.map(|t| t.0).unwrap_or(0);
        if from == BIAS_DAY_START {
            return Ok(self.block_ticks.clone());
        }
        if from == BIAS_PRIOR_DAY_START {
            // POC 1300 with light tails: the value area spans several levels inside [1296, 1304]
            return Ok(vec![
                bias_tick(BIAS_PRIOR_DAY_START + 60, 1296.0, 5),
                bias_tick(BIAS_PRIOR_DAY_START + 120, 1304.0, 5),
                bias_tick(BIAS_PRIOR_DAY_START + 300, 1299.0, 30),
                bias_tick(BIAS_PRIOR_DAY_START + 360, 1300.0, 40),
                bias_tick(BIAS_PRIOR_DAY_START + 420, 1301.0, 30),
            ]);
        }
        Ok(Vec::new())
    }
}

fn bias_service(
    block_ticks: Vec<Tick>,
) -> GatewayService<TestHistoricalBars, BiasScriptTicks, TestReplayRanges, TestAggregatedProfile> {
    GatewayService::new(
        TestHistoricalBars,
        BiasScriptTicks { block_ticks },
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    )
}

#[tokio::test]
async fn session_bias_open_above_va_blocks_inside_gives_bearish() {
    let service = bias_service(vec![
        bias_tick(BIAS_DAY_START + 10, 1320.0, 1), // session open above VAH
        bias_tick(BIAS_DAY_START + 300, 1300.0, 10), // block 1 close inside
        bias_tick(BIAS_DAY_START + 1000, 1300.0, 10), // block 2 close inside
    ]);
    let bias = service
        .compute_session_bias("VN30F1M", BIAS_DAY_START)
        .await
        .expect("bias computation")
        .expect("bias present");
    assert!(!bias.bullish);
    assert_eq!(bias.target, bias.prior_val);
    assert!(bias.prior_vah > bias.prior_val);
    assert!(bias.prior_vah < 1320.0, "open must be above prior VAH");
    assert_eq!(bias.confidence, 0.8);
}

#[tokio::test]
async fn session_bias_open_inside_va_gives_none() {
    let service = bias_service(vec![
        bias_tick(BIAS_DAY_START + 10, 1300.0, 1), // open at prior POC
        bias_tick(BIAS_DAY_START + 1000, 1300.0, 10),
    ]);
    let bias = service
        .compute_session_bias("VN30F1M", BIAS_DAY_START)
        .await
        .expect("bias computation");
    assert!(bias.is_none());
}

#[tokio::test]
async fn session_bias_missing_block_gives_none() {
    // only the first block has ticks
    let service = bias_service(vec![bias_tick(BIAS_DAY_START + 10, 1320.0, 1)]);
    let bias = service
        .compute_session_bias("VN30F1M", BIAS_DAY_START)
        .await
        .expect("bias computation");
    assert!(bias.is_none());
}

#[tokio::test]
async fn session_bias_block_escape_gives_none() {
    // both blocks close outside the prior value area
    let service = bias_service(vec![
        bias_tick(BIAS_DAY_START + 10, 1320.0, 1),
        bias_tick(BIAS_DAY_START + 300, 1319.0, 10),
        bias_tick(BIAS_DAY_START + 1000, 1318.0, 10),
    ]);
    let bias = service
        .compute_session_bias("VN30F1M", BIAS_DAY_START)
        .await
        .expect("bias computation");
    assert!(bias.is_none());
}

#[derive(Debug, Clone)]
struct UnavailableTicks;

#[async_trait]
impl HistoricalTickProvider for UnavailableTicks {
    async fn load_ticks(&self, _query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError> {
        Err(GatewayDataError::Unavailable)
    }
}

#[tokio::test]
async fn session_bias_provider_down_propagates_unavailable() {
    let service = GatewayService::new(
        TestHistoricalBars,
        UnavailableTicks,
        TestReplayRanges { has_value: false },
        None::<TestAggregatedProfile>,
    );
    let error = service
        .compute_session_bias("VN30F1M", BIAS_DAY_START)
        .await
        .expect_err("data layer down must be retryable");
    assert!(matches!(
        error,
        GatewayServiceError::Data(GatewayDataError::Unavailable)
    ));
}

// --- ClickHouse circuit breaker ---

/// Provider whose call `i` succeeds if `outcomes[i]`; past the end the last entry repeats.
#[derive(Debug, Clone)]
struct ScriptedAggregatedProfile {
    outcomes: Vec<bool>,
    calls: Arc<AtomicU32>,
}

impl ScriptedAggregatedProfile {
    fn new(outcomes: Vec<bool>) -> Self {
        Self {
            outcomes,
            calls: Arc::new(AtomicU32::new(0)),
        }
    }

    fn call_count(&self) -> u32 {
        self.calls.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl AggregatedProfileProvider for ScriptedAggregatedProfile {
    async fn load_aggregated_profile(
        &self,
        _query: AggregatedProfileQuery,
    ) -> Result<AggregatedProfileData, GatewayDataError> {
        let index = self.calls.fetch_add(1, Ordering::SeqCst) as usize;
        let succeed = self
            .outcomes
            .get(index)
            .or_else(|| self.outcomes.last())
            .copied()
            .unwrap_or(true);
        if !succeed {
            return Err(GatewayDataError::Unavailable);
        }
        Ok(AggregatedProfileData {
            volume_rows: vec![make_agg_vol_row(1251.4, 12, 12, 0)],
            tpo_rows: Vec::new(),
            session_high: None,
            session_low: None,
            session_open: None,
            ib_high: None,
            ib_low: None,
        })
    }

    async fn load_overview_bars(
        &self,
        _query: AggregatedProfileQuery,
    ) -> Result<Vec<market_types::OhlcBar>, GatewayDataError> {
        Ok(Vec::new())
    }
}

fn session_request(from_timestamp: i64, to_timestamp: i64) -> SessionProfileRequest {
    SessionProfileRequest {
        symbol: "VN30F1M".to_owned(),
        from_timestamp,
        to_timestamp,
        profile_type: "volume".to_owned(),
        price_grouping: 1,
        tpo_bucket_minutes: 30,
        value_area_pct: 0.7,
    }
}

#[tokio::test]
async fn breaker_opens_after_failure_window_and_returns_typed_error_on_empty_cache() {
    let provider = ScriptedAggregatedProfile::new(vec![false]);
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(provider.clone()),
    );

    for _ in 0..5 {
        let error = service
            .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
            .await
            .expect_err("provider failure should propagate");
        assert!(matches!(
            error,
            GatewayServiceError::Data(GatewayDataError::Unavailable)
        ));
    }
    assert_eq!(provider.call_count(), 5);
    assert_eq!(service.ch_breaker.state_str(), "open");

    // open breaker, empty cache: typed error without a provider call
    let error = service
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect_err("open breaker with empty cache should return typed error");
    assert!(matches!(
        error,
        GatewayServiceError::Data(GatewayDataError::Unavailable)
    ));
    assert_eq!(provider.call_count(), 5);
}

#[tokio::test]
async fn breaker_open_serves_cached_payload_marked_clickhouse_unavailable_cached() {
    // the first call succeeds and fills the cache; later calls fail
    let provider = ScriptedAggregatedProfile::new(vec![true, false]);
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(provider.clone()),
    );

    let payload = service
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect("initial payload");
    assert!(matches!(payload, OutboundPayload::SessionProfile(_)));

    // a different cache key, so each request reaches the failing provider
    for _ in 0..5 {
        service
            .handle_session_profile(session_request(1_776_391_200, 1_776_391_400))
            .await
            .expect_err("provider failure should propagate");
    }
    assert_eq!(service.ch_breaker.state_str(), "open");

    let payload = service
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect("cached payload while breaker open");
    match payload {
        OutboundPayload::SessionProfile(response) => {
            let source = response.source.expect("source metadata");
            assert_eq!(source.source_mode, "pre_agg");
            let degraded_reason = source.degraded_reason.expect("degraded reason");
            assert!(degraded_reason.contains(DEGRADED_CLICKHOUSE_UNAVAILABLE_CACHED));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
    assert_eq!(provider.call_count(), 6);
}

#[tokio::test]
async fn shared_resilience_serves_cache_across_service_instances() {
    // one service per connection: a new connection must serve another connection's cache
    let shared = ProfileResilience::default();
    let warm_conn = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(ScriptedAggregatedProfile::new(vec![true])),
    )
    .with_resilience(&shared);
    warm_conn
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect("warm cache on first connection");

    let fresh_conn = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(ScriptedAggregatedProfile::new(vec![false])),
    )
    .with_resilience(&shared);
    for _ in 0..5 {
        fresh_conn
            .handle_session_profile(session_request(1_776_391_200, 1_776_391_400))
            .await
            .expect_err("provider failure should propagate");
    }
    assert_eq!(fresh_conn.ch_breaker.state_str(), "open");
    assert_eq!(warm_conn.ch_breaker.state_str(), "open");

    let payload = fresh_conn
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect("fresh connection must serve the other connection's cached payload");
    match payload {
        OutboundPayload::SessionProfile(response) => {
            let source = response.source.expect("source metadata");
            let degraded_reason = source.degraded_reason.expect("degraded reason");
            assert!(degraded_reason.contains(DEGRADED_CLICKHOUSE_UNAVAILABLE_CACHED));
        }
        other => panic!("unexpected payload: {other:?}"),
    }
}

#[tokio::test]
async fn breaker_half_open_trial_success_closes_breaker() {
    let provider = ScriptedAggregatedProfile::new(vec![false, false, false, false, false, true]);
    let service = GatewayService::new(
        TestHistoricalBars,
        TestHistoricalTicks,
        TestReplayRanges { has_value: false },
        Some(provider.clone()),
    )
    .with_breaker(ClickHouseBreaker::new(
        5,
        Duration::from_secs(10),
        Duration::ZERO,
    ));

    for _ in 0..5 {
        service
            .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
            .await
            .expect_err("provider failure should propagate");
    }
    assert_eq!(service.ch_breaker.state_str(), "open");

    // zero cooldown: the next request is the half-open trial
    let payload = service
        .handle_session_profile(session_request(1_776_391_200, 1_776_391_200))
        .await
        .expect("half-open trial should succeed");
    assert!(matches!(payload, OutboundPayload::SessionProfile(_)));
    assert_eq!(service.ch_breaker.state_str(), "closed");
    assert_eq!(provider.call_count(), 6);
}

#[test]
fn breaker_failures_outside_window_do_not_open() {
    // zero-length window: each failure restarts it, so the threshold is never reached
    let breaker = ClickHouseBreaker::new(2, Duration::ZERO, Duration::from_secs(30));
    for _ in 0..5 {
        breaker.record_failure();
    }
    assert_eq!(breaker.state_str(), "closed");
}

#[test]
fn breaker_half_open_failure_reopens_and_success_closes() {
    let breaker = ClickHouseBreaker::new(1, Duration::from_secs(10), Duration::ZERO);
    breaker.record_failure();
    assert_eq!(breaker.state_str(), "open");

    assert_eq!(breaker.check(), BreakerState::HalfOpen);
    breaker.record_failure();
    assert_eq!(breaker.state_str(), "open");

    assert_eq!(breaker.check(), BreakerState::HalfOpen);
    breaker.record_success();
    assert_eq!(breaker.state_str(), "closed");
}
