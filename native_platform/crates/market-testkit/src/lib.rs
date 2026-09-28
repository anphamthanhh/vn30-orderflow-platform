//! JSON tick-fixture loader for tests.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use market_types::{EventTimestamp, ParseError, Price, Symbol, Tick, TickSide, Volume};
use serde::Deserialize;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum TestkitError {
    #[error("json parse failed: {0}")]
    Json(#[from] serde_json::Error),
    #[error("domain parse failed: {0}")]
    Domain(#[from] ParseError),
}

#[derive(Debug, Clone, Deserialize)]
pub struct TickFixture {
    pub symbol: String,
    pub timestamp: i64,
    pub price: f64,
    pub volume: i64,
    pub side: Option<String>,
}

impl TickFixture {
    pub fn into_tick(self) -> Result<Tick, TestkitError> {
        Ok(Tick {
            symbol: Symbol::new(self.symbol)?,
            timestamp: EventTimestamp::new(self.timestamp)?,
            price: Price::from_f64(self.price)?,
            volume: Volume::new(self.volume)?,
            side: parse_side(self.side),
        })
    }
}

pub fn load_tick_fixtures(json: &str) -> Result<Vec<TickFixture>, TestkitError> {
    serde_json::from_str(json).map_err(TestkitError::from)
}

fn parse_side(value: Option<String>) -> Option<TickSide> {
    match value.as_deref().map(str::trim).map(str::to_ascii_uppercase) {
        Some(side) if side == "B" || side == "BUY" => Some(TickSide::Buy),
        Some(side) if side == "S" || side == "SELL" => Some(TickSide::Sell),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn loads_tick_fixture_and_maps_to_domain() {
        let raw = r#"[
            {"symbol":"VN30F1M","timestamp":1713417306,"price":1251.5,"volume":12,"side":"B"}
        ]"#;
        let fixtures = load_tick_fixtures(raw).expect("fixture json should parse");
        let tick = fixtures
            .into_iter()
            .next()
            .expect("one fixture")
            .into_tick()
            .expect("tick should parse");
        assert_eq!(tick.symbol.as_str(), "VN30F1M");
        assert_eq!(tick.price.as_f64(), 1251.5);
    }

    proptest! {
        #[test]
        fn price_round_trip_stays_finite(value in -100000.0f64..100000.0f64) {
            let price = Price::from_f64(value).expect("finite range should parse");
            prop_assert!(price.as_f64().is_finite());
        }

        #[test]
        fn fixture_to_tick_rejects_negative_boundaries(
            symbol in "\\PC+",
            negative_timestamp in i64::MIN..0,
            negative_volume in i64::MIN..0,
            side in prop::option::of(".*"),
        ) {
            let timestamp_result = TickFixture {
                symbol: symbol.clone(),
                timestamp: negative_timestamp,
                price: 1.0,
                volume: 1,
                side: side.clone(),
            }.into_tick();
            prop_assert!(timestamp_result.is_err());

            let volume_result = TickFixture {
                symbol,
                timestamp: 1,
                price: 1.0,
                volume: negative_volume,
                side,
            }.into_tick();
            prop_assert!(volume_result.is_err());
        }
    }
}
