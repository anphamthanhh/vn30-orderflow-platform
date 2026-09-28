//! Property tests for `orderflow_engine::cvd::DivergenceDetector` through its public API.
//!
//! Covers warmup, determinism, no false positives on a clean trend, output consistency,
//! transparency of invalid bars, and the bearish-first branch order.

use orderflow_engine::cvd::{CvdDivergence, DivBar, DivergenceDetector, DivergenceType};
use proptest::prelude::*;

/// VN30F1M price tick.
const TICK: f64 = 0.1;

/// A valid bar: realistic close, high and low around it, any finite CVD.
fn valid_bar() -> impl Strategy<Value = (f64, f64, f64, f64)> {
    (
        900.0f64..1600.0,
        0.0f64..10.0,
        0.0f64..10.0,
        -1.0e5f64..1.0e5,
    )
        .prop_map(|(close, up, down, cvd)| (close, close + up, close - down, cvd))
}

fn mk_bar(ts: i64, (close, high, low, cvd): (f64, f64, f64, f64)) -> DivBar {
    DivBar {
        timestamp: ts,
        close,
        high,
        low,
        cvd,
    }
}

fn flat_bar(ts: i64, close: f64, cvd: f64) -> DivBar {
    DivBar {
        timestamp: ts,
        close,
        high: close,
        low: close,
        cvd,
    }
}

/// Bit-exact signature of an optional divergence, for determinism checks.
fn sig(d: &Option<CvdDivergence>) -> Option<(DivergenceType, u64, u64, u64, i64)> {
    d.as_ref().map(|d| {
        (
            d.divergence_type,
            d.strength.to_bits(),
            d.price.to_bits(),
            d.cvd.to_bits(),
            d.timestamp,
        )
    })
}

proptest! {
    /// The first `lookback + 1` valid bars never emit a divergence.
    #[test]
    fn no_divergence_during_warmup(
        lookback in 1usize..8,
        seed_bars in prop::collection::vec(valid_bar(), 9..20),
        cvd_threshold in 0.05f64..0.9,
        min_strength in 0.0f64..0.9,
    ) {
        let mut det = DivergenceDetector::new(lookback, cvd_threshold, min_strength);
        for (i, b) in seed_bars.iter().take(lookback + 1).enumerate() {
            let out = det.on_bar(mk_bar(i as i64, *b));
            prop_assert!(out.is_none(), "divergence emitted at warmup bar {i} (lookback {lookback})");
        }
    }

    /// Two detectors fed the same bars produce bit-identical output.
    #[test]
    fn deterministic_for_identical_input(
        lookback in 1usize..6,
        bars in prop::collection::vec(valid_bar(), 0..60),
    ) {
        let mut a = DivergenceDetector::new(lookback, 0.3, 0.3);
        let mut b = DivergenceDetector::new(lookback, 0.3, 0.3);
        for (i, params) in bars.iter().enumerate() {
            let out_a = a.on_bar(mk_bar(i as i64, *params));
            let out_b = b.on_bar(mk_bar(i as i64, *params));
            prop_assert_eq!(sig(&out_a), sig(&out_b), "nondeterminism at bar {}", i);
        }
    }

    /// Rising price with rising positive CVD emits nothing: the current bar is the CVD maximum
    /// (no bearish collapse) and the CVD minimum stays positive (no bullish case).
    #[test]
    fn rising_price_rising_positive_cvd_emits_nothing(
        lookback in 1usize..6,
        cvd_threshold in 0.05f64..0.9,
        min_strength in 0.0f64..0.9,
        start_price in 900.0f64..1500.0,
        start_cvd in 1.0f64..1000.0,
        steps in prop::collection::vec((1u32..10, 0.1f64..100.0), 8..40),
    ) {
        let mut det = DivergenceDetector::new(lookback, cvd_threshold, min_strength);
        let mut price = start_price;
        let mut cvd = start_cvd;
        for (i, (ticks, cvd_step)) in steps.iter().enumerate() {
            price += f64::from(*ticks) * TICK;
            cvd += cvd_step;
            let out = det.on_bar(flat_bar(i as i64, price, cvd));
            prop_assert!(
                out.is_none(),
                "false positive {:?} at bar {} (price {}, cvd {})",
                out.map(|d| d.divergence_type), i, price, cvd
            );
        }
    }

    /// Emitted strength lies in `[min_strength, 1]`; price, CVD and timestamp echo the bar.
    #[test]
    fn emitted_divergence_fields_are_consistent(
        lookback in 1usize..6,
        cvd_threshold in 0.05f64..0.9,
        min_strength in 0.0f64..0.9,
        bars in prop::collection::vec(valid_bar(), 0..80),
    ) {
        let mut det = DivergenceDetector::new(lookback, cvd_threshold, min_strength);
        for (i, params) in bars.iter().enumerate() {
            let bar = mk_bar(i as i64, *params);
            if let Some(div) = det.on_bar(bar) {
                prop_assert!(div.strength.is_finite());
                prop_assert!(
                    div.strength >= min_strength && div.strength <= 1.0 + 1e-12,
                    "strength {} outside [{min_strength}, 1.0]",
                    div.strength
                );
                // float fields echo the bar bit-for-bit
                prop_assert_eq!(div.price.to_bits(), bar.close.to_bits());
                prop_assert_eq!(div.cvd.to_bits(), bar.cvd.to_bits());
                prop_assert_eq!(div.timestamp, bar.timestamp);
            }
        }
    }

    /// Invalid bars return `None` and leave the output on valid bars unchanged.
    #[test]
    fn invalid_bars_are_transparent(
        lookback in 1usize..6,
        bars in prop::collection::vec((valid_bar(), 0u8..4), 1..60),
    ) {
        let mut dirty = DivergenceDetector::new(lookback, 0.3, 0.3);
        let mut clean = DivergenceDetector::new(lookback, 0.3, 0.3);
        for (i, (params, kind)) in bars.iter().enumerate() {
            let ts = i as i64;
            let garbage = match kind {
                1 => Some(DivBar { timestamp: ts, close: f64::NAN, high: 1000.0, low: 999.0, cvd: 0.0 }),
                2 => Some(DivBar { timestamp: ts, close: 1000.0, high: 1000.5, low: 999.5, cvd: f64::INFINITY }),
                3 => Some(DivBar { timestamp: ts, close: 1000.0, high: 999.0, low: 1001.0, cvd: 0.0 }),
                _ => None,
            };
            if let Some(g) = garbage {
                prop_assert!(dirty.on_bar(g).is_none(), "invalid bar emitted a divergence");
            }
            let out_dirty = dirty.on_bar(mk_bar(ts, *params));
            let out_clean = clean.on_bar(mk_bar(ts, *params));
            prop_assert_eq!(
                sig(&out_dirty), sig(&out_clean),
                "garbage perturbed detection at bar {}", i
            );
        }
    }
}

/// Rising closes with a CVD collapse on the last bar (lookback 3, both thresholds 0.3).
fn bearish_fixture(det: &mut DivergenceDetector) -> Option<CvdDivergence> {
    det.on_bar(flat_bar(1, 100.0, 50.0));
    det.on_bar(flat_bar(2, 101.0, 60.0));
    det.on_bar(flat_bar(3, 102.0, 55.0));
    det.on_bar(flat_bar(4, 103.0, 58.0));
    det.on_bar(flat_bar(5, 104.0, 30.0))
}

/// After `reset()` a would-be signal bar starts a new warmup; a full re-warm fires again.
#[test]
fn reset_restarts_warmup_then_redetects() {
    let mut det = DivergenceDetector::new(3, 0.3, 0.3);
    assert!(bearish_fixture(&mut det).is_some(), "sanity: fixture fires");

    det.reset();
    // Post-reset, the same collapse bar is bar #1 -> warmup, no signal.
    assert!(det.on_bar(flat_bar(10, 104.0, 30.0)).is_none());

    // A full fresh fixture after reset fires again.
    det.reset();
    let div = bearish_fixture(&mut det).expect("re-warmed detector fires");
    assert_eq!(div.divergence_type, DivergenceType::BearishExhaustion);
}

/// The bearish branch runs first: passing `cvd_threshold` but failing `min_strength` returns
/// `None`, so a qualifying bullish signal on the same bar is not evaluated.
/// Window cvd {10, -100, 5}: bearish strength 0.5 < 0.6; bullish strength would be 1.0.
#[test]
fn suppressed_bearish_shadows_qualifying_bullish() {
    let mut det = DivergenceDetector::new(3, 0.3, 0.6);
    assert!(det.on_bar(flat_bar(1, 100.0, 0.0)).is_none());
    assert!(det.on_bar(flat_bar(2, 100.0, 0.0)).is_none());
    assert!(det.on_bar(flat_bar(3, 100.0, 10.0)).is_none());
    assert!(det.on_bar(flat_bar(4, 100.0, -100.0)).is_none());
    // bars_seen = 5 = lookback + 2 -> detection active on this bar.
    let out = det.on_bar(flat_bar(5, 100.0, 5.0));
    assert!(
        out.is_none(),
        "characterized: suppressed bearish early-return shadows bullish, got {:?}",
        out.map(|d| d.divergence_type)
    );
}

/// With a lower `min_strength` the same window emits bearish, although bullish also qualifies.
#[test]
fn bearish_takes_precedence_when_both_qualify() {
    let mut det = DivergenceDetector::new(3, 0.3, 0.3);
    det.on_bar(flat_bar(1, 100.0, 0.0));
    det.on_bar(flat_bar(2, 100.0, 0.0));
    det.on_bar(flat_bar(3, 100.0, 10.0));
    det.on_bar(flat_bar(4, 100.0, -100.0));
    let div = det.on_bar(flat_bar(5, 100.0, 5.0)).expect("fires");
    assert_eq!(div.divergence_type, DivergenceType::BearishExhaustion);
    assert!((div.strength - 0.5).abs() <= 1e-12);
}
