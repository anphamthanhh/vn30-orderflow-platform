//! Gateway-client wire protocol: a 20-byte big-endian [`EnvelopeHeader`] followed by a JSON
//! payload.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use market_dom_viewmodel::{DomBatchView, DomSnapshotView};
use market_profile_viewmodel::{
    CompositeProfileView, ContextProfileView, ReplayRangeView, SessionProfileView,
};
use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u8 = 1;
pub const ENVELOPE_HEADER_LEN: usize = 20;
pub const FLAG_STREAM_END: u8 = 0b0000_0001;

/// Default upper bound on a frame payload accepted from a peer: 16 MiB.
///
/// Stops a corrupt `payload_len` from forcing an allocation of up to 4 GiB. Readers of bulk
/// history responses may pass a larger limit to [`EnvelopeHeader::decode`], servers a smaller one.
pub const MAX_PAYLOAD_LEN: u32 = 16 * 1024 * 1024;

/// Why [`EnvelopeHeader::decode`] rejected a header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeaderError {
    /// `kind` is not a known [`MessageKind`]; the reader should close the stream.
    UnknownKind(u16),
    /// `payload_len` exceeds the reader's limit; close the stream without reading the payload.
    PayloadTooLarge {
        payload_len: u32,
        max_payload_len: u32,
    },
}

impl std::fmt::Display for HeaderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownKind(kind) => write!(f, "unknown message kind {kind}"),
            Self::PayloadTooLarge {
                payload_len,
                max_payload_len,
            } => write!(
                f,
                "payload length {payload_len} exceeds the limit of {max_payload_len} bytes"
            ),
        }
    }
}

impl std::error::Error for HeaderError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameFlags {
    None,
    StreamEnd,
}

impl FrameFlags {
    #[must_use]
    pub const fn bits(self) -> u8 {
        match self {
            Self::None => 0,
            Self::StreamEnd => FLAG_STREAM_END,
        }
    }
}

#[must_use]
pub const fn is_stream_end(flags: u8) -> bool {
    flags & FLAG_STREAM_END != 0
}

/// Message type, sent as the `u16` `kind` field of the header.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u16)]
pub enum MessageKind {
    Hello = 1,
    Error = 2,
    HistoricalBarsRequest = 10,
    HistoricalBarsResponse = 11,
    ReplayRangeRequest = 12,
    ReplayRangeResponse = 13,
    SessionProfileRequest = 14,
    SessionProfileResponse = 15,
    ContextProfileRequest = 16,
    ContextProfileResponse = 17,
    CompositeProfileRequest = 18,
    CompositeProfileResponse = 19,
    DomSnapshot = 30,
    DomBatch = 31,
    LiveTick = 32,
    LiveQuote = 33,
    SubscribeDom = 34,
    SubscribeTicks = 35,
    SubscribeQuotes = 36,
    HistoricalDomRequest = 40,
    HistoricalDomResponse = 41,
    // order execution
    PlaceOrderRequest = 50,
    PlaceOrderResponse = 51,
    CancelOrderRequest = 52,
    CancelOrderResponse = 53,
    ListOrdersRequest = 54,
    ListOrdersResponse = 55,
    GetPositionsRequest = 56,
    GetPositionsResponse = 57,
    GetAccountRequest = 58,
    GetAccountResponse = 59,
    // OTP flow
    RequestOtp = 60,
    RequestOtpResponse = 61,
    SubmitOtp = 62,
    SubmitOtpResponse = 63,
    // position management: flatten, reverse, modify (cancel + replace)
    ClosePositionRequest = 64,
    ClosePositionResponse = 65,
    ReversePositionRequest = 66,
    ReversePositionResponse = 67,
    ModifyOrderRequest = 68,
    ModifyOrderResponse = 69,
    Ack = 90,
    Heartbeat = 91,
    // SIG-001 signal pipeline, server push on the tick lane
    SignalSnapshot = 92,
    SignalEvent = 93,
    // OMS blotter: live push on the tick lane, history on the query lane
    OmsOrderEvent = 94,
    OrderHistoryRequest = 95,
    OrderHistoryResponse = 96,
    // execution statistics from the `oms.*` tables (query lane)
    OrderStatsRequest = 97,
    OrderStatsResponse = 98,
    ClosedTradesRequest = 99,
    ClosedTradesResponse = 100,
    EquityCurveRequest = 101,
    EquityCurveResponse = 102,
    // gateway-side TP/SL/trailing brackets (execution lane)
    AttachBracketRequest = 103,
    AttachBracketResponse = 104,
    CancelBracketRequest = 105,
    CancelBracketResponse = 106,
    ListBracketsRequest = 107,
    ListBracketsResponse = 108,
    // order-flow metrics rebuilt from stored trades (query lane)
    FetchMetricsHistoryRequest = 109,
    FetchMetricsHistoryResponse = 110,
    // per-fill breakdown (query lane)
    FetchOrderFillsRequest = 111,
    FetchOrderFillsResponse = 112,
    // exchange-side TP/SL/trailing; setting it requires an OTP trading token
    ExchangeTpSlGetRequest = 113,
    ExchangeTpSlGetResponse = 114,
    ExchangeTpSlSetRequest = 115,
    ExchangeTpSlSetResponse = 116,
    // auto-trade control (execution lane) and state push (tick lane)
    AutoTradeControlRequest = 117,
    AutoTradeControlResponse = 118,
    AutoTradeCockpit = 119,
}

/// Frame header, encoded as 20 big-endian bytes.
///
/// Byte layout: `version` (1), `flags` (1), `kind` (2), `stream_id` (4), `sequence` (8),
/// `payload_len` (4).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EnvelopeHeader {
    pub version: u8,
    pub flags: u8,
    pub kind: MessageKind,
    pub stream_id: u32,
    pub sequence: u64,
    pub payload_len: u32,
}

impl EnvelopeHeader {
    #[must_use]
    pub fn new(kind: MessageKind, stream_id: u32, sequence: u64, payload_len: u32) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            flags: FrameFlags::None.bits(),
            kind,
            stream_id,
            sequence,
            payload_len,
        }
    }

    #[must_use]
    pub fn with_flags(mut self, flags: FrameFlags) -> Self {
        self.flags = flags.bits();
        self
    }

    #[must_use]
    pub fn encode(self) -> [u8; ENVELOPE_HEADER_LEN] {
        let mut bytes = [0_u8; ENVELOPE_HEADER_LEN];
        bytes[0] = self.version;
        bytes[1] = self.flags;
        bytes[2..4].copy_from_slice(&(self.kind as u16).to_be_bytes());
        bytes[4..8].copy_from_slice(&self.stream_id.to_be_bytes());
        bytes[8..16].copy_from_slice(&self.sequence.to_be_bytes());
        bytes[16..20].copy_from_slice(&self.payload_len.to_be_bytes());
        bytes
    }

    /// Decode a header read from a peer.
    ///
    /// Rejects an unknown `kind` or a `payload_len` above `max_payload_len` (normally
    /// [`MAX_PAYLOAD_LEN`]) before the caller allocates the payload.
    pub fn decode(
        bytes: [u8; ENVELOPE_HEADER_LEN],
        max_payload_len: u32,
    ) -> Result<Self, HeaderError> {
        let raw_kind = u16::from_be_bytes([bytes[2], bytes[3]]);
        let kind = MessageKind::from_u16(raw_kind).ok_or(HeaderError::UnknownKind(raw_kind))?;
        let payload_len = u32::from_be_bytes([bytes[16], bytes[17], bytes[18], bytes[19]]);
        if payload_len > max_payload_len {
            return Err(HeaderError::PayloadTooLarge {
                payload_len,
                max_payload_len,
            });
        }
        Ok(Self {
            version: bytes[0],
            flags: bytes[1],
            kind,
            stream_id: u32::from_be_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]),
            sequence: u64::from_be_bytes([
                bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14],
                bytes[15],
            ]),
            payload_len,
        })
    }
}

impl MessageKind {
    fn from_u16(value: u16) -> Option<Self> {
        Some(match value {
            1 => Self::Hello,
            2 => Self::Error,
            10 => Self::HistoricalBarsRequest,
            11 => Self::HistoricalBarsResponse,
            12 => Self::ReplayRangeRequest,
            13 => Self::ReplayRangeResponse,
            14 => Self::SessionProfileRequest,
            15 => Self::SessionProfileResponse,
            16 => Self::ContextProfileRequest,
            17 => Self::ContextProfileResponse,
            18 => Self::CompositeProfileRequest,
            19 => Self::CompositeProfileResponse,
            30 => Self::DomSnapshot,
            31 => Self::DomBatch,
            32 => Self::LiveTick,
            33 => Self::LiveQuote,
            34 => Self::SubscribeDom,
            35 => Self::SubscribeTicks,
            36 => Self::SubscribeQuotes,
            40 => Self::HistoricalDomRequest,
            41 => Self::HistoricalDomResponse,
            50 => Self::PlaceOrderRequest,
            51 => Self::PlaceOrderResponse,
            52 => Self::CancelOrderRequest,
            53 => Self::CancelOrderResponse,
            54 => Self::ListOrdersRequest,
            55 => Self::ListOrdersResponse,
            56 => Self::GetPositionsRequest,
            57 => Self::GetPositionsResponse,
            58 => Self::GetAccountRequest,
            59 => Self::GetAccountResponse,
            60 => Self::RequestOtp,
            61 => Self::RequestOtpResponse,
            62 => Self::SubmitOtp,
            63 => Self::SubmitOtpResponse,
            64 => Self::ClosePositionRequest,
            65 => Self::ClosePositionResponse,
            66 => Self::ReversePositionRequest,
            67 => Self::ReversePositionResponse,
            68 => Self::ModifyOrderRequest,
            69 => Self::ModifyOrderResponse,
            90 => Self::Ack,
            91 => Self::Heartbeat,
            92 => Self::SignalSnapshot,
            93 => Self::SignalEvent,
            94 => Self::OmsOrderEvent,
            95 => Self::OrderHistoryRequest,
            96 => Self::OrderHistoryResponse,
            97 => Self::OrderStatsRequest,
            98 => Self::OrderStatsResponse,
            99 => Self::ClosedTradesRequest,
            100 => Self::ClosedTradesResponse,
            101 => Self::EquityCurveRequest,
            102 => Self::EquityCurveResponse,
            103 => Self::AttachBracketRequest,
            104 => Self::AttachBracketResponse,
            105 => Self::CancelBracketRequest,
            106 => Self::CancelBracketResponse,
            107 => Self::ListBracketsRequest,
            108 => Self::ListBracketsResponse,
            109 => Self::FetchMetricsHistoryRequest,
            110 => Self::FetchMetricsHistoryResponse,
            111 => Self::FetchOrderFillsRequest,
            112 => Self::FetchOrderFillsResponse,
            113 => Self::ExchangeTpSlGetRequest,
            114 => Self::ExchangeTpSlGetResponse,
            115 => Self::ExchangeTpSlSetRequest,
            116 => Self::ExchangeTpSlSetResponse,
            117 => Self::AutoTradeControlRequest,
            118 => Self::AutoTradeControlResponse,
            119 => Self::AutoTradeCockpit,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HelloPayload {
    pub protocol_version: u8,
    pub server_name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ErrorPayload {
    pub code: &'static str,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AckPayload {
    pub request_sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HeartbeatPayload {
    pub server_time_unix_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SymbolRangeRequest {
    pub symbol: String,
    pub from_timestamp: Option<i64>,
    pub to_timestamp: Option<i64>,
    #[serde(default)]
    pub resolution: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReplayRangeRequest {
    pub symbol: String,
    pub replay_key: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionProfileRequest {
    pub symbol: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    pub profile_type: String,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub value_area_pct: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompositeProfileRequest {
    pub symbol: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub value_area_pct: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ContextProfileRequest {
    pub symbol: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    pub session_count: usize,
    pub context_mode: String,
    pub offset: usize,
    pub price_grouping: i32,
    pub tpo_bucket_minutes: i64,
    pub value_area_pct: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubscriptionRequest {
    pub symbol: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HistoricalDomRequest {
    pub symbol: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    pub max_snapshots: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HistoricalDomResponse {
    pub symbol: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    pub snapshots: Vec<market_dom_viewmodel::DomSnapshotView>,
    pub total_count: usize,
}

// --- order execution: requests ---

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlaceOrderRequest {
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: i64,
    pub price: Option<f64>,
    pub client_order_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CancelOrderRequest {
    pub order_id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListOrdersRequest {
    pub symbol: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GetPositionsRequest {
    pub symbol: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GetAccountRequest {}

// --- order execution: responses and views ---

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderView {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: i64,
    pub filled_quantity: i64,
    pub price: Option<f64>,
    pub avg_fill_price: Option<f64>,
    pub status: String,
    pub reject_reason: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlaceOrderResponse {
    pub order: OrderView,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CancelOrderResponse {
    pub order_id: String,
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListOrdersResponse {
    pub orders: Vec<OrderView>,
}

/// Flatten the position in `symbol`; the gateway reads the net quantity server-side.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosePositionRequest {
    pub symbol: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosePositionResponse {
    /// The opposite-side market order that flattens the position.
    pub order: OrderView,
}

/// Reverse the position in `symbol` with one opposite-side market order for 2 × |net|.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReversePositionRequest {
    pub symbol: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReversePositionResponse {
    /// The reversing market order.
    pub order: OrderView,
}

/// Modify a working order by cancelling `order_id` and placing a new order with these fields.
///
/// `order_type` is `market`, `limit`, `ato`, `atc`, `mok` or `mak`; `price` is for `limit` only.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModifyOrderRequest {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub quantity: i64,
    #[serde(default)]
    pub price: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModifyOrderResponse {
    /// The replacement order.
    pub order: OrderView,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PositionView {
    pub symbol: String,
    pub net_quantity: i64,
    pub avg_entry_price: f64,
    pub current_price: f64,
    pub unrealized_pnl: f64,
    pub realized_pnl: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GetPositionsResponse {
    pub positions: Vec<PositionView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AccountView {
    pub balance: f64,
    pub equity: f64,
    pub unrealized_pnl: f64,
    pub realized_pnl: f64,
    pub margin_used: f64,
    pub buying_power: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GetAccountResponse {
    pub account: AccountView,
}

// --- OTP flow ---

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RequestOtpRequest {}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RequestOtpResponse {
    pub status: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmitOtpRequest {
    pub otp: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SubmitOtpResponse {
    pub success: bool,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HistoricalBarView {
    pub symbol: String,
    pub timestamp: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HistoricalBarsResponse {
    pub symbol: String,
    pub bars: Vec<HistoricalBarView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LiveTickView {
    pub symbol: String,
    pub timestamp: i64,
    pub price: f64,
    pub volume: i64,
    pub side: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_sequence: Option<u64>,
    pub source_pickup_unix_ns: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LiveQuoteLevelView {
    pub price: f64,
    pub volume: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LiveQuoteView {
    pub symbol: String,
    pub timestamp: i64,
    pub total_bid_qty: i64,
    pub total_offer_qty: i64,
    pub bid_levels: Vec<LiveQuoteLevelView>,
    pub offer_levels: Vec<LiveQuoteLevelView>,
    pub last_trade_price: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_sequence: Option<u64>,
    pub source_pickup_unix_ns: Option<u64>,
}

/// One SIG-001 signal event.
///
/// `id` is a stable snake_case name such as `vpin_toxic` or `trailing_stop_update`; `stop`
/// carries the advisory trailing-stop level for the `trailing_stop_*` ids.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalEventView {
    pub id: String,
    pub direction: String,
    pub strength: f64,
    pub price: f64,
    pub timestamp: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop: Option<f64>,
    #[serde(default)]
    pub note: String,
}

/// 80% Rule session bias (SIG-001 Tier 2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionBiasView {
    pub direction: String,
    pub prior_vah: f64,
    pub prior_val: f64,
    pub target: f64,
    pub confidence: f64,
    pub computed_at: i64,
}

/// Open advisory position with its trailing stop (SIG-001).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AdvisoryPositionView {
    pub direction: String,
    pub entry_price: f64,
    pub entry_ts: i64,
    pub stop: f64,
    pub extreme: f64,
}

/// Signal-evaluator state, sent when a tick subscription starts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalStateView {
    pub vpin: f64,
    pub vpin_regime: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bias: Option<SessionBiasView>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub position: Option<AdvisoryPositionView>,
    #[serde(default)]
    pub recent_events: Vec<SignalEventView>,
}

/// One OMS order lifecycle event, pushed on the tick lane as the gateway observes it.
///
/// Fields match the `oms.orders_journal` rows. `event_kind` is `place`, `ack`, `partial`, `fill`,
/// `cancel`, `reject` or `list`; `side` is `B`, `S` or `U`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OmsOrderEventView {
    /// Unix milliseconds (gateway host clock).
    pub event_ts: i64,
    pub event_kind: String,
    pub order_id: String,
    #[serde(default)]
    pub client_order_id: String,
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub status: String,
    pub qty: i64,
    pub filled_qty: i64,
    pub price: f64,
    pub avg_price: f64,
    #[serde(default)]
    pub reject_reason: String,
}

/// Query past order events from `oms.orders_journal`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderHistoryRequest {
    #[serde(default)]
    pub symbol: Option<String>,
    /// Unix seconds, inclusive; the journal stores ICT timestamps.
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub max_rows: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderHistoryRow {
    /// Unix milliseconds.
    pub event_ts: i64,
    /// ICT session date, `YYYY-MM-DD`.
    pub session_date: String,
    pub order_id: String,
    #[serde(default)]
    pub client_order_id: String,
    pub symbol: String,
    pub side: String,
    pub order_type: String,
    pub status: String,
    pub qty: i64,
    pub filled_qty: i64,
    pub price: f64,
    pub avg_price: f64,
    #[serde(default)]
    pub reject_reason: String,
    pub event_kind: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderHistoryResponse {
    pub rows: Vec<OrderHistoryRow>,
    pub total_count: usize,
}

/// Query per-session order statistics from `oms.orders_journal`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderStatsRequest {
    /// Unix seconds, inclusive; the journal stores ICT timestamps.
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub symbol: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderStatsDay {
    /// ICT session date, `YYYY-MM-DD`.
    pub session_date: String,
    /// Orders that carried a `place` event this session.
    pub order_count: i64,
    pub fill_count: i64,
    /// Orders with a `reject` or gateway `error` event.
    pub reject_count: i64,
    /// `fill_count / order_count`, 0.0 when no orders.
    pub fill_rate: f64,
    /// Place→first-fill latency percentiles (ms), over filled orders only.
    pub latency_p50_ms: f64,
    pub latency_p95_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderStatsResponse {
    pub days: Vec<OrderStatsDay>,
    pub total_count: usize,
}

/// Query closed trades and realised P&L from `oms.closed_positions`, ascending by exit time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosedTradesRequest {
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub max_rows: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosedTradeRow {
    /// Unix milliseconds.
    pub exit_ts: i64,
    pub entry_ts: i64,
    pub session_date: String,
    pub symbol: String,
    pub side: String,
    pub qty: i64,
    pub entry_price: f64,
    pub exit_price: f64,
    pub realized_pnl_points: f64,
    pub realized_pnl_money: f64,
    pub hold_secs: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClosedTradesResponse {
    pub rows: Vec<ClosedTradeRow>,
    pub total_count: usize,
}

/// Query equity-curve points from `oms.account_snapshots` (nightly and intraday).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EquityCurveRequest {
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub max_rows: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EquityCurvePoint {
    /// Unix milliseconds.
    pub snapshot_ts: i64,
    pub session_date: String,
    pub nav: f64,
    pub equity: f64,
    pub unrealized_pnl: f64,
    pub realized_pnl: f64,
    pub margin_used: f64,
    pub buying_power: f64,
    /// `nightly` or `intraday`.
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EquityCurveResponse {
    pub points: Vec<EquityCurvePoint>,
    pub total_count: usize,
}

/// Arm or replace a gateway-side bracket that flattens the position in `symbol` on a trigger.
///
/// Set at least one of `take_profit`, `stop_loss`, `trailing_delta` (index points; the trailing
/// delta is a distance). `side` is the protected position side, `"long"` or `"short"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachBracketRequest {
    pub symbol: String,
    pub side: String,
    #[serde(default)]
    pub take_profit: Option<f64>,
    #[serde(default)]
    pub stop_loss: Option<f64>,
    #[serde(default)]
    pub trailing_delta: Option<f64>,
}

/// Current state of a bracket.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BracketView {
    pub symbol: String,
    pub side: String,
    pub take_profit: Option<f64>,
    pub stop_loss: Option<f64>,
    pub trailing_delta: Option<f64>,
    /// Ratcheting trailing-stop level; `None` before the first mark or without trailing.
    pub trailing_stop: Option<f64>,
    /// `armed` | `triggered_tp` | `triggered_sl` | `cancelled`.
    pub status: String,
    /// Unix seconds the bracket was armed.
    pub armed_at: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AttachBracketResponse {
    pub bracket: BracketView,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CancelBracketRequest {
    pub symbol: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CancelBracketResponse {
    pub symbol: String,
    /// True when a bracket was armed and is now cancelled.
    pub cancelled: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ListBracketsRequest {
    /// Optional symbol filter; `None` lists all armed brackets.
    #[serde(default)]
    pub symbol: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ListBracketsResponse {
    pub brackets: Vec<BracketView>,
}

/// Request order-flow metrics history rebuilt from stored trades.
///
/// Start the range at the session open so cumulative CVD is rebuilt from zero.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FetchMetricsHistoryRequest {
    pub symbol: String,
    /// Unix seconds.
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub max_points: Option<u32>,
}

/// One rebuilt metrics sample, about 5 s apart.
///
/// CVD and VPIN come from the trade tape and OFI from stored order-book snapshots; OBI and ATR
/// are not rebuilt.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricsHistoryPoint {
    /// Unix seconds.
    pub ts: i64,
    pub cvd: f64,
    pub vpin: f64,
    /// `normal`, `elevated` or `toxic`, as in the live SIG-001 regime.
    pub vpin_regime: String,
    /// Cumulative OFI replayed from stored `dom_snapshots` (best-level change, 0.99 decay per
    /// snapshot); `0.0` without DOM history.
    #[serde(default)]
    pub ofi_cumulative: f64,
    /// VWAP accumulator state at this sample, from a `VwapCalculator` fed the same ticks as `cvd`.
    ///
    /// Seed a live calculator from the last point: `restore(vwap_cumulative_pv,
    /// vwap_cumulative_vol, vwap_cumulative_pv_sq, 0.0, vwap_last_trading_day_utc)`.
    #[serde(default)]
    pub vwap_cumulative_pv: f64,
    #[serde(default)]
    pub vwap_cumulative_vol: f64,
    #[serde(default)]
    pub vwap_cumulative_pv_sq: f64,
    #[serde(default)]
    pub vwap_last_trading_day_utc: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FetchMetricsHistoryResponse {
    pub points: Vec<MetricsHistoryPoint>,
    pub total_count: usize,
}

/// Query the per-fill breakdown from `oms.order_fills`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FetchOrderFillsRequest {
    /// Unix seconds.
    pub from_timestamp: i64,
    pub to_timestamp: i64,
    #[serde(default)]
    pub max_rows: Option<u32>,
}

/// One fill from `oms.order_fills`; `fill_seq` numbers the fills of an order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OrderFillRow {
    /// Unix milliseconds.
    pub fill_ts: i64,
    pub session_date: String,
    pub order_id: String,
    pub client_order_id: String,
    pub symbol: String,
    /// `B` | `S`.
    pub side: String,
    pub fill_seq: u32,
    pub fill_qty: i64,
    pub fill_price: f64,
    pub remaining_qty: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FetchOrderFillsResponse {
    pub rows: Vec<OrderFillRow>,
    pub total_count: usize,
}

/// Read the account's exchange-side TP/SL/trailing configuration.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeTpSlGetRequest {}

/// One exchange-side TP/SL configuration, a subset of the broker's schema.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeTpSlView {
    pub account_no: String,
    pub take_profit_enabled: bool,
    pub stop_loss_enabled: bool,
    pub trailing_enabled: bool,
    pub take_profit_price: f64,
    pub stop_loss_price: f64,
    pub stop_loss_delta_price: f64,
    /// Broker status, e.g. `ACTIVE`.
    pub status: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeTpSlGetResponse {
    /// Whether the broker supports exchange-side TP/SL.
    pub supported: bool,
    /// Armed configurations: per deal, plus the account default when armed.
    pub configs: Vec<ExchangeTpSlView>,
}

/// Arm or replace the account's exchange-side TP/SL.
///
/// The gateway refuses the request without a valid OTP-issued `trading_token`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeTpSlSetRequest {
    #[serde(default)]
    pub take_profit_price: Option<f64>,
    #[serde(default)]
    pub stop_loss_price: Option<f64>,
    /// Trailing offset in index points (enables server trailing on the SL leg).
    #[serde(default)]
    pub trailing_delta: Option<f64>,
    pub trading_token: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeTpSlSetResponse {
    pub view: ExchangeTpSlView,
}

/// Runtime auto-trade control command for strategy `target`, or for all strategies (`*_all`).
///
/// Commands: `arm`, `run`, `pause`, `kill`, `set_dry_run`, `run_all`, `pause_all`, `kill_all`.
/// `kill` requires a `confirm_token`; `kill_all` deliberately does not, so it is never blocked.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AutoTradeControlRequest {
    pub command: String,
    #[serde(default)]
    pub target: String,
    #[serde(default)]
    pub dry: bool,
    #[serde(default)]
    pub confirm_token: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AutoTradeControlResponse {
    pub ok: bool,
    /// Resulting lifecycle state for a per-strategy command (lower-case tag).
    #[serde(default)]
    pub state: Option<String>,
    /// Affected strategy count for a global command.
    #[serde(default)]
    pub affected: u32,
    #[serde(default)]
    pub error: Option<String>,
}

/// Auto-trade engine state pushed on the tick lane; read-only, commands use `AutoTradeControl`.
///
/// `actual_net` is the broker position; per-strategy P&L is a virtual attribution.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AutoTradeViewModel {
    pub account: String,
    pub symbol: String,
    pub actual_net: i64,
    pub virtual_net: i64,
    pub desired_net: i64,
    pub reconcile_orders: u64,
    /// The net-position cap clamped the summed targets (`virtual_net != desired_net`).
    pub clamped: bool,
    /// Gateway time of this snapshot, Unix milliseconds.
    pub updated_ms: i64,
    pub strategies: Vec<AutoTradeStrategyRow>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AutoTradeStrategyRow {
    pub id: String,
    /// Lifecycle tag: registered/armed/running/paused/halted.
    pub state: String,
    pub target: i64,
    #[serde(default)]
    pub entry_price: Option<f64>,
    pub realized_pnl: f64,
    pub entries: u32,
    pub last_slippage: f64,
}

/// Server-to-client payload, serialised as a JSON object keyed by the variant name.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub enum OutboundPayload {
    Hello(HelloPayload),
    Error(ErrorPayload),
    Ack(AckPayload),
    Heartbeat(HeartbeatPayload),
    HistoricalBars(HistoricalBarsResponse),
    LiveTick(LiveTickView),
    LiveQuote(LiveQuoteView),
    SessionProfile(SessionProfileView),
    ContextProfile(ContextProfileView),
    CompositeProfile(CompositeProfileView),
    ReplayRange(ReplayRangeView),
    DomSnapshot(DomSnapshotView),
    DomBatch(DomBatchView),
    HistoricalDom(HistoricalDomResponse),
    PlaceOrder(PlaceOrderResponse),
    CancelOrder(CancelOrderResponse),
    ListOrders(ListOrdersResponse),
    GetPositions(GetPositionsResponse),
    GetAccount(GetAccountResponse),
    RequestOtp(RequestOtpResponse),
    SubmitOtp(SubmitOtpResponse),
    SignalSnapshot(SignalStateView),
    SignalEvent(SignalEventView),
    OmsOrderEvent(OmsOrderEventView),
    OrderHistory(OrderHistoryResponse),
    OrderStats(OrderStatsResponse),
    ClosedTrades(ClosedTradesResponse),
    EquityCurve(EquityCurveResponse),
    ClosePosition(ClosePositionResponse),
    ReversePosition(ReversePositionResponse),
    ModifyOrder(ModifyOrderResponse),
    AttachBracket(AttachBracketResponse),
    CancelBracket(CancelBracketResponse),
    ListBrackets(ListBracketsResponse),
    MetricsHistory(FetchMetricsHistoryResponse),
    OrderFills(FetchOrderFillsResponse),
    ExchangeTpSlGet(ExchangeTpSlGetResponse),
    ExchangeTpSlSet(ExchangeTpSlSetResponse),
    AutoTradeControl(AutoTradeControlResponse),
    AutoTradeCockpit(AutoTradeViewModel),
}

impl OutboundPayload {
    #[must_use]
    pub fn message_kind(&self) -> MessageKind {
        match self {
            Self::Hello(_) => MessageKind::Hello,
            Self::Error(_) => MessageKind::Error,
            Self::Ack(_) => MessageKind::Ack,
            Self::Heartbeat(_) => MessageKind::Heartbeat,
            Self::HistoricalBars(_) => MessageKind::HistoricalBarsResponse,
            Self::LiveTick(_) => MessageKind::LiveTick,
            Self::LiveQuote(_) => MessageKind::LiveQuote,
            Self::SessionProfile(_) => MessageKind::SessionProfileResponse,
            Self::ContextProfile(_) => MessageKind::ContextProfileResponse,
            Self::CompositeProfile(_) => MessageKind::CompositeProfileResponse,
            Self::ReplayRange(_) => MessageKind::ReplayRangeResponse,
            Self::DomSnapshot(_) => MessageKind::DomSnapshot,
            Self::DomBatch(_) => MessageKind::DomBatch,
            Self::HistoricalDom(_) => MessageKind::HistoricalDomResponse,
            Self::PlaceOrder(_) => MessageKind::PlaceOrderResponse,
            Self::CancelOrder(_) => MessageKind::CancelOrderResponse,
            Self::ListOrders(_) => MessageKind::ListOrdersResponse,
            Self::GetPositions(_) => MessageKind::GetPositionsResponse,
            Self::GetAccount(_) => MessageKind::GetAccountResponse,
            Self::RequestOtp(_) => MessageKind::RequestOtpResponse,
            Self::SubmitOtp(_) => MessageKind::SubmitOtpResponse,
            Self::SignalSnapshot(_) => MessageKind::SignalSnapshot,
            Self::SignalEvent(_) => MessageKind::SignalEvent,
            Self::OmsOrderEvent(_) => MessageKind::OmsOrderEvent,
            Self::OrderHistory(_) => MessageKind::OrderHistoryResponse,
            Self::OrderStats(_) => MessageKind::OrderStatsResponse,
            Self::ClosedTrades(_) => MessageKind::ClosedTradesResponse,
            Self::EquityCurve(_) => MessageKind::EquityCurveResponse,
            Self::ClosePosition(_) => MessageKind::ClosePositionResponse,
            Self::ReversePosition(_) => MessageKind::ReversePositionResponse,
            Self::ModifyOrder(_) => MessageKind::ModifyOrderResponse,
            Self::AttachBracket(_) => MessageKind::AttachBracketResponse,
            Self::CancelBracket(_) => MessageKind::CancelBracketResponse,
            Self::ListBrackets(_) => MessageKind::ListBracketsResponse,
            Self::MetricsHistory(_) => MessageKind::FetchMetricsHistoryResponse,
            Self::OrderFills(_) => MessageKind::FetchOrderFillsResponse,
            Self::ExchangeTpSlGet(_) => MessageKind::ExchangeTpSlGetResponse,
            Self::ExchangeTpSlSet(_) => MessageKind::ExchangeTpSlSetResponse,
            Self::AutoTradeControl(_) => MessageKind::AutoTradeControlResponse,
            Self::AutoTradeCockpit(_) => MessageKind::AutoTradeCockpit,
        }
    }
}

#[cfg(test)]
mod tests;
