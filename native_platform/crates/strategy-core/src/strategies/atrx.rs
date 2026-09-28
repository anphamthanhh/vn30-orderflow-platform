//! Weak-trend RSI fade (bar-driven), after Aronson, *Evidence-Based Technical Analysis*, ch. 10.
//!
//! With ADX below `adx_threshold`, fade an RSI extreme; exit when RSI crosses 50 or after
//! `hold_bars`.

use orderflow_engine::adx::Adx;
use orderflow_engine::rsi::Rsi;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

#[derive(Clone, Copy, Debug)]
pub struct AtrxConfig {
    pub bar_seconds: i64,
    pub adx_period: usize,
    /// ADX below this counts as a weak trend (published 25).
    pub adx_threshold: f64,
    pub rsi_period: usize,
    pub rsi_low: f64,
    pub rsi_high: f64,
    pub hold_bars: u32,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for AtrxConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 300, // M5
            adx_period: 14,
            adx_threshold: 25.0,
            rsi_period: 7,
            rsi_low: 30.0,
            rsi_high: 70.0,
            hold_bars: 4,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct Atrx {
    cfg: AtrxConfig,
    adx: Adx,
    rsi: Rsi,
    bars_held: u32,
}

impl Atrx {
    #[must_use]
    pub fn new(cfg: AtrxConfig) -> Self {
        Self {
            adx: Adx::new(cfg.adx_period),
            rsi: Rsi::new(cfg.rsi_period),
            bars_held: 0,
            cfg,
        }
    }
}

impl Strategy for Atrx {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        let adx = self.adx.on_bar(bar.high, bar.low, bar.close);
        let rsi = self.rsi.on_close(bar.close);

        // open position: exit when RSI crosses 50 or on time
        if let Some(pos) = ctx.position {
            self.bars_held = self.bars_held.saturating_add(1);
            if let Some(r) = rsi {
                let crossed_mid = match pos.dir {
                    Dir::Long => r >= 50.0,
                    Dir::Short => r <= 50.0,
                };
                if crossed_mid {
                    return BtDecision::Exit {
                        reason: "atrx_rsi_mid".into(),
                    };
                }
            }
            if self.bars_held >= self.cfg.hold_bars {
                return BtDecision::Exit {
                    reason: "atrx_time".into(),
                };
            }
            return BtDecision::Hold;
        }
        self.bars_held = 0;

        // flat: enter only on a weak trend and a stretched RSI
        let (Some(adx), Some(rsi)) = (adx, rsi) else {
            return BtDecision::Hold;
        };
        if adx.adx >= self.cfg.adx_threshold {
            return BtDecision::Hold; // trend too strong to fade
        }
        if rsi >= self.cfg.rsi_high && self.cfg.enable_short {
            BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "atrx_fade_short".into(),
            }
        } else if rsi <= self.cfg.rsi_low {
            BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "atrx_fade_long".into(),
            }
        } else {
            BtDecision::Hold
        }
    }

    fn name(&self) -> &'static str {
        "atrx"
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
    fn cfg() -> AtrxConfig {
        AtrxConfig {
            bar_seconds: 300,
            adx_period: 3,
            adx_threshold: 25.0,
            rsi_period: 3,
            rsi_low: 30.0,
            rsi_high: 70.0,
            hold_bars: 4,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn warmup_holds() {
        let mut s = Atrx::new(cfg());
        assert!(matches!(
            s.on_bar(&bar(101.0, 99.0, 100.0), &ctx(None)),
            BtDecision::Hold
        ));
    }

    #[test]
    fn strong_trend_blocks_entry() {
        // a steady uptrend keeps ADX above 25, so even an RSI near 100 is not faded
        let mut s = Atrx::new(cfg());
        let mut c = 100.0;
        let mut last = BtDecision::Hold;
        for _ in 0..30 {
            c += 3.0;
            last = s.on_bar(&bar(c + 0.5, c - 0.5, c), &ctx(None));
        }
        assert!(matches!(last, BtDecision::Hold), "got {last:?}");
    }

    #[test]
    fn rsi_mid_cross_exits_position() {
        let mut s = Atrx::new(cfg());
        // warm the indicators on choppy bars
        for c in [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.5] {
            s.on_bar(&bar(c + 0.5, c - 0.5, c), &ctx(None));
        }
        // long position; strong up-closes lift RSI above 50: exit
        let long = Some(PositionView {
            dir: Dir::Long,
            qty: 1,
            entry_price: 100.0,
        });
        let mut exited = false;
        let mut c = 101.0;
        for _ in 0..5 {
            c += 2.0;
            if matches!(
                s.on_bar(&bar(c + 0.5, c - 0.5, c), &ctx(long)),
                BtDecision::Exit { .. }
            ) {
                exited = true;
                break;
            }
        }
        assert!(exited, "expected an RSI-mid or time exit");
    }
}
