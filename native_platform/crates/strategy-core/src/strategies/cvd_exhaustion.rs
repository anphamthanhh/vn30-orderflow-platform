//! CVD exhaustion fade with fixed stop and target exits (tick-driven, evaluated at bar closes).
//!
//! Uses `orderflow_engine::cvd::DivergenceDetector`, the detector inside SIG-001's
//! `CvdExhaustionTrigger`: a bearish exhaustion enters short, a bullish one long.

use market_types::TickSide;
use orderflow_engine::cvd::{
    CvdDivergence, CvdTracker, DivBar, DivergenceDetector, DivergenceType,
};

use crate::strategy::{BtDecision, Dir, MarketCtx, Strategy};

/// Parameters; `stop_points` and `tp_points` are index points from entry.
#[derive(Clone, Copy, Debug)]
pub struct CvdConfig {
    pub bar_seconds: i64,
    pub lookback: usize,
    pub cvd_threshold: f64,
    pub min_strength: f64,
    pub stop_points: f64,
    pub tp_points: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for CvdConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 900, // 15 min, as in `SignalConfig::default`
            lookback: 3,
            cvd_threshold: 0.3,
            min_strength: 0.3,
            stop_points: 1.5,
            tp_points: 3.0,
            enable_short: true,
            qty: 1,
        }
    }
}

#[derive(Clone, Copy)]
struct BarAcc {
    index: i64,
    ts_close: i64,
    high: f64,
    low: f64,
    close: f64,
}

pub struct CvdExhaustion {
    cfg: CvdConfig,
    cvd: CvdTracker,
    detector: DivergenceDetector,
    cur_bar: Option<BarAcc>,
}

impl CvdExhaustion {
    #[must_use]
    pub fn new(cfg: CvdConfig) -> Self {
        // CVD runs across the whole tape without session resets, unlike `CvdExhaustionTrigger`
        Self {
            cvd: CvdTracker::new(10_000),
            detector: DivergenceDetector::new(cfg.lookback, cfg.cvd_threshold, cfg.min_strength),
            cur_bar: None,
            cfg,
        }
    }

    fn entry_from_divergence(&self, div: &CvdDivergence) -> BtDecision {
        match div.divergence_type {
            // bearish exhaustion at a high: fade short
            DivergenceType::BearishExhaustion if self.cfg.enable_short => BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: format!("cvd_bearish_exhaustion s{:.2}", div.strength).into(),
            },
            // bullish exhaustion at a low: fade long
            DivergenceType::BullishExhaustion => BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: format!("cvd_bullish_exhaustion s{:.2}", div.strength).into(),
            },
            DivergenceType::BearishExhaustion => BtDecision::Hold, // short disabled
        }
    }

    /// Fixed stop / take-profit exit on the open position.
    fn check_exit(&self, ctx: &MarketCtx) -> BtDecision {
        let Some(pos) = ctx.position else {
            return BtDecision::Hold;
        };
        let p = ctx.price;
        match pos.dir {
            Dir::Long => {
                if p <= pos.entry_price - self.cfg.stop_points {
                    BtDecision::Exit {
                        reason: "cvd_stop".into(),
                    }
                } else if p >= pos.entry_price + self.cfg.tp_points {
                    BtDecision::Exit {
                        reason: "cvd_tp".into(),
                    }
                } else {
                    BtDecision::Hold
                }
            }
            Dir::Short => {
                if p >= pos.entry_price + self.cfg.stop_points {
                    BtDecision::Exit {
                        reason: "cvd_stop".into(),
                    }
                } else if p <= pos.entry_price - self.cfg.tp_points {
                    BtDecision::Exit {
                        reason: "cvd_tp".into(),
                    }
                } else {
                    BtDecision::Hold
                }
            }
        }
    }
}

impl Strategy for CvdExhaustion {
    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        let ts = ctx.timestamp;
        let idx = ts.div_euclid(self.cfg.bar_seconds);

        // A. new bar: evaluate the closed bar at its closing CVD (this tick not yet applied)
        let mut divergence: Option<CvdDivergence> = None;
        if let Some(b) = self.cur_bar {
            if b.index != idx {
                let bar = DivBar {
                    timestamp: b.ts_close,
                    close: b.close,
                    high: b.high,
                    low: b.low,
                    cvd: self.cvd.current_value(),
                };
                divergence = self.detector.on_bar(bar);
                self.cur_bar = None;
            }
        }

        // B. apply this tick's signed volume
        let vol = ctx.volume;
        let delta = match ctx.side {
            Some(TickSide::Buy) => vol,
            Some(TickSide::Sell) => -vol,
            None => 0.0,
        };
        self.cvd.update(delta, ts);

        // C. accumulate the current bar
        match &mut self.cur_bar {
            Some(b) => {
                b.high = b.high.max(ctx.price);
                b.low = b.low.min(ctx.price);
                b.close = ctx.price;
                b.ts_close = ts;
            }
            None => {
                self.cur_bar = Some(BarAcc {
                    index: idx,
                    ts_close: ts,
                    high: ctx.price,
                    low: ctx.price,
                    close: ctx.price,
                });
            }
        }

        // D. exits take priority; entries only when flat
        if ctx.position.is_some() {
            self.check_exit(ctx)
        } else if let Some(div) = divergence {
            self.entry_from_divergence(&div)
        } else {
            BtDecision::Hold
        }
    }

    fn name(&self) -> &'static str {
        "cvd_exhaustion"
    }
}

#[cfg(test)]
mod tests {
    // driver-dependent tests live in `backtest-engine/tests/strategy_integration.rs`
    use super::*;
    use crate::strategy::PositionView;

    fn cfg_1s() -> CvdConfig {
        CvdConfig {
            bar_seconds: 1,
            ..CvdConfig::default()
        }
    }

    #[test]
    fn flat_position_holds_exit() {
        let strat = CvdExhaustion::new(cfg_1s());
        assert!(matches!(
            strat.check_exit(&MarketCtx {
                price: 1250.0,
                volume: 0.0,
                side: None,
                timestamp: 0,
                position: None,
            }),
            BtDecision::Hold
        ));
    }

    #[test]
    fn long_stop_and_tp_trigger_exit() {
        let strat = CvdExhaustion::new(cfg_1s()); // stop 1.5, tp 3.0
        let long = |price: f64| MarketCtx {
            price,
            volume: 0.0,
            side: None,
            timestamp: 0,
            position: Some(PositionView {
                dir: Dir::Long,
                qty: 1,
                entry_price: 1250.0,
            }),
        };
        // at or below entry − stop: cvd_stop
        assert!(
            matches!(strat.check_exit(&long(1248.4)), BtDecision::Exit { reason } if reason == "cvd_stop")
        );
        // at or above entry + tp: cvd_tp
        assert!(
            matches!(strat.check_exit(&long(1253.1)), BtDecision::Exit { reason } if reason == "cvd_tp")
        );
        // in between: Hold
        assert!(matches!(strat.check_exit(&long(1250.5)), BtDecision::Hold));
    }
}
