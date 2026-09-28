//! Advisory chandelier trailing stop: computes the stop and reports crossings; places no orders.
//!
//! ```text
//! initial stop = entry ∓ k_init × ATR(n)      (fallback ∓ fallback_stop_pts if ATR cold)
//! trailing     = extreme ∓ k_eff × ATR        (long: hh − k, short: ll + k)
//! k_eff        = k_trail × elevated_tighten   when VPIN regime == Elevated
//! ```
//!
//! The stop only ratchets (never widens), is rounded to the tick, and emits `Updated` only on a
//! move of at least one tick.

use orderflow_engine::vpin::VpinRegime;

use super::registry::Direction;

/// Trailing-stop parameters (VN30F1M defaults in [`Default`]).
#[derive(Debug, Clone, Copy)]
pub struct TrailingConfig {
    /// ATR period in bars.
    pub atr_period: usize,
    /// Initial stop distance multiplier: entry ∓ k_init × ATR.
    pub k_init: f64,
    /// Trailing distance multiplier: extreme ∓ k_trail × ATR.
    pub k_trail: f64,
    /// k_trail multiplier while VPIN regime is Elevated (tightens).
    pub elevated_tighten: f64,
    /// Instrument tick size (VN30F1M: 0.1 points).
    pub tick_size: f64,
    /// Initial stop distance in points while ATR is not yet available.
    pub fallback_stop_pts: f64,
}

impl Default for TrailingConfig {
    fn default() -> Self {
        Self {
            atr_period: 10,
            k_init: 2.0,
            k_trail: 1.5,
            elevated_tighten: 0.75,
            tick_size: 0.1,
            fallback_stop_pts: 1.5,
        }
    }
}

/// The single advisory position tracked by the pipeline.
#[derive(Debug, Clone, Copy)]
pub struct AdvisoryPosition {
    pub direction: Direction,
    pub entry_price: f64,
    pub entry_ts: i64,
    /// Current advisory stop (tick-rounded, monotonic).
    pub stop: f64,
    /// Favorable extreme since entry: hh (long) / ll (short).
    pub extreme: f64,
}

/// Outcome of a trailing-stop tick.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum StopEvent {
    None,
    /// Stop ratcheted by >= 1 tick.
    Updated {
        stop: f64,
    },
    /// Price crossed the stop — position closed (advisory exit).
    Hit {
        stop: f64,
        exit_price: f64,
    },
}

/// Holds the single advisory position and its chandelier trailing stop.
#[derive(Debug, Clone)]
pub struct TrailingStopManager {
    cfg: TrailingConfig,
    position: Option<AdvisoryPosition>,
}

impl TrailingStopManager {
    pub fn new(cfg: TrailingConfig) -> Self {
        Self {
            cfg,
            position: None,
        }
    }

    fn round_tick(&self, price: f64) -> f64 {
        let tick = self.cfg.tick_size.max(f64::EPSILON);
        (price / tick).round() * tick
    }

    /// Open an advisory position. No-op if one is already open or inputs
    /// are invalid (non-finite entry, Neutral direction).
    pub fn open(
        &mut self,
        direction: Direction,
        entry: f64,
        ts: i64,
        atr: Option<f64>,
        regime: VpinRegime,
    ) {
        if self.position.is_some() || !entry.is_finite() || direction == Direction::Neutral {
            return;
        }
        let _ = regime; // initial stop uses k_init regardless of regime
        let dist = match atr {
            Some(a) if a.is_finite() && a > 0.0 => self.cfg.k_init * a,
            _ => self.cfg.fallback_stop_pts,
        };
        let stop = match direction {
            Direction::Bullish => self.round_tick(entry - dist),
            Direction::Bearish => self.round_tick(entry + dist),
            Direction::Neutral => unreachable!(),
        };
        self.position = Some(AdvisoryPosition {
            direction,
            entry_price: entry,
            entry_ts: ts,
            stop,
            extreme: entry,
        });
    }

    /// Per-tick update: check the exit, then ratchet. The stop never widens, even when the
    /// regime returns to Normal.
    pub fn on_tick(
        &mut self,
        price: f64,
        _ts: i64,
        atr: Option<f64>,
        regime: VpinRegime,
    ) -> StopEvent {
        if !price.is_finite() {
            return StopEvent::None;
        }
        let Some(pos) = self.position.as_mut() else {
            return StopEvent::None;
        };

        // Exit first: price crossing the advisory stop closes the position.
        let crossed = match pos.direction {
            Direction::Bullish => price <= pos.stop,
            Direction::Bearish => price >= pos.stop,
            Direction::Neutral => false,
        };
        if crossed {
            let stop = pos.stop;
            self.position = None;
            return StopEvent::Hit {
                stop,
                exit_price: price,
            };
        }

        // Ratchet from the favorable extreme.
        match pos.direction {
            Direction::Bullish => pos.extreme = pos.extreme.max(price),
            Direction::Bearish => pos.extreme = pos.extreme.min(price),
            Direction::Neutral => {}
        }
        let Some(a) = atr.filter(|a| a.is_finite() && *a > 0.0) else {
            return StopEvent::None;
        };
        let k_eff = if regime == VpinRegime::Elevated {
            self.cfg.k_trail * self.cfg.elevated_tighten
        } else {
            self.cfg.k_trail
        };
        let tick = self.cfg.tick_size.max(f64::EPSILON);
        let improved = match pos.direction {
            Direction::Bullish => {
                let cand = (pos.extreme - k_eff * a) / tick;
                let cand = cand.round() * tick;
                if cand - pos.stop >= tick - 1e-9 {
                    pos.stop = cand;
                    true
                } else {
                    false
                }
            }
            Direction::Bearish => {
                let cand = ((pos.extreme + k_eff * a) / tick).round() * tick;
                if pos.stop - cand >= tick - 1e-9 {
                    pos.stop = cand;
                    true
                } else {
                    false
                }
            }
            Direction::Neutral => false,
        };
        if improved {
            StopEvent::Updated {
                stop: self.position.as_ref().map(|p| p.stop).unwrap_or(f64::NAN),
            }
        } else {
            StopEvent::None
        }
    }

    /// Session end / day reset: close the advisory position at `price`.
    pub fn force_exit(&mut self, price: f64) -> StopEvent {
        match self.position.take() {
            Some(pos) => StopEvent::Hit {
                stop: pos.stop,
                exit_price: price,
            },
            None => StopEvent::None,
        }
    }

    pub fn position(&self) -> Option<&AdvisoryPosition> {
        self.position.as_ref()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mgr() -> TrailingStopManager {
        TrailingStopManager::new(TrailingConfig::default())
    }

    #[test]
    fn long_initial_stop_from_atr() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        // entry − k_init×ATR = 1300 − 4.0 = 1296.0
        assert_eq!(m.position().unwrap().stop, 1296.0);
    }

    #[test]
    fn atr_cold_uses_fallback_stop() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, None, VpinRegime::Normal);
        assert!((m.position().unwrap().stop - 1298.5).abs() < 1e-9);
        let mut m = mgr();
        m.open(Direction::Bearish, 1300.0, 1, None, VpinRegime::Normal);
        assert!((m.position().unwrap().stop - 1301.5).abs() < 1e-9);
    }

    #[test]
    fn rising_prices_ratchet_long_stop_up_falling_never_widen() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        // hh=1305 → cand = 1305 − 1.5×2 = 1302.0 > 1296 → Updated.
        let ev = m.on_tick(1305.0, 2, Some(2.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::Updated { stop: 1302.0 });
        // Price falls (above stop): extreme + stop unchanged.
        let ev = m.on_tick(1303.0, 3, Some(2.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::None);
        assert_eq!(m.position().unwrap().stop, 1302.0);
        // a much larger ATR does not widen the stop
        let ev = m.on_tick(1303.0, 4, Some(10.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::None);
        assert_eq!(m.position().unwrap().stop, 1302.0);
    }

    #[test]
    fn sub_tick_improvement_throttled() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        m.on_tick(1305.0, 2, Some(2.0), VpinRegime::Normal);
        // stop is 1302.0; hh 1305.04 gives cand 1302.04, which rounds back to 1302.0
        let ev = m.on_tick(1305.04, 3, Some(2.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::None);
        assert_eq!(m.position().unwrap().stop, 1302.0);
    }

    #[test]
    fn stop_hit_clears_position_once() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        let ev = m.on_tick(1295.9, 2, Some(2.0), VpinRegime::Normal);
        assert_eq!(
            ev,
            StopEvent::Hit {
                stop: 1296.0,
                exit_price: 1295.9
            }
        );
        assert!(m.position().is_none());
        assert_eq!(
            m.on_tick(1290.0, 3, Some(2.0), VpinRegime::Normal),
            StopEvent::None
        );
    }

    #[test]
    fn elevated_regime_tightens_and_normal_does_not_widen() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        // Elevated: k_eff = 1.5 × 0.75 = 1.125, so cand = 1305 − 2.25 = 1302.75 → 1302.8;
        // compared approximately because 0.1-tick multiples are inexact in f64
        match m.on_tick(1305.0, 2, Some(2.0), VpinRegime::Elevated) {
            StopEvent::Updated { stop } => assert!((stop - 1302.8).abs() < 1e-9),
            other => panic!("expected Updated, got {other:?}"),
        }
        // Back to Normal with same extreme: cand 1302.0 < stop → ratchet holds.
        let ev = m.on_tick(1304.0, 3, Some(2.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::None);
        assert!((m.position().unwrap().stop - 1302.8).abs() < 1e-9);
    }

    #[test]
    fn short_mirror() {
        let mut m = mgr();
        m.open(Direction::Bearish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        assert_eq!(m.position().unwrap().stop, 1304.0);
        // initial stop 1300 + 4; ll 1295 gives cand 1295 + 3 = 1298.0 < 1304 → Updated
        let ev = m.on_tick(1295.0, 2, Some(2.0), VpinRegime::Normal);
        assert_eq!(ev, StopEvent::Updated { stop: 1298.0 });
        // Bounce to stop → Hit.
        let ev = m.on_tick(1298.0, 3, Some(2.0), VpinRegime::Normal);
        assert_eq!(
            ev,
            StopEvent::Hit {
                stop: 1298.0,
                exit_price: 1298.0
            }
        );
    }

    #[test]
    fn open_while_position_exists_is_noop() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        m.open(Direction::Bearish, 1310.0, 2, Some(2.0), VpinRegime::Normal);
        let pos = m.position().unwrap();
        assert_eq!(pos.direction, Direction::Bullish);
        assert_eq!(pos.entry_price, 1300.0);
    }

    #[test]
    fn nan_price_ignored_and_force_exit_clears() {
        let mut m = mgr();
        m.open(Direction::Bullish, 1300.0, 1, Some(2.0), VpinRegime::Normal);
        assert_eq!(
            m.on_tick(f64::NAN, 2, Some(2.0), VpinRegime::Normal),
            StopEvent::None
        );
        assert!(m.position().is_some());
        let ev = m.force_exit(1301.0);
        assert_eq!(
            ev,
            StopEvent::Hit {
                stop: 1296.0,
                exit_price: 1301.0
            }
        );
        assert!(m.position().is_none());
        assert_eq!(m.force_exit(1301.0), StopEvent::None);
        // NaN entry / Neutral direction refused.
        m.open(
            Direction::Bullish,
            f64::NAN,
            3,
            Some(2.0),
            VpinRegime::Normal,
        );
        assert!(m.position().is_none());
        m.open(Direction::Neutral, 1300.0, 3, Some(2.0), VpinRegime::Normal);
        assert!(m.position().is_none());
    }
}
