//! Value-area edge rejection fade toward the POC (bar-driven), after Dalton, *Markets in Profile*.
//!
//! The prior value area comes from the strategy's own volume-at-price profile. A bar beyond an
//! edge arms a watch; a close back inside within `reject_confirm_bars` fades toward the POC.

use orderflow_engine::volume_profile::VolumeProfile;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

/// VN30F1M session position `(segment, minute_since_open)`; `None` off-session.
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
pub struct VaEdgeConfig {
    pub bar_seconds: i64,
    /// Bars an edge probe may stay armed before it is deemed accepted (no fade).
    pub reject_confirm_bars: u32,
    /// Adverse stop in points from entry.
    pub stop_points: f64,
    /// Max bars to hold before a time exit.
    pub hold_bars: u32,
    /// No fresh entry past this many minutes from the segment open.
    pub entry_window_min: i64,
    /// Volume-profile bucket size in points (the VN30F1M tick is 0.1).
    pub bucket_points: f64,
    /// Value-area fraction (0.70 = the auction standard).
    pub value_area_pct: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for VaEdgeConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 300, // M5
            reject_confirm_bars: 2,
            stop_points: 4.0,
            hold_bars: 12,
            entry_window_min: 150,
            bucket_points: 0.1,
            value_area_pct: orderflow_engine::volume_profile::DEFAULT_VALUE_AREA_PCT,
            enable_short: true,
            qty: 1,
        }
    }
}

/// Prior-session value area, from its volume-at-price profile.
#[derive(Clone, Copy, Debug)]
struct ValueArea {
    vah: f64,
    val: f64,
    poc: f64,
}

pub struct VaEdgeRotation {
    cfg: VaEdgeConfig,
    /// Session key `(ict_day, segment)`.
    session: Option<(i64, u8)>,
    entered_this_session: bool,
    /// Armed probe and bars since arming: `Dir::Short` for a VAH probe, `Dir::Long` for VAL.
    armed: Option<(Dir, u32)>,
    bars_held: u32,
    /// Prior-session value area, frozen at the day rollover.
    va: Option<ValueArea>,
    /// Current ICT day; its profile is frozen into `va` at the day change, then reset.
    cur_day: Option<i64>,
    cur_profile: VolumeProfile,
}

impl VaEdgeRotation {
    #[must_use]
    pub fn new(cfg: VaEdgeConfig) -> Self {
        Self {
            cfg,
            session: None,
            entered_this_session: false,
            armed: None,
            bars_held: 0,
            va: None,
            cur_day: None,
            cur_profile: VolumeProfile::new(cfg.bucket_points, cfg.value_area_pct),
        }
    }

    /// The prior session's value area `(poc, val, vah)`, once one exists.
    #[must_use]
    pub fn prior_value_area(&self) -> Option<(f64, f64, f64)> {
        self.va.map(|va| (va.poc, va.val, va.vah))
    }

    fn fade_enter(&self, edge: Dir) -> Option<BtDecision> {
        match edge {
            Dir::Short if self.cfg.enable_short => Some(BtDecision::Enter {
                direction: Dir::Short,
                qty: self.cfg.qty,
                reason: "va_edge_short".into(),
            }),
            Dir::Short => None,
            Dir::Long => Some(BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "va_edge_long".into(),
            }),
        }
    }
}

impl Strategy for VaEdgeRotation {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        // day rollover: freeze the completed session's value area (closed bars only), then reset
        let day = ict_day(bar.ts_close);
        if self.cur_day != Some(day) {
            if self.cur_day.is_some() {
                if let Some((poc, val, vah)) = self.cur_profile.value_area() {
                    self.va = Some(ValueArea { vah, val, poc });
                }
            }
            self.cur_day = Some(day);
            self.cur_profile.reset();
        }
        // a bar without volume still carries price information: weight it 1
        let volume = if bar.volume.is_finite() && bar.volume > 0.0 {
            bar.volume
        } else {
            1.0
        };
        self.cur_profile.observe_bar(bar.high, bar.low, volume);

        // session rollover
        let pos = session_pos(bar.ts_close);
        let key = pos.map(|(seg, _)| (day, seg));
        if key != self.session {
            self.session = key;
            self.entered_this_session = false;
            self.armed = None;
        }

        // open position: POC target, stop or time exit
        if let Some(p) = ctx.position {
            self.bars_held = self.bars_held.saturating_add(1);
            if let Some(va) = self.va {
                let target_hit = match p.dir {
                    Dir::Long => bar.close >= va.poc,
                    Dir::Short => bar.close <= va.poc,
                };
                if target_hit {
                    return BtDecision::Exit {
                        reason: "va_edge_poc".into(),
                    };
                }
            }
            let stop_hit = match p.dir {
                Dir::Long => bar.close <= p.entry_price - self.cfg.stop_points,
                Dir::Short => bar.close >= p.entry_price + self.cfg.stop_points,
            };
            if stop_hit {
                return BtDecision::Exit {
                    reason: "va_edge_stop".into(),
                };
            }
            if self.bars_held >= self.cfg.hold_bars {
                return BtDecision::Exit {
                    reason: "va_edge_time".into(),
                };
            }
            return BtDecision::Hold;
        }
        self.bars_held = 0;

        // flat: needs a prior-day VA, an open session, the entry window, one entry per session
        if self.entered_this_session {
            return BtDecision::Hold;
        }
        let Some(va) = self.va else {
            return BtDecision::Hold; // first day: no prior-day reference yet
        };
        let Some((_, minute)) = pos else {
            return BtDecision::Hold;
        };
        if minute > self.cfg.entry_window_min {
            self.armed = None;
            return BtDecision::Hold;
        }

        match self.armed {
            None => {
                if bar.high >= va.vah {
                    // immediate rejection: probed VAH, closed back below it
                    if bar.close < va.vah {
                        if let Some(dec) = self.fade_enter(Dir::Short) {
                            self.entered_this_session = true;
                            return dec;
                        }
                    } else {
                        self.armed = Some((Dir::Short, 0));
                    }
                } else if bar.low <= va.val {
                    if bar.close > va.val {
                        self.entered_this_session = true;
                        return self.fade_enter(Dir::Long).unwrap();
                    }
                    self.armed = Some((Dir::Long, 0));
                }
                BtDecision::Hold
            }
            Some((edge, age)) => {
                let age = age + 1;
                if age > self.cfg.reject_confirm_bars {
                    self.armed = None; // accepted beyond the edge: no fade
                    return BtDecision::Hold;
                }
                let rejected = match edge {
                    Dir::Short => bar.close < va.vah, // back inside from above
                    Dir::Long => bar.close > va.val,  // back inside from below
                };
                if rejected {
                    if let Some(dec) = self.fade_enter(edge) {
                        self.armed = None;
                        self.entered_this_session = true;
                        return dec;
                    }
                    self.armed = None;
                    return BtDecision::Hold;
                }
                self.armed = Some((edge, age));
                BtDecision::Hold
            }
        }
    }

    fn name(&self) -> &'static str {
        "va_edge_rotation"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    const DAY0_UTC_MIDNIGHT: i64 = 1_767_571_200; // a Monday, 00:00 UTC
    fn ict_on(day: i64, hh: i64, mm: i64) -> i64 {
        DAY0_UTC_MIDNIGHT + day * 86_400 + (hh - 7) * 3600 + mm * 60
    }
    fn bar_at(ts: i64, o: f64, h: f64, l: f64, c: f64) -> Bar {
        bar_vol(ts, o, h, l, c, 10.0)
    }
    fn bar_vol(ts: i64, o: f64, h: f64, l: f64, c: f64, volume: f64) -> Bar {
        Bar {
            ts_open: ts - 60,
            ts_close: ts,
            open: o,
            high: h,
            low: l,
            close: c,
            volume,
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
    fn cfg() -> VaEdgeConfig {
        VaEdgeConfig {
            bar_seconds: 60,
            reject_confirm_bars: 2,
            stop_points: 4.0,
            hold_bars: 12,
            entry_window_min: 150,
            bucket_points: 0.1,
            value_area_pct: 0.70,
            enable_short: true,
            qty: 1,
        }
    }

    /// Day-0 session with its volume in 104-106 and thin excursions to 100 and 110.
    fn seed_day0(s: &mut VaEdgeRotation) {
        for m in 0..20 {
            let ts = ict_on(0, 9, m);
            s.on_bar(
                &bar_vol(ts, 105.0, 106.0, 104.0, 105.0, 100.0),
                &ctx(None, ts),
            );
        }
        // a cluster at 105.0 makes the POC unambiguous
        for m in 20..30 {
            let ts = ict_on(0, 9, m);
            s.on_bar(
                &bar_vol(ts, 105.0, 105.0, 105.0, 105.0, 200.0),
                &ctx(None, ts),
            );
        }
        // asymmetric shoulder: on a symmetric shelf every tie expands downward, leaving the POC
        // on the upper edge of the value area instead of strictly inside it
        for m in 30..36 {
            let ts = ict_on(0, 9, m);
            s.on_bar(
                &bar_vol(ts, 105.3, 105.5, 105.1, 105.3, 100.0),
                &ctx(None, ts),
            );
        }
        for m in 36..39 {
            let ts = ict_on(0, 9, m);
            s.on_bar(
                &bar_vol(ts, 104.7, 104.9, 104.5, 104.7, 100.0),
                &ctx(None, ts),
            );
        }
        // thin excursions that must fall outside value
        let ts = ict_on(0, 9, 39);
        s.on_bar(
            &bar_vol(ts, 106.0, 110.0, 106.0, 106.2, 5.0),
            &ctx(None, ts),
        );
        let ts = ict_on(0, 9, 40);
        s.on_bar(
            &bar_vol(ts, 104.0, 104.0, 100.0, 103.8, 5.0),
            &ctx(None, ts),
        );
    }

    /// `(poc, val, vah)` of the seeded prior session, as computed by the strategy.
    fn seeded_va(s: &VaEdgeRotation) -> (f64, f64, f64) {
        s.prior_value_area().expect("prior session value area")
    }

    #[test]
    fn prior_value_area_is_volume_weighted_not_the_days_extremes() {
        let mut s = VaEdgeRotation::new(cfg());
        seed_day0(&mut s);
        // roll the day so the completed session is frozen
        let ts = ict_on(1, 9, 0);
        s.on_bar(&bar_at(ts, 105.0, 105.1, 104.9, 105.0), &ctx(None, ts));

        let (poc, val, vah) = seeded_va(&s);
        assert!(val < poc && poc < vah, "va must bracket the poc");
        assert!(
            vah < 110.0 && val > 100.0,
            "the thin excursions to 100/110 must fall OUTSIDE value, got val={val} vah={vah}"
        );
        assert!(
            (104.0..=106.0).contains(&poc),
            "the POC belongs on the busy shelf, got {poc}"
        );
    }

    #[test]
    fn vah_rejection_fades_short() {
        let mut s = VaEdgeRotation::new(cfg());
        seed_day0(&mut s);
        // roll into day 1 with a quiet inside bar, then read the frozen VA
        let ts = ict_on(1, 9, 0);
        s.on_bar(&bar_at(ts, 105.0, 105.1, 104.9, 105.0), &ctx(None, ts));
        let (_, _, vah) = seeded_va(&s);

        // probe above VAH and close back inside: short fade
        let ts = ict_on(1, 9, 1);
        match s.on_bar(
            &bar_at(ts, vah, vah + 1.0, vah - 0.5, vah - 0.3),
            &ctx(None, ts),
        ) {
            BtDecision::Enter {
                direction: Dir::Short,
                ..
            } => {}
            other => panic!("expected Enter Short, got {other:?}"),
        }
    }

    #[test]
    fn val_rejection_fades_long_delayed() {
        let mut s = VaEdgeRotation::new(cfg());
        seed_day0(&mut s);
        let ts = ict_on(1, 9, 0);
        s.on_bar(&bar_at(ts, 105.0, 105.1, 104.9, 105.0), &ctx(None, ts));
        let (_, val, _) = seeded_va(&s);

        // close below VAL: armed, no immediate fade
        let ts = ict_on(1, 9, 1);
        assert!(matches!(
            s.on_bar(
                &bar_at(ts, val, val + 0.1, val - 1.0, val - 0.5),
                &ctx(None, ts)
            ),
            BtDecision::Hold
        ));
        // next bar closes back inside: long fade
        let ts = ict_on(1, 9, 2);
        match s.on_bar(
            &bar_at(ts, val - 0.5, val + 0.5, val - 0.6, val + 0.3),
            &ctx(None, ts),
        ) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected Enter Long, got {other:?}"),
        }
    }

    #[test]
    fn acceptance_beyond_edge_no_fade() {
        let mut s = VaEdgeRotation::new(cfg());
        seed_day0(&mut s);
        let ts = ict_on(1, 9, 0);
        s.on_bar(&bar_at(ts, 105.0, 105.1, 104.9, 105.0), &ctx(None, ts));
        let (_, _, vah) = seeded_va(&s);

        // closes above VAH past the reject window: accepted, nothing to fade
        let mut last = BtDecision::Hold;
        for m in 1..5 {
            let ts = ict_on(1, 9, m);
            let base = vah + 5.0;
            last = s.on_bar(
                &bar_at(ts, base, base + 1.5, base - 0.1, base + 1.0),
                &ctx(None, ts),
            );
        }
        assert!(matches!(last, BtDecision::Hold), "got {last:?}");
    }

    #[test]
    fn no_entry_on_first_day_without_prior_va() {
        let mut s = VaEdgeRotation::new(cfg());
        // day 0 has no prior-day reference, so even sharp moves produce no entry
        for m in 0..10 {
            let ts = ict_on(0, 9, m);
            let dec = s.on_bar(&bar_at(ts, 100.0, 115.0, 95.0, 96.0), &ctx(None, ts));
            assert!(matches!(dec, BtDecision::Hold), "bar {m}: got {dec:?}");
        }
    }

    #[test]
    fn poc_target_exit() {
        let mut s = VaEdgeRotation::new(cfg());
        seed_day0(&mut s);
        let ts = ict_on(1, 9, 0);
        s.on_bar(&bar_at(ts, 108.0, 108.1, 107.9, 108.0), &ctx(None, ts));
        let (poc, _, _) = seeded_va(&s);

        let short = Some(PositionView {
            dir: Dir::Short,
            qty: 1,
            entry_price: poc + 4.0,
        });
        // a close at or below the POC exits the short at target
        let ts = ict_on(1, 9, 1);
        assert!(matches!(
            s.on_bar(
                &bar_at(ts, poc + 1.0, poc + 1.2, poc - 0.2, poc),
                &ctx(short, ts)
            ),
            BtDecision::Exit { reason } if reason == "va_edge_poc"
        ));
    }
}
