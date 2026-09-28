//! Parse-don't-validate domain types for VN30F1M market data: fixed-point `Price`, non-negative
//! `Volume`, ticks, quotes, bars and order-book snapshots, each with a raw input form.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize};
use thiserror::Error;

const QUOTE_LEVEL_LIMIT: usize = 20;

fn normalize_side(value: &str) -> Option<TickSide> {
    match value.trim().to_ascii_uppercase().as_str() {
        "B" | "BUY" => Some(TickSide::Buy),
        "S" | "SELL" => Some(TickSide::Sell),
        _ => None,
    }
}

fn truncate_price_levels(prices: &[f64], volumes: &[i64]) -> Result<Vec<PriceLevel>, ParseError> {
    prices
        .iter()
        .zip(volumes.iter())
        .take(QUOTE_LEVEL_LIMIT)
        .map(|(price, volume)| {
            Ok(PriceLevel {
                price: Price::from_f64(*price)?,
                volume: Volume::new(*volume)?,
            })
        })
        .collect()
}

fn is_blank(value: &str) -> bool {
    value.trim().is_empty()
}

fn parse_optional_datetime(
    value: Option<&str>,
) -> Result<Option<DateTime<FixedOffset>>, ParseError> {
    match value {
        Some(raw) if !is_blank(raw) => DateTime::parse_from_rfc3339(raw)
            .map(Some)
            .map_err(|_| ParseError::InvalidDateTime),
        _ => Ok(None),
    }
}

fn parse_optional_last_trade(
    price: f64,
    volume: i64,
    side: &str,
    timestamp: i64,
) -> Result<Option<LastTrade>, ParseError> {
    let Some(side) = normalize_side(side) else {
        return Ok(None);
    };
    if !price.is_finite() || volume < 0 || timestamp < 0 {
        return Ok(None);
    }
    Ok(Some(LastTrade {
        price: Price::from_f64(price)?,
        volume: Volume::new(volume)?,
        side,
        timestamp: EventTimestamp::new(timestamp)?,
    }))
}

pub const PRICE_SCALE: i64 = 10_000;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ParseError {
    #[error("symbol must not be empty")]
    EmptySymbol,
    #[error("price is not finite")]
    NonFinitePrice,
    #[error("volume must be non-negative")]
    NegativeVolume,
    #[error("timestamp must be non-negative")]
    NegativeTimestamp,
    #[error("session key must not be empty")]
    EmptySessionKey,
    #[error("replay range key must not be empty")]
    EmptyReplayRangeKey,
    #[error("tick size must be positive")]
    NonPositiveTickSize,
    #[error("tick level conversion requires divisible price delta")]
    NonDivisibleTickLevel,
    #[error("tick level exceeds i32 range")]
    TickLevelOutOfRange,
    #[error("datetime string is invalid")]
    InvalidDateTime,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Symbol(String);

impl Symbol {
    pub fn new(value: impl Into<String>) -> Result<Self, ParseError> {
        let value = value.into().trim().to_owned();
        if value.is_empty() {
            return Err(ParseError::EmptySymbol);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
#[serde(transparent)]
pub struct Price(pub i64);

impl Price {
    pub fn from_f64(value: f64) -> Result<Self, ParseError> {
        if !value.is_finite() {
            return Err(ParseError::NonFinitePrice);
        }
        Ok(Self((value * PRICE_SCALE as f64).round() as i64))
    }

    #[must_use]
    pub fn as_f64(self) -> f64 {
        self.0 as f64 / PRICE_SCALE as f64
    }
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
#[serde(transparent)]
pub struct Volume(pub i64);

impl Volume {
    pub fn new(value: i64) -> Result<Self, ParseError> {
        if value < 0 {
            return Err(ParseError::NegativeVolume);
        }
        Ok(Self(value))
    }
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
#[serde(transparent)]
pub struct EventTimestamp(pub i64);

impl EventTimestamp {
    pub fn new(value: i64) -> Result<Self, ParseError> {
        if value < 0 {
            return Err(ParseError::NegativeTimestamp);
        }
        Ok(Self(value))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SessionKey(String);

impl SessionKey {
    pub fn new(value: impl Into<String>) -> Result<Self, ParseError> {
        let value = value.into().trim().to_owned();
        if value.is_empty() {
            return Err(ParseError::EmptySessionKey);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ReplayRangeKey(String);

impl ReplayRangeKey {
    pub fn new(value: impl Into<String>) -> Result<Self, ParseError> {
        let value = value.into().trim().to_owned();
        if value.is_empty() {
            return Err(ParseError::EmptyReplayRangeKey);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
#[serde(transparent)]
pub struct TickLevel(pub i32);

impl TickLevel {
    pub fn from_price(
        price: Price,
        reference_price: Price,
        tick_size: Price,
    ) -> Result<Self, ParseError> {
        if tick_size.0 <= 0 {
            return Err(ParseError::NonPositiveTickSize);
        }
        let diff = price.0 - reference_price.0;
        if diff % tick_size.0 != 0 {
            return Err(ParseError::NonDivisibleTickLevel);
        }
        let level = diff / tick_size.0;
        let level = i32::try_from(level).map_err(|_| ParseError::TickLevelOutOfRange)?;
        Ok(Self(level))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tick {
    pub symbol: Symbol,
    pub timestamp: EventTimestamp,
    pub price: Price,
    pub volume: Volume,
    pub side: Option<TickSide>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteSnapshot {
    pub symbol: Symbol,
    pub timestamp: EventTimestamp,
    pub total_bid_qty: Volume,
    pub total_offer_qty: Volume,
    pub bid_levels: Vec<PriceLevel>,
    pub offer_levels: Vec<PriceLevel>,
    pub last_trade_price: Option<Price>,
    pub last_trade: Option<LastTrade>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OhlcBar {
    pub symbol: Symbol,
    pub timestamp: EventTimestamp,
    pub open: Price,
    pub high: Price,
    pub low: Price,
    pub close: Price,
    pub volume: Volume,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionRange {
    pub session_key: SessionKey,
    pub from_timestamp: EventTimestamp,
    pub to_timestamp: EventTimestamp,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawTick {
    pub symbol: String,
    pub timestamp: i64,
    pub price: f64,
    pub volume: i64,
    pub side: String,
}

impl TryFrom<RawTick> for Tick {
    type Error = ParseError;

    fn try_from(value: RawTick) -> Result<Self, Self::Error> {
        Ok(Self {
            symbol: Symbol::new(value.symbol)?,
            timestamp: EventTimestamp::new(value.timestamp)?,
            price: Price::from_f64(value.price)?,
            volume: Volume::new(value.volume)?,
            side: normalize_side(&value.side),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawQuoteSnapshot {
    pub symbol: String,
    pub timestamp: i64,
    pub datetime: Option<String>,
    pub total_bid_qty: i64,
    pub total_offer_qty: i64,
    pub bid_prices: Vec<f64>,
    pub bid_volumes: Vec<i64>,
    pub offer_prices: Vec<f64>,
    pub offer_volumes: Vec<i64>,
    pub last_trade_price: f64,
    pub last_trade_volume: i64,
    pub last_trade_side: String,
    pub last_trade_timestamp: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PriceLevel {
    pub price: Price,
    pub volume: Volume,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedQuoteSnapshot {
    pub quote: QuoteSnapshot,
    pub datetime: Option<DateTime<FixedOffset>>,
}

impl TryFrom<RawQuoteSnapshot> for ParsedQuoteSnapshot {
    type Error = ParseError;

    fn try_from(value: RawQuoteSnapshot) -> Result<Self, Self::Error> {
        let quote_timestamp = EventTimestamp::new(value.timestamp)?;
        let datetime = parse_optional_datetime(value.datetime.as_deref())?;
        let last_trade_price = if value.last_trade_price.is_finite() {
            Some(Price::from_f64(value.last_trade_price)?)
        } else {
            None
        };
        let last_trade = parse_optional_last_trade(
            value.last_trade_price,
            value.last_trade_volume,
            &value.last_trade_side,
            value.last_trade_timestamp,
        )?;

        Ok(Self {
            quote: QuoteSnapshot {
                symbol: Symbol::new(value.symbol)?,
                timestamp: quote_timestamp,
                total_bid_qty: Volume::new(value.total_bid_qty)?,
                total_offer_qty: Volume::new(value.total_offer_qty)?,
                bid_levels: truncate_price_levels(&value.bid_prices, &value.bid_volumes)?,
                offer_levels: truncate_price_levels(&value.offer_prices, &value.offer_volumes)?,
                last_trade_price,
                last_trade,
            },
            datetime,
        })
    }
}

impl TryFrom<RawQuoteSnapshot> for QuoteSnapshot {
    type Error = ParseError;

    fn try_from(value: RawQuoteSnapshot) -> Result<Self, Self::Error> {
        ParsedQuoteSnapshot::try_from(value).map(|parsed| parsed.quote)
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawOhlcBar {
    pub symbol: String,
    pub timestamp: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
}

impl TryFrom<RawOhlcBar> for OhlcBar {
    type Error = ParseError;

    fn try_from(value: RawOhlcBar) -> Result<Self, Self::Error> {
        Ok(Self {
            symbol: Symbol::new(value.symbol)?,
            timestamp: EventTimestamp::new(value.timestamp)?,
            open: Price::from_f64(value.open)?,
            high: Price::from_f64(value.high)?,
            low: Price::from_f64(value.low)?,
            close: Price::from_f64(value.close)?,
            volume: Volume::new(value.volume)?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawSessionRange {
    pub session_key: String,
    pub from_timestamp: i64,
    pub to_timestamp: i64,
}

impl TryFrom<RawSessionRange> for SessionRange {
    type Error = ParseError;

    fn try_from(value: RawSessionRange) -> Result<Self, Self::Error> {
        Ok(Self {
            session_key: SessionKey::new(value.session_key)?,
            from_timestamp: EventTimestamp::new(value.from_timestamp)?,
            to_timestamp: EventTimestamp::new(value.to_timestamp)?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawTickTimeInput {
    pub datetime_iso: Option<String>,
    pub timestamp: Option<i64>,
}

impl TryFrom<RawTickTimeInput> for RawTickTimeFields {
    type Error = ParseError;

    fn try_from(value: RawTickTimeInput) -> Result<Self, Self::Error> {
        Ok(Self {
            datetime_iso: value.datetime_iso.filter(|raw| !is_blank(raw)),
            timestamp: value.timestamp.map(EventTimestamp::new).transpose()?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawTickTimeFields {
    pub datetime_iso: Option<String>,
    pub timestamp: Option<EventTimestamp>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LastTrade {
    pub price: Price,
    pub volume: Volume,
    pub side: TickSide,
    pub timestamp: EventTimestamp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TickSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct MalformedFlag(pub bool);

/// Order-book snapshot for offline backtest replay; index 0 is the best bid or ask.
///
/// Prices and sizes are plain `f64`: the book feeds signals only, and fills use tick prices.
/// `ts_ms` is Unix milliseconds, so sub-second updates keep their order among the ticks.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BookSnapshot {
    pub ts_ms: i64,
    pub bid_px: Vec<f64>,
    pub bid_sz: Vec<f64>,
    pub ask_px: Vec<f64>,
    pub ask_sz: Vec<f64>,
}

impl BookSnapshot {
    /// (price, size) at the best bid, if present.
    #[must_use]
    pub fn best_bid(&self) -> Option<(f64, f64)> {
        Some((*self.bid_px.first()?, *self.bid_sz.first()?))
    }

    /// (price, size) at the best ask, if present.
    #[must_use]
    pub fn best_ask(&self) -> Option<(f64, f64)> {
        Some((*self.ask_px.first()?, *self.ask_sz.first()?))
    }

    /// Best-quote mid price, if both sides present.
    #[must_use]
    pub fn mid(&self) -> Option<f64> {
        Some((self.best_bid()?.0 + self.best_ask()?.0) / 2.0)
    }

    /// Event time in whole Unix seconds (floored), for session gating and merging.
    #[must_use]
    pub fn ts_secs(&self) -> i64 {
        self.ts_ms.div_euclid(1000)
    }
}

/// One JSONL line of an L2 book tape: `ts` in float Unix seconds (millisecond precision) and
/// top-N book levels.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RawBookSnapshot {
    pub ts: f64,
    pub bid_px: Vec<f64>,
    pub bid_sz: Vec<f64>,
    pub ask_px: Vec<f64>,
    pub ask_sz: Vec<f64>,
}

impl From<RawBookSnapshot> for BookSnapshot {
    fn from(r: RawBookSnapshot) -> Self {
        Self {
            ts_ms: (r.ts * 1000.0).round() as i64,
            bid_px: r.bid_px,
            bid_sz: r.bid_sz,
            ask_px: r.ask_px,
            ask_sz: r.ask_sz,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn price_round_trip_uses_fixed_scale() {
        let price = Price::from_f64(1050.1).expect("finite price should parse");
        assert_eq!(price.0, 10_501_000);
        assert_eq!(price.as_f64(), 1050.1);
    }

    #[test]
    fn tick_level_uses_domain_price_plus_tick_size() {
        let reference = Price::from_f64(1050.0).expect("finite price should parse");
        let price = Price::from_f64(1050.1).expect("finite price should parse");
        let tick_size = Price::from_f64(0.1).expect("finite price should parse");
        let level =
            TickLevel::from_price(price, reference, tick_size).expect("divisible tick level");
        assert_eq!(level, TickLevel(1));
    }

    #[test]
    fn raw_tick_maps_to_valid_domain_tick() {
        let tick = Tick::try_from(RawTick {
            symbol: "VN30F1M".to_string(),
            timestamp: 1_776_391_200,
            price: 1251.5,
            volume: 12,
            side: "sell".to_string(),
        })
        .expect("raw tick should parse");

        assert_eq!(tick.symbol.as_str(), "VN30F1M");
        assert_eq!(tick.side, Some(TickSide::Sell));
        assert_eq!(tick.price.as_f64(), 1251.5);
    }

    #[test]
    fn raw_quote_truncates_mismatched_depth_and_parses_datetime() {
        let parsed = ParsedQuoteSnapshot::try_from(RawQuoteSnapshot {
            symbol: "VN30F1M".to_string(),
            timestamp: 1_776_391_205,
            datetime: Some("2026-04-17T09:00:05+07:00".to_string()),
            total_bid_qty: 300,
            total_offer_qty: 330,
            bid_prices: vec![1251.4, 1251.3, 1251.2],
            bid_volumes: vec![100, 200],
            offer_prices: vec![1251.5, 1251.6],
            offer_volumes: vec![110, 220, 330],
            last_trade_price: 1251.5,
            last_trade_volume: 12,
            last_trade_side: "B".to_string(),
            last_trade_timestamp: 1_776_391_206,
        })
        .expect("raw quote should parse");

        assert_eq!(parsed.quote.bid_levels.len(), 2);
        assert_eq!(parsed.quote.offer_levels.len(), 2);
        assert!(parsed.datetime.is_some());
        assert_eq!(
            parsed.quote.last_trade.as_ref().map(|trade| trade.side),
            Some(TickSide::Buy)
        );
    }

    #[test]
    fn raw_quote_drops_invalid_last_trade_without_failing_snapshot() {
        let parsed = ParsedQuoteSnapshot::try_from(RawQuoteSnapshot {
            symbol: "VN30F1M".to_string(),
            timestamp: 1_776_391_205,
            datetime: Some("".to_string()),
            total_bid_qty: 300,
            total_offer_qty: 330,
            bid_prices: vec![1251.4],
            bid_volumes: vec![100],
            offer_prices: vec![1251.5],
            offer_volumes: vec![110],
            last_trade_price: 1251.5,
            last_trade_volume: 12,
            last_trade_side: "X".to_string(),
            last_trade_timestamp: 1_776_391_206,
        })
        .expect("raw quote should parse");

        assert!(parsed.datetime.is_none());
        assert_eq!(
            parsed.quote.last_trade_price.map(Price::as_f64),
            Some(1251.5)
        );
        assert!(parsed.quote.last_trade.is_none());
    }

    #[test]
    fn raw_tick_time_input_trims_blank_datetime() {
        let parsed = RawTickTimeFields::try_from(RawTickTimeInput {
            datetime_iso: Some("   ".to_string()),
            timestamp: Some(1_776_391_200),
        })
        .expect("raw tick time should parse");

        assert!(parsed.datetime_iso.is_none());
        assert_eq!(parsed.timestamp, Some(EventTimestamp(1_776_391_200)));
    }

    #[test]
    fn raw_session_range_maps_to_domain() {
        let range = SessionRange::try_from(RawSessionRange {
            session_key: "2026-04-17".to_string(),
            from_timestamp: 1_776_391_200,
            to_timestamp: 1_776_411_900,
        })
        .expect("session range should parse");

        assert_eq!(range.session_key.as_str(), "2026-04-17");
        assert_eq!(range.from_timestamp, EventTimestamp(1_776_391_200));
        assert_eq!(range.to_timestamp, EventTimestamp(1_776_411_900));
    }

    proptest! {
        #[test]
        fn raw_tick_rejects_non_finite_or_negative_boundaries(
            symbol in "\\PC*",
            side in "\\PC*",
            negative_volume in i64::MIN..0,
            negative_timestamp in i64::MIN..0,
            bad_price in prop_oneof![Just(f64::NAN), Just(f64::INFINITY), Just(f64::NEG_INFINITY)],
        ) {
            let invalid_price = Tick::try_from(RawTick {
                symbol: symbol.clone(),
                timestamp: 1,
                price: bad_price,
                volume: 1,
                side: side.clone(),
            });
            prop_assert!(invalid_price.is_err());

            let invalid_volume = Tick::try_from(RawTick {
                symbol: symbol.clone(),
                timestamp: 1,
                price: 1.0,
                volume: negative_volume,
                side: side.clone(),
            });
            prop_assert!(invalid_volume.is_err());

            let invalid_timestamp = Tick::try_from(RawTick {
                symbol,
                timestamp: negative_timestamp,
                price: 1.0,
                volume: 1,
                side,
            });
            prop_assert!(invalid_timestamp.is_err());
        }

        #[test]
        fn raw_tick_time_input_preserves_only_non_blank_datetime_and_non_negative_timestamp(
            datetime in prop::option::of(".*"),
            timestamp in any::<i64>(),
        ) {
            let result = RawTickTimeFields::try_from(RawTickTimeInput {
                datetime_iso: datetime.clone(),
                timestamp: Some(timestamp),
            });

            if timestamp < 0 {
                prop_assert!(result.is_err());
            } else {
                let parsed = result.expect("non-negative timestamp should parse");
                let expected_datetime = datetime.filter(|raw| !raw.trim().is_empty());
                prop_assert_eq!(parsed.datetime_iso, expected_datetime);
                prop_assert_eq!(parsed.timestamp, Some(EventTimestamp(timestamp)));
            }
        }

        #[test]
        fn raw_quote_truncation_never_exceeds_shortest_side(
            bid_prices in prop::collection::vec(-10_000.0f64..10_000.0f64, 0..30),
            bid_volumes in prop::collection::vec(0i64..10_000i64, 0..30),
            offer_prices in prop::collection::vec(-10_000.0f64..10_000.0f64, 0..30),
            offer_volumes in prop::collection::vec(0i64..10_000i64, 0..30),
        ) {
            let expected_bid_len = bid_prices.len().min(bid_volumes.len()).min(QUOTE_LEVEL_LIMIT);
            let expected_offer_len = offer_prices.len().min(offer_volumes.len()).min(QUOTE_LEVEL_LIMIT);
            let parsed = ParsedQuoteSnapshot::try_from(RawQuoteSnapshot {
                symbol: "VN30F1M".to_string(),
                timestamp: 1,
                datetime: None,
                total_bid_qty: 0,
                total_offer_qty: 0,
                bid_prices,
                bid_volumes,
                offer_prices,
                offer_volumes,
                last_trade_price: 0.0,
                last_trade_volume: 0,
                last_trade_side: String::new(),
                last_trade_timestamp: 0,
            }).expect("quote with finite levels should parse");

            prop_assert_eq!(parsed.quote.bid_levels.len(), expected_bid_len);
            prop_assert_eq!(parsed.quote.offer_levels.len(), expected_offer_len);
        }
    }
}
