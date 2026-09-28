//! Property tests for `orderflow_engine::obi::WeightedObi`.
//!
//! Covers the [-1, 1] bound with invalid volumes and agreement with an independent shadow model.

use orderflow_engine::obi::WeightedObi;
use proptest::prelude::*;

const TOL: f64 = 1e-9;

fn finite_vol() -> impl Strategy<Value = f64> {
    0.0f64..500.0
}

fn any_vol() -> impl Strategy<Value = f64> {
    prop_oneof![
        5 => 0.0f64..500.0,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
        1 => Just(f64::NEG_INFINITY),
        1 => -100.0f64..0.0,
    ]
}

fn sanitize(v: f64) -> f64 {
    if v.is_finite() && v > 0.0 {
        v
    } else {
        0.0
    }
}

/// Independent shadow recomputation of the weighted-OBI formula.
fn shadow_obi(bids: &[(f64, f64)], asks: &[(f64, f64)], depth: usize, beta: f64) -> Option<f64> {
    let n = depth.min(bids.len()).min(asks.len());
    if n == 0 {
        return None;
    }
    let mut w_bid = 0.0;
    let mut w_ask = 0.0;
    let mut w_total = 0.0;
    for i in 0..n {
        let w = (-beta * i as f64).exp();
        let bv = sanitize(bids[i].1);
        let av = sanitize(asks[i].1);
        w_bid += w * bv;
        w_ask += w * av;
        w_total += w * (bv + av);
    }
    if w_total <= 0.0 {
        None
    } else {
        Some((w_bid - w_ask) / w_total)
    }
}

fn levels(prices: Vec<f64>, vols: Vec<f64>) -> Vec<(f64, f64)> {
    prices.into_iter().zip(vols).collect()
}

proptest! {
    #[test]
    fn obi_always_in_unit_interval_or_none(
        depth in 1usize..10,
        beta in 0.0f64..1.0,
        bid_vols in prop::collection::vec(any_vol(), 0..10),
        ask_vols in prop::collection::vec(any_vol(), 0..10),
    ) {
        let bids = levels((0..bid_vols.len()).map(|i| 1300.0 - i as f64 * 0.1).collect(), bid_vols);
        let asks = levels((0..ask_vols.len()).map(|i| 1300.1 + i as f64 * 0.1).collect(), ask_vols);
        let obi = WeightedObi::new(depth, beta);
        if let Some((val, _, _)) = obi.compute(&bids, &asks) {
            prop_assert!(val.is_finite());
            prop_assert!((-1.0..=1.0).contains(&val), "OBI {val} outside [-1,1]");
        }
    }

    #[test]
    fn matches_independent_shadow_model(
        depth in 1usize..10,
        beta in 0.0f64..1.0,
        bid_vols in prop::collection::vec(finite_vol(), 0..10),
        ask_vols in prop::collection::vec(finite_vol(), 0..10),
    ) {
        let bids = levels((0..bid_vols.len()).map(|i| 1300.0 - i as f64 * 0.1).collect(), bid_vols);
        let asks = levels((0..ask_vols.len()).map(|i| 1300.1 + i as f64 * 0.1).collect(), ask_vols);
        let obi = WeightedObi::new(depth, beta);
        let engine = obi.compute(&bids, &asks).map(|(v, _, _)| v);
        let shadow = shadow_obi(&bids, &asks, depth, beta);
        match (engine, shadow) {
            (None, None) => {}
            (Some(e), Some(s)) => prop_assert!((e - s).abs() <= TOL, "engine {e} shadow {s}"),
            other => prop_assert!(false, "mismatch: {other:?}"),
        }
    }
}
