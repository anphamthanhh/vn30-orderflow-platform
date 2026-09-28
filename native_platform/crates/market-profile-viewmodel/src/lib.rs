//! Serializable session, context, composite and replay profile views, with TPO letter encoding.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use market_profile_engine::{
    AdvancedReferences, AuctionReferences, CompositeProfile, ContextMode, ContextProfile,
    EitherProfile, OverviewBar, ProfileSummary, ProfileType, ReplayRange, ReplayTick,
    SessionProfile, TpoProfileRow, VolumeProfileRow,
};
use market_types::{Price, TickSide};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileSourceMetaView {
    pub source_mode: String,
    pub source_name: String,
    pub sequence_mode: String,
    pub generated_at_unix_ms: i64,
    pub latest_event_timestamp: Option<i64>,
    pub freshness_age_ms: Option<i64>,
    pub row_count: usize,
    pub degraded_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VolumeProfileRowView {
    pub price: f64,
    pub volume: f64,
    pub buy_volume: f64,
    pub sell_volume: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TpoProfileRowView {
    pub price: f64,
    pub tpo_count: i64,
    pub bucket_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProfileSummaryView<Row> {
    pub rows: Vec<Row>,
    pub poc: Option<f64>,
    pub vah: Option<f64>,
    pub val: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuctionReferencesView {
    pub session_open: Option<f64>,
    pub session_high: Option<f64>,
    pub session_low: Option<f64>,
    pub ib_high: Option<f64>,
    pub ib_low: Option<f64>,
    pub tpo_poc: Option<f64>,
    pub volume_poc: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AdvancedReferencesView {
    pub poor_high: Option<f64>,
    pub poor_low: Option<f64>,
    pub single_print_prices: Vec<f64>,
    pub excess_high: Vec<f64>,
    pub excess_low: Vec<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionProfileView {
    pub symbol: String,
    pub from: String,
    pub to: String,
    pub profile_type: String,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub volume_profile: Option<ProfileSummaryView<VolumeProfileRowView>>,
    pub tpo_profile: Option<ProfileSummaryView<TpoProfileRowView>>,
    pub auction_references: Option<AuctionReferencesView>,
    pub advanced_references: Option<AdvancedReferencesView>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<ProfileSourceMetaView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContextSessionView {
    pub session_key: String,
    pub from: String,
    pub to: String,
    pub shared_price_levels: Vec<f64>,
    pub market_profile: ProfileSummaryView<TpoProfileRowView>,
    pub volume_profile: ProfileSummaryView<VolumeProfileRowView>,
    pub auction_references: Option<AuctionReferencesView>,
    pub advanced_references: Option<AdvancedReferencesView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContextProfileView {
    pub symbol: String,
    pub context_mode: String,
    pub window_size: Option<usize>,
    pub offset: usize,
    pub shared_price_levels: Option<Vec<f64>>,
    pub sessions: Vec<ContextSessionView>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<ProfileSourceMetaView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OverviewBarView {
    pub timestamp: i64,
    pub datetime: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub interval: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompositeRangeView {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompositeProfileView {
    pub symbol: String,
    pub range: CompositeRangeView,
    pub applied_range: CompositeRangeView,
    pub overview_interval: String,
    pub overview_bars: Vec<OverviewBarView>,
    pub market_profile: ProfileSummaryView<TpoProfileRowView>,
    pub volume_profile: ProfileSummaryView<VolumeProfileRowView>,
    pub auction_references: Option<AuctionReferencesView>,
    pub advanced_references: Option<AdvancedReferencesView>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<ProfileSourceMetaView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReplayTickView {
    pub datetime: String,
    pub price: f64,
    pub volume: f64,
    pub timestamp: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_sequence: Option<u64>,
    pub side: Option<String>,
    pub last_trade_side: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReplayRangeView {
    pub symbol: String,
    pub range: CompositeRangeView,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub value_area_pct: f64,
    pub sequence_mode: String,
    pub ticks: Vec<ReplayTickView>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub enum FinalProfileView {
    Session(SessionProfileView),
    Composite(CompositeProfileView),
}

fn price_view(price: Price) -> f64 {
    price.as_f64()
}

fn optional_price_view(price: Option<Price>) -> Option<f64> {
    price.map(Price::as_f64)
}

fn side_view(side: TickSide) -> &'static str {
    match side {
        TickSide::Buy => "B",
        TickSide::Sell => "S",
    }
}

fn profile_type_view(profile_type: ProfileType) -> &'static str {
    match profile_type {
        ProfileType::Volume => "volume",
        ProfileType::Tpo => "tpo",
        ProfileType::Both => "both",
    }
}

fn context_mode_view(context_mode: ContextMode) -> &'static str {
    match context_mode {
        ContextMode::Standard => "standard",
        ContextMode::Deep => "deep",
    }
}

fn auction_references_view(references: &AuctionReferences) -> AuctionReferencesView {
    AuctionReferencesView {
        session_open: Some(price_view(references.session_open)),
        session_high: Some(price_view(references.session_high)),
        session_low: Some(price_view(references.session_low)),
        ib_high: optional_price_view(references.ib_high),
        ib_low: optional_price_view(references.ib_low),
        tpo_poc: optional_price_view(references.tpo_poc),
        volume_poc: optional_price_view(references.volume_poc),
    }
}

fn advanced_references_view(references: &AdvancedReferences) -> AdvancedReferencesView {
    AdvancedReferencesView {
        poor_high: optional_price_view(references.poor_high),
        poor_low: optional_price_view(references.poor_low),
        single_print_prices: references
            .single_print_prices
            .iter()
            .copied()
            .map(price_view)
            .collect(),
        excess_high: references
            .excess_high
            .iter()
            .copied()
            .map(price_view)
            .collect(),
        excess_low: references
            .excess_low
            .iter()
            .copied()
            .map(price_view)
            .collect(),
    }
}

fn volume_summary_view(
    summary: &ProfileSummary<VolumeProfileRow>,
) -> ProfileSummaryView<VolumeProfileRowView> {
    ProfileSummaryView {
        rows: summary
            .rows
            .iter()
            .map(|row| VolumeProfileRowView {
                price: price_view(row.price),
                volume: row.volume.0 as f64,
                buy_volume: row.buy_volume.0 as f64,
                sell_volume: row.sell_volume.0 as f64,
            })
            .collect(),
        poc: optional_price_view(summary.poc),
        vah: optional_price_view(summary.vah),
        val: optional_price_view(summary.val),
    }
}

fn encode_tpo_letters(bucket_ids: &[String]) -> Vec<String> {
    if bucket_ids.is_empty() {
        return Vec::new();
    }
    let mut sorted: Vec<String> = bucket_ids.to_vec();
    sorted.sort();
    sorted.dedup();
    let letter_map: std::collections::HashMap<&str, String> = sorted
        .iter()
        .enumerate()
        .map(|(i, id)| {
            let letter = if i < 26 {
                let code = u32::from(b'A') + i as u32;
                std::char::from_u32(code).unwrap().to_string()
            } else {
                let outer = (i / 26).saturating_sub(1);
                let inner = i % 26;
                let code = u32::from(b'A') + inner as u32;
                let c = std::char::from_u32(code).unwrap();
                format!("{}{}", c, outer + 1)
            };
            (id.as_str(), letter)
        })
        .collect();
    bucket_ids
        .iter()
        .map(|id| letter_map.get(id.as_str()).cloned().unwrap_or_default())
        .collect()
}

fn tpo_summary_view(
    summary: &ProfileSummary<TpoProfileRow>,
) -> ProfileSummaryView<TpoProfileRowView> {
    ProfileSummaryView {
        rows: summary
            .rows
            .iter()
            .map(|row| TpoProfileRowView {
                price: price_view(row.price),
                tpo_count: i64::try_from(row.tpo_count).unwrap_or(i64::MAX),
                bucket_ids: encode_tpo_letters(&row.bucket_ids),
            })
            .collect(),
        poc: optional_price_view(summary.poc),
        vah: optional_price_view(summary.vah),
        val: optional_price_view(summary.val),
    }
}

fn overview_bar_view(bar: &OverviewBar, interval: &str) -> OverviewBarView {
    OverviewBarView {
        timestamp: bar.timestamp.0,
        datetime: bar.datetime_iso.clone(),
        open: price_view(bar.open),
        high: price_view(bar.high),
        low: price_view(bar.low),
        close: price_view(bar.close),
        volume: bar.volume.0 as f64,
        interval: interval.to_owned(),
    }
}

fn replay_tick_view(tick: &ReplayTick) -> ReplayTickView {
    ReplayTickView {
        datetime: tick.datetime_iso.clone(),
        price: price_view(tick.price),
        volume: tick.volume.0 as f64,
        timestamp: tick.timestamp.map(|timestamp| timestamp.0),
        event_sequence: tick.event_sequence,
        side: tick.side.map(|side| side_view(side).to_owned()),
        last_trade_side: tick.last_trade_side.map(|side| side_view(side).to_owned()),
    }
}

pub fn build_session_profile_view(profile: &SessionProfile) -> SessionProfileView {
    SessionProfileView {
        symbol: profile.symbol.clone(),
        from: profile.from_iso.clone(),
        to: profile.to_iso.clone(),
        profile_type: profile_type_view(profile.profile_type).to_owned(),
        price_grouping: profile.price_grouping,
        tpo_bucket_minutes: profile.tpo_bucket_minutes,
        volume_profile: profile.volume_profile.as_ref().map(volume_summary_view),
        tpo_profile: profile.tpo_profile.as_ref().map(tpo_summary_view),
        auction_references: profile
            .auction_references
            .as_ref()
            .map(auction_references_view),
        advanced_references: profile
            .advanced_references
            .as_ref()
            .map(advanced_references_view),
        source: None,
    }
}

pub fn build_context_profile_view(profile: &ContextProfile) -> ContextProfileView {
    ContextProfileView {
        symbol: profile.symbol.clone(),
        context_mode: context_mode_view(profile.context_mode).to_owned(),
        window_size: Some(profile.window_size),
        offset: profile.offset,
        shared_price_levels: Some(
            profile
                .shared_price_levels
                .iter()
                .copied()
                .map(price_view)
                .collect(),
        ),
        sessions: profile
            .sessions
            .iter()
            .map(|session| ContextSessionView {
                session_key: session.session_key.clone(),
                from: session.from_iso.clone(),
                to: session.to_iso.clone(),
                shared_price_levels: session
                    .shared_price_levels
                    .iter()
                    .copied()
                    .map(price_view)
                    .collect(),
                market_profile: tpo_summary_view(&session.market_profile),
                volume_profile: volume_summary_view(&session.volume_profile),
                auction_references: session
                    .auction_references
                    .as_ref()
                    .map(auction_references_view),
                advanced_references: session
                    .advanced_references
                    .as_ref()
                    .map(advanced_references_view),
            })
            .collect(),
        source: None,
    }
}

pub fn build_composite_profile_view(profile: &CompositeProfile) -> CompositeProfileView {
    CompositeProfileView {
        symbol: profile.symbol.clone(),
        range: CompositeRangeView {
            from: profile.from_iso.clone(),
            to: profile.to_iso.clone(),
        },
        applied_range: CompositeRangeView {
            from: profile.from_iso.clone(),
            to: profile.to_iso.clone(),
        },
        overview_interval: profile.overview_interval.clone(),
        overview_bars: profile
            .overview_bars
            .iter()
            .map(|bar| overview_bar_view(bar, &profile.overview_interval))
            .collect(),
        market_profile: tpo_summary_view(&profile.market_profile),
        volume_profile: volume_summary_view(&profile.volume_profile),
        auction_references: profile
            .auction_references
            .as_ref()
            .map(auction_references_view),
        advanced_references: profile
            .advanced_references
            .as_ref()
            .map(advanced_references_view),
        source: None,
    }
}

pub fn build_replay_range_view(range: &ReplayRange) -> ReplayRangeView {
    ReplayRangeView {
        symbol: range.symbol.clone(),
        range: CompositeRangeView {
            from: range.from_iso.clone(),
            to: range.to_iso.clone(),
        },
        price_grouping: range.price_grouping,
        tpo_bucket_minutes: range.tpo_bucket_minutes,
        value_area_pct: f64::from(range.value_area_pct_bps) / 10_000.0,
        sequence_mode: String::from("source_authoritative"),
        ticks: range.ticks.iter().map(replay_tick_view).collect(),
    }
}

pub fn build_final_profile_view(profile: &EitherProfile) -> FinalProfileView {
    match profile {
        EitherProfile::Session(session) => {
            FinalProfileView::Session(build_session_profile_view(session))
        }
        EitherProfile::Composite(composite) => {
            FinalProfileView::Composite(build_composite_profile_view(composite))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::DateTime;
    use market_profile_engine::{
        build_composite_profile, build_context_profile, build_replay_range, build_session_profile,
        EngineTick, ProfileBuildConfig, ValueAreaConfig,
    };
    use market_types::{EventTimestamp, Price, RawTickTimeFields, Tick, Volume};

    fn tick(
        timestamp: i64,
        datetime_iso: &str,
        price: f64,
        volume: i64,
        side: TickSide,
    ) -> EngineTick {
        EngineTick::from_tick_and_time_fields(
            Tick {
                symbol: market_types::Symbol::new("VN30F1M").expect("symbol"),
                timestamp: EventTimestamp::new(timestamp).expect("timestamp"),
                price: Price::from_f64(price).expect("price"),
                volume: Volume::new(volume).expect("volume"),
                side: Some(side),
            },
            RawTickTimeFields {
                datetime_iso: Some(datetime_iso.to_owned()),
                timestamp: None,
            },
            Some(side),
        )
        .expect("engine tick")
    }

    fn config() -> ProfileBuildConfig {
        ProfileBuildConfig {
            price_grouping: 1,
            tick_size: Price::from_f64(0.1).expect("tick size"),
            tpo_bucket_minutes: 30,
        }
    }

    fn value_area() -> ValueAreaConfig {
        ValueAreaConfig {
            value_area_pct: 0.7,
        }
    }

    fn from_to(
        from: &str,
        to: &str,
    ) -> (DateTime<chrono::FixedOffset>, DateTime<chrono::FixedOffset>) {
        (
            DateTime::parse_from_rfc3339(from).expect("from"),
            DateTime::parse_from_rfc3339(to).expect("to"),
        )
    }

    fn session_ticks() -> Vec<EngineTick> {
        vec![
            tick(
                1776564000,
                "2026-04-19T09:00:00+07:00",
                1251.0,
                10,
                TickSide::Buy,
            ),
            tick(
                1776564900,
                "2026-04-19T09:15:00+07:00",
                1251.1,
                12,
                TickSide::Sell,
            ),
            tick(
                1776565800,
                "2026-04-19T09:30:00+07:00",
                1251.2,
                8,
                TickSide::Buy,
            ),
            tick(
                1776567600,
                "2026-04-19T10:00:00+07:00",
                1251.3,
                20,
                TickSide::Sell,
            ),
        ]
    }

    #[test]
    fn session_view_matches_python_semantics() {
        let ticks = session_ticks();
        let (from, to) = from_to("2026-04-19T09:00:00+07:00", "2026-04-19T11:30:00+07:00");
        let profile = build_session_profile(
            "VN30F1M",
            from,
            to,
            &ticks,
            ProfileType::Both,
            config(),
            value_area(),
        )
        .expect("session profile");

        let view = build_session_profile_view(&profile);

        assert_eq!(view.symbol, "VN30F1M");
        assert_eq!(view.from, "2026-04-19T09:00:00+07:00");
        assert_eq!(view.to, "2026-04-19T11:30:00+07:00");
        assert_eq!(view.profile_type, "both");
        assert_eq!(view.price_grouping, 1);
        assert_eq!(view.tpo_bucket_minutes, 30);
        assert!(view.volume_profile.is_some());
        assert!(view.tpo_profile.is_some());
        assert!(view.auction_references.is_some());
    }

    #[test]
    fn context_view_keeps_shared_price_levels_and_offset_shape() {
        let ticks = session_ticks();
        let profile = build_context_profile(
            "VN30F1M",
            &ticks,
            1,
            ContextMode::Standard,
            0,
            config(),
            value_area(),
        )
        .expect("context profile");

        let view = build_context_profile_view(&profile);

        assert_eq!(view.symbol, "VN30F1M");
        assert_eq!(view.context_mode, "standard");
        assert_eq!(view.window_size, Some(1));
        assert_eq!(view.offset, 0);
        assert!(view
            .shared_price_levels
            .as_ref()
            .is_some_and(|levels| !levels.is_empty()));
        assert_eq!(view.sessions.len(), 1);
        assert_eq!(
            view.sessions[0].session_key,
            profile.sessions[0].session_key
        );
    }

    #[test]
    fn composite_view_uses_range_and_applied_range_shape() {
        let ticks = session_ticks();
        let (from, to) = from_to("2026-04-19T09:00:00+07:00", "2026-04-19T11:30:00+07:00");
        let profile = build_composite_profile("VN30F1M", from, to, &ticks, config(), value_area())
            .expect("composite profile");

        let view = build_composite_profile_view(&profile);

        assert_eq!(view.symbol, "VN30F1M");
        assert_eq!(view.range.from, "2026-04-19T09:00:00+07:00");
        assert_eq!(view.applied_range.to, "2026-04-19T11:30:00+07:00");
        assert_eq!(view.overview_interval, "M5");
        assert!(!view.overview_bars.is_empty());
    }

    #[test]
    fn replay_view_converts_bps_to_fraction_and_side_codes() {
        let ticks = session_ticks();
        let (from, to) = from_to("2026-04-19T09:00:00+07:00", "2026-04-19T11:30:00+07:00");
        let range = build_replay_range("VN30F1M", from, to, &ticks, config(), value_area())
            .expect("replay range");

        let view = build_replay_range_view(&range);

        assert_eq!(view.symbol, "VN30F1M");
        assert_eq!(view.value_area_pct, 0.7);
        assert_eq!(view.ticks[0].side, Some("B".to_owned()));
        assert!(view.ticks[0].timestamp.is_some());
    }

    #[test]
    fn final_profile_view_wraps_session_and_composite_outputs() {
        let ticks = session_ticks();
        let (from, to) = from_to("2026-04-19T09:00:00+07:00", "2026-04-19T11:30:00+07:00");
        let session = build_session_profile(
            "VN30F1M",
            from,
            to,
            &ticks,
            ProfileType::Both,
            config(),
            value_area(),
        )
        .expect("session profile");
        let composite =
            build_composite_profile("VN30F1M", from, to, &ticks, config(), value_area())
                .expect("composite profile");

        let session_view = build_final_profile_view(&EitherProfile::Session(session));
        let composite_view = build_final_profile_view(&EitherProfile::Composite(composite));

        assert!(matches!(session_view, FinalProfileView::Session(_)));
        assert!(matches!(composite_view, FinalProfileView::Composite(_)));
    }
}
