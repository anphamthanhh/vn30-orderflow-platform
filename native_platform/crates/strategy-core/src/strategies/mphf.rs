//! Market-profile POC reversion (bar-driven): fade a close more than `band_points` from the
//! rolling volume POC; exit near the POC or after `hold_bars`.
//!
//! The POC comes from a volume-at-price profile over the last `window_bars` bars.

use orderflow_engine::volume_profile::RollingVolumeProfile;

use crate::strategy::{Bar, BtDecision, Dir, MarketCtx, Strategy};

#[derive(Clone, Copy, Debug)]
pub struct MphfConfig {
    pub bar_seconds: i64,
    /// Rolling window length in bars.
    pub window_bars: usize,
    /// Profile bucket size in points (the tick is 0.1).
    pub bucket_points: f64,
    /// Distance from the POC, in points, required to fade.
    pub band_points: f64,
    /// Exit when price returns within this many points of the POC.
    pub exit_band_points: f64,
    /// Max bars to hold before a time exit.
    pub hold_bars: u32,
    /// Value-area fraction of the rolling profile, used only by [`Mphf::value_area`].
    pub value_area_pct: f64,
    pub enable_short: bool,
    pub qty: u32,
}

impl Default for MphfConfig {
    fn default() -> Self {
        Self {
            bar_seconds: 60, // M1
            window_bars: 60,
            bucket_points: 0.5,
            band_points: 4.0,
            exit_band_points: 1.0,
            hold_bars: 3,
            value_area_pct: orderflow_engine::volume_profile::DEFAULT_VALUE_AREA_PCT,
            enable_short: true,
            qty: 1,
        }
    }
}

pub struct Mphf {
    cfg: MphfConfig,
    profile: RollingVolumeProfile,
    bars_held: u32,
}

impl Mphf {
    #[must_use]
    pub fn new(cfg: MphfConfig) -> Self {
        Self {
            cfg,
            profile: RollingVolumeProfile::new(
                cfg.window_bars,
                cfg.bucket_points,
                cfg.value_area_pct,
            ),
            bars_held: 0,
        }
    }

    /// Volume POC of the rolling window, or `None` until the window is full.
    fn poc(&self) -> Option<f64> {
        if !self.profile.is_full() {
            return None;
        }
        self.profile.poc()
    }

    /// `(poc, val, vah)` of the rolling window for diagnostics; `None` until the window is full.
    #[must_use]
    pub fn value_area(&self) -> Option<(f64, f64, f64)> {
        if !self.profile.is_full() {
            return None;
        }
        self.profile.value_area()
    }
}

impl Strategy for Mphf {
    fn on_bar(&mut self, bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        // open position: exit near the POC or on time
        if ctx.position.is_some() {
            self.bars_held = self.bars_held.saturating_add(1);
            if let Some(poc) = self.poc() {
                if (bar.close - poc).abs() <= self.cfg.exit_band_points {
                    self.observe(bar);
                    return BtDecision::Exit {
                        reason: "mphf_revert_poc".into(),
                    };
                }
            }
            let timed = self.bars_held >= self.cfg.hold_bars;
            self.observe(bar);
            if timed {
                return BtDecision::Exit {
                    reason: "mphf_time".into(),
                };
            }
            return BtDecision::Hold;
        }
        self.bars_held = 0;

        let poc = self.poc();
        self.observe(bar);
        let Some(poc) = poc else {
            return BtDecision::Hold; // window not full
        };
        let dev = bar.close - poc;
        if dev > self.cfg.band_points {
            if self.cfg.enable_short {
                BtDecision::Enter {
                    direction: Dir::Short,
                    qty: self.cfg.qty,
                    reason: "mphf_fade_short".into(),
                }
            } else {
                BtDecision::Hold
            }
        } else if dev < -self.cfg.band_points {
            BtDecision::Enter {
                direction: Dir::Long,
                qty: self.cfg.qty,
                reason: "mphf_fade_long".into(),
            }
        } else {
            BtDecision::Hold
        }
    }

    fn name(&self) -> &'static str {
        "mphf"
    }
}

impl Mphf {
    /// Add a completed bar to the rolling profile; a bar without volume gets weight 1 rather
    /// than being dropped, so the window keeps its length.
    fn observe(&mut self, bar: &Bar) {
        let volume = if bar.volume.is_finite() && bar.volume > 0.0 {
            bar.volume
        } else {
            1.0
        };
        self.profile.push_bar(bar.high, bar.low, volume);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::PositionView;

    fn bar(close: f64) -> Bar {
        Bar {
            ts_open: 0,
            ts_close: 0,
            open: close,
            high: close + 0.2,
            low: close - 0.2,
            close,
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
    fn cfg() -> MphfConfig {
        MphfConfig {
            bar_seconds: 60,
            window_bars: 5,
            bucket_points: 0.5,
            band_points: 2.0,
            exit_band_points: 0.5,
            hold_bars: 3,
            value_area_pct: 0.70,
            enable_short: true,
            qty: 1,
        }
    }

    #[test]
    fn holds_until_window_full() {
        let mut s = Mphf::new(cfg());
        for _ in 0..4 {
            assert!(matches!(
                s.on_bar(&bar(100.0), &ctx(None)),
                BtDecision::Hold
            ));
        }
    }

    #[test]
    fn extension_above_poc_fades_short() {
        let mut s = Mphf::new(cfg());
        // fill the window at 100, so the POC is 100
        for _ in 0..5 {
            s.on_bar(&bar(100.0), &ctx(None));
        }
        // 103 is beyond the 2-point band: fade short
        match s.on_bar(&bar(103.0), &ctx(None)) {
            BtDecision::Enter {
                direction: Dir::Short,
                ..
            } => {}
            other => panic!("expected short fade, got {other:?}"),
        }
    }

    #[test]
    fn extension_below_poc_fades_long() {
        let mut s = Mphf::new(cfg());
        for _ in 0..5 {
            s.on_bar(&bar(100.0), &ctx(None));
        }
        match s.on_bar(&bar(97.0), &ctx(None)) {
            BtDecision::Enter {
                direction: Dir::Long,
                ..
            } => {}
            other => panic!("expected long fade, got {other:?}"),
        }
    }

    #[test]
    fn time_exit_after_hold_bars() {
        let mut s = Mphf::new(cfg());
        for _ in 0..5 {
            s.on_bar(&bar(100.0), &ctx(None));
        }
        let short = Some(PositionView {
            dir: Dir::Short,
            qty: 1,
            entry_price: 103.0,
        });
        // stay at 103, outside the exit band, for hold_bars: time exit
        let mut last = BtDecision::Hold;
        for _ in 0..3 {
            last = s.on_bar(&bar(103.0), &ctx(short));
        }
        assert!(matches!(last, BtDecision::Exit { .. }), "got {last:?}");
    }

    /// A few heavy bars outweigh more light ones: the POC follows volume, not bar count.
    #[test]
    fn poc_follows_volume_not_bar_closes() {
        fn heavy(close: f64, volume: f64) -> Bar {
            Bar {
                ts_open: 0,
                ts_close: 0,
                open: close,
                high: close,
                low: close,
                close,
                volume,
            }
        }

        let mut s = Mphf::new(cfg());
        // window of 5: three light bars at 100, two heavy ones at 106
        s.on_bar(&heavy(100.0, 1.0), &ctx(None));
        s.on_bar(&heavy(100.0, 1.0), &ctx(None));
        s.on_bar(&heavy(100.0, 1.0), &ctx(None));
        s.on_bar(&heavy(106.0, 500.0), &ctx(None));
        s.on_bar(&heavy(106.0, 500.0), &ctx(None));

        let (poc, val, vah) = s.value_area().expect("window is full");
        assert!(
            (poc - 106.0).abs() < 1e-9,
            "POC must follow volume, got {poc}"
        );
        assert!(val <= poc && poc <= vah, "value area must bracket the POC");
    }

    #[test]
    fn no_reference_until_the_window_is_full() {
        let mut s = Mphf::new(cfg()); // window 5
        for _ in 0..4 {
            s.on_bar(&bar(100.0), &ctx(None));
            assert!(
                s.value_area().is_none(),
                "a partially-formed profile must not be readable"
            );
        }
        s.on_bar(&bar(100.0), &ctx(None));
        assert!(s.value_area().is_some());
    }
}
