//! VWAP reversion on CVD exhaustion (tick-driven): fade a new session extreme `band_points` or
//! more from VWAP when CVD fails to confirm it; exit near VWAP or at `stop_points`.
//!
//! VWAP and CVD reset at each session open.

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
pub struct VwapRevCvdConfig {
    /// Points beyond VWAP required to consider price "extended".
    pub band_points: f64,
    /// Exit when price returns within this many points of VWAP.
    pub exit_band_points: f64,
    /// Adverse stop in points from entry.
    pub stop_points: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for VwapRevCvdConfig {
    fn default() -> Self {
        Self {
            band_points: 3.0,
            exit_band_points: 0.5,
            stop_points: 5.0,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct VwapRevCvd {
    cfg: VwapRevCvdConfig,
    vwap: AnchoredVwap,
    session: Option<(i64, u8)>,
    /// Cumulative signed volume since the session anchor.
    cvd: f64,
    /// Session high and the CVD when it was set.
    hi_ref: Option<(f64, f64)>,
    /// Session low and the CVD when it was set.
    lo_ref: Option<(f64, f64)>,
}

impl VwapRevCvd {
    #[must_use]
    pub fn new(cfg: VwapRevCvdConfig) -> Self {
        Self {
            cfg,
            vwap: AnchoredVwap::new(),
            session: None,
            cvd: 0.0,
            hi_ref: None,
            lo_ref: None,
        }
    }

    fn exit_decision(&self, ctx: &MarketCtx, vwap: f64) -> Option<BtDecision> {
        let pos = ctx.position?;
        let p = ctx.price;
        if (p - vwap).abs() <= self.cfg.exit_band_points {
            return Some(BtDecision::Exit {
                reason: "vwaprev_revert".into(),
            });
        }
        let adverse = match pos.dir {
            Dir::Long => p <= pos.entry_price - self.cfg.stop_points,
            Dir::Short => p >= pos.entry_price + self.cfg.stop_points,
        };
        if adverse {
            return Some(BtDecision::Exit {
                reason: "vwaprev_stop".into(),
            });
        }
        None
    }
}

impl Strategy for VwapRevCvd {
    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        let key = ict_session_key(ctx.timestamp);
        if key != self.session {
            self.session = key;
            self.vwap.reset();
            self.cvd = 0.0;
            self.hi_ref = None;
            self.lo_ref = None;
        }

        // fold in this tick's signed volume and VWAP sample
        let delta = match ctx.side {
            Some(TickSide::Buy) => ctx.volume,
            Some(TickSide::Sell) => -ctx.volume,
            None => 0.0,
        };
        self.cvd += delta;
        self.vwap.update(ctx.price, ctx.volume);

        // off-session: exits only
        if key.is_none() {
            if let Some(v) = self.vwap.value() {
                if let Some(x) = self.exit_decision(ctx, v) {
                    return x;
                }
            }
            return BtDecision::Hold;
        }

        let price = ctx.price;
        let mut signal: Option<Dir> = None;
        if let Some(v) = self.vwap.value() {
            let dev = price - v;
            // bearish divergence: extended above VWAP, new high on weaker CVD
            if dev >= self.cfg.band_points {
                if let Some((ph, pc)) = self.hi_ref {
                    if price > ph && self.cvd < pc {
                        signal = Some(Dir::Short);
                    }
                }
            }
            // bullish divergence: extended below VWAP, new low on stronger CVD
            if dev <= -self.cfg.band_points {
                if let Some((pl, pc)) = self.lo_ref {
                    if price < pl && self.cvd > pc {
                        signal = Some(Dir::Long);
                    }
                }
            }
        }

        // update the extremes after the divergence check
        if self.hi_ref.is_none_or(|(ph, _)| price > ph) {
            self.hi_ref = Some((price, self.cvd));
        }
        if self.lo_ref.is_none_or(|(pl, _)| price < pl) {
            self.lo_ref = Some((price, self.cvd));
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
                reason: "vwaprev_fade_short".into(),
            },
            Some(Dir::Long) => BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "vwaprev_fade_long".into(),
            },
            _ => BtDecision::Hold,
        }
    }

    fn name(&self) -> &'static str {
        "vwap_reversion_cvd"
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
    fn cfg() -> VwapRevCvdConfig {
        VwapRevCvdConfig {
            band_points: 2.0,
            exit_band_points: 0.5,
            stop_points: 8.0,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn new_high_weak_cvd_fades_short() {
        let mut s = VwapRevCvd::new(cfg());
        // anchor VWAP near 100 on buy flow
        for i in 0..10 {
            s.on_tick(&tick(ict(9, 0, i), 100.0, 10.0, Some(TickSide::Buy)));
        }
        // prior high at 103 on buy flow
        s.on_tick(&tick(ict(9, 1, 0), 103.0, 10.0, Some(TickSide::Buy)));
        // new high at 104 on sell flow: CVD below its level at 103, a bearish divergence
        let dec = s.on_tick(&tick(ict(9, 1, 5), 104.0, 30.0, Some(TickSide::Sell)));
        assert!(
            matches!(
                dec,
                BtDecision::Enter {
                    direction: Dir::Short,
                    ..
                }
            ),
            "got {dec:?}"
        );
    }

    #[test]
    fn new_high_strong_cvd_holds() {
        let mut s = VwapRevCvd::new(cfg());
        for i in 0..10 {
            s.on_tick(&tick(ict(9, 0, i), 100.0, 10.0, Some(TickSide::Buy)));
        }
        s.on_tick(&tick(ict(9, 1, 0), 103.0, 10.0, Some(TickSide::Buy)));
        // new high on more buying: CVD confirms, no divergence
        let dec = s.on_tick(&tick(ict(9, 1, 5), 104.0, 30.0, Some(TickSide::Buy)));
        assert!(matches!(dec, BtDecision::Hold), "got {dec:?}");
    }

    #[test]
    fn revert_to_vwap_exits() {
        let mut s = VwapRevCvd::new(cfg());
        for i in 0..10 {
            s.on_tick(&tick(ict(9, 0, i), 100.0, 10.0, Some(TickSide::Buy)));
        }
        let mut ctx = tick(ict(9, 1, 0), 100.2, 1.0, Some(TickSide::Buy));
        ctx.position = Some(PositionView {
            dir: Dir::Short,
            qty: 1,
            entry_price: 104.0,
        });
        assert!(matches!(
            s.on_tick(&ctx),
            BtDecision::Exit { reason } if reason == "vwaprev_revert"
        ));
    }
}
