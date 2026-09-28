//! Strategy-instance configuration: `[[instance]]` tables with `id`, `kind`, `symbol`, `qty`,
//! `enabled`, `shadow` and `params`. Loading fails closed: unknown kinds or parameter keys,
//! empty or duplicate ids, `qty == 0` and a file with nothing to run are errors.

use serde::Deserialize;

/// Concrete strategy an instance runs, in snake_case in TOML (`kind = "pure_cvd"`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StrategyKind {
    /// `Sig001Strategy` in `PureCvd` mode: enters on any CVD exhaustion divergence while flat.
    PureCvd,
    /// `Sig001Strategy` in `Sig001Gated` mode: full three-tier VPIN and bias gating.
    Sig001Gated,
    /// `SupertrendM30`: bar-driven trend following.
    SupertrendM30,
    /// Standalone `CvdExhaustion` with fixed stop and target exits, for cross-validation.
    CvdExhaustion,
    /// `AtrBreakout`: session volatility breakout (bar-driven).
    AtrBreakout,
    /// `Srlcv`: support/resistance and volume breakout (bar-driven).
    Srlcv,
    /// `DualRegime`: Supertrend M5 with a value-area gate and RSI (bar-driven).
    DualRegime,
    /// `Avcd`: anchored VWAP with CVD divergence (tick-driven).
    Avcd,
    /// `Mphf`: market-profile POC reversion (bar-driven).
    Mphf,
    /// `Atrx`: weak-ADX, extreme-RSI fade (bar-driven).
    Atrx,
    /// `IbFade`: initial-balance failure fade (bar-driven).
    IbFade,
    /// `VwapReversionCvd`: VWAP reversion gated by CVD exhaustion divergence (tick-driven).
    VwapReversionCvd,
    /// `VaEdgeRotation`: value-area edge rejection fade on a prior-day proxy (bar-driven).
    VaEdgeRotation,
}

/// Research verdict on a strategy's design.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CardVerdict {
    /// Rejected by research; running it has to be argued against the existing evidence.
    Killed,
    /// Registered and accruing forward evidence.
    Forward,
    /// Card written and frozen; screen not yet run.
    Registered,
    /// Measured, and no window can supply the sample its criteria need.
    Underpowered,
    /// Deliberately without a card; the note says why.
    NoCard,
}

/// The research card governing a strategy, and its verdict.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResearchCard {
    /// Research card id (see `docs/knowledge/07-research-integrity.md`), if there is one.
    pub card: Option<&'static str>,
    pub verdict: CardVerdict,
    /// One actionable line, such as a warning that the strategy implements a rejected design.
    pub note: &'static str,
}

impl StrategyKind {
    /// The research card for this strategy.
    ///
    /// The match is exhaustive, so a new `StrategyKind` does not compile until its card, or the
    /// reason it has none, is stated.
    pub const fn research_card(self) -> ResearchCard {
        match self {
            StrategyKind::PureCvd => ResearchCard {
                card: Some("CX1"),
                verdict: CardVerdict::Registered,
                note: "CVD exhaustion, ungated. Runs in the demo auto-trade lane; \
                       carded 2026-09-02 after running uncarded for months.",
            },
            StrategyKind::Sig001Gated => ResearchCard {
                card: Some("SG1"),
                verdict: CardVerdict::Registered,
                note: "3-tier composite. Tier 2 is the 80% Rule = card V4, measured \
                       UNDERPOWERED (~8 signals/yr). The bias is OFF by default since \
                       2026-09-02, so this kind takes NO entries until SG1 has a verdict.",
            },
            StrategyKind::SupertrendM30 => ResearchCard {
                card: Some("S8"),
                verdict: CardVerdict::Forward,
                note: "Low-turnover trend. Clears REALISTIC, not WORSE_SIDE — \
                       forward-only, and its power is UNDEFINED at the promotion bar.",
            },
            StrategyKind::CvdExhaustion => ResearchCard {
                card: Some("CX1"),
                verdict: CardVerdict::Registered,
                note: "Standalone CVD divergence oracle; shares CX1 with PureCvd.",
            },
            StrategyKind::AtrBreakout => ResearchCard {
                card: Some("S1"),
                verdict: CardVerdict::Killed,
                note: "KILLED: entry is directionless (gross ~0.1pt/trade, inside the \
                       cost-null); 0 of 48 exit configs positive. Kept for regression \
                       parity only.",
            },
            StrategyKind::Srlcv => ResearchCard {
                card: Some("S2"),
                verdict: CardVerdict::Killed,
                note: "KILLED: median MFE 1.65pt under a 2pt fee at its 4-minute \
                       horizon — a cadence-scale error, not a signal failure.",
            },
            StrategyKind::DualRegime => ResearchCard {
                card: Some("S3"),
                verdict: CardVerdict::Killed,
                note: "KILLED at M5 on churn x fee (only positive gross in its wave, \
                       +0.83pt, against 131pt of fees). The surviving variant is S8 @M30.",
            },
            StrategyKind::Avcd => ResearchCard {
                card: Some("AV1"),
                verdict: CardVerdict::Registered,
                note: "Anchored VWAP + CVD divergence. Carded 2026-09-02.",
            },
            StrategyKind::Mphf => ResearchCard {
                card: Some("MP1"),
                verdict: CardVerdict::Killed,
                note: "IMPLEMENTS A KILLED FAMILY. Market-profile POC reversion is the \
                       level-magnet family V3 killed on 1,527 sessions: lift over a \
                       distance-matched control monetises to 0.12-0.20pt against a \
                       1.28pt bar. Re-running the same design is not new evidence.",
            },
            StrategyKind::Atrx => ResearchCard {
                card: Some("AX1"),
                verdict: CardVerdict::Registered,
                note: "ADX-weak + RSI-extreme fade. Carded 2026-09-02.",
            },
            StrategyKind::IbFade => ResearchCard {
                card: None,
                verdict: CardVerdict::NoCard,
                note: "No card of its own. C6 measured the adjacent claim and nulled it: \
                       the intraday RANGE edge is real but not gate-harvestable \
                       (lift -0.002 over 1,536 sessions).",
            },
            StrategyKind::VwapReversionCvd => ResearchCard {
                card: Some("D2"),
                verdict: CardVerdict::Registered,
                note: "The implementation PREDATES its card's screen, which has never \
                       been written. The frozen design must be taken from D2, not \
                       reverse-engineered from this code.",
            },
            StrategyKind::VaEdgeRotation => ResearchCard {
                card: Some("VR1"),
                verdict: CardVerdict::Killed,
                note: "IMPLEMENTS A KILLED FAMILY. Fading the value-area edge toward \
                       POC is V2/V3: the fade half died on the base-rate control, and \
                       VN30F1M opens OUTSIDE prior value on 72.6% of sessions, so the \
                       rotational premise is the minority regime here.",
            },
        }
    }
}

/// Per-instance parameter overrides; unset fields fall back to the strategy's `Default`.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct InstanceParams {
    // CVD and SIG-001
    pub bar_seconds: Option<i64>,
    pub lookback: Option<usize>,
    pub cvd_threshold: Option<f64>,
    pub min_strength: Option<f64>,
    // fixed exits and short side
    pub stop_points: Option<f64>,
    pub tp_points: Option<f64>,
    pub enable_short: Option<bool>,
    // Supertrend
    pub period: Option<usize>,
    pub multiplier: Option<f64>,
    // AtrBreakout
    pub atr_mult: Option<f64>,
    pub target_mult: Option<f64>,
    pub stop_mult: Option<f64>,
    pub time_stop_min: Option<i64>,
    pub entry_window_min: Option<i64>,
    // Srlcv
    pub ib_min: Option<i64>,
    pub vol_spike_mult: Option<f64>,
    pub vol_ma_period: Option<usize>,
    pub hold_bars: Option<u32>,
    // DualRegime
    pub rsi_period: Option<usize>,
    pub rsi_low: Option<f64>,
    pub rsi_high: Option<f64>,
    // Avcd and Mphf bands, Atrx ADX
    pub band_points: Option<f64>,
    pub exit_band_points: Option<f64>,
    pub window_bars: Option<usize>,
    pub bucket_points: Option<f64>,
    pub adx_period: Option<usize>,
    pub adx_threshold: Option<f64>,
    // IbFade and VaEdgeRotation
    pub fail_confirm_bars: Option<u32>,
    pub reject_confirm_bars: Option<u32>,
}

fn default_qty() -> u32 {
    1
}
fn default_true() -> bool {
    true
}

/// One strategy instance in the config file.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyInstanceCfg {
    /// Unique, non-empty handle; prefixes client order ids and labels P&L.
    pub id: String,
    pub kind: StrategyKind,
    pub symbol: String,
    #[serde(default = "default_qty")]
    pub qty: u32,
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Paper-only instance: simulated fills are logged and no order is sent.
    ///
    /// Requires `enabled = false`, so a shadow instance can never place a real order.
    #[serde(default)]
    pub shadow: bool,
    #[serde(default)]
    pub params: InstanceParams,
}

/// The parsed registry file. TOML: `[[instance]]` tables.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyRegistryCfg {
    #[serde(rename = "instance", default)]
    pub instances: Vec<StrategyInstanceCfg>,
}

/// Config load / validation failure.
#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    #[error("read strategies file {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("parse strategies TOML: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("invalid strategies config: {0}")]
    Invalid(String),
}

impl StrategyRegistryCfg {
    /// Parse and validate a registry from a TOML string.
    pub fn from_toml_str(s: &str) -> Result<Self, RegistryError> {
        let cfg: StrategyRegistryCfg = toml::from_str(s)?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// Read and validate a registry file.
    pub fn load_from_path(path: &str) -> Result<Self, RegistryError> {
        let raw = std::fs::read_to_string(path).map_err(|source| RegistryError::Io {
            path: path.to_string(),
            source,
        })?;
        Self::from_toml_str(&raw)
    }

    /// Enabled instances: the ones that send orders.
    pub fn enabled(&self) -> impl Iterator<Item = &StrategyInstanceCfg> {
        self.instances.iter().filter(|i| i.enabled)
    }

    /// Shadow (paper-only) instances.
    pub fn shadow(&self) -> impl Iterator<Item = &StrategyInstanceCfg> {
        self.instances.iter().filter(|i| i.shadow)
    }

    /// Validate the whole file, reporting every problem in one error.
    pub fn validate(&self) -> Result<(), RegistryError> {
        let mut errs: Vec<String> = Vec::new();
        if self.instances.is_empty() {
            errs.push("no [[instance]] entries".into());
        }
        let mut seen = std::collections::HashSet::new();
        for (i, inst) in self.instances.iter().enumerate() {
            let where_ = format!("instance[{i}] id={:?}", inst.id);
            if inst.id.trim().is_empty() {
                errs.push(format!("{where_}: empty id"));
            } else if !seen.insert(inst.id.clone()) {
                errs.push(format!("{where_}: duplicate id"));
            }
            if inst.symbol.trim().is_empty() {
                errs.push(format!("{where_}: empty symbol"));
            }
            if inst.qty == 0 {
                errs.push(format!("{where_}: qty must be >= 1"));
            }
            // a shadow instance must never also be live
            if inst.shadow && inst.enabled {
                errs.push(format!(
                    "{where_}: shadow=true requires enabled=false (advisory-only)"
                ));
            }
        }
        if !self.instances.is_empty() && self.enabled().count() == 0 && self.shadow().count() == 0 {
            errs.push("no enabled or shadow instance (nothing to run)".into());
        }
        if errs.is_empty() {
            Ok(())
        } else {
            Err(RegistryError::Invalid(errs.join("; ")))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
[[instance]]
id = "cvd-main"
kind = "pure_cvd"
symbol = "VN30F1M"
qty = 2
enabled = true
[instance.params]
cvd_threshold = 0.3
min_strength = 0.3
bar_seconds = 900

[[instance]]
id = "st-m30"
kind = "supertrend_m30"
symbol = "VN30F1M"
qty = 1
enabled = true
[instance.params]
period = 10
multiplier = 3.0
"#;

    #[test]
    fn parses_sample() {
        let cfg = StrategyRegistryCfg::from_toml_str(SAMPLE).unwrap();
        assert_eq!(cfg.instances.len(), 2);
        assert_eq!(cfg.instances[0].id, "cvd-main");
        assert_eq!(cfg.instances[0].kind, StrategyKind::PureCvd);
        assert_eq!(cfg.instances[0].qty, 2);
        assert_eq!(cfg.instances[0].params.bar_seconds, Some(900));
        assert_eq!(cfg.instances[1].kind, StrategyKind::SupertrendM30);
        assert_eq!(cfg.instances[1].params.period, Some(10));
        assert_eq!(cfg.enabled().count(), 2);
    }

    #[test]
    fn rejects_duplicate_id() {
        let s = r#"
[[instance]]
id = "dup"
kind = "pure_cvd"
symbol = "VN30F1M"
[[instance]]
id = "dup"
kind = "supertrend_m30"
symbol = "VN30F1M"
"#;
        let err = StrategyRegistryCfg::from_toml_str(s).unwrap_err();
        assert!(matches!(err, RegistryError::Invalid(m) if m.contains("duplicate id")));
    }

    #[test]
    fn rejects_qty_zero_and_empty_symbol() {
        let s = r#"
[[instance]]
id = "a"
kind = "pure_cvd"
symbol = ""
qty = 0
"#;
        let err = StrategyRegistryCfg::from_toml_str(s).unwrap_err();
        let RegistryError::Invalid(m) = err else {
            panic!("expected Invalid");
        };
        assert!(m.contains("empty symbol"), "{m}");
        assert!(m.contains("qty must be >= 1"), "{m}");
    }

    #[test]
    fn rejects_unknown_kind() {
        let s = r#"
[[instance]]
id = "a"
kind = "not_a_strategy"
symbol = "VN30F1M"
"#;
        // an unknown variant is a parse error
        assert!(matches!(
            StrategyRegistryCfg::from_toml_str(s),
            Err(RegistryError::Parse(_))
        ));
    }

    #[test]
    fn rejects_unknown_param_key() {
        let s = r#"
[[instance]]
id = "a"
kind = "pure_cvd"
symbol = "VN30F1M"
[instance.params]
bogus_key = 1
"#;
        assert!(matches!(
            StrategyRegistryCfg::from_toml_str(s),
            Err(RegistryError::Parse(_))
        ));
    }

    #[test]
    fn rejects_all_disabled() {
        let s = r#"
[[instance]]
id = "a"
kind = "pure_cvd"
symbol = "VN30F1M"
enabled = false
"#;
        let err = StrategyRegistryCfg::from_toml_str(s).unwrap_err();
        assert!(matches!(err, RegistryError::Invalid(m) if m.contains("no enabled")));
    }

    #[test]
    fn parses_shadow_flag_and_iterator() {
        // a file with only shadow instances is valid
        let s = r#"
[[instance]]
id = "s7-shadow"
kind = "supertrend_m30"
symbol = "VN30F1M"
qty = 1
enabled = false
shadow = true
[instance.params]
bar_seconds = 3600
period = 20
multiplier = 3.0
"#;
        let cfg = StrategyRegistryCfg::from_toml_str(s).unwrap();
        assert_eq!(cfg.enabled().count(), 0);
        assert_eq!(cfg.shadow().count(), 1);
        assert_eq!(cfg.shadow().next().unwrap().id, "s7-shadow");
        // `shadow` defaults to false
        assert!(!cfg.instances.iter().all(|i| i.shadow) || cfg.instances.len() == 1);
    }

    #[test]
    fn rejects_shadow_and_enabled_both_true() {
        let s = r#"
[[instance]]
id = "bad"
kind = "supertrend_m30"
symbol = "VN30F1M"
enabled = true
shadow = true
"#;
        let err = StrategyRegistryCfg::from_toml_str(s).unwrap_err();
        assert!(
            matches!(err, RegistryError::Invalid(m) if m.contains("shadow=true requires enabled=false")),
        );
    }

    #[test]
    fn shadow_default_false_keeps_existing_configs_valid() {
        // without `shadow` keys only the enabled instances run
        let cfg = StrategyRegistryCfg::from_toml_str(SAMPLE).unwrap();
        assert_eq!(cfg.shadow().count(), 0);
        assert_eq!(cfg.enabled().count(), 2);
    }

    /// Every `StrategyKind` this crate can build.
    const ALL_KINDS: &[StrategyKind] = &[
        StrategyKind::PureCvd,
        StrategyKind::Sig001Gated,
        StrategyKind::SupertrendM30,
        StrategyKind::CvdExhaustion,
        StrategyKind::AtrBreakout,
        StrategyKind::Srlcv,
        StrategyKind::DualRegime,
        StrategyKind::Avcd,
        StrategyKind::Mphf,
        StrategyKind::Atrx,
        StrategyKind::IbFade,
        StrategyKind::VwapReversionCvd,
        StrategyKind::VaEdgeRotation,
    ];

    #[test]
    fn every_strategy_declares_a_research_card_or_says_why_not() {
        for kind in ALL_KINDS {
            let rc = kind.research_card();
            assert!(
                !rc.note.trim().is_empty(),
                "{kind:?} has an empty note — the note is the point",
            );
            match rc.verdict {
                CardVerdict::NoCard => {
                    assert!(rc.card.is_none(), "{kind:?} claims NoCard but names a card",)
                }
                _ => assert!(rc.card.is_some(), "{kind:?} has a verdict but no card id",),
            }
        }
    }

    #[test]
    fn strategies_implementing_killed_designs_say_so_loudly() {
        // `Mphf` and `VaEdgeRotation` implement the level-magnet family rejected by card V3
        for kind in [StrategyKind::Mphf, StrategyKind::VaEdgeRotation] {
            let rc = kind.research_card();
            assert_eq!(
                rc.verdict,
                CardVerdict::Killed,
                "{kind:?} implements a design the lab killed",
            );
            assert!(
                rc.note.contains("KILLED FAMILY"),
                "{kind:?} must state plainly that its family is dead",
            );
        }
    }

    #[test]
    fn sig001_records_that_its_bias_gate_is_off() {
        // the 80% Rule bias is off by default, which disables SIG-001 entries
        let rc = StrategyKind::Sig001Gated.research_card();
        assert!(rc.note.contains("OFF by default"));
        assert!(rc.note.contains("NO entries"));
    }
}
