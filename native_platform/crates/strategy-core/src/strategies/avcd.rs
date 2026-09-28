//! Anchored VWAP with CVD divergence (tick-driven, evaluated at bar closes).
//!
//! A bar closing more than `band_points` from the session VWAP while its CVD change opposes the
//! move is faded toward VWAP; exit near VWAP or at `stop_points`. Both reset each session.

use market_types::TickSide;
use orderflow_engine::anchored_vwap::AnchoredVwap;

use crate::strategy::{BtDecision, Dir, MarketCtx, Strategy};

fn ict_session_key(ts: i64) -> Option<(i64, u8)> {
    let tod = (ts + 7 * 3600).rem_euclid(86_400);
    let seg = if (32_400..41_400).contains(&tod) {
        0
    } else if (46_800..53_100).contains(&tod) {
        1
    } else {
        return None;
    };
    Some(((ts + 7 * 3600).div_euclid(86_400), seg))
}

#[derive(Clone, Copy, Debug)]
pub struct AvcdConfig {
    pub bar_seconds: i64,
    /// Points beyond VWAP required to consider price "extended" (published ~3).
    pub band_points: f64,
    /// Exit when price returns within this many points of VWAP (published ~0.5).
    pub exit_band_points: f64,
    /// Adverse stop in points from entry.
    pub stop_points: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for AvcdConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 300, // M5
            band_points: 3.0,
            exit_band_points: 0.5,
            stop_points: 4.0,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct Avcd {
    cfg: AvcdConfig,
    vwap: AnchoredVwap,
    session: Option<(i64, u8)>,
    /// Cumulative signed volume since the session anchor.
    cvd: f64,
    /// CVD at the previous bar close, for the per-bar change.
    prev_bar_cvd: Option<f64>,
    /// Current bar index and its last price.
    cur_bar_idx: Option<i64>,
    cur_close: f64,
}

impl Avcd {
    #[must_use]
    pub fn new(cfg: AvcdConfig) -> Self {
        Self {
            cfg,
            vwap: AnchoredVwap::new(),
            session: None,
            cvd: 0.0,
            prev_bar_cvd: None,
            cur_bar_idx: None,
            cur_close: 0.0,
        }
    }

    fn exit_decision(&self, ctx: &MarketCtx, vwap: f64) -> Option<BtDecision> {
        let pos = ctx.position?;
        let p = ctx.price;
        if (p - vwap).abs() <= self.cfg.exit_band_points {
            return Some(BtDecision::Exit {
                reason: "avcd_revert_vwap".into(),
            });
        }
        let adverse = match pos.dir {
            Dir::Long => p <= pos.entry_price - self.cfg.stop_points,
            Dir::Short => p >= pos.entry_price + self.cfg.stop_points,
        };
        if adverse {
            return Some(BtDecision::Exit {
                reason: "avcd_stop".into(),
            });
        }
        None
    }
}

impl Strategy for Avcd {
    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        let ts = ctx.timestamp;

        // session rollover: re-anchor VWAP and reset CVD
        let key = ict_session_key(ts);
        if key != self.session {
            self.session = key;
            self.vwap.reset();
            self.cvd = 0.0;
            self.prev_bar_cvd = None;
            self.cur_bar_idx = None;
        }
        // off-session: exits only
        let Some(_) = key else {
            if ctx.position.is_some() {
                if let Some(v) = self.vwap.value() {
                    if let Some(x) = self.exit_decision(ctx, v) {
                        return x;
                    }
                }
            }
            return BtDecision::Hold;
        };

        let idx = ts.div_euclid(self.cfg.bar_seconds);

        // new bar: evaluate the bar that just closed
        let mut signal: Option<Dir> = None;
        if let (Some(prev_idx), Some(vwap)) = (self.cur_bar_idx, self.vwap.value()) {
            if prev_idx != idx {
                let cvd_now = self.cvd;
                let cvd_delta = cvd_now - self.prev_bar_cvd.unwrap_or(cvd_now);
                self.prev_bar_cvd = Some(cvd_now);
                let dev = self.cur_close - vwap;
                if dev > self.cfg.band_points && cvd_delta < 0.0 {
                    signal = Some(Dir::Short); // extended up on falling CVD
                } else if dev < -self.cfg.band_points && cvd_delta > 0.0 {
                    signal = Some(Dir::Long); // extended down on rising CVD
                }
            }
        }

        // apply this tick's signed volume and VWAP sample, then track the bar
        let delta = match ctx.side {
            Some(TickSide::Buy) => ctx.volume,
            Some(TickSide::Sell) => -ctx.volume,
            None => 0.0,
        };
        self.cvd += delta;
        self.vwap.update(ctx.price, ctx.volume);
        self.cur_bar_idx = Some(idx);
        self.cur_close = ctx.price;
        if self.prev_bar_cvd.is_none() {
            self.prev_bar_cvd = Some(self.cvd);
        }

        // exits first, then entries when flat
        if ctx.position.is_some() {
            if let Some(v) = self.vwap.value() {
                if let Some(x) = self.exit_decision(ctx, v) {
                    return x;
                }
            }
            return BtDecision::Hold;
        }
        match signal {
            Some(Dir::Short) if self.cfg.enable_short => BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "avcd_fade_short".into(),
            },
            Some(Dir::Long) => BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "avcd_fade_long".into(),
            },
            _ => BtDecision::Hold,
        }
    }

    fn name(&self) -> &'static str {
        "avcd"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    const DAY0: i64 = 1_767_571_200; // a Monday, 00:00 UTC
    fn ict(hh: i64, mm: i64, ss: i64) -> i64 {
        DAY0 + (hh - 7) * 3600 + mm * 60 + ss
    }
    fn tick(ts: i64, price: f64, vol: f64, side: Option<TickSide>) -> MarketCtx {
        MarketCtx {
            price,
            volume: vol,
            side,
            timestamp: ts,
            position: None,
        }
    }

    fn cfg() -> AvcdConfig {
        AvcdConfig {
            bar_seconds: 60,
            band_points: 2.0,
            exit_band_points: 0.5,
            stop_points: 5.0,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn extended_up_with_falling_cvd_fades_short() {
        let mut s = Avcd::new(cfg());
        // bar 0 (09:00): heavy buying anchors VWAP at 100
        for i in 0..20 {
            s.on_tick(&tick(ict(9, 0, i), 100.0, 10.0, Some(TickSide::Buy)));
        }
        // bar 1: a push to 106 on light selling; CVD falls and price ends beyond the band
        let mut last = BtDecision::Hold;
        for i in 0..5 {
            last = s.on_tick(&tick(ict(9, 1, i), 106.0, 2.0, Some(TickSide::Sell)));
        }
        // bar 1 is evaluated on the first tick of bar 2
        let sig = s.on_tick(&tick(ict(9, 2, 0), 106.0, 1.0, Some(TickSide::Sell)));
        let fired = matches!(
            last,
            BtDecision::Enter {
                direction: Dir::Short,
                ..
            }
        ) || matches!(
            sig,
            BtDecision::Enter {
                direction: Dir::Short,
                ..
            }
        );
        assert!(fired, "expected a short fade, got {last:?} then {sig:?}");
    }

    #[test]
    fn revert_to_vwap_exits() {
        let s = Avcd::new(cfg());
        // establish VWAP near 100, then a short from 103 sees price back at VWAP
        let mut s = s;
        for i in 0..10 {
            s.on_tick(&tick(ict(9, 0, i), 100.0, 10.0, Some(TickSide::Buy)));
        }
        let mut ctx = tick(ict(9, 1, 0), 100.2, 1.0, Some(TickSide::Buy));
        ctx.position = Some(PositionView {
            dir: Dir::Short,
            qty: 1,
            entry_price: 103.0,
        });
        assert!(matches!(
            s.on_tick(&ctx),
            BtDecision::Exit { reason } if reason == "avcd_revert_vwap"
        ));
    }
}
