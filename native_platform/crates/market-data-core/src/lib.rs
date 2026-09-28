//! Market-data provider and broker-executor traits, with their query, result and error types.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use async_trait::async_trait;
use market_types::{
    EventTimestamp, OhlcBar, Price, QuoteSnapshot, ReplayRangeKey, SessionKey, SessionRange,
    Symbol, Tick, Volume,
};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum GatewayDataError {
    #[error("provider is unavailable")]
    Unavailable,
    #[error("requested range is invalid")]
    InvalidRange,
    #[error("symbol is not supported")]
    UnsupportedSymbol,
    #[error("stream ended")]
    StreamEnded,
    #[error("internal provider error: {0}")]
    Internal(String),
    #[error("clickhouse table not found: {0}")]
    TableNotFound(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoricalTickQuery {
    pub symbol: Symbol,
    pub from_timestamp: Option<EventTimestamp>,
    pub to_timestamp: Option<EventTimestamp>,
}

impl HistoricalTickQuery {
    pub fn validate(&self) -> Result<(), GatewayDataError> {
        if matches!((self.from_timestamp, self.to_timestamp), (Some(from), Some(to)) if from > to) {
            return Err(GatewayDataError::InvalidRange);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoricalBarQuery {
    pub symbol: Symbol,
    pub from_timestamp: Option<EventTimestamp>,
    pub to_timestamp: Option<EventTimestamp>,
    pub resolution: Option<String>,
}

impl HistoricalBarQuery {
    pub fn validate(&self) -> Result<(), GatewayDataError> {
        if matches!((self.from_timestamp, self.to_timestamp), (Some(from), Some(to)) if from > to) {
            return Err(GatewayDataError::InvalidRange);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayRangeQuery {
    pub symbol: Symbol,
    pub replay_key: ReplayRangeKey,
    pub session_range: SessionRange,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplaySequenceMode {
    SourceAuthoritative,
    TimestampDegraded,
}

impl ReplaySequenceMode {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SourceAuthoritative => "source_authoritative",
            Self::TimestampDegraded => "timestamp_degraded",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayRangeMeta {
    pub session_key: SessionKey,
    pub from_timestamp: EventTimestamp,
    pub to_timestamp: EventTimestamp,
    pub tick_count: usize,
    pub sequence_mode: ReplaySequenceMode,
}

impl ReplayRangeMeta {
    #[must_use]
    pub fn session_range(&self) -> SessionRange {
        SessionRange {
            session_key: self.session_key.clone(),
            from_timestamp: self.from_timestamp,
            to_timestamp: self.to_timestamp,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayTickData {
    pub tick: Tick,
    pub event_sequence: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayRangeData {
    pub meta: ReplayRangeMeta,
    pub ticks: Vec<ReplayTickData>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplayRangeLoadResult {
    NotFound,
    Found(ReplayRangeData),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiveFeedKind {
    Tick,
    Quote,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveEvent<T> {
    pub sequence: u64,
    pub payload: T,
    pub source_pickup_unix_ns: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct ShutdownSignal {
    inner: Arc<AtomicBool>,
}

impl Default for ShutdownSignal {
    fn default() -> Self {
        Self {
            inner: Arc::new(AtomicBool::new(false)),
        }
    }
}

impl ShutdownSignal {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn trigger(&self) {
        self.inner.store(true, Ordering::SeqCst);
    }

    #[must_use]
    pub fn is_triggered(&self) -> bool {
        self.inner.load(Ordering::SeqCst)
    }
}

#[async_trait]
pub trait HistoricalTickProvider: Send + Sync {
    async fn load_ticks(&self, query: HistoricalTickQuery) -> Result<Vec<Tick>, GatewayDataError>;
}

#[async_trait]
pub trait HistoricalBarProvider: Send + Sync {
    async fn load_bars(&self, query: HistoricalBarQuery) -> Result<Vec<OhlcBar>, GatewayDataError>;
}

#[async_trait]
pub trait ReplayRangeProvider: Send + Sync {
    async fn load_replay_range(
        &self,
        query: ReplayRangeQuery,
    ) -> Result<ReplayRangeLoadResult, GatewayDataError>;
}

#[async_trait]
pub trait LiveTickProvider: Send + Sync {
    async fn next_tick(
        &mut self,
        shutdown: &ShutdownSignal,
    ) -> Result<Option<LiveEvent<Tick>>, GatewayDataError>;
}

#[async_trait]
pub trait LiveQuoteProvider: Send + Sync {
    async fn next_quote(
        &mut self,
        shutdown: &ShutdownSignal,
    ) -> Result<Option<LiveEvent<QuoteSnapshot>>, GatewayDataError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatedVolumeRow {
    pub price: Price,
    pub total_volume: Volume,
    pub buy_volume: Volume,
    pub sell_volume: Volume,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatedTpoRow {
    pub price_level: Price,
    pub tpo_count: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatedProfileData {
    pub volume_rows: Vec<AggregatedVolumeRow>,
    pub tpo_rows: Vec<AggregatedTpoRow>,
    pub session_high: Option<Price>,
    pub session_low: Option<Price>,
    pub session_open: Option<Price>,
    /// Initial-balance high and low, 09:00-10:00 ICT as in `market_time`.
    ///
    /// Aggregated rows carry no time, so the provider supplies these; `None` leaves single prints
    /// and excess empty, as `AdvancedReferences::ib_available` reports.
    pub ib_high: Option<Price>,
    pub ib_low: Option<Price>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatedProfileQuery {
    pub symbol: Symbol,
    pub from_timestamp: Option<EventTimestamp>,
    pub to_timestamp: Option<EventTimestamp>,
    pub price_grouping: i32,
    pub tick_size: Price,
    pub tpo_bucket_minutes: i64,
    pub session_date: Option<String>,
}

impl AggregatedProfileQuery {
    pub fn validate(&self) -> Result<(), GatewayDataError> {
        if matches!((self.from_timestamp, self.to_timestamp), (Some(from), Some(to)) if from > to) {
            return Err(GatewayDataError::InvalidRange);
        }
        Ok(())
    }
}

#[async_trait]
pub trait AggregatedProfileProvider: Send + Sync {
    async fn load_aggregated_profile(
        &self,
        query: AggregatedProfileQuery,
    ) -> Result<AggregatedProfileData, GatewayDataError>;

    async fn load_overview_bars(
        &self,
        query: AggregatedProfileQuery,
    ) -> Result<Vec<OhlcBar>, GatewayDataError>;
}

// --- broker executor: paper and live order execution ---

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ExecutorError {
    #[error("insufficient buying power")]
    InsufficientBuyingPower,
    #[error("invalid order: {0}")]
    InvalidOrder(String),
    #[error("order not found: {0}")]
    OrderNotFound(String),
    #[error("symbol not supported: {0}")]
    UnsupportedSymbol(String),
    #[error("no price data for symbol: {0}")]
    NoPriceData(String),
    #[error("internal executor error: {0}")]
    Internal(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderType {
    Market,
    Limit,
    /// At-the-open auction (rejected during the continuous session).
    Ato,
    /// At-the-close auction (rests until the close auction).
    Atc,
    /// Market-or-kill (fill immediately or cancel).
    Mok,
    /// Match-and-kill (fill what it can immediately, cancel the rest).
    Mak,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderStatus {
    Pending,
    Filled,
    PartiallyFilled,
    Canceled,
    Rejected { reason: String },
}

#[derive(Debug, Clone)]
pub struct PlaceOrderRequest {
    pub symbol: Symbol,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub quantity: Volume,
    pub price: Option<Price>,
    pub client_order_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct OrderResult {
    pub order_id: String,
    pub symbol: Symbol,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub quantity: Volume,
    pub filled_quantity: Volume,
    pub price: Option<Price>,
    pub avg_fill_price: Option<Price>,
    pub status: OrderStatus,
    pub created_at: EventTimestamp,
    pub updated_at: EventTimestamp,
}

#[derive(Debug, Clone)]
pub struct PositionSnapshot {
    pub symbol: Symbol,
    pub net_quantity: i64,
    pub avg_entry_price: Price,
    pub current_price: Price,
    pub unrealized_pnl: f64,
    pub realized_pnl: f64,
}

#[derive(Debug, Clone)]
pub struct AccountSummary {
    pub balance: f64,
    pub equity: f64,
    pub unrealized_pnl: f64,
    pub realized_pnl: f64,
    pub margin_used: f64,
    pub buying_power: f64,
}

#[async_trait]
pub trait BrokerExecutor: Send + Sync {
    async fn place_order(&self, request: PlaceOrderRequest) -> Result<OrderResult, ExecutorError>;
    async fn cancel_order(&self, order_id: &str) -> Result<OrderResult, ExecutorError>;
    async fn list_orders(&self, symbol: Option<&str>) -> Result<Vec<OrderResult>, ExecutorError>;
    async fn get_positions(
        &self,
        symbol: Option<&str>,
    ) -> Result<Vec<PositionSnapshot>, ExecutorError>;
    async fn get_account(&self) -> Result<AccountSummary, ExecutorError>;

    /// Flatten the position in `symbol` with an opposite-side market order for the net quantity.
    ///
    /// The quantity comes from `get_positions`, so a stale client view cannot over- or
    /// under-flatten; a flat position returns `InvalidOrder` without placing an order.
    async fn close_position(&self, symbol: &str) -> Result<OrderResult, ExecutorError> {
        let net = self
            .get_positions(Some(symbol))
            .await?
            .into_iter()
            .find(|position| position.symbol.as_str() == symbol)
            .map(|position| position.net_quantity)
            .unwrap_or(0);
        if net == 0 {
            return Err(ExecutorError::InvalidOrder(format!(
                "no open position for {symbol}"
            )));
        }
        let side = if net > 0 {
            OrderSide::Sell
        } else {
            OrderSide::Buy
        };
        let quantity = Volume::new(net.abs())
            .map_err(|_| ExecutorError::InvalidOrder(format!("invalid net quantity {net}")))?;
        let symbol_typed = Symbol::new(symbol)
            .map_err(|_| ExecutorError::UnsupportedSymbol(symbol.to_string()))?;
        self.place_order(PlaceOrderRequest {
            symbol: symbol_typed,
            side,
            order_type: OrderType::Market,
            quantity,
            price: None,
            client_order_id: Some(format!("flatten-{symbol}")),
        })
        .await
    }

    /// Reverse the position in `symbol` with one opposite-side market order for `2 × |net|`.
    ///
    /// The net quantity is read server-side; a flat position returns `InvalidOrder`.
    async fn reverse_position(&self, symbol: &str) -> Result<OrderResult, ExecutorError> {
        let net = self
            .get_positions(Some(symbol))
            .await?
            .into_iter()
            .find(|position| position.symbol.as_str() == symbol)
            .map(|position| position.net_quantity)
            .unwrap_or(0);
        if net == 0 {
            return Err(ExecutorError::InvalidOrder(format!(
                "no open position for {symbol}"
            )));
        }
        let side = if net > 0 {
            OrderSide::Sell
        } else {
            OrderSide::Buy
        };
        let quantity = Volume::new(net.abs() * 2)
            .map_err(|_| ExecutorError::InvalidOrder(format!("invalid net quantity {net}")))?;
        let symbol_typed = Symbol::new(symbol)
            .map_err(|_| ExecutorError::UnsupportedSymbol(symbol.to_string()))?;
        self.place_order(PlaceOrderRequest {
            symbol: symbol_typed,
            side,
            order_type: OrderType::Market,
            quantity,
            price: None,
            client_order_id: Some(format!("reverse-{symbol}")),
        })
        .await
    }

    /// Modify a working order by cancelling `order_id` and then placing `new`; not atomic.
    ///
    /// Size `new.quantity` against the remaining quantity so a partly filled order is not
    /// over-filled. Returns the new order's result.
    async fn modify_order(
        &self,
        order_id: &str,
        new: PlaceOrderRequest,
    ) -> Result<OrderResult, ExecutorError> {
        self.cancel_order(order_id).await?;
        self.place_order(new).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use market_types::{ParseError, Price, SessionKey, Volume};

    fn symbol() -> Symbol {
        Symbol::new("VN30F1M").expect("symbol")
    }

    #[test]
    fn historical_tick_query_rejects_reversed_ranges() {
        let query = HistoricalTickQuery {
            symbol: symbol(),
            from_timestamp: Some(EventTimestamp::new(20).expect("from")),
            to_timestamp: Some(EventTimestamp::new(10).expect("to")),
        };

        assert_eq!(query.validate(), Err(GatewayDataError::InvalidRange));
    }

    #[test]
    fn historical_bar_query_accepts_open_ranges() {
        let query = HistoricalBarQuery {
            symbol: symbol(),
            from_timestamp: Some(EventTimestamp::new(10).expect("from")),
            to_timestamp: None,
            resolution: None,
        };

        assert_eq!(query.validate(), Ok(()));
    }

    #[test]
    fn shutdown_signal_is_shared_and_triggerable() {
        let signal = ShutdownSignal::new();
        let clone = signal.clone();

        assert!(!signal.is_triggered());
        clone.trigger();
        assert!(signal.is_triggered());
    }

    #[test]
    fn replay_range_query_uses_existing_domain_types() {
        let query = ReplayRangeQuery {
            symbol: symbol(),
            replay_key: ReplayRangeKey::new("session-replay").expect("replay key"),
            session_range: SessionRange {
                session_key: SessionKey::new("2026-04-23-am").expect("session key"),
                from_timestamp: EventTimestamp::new(1).expect("from"),
                to_timestamp: EventTimestamp::new(2).expect("to"),
            },
        };

        assert_eq!(query.replay_key.as_str(), "session-replay");
        assert_eq!(query.session_range.to_timestamp.0, 2);
    }

    #[test]
    fn replay_range_meta_builds_session_range_and_tracks_tick_count() {
        let meta = ReplayRangeMeta {
            session_key: SessionKey::new("2026-04-23-am").expect("session key"),
            from_timestamp: EventTimestamp::new(10).expect("from"),
            to_timestamp: EventTimestamp::new(20).expect("to"),
            tick_count: 2,
            sequence_mode: ReplaySequenceMode::SourceAuthoritative,
        };

        let range = meta.session_range();
        assert_eq!(range.session_key.as_str(), "2026-04-23-am");
        assert_eq!(range.from_timestamp.0, 10);
        assert_eq!(range.to_timestamp.0, 20);
    }

    #[test]
    fn replay_range_load_result_can_hold_ticks_and_metadata() {
        let result = ReplayRangeLoadResult::Found(ReplayRangeData {
            meta: ReplayRangeMeta {
                session_key: SessionKey::new("2026-04-23-am").expect("session key"),
                from_timestamp: EventTimestamp::new(1).expect("from"),
                to_timestamp: EventTimestamp::new(2).expect("to"),
                tick_count: 1,
                sequence_mode: ReplaySequenceMode::SourceAuthoritative,
            },
            ticks: vec![ReplayTickData {
                tick: Tick {
                    symbol: symbol(),
                    timestamp: EventTimestamp::new(1).expect("timestamp"),
                    price: Price::from_f64(1250.5).expect("price"),
                    volume: Volume::new(10).expect("volume"),
                    side: None,
                },
                event_sequence: Some(7),
            }],
        });

        match result {
            ReplayRangeLoadResult::Found(data) => {
                assert_eq!(data.meta.tick_count, data.ticks.len());
                assert_eq!(data.ticks[0].tick.symbol.as_str(), "VN30F1M");
            }
            ReplayRangeLoadResult::NotFound => panic!("expected replay data"),
        }
    }

    #[test]
    fn symbol_construction_still_uses_market_types_validation() {
        let error = Symbol::new("").expect_err("empty symbol should fail");
        assert_eq!(error, ParseError::EmptySymbol);
    }
}
