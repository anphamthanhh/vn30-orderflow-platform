//! Serializable DOM snapshot and batch views for the wire and the UI.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use market_dom_engine::{
    build_dom_batch_from_source_payloads, build_dom_snapshot_from_source_payload, DomSnapshot,
    DomSourceQuotePayload,
};
use market_types::{LastTrade, ParsedQuoteSnapshot, PriceLevel};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DomLevelView {
    pub price: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DomLastTradeView {
    pub price: f64,
    pub volume: f64,
    pub side: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DomSnapshotView {
    pub r#type: String,
    pub symbol: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_sequence: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub datetime: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_trade_price: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_bid: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_ask: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spread: Option<f64>,
    pub total_bid_qty: i64,
    pub total_offer_qty: i64,
    pub bids: Vec<DomLevelView>,
    pub asks: Vec<DomLevelView>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_trade: Option<DomLastTradeView>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DomBatchView {
    pub r#type: String,
    pub symbol: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_sequence: Option<u64>,
    pub snapshots: Vec<DomSnapshotView>,
}

fn level_view(level: &PriceLevel) -> DomLevelView {
    DomLevelView {
        price: level.price.as_f64(),
        volume: level.volume.0 as f64,
    }
}

fn side_view(side: market_types::TickSide) -> &'static str {
    match side {
        market_types::TickSide::Buy => "B",
        market_types::TickSide::Sell => "S",
    }
}

fn last_trade_view(last_trade: &LastTrade) -> DomLastTradeView {
    DomLastTradeView {
        price: last_trade.price.as_f64(),
        volume: last_trade.volume.0 as f64,
        side: side_view(last_trade.side).to_owned(),
        timestamp: Some(last_trade.timestamp.0),
    }
}

pub fn build_dom_snapshot_view(
    snapshot: &DomSnapshot,
    parsed_quote: Option<&ParsedQuoteSnapshot>,
) -> DomSnapshotView {
    DomSnapshotView {
        r#type: String::from("dom_snapshot"),
        symbol: snapshot.symbol.clone(),
        timestamp: snapshot.timestamp.map(|timestamp| timestamp.0),
        event_sequence: None,
        datetime: parsed_quote
            .and_then(|parsed_quote| parsed_quote.datetime.as_ref())
            .map(chrono::DateTime::to_rfc3339),
        last_trade_price: snapshot.last_trade_price.map(|price| price.as_f64()),
        best_bid: snapshot.best_bid.map(|price| price.as_f64()),
        best_ask: snapshot.best_ask.map(|price| price.as_f64()),
        spread: snapshot.spread.map(|price| price.as_f64()),
        total_bid_qty: snapshot.total_bid_qty.0,
        total_offer_qty: snapshot.total_offer_qty.0,
        bids: snapshot.bids.iter().map(level_view).collect(),
        asks: snapshot.asks.iter().map(level_view).collect(),
        last_trade: snapshot.last_trade.as_ref().map(last_trade_view),
    }
}

pub fn build_dom_snapshot_view_from_source_payload(
    symbol: &str,
    payload: DomSourceQuotePayload,
    latest_tick: Option<LastTrade>,
) -> Result<DomSnapshotView, market_dom_engine::DomError> {
    let parsed_quote = market_dom_engine::build_parsed_quote_from_source_payload(payload.clone())?;
    let snapshot = build_dom_snapshot_from_source_payload(symbol, payload, latest_tick)?;
    Ok(build_dom_snapshot_view(&snapshot, Some(&parsed_quote)))
}

pub fn build_dom_batch_view_from_source_payloads(
    symbol: &str,
    payloads: Vec<DomSourceQuotePayload>,
    ticks: &[LastTrade],
    latest_tick: Option<LastTrade>,
) -> Result<DomBatchView, market_dom_engine::DomError> {
    let parsed_quotes = payloads
        .iter()
        .cloned()
        .map(market_dom_engine::build_parsed_quote_from_source_payload)
        .collect::<Result<Vec<_>, _>>()?;
    let snapshots = build_dom_batch_from_source_payloads(symbol, payloads, ticks, latest_tick)?;
    let views = snapshots
        .iter()
        .zip(parsed_quotes.iter())
        .map(|(snapshot, parsed_quote)| build_dom_snapshot_view(snapshot, Some(parsed_quote)))
        .collect();

    Ok(DomBatchView {
        r#type: String::from("dom_batch"),
        symbol: symbol.to_owned(),
        event_sequence: None,
        snapshots: views,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use market_types::TickSide;

    fn source_payload(timestamp: i64) -> DomSourceQuotePayload {
        DomSourceQuotePayload {
            symbol: "VN30F1M".to_string(),
            timestamp,
            datetime: Some("2026-04-19T09:15:05+07:00".to_string()),
            total_bid_qty: 300,
            total_offer_qty: 330,
            bid_prices: vec![1251.4, 1251.3],
            bid_volumes: vec![100, 200],
            offer_prices: vec![1251.5, 1251.6],
            offer_volumes: vec![110, 220],
            last_trade_price: 1251.45,
            last_trade_volume: 12,
            last_trade_side: "B".to_string(),
            last_trade_timestamp: Some(timestamp),
        }
    }

    fn trade(price: f64, volume: i64, side: TickSide, timestamp: i64) -> LastTrade {
        LastTrade {
            price: market_types::Price::from_f64(price).expect("price"),
            volume: market_types::Volume::new(volume).expect("volume"),
            side,
            timestamp: market_types::EventTimestamp(timestamp),
        }
    }

    #[test]
    fn snapshot_view_preserves_python_payload_shape() {
        let view = build_dom_snapshot_view_from_source_payload(
            "VN30F1M",
            source_payload(1713608105),
            None,
        )
        .expect("view");

        assert_eq!(view.r#type, "dom_snapshot");
        assert_eq!(view.symbol, "VN30F1M");
        assert_eq!(view.timestamp, Some(1713608105));
        assert_eq!(view.datetime.as_deref(), Some("2026-04-19T09:15:05+07:00"));
        assert_eq!(view.last_trade_price, Some(1251.45));
        assert_eq!(view.bids[0].price, 1251.4);
        assert_eq!(view.bids[0].volume, 100.0);
        assert_eq!(
            view.last_trade.as_ref().map(|trade| trade.side.clone()),
            Some("B".to_owned())
        );
    }

    #[test]
    fn snapshot_view_uses_latest_tick_for_trade_sidecar() {
        let view = build_dom_snapshot_view_from_source_payload(
            "VN30F1M",
            source_payload(1713608105),
            Some(trade(1251.5, 15, TickSide::Sell, 1713608106)),
        )
        .expect("view");

        assert_eq!(view.last_trade_price, Some(1251.5));
        assert_eq!(
            view.last_trade.as_ref().map(|trade| trade.side.clone()),
            Some("S".to_owned())
        );
        assert_eq!(
            view.last_trade.as_ref().and_then(|trade| trade.timestamp),
            Some(1713608106)
        );
    }

    #[test]
    fn batch_view_preserves_input_order() {
        let payloads = vec![source_payload(1002), source_payload(1001)];
        let ticks = vec![
            trade(1251.1, 7, TickSide::Buy, 1002),
            trade(1251.0, 5, TickSide::Sell, 1001),
        ];

        let batch = build_dom_batch_view_from_source_payloads("VN30F1M", payloads, &ticks, None)
            .expect("batch");

        assert_eq!(batch.r#type, "dom_batch");
        assert_eq!(batch.snapshots.len(), 2);
        assert_eq!(batch.snapshots[0].timestamp, Some(1002));
        assert_eq!(
            batch.snapshots[0]
                .last_trade
                .as_ref()
                .and_then(|trade| trade.timestamp),
            Some(1002)
        );
        assert_eq!(batch.snapshots[1].timestamp, Some(1001));
        assert_eq!(
            batch.snapshots[1]
                .last_trade
                .as_ref()
                .and_then(|trade| trade.timestamp),
            Some(1001)
        );
    }

    #[test]
    fn snapshot_view_keeps_explicit_last_trade_timestamp_from_source_payload() {
        let mut payload = source_payload(1713608105);
        payload.last_trade_timestamp = Some(1713608104);

        let view =
            build_dom_snapshot_view_from_source_payload("VN30F1M", payload, None).expect("view");

        assert_eq!(
            view.last_trade.as_ref().and_then(|trade| trade.timestamp),
            Some(1713608104)
        );
    }

    #[test]
    fn snapshot_view_drops_blank_datetime_from_source_payload() {
        let mut payload = source_payload(1713608105);
        payload.datetime = Some("   ".to_string());

        let view =
            build_dom_snapshot_view_from_source_payload("VN30F1M", payload, None).expect("view");

        assert!(view.datetime.is_none());
    }

    #[test]
    fn snapshot_view_truncates_mismatched_depth_arrays_via_typed_parsing() {
        let mut payload = source_payload(1713608105);
        payload.bid_prices = vec![1251.4, 1251.3, 1251.2];
        payload.bid_volumes = vec![100, 200];
        payload.offer_prices = vec![1251.5, 1251.6, 1251.7];
        payload.offer_volumes = vec![110];

        let view =
            build_dom_snapshot_view_from_source_payload("VN30F1M", payload, None).expect("view");

        assert_eq!(view.bids.len(), 2);
        assert_eq!(view.asks.len(), 1);
        assert_eq!(view.bids[1].price, 1251.3);
        assert_eq!(view.asks[0].price, 1251.5);
    }

    #[test]
    fn snapshot_view_rejects_invalid_datetime_at_typed_boundary() {
        let mut payload = source_payload(1713608105);
        payload.datetime = Some("not-a-datetime".to_string());

        let error = build_dom_snapshot_view_from_source_payload("VN30F1M", payload, None)
            .expect_err("invalid datetime should fail typed boundary");

        assert!(matches!(
            error,
            market_dom_engine::DomError::Parse(market_types::ParseError::InvalidDateTime)
        ));
    }

    #[test]
    fn snapshot_view_drops_non_finite_trade_price_but_keeps_levels() {
        let mut payload = source_payload(1713608105);
        payload.last_trade_price = f64::NAN;

        let view =
            build_dom_snapshot_view_from_source_payload("VN30F1M", payload, None).expect("view");

        assert!(view.last_trade_price.is_none());
        assert!(view.last_trade.is_none());
        assert_eq!(view.bids.len(), 2);
        assert_eq!(view.asks.len(), 2);
    }
}
