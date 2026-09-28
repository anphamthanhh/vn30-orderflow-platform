//! Serializable order-flow views (CVD, level imbalance) for the UI.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use orderflow_engine::cvd::CvdPoint;
use orderflow_engine::imbalance::LevelImbalance;
use serde::{Deserialize, Serialize};

/// View-safe CVD point (all f64/i64, no engine types).
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CvdPointView {
    pub timestamp: i64,
    pub value: f64,
}

/// View-safe level imbalance (all f64).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LevelImbalanceView {
    pub price: f64,
    pub bid_vol: f64,
    pub ask_vol: f64,
    pub ratio: f64,
    pub direction: String, // "bid", "ask", "neutral"
}

/// Order flow state bundle for the UI.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderFlowStateView {
    pub delta: f64,
    pub bar_delta: f64,
    pub bar_buy_vol: f64,
    pub bar_sell_vol: f64,
    pub cvd: f64,
    pub cvd_history: Vec<CvdPointView>,
    pub imbalances: Vec<LevelImbalanceView>,
    pub stacked_count: usize,
}

impl From<&CvdPoint> for CvdPointView {
    fn from(p: &CvdPoint) -> Self {
        CvdPointView {
            timestamp: p.timestamp,
            value: p.value,
        }
    }
}

impl From<&LevelImbalance> for LevelImbalanceView {
    fn from(im: &LevelImbalance) -> Self {
        let direction = match im.direction {
            orderflow_engine::imbalance::ImbalanceDirection::BidHeavy => "bid",
            orderflow_engine::imbalance::ImbalanceDirection::AskHeavy => "ask",
            orderflow_engine::imbalance::ImbalanceDirection::Neutral => "neutral",
        };
        LevelImbalanceView {
            price: im.price,
            bid_vol: im.bid_vol,
            ask_vol: im.ask_vol,
            ratio: im.ratio,
            direction: direction.to_owned(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use orderflow_engine::cvd::CvdPoint;
    use orderflow_engine::imbalance::{ImbalanceDirection, LevelImbalance};

    #[test]
    fn cvd_point_view_from_engine_point() {
        let pt = CvdPoint {
            timestamp: 1000,
            value: 42.5,
        };
        let view = CvdPointView::from(&pt);
        assert_eq!(view.timestamp, 1000);
        assert_eq!(view.value, 42.5);
    }

    #[test]
    fn imbalance_view_maps_direction_to_string() {
        let im = LevelImbalance {
            price: 1251.3,
            bid_vol: 200.0,
            ask_vol: 50.0,
            ratio: 0.75,
            direction: ImbalanceDirection::BidHeavy,
        };
        let view = LevelImbalanceView::from(&im);
        assert_eq!(view.direction, "bid");
        assert_eq!(view.price, 1251.3);
    }

    #[test]
    fn order_flow_state_view_serializes() {
        let state = OrderFlowStateView {
            delta: 0.0,
            bar_delta: 0.0,
            bar_buy_vol: 0.0,
            bar_sell_vol: 0.0,
            cvd: 0.0,
            cvd_history: Vec::new(),
            imbalances: Vec::new(),
            stacked_count: 0,
        };
        let json = serde_json::to_string(&state).expect("serialize");
        assert!(json.contains("\"cvd\":0.0"));
    }
}
