//! Golden tests: `<case>.input.json` quote sequences against frozen `<case>.expected.json` output.
//!
//! The expected files record current behaviour; regenerate them with `DOM_GOLDEN_REGEN=1`. Prices
//! are compared at the 0.1 tick, volumes and timestamps exactly.

use market_dom_engine::{build_dom_batch_from_source_payloads, DomSnapshot, DomSourceQuotePayload};
use market_types::{EventTimestamp, LastTrade, Price, TickSide, Volume};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;

#[derive(Deserialize)]
struct Fixture {
    symbol: String,
    payloads: Vec<DomSourceQuotePayload>,
    #[serde(default)]
    ticks: Vec<FixtureTick>,
    #[serde(default)]
    latest_tick: Option<FixtureTick>,
}

#[derive(Deserialize)]
struct FixtureTick {
    price: f64,
    volume: i64,
    side: String,
    timestamp: i64,
}

impl FixtureTick {
    fn to_last_trade(&self) -> LastTrade {
        LastTrade {
            price: Price::from_f64(self.price).expect("fixture tick price"),
            volume: Volume::new(self.volume).expect("fixture tick volume"),
            side: match self.side.as_str() {
                "B" => TickSide::Buy,
                "S" => TickSide::Sell,
                other => panic!("fixture tick side must be B or S, got {other:?}"),
            },
            timestamp: EventTimestamp(self.timestamp),
        }
    }
}

/// Round to the 0.1 tick; lossless, as every fixture price is on the grid.
fn round_tick(price: Price) -> f64 {
    (price.as_f64() * 10.0).round() / 10.0
}

fn trade_to_value(trade: &LastTrade) -> Value {
    json!({
        "price": round_tick(trade.price),
        "volume": trade.volume.0,
        "side": match trade.side { TickSide::Buy => "Buy", TickSide::Sell => "Sell" },
        "timestamp": trade.timestamp.0,
    })
}

fn snapshot_to_value(snapshot: &DomSnapshot) -> Value {
    json!({
        "symbol": snapshot.symbol,
        "timestamp": snapshot.timestamp.map(|ts| ts.0),
        "last_trade_price": snapshot.last_trade_price.map(round_tick),
        "best_bid": snapshot.best_bid.map(round_tick),
        "best_ask": snapshot.best_ask.map(round_tick),
        "spread": snapshot.spread.map(round_tick),
        "total_bid_qty": snapshot.total_bid_qty.0,
        "total_offer_qty": snapshot.total_offer_qty.0,
        "bids": snapshot
            .bids
            .iter()
            .map(|lvl| json!({"price": round_tick(lvl.price), "volume": lvl.volume.0}))
            .collect::<Vec<_>>(),
        "asks": snapshot
            .asks
            .iter()
            .map(|lvl| json!({"price": round_tick(lvl.price), "volume": lvl.volume.0}))
            .collect::<Vec<_>>(),
        "last_trade": snapshot.last_trade.as_ref().map(trade_to_value),
        "malformed": snapshot.malformed.0,
    })
}

fn fixture_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(name)
}

fn run_golden(case: &str) {
    let input_raw = fs::read_to_string(fixture_path(&format!("{case}.input.json")))
        .expect("read input fixture");
    let fixture: Fixture = serde_json::from_str(&input_raw).expect("parse input fixture");

    let ticks: Vec<LastTrade> = fixture
        .ticks
        .iter()
        .map(FixtureTick::to_last_trade)
        .collect();
    let latest_tick = fixture.latest_tick.as_ref().map(FixtureTick::to_last_trade);

    let batch = build_dom_batch_from_source_payloads(
        &fixture.symbol,
        fixture.payloads,
        &ticks,
        latest_tick,
    )
    .expect("golden fixtures must build cleanly");
    let actual = Value::Array(batch.iter().map(snapshot_to_value).collect());

    let expected_path = fixture_path(&format!("{case}.expected.json"));
    if std::env::var("DOM_GOLDEN_REGEN").is_ok() {
        let mut serialized = serde_json::to_string_pretty(&actual).expect("serialize expected");
        serialized.push('\n');
        fs::write(&expected_path, serialized).expect("write expected fixture");
        return;
    }

    let expected: Value = serde_json::from_str(
        &fs::read_to_string(&expected_path)
            .expect("read expected fixture (regen with DOM_GOLDEN_REGEN=1)"),
    )
    .expect("parse expected fixture");

    assert_eq!(actual, expected, "golden mismatch for case {case}");
}

/// A full 10-level book over three quotes with matching ticks.
#[test]
fn golden_full_book_build() {
    run_golden("full_book_build");
}

/// Shrinking depth, zero-volume rungs, disordered, crossed and one-sided books, and sparse
/// ticks with a stale `latest_tick` fallback.
#[test]
fn golden_partial_updates_removals() {
    run_golden("partial_updates_removals");
}

/// One-sided and empty books; an invalid trade side drops the trade but keeps its price.
#[test]
fn golden_one_sided_book() {
    run_golden("one_sided_book");
}

/// Quotes sharing one timestamp all take the last equal-timestamp tick; a future `latest_tick`
/// loses to stream ticks.
#[test]
fn golden_burst_same_price() {
    run_golden("burst_same_price");
}
