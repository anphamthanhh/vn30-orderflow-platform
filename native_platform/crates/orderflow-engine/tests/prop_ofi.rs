//! Property tests for `orderflow_engine::ofi::MultiLevelOfi`.
//!
//! Covers finite output on arbitrary books, a zero first update, and agreement with an
//! independently written shadow model.

use orderflow_engine::ofi::MultiLevelOfi;
use proptest::prelude::*;

const TOL: f64 = 1e-9;

fn finite_price() -> impl Strategy<Value = f64> {
    1290.0f64..1310.0
}

fn finite_vol() -> impl Strategy<Value = f64> {
    0.0f64..500.0
}

fn any_price() -> impl Strategy<Value = f64> {
    prop_oneof![
        5 => 1290.0f64..1310.0,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
    ]
}

fn any_vol() -> impl Strategy<Value = f64> {
    prop_oneof![
        5 => 0.0f64..500.0,
        1 => Just(f64::NAN),
        1 => -50.0f64..0.0,
    ]
}

fn book(n: usize, prices: Vec<f64>, vols: Vec<f64>) -> Vec<(f64, f64)> {
    prices.into_iter().take(n).zip(vols).collect()
}

fn sanitize_qty(v: f64) -> f64 {
    if v.is_finite() && v >= 0.0 {
        v
    } else {
        0.0
    }
}

struct ShadowOfi {
    levels: usize,
    beta: f64,
    prev_bids: Vec<(f64, f64)>,
    prev_asks: Vec<(f64, f64)>,
    has_prev: bool,
    cumulative: f64,
}

impl ShadowOfi {
    fn new(levels: usize, beta: f64) -> Self {
        Self {
            levels: levels.max(1),
            beta,
            prev_bids: Vec::new(),
            prev_asks: Vec::new(),
            has_prev: false,
            cumulative: 0.0,
        }
    }

    fn term(pb0: f64, qb0: f64, pb1: f64, qb1: f64, is_ask: bool) -> f64 {
        if !pb0.is_finite() || !pb1.is_finite() {
            return 0.0;
        }
        let (qb0, qb1) = (sanitize_qty(qb0), sanitize_qty(qb1));
        if !is_ask {
            let up = if pb1 >= pb0 { qb1 } else { 0.0 };
            let down = if pb1 <= pb0 { qb0 } else { 0.0 };
            up - down
        } else {
            let down = if pb1 <= pb0 { qb1 } else { 0.0 };
            let up = if pb1 >= pb0 { qb0 } else { 0.0 };
            up - down
        }
    }

    fn update(&mut self, bids: &[(f64, f64)], asks: &[(f64, f64)]) -> f64 {
        let cur_bids: Vec<(f64, f64)> = bids.iter().take(self.levels).copied().collect();
        let cur_asks: Vec<(f64, f64)> = asks.iter().take(self.levels).copied().collect();
        let raw = if !self.has_prev {
            0.0
        } else {
            let n = self
                .levels
                .min(self.prev_bids.len())
                .min(self.prev_asks.len())
                .min(cur_bids.len())
                .min(cur_asks.len());
            let mut total = 0.0;
            for i in 0..n {
                let w = (-self.beta * i as f64).exp();
                let (pb0, qb0) = self.prev_bids[i];
                let (pb1, qb1) = cur_bids[i];
                let (pa0, qa0) = self.prev_asks[i];
                let (pa1, qa1) = cur_asks[i];
                total += w
                    * (Self::term(pb0, qb0, pb1, qb1, false)
                        + Self::term(pa0, qa0, pa1, qa1, true));
            }
            total
        };
        self.prev_bids = cur_bids;
        self.prev_asks = cur_asks;
        self.has_prev = true;
        self.cumulative = (self.cumulative + raw) * 0.99;
        raw
    }
}

proptest! {
    #[test]
    fn first_update_always_zero(
        levels in 1usize..8,
        beta in 0.0f64..1.0,
        prices in prop::collection::vec(finite_price(), 0..8),
        vols in prop::collection::vec(finite_vol(), 0..8),
    ) {
        let mut ofi = MultiLevelOfi::new(levels, beta);
        let b = book(levels, prices.clone(), vols.clone());
        let v = ofi.update(&b, &b);
        prop_assert_eq!(v, 0.0);
        prop_assert_eq!(ofi.cumulative(), 0.0);
    }

    #[test]
    fn always_finite_for_arbitrary_sequences(
        levels in 1usize..6,
        beta in 0.0f64..1.0,
        updates in prop::collection::vec(
            (
                prop::collection::vec((any_price(), any_vol()), 0..6),
                prop::collection::vec((any_price(), any_vol()), 0..6),
            ),
            0..50,
        ),
    ) {
        let mut ofi = MultiLevelOfi::new(levels, beta);
        for (bids, asks) in updates {
            let v = ofi.update(&bids, &asks);
            prop_assert!(v.is_finite(), "raw not finite: {v}");
            prop_assert!(ofi.cumulative().is_finite(), "cumulative not finite");
        }
    }

    #[test]
    fn engine_matches_independent_shadow_model(
        levels in 1usize..6,
        beta in 0.0f64..1.0,
        n_levels_per_step in 1usize..6,
        updates in prop::collection::vec(
            (
                prop::collection::vec(finite_vol(), 6),
                prop::collection::vec(finite_vol(), 6),
            ),
            1..40,
        ),
    ) {
        let mut engine = MultiLevelOfi::new(levels, beta);
        let mut shadow = ShadowOfi::new(levels, beta);
        let prices: Vec<f64> = (0..6).map(|i| 1300.0 - i as f64 * 0.1).collect();
        let ask_prices: Vec<f64> = (0..6).map(|i| 1300.1 + i as f64 * 0.1).collect();
        for (bid_vols, ask_vols) in updates {
            let bids = book(n_levels_per_step, prices.clone(), bid_vols);
            let asks = book(n_levels_per_step, ask_prices.clone(), ask_vols);
            let ev = engine.update(&bids, &asks);
            let sv = shadow.update(&bids, &asks);
            prop_assert!((ev - sv).abs() <= TOL, "raw diverged: engine {ev} shadow {sv}");
            prop_assert!(
                (engine.cumulative() - shadow.cumulative).abs() <= TOL,
                "cumulative diverged: engine {} shadow {}", engine.cumulative(), shadow.cumulative
            );
        }
    }
}
