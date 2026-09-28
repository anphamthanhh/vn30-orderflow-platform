//! Supertrend gated by a prior-day regime (bar-driven), after Kaufman ch. 5-7.
//!
//! A session opening outside the prior-day high-low range (a value-area proxy) follows Supertrend;
//! one opening inside enters only on an RSI extreme (`rsi_high` for longs, `rsi_low` for shorts).

use orderflow_engine::atr::Atr;
use orderflow_engine::rsi::Rsi;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

fn session_pos(ts: i64) -> Option<(u8, i64)> {
    let tod = (ts + 7 * 3600).rem_euclid(86_400);
    if (32_400..41_400).contains(&tod) {
        Some((0, (tod - 32_400) / 60))
    } else if (46_800..53_100).contains(&tod) {
        Some((1, (tod - 46_800) / 60))
    } else {
        None
    }
}

fn ict_day(ts: i64) -> i64 {
    (ts + 7 * 3600).div_euclid(86_400)
}

#[derive(Clone, Copy, Debug)]
pub struct DualRegimeConfig {
    pub bar_seconds: i64,
    pub period: usize,
    pub multiplier: f64,
    pub rsi_period: usize,
    pub rsi_low: f64,
    pub rsi_high: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for DualRegimeConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 300, // M5
            period: 10,
            multiplier: 2.0,
            rsi_period: 7,
            rsi_low: 30.0,
            rsi_high: 70.0,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct DualRegime {
    cfg: DualRegimeConfig,
    atr: Atr,
    rsi: Rsi,
    // Supertrend band state, as in `supertrend_m30`
    prev_final_ub: Option<f64>,
    prev_final_lb: Option<f64>,
    prev_close: Option<f64>,
    prev_trend: Option<i32>,
    // session and regime state
    session: Option<(i64, u8)>,
    /// This session opened outside the prior-day range, so Supertrend runs ungated.
    naked_regime: bool,
    prior_day_high: Option<f64>,
    prior_day_low: Option<f64>,
    cur_day: Option<i64>,
    cur_day_high: f64,
    cur_day_low: f64,
}

impl DualRegime {
    #[must_use]
    pub fn new(cfg: DualRegimeConfig) -> Self {
        Self {
            atr: Atr::new(cfg.period),
            rsi: Rsi::new(cfg.rsi_period),
            prev_final_ub: None,
            prev_final_lb: None,
            prev_close: None,
            prev_trend: None,
            session: None,
            naked_regime: false,
            prior_day_high: None,
            prior_day_low: None,
            cur_day: None,
            cur_day_high: f64::NEG_INFINITY,
            cur_day_low: f64::INFINITY,
            cfg,
        }
    }

    /// Supertrend trend (+1 up / −1 down) for this bar, or `None` during warmup.
    fn supertrend(&mut self, bar: &Bar) -> Option<i32> {
        let atr = match self.atr.on_bar(bar.high, bar.low, bar.close) {
            Some(a) => a,
            None => {
                self.prev_close = Some(bar.close);
                return None;
            }
        };
        let src = (bar.high + bar.low) / 2.0;
        let basic_ub = src + self.cfg.multiplier * atr;
        let basic_lb = src - self.cfg.multiplier * atr;
        let pc = self.prev_close;
        let final_ub = match (self.prev_final_ub, pc) {
            (Some(p), Some(c)) if basic_ub >= p && c <= p => p,
            _ => basic_ub,
        };
        let final_lb = match (self.prev_final_lb, pc) {
            (Some(p), Some(c)) if basic_lb <= p && c >= p => p,
            _ => basic_lb,
        };
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
            None => 1,
        };
        self.prev_final_ub = Some(final_ub);
        self.prev_final_lb = Some(final_lb);
        self.prev_close = Some(bar.close);
        self.prev_trend = Some(trend);
        Some(trend)
    }

    fn reconcile(&self, ctx: &MarketCtx, desired: i32) -> BtDecision {
        let cur = ctx.position.map_or(0, |p| match p.dir {
            Dir::Long => 1,
            Dir::Short => -1,
        });
        if cur == desired {
            BtDecision::Hold
        } else if cur != 0 {
            BtDecision::Exit {
                reason: "dual_regime_flip".into(),
            }
        } else if desired == 0 {
            BtDecision::Hold
        } else {
            let direction = if desired > 0 { Dir::Long } else { Dir::Short };
            BtDecision::Enter {
                direction,
                qty: self.cfg.qty,
                reason: if desired > 0 {
                    "dual_regime_long".into()
                } else {
                    "dual_regime_short".into()
                },
            }
        }
    }
}

impl Strategy for DualRegime {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        let rsi = self.rsi.on_close(bar.close);

        // day rollover: keep the finished day's range
        let day = ict_day(bar.ts_close);
        if self.cur_day != Some(day) {
            if self.cur_day_high.is_finite() && self.cur_day_low.is_finite() {
                self.prior_day_high = Some(self.cur_day_high);
                self.prior_day_low = Some(self.cur_day_low);
            }
            self.cur_day = Some(day);
            self.cur_day_high = f64::NEG_INFINITY;
            self.cur_day_low = f64::INFINITY;
        }
        self.cur_day_high = self.cur_day_high.max(bar.high);
        self.cur_day_low = self.cur_day_low.min(bar.low);

        // session rollover: the session-open price sets the regime
        let pos = session_pos(bar.ts_close);
        let key = pos.map(|(seg, _)| (day, seg));
        if key != self.session {
            self.session = key;
            if pos.is_some() {
                self.naked_regime = match (self.prior_day_high, self.prior_day_low) {
                    (Some(h), Some(l)) => bar.open > h || bar.open < l,
                    // no prior day yet: follow Supertrend
                    _ => true,
                };
            }
        }

        let trend = match self.supertrend(bar) {
            Some(t) => t,
            None => return BtDecision::Hold, // warmup
        };

        // desired position from the trend; RSI-gated below when rangebound
        let base_desired = if trend == 1 {
            1
        } else if self.cfg.enable_short {
            -1
        } else {
            0
        };

        if pos.is_none() {
            // off-session: exits only, never new entries
            if ctx.position.is_some() {
                return self.reconcile(ctx, base_desired);
            }
            return BtDecision::Hold;
        }

        let desired = if self.naked_regime {
            base_desired
        } else {
            // rangebound: enter only when RSI confirms the extreme
            match (base_desired, rsi) {
                (1, Some(r)) if r >= self.cfg.rsi_high => 1,
                (-1, Some(r)) if r <= self.cfg.rsi_low => -1,
                // no RSI confirmation: stay flat, but still exit an opposing position
                _ => 0,
            }
        };
        self.reconcile(ctx, desired)
    }

    fn name(&self) -> &'static str {
        "dual_regime"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    const DAY0_UTC_MIDNIGHT: i64 = 1_767_571_200;
    fn ict(day: i64, hh: i64, mm: i64) -> i64 {
        DAY0_UTC_MIDNIGHT + day * 86_400 + (hh - 7) * 3600 + mm * 60
    }
    fn bar_at(ts: i64, o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar {
            ts_open: ts - 300,
            ts_close: ts,
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 1.0,
        }
    }
    fn ctx(pos: Option<PositionView>, ts: i64) -> MarketCtx {
        MarketCtx {
            price: 0.0,
            volume: 0.0,
            side: None,
            timestamp: ts,
            position: pos,
        }
    }
    fn cfg() -> DualRegimeConfig {
        DualRegimeConfig {
            bar_seconds: 300,
            period: 2,
            multiplier: 1.0,
            rsi_period: 2,
            rsi_low: 30.0,
            rsi_high: 70.0,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn naked_regime_enters_on_uptrend() {
        let mut s = DualRegime::new(cfg());
        // day 0 has no prior day: warm-up, then rising closes in session
        let mut last = BtDecision::Hold;
        for (i, c) in [100.0, 101.0, 102.0, 103.0].iter().enumerate() {
            let ts = ict(0, 9, i as i64 * 5);
            last = s.on_bar(
                &bar_at(ts, *c - 0.5, *c + 0.5, *c - 1.0, *c),
                &ctx(None, ts),
            );
        }
        assert!(
            matches!(
                last,
                BtDecision::Enter {
                    direction: Dir::Long,
                    ..
                }
            ),
            "got {last:?}"
        );
    }

    #[test]
    fn warmup_holds() {
        let mut s = DualRegime::new(cfg());
        let ts = ict(0, 9, 0);
        assert!(matches!(
            s.on_bar(&bar_at(ts, 100.0, 101.0, 99.0, 100.0), &ctx(None, ts)),
            BtDecision::Hold
        ));
    }

    #[test]
    fn rangebound_without_rsi_confirm_holds() {
        let mut s = DualRegime::new(cfg());
        // day 0 builds a prior-day range of 90-110
        for (i, c) in [100.0, 95.0, 105.0, 100.0].iter().enumerate() {
            let ts = ict(0, 9, i as i64 * 5);
            s.on_bar(&bar_at(ts, *c, (*c) + 5.0, (*c) - 5.0, *c), &ctx(None, ts));
        }
        // day 1 opens inside it; a gentle uptrend never lifts RSI to 70, so no long
        let mut last = BtDecision::Hold;
        for (i, c) in [100.0, 100.2, 100.1, 100.3].iter().enumerate() {
            let ts = ict(1, 9, i as i64 * 5);
            last = s.on_bar(&bar_at(ts, *c, *c + 0.2, *c - 0.2, *c), &ctx(None, ts));
        }
        assert!(matches!(last, BtDecision::Hold), "got {last:?}");
    }
}
