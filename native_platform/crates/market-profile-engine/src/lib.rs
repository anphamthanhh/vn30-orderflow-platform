//! TPO and volume profiles for VN30F1M sessions: POC and value area with explicit tie rules,
//! initial-balance references and the 80% Rule ([`eighty_percent`]).

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

pub mod eighty_percent;

use std::collections::{BTreeMap, BTreeSet, HashSet};

use chrono::{DateTime, Duration, FixedOffset, TimeZone, Timelike};
use market_time::{
    extract_tick_datetime, intraday_session_bounds, intraday_session_key, TimeError,
};
use market_types::{EventTimestamp, ParseError, Price, RawTickTimeFields, Tick, TickSide, Volume};
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const DEFAULT_VALUE_AREA_PCT: f64 = 0.70;
pub const DEFAULT_TPO_BUCKET_MINUTES: i64 = 30;
pub const OVERVIEW_BUCKET_MINUTES: i64 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProfileBuildConfig {
    pub price_grouping: i32,
    pub tick_size: Price,
    pub tpo_bucket_minutes: i64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ValueAreaConfig {
    pub value_area_pct: f64,
}

#[derive(Debug, Error, Clone, PartialEq)]
pub enum ProfileError {
    #[error("parse error: {0}")]
    Parse(#[from] ParseError),
    #[error("symbol must not be empty")]
    EmptySymbol,
    #[error("tick size must be positive")]
    NonPositiveTickSize,
    #[error("price grouping must be greater than zero")]
    NonPositivePriceGrouping,
    #[error("value area pct must be in the range (0, 1]")]
    InvalidValueAreaPct,
    #[error("tpo bucket minutes must be greater than zero")]
    NonPositiveTpoBucketMinutes,
    #[error("window seconds must be greater than zero")]
    NonPositiveWindowSeconds,
    #[error("step seconds must be greater than zero")]
    NonPositiveStepSeconds,
    #[error("step seconds must be less than or equal to window seconds")]
    StepExceedsWindow,
    #[error("offset must be non-negative")]
    NegativeOffset,
    #[error("session count must be greater than zero")]
    NonPositiveSessionCount,
    #[error("from must be less than or equal to to")]
    InvalidRangeOrder,
    #[error("rows must not be empty")]
    EmptyRows,
    #[error("value area cannot be calculated when total volume is zero")]
    ZeroTotalVolume,
    #[error("mixed-session input in session-only builder")]
    MixedSessionInput,
    #[error("no ticks found in the morning initial-balance window")]
    MissingInitialBalance,
    #[error("tick datetime is outside VN30F intraday trading hours")]
    OutsideTradingHours,
    #[error("context mode must be either 'standard' or 'deep'")]
    InvalidContextMode,
    #[error("time error: {0}")]
    Time(#[from] TimeError),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProfileType {
    Volume,
    Tpo,
    Both,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ContextMode {
    Standard,
    Deep,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineTick {
    pub timestamp: EventTimestamp,
    pub datetime: DateTime<FixedOffset>,
    pub price: Price,
    pub volume: Volume,
    pub side: Option<TickSide>,
    pub last_trade_side: Option<TickSide>,
}

impl EngineTick {
    pub fn from_tick_and_time_fields(
        tick: Tick,
        time_fields: RawTickTimeFields,
        last_trade_side: Option<TickSide>,
    ) -> Result<Self, ProfileError> {
        let datetime = extract_tick_datetime(&market_time::TickTimeInput {
            datetime_iso: time_fields.datetime_iso,
            timestamp: Some(tick.timestamp),
        })?;
        Ok(Self {
            timestamp: tick.timestamp,
            datetime,
            price: tick.price,
            volume: tick.volume,
            side: tick.side,
            last_trade_side,
        })
    }

    pub fn session_key(&self) -> Result<String, ProfileError> {
        Ok(intraday_session_key(self.datetime)?.as_str().to_owned())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeProfileRow {
    pub price: Price,
    pub volume: Volume,
    pub buy_volume: Volume,
    pub sell_volume: Volume,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TpoProfileRow {
    pub price: Price,
    pub tpo_count: usize,
    pub bucket_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfileSummary<Row> {
    pub rows: Vec<Row>,
    pub poc: Option<Price>,
    pub vah: Option<Price>,
    pub val: Option<Price>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuctionReferences {
    pub session_open: Price,
    pub session_high: Price,
    pub session_low: Price,
    pub ib_high: Option<Price>,
    pub ib_low: Option<Price>,
    pub tpo_poc: Option<Price>,
    pub volume_poc: Option<Price>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdvancedReferences {
    pub poor_high: Option<Price>,
    pub poor_low: Option<Price>,
    pub single_print_prices: Vec<Price>,
    pub excess_high: Vec<Price>,
    pub excess_low: Vec<Price>,
    /// Whether an initial balance was available when these references were built.
    ///
    /// Single prints and excess are defined relative to the IB, so `false` means "not computed",
    /// not "none found".
    pub ib_available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionProfile {
    pub symbol: String,
    pub from_iso: String,
    pub to_iso: String,
    pub profile_type: ProfileType,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub volume_profile: Option<ProfileSummary<VolumeProfileRow>>,
    pub tpo_profile: Option<ProfileSummary<TpoProfileRow>>,
    pub auction_references: Option<AuctionReferences>,
    pub advanced_references: Option<AdvancedReferences>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextSessionProfile {
    pub session_key: String,
    pub from_iso: String,
    pub to_iso: String,
    pub shared_price_levels: Vec<Price>,
    pub market_profile: ProfileSummary<TpoProfileRow>,
    pub volume_profile: ProfileSummary<VolumeProfileRow>,
    pub auction_references: Option<AuctionReferences>,
    pub advanced_references: Option<AdvancedReferences>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextProfile {
    pub symbol: String,
    pub context_mode: ContextMode,
    pub window_size: usize,
    pub offset: usize,
    pub shared_price_levels: Vec<Price>,
    pub sessions: Vec<ContextSessionProfile>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OverviewBar {
    pub timestamp: EventTimestamp,
    pub datetime_iso: String,
    pub open: Price,
    pub high: Price,
    pub low: Price,
    pub close: Price,
    pub volume: Volume,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompositeProfile {
    pub symbol: String,
    pub from_iso: String,
    pub to_iso: String,
    pub overview_bars: Vec<OverviewBar>,
    pub overview_interval: String,
    pub market_profile: ProfileSummary<TpoProfileRow>,
    pub volume_profile: ProfileSummary<VolumeProfileRow>,
    pub auction_references: Option<AuctionReferences>,
    pub advanced_references: Option<AdvancedReferences>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayTick {
    pub datetime_iso: String,
    pub price: Price,
    pub volume: Volume,
    pub timestamp: Option<EventTimestamp>,
    pub event_sequence: Option<u64>,
    pub side: Option<TickSide>,
    pub last_trade_side: Option<TickSide>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayRange {
    pub symbol: String,
    pub from_iso: String,
    pub to_iso: String,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub value_area_pct_bps: u32,
    pub ticks: Vec<ReplayTick>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SessionStats {
    open: Price,
    high: Price,
    low: Price,
}

fn validate_symbol(symbol: &str) -> Result<(), ProfileError> {
    if symbol.trim().is_empty() {
        return Err(ProfileError::EmptySymbol);
    }
    Ok(())
}

fn validate_price_grouping(price_grouping: i32) -> Result<(), ProfileError> {
    if price_grouping <= 0 {
        return Err(ProfileError::NonPositivePriceGrouping);
    }
    Ok(())
}

fn validate_tick_size(tick_size: Price) -> Result<(), ProfileError> {
    if tick_size.0 <= 0 {
        return Err(ProfileError::NonPositiveTickSize);
    }
    Ok(())
}

fn validate_value_area_pct(value_area_pct: f64) -> Result<(), ProfileError> {
    if !(0.0 < value_area_pct && value_area_pct <= 1.0) {
        return Err(ProfileError::InvalidValueAreaPct);
    }
    Ok(())
}

fn validate_tpo_bucket_minutes(value: i64) -> Result<(), ProfileError> {
    if value <= 0 {
        return Err(ProfileError::NonPositiveTpoBucketMinutes);
    }
    Ok(())
}

fn validate_range(
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
) -> Result<(), ProfileError> {
    if from > to {
        return Err(ProfileError::InvalidRangeOrder);
    }
    Ok(())
}

fn bucket_price(
    price: Price,
    tick_size: Price,
    price_grouping: i32,
) -> Result<Price, ProfileError> {
    validate_tick_size(tick_size)?;
    validate_price_grouping(price_grouping)?;
    let step = tick_size.0 * i64::from(price_grouping);
    let ratio = price.0 as f64 / step as f64;
    let bucket_index = if ratio >= 0.0 {
        (ratio + 0.5).floor() as i64
    } else {
        (ratio - 0.5).ceil() as i64
    };
    Ok(Price(bucket_index * step))
}

fn tpo_bucket_id(tick_dt: DateTime<FixedOffset>, bucket_size_seconds: i64) -> String {
    // zero-padded: the viewmodel sorts these ids as strings, so string order must be time order
    format!("{:012}", tick_dt.timestamp() / bucket_size_seconds)
}

fn empty_volume_profile() -> ProfileSummary<VolumeProfileRow> {
    ProfileSummary {
        rows: Vec::new(),
        poc: None,
        vah: None,
        val: None,
    }
}

fn empty_tpo_profile() -> ProfileSummary<TpoProfileRow> {
    ProfileSummary {
        rows: Vec::new(),
        poc: None,
        vah: None,
        val: None,
    }
}

/// POC and value area `(poc, vah, val)` of `(price, weight)` rows in ascending price order.
///
/// - POC: highest weight; ties go to the price nearest the mid-range, then to the higher price.
/// - Expansion: one price level per step, heavier neighbour first; a tie takes the lower side.
fn value_area_from_pairs(
    rows: &[(Price, i64)],
    value_area_pct: f64,
) -> Result<(Price, Price, Price), ProfileError> {
    if rows.is_empty() {
        return Err(ProfileError::EmptyRows);
    }
    validate_value_area_pct(value_area_pct)?;
    let total_volume: i64 = rows.iter().map(|(_, volume)| *volume).sum();
    if total_volume <= 0 {
        return Err(ProfileError::ZeroTotalVolume);
    }
    // rows ascend, so first and last bound the range; |2p - (low + high)| avoids halving
    let two_mid = rows[0].0 .0 + rows[rows.len() - 1].0 .0;
    let midrange_distance = |price: Price| (2 * price.0 - two_mid).abs();
    let mut poc_index = 0usize;
    for idx in 1..rows.len() {
        let is_better = rows[idx].1 > rows[poc_index].1
            || (rows[idx].1 == rows[poc_index].1
                && (midrange_distance(rows[idx].0) < midrange_distance(rows[poc_index].0)
                    || (midrange_distance(rows[idx].0) == midrange_distance(rows[poc_index].0)
                        && rows[idx].0 .0 > rows[poc_index].0 .0)));
        if is_better {
            poc_index = idx;
        }
    }
    let target_volume = (total_volume as f64) * value_area_pct;
    let mut covered_volume = rows[poc_index].1 as f64;
    let mut included = BTreeSet::from([poc_index]);
    let mut left = poc_index.checked_sub(1);
    let mut right = poc_index + 1;

    while covered_volume < target_volume && (left.is_some() || right < rows.len()) {
        let left_volume = left.map(|index| rows[index].1).unwrap_or(-1);
        let right_volume = if right < rows.len() {
            rows[right].1
        } else {
            -1
        };
        if right_volume > left_volume {
            included.insert(right);
            covered_volume += rows[right].1 as f64;
            right += 1;
        } else if let Some(index) = left {
            included.insert(index);
            covered_volume += rows[index].1 as f64;
            left = index.checked_sub(1);
        } else if right < rows.len() {
            included.insert(right);
            covered_volume += rows[right].1 as f64;
            right += 1;
        }
    }

    let first = *included.iter().next().ok_or(ProfileError::EmptyRows)?;
    let last = *included.iter().next_back().ok_or(ProfileError::EmptyRows)?;
    Ok((rows[poc_index].0, rows[last].0, rows[first].0))
}

fn build_volume_profile(
    ticks: &[EngineTick],
    tick_size: Price,
    price_grouping: i32,
    value_area_pct: f64,
) -> Result<ProfileSummary<VolumeProfileRow>, ProfileError> {
    validate_tick_size(tick_size)?;
    validate_price_grouping(price_grouping)?;
    validate_value_area_pct(value_area_pct)?;
    let mut volume_by_price = BTreeMap::<Price, (i64, i64, i64)>::new(); // (total, buy, sell)
    for tick in ticks {
        let bucket = bucket_price(tick.price, tick_size, price_grouping)?;
        let entry = volume_by_price.entry(bucket).or_default();
        entry.0 += tick.volume.0;
        match tick.side {
            Some(TickSide::Buy) => entry.1 += tick.volume.0,
            Some(TickSide::Sell) => entry.2 += tick.volume.0,
            None => {} // uninferred ticks add to total only
        }
    }
    let rows: Vec<(Price, i64)> = volume_by_price
        .iter()
        .map(|(price, (total, _, _))| (*price, *total))
        .collect();
    let total_volume: i64 = rows.iter().map(|(_, volume)| *volume).sum();
    if total_volume <= 0 {
        return Ok(empty_volume_profile());
    }
    let (poc, vah, val) = value_area_from_pairs(&rows, value_area_pct)?;
    Ok(ProfileSummary {
        rows: volume_by_price
            .into_iter()
            .map(|(price, (total, buy, sell))| VolumeProfileRow {
                price,
                volume: Volume(total),
                buy_volume: Volume(buy),
                sell_volume: Volume(sell),
            })
            .collect(),
        poc: Some(poc),
        vah: Some(vah),
        val: Some(val),
    })
}

pub fn build_tpo_profile_from_ticks(
    ticks: &[EngineTick],
    tick_size: Price,
    price_grouping: i32,
    value_area_pct: f64,
    tpo_bucket_minutes: i64,
) -> Result<ProfileSummary<TpoProfileRow>, ProfileError> {
    validate_tick_size(tick_size)?;
    validate_price_grouping(price_grouping)?;
    validate_value_area_pct(value_area_pct)?;
    validate_tpo_bucket_minutes(tpo_bucket_minutes)?;
    let bucket_size_seconds = tpo_bucket_minutes * 60;
    let mut buckets_by_price = BTreeMap::<Price, BTreeSet<String>>::new();
    for tick in ticks {
        let bucket_id = tpo_bucket_id(tick.datetime, bucket_size_seconds);
        let price = bucket_price(tick.price, tick_size, price_grouping)?;
        buckets_by_price.entry(price).or_default().insert(bucket_id);
    }
    if buckets_by_price.is_empty() {
        return Ok(empty_tpo_profile());
    }
    let value_area_rows: Vec<(Price, i64)> = buckets_by_price
        .iter()
        .map(|(price, bucket_ids)| (*price, i64::try_from(bucket_ids.len()).unwrap_or(i64::MAX)))
        .collect();
    let (poc, vah, val) = value_area_from_pairs(&value_area_rows, value_area_pct)?;
    Ok(ProfileSummary {
        rows: buckets_by_price
            .into_iter()
            .map(|(price, bucket_ids)| TpoProfileRow {
                price,
                tpo_count: bucket_ids.len(),
                bucket_ids: bucket_ids.into_iter().collect(),
            })
            .collect(),
        poc: Some(poc),
        vah: Some(vah),
        val: Some(val),
    })
}

fn calculate_initial_balance(ticks: &[EngineTick]) -> Result<(Price, Price), ProfileError> {
    if ticks.is_empty() {
        return Err(ProfileError::EmptyRows);
    }
    let mut session_dates = HashSet::new();
    let mut ib_prices = Vec::new();
    for tick in ticks {
        if !market_time::is_vn30f_trading_time(tick.datetime) {
            return Err(ProfileError::OutsideTradingHours);
        }
        session_dates.insert(tick.datetime.date_naive());
        if market_time::is_morning_initial_balance_time(tick.datetime) {
            ib_prices.push(tick.price);
        }
    }
    if session_dates.len() > 1 {
        return Err(ProfileError::MixedSessionInput);
    }
    if ib_prices.is_empty() {
        return Err(ProfileError::MissingInitialBalance);
    }
    let ib_high = *ib_prices
        .iter()
        .max()
        .ok_or(ProfileError::MissingInitialBalance)?;
    let ib_low = *ib_prices
        .iter()
        .min()
        .ok_or(ProfileError::MissingInitialBalance)?;
    Ok((ib_high, ib_low))
}

fn calculate_session_stats(ticks: &[EngineTick]) -> Result<SessionStats, ProfileError> {
    let first = ticks.first().ok_or(ProfileError::EmptyRows)?;
    let mut high = first.price;
    let mut low = first.price;
    for tick in ticks.iter().skip(1) {
        if tick.price > high {
            high = tick.price;
        }
        if tick.price < low {
            low = tick.price;
        }
    }
    Ok(SessionStats {
        open: first.price,
        high,
        low,
    })
}

fn build_auction_references(
    ticks: &[EngineTick],
    volume_profile: Option<&ProfileSummary<VolumeProfileRow>>,
    tpo_profile: Option<&ProfileSummary<TpoProfileRow>>,
) -> Result<Option<AuctionReferences>, ProfileError> {
    if ticks.is_empty() {
        return Ok(None);
    }
    let stats = calculate_session_stats(ticks)?;
    let initial_balance = calculate_initial_balance(ticks).ok();
    Ok(Some(AuctionReferences {
        session_open: stats.open,
        session_high: stats.high,
        session_low: stats.low,
        ib_high: initial_balance.map(|(high, _)| high),
        ib_low: initial_balance.map(|(_, low)| low),
        tpo_poc: tpo_profile.and_then(|profile| profile.poc),
        volume_poc: volume_profile.and_then(|profile| profile.poc),
    }))
}

fn build_advanced_references(
    ticks: &[EngineTick],
    tpo_profile: Option<&ProfileSummary<TpoProfileRow>>,
    auction_references: Option<&AuctionReferences>,
) -> Option<AdvancedReferences> {
    if ticks.is_empty() {
        return None;
    }
    let rows = tpo_profile?.rows.as_slice();
    if rows.is_empty() {
        return None;
    }
    let high_row = rows.last()?;
    let low_row = rows.first()?;
    let poor_high = (high_row.tpo_count <= 2).then_some(high_row.price);
    let poor_low = (low_row.tpo_count <= 2).then_some(low_row.price);
    let ib_high = auction_references.and_then(|refs| refs.ib_high);
    let ib_low = auction_references.and_then(|refs| refs.ib_low);

    let single_print_prices = rows
        .iter()
        .enumerate()
        .filter(|(index, _)| *index > 0 && *index + 1 < rows.len())
        .map(|(_, row)| row)
        .filter(|row| {
            row.tpo_count == 1
                && ((ib_high.is_some() && Some(row.price) > ib_high)
                    || (ib_low.is_some() && Some(row.price) < ib_low))
        })
        .map(|row| row.price)
        .collect();

    let mut excess_high = Vec::new();
    if let Some(ib_high) = ib_high {
        for row in rows.iter().rev() {
            if row.tpo_count != 1 || row.price < ib_high {
                break;
            }
            excess_high.push(row.price);
        }
        excess_high.reverse();
    }

    let mut excess_low = Vec::new();
    if let Some(ib_low) = ib_low {
        for row in rows {
            if row.tpo_count != 1 || row.price > ib_low {
                break;
            }
            excess_low.push(row.price);
        }
    }

    Some(AdvancedReferences {
        poor_high,
        poor_low,
        single_print_prices,
        excess_high,
        excess_low,
        ib_available: ib_high.is_some() || ib_low.is_some(),
    })
}

fn group_ticks_by_session(
    ticks: &[EngineTick],
) -> Result<BTreeMap<String, Vec<&EngineTick>>, ProfileError> {
    let mut grouped = BTreeMap::<String, Vec<&EngineTick>>::new();
    for tick in ticks {
        grouped.entry(tick.session_key()?).or_default().push(tick);
    }
    Ok(grouped)
}

fn build_overview_bars(ticks: &[EngineTick]) -> Vec<OverviewBar> {
    let mut bars = BTreeMap::<DateTime<FixedOffset>, OverviewBar>::new();
    for tick in ticks {
        let minute =
            (tick.datetime.minute() as i64 / OVERVIEW_BUCKET_MINUTES) * OVERVIEW_BUCKET_MINUTES;
        let bucket_dt = tick.datetime
            - Duration::minutes(i64::from(tick.datetime.minute()) - minute)
            - Duration::seconds(i64::from(tick.datetime.second()))
            - Duration::nanoseconds(i64::from(tick.datetime.nanosecond()));
        bars.entry(bucket_dt)
            .and_modify(|bar| {
                if tick.price > bar.high {
                    bar.high = tick.price;
                }
                if tick.price < bar.low {
                    bar.low = tick.price;
                }
                bar.close = tick.price;
                bar.volume.0 += tick.volume.0;
            })
            .or_insert_with(|| OverviewBar {
                timestamp: EventTimestamp(bucket_dt.timestamp()),
                datetime_iso: bucket_dt.to_rfc3339(),
                open: tick.price,
                high: tick.price,
                low: tick.price,
                close: tick.price,
                volume: tick.volume,
            });
    }
    bars.into_values().collect()
}

pub fn build_session_profile(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    ticks: &[EngineTick],
    profile_type: ProfileType,
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<SessionProfile, ProfileError> {
    validate_symbol(symbol)?;
    validate_range(from, to)?;
    let volume_profile = if ticks.is_empty() {
        matches!(profile_type, ProfileType::Volume | ProfileType::Both)
            .then_some(empty_volume_profile())
    } else if matches!(profile_type, ProfileType::Volume | ProfileType::Both) {
        Some(build_volume_profile(
            ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?)
    } else {
        None
    };
    let tpo_profile = if ticks.is_empty() {
        matches!(profile_type, ProfileType::Tpo | ProfileType::Both).then_some(empty_tpo_profile())
    } else if matches!(profile_type, ProfileType::Tpo | ProfileType::Both) {
        Some(build_tpo_profile_from_ticks(
            ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
            profile_config.tpo_bucket_minutes,
        )?)
    } else {
        None
    };
    let auction_references =
        build_auction_references(ticks, volume_profile.as_ref(), tpo_profile.as_ref())?;
    let advanced_references =
        build_advanced_references(ticks, tpo_profile.as_ref(), auction_references.as_ref());
    Ok(SessionProfile {
        symbol: symbol.to_owned(),
        from_iso: from.to_rfc3339(),
        to_iso: to.to_rfc3339(),
        profile_type,
        price_grouping: profile_config.price_grouping,
        tpo_bucket_minutes: profile_config.tpo_bucket_minutes,
        volume_profile,
        tpo_profile,
        auction_references,
        advanced_references,
    })
}

pub fn build_context_profile(
    symbol: &str,
    ticks: &[EngineTick],
    session_count: usize,
    context_mode: ContextMode,
    offset: usize,
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<ContextProfile, ProfileError> {
    validate_symbol(symbol)?;
    if session_count == 0 {
        return Err(ProfileError::NonPositiveSessionCount);
    }
    let grouped = group_ticks_by_session(ticks)?;
    let all_session_keys: Vec<String> = grouped.keys().cloned().collect();
    let ordered_session_keys = if offset >= all_session_keys.len() {
        Vec::new()
    } else {
        let end_index = all_session_keys.len() - offset;
        let start_index = end_index.saturating_sub(session_count);
        all_session_keys[start_index..end_index].to_vec()
    };

    let mut sessions = Vec::new();
    let mut shared = BTreeSet::new();
    for session_key in ordered_session_keys {
        let session_ticks = grouped.get(&session_key).ok_or(ProfileError::EmptyRows)?;
        let owned_ticks: Vec<EngineTick> =
            session_ticks.iter().map(|tick| (*tick).clone()).collect();
        let volume_profile = build_volume_profile(
            &owned_ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?;
        let market_profile = build_tpo_profile_from_ticks(
            &owned_ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
            profile_config.tpo_bucket_minutes,
        )?;
        let auction_references =
            build_auction_references(&owned_ticks, Some(&volume_profile), Some(&market_profile))?;
        let advanced_references = build_advanced_references(
            &owned_ticks,
            Some(&market_profile),
            auction_references.as_ref(),
        );
        for row in &volume_profile.rows {
            shared.insert(row.price);
        }
        for row in &market_profile.rows {
            shared.insert(row.price);
        }
        let (session_start, session_end) = intraday_session_bounds(owned_ticks[0].datetime)?;
        sessions.push((
            session_key,
            session_start,
            session_end,
            volume_profile,
            market_profile,
            auction_references,
            advanced_references,
        ));
    }
    let shared_price_levels: Vec<Price> = shared.into_iter().collect();
    Ok(ContextProfile {
        symbol: symbol.to_owned(),
        context_mode,
        window_size: session_count,
        offset,
        shared_price_levels: shared_price_levels.clone(),
        sessions: sessions
            .into_iter()
            .map(
                |(
                    session_key,
                    session_start,
                    session_end,
                    volume_profile,
                    market_profile,
                    auction_references,
                    advanced_references,
                )| ContextSessionProfile {
                    session_key,
                    from_iso: session_start.to_rfc3339(),
                    to_iso: session_end.to_rfc3339(),
                    shared_price_levels: shared_price_levels.clone(),
                    market_profile,
                    volume_profile,
                    auction_references,
                    advanced_references,
                },
            )
            .collect(),
    })
}

pub fn build_composite_profile(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    ticks: &[EngineTick],
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<CompositeProfile, ProfileError> {
    validate_symbol(symbol)?;
    validate_range(from, to)?;
    let overview_bars = build_overview_bars(ticks);
    let volume_profile = if ticks.is_empty() {
        empty_volume_profile()
    } else {
        build_volume_profile(
            ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?
    };
    let market_profile = if ticks.is_empty() {
        empty_tpo_profile()
    } else {
        build_tpo_profile_from_ticks(
            ticks,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
            profile_config.tpo_bucket_minutes,
        )?
    };
    let auction_references =
        build_auction_references(ticks, Some(&volume_profile), Some(&market_profile))?;
    let advanced_references =
        build_advanced_references(ticks, Some(&market_profile), auction_references.as_ref());
    Ok(CompositeProfile {
        symbol: symbol.to_owned(),
        from_iso: from.to_rfc3339(),
        to_iso: to.to_rfc3339(),
        overview_bars,
        overview_interval: "M5".to_owned(),
        market_profile,
        volume_profile,
        auction_references,
        advanced_references,
    })
}

pub fn build_replay_range(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    ticks: &[EngineTick],
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<ReplayRange, ProfileError> {
    validate_symbol(symbol)?;
    validate_tick_size(profile_config.tick_size)?;
    validate_price_grouping(profile_config.price_grouping)?;
    validate_value_area_pct(value_area.value_area_pct)?;
    validate_tpo_bucket_minutes(profile_config.tpo_bucket_minutes)?;
    validate_range(from, to)?;
    let mut ordered = ticks.to_vec();
    ordered.sort_by(|left, right| {
        left.datetime
            .cmp(&right.datetime)
            .then(left.timestamp.0.cmp(&right.timestamp.0))
            .then(left.price.0.cmp(&right.price.0))
    });
    let ticks = ordered
        .into_iter()
        .filter(|tick| tick.datetime >= from && tick.datetime <= to)
        .map(|tick| ReplayTick {
            datetime_iso: tick.datetime.to_rfc3339(),
            price: tick.price,
            volume: tick.volume,
            timestamp: Some(tick.timestamp),
            event_sequence: None,
            side: tick.side,
            last_trade_side: tick.last_trade_side,
        })
        .collect();
    Ok(ReplayRange {
        symbol: symbol.to_owned(),
        from_iso: from.to_rfc3339(),
        to_iso: to.to_rfc3339(),
        price_grouping: profile_config.price_grouping,
        tpo_bucket_minutes: profile_config.tpo_bucket_minutes,
        value_area_pct_bps: (value_area.value_area_pct * 10_000.0).round() as u32,
        ticks,
    })
}

pub fn build_replay_final_profile(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    ticks: &[EngineTick],
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<EitherProfile, ProfileError> {
    let replay = build_replay_range(symbol, from, to, ticks, profile_config, value_area)?;
    let replay_ticks: Vec<EngineTick> = replay
        .ticks
        .iter()
        .map(|tick| {
            let timestamp = tick.timestamp.ok_or(ProfileError::EmptyRows)?;
            let datetime = DateTime::parse_from_rfc3339(&tick.datetime_iso)
                .map_err(|_| ProfileError::EmptyRows)?;
            Ok(EngineTick {
                timestamp,
                datetime,
                price: tick.price,
                volume: tick.volume,
                side: tick.side,
                last_trade_side: tick.last_trade_side,
            })
        })
        .collect::<Result<_, ProfileError>>()?;
    let session_keys: BTreeSet<String> = replay_ticks
        .iter()
        .map(EngineTick::session_key)
        .collect::<Result<_, _>>()?;
    if session_keys.len() <= 1 {
        Ok(EitherProfile::Session(build_session_profile(
            symbol,
            from,
            to,
            &replay_ticks,
            ProfileType::Both,
            profile_config,
            value_area,
        )?))
    } else {
        Ok(EitherProfile::Composite(build_composite_profile(
            symbol,
            from,
            to,
            &replay_ticks,
            profile_config,
            value_area,
        )?))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EitherProfile {
    Session(SessionProfile),
    Composite(CompositeProfile),
}

fn build_volume_profile_from_rows(
    rows: &[market_data_core::AggregatedVolumeRow],
    tick_size: Price,
    price_grouping: i32,
    value_area_pct: f64,
) -> Result<ProfileSummary<VolumeProfileRow>, ProfileError> {
    validate_tick_size(tick_size)?;
    validate_price_grouping(price_grouping)?;
    validate_value_area_pct(value_area_pct)?;
    let mut volume_by_price = BTreeMap::<Price, (i64, i64, i64)>::new(); // (total, buy, sell)
    for row in rows {
        let bucket = bucket_price(row.price, tick_size, price_grouping)?;
        let entry = volume_by_price.entry(bucket).or_default();
        entry.0 += row.total_volume.0;
        entry.1 += row.buy_volume.0;
        entry.2 += row.sell_volume.0;
    }
    let pairs: Vec<(Price, i64)> = volume_by_price
        .iter()
        .map(|(price, (total, _, _))| (*price, *total))
        .collect();
    let total_volume: i64 = pairs.iter().map(|(_, volume)| *volume).sum();
    if total_volume <= 0 {
        return Ok(empty_volume_profile());
    }
    let (poc, vah, val) = value_area_from_pairs(&pairs, value_area_pct)?;
    Ok(ProfileSummary {
        rows: volume_by_price
            .into_iter()
            .map(|(price, (total, buy, sell))| VolumeProfileRow {
                price,
                volume: Volume(total),
                buy_volume: Volume(buy),
                sell_volume: Volume(sell),
            })
            .collect(),
        poc: Some(poc),
        vah: Some(vah),
        val: Some(val),
    })
}

fn build_tpo_profile_from_aggregated(
    rows: &[market_data_core::AggregatedTpoRow],
    tick_size: Price,
    price_grouping: i32,
    value_area_pct: f64,
) -> Result<ProfileSummary<TpoProfileRow>, ProfileError> {
    validate_tick_size(tick_size)?;
    validate_price_grouping(price_grouping)?;
    validate_value_area_pct(value_area_pct)?;
    let mut tpo_by_price = BTreeMap::<Price, usize>::new();
    for row in rows {
        let bucket = bucket_price(row.price_level, tick_size, price_grouping)?;
        *tpo_by_price.entry(bucket).or_default() += row.tpo_count as usize;
    }
    if tpo_by_price.is_empty() {
        return Ok(empty_tpo_profile());
    }
    let value_area_rows: Vec<(Price, i64)> = tpo_by_price
        .iter()
        .map(|(price, count)| (*price, i64::try_from(*count).unwrap_or(i64::MAX)))
        .collect();
    let (poc, vah, val) = value_area_from_pairs(&value_area_rows, value_area_pct)?;
    Ok(ProfileSummary {
        rows: tpo_by_price
            .into_iter()
            .map(|(price, tpo_count)| {
                // aggregated rows carry no bucket ids: synthesise "0000", "0001", ... for the
                // letter encoder, zero-padded so its string sort stays numeric
                let bucket_ids: Vec<String> = (0..tpo_count).map(|i| format!("{i:04}")).collect();
                TpoProfileRow {
                    price,
                    tpo_count,
                    bucket_ids,
                }
            })
            .collect(),
        poc: Some(poc),
        vah: Some(vah),
        val: Some(val),
    })
}

fn build_auction_references_from_session_stats(
    session_open: Option<Price>,
    session_high: Option<Price>,
    session_low: Option<Price>,
    ib_high: Option<Price>,
    ib_low: Option<Price>,
    volume_profile: Option<&ProfileSummary<VolumeProfileRow>>,
    tpo_profile: Option<&ProfileSummary<TpoProfileRow>>,
) -> Option<AuctionReferences> {
    let open = session_open?;
    let high = session_high?;
    let low = session_low?;
    // the provider supplies the 09:00-10:00 ICT IB; price-keyed aggregated rows cannot give it
    Some(AuctionReferences {
        session_open: open,
        session_high: high,
        session_low: low,
        ib_high,
        ib_low,
        tpo_poc: tpo_profile.and_then(|profile| profile.poc),
        volume_poc: volume_profile.and_then(|profile| profile.poc),
    })
}

pub fn build_session_profile_from_aggregated(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    data: &market_data_core::AggregatedProfileData,
    profile_type: ProfileType,
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<SessionProfile, ProfileError> {
    validate_symbol(symbol)?;
    validate_range(from, to)?;

    let volume_profile = if data.volume_rows.is_empty() {
        matches!(profile_type, ProfileType::Volume | ProfileType::Both)
            .then_some(empty_volume_profile())
    } else if matches!(profile_type, ProfileType::Volume | ProfileType::Both) {
        Some(build_volume_profile_from_rows(
            &data.volume_rows,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?)
    } else {
        None
    };

    let tpo_profile = if data.tpo_rows.is_empty() {
        matches!(profile_type, ProfileType::Tpo | ProfileType::Both).then_some(empty_tpo_profile())
    } else if matches!(profile_type, ProfileType::Tpo | ProfileType::Both) {
        Some(build_tpo_profile_from_aggregated(
            &data.tpo_rows,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?)
    } else {
        None
    };

    let auction_references = build_auction_references_from_session_stats(
        data.session_open,
        data.session_high,
        data.session_low,
        data.ib_high,
        data.ib_low,
        volume_profile.as_ref(),
        tpo_profile.as_ref(),
    );

    let advanced_references =
        build_advanced_references_from_profile(tpo_profile.as_ref(), auction_references.as_ref());

    Ok(SessionProfile {
        symbol: symbol.to_owned(),
        from_iso: from.to_rfc3339(),
        to_iso: to.to_rfc3339(),
        profile_type,
        price_grouping: profile_config.price_grouping,
        tpo_bucket_minutes: profile_config.tpo_bucket_minutes,
        volume_profile,
        tpo_profile,
        auction_references,
        advanced_references,
    })
}

fn build_advanced_references_from_profile(
    tpo_profile: Option<&ProfileSummary<TpoProfileRow>>,
    auction_references: Option<&AuctionReferences>,
) -> Option<AdvancedReferences> {
    let rows = tpo_profile?.rows.as_slice();
    if rows.is_empty() {
        return None;
    }
    let high_row = rows.last()?;
    let low_row = rows.first()?;
    let poor_high = (high_row.tpo_count <= 2).then_some(high_row.price);
    let poor_low = (low_row.tpo_count <= 2).then_some(low_row.price);
    let ib_high = auction_references.and_then(|refs| refs.ib_high);
    let ib_low = auction_references.and_then(|refs| refs.ib_low);

    let single_print_prices = rows
        .iter()
        .enumerate()
        .filter(|(index, _)| *index > 0 && *index + 1 < rows.len())
        .map(|(_, row)| row)
        .filter(|row| {
            row.tpo_count == 1
                && ((ib_high.is_some() && Some(row.price) > ib_high)
                    || (ib_low.is_some() && Some(row.price) < ib_low))
        })
        .map(|row| row.price)
        .collect();

    let mut excess_high = Vec::new();
    if let Some(ib_high) = ib_high {
        for row in rows.iter().rev() {
            if row.tpo_count != 1 || row.price < ib_high {
                break;
            }
            excess_high.push(row.price);
        }
        excess_high.reverse();
    }

    let mut excess_low = Vec::new();
    if let Some(ib_low) = ib_low {
        for row in rows {
            if row.tpo_count != 1 || row.price > ib_low {
                break;
            }
            excess_low.push(row.price);
        }
    }

    Some(AdvancedReferences {
        poor_high,
        poor_low,
        single_print_prices,
        excess_high,
        excess_low,
        ib_available: ib_high.is_some() || ib_low.is_some(),
    })
}

fn overview_bar_from_ohlc(bar: &market_types::OhlcBar) -> OverviewBar {
    let datetime_iso = chrono::DateTime::from_timestamp(bar.timestamp.0, 0)
        .map(|dt| dt.to_rfc3339())
        .unwrap_or_else(|| bar.timestamp.0.to_string());
    OverviewBar {
        timestamp: bar.timestamp,
        datetime_iso,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume,
    }
}

pub fn build_composite_profile_from_aggregated(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    profile_data: &market_data_core::AggregatedProfileData,
    overview_bars: &[market_types::OhlcBar],
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<CompositeProfile, ProfileError> {
    validate_symbol(symbol)?;
    validate_range(from, to)?;

    let volume_profile = if profile_data.volume_rows.is_empty() {
        empty_volume_profile()
    } else {
        build_volume_profile_from_rows(
            &profile_data.volume_rows,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?
    };

    let market_profile = if profile_data.tpo_rows.is_empty() {
        empty_tpo_profile()
    } else {
        build_tpo_profile_from_aggregated(
            &profile_data.tpo_rows,
            profile_config.tick_size,
            profile_config.price_grouping,
            value_area.value_area_pct,
        )?
    };

    let auction_references = build_auction_references_from_session_stats(
        profile_data.session_open,
        profile_data.session_high,
        profile_data.session_low,
        profile_data.ib_high,
        profile_data.ib_low,
        Some(&volume_profile),
        Some(&market_profile),
    );

    let advanced_references =
        build_advanced_references_from_profile(Some(&market_profile), auction_references.as_ref());

    let overview_bars: Vec<OverviewBar> =
        overview_bars.iter().map(overview_bar_from_ohlc).collect();

    Ok(CompositeProfile {
        symbol: symbol.to_owned(),
        from_iso: from.to_rfc3339(),
        to_iso: to.to_rfc3339(),
        overview_bars,
        overview_interval: "M5".to_owned(),
        market_profile,
        volume_profile,
        auction_references,
        advanced_references,
    })
}

#[allow(clippy::too_many_arguments)]
pub fn build_context_profile_from_aggregated(
    symbol: &str,
    from: DateTime<FixedOffset>,
    to: DateTime<FixedOffset>,
    session_profiles: BTreeMap<String, market_data_core::AggregatedProfileData>,
    session_count: usize,
    context_mode: ContextMode,
    offset: usize,
    profile_config: ProfileBuildConfig,
    value_area: ValueAreaConfig,
) -> Result<ContextProfile, ProfileError> {
    validate_symbol(symbol)?;
    validate_range(from, to)?;
    if session_count == 0 {
        return Err(ProfileError::NonPositiveSessionCount);
    }

    let ordered_keys: Vec<&String> = session_profiles.keys().collect();
    let window_keys: Vec<&String> = if offset >= ordered_keys.len() {
        Vec::new()
    } else {
        let end_index = ordered_keys.len() - offset;
        let start_index = end_index.saturating_sub(session_count);
        ordered_keys[start_index..end_index].to_vec()
    };

    let mut sessions = Vec::new();
    let mut shared = BTreeSet::new();

    let tz = market_time::vn30f_timezone()?;

    for session_key_str in &window_keys {
        let data = &session_profiles[session_key_str.as_str()];

        let volume_profile = if data.volume_rows.is_empty() {
            empty_volume_profile()
        } else {
            build_volume_profile_from_rows(
                &data.volume_rows,
                profile_config.tick_size,
                profile_config.price_grouping,
                value_area.value_area_pct,
            )?
        };

        let market_profile = if data.tpo_rows.is_empty() {
            empty_tpo_profile()
        } else {
            build_tpo_profile_from_aggregated(
                &data.tpo_rows,
                profile_config.tick_size,
                profile_config.price_grouping,
                value_area.value_area_pct,
            )?
        };

        let auction_references = build_auction_references_from_session_stats(
            data.session_open,
            data.session_high,
            data.session_low,
            data.ib_high,
            data.ib_low,
            Some(&volume_profile),
            Some(&market_profile),
        );

        let advanced_references = build_advanced_references_from_profile(
            Some(&market_profile),
            auction_references.as_ref(),
        );

        for row in &volume_profile.rows {
            shared.insert(row.price);
        }
        for row in &market_profile.rows {
            shared.insert(row.price);
        }

        let naive_date = chrono::NaiveDate::parse_from_str(session_key_str, "%Y-%m-%d")
            .map_err(|_| ProfileError::Time(TimeError::InvalidDateTime))?;
        let session_start_of_day = tz
            .from_local_datetime(&naive_date.and_hms_opt(0, 0, 0).unwrap())
            .unwrap();
        let (session_start, session_end) = intraday_session_bounds(session_start_of_day)?;

        sessions.push((
            (*session_key_str).clone(),
            session_start,
            session_end,
            volume_profile,
            market_profile,
            auction_references,
            advanced_references,
        ));
    }

    let shared_price_levels: Vec<Price> = shared.into_iter().collect();

    Ok(ContextProfile {
        symbol: symbol.to_owned(),
        context_mode,
        window_size: session_count,
        offset,
        shared_price_levels: shared_price_levels.clone(),
        sessions: sessions
            .into_iter()
            .map(
                |(
                    session_key,
                    session_start,
                    session_end,
                    volume_profile,
                    market_profile,
                    auction_references,
                    advanced_references,
                )| ContextSessionProfile {
                    session_key,
                    from_iso: session_start.to_rfc3339(),
                    to_iso: session_end.to_rfc3339(),
                    shared_price_levels: shared_price_levels.clone(),
                    market_profile,
                    volume_profile,
                    auction_references,
                    advanced_references,
                },
            )
            .collect(),
    })
}

#[cfg(test)]
mod tests;
