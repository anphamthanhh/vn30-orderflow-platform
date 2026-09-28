//! Table-driven and property tests for `eighty_percent::check_eighty_percent_rule`.
//!
//! The inside band is closed on both widened edges, `val - tol <= close <= vah + tol` with
//! `tol = (vah - val) × tolerance_pct`; short gating and the block count belong to the caller.

use market_profile_engine::eighty_percent::{
    check_eighty_percent_rule, BiasDirection, BlockOhlc, EightyPercentOutcome,
};
use proptest::prelude::*;

const VAH: f64 = 1305.0;
const VAL: f64 = 1295.0;
const TOL_PCT: f64 = 0.001;

fn block(close: f64) -> BlockOhlc {
    BlockOhlc {
        open: close,
        high: close + 0.5,
        low: close - 0.5,
        close,
    }
}

fn no_signal(reason: &'static str) -> EightyPercentOutcome {
    EightyPercentOutcome::NoSignal { reason }
}

// --- table-driven cases ---

#[test]
fn open_above_prior_vah_gives_bearish_bias_targeting_val() {
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(1302.0), block(1300.5)], TOL_PCT);
    assert_eq!(
        out,
        EightyPercentOutcome::Bias {
            direction: BiasDirection::Bearish,
            target: VAL,
            prior_vah: VAH,
            prior_val: VAL,
            confidence: 0.8,
        }
    );
}

#[test]
fn open_below_prior_val_gives_bullish_bias_targeting_vah() {
    let out = check_eighty_percent_rule(VAH, VAL, 1291.5, &[block(1297.0), block(1299.5)], TOL_PCT);
    assert_eq!(
        out,
        EightyPercentOutcome::Bias {
            direction: BiasDirection::Bullish,
            target: VAH,
            prior_vah: VAH,
            prior_val: VAL,
            confidence: 0.8,
        }
    );
}

/// The open comparison is strict, so an open exactly on a value-area edge counts as inside.
#[test]
fn open_inside_or_exactly_on_va_edge_gives_no_signal() {
    for open in [1300.0, VAH, VAL] {
        assert_eq!(
            check_eighty_percent_rule(VAH, VAL, open, &[block(1300.0)], TOL_PCT),
            no_signal("open_inside_va"),
            "open {open} should be treated as inside the VA"
        );
    }
}

#[test]
fn block_escaping_band_gives_no_signal() {
    // escape above VAH
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(1302.0), block(1306.0)], TOL_PCT);
    assert_eq!(out, no_signal("blocks_not_inside_va"));
    // escape below VAL
    let out = check_eighty_percent_rule(VAH, VAL, 1291.0, &[block(1296.0), block(1293.0)], TOL_PCT);
    assert_eq!(out, no_signal("blocks_not_inside_va"));
}

#[test]
fn tolerance_boundary_is_inclusive_on_both_edges() {
    let tol = (VAH - VAL) * TOL_PCT;
    // exactly at the widened high edge: inside
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(VAH + tol)], TOL_PCT);
    assert!(
        matches!(out, EightyPercentOutcome::Bias { .. }),
        "hi edge must be inclusive"
    );
    // exactly at the widened low edge: inside
    let out = check_eighty_percent_rule(VAH, VAL, 1291.0, &[block(VAL - tol)], TOL_PCT);
    assert!(
        matches!(out, EightyPercentOutcome::Bias { .. }),
        "lo edge must be inclusive"
    );
    // just beyond either widened edge: rejected
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(VAH + tol + 0.01)], TOL_PCT);
    assert_eq!(out, no_signal("blocks_not_inside_va"));
    let out = check_eighty_percent_rule(VAH, VAL, 1291.0, &[block(VAL - tol - 0.01)], TOL_PCT);
    assert_eq!(out, no_signal("blocks_not_inside_va"));
}

/// Non-finite inputs give `invalid_value_area`; a non-finite close is never inside.
#[test]
fn nan_and_inf_inputs_give_no_signal() {
    let bad = [f64::NAN, f64::INFINITY, f64::NEG_INFINITY];
    for value in bad {
        assert_eq!(
            check_eighty_percent_rule(value, VAL, 1309.0, &[block(1300.0)], TOL_PCT),
            no_signal("invalid_value_area"),
            "vah = {value}"
        );
        assert_eq!(
            check_eighty_percent_rule(VAH, value, 1309.0, &[block(1300.0)], TOL_PCT),
            no_signal("invalid_value_area"),
            "val = {value}"
        );
        assert_eq!(
            check_eighty_percent_rule(VAH, VAL, value, &[block(1300.0)], TOL_PCT),
            no_signal("invalid_value_area"),
            "open = {value}"
        );
        assert_eq!(
            check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(1300.0)], value),
            no_signal("invalid_value_area"),
            "tolerance_pct = {value}"
        );
    }
    assert_eq!(
        check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(f64::NAN)], TOL_PCT),
        no_signal("blocks_not_inside_va"),
        "NaN close must never count as inside"
    );
}

#[test]
fn inverted_or_zero_width_va_gives_no_signal() {
    assert_eq!(
        check_eighty_percent_rule(VAL, VAH, 1290.0, &[block(1300.0)], TOL_PCT),
        no_signal("invalid_value_area")
    );
    assert_eq!(
        check_eighty_percent_rule(1300.0, 1300.0, 1305.0, &[block(1300.0)], TOL_PCT),
        no_signal("invalid_value_area")
    );
}

/// Guard order: an invalid value area first, then the open position, then block presence.
#[test]
fn empty_blocks_and_reason_precedence() {
    // open outside, no blocks: no_blocks
    assert_eq!(
        check_eighty_percent_rule(VAH, VAL, 1309.0, &[], TOL_PCT),
        no_signal("no_blocks")
    );
    // an inside open wins over missing blocks
    assert_eq!(
        check_eighty_percent_rule(VAH, VAL, 1300.0, &[], TOL_PCT),
        no_signal("open_inside_va")
    );
    // an invalid value area wins over everything
    assert_eq!(
        check_eighty_percent_rule(f64::NAN, VAL, 1300.0, &[], TOL_PCT),
        no_signal("invalid_value_area")
    );
}

#[test]
fn negative_tolerance_narrows_band() {
    let tol_pct = -0.01; // tol = -0.1: band [1295.1, 1304.9]
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(1304.95)], tol_pct);
    assert_eq!(out, no_signal("blocks_not_inside_va"));
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[block(1304.85)], tol_pct);
    assert!(matches!(out, EightyPercentOutcome::Bias { .. }));
}

#[test]
fn only_block_close_matters_not_high_low() {
    let wild = BlockOhlc {
        open: 1315.0,
        high: 1320.0, // far above VAH
        low: 1285.0,  // far below VAL
        close: 1301.0,
    };
    let out = check_eighty_percent_rule(VAH, VAL, 1309.0, &[wild], TOL_PCT);
    assert!(matches!(
        out,
        EightyPercentOutcome::Bias {
            direction: BiasDirection::Bearish,
            ..
        }
    ));
}

// --- properties ---

fn arb_inputs() -> impl Strategy<
    Value = (
        f64,      // val
        f64,      // width (vah = val + width)
        f64,      // open
        Vec<f64>, // block closes
        f64,      // tolerance_pct
    ),
> {
    (
        900.0f64..1350.0,
        0.5f64..40.0,
        890.0f64..1460.0,
        proptest::collection::vec(880.0f64..1470.0, 1..5),
        0.0f64..0.005,
    )
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(256))]

    /// Every bias targets the opposite edge, agrees with the open's side, has confidence 0.8
    /// and only closes inside the widened band.
    #[test]
    fn prop_bias_invariants((val, width, open, closes, tol_pct) in arb_inputs()) {
        let vah = val + width;
        let blocks: Vec<BlockOhlc> = closes.iter().copied().map(block).collect();
        let out = check_eighty_percent_rule(vah, val, open, &blocks, tol_pct);
        if let EightyPercentOutcome::Bias {
            direction,
            target,
            prior_vah,
            prior_val,
            confidence,
        } = out
        {
            prop_assert_eq!(prior_vah, vah);
            prop_assert_eq!(prior_val, val);
            prop_assert_eq!(confidence, 0.8);
            match direction {
                BiasDirection::Bearish => {
                    prop_assert!(open > vah, "Bearish requires open above VAH");
                    prop_assert_eq!(target, val, "Bearish must target prior VAL");
                }
                BiasDirection::Bullish => {
                    prop_assert!(open < val, "Bullish requires open below VAL");
                    prop_assert_eq!(target, vah, "Bullish must target prior VAH");
                }
            }
            let tol = (vah - val) * tol_pct;
            for close in &closes {
                prop_assert!(
                    *close >= val - tol && *close <= vah + tol,
                    "Bias emitted with block close {close} outside band"
                );
            }
        }
    }

    #[test]
    fn prop_open_inside_va_never_signals(
        val in 900.0f64..1350.0,
        width in 0.5f64..40.0,
        fraction in 0.0f64..=1.0,
        closes in proptest::collection::vec(880.0f64..1470.0, 0..5),
    ) {
        let vah = val + width;
        let open = (val + fraction * width).clamp(val, vah);
        let blocks: Vec<BlockOhlc> = closes.iter().copied().map(block).collect();
        let out = check_eighty_percent_rule(vah, val, open, &blocks, TOL_PCT);
        prop_assert_eq!(out, no_signal("open_inside_va"));
    }

    #[test]
    fn prop_outcome_is_deterministic((val, width, open, closes, tol_pct) in arb_inputs()) {
        let vah = val + width;
        let blocks: Vec<BlockOhlc> = closes.iter().copied().map(block).collect();
        let first = check_eighty_percent_rule(vah, val, open, &blocks, tol_pct);
        let second = check_eighty_percent_rule(vah, val, open, &blocks, tol_pct);
        prop_assert_eq!(first, second);
    }
}
