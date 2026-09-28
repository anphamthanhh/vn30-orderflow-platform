//! Build a validated [`StrategyInstanceCfg`] into a `Box<dyn Strategy>` with its id, symbol and
//! cadence; unset parameters fall back to each strategy's `Default`.

use signal_engine::evaluator::{EntryMode, SignalConfig};

use super::config::{InstanceParams, StrategyInstanceCfg, StrategyKind};
use crate::strategies::{
    AtrBreakout, AtrBreakoutConfig, Atrx, AtrxConfig, Avcd, AvcdConfig, CvdConfig, CvdExhaustion,
    DualRegime, DualRegimeConfig, IbFade, IbFadeConfig, Mphf, MphfConfig, Sig001Strategy, Srlcv,
    SrlcvConfig, SupertrendConfig, SupertrendM30, VaEdgeConfig, VaEdgeRotation, VwapRevCvd,
    VwapRevCvdConfig,
};
use crate::strategy::Strategy;

/// How the runner must feed an instance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cadence {
    /// Fed every tick (`on_tick`).
    Tick,
    /// Fed each completed bar of the given length in seconds (`on_bar`).
    Bar(i64),
}

/// A built instance: the strategy and its metadata.
pub struct BuiltStrategy {
    pub id: String,
    pub symbol: String,
    pub kind: StrategyKind,
    pub cadence: Cadence,
    pub strategy: Box<dyn Strategy>,
}

fn signal_cfg(p: &InstanceParams, qty: u32, entry_mode: EntryMode) -> SignalConfig {
    let d = SignalConfig::default();
    SignalConfig {
        entry_mode,
        base_qty: qty.max(1),
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        lookback: p.lookback.unwrap_or(d.lookback),
        cvd_threshold: p.cvd_threshold.unwrap_or(d.cvd_threshold),
        min_strength: p.min_strength.unwrap_or(d.min_strength),
        ..d
    }
}

fn supertrend_cfg(p: &InstanceParams, qty: u32) -> SupertrendConfig {
    let d = SupertrendConfig::default();
    SupertrendConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        period: p.period.unwrap_or(d.period),
        multiplier: p.multiplier.unwrap_or(d.multiplier),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn cvd_cfg(p: &InstanceParams, qty: u32) -> CvdConfig {
    let d = CvdConfig::default();
    CvdConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        lookback: p.lookback.unwrap_or(d.lookback),
        cvd_threshold: p.cvd_threshold.unwrap_or(d.cvd_threshold),
        min_strength: p.min_strength.unwrap_or(d.min_strength),
        stop_points: p.stop_points.unwrap_or(d.stop_points),
        tp_points: p.tp_points.unwrap_or(d.tp_points),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn atr_breakout_cfg(p: &InstanceParams, qty: u32) -> AtrBreakoutConfig {
    let d = AtrBreakoutConfig::default();
    AtrBreakoutConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        period: p.period.unwrap_or(d.period),
        atr_mult: p.atr_mult.unwrap_or(d.atr_mult),
        target_mult: p.target_mult.unwrap_or(d.target_mult),
        stop_mult: p.stop_mult.unwrap_or(d.stop_mult),
        time_stop_min: p.time_stop_min.unwrap_or(d.time_stop_min),
        entry_window_min: p.entry_window_min.unwrap_or(d.entry_window_min),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn srlcv_cfg(p: &InstanceParams, qty: u32) -> SrlcvConfig {
    let d = SrlcvConfig::default();
    SrlcvConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        ib_min: p.ib_min.unwrap_or(d.ib_min),
        vol_spike_mult: p.vol_spike_mult.unwrap_or(d.vol_spike_mult),
        vol_ma_period: p.vol_ma_period.unwrap_or(d.vol_ma_period),
        hold_bars: p.hold_bars.unwrap_or(d.hold_bars),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn dual_regime_cfg(p: &InstanceParams, qty: u32) -> DualRegimeConfig {
    let d = DualRegimeConfig::default();
    DualRegimeConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        period: p.period.unwrap_or(d.period),
        multiplier: p.multiplier.unwrap_or(d.multiplier),
        rsi_period: p.rsi_period.unwrap_or(d.rsi_period),
        rsi_low: p.rsi_low.unwrap_or(d.rsi_low),
        rsi_high: p.rsi_high.unwrap_or(d.rsi_high),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn avcd_cfg(p: &InstanceParams, qty: u32) -> AvcdConfig {
    let d = AvcdConfig::default();
    AvcdConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        band_points: p.band_points.unwrap_or(d.band_points),
        exit_band_points: p.exit_band_points.unwrap_or(d.exit_band_points),
        stop_points: p.stop_points.unwrap_or(d.stop_points),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn mphf_cfg(p: &InstanceParams, qty: u32) -> MphfConfig {
    let d = MphfConfig::default();
    MphfConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        window_bars: p.window_bars.unwrap_or(d.window_bars),
        bucket_points: p.bucket_points.unwrap_or(d.bucket_points),
        band_points: p.band_points.unwrap_or(d.band_points),
        exit_band_points: p.exit_band_points.unwrap_or(d.exit_band_points),
        hold_bars: p.hold_bars.unwrap_or(d.hold_bars),
        value_area_pct: d.value_area_pct,
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn atrx_cfg(p: &InstanceParams, qty: u32) -> AtrxConfig {
    let d = AtrxConfig::default();
    AtrxConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        adx_period: p.adx_period.unwrap_or(d.adx_period),
        adx_threshold: p.adx_threshold.unwrap_or(d.adx_threshold),
        rsi_period: p.rsi_period.unwrap_or(d.rsi_period),
        rsi_low: p.rsi_low.unwrap_or(d.rsi_low),
        rsi_high: p.rsi_high.unwrap_or(d.rsi_high),
        hold_bars: p.hold_bars.unwrap_or(d.hold_bars),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn ib_fade_cfg(p: &InstanceParams, qty: u32) -> IbFadeConfig {
    let d = IbFadeConfig::default();
    IbFadeConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        ib_min: p.ib_min.unwrap_or(d.ib_min),
        fail_confirm_bars: p.fail_confirm_bars.unwrap_or(d.fail_confirm_bars),
        stop_points: p.stop_points.unwrap_or(d.stop_points),
        hold_bars: p.hold_bars.unwrap_or(d.hold_bars),
        entry_window_min: p.entry_window_min.unwrap_or(d.entry_window_min),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn vwap_rev_cvd_cfg(p: &InstanceParams, qty: u32) -> VwapRevCvdConfig {
    let d = VwapRevCvdConfig::default();
    VwapRevCvdConfig {
        band_points: p.band_points.unwrap_or(d.band_points),
        exit_band_points: p.exit_band_points.unwrap_or(d.exit_band_points),
        stop_points: p.stop_points.unwrap_or(d.stop_points),
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

fn va_edge_cfg(p: &InstanceParams, qty: u32) -> VaEdgeConfig {
    let d = VaEdgeConfig::default();
    VaEdgeConfig {
        bar_seconds: p.bar_seconds.unwrap_or(d.bar_seconds),
        reject_confirm_bars: p.reject_confirm_bars.unwrap_or(d.reject_confirm_bars),
        stop_points: p.stop_points.unwrap_or(d.stop_points),
        hold_bars: p.hold_bars.unwrap_or(d.hold_bars),
        entry_window_min: p.entry_window_min.unwrap_or(d.entry_window_min),
        bucket_points: p.bucket_points.unwrap_or(d.bucket_points),
        value_area_pct: d.value_area_pct,
        enable_short: p.enable_short.unwrap_or(d.enable_short),
        qty: qty.max(1),
    }
}

/// Build a strategy from a validated instance config.
#[must_use]
pub fn build(cfg: &StrategyInstanceCfg) -> BuiltStrategy {
    let (cadence, strategy): (Cadence, Box<dyn Strategy>) = match cfg.kind {
        StrategyKind::PureCvd => (
            Cadence::Tick,
            Box::new(Sig001Strategy::new(signal_cfg(
                &cfg.params,
                cfg.qty,
                EntryMode::PureCvd,
            ))),
        ),
        StrategyKind::Sig001Gated => (
            Cadence::Tick,
            Box::new(Sig001Strategy::new(signal_cfg(
                &cfg.params,
                cfg.qty,
                EntryMode::Sig001Gated,
            ))),
        ),
        StrategyKind::CvdExhaustion => (
            Cadence::Tick,
            Box::new(CvdExhaustion::new(cvd_cfg(&cfg.params, cfg.qty))),
        ),
        StrategyKind::SupertrendM30 => {
            let sc = supertrend_cfg(&cfg.params, cfg.qty);
            (
                Cadence::Bar(sc.bar_seconds),
                Box::new(SupertrendM30::new(sc)),
            )
        }
        StrategyKind::AtrBreakout => {
            let ac = atr_breakout_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(ac.bar_seconds), Box::new(AtrBreakout::new(ac)))
        }
        StrategyKind::Srlcv => {
            let sc = srlcv_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(sc.bar_seconds), Box::new(Srlcv::new(sc)))
        }
        StrategyKind::DualRegime => {
            let dc = dual_regime_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(dc.bar_seconds), Box::new(DualRegime::new(dc)))
        }
        StrategyKind::Avcd => (
            // builds its own bars from ticks
            Cadence::Tick,
            Box::new(Avcd::new(avcd_cfg(&cfg.params, cfg.qty))),
        ),
        StrategyKind::Mphf => {
            let mc = mphf_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(mc.bar_seconds), Box::new(Mphf::new(mc)))
        }
        StrategyKind::Atrx => {
            let ac = atrx_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(ac.bar_seconds), Box::new(Atrx::new(ac)))
        }
        StrategyKind::IbFade => {
            let ic = ib_fade_cfg(&cfg.params, cfg.qty);
            (Cadence::Bar(ic.bar_seconds), Box::new(IbFade::new(ic)))
        }
        StrategyKind::VwapReversionCvd => (
            Cadence::Tick,
            Box::new(VwapRevCvd::new(vwap_rev_cvd_cfg(&cfg.params, cfg.qty))),
        ),
        StrategyKind::VaEdgeRotation => {
            let vc = va_edge_cfg(&cfg.params, cfg.qty);
            (
                Cadence::Bar(vc.bar_seconds),
                Box::new(VaEdgeRotation::new(vc)),
            )
        }
    };
    BuiltStrategy {
        id: cfg.id.clone(),
        symbol: cfg.symbol.clone(),
        kind: cfg.kind,
        cadence,
        strategy,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry::config::StrategyRegistryCfg;

    fn build_all(toml: &str) -> Vec<BuiltStrategy> {
        StrategyRegistryCfg::from_toml_str(toml)
            .unwrap()
            .enabled()
            .map(build)
            .collect()
    }

    #[test]
    fn builds_cvd_and_supertrend_with_cadence() {
        let built = build_all(
            r#"
[[instance]]
id = "cvd-main"
kind = "pure_cvd"
symbol = "VN30F1M"
qty = 2
[[instance]]
id = "st-m30"
kind = "supertrend_m30"
symbol = "VN30F1M"
qty = 1
[instance.params]
period = 7
multiplier = 2.5
"#,
        );
        assert_eq!(built.len(), 2);
        assert_eq!(built[0].id, "cvd-main");
        assert_eq!(built[0].kind, StrategyKind::PureCvd);
        assert_eq!(built[0].cadence, Cadence::Tick);
        assert_eq!(built[0].strategy.name(), "sig001");
        assert_eq!(built[1].kind, StrategyKind::SupertrendM30);
        // the default bar_seconds (1800) becomes the cadence
        assert_eq!(built[1].cadence, Cadence::Bar(1800));
        assert_eq!(built[1].strategy.name(), "supertrend_m30");
    }

    /// The S7 configuration (Supertrend period 20, multiplier 3) builds with an H1 cadence.
    #[test]
    fn builds_s7_slow_trend_h1_sleeve() {
        let built = build_all(
            r#"
[[instance]]
id = "s7-slowtrend-h1"
kind = "supertrend_m30"
symbol = "VN30F1M"
qty = 1
[instance.params]
bar_seconds = 3600
period = 20
multiplier = 3.0
"#,
        );
        assert_eq!(built.len(), 1);
        assert_eq!(built[0].id, "s7-slowtrend-h1");
        assert_eq!(built[0].kind, StrategyKind::SupertrendM30);
        assert_eq!(built[0].cadence, Cadence::Bar(3600));
        assert_eq!(built[0].strategy.name(), "supertrend_m30");
    }

    #[test]
    fn params_override_bar_seconds_for_bar_cadence() {
        let built = build_all(
            r#"
[[instance]]
id = "st-fast"
kind = "supertrend_m30"
symbol = "VN30F1M"
[instance.params]
bar_seconds = 300
"#,
        );
        assert_eq!(built[0].cadence, Cadence::Bar(300));
    }
}
