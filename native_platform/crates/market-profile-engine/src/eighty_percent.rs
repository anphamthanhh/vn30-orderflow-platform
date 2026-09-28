//! Dalton's 80% Rule (*Mind Over Markets*, ch. 8) as a pure check on already-fetched inputs.
//!
//! A session opening outside the prior value area whose first blocks close back inside it is
//! biased toward the opposite edge (see `docs/knowledge/06-market-profile.md`).

/// Direction of the 80% Rule session bias.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BiasDirection {
    /// Open below prior VAL, rotated back in → target prior VAH.
    Bullish,
    /// Open above prior VAH, rotated back in → target prior VAL.
    Bearish,
}

/// One early-session block's OHLC (typically 15-minute).
#[derive(Debug, Clone, Copy)]
pub struct BlockOhlc {
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
}

/// Outcome of the 80% Rule check.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum EightyPercentOutcome {
    /// Rule does not apply; `reason` is a stable diagnostic token.
    NoSignal { reason: &'static str },
    /// Rule confirmed: directional bias toward the opposite VA edge.
    Bias {
        direction: BiasDirection,
        /// Opposite VA edge (Bullish → prior VAH, Bearish → prior VAL).
        target: f64,
        prior_vah: f64,
        prior_val: f64,
        /// Fixed 0.8 (Dalton's empirical 80%).
        confidence: f64,
    },
}

/// Check the 80% Rule against the prior value area and the first session blocks.
///
/// `tolerance_pct` widens the inside band by `(vah - val) × tolerance_pct` on each side
/// (the gateway passes 0.001).
pub fn check_eighty_percent_rule(
    prior_vah: f64,
    prior_val: f64,
    session_open: f64,
    first_blocks: &[BlockOhlc],
    tolerance_pct: f64,
) -> EightyPercentOutcome {
    if !prior_vah.is_finite()
        || !prior_val.is_finite()
        || !session_open.is_finite()
        || !tolerance_pct.is_finite()
        || prior_vah <= prior_val
    {
        return EightyPercentOutcome::NoSignal {
            reason: "invalid_value_area",
        };
    }

    let direction = if session_open > prior_vah {
        BiasDirection::Bearish
    } else if session_open < prior_val {
        BiasDirection::Bullish
    } else {
        return EightyPercentOutcome::NoSignal {
            reason: "open_inside_va",
        };
    };

    if first_blocks.is_empty() {
        return EightyPercentOutcome::NoSignal {
            reason: "no_blocks",
        };
    }

    let tolerance = (prior_vah - prior_val) * tolerance_pct;
    let lo = prior_val - tolerance;
    let hi = prior_vah + tolerance;
    let all_inside = first_blocks
        .iter()
        .all(|b| b.close.is_finite() && b.close >= lo && b.close <= hi);
    if !all_inside {
        return EightyPercentOutcome::NoSignal {
            reason: "blocks_not_inside_va",
        };
    }

    let target = match direction {
        BiasDirection::Bullish => prior_vah,
        BiasDirection::Bearish => prior_val,
    };
    EightyPercentOutcome::Bias {
        direction,
        target,
        prior_vah,
        prior_val,
        confidence: 0.8,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(close: f64) -> BlockOhlc {
        BlockOhlc {
            open: close,
            high: close + 0.5,
            low: close - 0.5,
            close,
        }
    }

    const VAH: f64 = 1305.0;
    const VAL: f64 = 1295.0;

    #[test]
    fn open_above_va_blocks_inside_gives_bearish_target_val() {
        let out =
            check_eighty_percent_rule(VAH, VAL, 1308.0, &[block(1303.0), block(1301.0)], 0.001);
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
    fn open_below_va_blocks_inside_gives_bullish_target_vah() {
        let out =
            check_eighty_percent_rule(VAH, VAL, 1292.0, &[block(1296.0), block(1298.0)], 0.001);
        assert!(matches!(
            out,
            EightyPercentOutcome::Bias {
                direction: BiasDirection::Bullish,
                target,
                ..
            } if target == VAH
        ));
    }

    #[test]
    fn open_inside_va_no_signal() {
        let out =
            check_eighty_percent_rule(VAH, VAL, 1300.0, &[block(1300.0), block(1300.0)], 0.001);
        assert_eq!(
            out,
            EightyPercentOutcome::NoSignal {
                reason: "open_inside_va"
            }
        );
    }

    #[test]
    fn block_escaping_va_no_signal() {
        // the second block closes above VAH: rotation not confirmed
        let out =
            check_eighty_percent_rule(VAH, VAL, 1308.0, &[block(1303.0), block(1307.0)], 0.001);
        assert_eq!(
            out,
            EightyPercentOutcome::NoSignal {
                reason: "blocks_not_inside_va"
            }
        );
    }

    #[test]
    fn tolerance_boundary_inclusive() {
        // VA range 10, so the tolerance is 0.01 and a close at VAH + 0.01 is inside
        let out =
            check_eighty_percent_rule(VAH, VAL, 1308.0, &[block(VAH + 0.01), block(1300.0)], 0.001);
        assert!(matches!(out, EightyPercentOutcome::Bias { .. }));
        // just beyond the tolerance: rejected
        let out =
            check_eighty_percent_rule(VAH, VAL, 1308.0, &[block(VAH + 0.02), block(1300.0)], 0.001);
        assert_eq!(
            out,
            EightyPercentOutcome::NoSignal {
                reason: "blocks_not_inside_va"
            }
        );
    }

    #[test]
    fn nan_inputs_no_signal() {
        let nan = f64::NAN;
        for (vah, val, open) in [(nan, VAL, 1308.0), (VAH, nan, 1308.0), (VAH, VAL, nan)] {
            assert_eq!(
                check_eighty_percent_rule(vah, val, open, &[block(1300.0)], 0.001),
                EightyPercentOutcome::NoSignal {
                    reason: "invalid_value_area"
                }
            );
        }
        // a NaN close is never inside
        let out = check_eighty_percent_rule(VAH, VAL, 1308.0, &[block(f64::NAN)], 0.001);
        assert_eq!(
            out,
            EightyPercentOutcome::NoSignal {
                reason: "blocks_not_inside_va"
            }
        );
    }

    #[test]
    fn inverted_or_degenerate_va_no_signal() {
        assert_eq!(
            check_eighty_percent_rule(VAL, VAH, 1290.0, &[block(1300.0)], 0.001),
            EightyPercentOutcome::NoSignal {
                reason: "invalid_value_area"
            }
        );
        assert_eq!(
            check_eighty_percent_rule(1300.0, 1300.0, 1305.0, &[block(1300.0)], 0.001),
            EightyPercentOutcome::NoSignal {
                reason: "invalid_value_area"
            }
        );
    }

    #[test]
    fn empty_blocks_no_signal() {
        assert_eq!(
            check_eighty_percent_rule(VAH, VAL, 1308.0, &[], 0.001),
            EightyPercentOutcome::NoSignal {
                reason: "no_blocks"
            }
        );
    }
}
