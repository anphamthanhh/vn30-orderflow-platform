//! Supertrend on M30 bars (bar-driven): hl2 bands at `multiplier × ATR(period)`, long in an
//! uptrend, short in a downtrend; holds during the ATR warm-up.
//!
//! A reversal is `Exit` on the flip bar and `Enter` on the next, so it spends one bar flat.

use orderflow_engine::atr::Atr;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

/// Supertrend parameters.
#[derive(Clone, Copy, Debug)]
pub struct SupertrendConfig {
    pub bar_seconds: i64,
    pub period: usize,
    pub multiplier: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for SupertrendConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 1800, // M30
            period: 10,
            multiplier: 3.0,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct SupertrendM30 {
    cfg: SupertrendConfig,
    atr: Atr,
    prev_final_ub: Option<f64>,
    prev_final_lb: Option<f64>,
    prev_close: Option<f64>,
    prev_trend: Option<i32>,
}

impl SupertrendM30 {
    #[must_use]
    pub fn new(cfg: SupertrendConfig) -> Self {
        Self {
            atr: Atr::new(cfg.period),
            prev_final_ub: None,
            prev_final_lb: None,
            prev_close: None,
            prev_trend: None,
            cfg,
        }
    }

    /// Move the current position toward `desired` (+1 long, -1 short, 0 flat).
    fn reconcile(&self, ctx: &MarketCtx, desired: i32) -> BtDecision {
        let cur = ctx.position.map_or(0, |p| match p.dir {
            Dir::Long => 1,
            Dir::Short => -1,
        });
        if cur == desired {
            BtDecision::Hold
        } else if cur != 0 {
            // close first; the opposite entry follows on the next bar
            BtDecision::Exit {
                reason: "supertrend_flip".into(),
            }
        } else {
            let direction = if desired > 0 { Dir::Long } else { Dir::Short };
            BtDecision::Enter {
                direction,
                qty: self.cfg.qty,
                reason: if desired > 0 {
                    "supertrend_long".into()
                } else {
                    "supertrend_short".into()
                },
            }
        }
    }
}

impl Strategy for SupertrendM30 {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        let atr = match self.atr.on_bar(bar.high, bar.low, bar.close) {
            Some(a) => a,
            None => {
                // warm-up: keep prev_close current so the bands are right once warm
                self.prev_close = Some(bar.close);
                return BtDecision::Hold;
            }
        };

        let src = (bar.high + bar.low) / 2.0;
        let basic_ub = src + self.cfg.multiplier * atr;
        let basic_lb = src - self.cfg.multiplier * atr;
        let prev_close = self.prev_close;

        // final bands ratchet toward price and reset once the previous close breaks through
        let final_ub = match (self.prev_final_ub, prev_close) {
            (Some(pub_), Some(pc)) if basic_ub >= pub_ && pc <= pub_ => pub_,
            _ => basic_ub,
        };
        let final_lb = match (self.prev_final_lb, prev_close) {
            (Some(plb), Some(pc)) if basic_lb <= plb && pc >= plb => plb,
            _ => basic_lb,
        };

        // the trend flips when the close crosses the opposite final band
        let trend = match self.prev_trend {
            Some(1) => {
                if bar.close < final_lb {
                    -1
                } else {
                    1
                }
            }
            Some(_) => {
                if bar.close > final_ub {
                    1
                } else {
                    -1
                }
            }
            // the first warm bar seeds an uptrend
            None => 1,
        };

        self.prev_final_ub = Some(final_ub);
        self.prev_final_lb = Some(final_lb);
        self.prev_close = Some(bar.close);
        self.prev_trend = Some(trend);

        let desired = if trend == 1 {
            1
        } else if self.cfg.enable_short {
            -1
        } else {
            0
        };
        self.reconcile(ctx, desired)
    }

    fn name(&self) -> &'static str {
        "supertrend_m30"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    fn bar(h: f64, l: f64, c: f64) -> Bar {
        Bar {
            ts_open: 0,
            ts_close: 0,
            open: c,
            high: h,
            low: l,
            close: c,
            volume: 1.0,
        }
    }

    fn ctx(pos: Option<PositionView>) -> MarketCtx {
        MarketCtx {
            price: 0.0,
            volume: 0.0,
            side: None,
            timestamp: 0,
            position: pos,
        }
    }

    fn cfg() -> SupertrendConfig {
        SupertrendConfig {
            bar_seconds: 1,
            period: 2,
            multiplier: 1.0,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn warmup_holds_then_enters_long_in_uptrend() {
        let mut s = SupertrendM30::new(cfg());
        // bar 1: ATR cold, hold
        assert!(matches!(
            s.on_bar(&bar(101.0, 99.0, 100.0), &ctx(None)),
            BtDecision::Hold
        ));
        // bar 2: ATR warm and the uptrend seed: enter long
        match s.on_bar(&bar(102.0, 100.0, 101.0), &ctx(None)) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected Enter Long, got {other:?}"),
        }
    }

    #[test]
    fn flip_to_downtrend_exits_long_position() {
        let mut s = SupertrendM30::new(cfg());
        s.on_bar(&bar(101.0, 99.0, 100.0), &ctx(None)); // warmup
        s.on_bar(&bar(102.0, 100.0, 101.0), &ctx(None)); // uptrend seed
        let long = Some(PositionView {
            dir: Dir::Long,
            qty: 1,
            entry_price: 101.0,
        });
        // a drop below the lower band flips the trend and closes the long
        match s.on_bar(&bar(95.0, 90.0, 91.0), &ctx(long)) {
            BtDecision::Exit { .. } => {}
            other => panic!("expected Exit on flip, got {other:?}"),
        }
    }

    #[test]
    fn short_disabled_goes_flat_not_short() {
        let mut s = SupertrendM30::new(SupertrendConfig {
            enable_short: false,
            ..cfg()
        });
        s.on_bar(&bar(101.0, 99.0, 100.0), &ctx(None));
        s.on_bar(&bar(102.0, 100.0, 101.0), &ctx(None));
        // flip down while flat with shorts disabled: hold
        assert!(matches!(
            s.on_bar(&bar(95.0, 90.0, 91.0), &ctx(None)),
            BtDecision::Hold
        ));
    }
}
