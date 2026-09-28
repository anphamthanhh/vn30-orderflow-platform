//! Property tests for `orderflow_engine::atr::Atr` through its public API.
//!
//! Covers zero ATR on constant prices, non-negativity, parity with an independent Wilder
//! recurrence, the warmup boundary, transparency of invalid bars and the max-true-range bound.

use orderflow_engine::atr::Atr;
use proptest::prelude::*;

const TOL: f64 = 1e-9;

/// A valid `(high, low, close)` bar: prices 900–1650, range up to 50 points, close inside it.
fn valid_bar() -> impl Strategy<Value = (f64, f64, f64)> {
    (900.0f64..1600.0, 0.0f64..50.0, 0.0f64..=1.0).prop_map(|(low, range, t)| {
        let high = low + range;
        let close = low + t * range;
        (high, low, close)
    })
}

/// True range: `max(h - l, |h - pc|, |l - pc|)`.
fn true_range(high: f64, low: f64, prev_close: Option<f64>) -> f64 {
    match prev_close {
        Some(pc) => (high - low).max((high - pc).abs()).max((low - pc).abs()),
        None => high - low,
    }
}

proptest! {
    /// Constant-price bars give an ATR of exactly 0 once warm.
    #[test]
    fn constant_price_bars_give_zero_atr(
        price in 900.0f64..1600.0,
        period in 1usize..20,
        extra in 0usize..50,
    ) {
        let mut atr = Atr::new(period);
        for i in 0..(period + extra) {
            let out = atr.on_bar(price, price, price);
            if i + 1 >= period {
                let v = out.expect("warm after period bars");
                prop_assert!(v.abs() <= 1e-12, "constant price gave ATR {v} at bar {i}");
            } else {
                prop_assert!(out.is_none());
            }
        }
    }

    /// ATR is finite and non-negative after every valid bar.
    #[test]
    fn atr_nonnegative_and_finite(
        period in 1usize..20,
        bars in prop::collection::vec(valid_bar(), 0..120),
    ) {
        let mut atr = Atr::new(period);
        for (high, low, close) in bars {
            if let Some(v) = atr.on_bar(high, low, close) {
                prop_assert!(v.is_finite(), "ATR not finite: {v}");
                prop_assert!(v >= 0.0, "ATR negative: {v}");
            }
        }
    }

    /// Matches an independent Wilder recurrence (n = 10) within 1e-9: the seed is the mean of
    /// the first 10 TRs, then `ATR' = (ATR * 9 + TR) / 10`.
    #[test]
    fn wilder_recurrence_parity_n10(bars in prop::collection::vec(valid_bar(), 10..80)) {
        const N: usize = 10;
        let mut atr = Atr::new(N);
        let mut prev_close: Option<f64> = None;
        let mut seed: Vec<f64> = Vec::new();
        let mut expected: Option<f64> = None;
        for (high, low, close) in bars {
            let tr = true_range(high, low, prev_close);
            prev_close = Some(close);
            expected = match expected {
                Some(e) => Some((e * (N as f64 - 1.0) + tr) / N as f64),
                None => {
                    seed.push(tr);
                    if seed.len() >= N {
                        Some(seed.iter().sum::<f64>() / N as f64)
                    } else {
                        None
                    }
                }
            };
            let got = atr.on_bar(high, low, close);
            match (got, expected) {
                (Some(g), Some(e)) => prop_assert!(
                    (g - e).abs() <= TOL * (1.0 + e.abs()),
                    "engine {g} != closed-form {e}"
                ),
                (None, None) => {}
                (g, e) => prop_assert!(false, "warmup mismatch: engine {g:?} expected {e:?}"),
            }
        }
    }

    /// `None` for the first `period - 1` valid bars, `Some` afterwards; `is_warm()` agrees.
    #[test]
    fn warmup_exactly_period_valid_bars(
        period in 1usize..25,
        bars in prop::collection::vec(valid_bar(), 25..60),
    ) {
        let mut atr = Atr::new(period);
        for (i, (high, low, close)) in bars.iter().enumerate() {
            let out = atr.on_bar(*high, *low, *close);
            if i + 1 < period {
                prop_assert!(out.is_none(), "bar {i} warm too early (period {period})");
                prop_assert!(!atr.is_warm());
            } else {
                prop_assert!(out.is_some(), "bar {i} still cold (period {period})");
                prop_assert!(atr.is_warm());
            }
        }
    }

    /// Invalid bars echo the current value and leave the ATR and warm state unchanged.
    #[test]
    fn invalid_bars_are_transparent(
        period in 1usize..10,
        bars in prop::collection::vec((valid_bar(), 0u8..4), 1..80),
    ) {
        let mut dirty = Atr::new(period);
        let mut clean = Atr::new(period);
        for ((high, low, close), kind) in &bars {
            let clean_out = clean.on_bar(*high, *low, *close);
            let garbage = match kind {
                1 => Some((f64::NAN, *low, *close)),
                2 => Some((*high, *low, f64::INFINITY)),
                3 => Some((*low - 1.0, *high + 1.0, *close)), // high < low
                _ => None,
            };
            if let Some((gh, gl, gc)) = garbage {
                let before = dirty.value();
                let echoed = dirty.on_bar(gh, gl, gc);
                prop_assert_eq!(echoed, before, "garbage bar changed the reported value");
            }
            let dirty_out = dirty.on_bar(*high, *low, *close);
            match (dirty_out, clean_out) {
                (Some(d), Some(c)) => prop_assert!((d - c).abs() <= TOL * (1.0 + c.abs())),
                (None, None) => {}
                (d, c) => prop_assert!(false, "warmup diverged: dirty {d:?} clean {c:?}"),
            }
        }
        prop_assert_eq!(dirty.is_warm(), clean.is_warm());
    }

    /// ATR never exceeds the largest true range so far (a mean, then convex combinations).
    #[test]
    fn atr_never_exceeds_max_true_range(
        period in 1usize..15,
        bars in prop::collection::vec(valid_bar(), 1..100),
    ) {
        let mut atr = Atr::new(period);
        let mut prev_close: Option<f64> = None;
        let mut max_tr = 0.0f64;
        for (high, low, close) in bars {
            max_tr = max_tr.max(true_range(high, low, prev_close));
            prev_close = Some(close);
            if let Some(v) = atr.on_bar(high, low, close) {
                prop_assert!(
                    v <= max_tr + TOL * (1.0 + max_tr),
                    "ATR {v} exceeded max TR {max_tr}"
                );
            }
        }
    }
}

/// `period == 0` clamps to 1; `reset()` forgets `prev_close`, so the next TR is `high - low`.
#[test]
fn zero_period_clamps_and_reset_forgets_prev_close() {
    let mut atr = Atr::new(0);
    let v = atr.on_bar(1200.1, 1200.0, 1200.1).expect("warm at bar 1");
    assert!(
        (v - 0.1).abs() <= TOL,
        "one-tick bar should give ATR 0.1, got {v}"
    );

    atr.reset();
    assert!(!atr.is_warm());
    assert_eq!(atr.value(), None);
    // without the reset this TR would use prev_close 1200.1; after it, TR = high - low = 0.5
    let v = atr.on_bar(1300.5, 1300.0, 1300.2).expect("warm again");
    assert!(
        (v - 0.5).abs() <= TOL,
        "post-reset TR must ignore old prev_close, got {v}"
    );
}
