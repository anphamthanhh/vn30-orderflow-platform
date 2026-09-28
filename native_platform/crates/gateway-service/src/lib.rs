//! Gateway request handlers over the market-data provider traits, with a shared cache and
//! circuit breaker for profile queries.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use chrono::{Datelike, FixedOffset};
use client_proto::{
    CompositeProfileRequest, ContextProfileRequest, ErrorPayload, HistoricalBarView,
    HistoricalBarsResponse, OutboundPayload, ReplayRangeRequest, SessionProfileRequest,
    SymbolRangeRequest,
};
use market_data_core::{
    AggregatedProfileData, AggregatedProfileProvider, AggregatedProfileQuery, GatewayDataError,
    HistoricalBarProvider, HistoricalBarQuery, HistoricalTickProvider, HistoricalTickQuery,
    ReplayRangeData, ReplayRangeLoadResult, ReplayRangeProvider, ReplayRangeQuery, ReplayTickData,
};
use market_profile_engine::{
    build_composite_profile, build_composite_profile_from_aggregated, build_context_profile,
    build_context_profile_from_aggregated, build_session_profile,
    build_session_profile_from_aggregated, ContextMode, EngineTick, ProfileBuildConfig,
    ProfileError, ProfileType, ValueAreaConfig,
};
use market_profile_viewmodel::{
    build_composite_profile_view, build_context_profile_view, build_session_profile_view,
    CompositeRangeView, ProfileSourceMetaView, ReplayRangeView, ReplayTickView,
};
use market_time::{extract_tick_datetime, intraday_session_bounds, TickTimeInput};
use market_types::{
    EventTimestamp, OhlcBar, ReplayRangeKey, SessionKey, SessionRange, Symbol, Tick, TickSide,
};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum GatewayServiceError {
    #[error("invalid symbol")]
    InvalidSymbol,
    #[error("invalid replay key")]
    InvalidReplayKey,
    #[error("invalid range")]
    InvalidRange,
    #[error(transparent)]
    Data(#[from] GatewayDataError),
}

fn map_gateway_data_error(error: GatewayDataError) -> GatewayServiceError {
    match error {
        GatewayDataError::InvalidRange => GatewayServiceError::InvalidRange,
        other => GatewayServiceError::Data(other),
    }
}

fn parse_symbol(value: String) -> Result<Symbol, GatewayServiceError> {
    Symbol::new(value).map_err(|_| GatewayServiceError::InvalidSymbol)
}

fn parse_replay_key(value: String) -> Result<ReplayRangeKey, GatewayServiceError> {
    ReplayRangeKey::new(value).map_err(|_| GatewayServiceError::InvalidReplayKey)
}

fn parse_optional_timestamp(
    value: Option<i64>,
) -> Result<Option<EventTimestamp>, GatewayServiceError> {
    value
        .map(|timestamp| {
            EventTimestamp::new(timestamp).map_err(|_| GatewayServiceError::InvalidRange)
        })
        .transpose()
}

fn parse_timestamp(value: i64) -> Result<EventTimestamp, GatewayServiceError> {
    EventTimestamp::new(value).map_err(|_| GatewayServiceError::InvalidRange)
}

fn historical_bar_view(bar: OhlcBar) -> HistoricalBarView {
    HistoricalBarView {
        symbol: bar.symbol.as_str().to_owned(),
        timestamp: bar.timestamp.0,
        open: bar.open.as_f64(),
        high: bar.high.as_f64(),
        low: bar.low.as_f64(),
        close: bar.close.as_f64(),
        volume: bar.volume.0,
    }
}

fn fallback_timestamp_iso(timestamp: EventTimestamp) -> String {
    timestamp.0.to_string()
}

fn side_view(side: TickSide) -> &'static str {
    match side {
        TickSide::Buy => "B",
        TickSide::Sell => "S",
    }
}

fn replay_tick_view(replay_tick: &ReplayTickData) -> ReplayTickView {
    let tick = &replay_tick.tick;
    ReplayTickView {
        datetime: fallback_timestamp_iso(tick.timestamp),
        price: tick.price.as_f64(),
        volume: tick.volume.0 as f64,
        timestamp: Some(tick.timestamp.0),
        event_sequence: replay_tick.event_sequence,
        side: tick.side.map(|side| side_view(side).to_owned()),
        last_trade_side: None,
    }
}

fn replay_range_view(symbol: &Symbol, replay: &ReplayRangeData) -> ReplayRangeView {
    let session_range = replay.meta.session_range();
    ReplayRangeView {
        symbol: symbol.as_str().to_owned(),
        range: CompositeRangeView {
            from: session_range.from_timestamp.0.to_string(),
            to: session_range.to_timestamp.0.to_string(),
        },
        price_grouping: 1,
        tpo_bucket_minutes: 30,
        value_area_pct: 0.7,
        sequence_mode: replay.meta.sequence_mode.as_str().to_owned(),
        ticks: replay.ticks.iter().map(replay_tick_view).collect(),
    }
}

fn replay_not_found_payload() -> OutboundPayload {
    OutboundPayload::Error(ErrorPayload {
        code: "replay_range_not_found",
        message: "no replay range found".to_owned(),
    })
}

fn now_unix_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

fn profile_row_count(volume_rows: Option<usize>, tpo_rows: Option<usize>) -> usize {
    volume_rows.unwrap_or(0) + tpo_rows.unwrap_or(0)
}

const PROFILE_ROW_BUDGET: usize = 5_000;
const PROFILE_FRESHNESS_BUDGET_MS: i64 = 7 * 24 * 60 * 60 * 1000;

#[derive(Debug, Hash, Clone, PartialEq, Eq)]
struct ProfileCacheKey {
    symbol: String,
    from_timestamp: i64,
    to_timestamp: i64,
    profile_type: String,
    profile_subtype: String,
}

struct ProfileCacheEntry {
    payload: OutboundPayload,
    cached_at: Instant,
}

const PROFILE_CACHE_TTL_SESSION: Duration = Duration::from_secs(10);
const PROFILE_CACHE_TTL_COMPOSITE: Duration = Duration::from_secs(30);
const PROFILE_CACHE_TTL_CONTEXT: Duration = Duration::from_secs(30);
const PROFILE_CACHE_MAX_ENTRIES: usize = 50;

const BREAKER_FAILURE_THRESHOLD: u32 = 5;
const BREAKER_FAILURE_WINDOW: Duration = Duration::from_secs(10);
const BREAKER_COOLDOWN: Duration = Duration::from_secs(30);
const DEGRADED_CLICKHOUSE_UNAVAILABLE_CACHED: &str = "clickhouse_unavailable_cached";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BreakerState {
    Closed,
    Open,
    HalfOpen,
}

fn breaker_state_str(state: BreakerState) -> &'static str {
    match state {
        BreakerState::Closed => "closed",
        BreakerState::Open => "open",
        BreakerState::HalfOpen => "half_open",
    }
}

#[derive(Debug)]
struct BreakerInner {
    state: BreakerState,
    failure_count: u32,
    window_start: Option<Instant>,
    opened_at: Option<Instant>,
    trial_started_at: Option<Instant>,
}

/// Circuit breaker for the profile handlers' provider (ClickHouse) queries.
///
/// `failure_threshold` failures within `failure_window` open it for `cooldown`; then one
/// half-open trial query closes it on success or re-opens it on failure.
#[derive(Debug)]
struct ClickHouseBreaker {
    failure_threshold: u32,
    failure_window: Duration,
    cooldown: Duration,
    inner: Mutex<BreakerInner>,
}

impl Default for ClickHouseBreaker {
    fn default() -> Self {
        Self::new(
            BREAKER_FAILURE_THRESHOLD,
            BREAKER_FAILURE_WINDOW,
            BREAKER_COOLDOWN,
        )
    }
}

impl ClickHouseBreaker {
    fn new(failure_threshold: u32, failure_window: Duration, cooldown: Duration) -> Self {
        Self {
            failure_threshold,
            failure_window,
            cooldown,
            inner: Mutex::new(BreakerInner {
                state: BreakerState::Closed,
                failure_count: 0,
                window_start: None,
                opened_at: None,
                trial_started_at: None,
            }),
        }
    }

    /// Effective state for a request: after `cooldown`, one trial sees `HalfOpen` and concurrent
    /// requests see `Open`; a trial that never reports back is re-armed after `cooldown`.
    fn check(&self) -> BreakerState {
        let Ok(mut inner) = self.inner.lock() else {
            return BreakerState::Closed;
        };
        match inner.state {
            BreakerState::Closed => BreakerState::Closed,
            BreakerState::Open => {
                let cooled_down = inner
                    .opened_at
                    .is_none_or(|opened_at| opened_at.elapsed() >= self.cooldown);
                if cooled_down {
                    inner.state = BreakerState::HalfOpen;
                    inner.trial_started_at = Some(Instant::now());
                    BreakerState::HalfOpen
                } else {
                    BreakerState::Open
                }
            }
            BreakerState::HalfOpen => {
                let trial_stalled = inner
                    .trial_started_at
                    .is_none_or(|started_at| started_at.elapsed() >= self.cooldown);
                if trial_stalled {
                    inner.trial_started_at = Some(Instant::now());
                    BreakerState::HalfOpen
                } else {
                    BreakerState::Open
                }
            }
        }
    }

    fn record_success(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.state = BreakerState::Closed;
            inner.failure_count = 0;
            inner.window_start = None;
            inner.opened_at = None;
            inner.trial_started_at = None;
        }
    }

    fn record_failure(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            match inner.state {
                BreakerState::HalfOpen => {
                    inner.state = BreakerState::Open;
                    inner.opened_at = Some(Instant::now());
                    inner.trial_started_at = None;
                }
                BreakerState::Open => {}
                BreakerState::Closed => {
                    let now = Instant::now();
                    let window_expired = inner
                        .window_start
                        .is_none_or(|start| now.duration_since(start) >= self.failure_window);
                    if window_expired {
                        inner.window_start = Some(now);
                        inner.failure_count = 1;
                    } else {
                        inner.failure_count = inner.failure_count.saturating_add(1);
                    }
                    if inner.failure_count >= self.failure_threshold {
                        inner.state = BreakerState::Open;
                        inner.opened_at = Some(now);
                        log::warn!(
                            "clickhouse breaker opened after {} failures within window",
                            inner.failure_count
                        );
                    }
                }
            }
        }
    }

    fn state_str(&self) -> &'static str {
        let state = self
            .inner
            .lock()
            .map_or(BreakerState::Closed, |inner| inner.state);
        breaker_state_str(state)
    }
}

fn elapsed_ms(started_at: Instant) -> u64 {
    u64::try_from(started_at.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn log_profile_telemetry(op: &str, cache_hit: bool, breaker_state: &str, ch_query_ms: u64) {
    log::info!(
        "lane=query op={op} stage=profile_provider cache_hit={cache_hit} breaker_state={breaker_state} ch_query_ms={ch_query_ms}"
    );
}

/// Add the `clickhouse_unavailable_cached` marker to a cached profile payload.
fn mark_cached_payload_degraded(payload: &mut OutboundPayload) {
    let source = match payload {
        OutboundPayload::SessionProfile(view) => view.source.as_mut(),
        OutboundPayload::CompositeProfile(view) => view.source.as_mut(),
        OutboundPayload::ContextProfile(view) => view.source.as_mut(),
        _ => None,
    };
    if let Some(source) = source {
        append_degraded_reason(
            &mut source.degraded_reason,
            DEGRADED_CLICKHOUSE_UNAVAILABLE_CACHED,
        );
    }
}

fn append_degraded_reason(reason: &mut Option<String>, value: &str) {
    match reason {
        Some(existing) if !existing.split(',').any(|part| part == value) => {
            existing.push(',');
            existing.push_str(value);
        }
        Some(_) => {}
        None => *reason = Some(value.to_owned()),
    }
}

fn apply_profile_budget_state(
    source_mode: &str,
    row_count: usize,
    latest_event_timestamp: Option<EventTimestamp>,
    generated_at_unix_ms: i64,
    degraded_reason: &mut Option<String>,
) {
    if source_mode == "raw" {
        append_degraded_reason(degraded_reason, "raw_profile_fallback");
    }
    if row_count == 0 {
        append_degraded_reason(degraded_reason, "missing_aggregate");
    }
    if row_count > PROFILE_ROW_BUDGET {
        append_degraded_reason(degraded_reason, "profile_row_budget_exceeded");
    }
    if latest_event_timestamp
        .map(|timestamp| generated_at_unix_ms.saturating_sub(timestamp.0.saturating_mul(1000)))
        .is_some_and(|freshness_age_ms| freshness_age_ms > PROFILE_FRESHNESS_BUDGET_MS)
    {
        append_degraded_reason(degraded_reason, "stale_freshness");
    }
}

fn profile_source_meta(
    source_mode: &str,
    source_name: &str,
    sequence_mode: &str,
    latest_event_timestamp: Option<EventTimestamp>,
    row_count: usize,
    mut degraded_reason: Option<String>,
) -> ProfileSourceMetaView {
    let generated_at_unix_ms = now_unix_ms();
    apply_profile_budget_state(
        source_mode,
        row_count,
        latest_event_timestamp,
        generated_at_unix_ms,
        &mut degraded_reason,
    );
    let freshness_age_ms = latest_event_timestamp
        .map(|timestamp| generated_at_unix_ms.saturating_sub(timestamp.0.saturating_mul(1000)));
    ProfileSourceMetaView {
        source_mode: source_mode.to_owned(),
        source_name: source_name.to_owned(),
        sequence_mode: sequence_mode.to_owned(),
        generated_at_unix_ms,
        latest_event_timestamp: latest_event_timestamp.map(|timestamp| timestamp.0),
        freshness_age_ms,
        row_count,
        degraded_reason,
    }
}

fn parse_profile_type(value: String) -> Result<ProfileType, GatewayServiceError> {
    match value.as_str() {
        "volume" => Ok(ProfileType::Volume),
        "tpo" => Ok(ProfileType::Tpo),
        "both" => Ok(ProfileType::Both),
        _ => Err(GatewayServiceError::Data(GatewayDataError::Internal(
            "invalid profile type".to_owned(),
        ))),
    }
}

fn parse_context_mode(value: String) -> Result<ContextMode, GatewayServiceError> {
    match value.as_str() {
        "standard" => Ok(ContextMode::Standard),
        "deep" => Ok(ContextMode::Deep),
        _ => Err(GatewayServiceError::Data(GatewayDataError::Internal(
            "invalid context mode".to_owned(),
        ))),
    }
}

fn map_profile_error(error: ProfileError) -> GatewayServiceError {
    match error {
        ProfileError::InvalidRangeOrder => GatewayServiceError::InvalidRange,
        other => GatewayServiceError::Data(GatewayDataError::Internal(other.to_string())),
    }
}

fn engine_tick_from_tick(tick: Tick) -> Result<EngineTick, GatewayServiceError> {
    let datetime = extract_tick_datetime(&TickTimeInput {
        datetime_iso: None,
        timestamp: Some(tick.timestamp),
    })
    .map_err(|error| GatewayServiceError::Data(GatewayDataError::Internal(error.to_string())))?;
    Ok(EngineTick {
        timestamp: tick.timestamp,
        datetime,
        price: tick.price,
        volume: tick.volume,
        side: tick.side,
        last_trade_side: None,
    })
}

type SharedProfileCache = Arc<Mutex<HashMap<ProfileCacheKey, ProfileCacheEntry>>>;

/// Profile cache and circuit breaker shared by all connections.
///
/// The server builds one `GatewayService` per connection; attaching a clone of this handle to each
/// lets any connection serve profiles cached by another while the breaker is open.
#[derive(Clone, Default)]
pub struct ProfileResilience {
    cache: SharedProfileCache,
    breaker: Arc<ClickHouseBreaker>,
}

/// Calendar days to search back for a prior session with a usable value area.
const PRIOR_VA_MAX_LOOKBACK_DAYS: i64 = 5;

/// Advisory 80% Rule session bias (SIG-001 tier 2) from [`GatewayService::compute_session_bias`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SessionBiasData {
    /// `true` when the session opened below the prior value area and rotated back in (target
    /// VAH); `false` for the bearish mirror (target VAL).
    pub bullish: bool,
    /// The opposite value-area edge.
    pub target: f64,
    pub prior_vah: f64,
    pub prior_val: f64,
    pub confidence: f64,
}

pub struct GatewayService<B, T, R, A = ()> {
    historical_bars: B,
    historical_ticks: T,
    replay_ranges: R,
    aggregated_profile: Option<A>,
    profile_cache: SharedProfileCache,
    ch_breaker: Arc<ClickHouseBreaker>,
    /// Current-session ticks fetched at connect time, covering `backfill_range` inclusive; a
    /// session-profile request inside that range is built from them instead of a new query.
    backfill_ticks: Option<Arc<Vec<Tick>>>,
    backfill_range: Option<(EventTimestamp, EventTimestamp)>,
}

impl<B, T, R, A> GatewayService<B, T, R, A> {
    #[must_use]
    pub fn new(
        historical_bars: B,
        historical_ticks: T,
        replay_ranges: R,
        aggregated_profile: Option<A>,
    ) -> Self {
        Self {
            historical_bars,
            historical_ticks,
            replay_ranges,
            aggregated_profile,
            profile_cache: Arc::new(Mutex::new(HashMap::new())),
            ch_breaker: Arc::new(ClickHouseBreaker::default()),
            backfill_ticks: None,
            backfill_range: None,
        }
    }

    /// Use the shared cache and breaker of [`ProfileResilience`].
    #[must_use]
    pub fn with_resilience(mut self, shared: &ProfileResilience) -> Self {
        self.profile_cache = Arc::clone(&shared.cache);
        self.ch_breaker = Arc::clone(&shared.breaker);
        self
    }

    /// Attach connect-time ticks that serve session-profile requests inside `[from, to]`.
    ///
    /// `from` and `to` must be the exact range the ticks were fetched for.
    #[must_use]
    pub fn with_backfill_ticks(
        mut self,
        from: EventTimestamp,
        to: EventTimestamp,
        ticks: Arc<Vec<Tick>>,
    ) -> Self {
        self.backfill_range = Some((from, to));
        self.backfill_ticks = Some(ticks);
        self
    }

    #[cfg(test)]
    fn with_breaker(mut self, breaker: ClickHouseBreaker) -> Self {
        self.ch_breaker = Arc::new(breaker);
        self
    }

    fn cache_get(&self, key: &ProfileCacheKey, ttl: Duration) -> Option<OutboundPayload> {
        let cache = self.profile_cache.lock().ok()?;
        if let Some(entry) = cache.get(key) {
            if entry.cached_at.elapsed() < ttl {
                return Some(entry.payload.clone());
            }
        }
        None
    }

    /// Cache lookup that ignores the TTL, used while the breaker is open.
    fn cache_get_stale(&self, key: &ProfileCacheKey) -> Option<OutboundPayload> {
        let cache = self.profile_cache.lock().ok()?;
        cache.get(key).map(|entry| entry.payload.clone())
    }

    /// Record the provider outcome on the breaker, then map the error.
    fn observe_provider<V>(
        &self,
        result: Result<V, GatewayDataError>,
    ) -> Result<V, GatewayServiceError> {
        match result {
            Ok(value) => {
                self.ch_breaker.record_success();
                Ok(value)
            }
            Err(error) => {
                self.ch_breaker.record_failure();
                Err(map_gateway_data_error(error))
            }
        }
    }

    /// Breaker-open path: the cached payload marked `clickhouse_unavailable_cached`, else a typed
    /// `Unavailable` error.
    fn serve_profile_while_breaker_open(
        &self,
        cache_key: &ProfileCacheKey,
        op: &str,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        if let Some(mut cached) = self.cache_get_stale(cache_key) {
            mark_cached_payload_degraded(&mut cached);
            log::warn!(
                "clickhouse breaker open: serving cached {op} payload marked {DEGRADED_CLICKHOUSE_UNAVAILABLE_CACHED}"
            );
            log_profile_telemetry(op, true, "open", 0);
            return Ok(cached);
        }
        log_profile_telemetry(op, false, "open", 0);
        Err(GatewayServiceError::Data(GatewayDataError::Unavailable))
    }

    fn cache_set(&self, key: ProfileCacheKey, payload: OutboundPayload) {
        if let Ok(mut cache) = self.profile_cache.lock() {
            if cache.len() >= PROFILE_CACHE_MAX_ENTRIES {
                if let Some(oldest_key) = cache
                    .iter()
                    .min_by_key(|(_, e)| e.cached_at)
                    .map(|(k, _)| k.clone())
                {
                    cache.remove(&oldest_key);
                }
            }
            if !matches!(payload, OutboundPayload::Error(_)) {
                cache.insert(
                    key,
                    ProfileCacheEntry {
                        payload,
                        cached_at: Instant::now(),
                    },
                );
            }
        }
    }
}

impl<B, T, R, A> GatewayService<B, T, R, A>
where
    B: HistoricalBarProvider,
    T: HistoricalTickProvider,
    R: ReplayRangeProvider,
    A: AggregatedProfileProvider,
{
    pub async fn handle_historical_bars(
        &self,
        request: SymbolRangeRequest,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        let requested_symbol = request.symbol.clone();
        let query = HistoricalBarQuery {
            symbol: parse_symbol(request.symbol)?,
            from_timestamp: parse_optional_timestamp(request.from_timestamp)?,
            to_timestamp: parse_optional_timestamp(request.to_timestamp)?,
            resolution: request.resolution.clone(),
        };
        query.validate().map_err(map_gateway_data_error)?;

        let bars = self
            .historical_bars
            .load_bars(query)
            .await
            .map_err(map_gateway_data_error)?;

        let symbol = bars
            .first()
            .map(|bar| bar.symbol.as_str().to_owned())
            .unwrap_or(requested_symbol);

        Ok(OutboundPayload::HistoricalBars(HistoricalBarsResponse {
            symbol,
            bars: bars.into_iter().map(historical_bar_view).collect(),
        }))
    }

    pub async fn handle_replay_range(
        &self,
        request: ReplayRangeRequest,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        let symbol = parse_symbol(request.symbol)?;
        let replay_key = parse_replay_key(request.replay_key)?;
        let session_range = SessionRange {
            session_key: SessionKey::new("replay").expect("static session key"),
            from_timestamp: parse_timestamp(request.from_timestamp)?,
            to_timestamp: parse_timestamp(request.to_timestamp)?,
        };
        if session_range.from_timestamp > session_range.to_timestamp {
            return Err(GatewayServiceError::InvalidRange);
        }

        let range = self
            .replay_ranges
            .load_replay_range(ReplayRangeQuery {
                symbol: symbol.clone(),
                replay_key,
                session_range: session_range.clone(),
            })
            .await
            .map_err(map_gateway_data_error)?;

        match range {
            ReplayRangeLoadResult::Found(found_range) => Ok(OutboundPayload::ReplayRange(
                replay_range_view(&symbol, &found_range),
            )),
            ReplayRangeLoadResult::NotFound => Ok(replay_not_found_payload()),
        }
    }

    pub async fn handle_session_profile(
        &self,
        request: SessionProfileRequest,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        let symbol = parse_symbol(request.symbol)?;
        let from_timestamp = parse_timestamp(request.from_timestamp)?;
        let to_timestamp = parse_timestamp(request.to_timestamp)?;
        if from_timestamp > to_timestamp {
            return Err(GatewayServiceError::InvalidRange);
        }

        let cache_key = ProfileCacheKey {
            symbol: symbol.as_str().to_owned(),
            from_timestamp: from_timestamp.0,
            to_timestamp: to_timestamp.0,
            profile_type: "session".to_owned(),
            profile_subtype: request.profile_type.clone(),
        };

        let breaker_state = self.ch_breaker.check();
        if breaker_state == BreakerState::Open {
            return self.serve_profile_while_breaker_open(&cache_key, "session_profile");
        }

        if breaker_state == BreakerState::Closed {
            if let Some(cached) = self.cache_get(&cache_key, PROFILE_CACHE_TTL_SESSION) {
                log::info!("cache hit for session profile {cache_key:?}");
                log_profile_telemetry("session_profile", true, self.ch_breaker.state_str(), 0);
                return Ok(cached);
            }
        }

        let from_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(from_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let to_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(to_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;

        let tick_size = market_types::Price::from_f64(0.1).map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let profile_config = ProfileBuildConfig {
            price_grouping: request.price_grouping,
            tick_size,
            tpo_bucket_minutes: request.tpo_bucket_minutes,
        };
        let value_area = ValueAreaConfig {
            value_area_pct: request.value_area_pct,
        };
        let profile_type = parse_profile_type(request.profile_type)?;

        let mut profile_source_mode = "raw";
        let mut profile_source_name = "historical_tick_provider";
        let mut profile_degraded_reason: Option<String> = None;
        let mut ch_query_ms: u64 = 0;
        // latest tick time for freshness; `to_timestamp` lies in the future during a live session
        let mut latest_event_ts: Option<EventTimestamp> = None;

        let profile = if let Some(agg) = &self.aggregated_profile {
            profile_source_mode = "pre_agg";
            profile_source_name = "aggregated_profile_provider";
            let session_date = format!(
                "{:04}-{:02}-{:02}",
                from_datetime.year(),
                from_datetime.month(),
                from_datetime.day()
            );
            let session_date_clone = session_date.clone();
            let agg_query = AggregatedProfileQuery {
                symbol: symbol.clone(),
                from_timestamp: Some(from_timestamp),
                to_timestamp: Some(to_timestamp),
                price_grouping: request.price_grouping,
                tick_size,
                tpo_bucket_minutes: request.tpo_bucket_minutes,
                session_date: Some(session_date),
            };
            let query_started = Instant::now();
            let agg_result = agg.load_aggregated_profile(agg_query).await;
            ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
            let agg_data = self.observe_provider(agg_result)?;
            let mut profile = build_session_profile_from_aggregated(
                symbol.as_str(),
                from_datetime,
                to_datetime,
                &agg_data,
                profile_type,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?;

            // empty pre-aggregated TPO: rebuild TPO from raw ticks, keep the aggregated volume
            let tpo_empty = profile
                .tpo_profile
                .as_ref()
                .is_none_or(|tp| tp.rows.is_empty());
            if tpo_empty && matches!(profile_type, ProfileType::Tpo | ProfileType::Both) {
                log::info!(
                    "pre-agg TPO empty for session_date={}, falling back to raw-ticks TPO",
                    session_date_clone
                );
                let query = HistoricalTickQuery {
                    symbol: symbol.clone(),
                    from_timestamp: Some(from_timestamp),
                    to_timestamp: Some(to_timestamp),
                };
                if query.validate().is_ok() {
                    let query_started = Instant::now();
                    let ticks_result = self.historical_ticks.load_ticks(query).await;
                    ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
                    if ticks_result.is_ok() {
                        self.ch_breaker.record_success();
                    } else {
                        self.ch_breaker.record_failure();
                    }
                    if let Ok(ticks) = ticks_result {
                        let engine_ticks: Vec<EngineTick> = ticks
                            .into_iter()
                            .filter_map(|t| engine_tick_from_tick(t).ok())
                            .collect();
                        if !engine_ticks.is_empty() {
                            latest_event_ts = engine_ticks.iter().map(|t| t.timestamp).max();
                            if let Ok(tpo) = market_profile_engine::build_tpo_profile_from_ticks(
                                &engine_ticks,
                                profile_config.tick_size,
                                profile_config.price_grouping,
                                value_area.value_area_pct,
                                profile_config.tpo_bucket_minutes,
                            ) {
                                profile.tpo_profile = Some(tpo);
                                profile_source_mode = "mixed";
                                profile_degraded_reason =
                                    Some("pre_agg_tpo_empty_raw_tpo_fallback".to_owned());
                                log::info!(
                                    "TPO fallback: built {} TPO rows from {} raw ticks",
                                    profile.tpo_profile.as_ref().map_or(0, |t| t.rows.len()),
                                    engine_ticks.len(),
                                );
                            }
                        }
                    }
                }
            }
            profile
        } else {
            // use the connect-time backfill ticks only when they cover the whole request range
            let cached_in_range = self.backfill_ticks.as_ref().and_then(|ticks| {
                let (cached_from, cached_to) = self.backfill_range?;
                (from_timestamp >= cached_from && to_timestamp <= cached_to)
                    .then(|| Arc::clone(ticks))
            });

            let engine_ticks = if let Some(cached) = cached_in_range {
                profile_source_name = "backfill_cache";
                cached
                    .iter()
                    .filter(|t| t.timestamp >= from_timestamp && t.timestamp <= to_timestamp)
                    .cloned()
                    .map(engine_tick_from_tick)
                    .collect::<Result<Vec<_>, _>>()?
            } else {
                let query = HistoricalTickQuery {
                    symbol: symbol.clone(),
                    from_timestamp: Some(from_timestamp),
                    to_timestamp: Some(to_timestamp),
                };
                query.validate().map_err(map_gateway_data_error)?;

                let query_started = Instant::now();
                let ticks_result = self.historical_ticks.load_ticks(query).await;
                ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
                let ticks = self.observe_provider(ticks_result)?;
                ticks
                    .into_iter()
                    .map(engine_tick_from_tick)
                    .collect::<Result<Vec<_>, _>>()?
            };
            latest_event_ts = engine_ticks.iter().map(|t| t.timestamp).max();
            build_session_profile(
                symbol.as_str(),
                from_datetime,
                to_datetime,
                &engine_ticks,
                profile_type,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?
        };

        let row_count = profile_row_count(
            profile
                .volume_profile
                .as_ref()
                .map(|summary| summary.rows.len()),
            profile
                .tpo_profile
                .as_ref()
                .map(|summary| summary.rows.len()),
        );
        let mut view = build_session_profile_view(&profile);
        view.source = Some(profile_source_meta(
            profile_source_mode,
            profile_source_name,
            "unknown",
            latest_event_ts,
            row_count,
            profile_degraded_reason,
        ));
        log_profile_telemetry(
            "session_profile",
            false,
            self.ch_breaker.state_str(),
            ch_query_ms,
        );
        let result = Ok(OutboundPayload::SessionProfile(view));
        if let Ok(ref payload) = result {
            self.cache_set(cache_key, payload.clone());
        }
        result
    }

    /// Compute the 80% Rule session bias (SIG-001 tier 2); `None` when the rule does not apply.
    ///
    /// `day_start_ts` is the session's 09:00 ICT epoch second; the prior value area is searched for
    /// up to `PRIOR_VA_MAX_LOOKBACK_DAYS` days back. `Err(Unavailable)` is retryable.
    pub async fn compute_session_bias(
        &self,
        symbol_str: &str,
        day_start_ts: i64,
    ) -> Result<Option<SessionBiasData>, GatewayServiceError> {
        use market_profile_engine::eighty_percent::{
            check_eighty_percent_rule, BlockOhlc, EightyPercentOutcome,
        };
        const BLOCK_SECONDS: i64 = 900;
        const BLOCK_COUNT: usize = 2;
        // 09:00-15:00 ICT covers the whole session, ATC included
        const PRIOR_SESSION_SPAN_SECONDS: i64 = 6 * 3600;

        // 1. early-session blocks from raw ticks
        let symbol = parse_symbol(symbol_str.to_owned())?;
        let blocks_query = HistoricalTickQuery {
            symbol: symbol.clone(),
            from_timestamp: Some(parse_timestamp(day_start_ts)?),
            to_timestamp: Some(parse_timestamp(
                day_start_ts + BLOCK_SECONDS * BLOCK_COUNT as i64,
            )?),
        };
        blocks_query.validate().map_err(map_gateway_data_error)?;
        let ticks_result = self.historical_ticks.load_ticks(blocks_query).await;
        let mut ticks = self.observe_provider(ticks_result)?;
        ticks.sort_by_key(|t| t.timestamp.0);

        let mut blocks: [Option<BlockOhlc>; BLOCK_COUNT] = [None; BLOCK_COUNT];
        let mut session_open = f64::NAN;
        for tick in &ticks {
            let price = tick.price.as_f64();
            let idx = ((tick.timestamp.0 - day_start_ts) / BLOCK_SECONDS).clamp(0, 1) as usize;
            if session_open.is_nan() {
                session_open = price;
            }
            match blocks[idx].as_mut() {
                Some(b) => {
                    b.high = b.high.max(price);
                    b.low = b.low.min(price);
                    b.close = price;
                }
                None => {
                    blocks[idx] = Some(BlockOhlc {
                        open: price,
                        high: price,
                        low: price,
                        close: price,
                    });
                }
            }
        }
        let blocks: Vec<BlockOhlc> = blocks.iter().filter_map(|b| *b).collect();
        if blocks.len() < BLOCK_COUNT {
            log::info!(
                "session_bias: only {}/{} early blocks with ticks — no bias",
                blocks.len(),
                BLOCK_COUNT
            );
            return Ok(None);
        }

        // 2. prior value area through the session-profile path, so the cache and breaker apply
        let mut prior_va: Option<(f64, f64)> = None;
        for offset in 1..=PRIOR_VA_MAX_LOOKBACK_DAYS {
            let from = day_start_ts - offset * 86_400;
            let request = SessionProfileRequest {
                symbol: symbol_str.to_owned(),
                from_timestamp: from,
                to_timestamp: from + PRIOR_SESSION_SPAN_SECONDS,
                profile_type: "volume".to_owned(),
                price_grouping: 1,
                tpo_bucket_minutes: 30,
                value_area_pct: 0.7,
            };
            match self.handle_session_profile(request).await {
                Ok(OutboundPayload::SessionProfile(view)) => {
                    if let Some(vp) = view.volume_profile {
                        if !vp.rows.is_empty() {
                            if let (Some(vah), Some(val)) = (vp.vah, vp.val) {
                                prior_va = Some((vah, val));
                                break;
                            }
                        }
                    }
                }
                Ok(_) => {}
                // data layer down: propagate so the caller can retry
                Err(GatewayServiceError::Data(GatewayDataError::Unavailable)) => {
                    return Err(GatewayServiceError::Data(GatewayDataError::Unavailable));
                }
                // no usable data that day: try the day before
                Err(error) => {
                    log::debug!("session_bias: prior day offset {offset} unusable: {error}");
                }
            }
        }
        let Some((prior_vah, prior_val)) = prior_va else {
            log::info!(
                "session_bias: no usable prior VA within {PRIOR_VA_MAX_LOOKBACK_DAYS} days — no bias"
            );
            return Ok(None);
        };

        // 3. the 80% Rule check
        match check_eighty_percent_rule(prior_vah, prior_val, session_open, &blocks, 0.001) {
            EightyPercentOutcome::Bias {
                direction,
                target,
                prior_vah,
                prior_val,
                confidence,
            } => Ok(Some(SessionBiasData {
                bullish: direction == market_profile_engine::eighty_percent::BiasDirection::Bullish,
                target,
                prior_vah,
                prior_val,
                confidence,
            })),
            EightyPercentOutcome::NoSignal { reason } => {
                log::info!("session_bias: 80% rule not confirmed: {reason}");
                Ok(None)
            }
        }
    }

    pub async fn handle_composite_profile(
        &self,
        request: CompositeProfileRequest,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        let symbol = parse_symbol(request.symbol)?;
        let from_timestamp = parse_timestamp(request.from_timestamp)?;
        let to_timestamp = parse_timestamp(request.to_timestamp)?;
        if from_timestamp > to_timestamp {
            return Err(GatewayServiceError::InvalidRange);
        }

        let cache_key = ProfileCacheKey {
            symbol: symbol.as_str().to_owned(),
            from_timestamp: from_timestamp.0,
            to_timestamp: to_timestamp.0,
            profile_type: "composite".to_owned(),
            profile_subtype: String::new(),
        };

        let breaker_state = self.ch_breaker.check();
        if breaker_state == BreakerState::Open {
            return self.serve_profile_while_breaker_open(&cache_key, "composite_profile");
        }

        if breaker_state == BreakerState::Closed {
            if let Some(cached) = self.cache_get(&cache_key, PROFILE_CACHE_TTL_COMPOSITE) {
                log::info!("cache hit for composite profile {cache_key:?}");
                log_profile_telemetry("composite_profile", true, self.ch_breaker.state_str(), 0);
                return Ok(cached);
            }
        }

        let from_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(from_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let to_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(to_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;

        let tick_size = market_types::Price::from_f64(0.1).map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let profile_config = ProfileBuildConfig {
            price_grouping: request.price_grouping,
            tick_size,
            tpo_bucket_minutes: request.tpo_bucket_minutes,
        };
        let value_area = ValueAreaConfig {
            value_area_pct: request.value_area_pct,
        };

        let mut ch_query_ms: u64 = 0;
        let profile = if let Some(agg) = &self.aggregated_profile {
            let agg_query = AggregatedProfileQuery {
                symbol: symbol.clone(),
                from_timestamp: Some(from_timestamp),
                to_timestamp: Some(to_timestamp),
                price_grouping: request.price_grouping,
                tick_size,
                tpo_bucket_minutes: request.tpo_bucket_minutes,
                session_date: None,
            };
            let query_started = Instant::now();
            let agg_result = agg.load_aggregated_profile(agg_query.clone()).await;
            ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
            let agg_data = self.observe_provider(agg_result)?;
            let query_started = Instant::now();
            let overview_result = agg.load_overview_bars(agg_query).await;
            ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
            let overview_bars = self.observe_provider(overview_result)?;
            build_composite_profile_from_aggregated(
                symbol.as_str(),
                from_datetime,
                to_datetime,
                &agg_data,
                &overview_bars,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?
        } else {
            let query = HistoricalTickQuery {
                symbol: symbol.clone(),
                from_timestamp: Some(from_timestamp),
                to_timestamp: Some(to_timestamp),
            };
            query.validate().map_err(map_gateway_data_error)?;

            let query_started = Instant::now();
            let ticks_result = self.historical_ticks.load_ticks(query).await;
            ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
            let ticks = self.observe_provider(ticks_result)?;
            let engine_ticks = ticks
                .into_iter()
                .map(engine_tick_from_tick)
                .collect::<Result<Vec<_>, _>>()?;
            build_composite_profile(
                symbol.as_str(),
                from_datetime,
                to_datetime,
                &engine_ticks,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?
        };

        let row_count = profile_row_count(
            Some(profile.volume_profile.rows.len()),
            Some(profile.market_profile.rows.len()),
        );
        let mut view = build_composite_profile_view(&profile);
        view.source = Some(profile_source_meta(
            if self.aggregated_profile.is_some() {
                "pre_agg"
            } else {
                "raw"
            },
            if self.aggregated_profile.is_some() {
                "aggregated_profile_provider"
            } else {
                "historical_tick_provider"
            },
            "unknown",
            Some(to_timestamp),
            row_count,
            None,
        ));
        log_profile_telemetry(
            "composite_profile",
            false,
            self.ch_breaker.state_str(),
            ch_query_ms,
        );
        let result = Ok(OutboundPayload::CompositeProfile(view));
        if let Ok(ref payload) = result {
            self.cache_set(cache_key, payload.clone());
        }
        result
    }

    pub async fn handle_context_profile(
        &self,
        request: ContextProfileRequest,
    ) -> Result<OutboundPayload, GatewayServiceError> {
        let symbol = parse_symbol(request.symbol)?;
        let from_timestamp = parse_timestamp(request.from_timestamp)?;
        let to_timestamp = parse_timestamp(request.to_timestamp)?;
        if from_timestamp > to_timestamp {
            return Err(GatewayServiceError::InvalidRange);
        }

        let cache_key = ProfileCacheKey {
            symbol: symbol.as_str().to_owned(),
            from_timestamp: from_timestamp.0,
            to_timestamp: to_timestamp.0,
            profile_type: "context".to_owned(),
            profile_subtype: request.context_mode.clone(),
        };

        let breaker_state = self.ch_breaker.check();
        if breaker_state == BreakerState::Open {
            return self.serve_profile_while_breaker_open(&cache_key, "context_profile");
        }

        if breaker_state == BreakerState::Closed {
            if let Some(cached) = self.cache_get(&cache_key, PROFILE_CACHE_TTL_CONTEXT) {
                log::info!("cache hit for context profile {cache_key:?}");
                log_profile_telemetry("context_profile", true, self.ch_breaker.state_str(), 0);
                return Ok(cached);
            }
        }

        let from_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(from_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let to_datetime = extract_tick_datetime(&TickTimeInput {
            datetime_iso: None,
            timestamp: Some(to_timestamp),
        })
        .map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;

        let tick_size = market_types::Price::from_f64(0.1).map_err(|error| {
            GatewayServiceError::Data(GatewayDataError::Internal(error.to_string()))
        })?;
        let profile_config = ProfileBuildConfig {
            price_grouping: request.price_grouping,
            tick_size,
            tpo_bucket_minutes: request.tpo_bucket_minutes,
        };
        let value_area = ValueAreaConfig {
            value_area_pct: request.value_area_pct,
        };

        let mut ch_query_ms: u64 = 0;
        let profile = if let Some(agg) = &self.aggregated_profile {
            let needed = request.session_count + request.offset;
            let mut current = to_datetime.date_naive();
            let start = from_datetime.date_naive();
            let mut session_profiles: BTreeMap<String, AggregatedProfileData> = BTreeMap::new();

            loop {
                let date_str = current.format("%Y-%m-%d").to_string();
                let session_dt = current
                    .and_hms_opt(0, 0, 0)
                    .unwrap()
                    .and_local_timezone(FixedOffset::east_opt(7 * 3600).unwrap())
                    .unwrap();
                let (s_start, s_end) = intraday_session_bounds(session_dt).map_err(|e| {
                    GatewayServiceError::Data(GatewayDataError::Internal(e.to_string()))
                })?;
                let agg_query = AggregatedProfileQuery {
                    symbol: symbol.clone(),
                    from_timestamp: Some(EventTimestamp(s_start.timestamp())),
                    to_timestamp: Some(EventTimestamp(s_end.timestamp())),
                    price_grouping: request.price_grouping,
                    tick_size,
                    tpo_bucket_minutes: request.tpo_bucket_minutes,
                    session_date: Some(date_str.clone()),
                };
                let query_started = Instant::now();
                let agg_result = agg.load_aggregated_profile(agg_query).await;
                ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
                let data = self.observe_provider(agg_result)?;
                session_profiles.insert(date_str, data);

                if current == start || session_profiles.len() >= needed {
                    break;
                }
                current = current.pred_opt().unwrap_or(current);
            }

            build_context_profile_from_aggregated(
                symbol.as_str(),
                from_datetime,
                to_datetime,
                session_profiles,
                request.session_count,
                parse_context_mode(request.context_mode)?,
                request.offset,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?
        } else {
            let query = HistoricalTickQuery {
                symbol: symbol.clone(),
                from_timestamp: Some(from_timestamp),
                to_timestamp: Some(to_timestamp),
            };
            query.validate().map_err(map_gateway_data_error)?;

            let query_started = Instant::now();
            let ticks_result = self.historical_ticks.load_ticks(query).await;
            ch_query_ms = ch_query_ms.saturating_add(elapsed_ms(query_started));
            let ticks = self.observe_provider(ticks_result)?;
            let engine_ticks = ticks
                .into_iter()
                .map(engine_tick_from_tick)
                .collect::<Result<Vec<_>, _>>()?;
            build_context_profile(
                symbol.as_str(),
                &engine_ticks,
                request.session_count,
                parse_context_mode(request.context_mode)?,
                request.offset,
                profile_config,
                value_area,
            )
            .map_err(map_profile_error)?
        };

        let row_count = profile
            .sessions
            .iter()
            .map(|session| session.market_profile.rows.len() + session.volume_profile.rows.len())
            .sum();
        let mut view = build_context_profile_view(&profile);
        view.source = Some(profile_source_meta(
            if self.aggregated_profile.is_some() {
                "pre_agg"
            } else {
                "raw"
            },
            if self.aggregated_profile.is_some() {
                "aggregated_profile_provider"
            } else {
                "historical_tick_provider"
            },
            "unknown",
            Some(to_timestamp),
            row_count,
            None,
        ));
        log_profile_telemetry(
            "context_profile",
            false,
            self.ch_breaker.state_str(),
            ch_query_ms,
        );
        let result = Ok(OutboundPayload::ContextProfile(view));
        if let Ok(ref payload) = result {
            self.cache_set(cache_key, payload.clone());
        }
        result
    }
}

#[cfg(test)]
mod tests;
