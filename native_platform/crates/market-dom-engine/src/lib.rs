//! Top-N order-book snapshots: level normalisation, a malformed-book flag and an as-of join of
//! trades onto quotes.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use market_types::ParseError;
use market_types::{
    EventTimestamp, LastTrade, MalformedFlag, ParsedQuoteSnapshot, Price, PriceLevel,
    QuoteSnapshot, RawQuoteSnapshot, Volume,
};
use serde::Deserialize;
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct DomSourceQuotePayload {
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
    pub last_trade_timestamp: Option<i64>,
}

impl From<DomSourceQuotePayload> for RawQuoteSnapshot {
    fn from(value: DomSourceQuotePayload) -> Self {
        let last_trade_timestamp = value.last_trade_timestamp.unwrap_or(value.timestamp);
        Self {
            symbol: value.symbol,
            timestamp: value.timestamp,
            datetime: value.datetime,
            total_bid_qty: value.total_bid_qty,
            total_offer_qty: value.total_offer_qty,
            bid_prices: value.bid_prices,
            bid_volumes: value.bid_volumes,
            offer_prices: value.offer_prices,
            offer_volumes: value.offer_volumes,
            last_trade_price: value.last_trade_price,
            last_trade_volume: value.last_trade_volume,
            last_trade_side: value.last_trade_side,
            last_trade_timestamp,
        }
    }
}

pub fn build_raw_quote_from_source_payload(payload: DomSourceQuotePayload) -> RawQuoteSnapshot {
    payload.into()
}

pub fn build_parsed_quote_from_source_payload(
    payload: DomSourceQuotePayload,
) -> Result<ParsedQuoteSnapshot, DomError> {
    Ok(ParsedQuoteSnapshot::try_from(
        build_raw_quote_from_source_payload(payload),
    )?)
}

pub fn build_dom_snapshot_from_source_payload(
    symbol: &str,
    payload: DomSourceQuotePayload,
    latest_tick: Option<LastTrade>,
) -> Result<DomSnapshot, DomError> {
    build_dom_snapshot_from_raw_quote(
        symbol,
        build_raw_quote_from_source_payload(payload),
        latest_tick,
    )
}

pub fn build_dom_batch_from_source_payloads(
    symbol: &str,
    payloads: Vec<DomSourceQuotePayload>,
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Result<Vec<DomSnapshot>, DomError> {
    let raw_quotes = payloads
        .into_iter()
        .map(build_raw_quote_from_source_payload)
        .collect::<Vec<_>>();
    build_dom_batch_from_raw_quotes(symbol, raw_quotes, ticks, latest_tick)
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum DomError {
    #[error("symbol must not be empty")]
    EmptySymbol,
    #[error(transparent)]
    Parse(#[from] ParseError),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomSnapshot {
    pub symbol: String,
    pub timestamp: Option<EventTimestamp>,
    pub last_trade_price: Option<Price>,
    pub best_bid: Option<Price>,
    pub best_ask: Option<Price>,
    pub spread: Option<Price>,
    pub total_bid_qty: Volume,
    pub total_offer_qty: Volume,
    pub bids: Vec<PriceLevel>,
    pub asks: Vec<PriceLevel>,
    pub last_trade: Option<LastTrade>,
    /// Malformed-book warning for consumers; the snapshot stays renderable.
    pub malformed: MalformedFlag,
}

fn validate_symbol(symbol: &str) -> Result<(), DomError> {
    if symbol.trim().is_empty() {
        return Err(DomError::EmptySymbol);
    }
    Ok(())
}

fn normalize_bid_levels(levels: &[PriceLevel]) -> (Vec<PriceLevel>, bool) {
    let normalized = levels.to_vec();
    let malformed = normalized
        .windows(2)
        .any(|window| window[0].price < window[1].price);
    (normalized, malformed)
}

fn normalize_ask_levels(levels: &[PriceLevel]) -> (Vec<PriceLevel>, bool) {
    let normalized = levels.to_vec();
    let malformed = normalized
        .windows(2)
        .any(|window| window[0].price > window[1].price);
    (normalized, malformed)
}

fn merge_last_trade(base: Option<LastTrade>, latest_tick: Option<LastTrade>) -> Option<LastTrade> {
    match (base, latest_tick) {
        (_, Some(latest_tick)) => Some(latest_tick),
        (base, None) => base,
    }
}

fn merge_last_trade_price(base: Option<Price>, latest_tick: Option<&LastTrade>) -> Option<Price> {
    latest_tick.map(|tick| tick.price).or(base)
}

fn quote_is_malformed(quote: &QuoteSnapshot) -> bool {
    quote.last_trade.is_some() && quote.last_trade_price.is_none()
}

fn batch_quote_sort_key(quote: &QuoteSnapshot) -> i64 {
    quote.timestamp.0
}

fn batch_tick_sort_key(tick: &LastTrade) -> i64 {
    tick.timestamp.0
}

fn quote_timestamp(quote: &QuoteSnapshot) -> i64 {
    quote.timestamp.0
}

fn tick_timestamp(tick: &LastTrade) -> i64 {
    tick.timestamp.0
}

fn tick_at_or_before_quote(quote: &QuoteSnapshot, tick: &LastTrade) -> bool {
    tick_timestamp(tick) <= quote_timestamp(quote)
}

fn sort_quotes_for_matching(quotes: &[QuoteSnapshot]) -> Vec<(usize, &QuoteSnapshot)> {
    let mut ordered = quotes.iter().enumerate().collect::<Vec<_>>();
    ordered.sort_by_key(|(_, quote)| batch_quote_sort_key(quote));
    ordered
}

fn sorted_ticks(ticks: &[LastTrade]) -> Vec<LastTrade> {
    let mut ordered = ticks.to_vec();
    ordered.sort_by_key(batch_tick_sort_key);
    ordered
}

fn align_matched_ticks_to_input_order(
    quotes_len: usize,
    ordered_matches: Vec<(usize, Option<LastTrade>)>,
) -> Vec<Option<LastTrade>> {
    let mut matched = vec![None; quotes_len];
    for (index, tick) in ordered_matches {
        matched[index] = tick;
    }
    matched
}

fn build_ordered_tick_matches(
    ordered_quotes: Vec<(usize, &QuoteSnapshot)>,
    ordered_ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Vec<(usize, Option<LastTrade>)> {
    let mut tick_index = 0usize;
    let mut current_tick = latest_tick;
    let mut matched = Vec::with_capacity(ordered_quotes.len());

    for (quote_index, quote) in ordered_quotes {
        while tick_index < ordered_ticks.len() {
            let candidate = ordered_ticks[tick_index].clone();
            if !tick_at_or_before_quote(quote, &candidate) {
                break;
            }
            current_tick = Some(candidate);
            tick_index += 1;
        }
        matched.push((quote_index, current_tick.clone()));
    }

    matched
}

fn malformed_flag_for_snapshot(
    malformed_bids: bool,
    malformed_asks: bool,
    crossed: bool,
    quote: &QuoteSnapshot,
) -> MalformedFlag {
    MalformedFlag(malformed_bids || malformed_asks || crossed || quote_is_malformed(quote))
}

fn spread_from_best_levels(best_bid: Option<Price>, best_ask: Option<Price>) -> Option<Price> {
    match (best_bid, best_ask) {
        (Some(bid), Some(ask)) => Some(Price(ask.0 - bid.0)),
        _ => None,
    }
}

pub fn build_dom_snapshot(
    symbol: &str,
    quote: &QuoteSnapshot,
    latest_tick: Option<LastTrade>,
) -> Result<DomSnapshot, DomError> {
    validate_symbol(symbol)?;
    let (bids, malformed_bids) = normalize_bid_levels(&quote.bid_levels);
    let (asks, malformed_asks) = normalize_ask_levels(&quote.offer_levels);
    let best_bid = bids.first().map(|level| level.price);
    let best_ask = asks.first().map(|level| level.price);
    let crossed = matches!((best_bid, best_ask), (Some(bid), Some(ask)) if bid > ask);
    let last_trade = merge_last_trade(quote.last_trade.clone(), latest_tick);
    let last_trade_price = merge_last_trade_price(quote.last_trade_price, last_trade.as_ref());
    let spread = spread_from_best_levels(best_bid, best_ask);

    Ok(DomSnapshot {
        symbol: symbol.to_owned(),
        timestamp: Some(quote.timestamp),
        last_trade_price,
        best_bid,
        best_ask,
        spread,
        total_bid_qty: quote.total_bid_qty,
        total_offer_qty: quote.total_offer_qty,
        bids,
        asks,
        last_trade,
        malformed: malformed_flag_for_snapshot(malformed_bids, malformed_asks, crossed, quote),
    })
}

pub fn match_ticks_to_quotes(
    quotes: &[QuoteSnapshot],
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Vec<Option<LastTrade>> {
    if quotes.is_empty() {
        return Vec::new();
    }

    let ordered_quotes = sort_quotes_for_matching(quotes);
    let ordered_ticks = sorted_ticks(ticks);
    let ordered_matches = build_ordered_tick_matches(ordered_quotes, &ordered_ticks, latest_tick);
    align_matched_ticks_to_input_order(quotes.len(), ordered_matches)
}

pub fn build_dom_batch(
    symbol: &str,
    quotes: &[QuoteSnapshot],
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Result<Vec<DomSnapshot>, DomError> {
    let matched_ticks = match_ticks_to_quotes(quotes, ticks, latest_tick);
    quotes
        .iter()
        .zip(matched_ticks)
        .map(|(quote, matched_tick)| build_dom_snapshot(symbol, quote, matched_tick))
        .collect()
}

pub fn build_dom_snapshot_from_parsed_quote(
    symbol: &str,
    parsed_quote: &ParsedQuoteSnapshot,
    latest_tick: Option<LastTrade>,
) -> Result<DomSnapshot, DomError> {
    build_dom_snapshot(symbol, &parsed_quote.quote, latest_tick)
}

pub fn build_dom_snapshot_from_raw_quote(
    symbol: &str,
    raw_quote: RawQuoteSnapshot,
    latest_tick: Option<LastTrade>,
) -> Result<DomSnapshot, DomError> {
    let parsed_quote = ParsedQuoteSnapshot::try_from(raw_quote)?;
    build_dom_snapshot_from_parsed_quote(symbol, &parsed_quote, latest_tick)
}

pub fn build_dom_batch_from_parsed_quotes(
    symbol: &str,
    parsed_quotes: &[ParsedQuoteSnapshot],
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Result<Vec<DomSnapshot>, DomError> {
    let quotes = parsed_quotes
        .iter()
        .map(|parsed_quote| parsed_quote.quote.clone())
        .collect::<Vec<_>>();
    build_dom_batch(symbol, &quotes, ticks, latest_tick)
}

pub fn build_dom_batch_from_raw_quotes(
    symbol: &str,
    raw_quotes: Vec<RawQuoteSnapshot>,
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Result<Vec<DomSnapshot>, DomError> {
    let parsed_quotes = raw_quotes
        .into_iter()
        .map(ParsedQuoteSnapshot::try_from)
        .collect::<Result<Vec<_>, _>>()?;
    build_dom_batch_from_parsed_quotes(symbol, &parsed_quotes, ticks, latest_tick)
}

#[cfg(test)]
mod tests {
    use super::*;
    use market_types::TickSide;
    use proptest::prelude::*;

    fn level(price: f64, volume: i64) -> PriceLevel {
        PriceLevel {
            price: Price::from_f64(price).expect("price"),
            volume: Volume::new(volume).expect("volume"),
        }
    }

    fn trade(price: f64, volume: i64, side: TickSide, timestamp: i64) -> LastTrade {
        LastTrade {
            price: Price::from_f64(price).expect("price"),
            volume: Volume::new(volume).expect("volume"),
            side,
            timestamp: EventTimestamp(timestamp),
        }
    }

    fn quote(timestamp: i64, bids: Vec<PriceLevel>, asks: Vec<PriceLevel>) -> QuoteSnapshot {
        QuoteSnapshot {
            symbol: market_types::Symbol::new("VN30F1M").expect("symbol"),
            timestamp: EventTimestamp(timestamp),
            total_bid_qty: Volume::new(bids.iter().map(|level| level.volume.0).sum())
                .expect("bid total"),
            total_offer_qty: Volume::new(asks.iter().map(|level| level.volume.0).sum())
                .expect("ask total"),
            bid_levels: bids,
            offer_levels: asks,
            last_trade_price: None,
            last_trade: None,
        }
    }

    fn raw_quote(timestamp: i64) -> RawQuoteSnapshot {
        RawQuoteSnapshot {
            symbol: "VN30F1M".to_string(),
            timestamp,
            datetime: None,
            total_bid_qty: 300,
            total_offer_qty: 330,
            bid_prices: vec![1251.4, 1251.3],
            bid_volumes: vec![100, 200],
            offer_prices: vec![1251.5, 1251.6],
            offer_volumes: vec![110, 220],
            last_trade_price: 1251.45,
            last_trade_volume: 12,
            last_trade_side: "B".to_string(),
            last_trade_timestamp: timestamp,
        }
    }

    fn source_payload(timestamp: i64) -> DomSourceQuotePayload {
        DomSourceQuotePayload {
            symbol: "VN30F1M".to_string(),
            timestamp,
            datetime: Some("2026-04-23T09:15:05+07:00".to_string()),
            total_bid_qty: 300,
            total_offer_qty: 330,
            bid_prices: vec![1251.4, 1251.3],
            bid_volumes: vec![100, 200],
            offer_prices: vec![1251.5, 1251.6],
            offer_volumes: vec![110, 220],
            last_trade_price: 1251.45,
            last_trade_volume: 12,
            last_trade_side: "B".to_string(),
            last_trade_timestamp: None,
        }
    }

    #[test]
    fn raw_quote_from_source_payload_defaults_trade_timestamp_to_quote_timestamp() {
        let raw_quote = build_raw_quote_from_source_payload(source_payload(1713417305));

        assert_eq!(raw_quote.timestamp, 1713417305);
        assert_eq!(raw_quote.last_trade_timestamp, 1713417305);
        assert_eq!(raw_quote.last_trade_price, 1251.45);
        assert_eq!(raw_quote.bid_prices, vec![1251.4, 1251.3]);
    }

    #[test]
    fn parsed_quote_from_source_payload_preserves_python_style_trade_price() {
        let parsed_quote = build_parsed_quote_from_source_payload(source_payload(1713417305))
            .expect("parsed quote");

        assert_eq!(
            parsed_quote.quote.last_trade_price.map(Price::as_f64),
            Some(1251.45)
        );
        assert_eq!(
            parsed_quote
                .quote
                .last_trade
                .as_ref()
                .map(|trade| trade.side),
            Some(TickSide::Buy)
        );
    }

    #[test]
    fn dom_snapshot_from_source_payload_reuses_raw_quote_ingress_semantics() {
        let snapshot =
            build_dom_snapshot_from_source_payload("VN30F1M", source_payload(1713417305), None)
                .expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1713417305)
        );
        assert_eq!(snapshot.bids.len(), 2);
        assert_eq!(snapshot.asks.len(), 2);
    }

    #[test]
    fn dom_batch_from_source_payloads_preserves_input_order() {
        let payloads = vec![source_payload(1002), source_payload(1001)];
        let ticks = vec![
            trade(1251.1, 7, TickSide::Buy, 1002),
            trade(1251.0, 5, TickSide::Sell, 1001),
        ];

        let batch =
            build_dom_batch_from_source_payloads("VN30F1M", payloads, &ticks, None).expect("batch");

        assert_eq!(batch.len(), 2);
        assert_eq!(batch[0].timestamp.map(|timestamp| timestamp.0), Some(1002));
        assert_eq!(
            batch[0].last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1002)
        );
        assert_eq!(batch[1].timestamp.map(|timestamp| timestamp.0), Some(1001));
        assert_eq!(
            batch[1].last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1001)
        );
    }

    #[test]
    fn source_payload_without_symbol_fails_at_parse_boundary() {
        let mut payload = source_payload(1713417305);
        payload.symbol.clear();

        let error = build_dom_snapshot_from_source_payload("VN30F1M", payload, None)
            .expect_err("empty source symbol should fail");

        assert_eq!(error, DomError::Parse(ParseError::EmptySymbol));
    }

    #[test]
    fn dom_snapshot_from_raw_quote_preserves_python_style_trade_price_and_levels() {
        let snapshot = build_dom_snapshot_from_raw_quote("VN30F1M", raw_quote(1713417305), None)
            .expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.side),
            Some(TickSide::Buy)
        );
        assert_eq!(snapshot.bids.len(), 2);
        assert_eq!(snapshot.asks.len(), 2);
    }

    #[test]
    fn dom_snapshot_from_parsed_quote_keeps_partial_trade_price_without_full_trade() {
        let mut raw_quote = raw_quote(1713417305);
        raw_quote.last_trade_side = "X".to_string();
        let parsed_quote = ParsedQuoteSnapshot::try_from(raw_quote).expect("parsed quote");

        let snapshot =
            build_dom_snapshot_from_parsed_quote("VN30F1M", &parsed_quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert!(snapshot.last_trade.is_none());
    }

    #[test]
    fn dom_batch_from_raw_quotes_uses_tick_matching_and_input_order() {
        let raw_quotes = vec![raw_quote(1002), raw_quote(1001)];
        let ticks = vec![
            trade(1251.1, 7, TickSide::Buy, 1002),
            trade(1251.0, 5, TickSide::Sell, 1001),
        ];

        let batch =
            build_dom_batch_from_raw_quotes("VN30F1M", raw_quotes, &ticks, None).expect("batch");

        assert_eq!(batch.len(), 2);
        assert_eq!(batch[0].timestamp.map(|timestamp| timestamp.0), Some(1002));
        assert_eq!(
            batch[0].last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1002)
        );
        assert_eq!(batch[1].timestamp.map(|timestamp| timestamp.0), Some(1001));
        assert_eq!(
            batch[1].last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1001)
        );
    }

    #[test]
    fn dom_snapshot_preserves_quote_levels_in_canonical_order() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(
                1713417305,
                vec![level(1251.4, 100), level(1251.3, 200)],
                vec![level(1251.5, 110), level(1251.6, 220)],
            ),
            None,
        )
        .expect("snapshot");

        assert_eq!(snapshot.bids[0].price.as_f64(), 1251.4);
        assert_eq!(snapshot.bids[1].price.as_f64(), 1251.3);
        assert_eq!(snapshot.asks[0].price.as_f64(), 1251.5);
        assert_eq!(snapshot.asks[1].price.as_f64(), 1251.6);
        assert_eq!(snapshot.best_bid.map(Price::as_f64), Some(1251.4));
        assert_eq!(snapshot.best_ask.map(Price::as_f64), Some(1251.5));
        assert_eq!(snapshot.spread.map(Price::as_f64), Some(0.1));
        assert!(!snapshot.malformed.0);
    }

    #[test]
    fn dom_snapshot_merges_latest_tick_without_changing_quote_levels() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(
                1713417305,
                vec![level(1251.4, 100), level(1251.3, 200)],
                vec![level(1251.5, 110), level(1251.6, 220)],
            ),
            Some(trade(1251.5, 12, TickSide::Buy, 1713417306)),
        )
        .expect("snapshot");

        assert_eq!(snapshot.bids.len(), 2);
        assert_eq!(snapshot.asks.len(), 2);
        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.5));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|tick| tick.volume.0),
            Some(12)
        );
        assert_eq!(
            snapshot.last_trade.as_ref().map(|tick| tick.side),
            Some(TickSide::Buy)
        );
    }

    #[test]
    fn dom_snapshot_preserves_quote_last_trade_price_when_no_latest_tick_exists() {
        let mut quote = quote(
            1713417305,
            vec![level(1251.4, 100), level(1251.3, 200)],
            vec![level(1251.5, 110), level(1251.6, 220)],
        );
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert!(snapshot.last_trade.is_none());
        assert!(!snapshot.malformed.0);
    }

    #[test]
    fn dom_snapshot_flags_partial_quote_trade_state_as_malformed() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade = Some(trade(1251.5, 10, TickSide::Buy, 1000));
        quote.last_trade_price = None;

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert!(snapshot.malformed.0);
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.volume.0),
            Some(10)
        );
    }

    #[test]
    fn dom_batch_matches_ticks_by_timestamp_but_preserves_input_quote_order() {
        let quotes = vec![
            quote(1002, vec![level(1251.0, 95)], vec![level(1251.1, 96)]),
            quote(1001, vec![level(1250.9, 90)], vec![level(1251.0, 91)]),
        ];
        let ticks = vec![
            trade(1251.1, 7, TickSide::Buy, 1002),
            trade(1251.0, 5, TickSide::Sell, 1001),
        ];

        let matched = match_ticks_to_quotes(&quotes, &ticks, None);

        assert_eq!(matched.len(), 2);
        assert_eq!(matched[0].as_ref().map(|tick| tick.timestamp.0), Some(1002));
        assert_eq!(matched[1].as_ref().map(|tick| tick.timestamp.0), Some(1001));
    }

    #[test]
    fn dom_batch_uses_latest_tick_fallback_when_quotes_arrive_out_of_order() {
        let quotes = vec![
            quote(1002, vec![level(1251.0, 95)], vec![level(1251.1, 96)]),
            quote(999, vec![level(1250.7, 80)], vec![level(1250.8, 81)]),
        ];
        let ticks = vec![trade(1251.1, 7, TickSide::Buy, 1002)];
        let latest_tick = Some(trade(1250.8, 4, TickSide::Sell, 998));

        let matched = match_ticks_to_quotes(&quotes, &ticks, latest_tick);

        assert_eq!(matched[0].as_ref().map(|tick| tick.timestamp.0), Some(1002));
        assert_eq!(matched[1].as_ref().map(|tick| tick.timestamp.0), Some(998));
    }

    #[test]
    fn dom_snapshot_latest_tick_overrides_quote_last_trade_price() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));

        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote,
            Some(trade(1251.55, 8, TickSide::Sell, 1001)),
        )
        .expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.55));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.side),
            Some(TickSide::Sell)
        );
    }

    #[test]
    fn dom_snapshot_handles_quote_trade_price_without_full_last_trade() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert!(snapshot.last_trade.is_none());
    }

    #[test]
    fn dom_snapshot_keeps_quote_last_trade_when_no_latest_tick_arrives() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.5).expect("price"));
        quote.last_trade = Some(trade(1251.5, 10, TickSide::Buy, 1000));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.5));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1000)
        );
    }

    #[test]
    fn dom_snapshot_latest_tick_can_fill_missing_quote_trade_price() {
        let quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);

        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote,
            Some(trade(1251.55, 8, TickSide::Sell, 1001)),
        )
        .expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.55));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.volume.0),
            Some(8)
        );
    }

    #[test]
    fn dom_snapshot_marks_crossed_and_partial_trade_state_together() {
        let mut quote = quote(1000, vec![level(1251.6, 100)], vec![level(1251.5, 90)]);
        quote.last_trade = Some(trade(1251.55, 9, TickSide::Buy, 1000));
        quote.last_trade_price = None;

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert!(snapshot.malformed.0);
        assert_eq!(snapshot.best_bid.map(Price::as_f64), Some(1251.6));
        assert_eq!(snapshot.best_ask.map(Price::as_f64), Some(1251.5));
    }

    #[test]
    fn dom_snapshot_spread_can_be_negative_for_crossed_book() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(1000, vec![level(1251.6, 100)], vec![level(1251.5, 90)]),
            None,
        )
        .expect("snapshot");

        assert_eq!(snapshot.spread.map(Price::as_f64), Some(-0.1));
        assert!(snapshot.malformed.0);
    }

    #[test]
    fn dom_snapshot_preserves_zero_quote_trade_price() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(0.0).expect("price"));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(0.0));
    }

    #[test]
    fn dom_snapshot_treats_existing_quote_trade_without_scalar_price_as_malformed() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade = Some(trade(1251.5, 10, TickSide::Buy, 1000));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert!(snapshot.malformed.0);
    }

    #[test]
    fn dom_batch_matching_handles_unsorted_ticks() {
        let quotes = vec![
            quote(1001, vec![level(1250.9, 90)], vec![level(1251.0, 91)]),
            quote(1002, vec![level(1251.0, 95)], vec![level(1251.1, 96)]),
        ];
        let ticks = vec![
            trade(1251.1, 7, TickSide::Buy, 1002),
            trade(1251.0, 5, TickSide::Sell, 1001),
        ];

        let matched = match_ticks_to_quotes(&quotes, &ticks, None);

        assert_eq!(matched[0].as_ref().map(|tick| tick.timestamp.0), Some(1001));
        assert_eq!(matched[1].as_ref().map(|tick| tick.timestamp.0), Some(1002));
    }

    #[test]
    fn dom_batch_returns_empty_for_empty_quotes() {
        let matched = match_ticks_to_quotes(&[], &[trade(1251.0, 5, TickSide::Sell, 1001)], None);
        assert!(matched.is_empty());
    }

    #[test]
    fn dom_snapshot_uses_quote_trade_price_even_when_quote_trade_is_missing() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
        assert!(snapshot.last_trade.is_none());
    }

    #[test]
    fn dom_snapshot_quote_trade_price_remains_when_latest_tick_is_absent() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));

        let snapshot = build_dom_snapshot("VN30F1M", &quote, None).expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.45));
    }

    #[test]
    fn dom_snapshot_latest_tick_replaces_quote_trade_struct_and_scalar() {
        let mut quote = quote(1000, vec![level(1251.4, 100)], vec![level(1251.5, 110)]);
        quote.last_trade_price = Some(Price::from_f64(1251.45).expect("price"));
        quote.last_trade = Some(trade(1251.45, 5, TickSide::Buy, 1000));

        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote,
            Some(trade(1251.55, 8, TickSide::Sell, 1001)),
        )
        .expect("snapshot");

        assert_eq!(snapshot.last_trade_price.map(Price::as_f64), Some(1251.55));
        assert_eq!(
            snapshot.last_trade.as_ref().map(|trade| trade.timestamp.0),
            Some(1001)
        );
    }

    #[test]
    fn dom_batch_matching_uses_latest_tick_before_earliest_tick() {
        let quotes = vec![quote(
            1001,
            vec![level(1250.9, 90)],
            vec![level(1251.0, 91)],
        )];
        let ticks = vec![trade(1251.1, 7, TickSide::Buy, 1002)];
        let latest_tick = Some(trade(1250.8, 4, TickSide::Sell, 999));

        let matched = match_ticks_to_quotes(&quotes, &ticks, latest_tick);

        assert_eq!(matched[0].as_ref().map(|tick| tick.timestamp.0), Some(999));
    }

    #[test]
    fn dom_snapshot_keeps_zero_volume_levels_like_python_oracle() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(
                1713417305,
                vec![level(1251.4, 0), level(1251.3, 200)],
                vec![level(1251.5, 0), level(1251.6, 220)],
            ),
            None,
        )
        .expect("snapshot");

        assert_eq!(snapshot.bids.len(), 2);
        assert_eq!(snapshot.asks.len(), 2);
        assert_eq!(snapshot.bids[0].volume.0, 0);
        assert_eq!(snapshot.asks[0].volume.0, 0);
    }

    #[test]
    fn dom_batch_matches_latest_tick_at_or_before_quote_timestamp() {
        let quotes = vec![
            quote(1001, vec![level(1250.9, 90)], vec![level(1251.0, 91)]),
            quote(1002, vec![level(1251.0, 95)], vec![level(1251.1, 96)]),
        ];
        let ticks = vec![
            trade(1251.0, 5, TickSide::Sell, 1001),
            trade(1251.1, 7, TickSide::Buy, 1002),
        ];

        let batch = build_dom_batch("VN30F1M", &quotes, &ticks, None).expect("batch");
        assert_eq!(
            batch[0].last_trade.as_ref().map(|tick| tick.side),
            Some(TickSide::Sell)
        );
        assert_eq!(
            batch[0].last_trade.as_ref().map(|tick| tick.volume.0),
            Some(5)
        );
        assert_eq!(
            batch[1].last_trade.as_ref().map(|tick| tick.side),
            Some(TickSide::Buy)
        );
        assert_eq!(
            batch[1].last_trade.as_ref().map(|tick| tick.timestamp.0),
            Some(1002)
        );
    }

    #[test]
    fn dom_snapshot_flags_crossed_book_as_malformed() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(1000, vec![level(1251.6, 100)], vec![level(1251.5, 90)]),
            None,
        )
        .expect("snapshot");
        assert!(snapshot.malformed.0);
    }

    #[test]
    fn dom_snapshot_preserves_input_order_and_flags_bad_bid_order() {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(
                1000,
                vec![level(1251.3, 100), level(1251.4, 90)],
                vec![level(1251.5, 80), level(1251.6, 70)],
            ),
            None,
        )
        .expect("snapshot");

        assert_eq!(
            snapshot
                .bids
                .iter()
                .map(|level| level.price.as_f64())
                .collect::<Vec<_>>(),
            vec![1251.3, 1251.4]
        );
        assert!(snapshot.malformed.0);
    }

    #[test]
    fn dom_batch_uses_latest_tick_fallback_for_early_quotes() {
        let quotes = vec![
            quote(1001, vec![level(1250.9, 90)], vec![level(1251.0, 91)]),
            quote(1002, vec![level(1251.0, 95)], vec![level(1251.1, 96)]),
        ];
        let ticks = vec![trade(1251.1, 7, TickSide::Buy, 1002)];
        let latest_tick = Some(trade(1250.8, 4, TickSide::Sell, 999));

        let batch = build_dom_batch("VN30F1M", &quotes, &ticks, latest_tick).expect("batch");
        assert_eq!(
            batch[0].last_trade.as_ref().map(|tick| tick.side),
            Some(TickSide::Sell)
        );
        assert_eq!(
            batch[0].last_trade.as_ref().map(|tick| tick.timestamp.0),
            Some(999)
        );
        assert_eq!(
            batch[1].last_trade.as_ref().map(|tick| tick.side),
            Some(TickSide::Buy)
        );
        assert_eq!(
            batch[1].last_trade.as_ref().map(|tick| tick.timestamp.0),
            Some(1002)
        );
    }

    proptest! {
        #[test]
        fn dom_snapshot_never_fabricates_levels_from_empty_inputs(timestamp in 0i64..10_000i64) {
            let snapshot = build_dom_snapshot("VN30F1M", &quote(timestamp, Vec::new(), Vec::new()), None)
                .expect("snapshot");
            prop_assert!(snapshot.bids.is_empty());
            prop_assert!(snapshot.asks.is_empty());
            prop_assert!(snapshot.best_bid.is_none());
            prop_assert!(snapshot.best_ask.is_none());
        }
    }
}
