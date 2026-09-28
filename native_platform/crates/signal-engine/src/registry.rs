//! Signal identities and their stable wire names.
//!
//! Clients match on the snake_case strings from [`SignalId::as_str`], not on enum values.

/// Identity of every signal the 3-tier pipeline can emit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SignalId {
    /// Tier 1: VPIN crossed into the Toxic regime (no trade).
    VpinToxic,
    /// Tier 1: VPIN crossed into the Elevated regime (reduce size).
    VpinElevated,
    /// Tier 2: 80% Rule bias — open below prior VA, rotated back in.
    EightyPercentBullish,
    /// Tier 2: 80% Rule bias — open above prior VA, rotated back in.
    EightyPercentBearish,
    /// Tier 3: price lower low with CVD higher low (reversal up).
    CvdBullishExhaustion,
    /// Tier 3: price higher high with CVD lower high (reversal down).
    CvdBearishExhaustion,
    /// Advisory trailing stop ratcheted by >= 1 tick.
    TrailingStopUpdate,
    /// Advisory trailing stop crossed — exit.
    TrailingStopExit,
}

impl SignalId {
    /// Stable wire name; clients match on it, so existing values must not change.
    pub fn as_str(&self) -> &'static str {
        match self {
            SignalId::VpinToxic => "vpin_toxic",
            SignalId::VpinElevated => "vpin_elevated",
            SignalId::EightyPercentBullish => "eighty_percent_bullish",
            SignalId::EightyPercentBearish => "eighty_percent_bearish",
            SignalId::CvdBullishExhaustion => "cvd_bullish_exhaustion",
            SignalId::CvdBearishExhaustion => "cvd_bearish_exhaustion",
            SignalId::TrailingStopUpdate => "trailing_stop_update",
            SignalId::TrailingStopExit => "trailing_stop_exit",
        }
    }
}

/// Directional read of a signal or session bias.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Bullish,
    Bearish,
    Neutral,
}

impl Direction {
    /// Stable wire name.
    pub fn as_str(&self) -> &'static str {
        match self {
            Direction::Bullish => "bullish",
            Direction::Bearish => "bearish",
            Direction::Neutral => "neutral",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_names_are_stable() {
        // wire contract with clients: do not rename
        let expected = [
            (SignalId::VpinToxic, "vpin_toxic"),
            (SignalId::VpinElevated, "vpin_elevated"),
            (SignalId::EightyPercentBullish, "eighty_percent_bullish"),
            (SignalId::EightyPercentBearish, "eighty_percent_bearish"),
            (SignalId::CvdBullishExhaustion, "cvd_bullish_exhaustion"),
            (SignalId::CvdBearishExhaustion, "cvd_bearish_exhaustion"),
            (SignalId::TrailingStopUpdate, "trailing_stop_update"),
            (SignalId::TrailingStopExit, "trailing_stop_exit"),
        ];
        for (id, name) in expected {
            assert_eq!(id.as_str(), name);
        }
        assert_eq!(Direction::Bullish.as_str(), "bullish");
        assert_eq!(Direction::Bearish.as_str(), "bearish");
        assert_eq!(Direction::Neutral.as_str(), "neutral");
    }
}
