//! Fill simulation for VN30F1M: DNSE fee schedule, derivatives PIT and session gating.
//!
//! Per contract and side: transaction fee 3,700 VND (DNSE 1,000 + exchange 2,700) and PIT 0.1 %
//! of `price × multiplier × qty × IM / 2` (Thông tư 87/2026/TT-BTC; see ADR 0020); position fee
//! 2,550 VND per ICT day held. Market fills pay one tick (0.1 pt) of slippage. Account-level
//! VSDC, bank and margin fees are outside this per-trade model.

use crate::emit::TradeRecord;
use crate::portfolio::{Portfolio, Position};
use crate::strategy::{dir_str, Action, BtDecision, Dir, Fill};

/// Fill price model: how the adverse offset from `mid` is computed.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FillModel {
    /// Taker at the next tick: adverse = `slippage_ticks * tick_size`. The default.
    Marketable,
    /// Taker paying the half-spread: adverse = `half_spread_pts` per fill. The pre-registered
    /// research floor is 0.5 pt (`historical/research_lab/PRE_REGISTRATION.md`).
    WorseSide { half_spread_pts: f64 },
    /// Upper bound for a passive fill: always fills at the touch and earns `half_spread_pts`.
    ///
    /// Ignores queue position, non-fills and adverse selection; with `WorseSide` it brackets
    /// real maker performance.
    Passive { half_spread_pts: f64 },
    /// Passive fill with queue and non-fill accounting, between `Passive` and `WorseSide`
    /// (ADR 0023). Expected adverse offset per fill:
    ///
    /// ```text
    /// adverse = p * (adverse_selection_pts - half_spread_pts) + (1 - p) * chase_pts
    /// ```
    ///
    /// `p` is an expectation, not a random draw: use it for expectancy, not tail statistics.
    PassiveQueue {
        /// Points earned when the resting order fills at the touch (half the spread).
        half_spread_pts: f64,
        /// Probability of a fill within the decision horizon.
        fill_prob: f64,
        /// Points given back to adverse selection when the order fills.
        adverse_selection_pts: f64,
        /// Points paid on a non-fill, when the trader chases at the horizon.
        chase_pts: f64,
    },
}

/// Calibration for [`FillModel::PassiveQueue`] from the E1 passive-fill screen.
///
/// See `docs/knowledge/03-execution-realism-and-fill-models.md`. Mirrored in
/// `historical/backtest_engine/costs.py` and checked by `historical/tests/test_fee_parity.py`.
pub mod e1 {
    /// Half of the measured mean spread (0.24 pt): what a touch fill earns.
    pub const HALF_SPREAD_PTS: f64 = 0.12;
    /// Low end of the measured 90–96 % fill band.
    pub const FILL_PROB: f64 = 0.90;
    /// Adverse-selection residual: a passive fill kept 0.077 of the 0.12 half-spread.
    pub const ADVERSE_SELECTION_PTS: f64 = 0.045;
    /// Chasing at the horizon pays the half-spread (`mid - ask` at t+H).
    pub const CHASE_PTS: f64 = 0.12;
}

/// VN30F1M fee and microstructure constants; money in VND, prices in points.
#[derive(Clone, Copy, Debug)]
pub struct FillConfig {
    pub tick_size: f64,
    pub multiplier: f64,
    pub txn_fee_vnd_per_side: f64,
    pub position_fee_vnd_per_day: f64,
    /// Derivatives PIT rate (0.1 %), applied to the reduced base (see `side_fee`).
    pub income_tax_rate: f64,
    /// VSDC initial-margin ratio; the PIT base is `notional × ratio / 2`.
    pub initial_margin_ratio: f64,
    pub slippage_ticks: f64,
    pub initial_cash: f64,
    /// How fill prices are offset from mid (default `Marketable`).
    pub fill_model: FillModel,
}

impl Default for FillConfig {
    /// DNSE VN30F1M schedule.
    fn default() -> Self {
        Self {
            tick_size: 0.1,
            multiplier: 100_000.0,
            txn_fee_vnd_per_side: 3_700.0,
            position_fee_vnd_per_day: 2_550.0,
            income_tax_rate: 0.001, // 0.1 % derivatives PIT on the reduced base
            initial_margin_ratio: 0.17, // VSDC VN30F IM; PIT base = notional × 0.17/2
            slippage_ticks: 1.0,
            initial_cash: 100_000_000.0,
            fill_model: FillModel::Marketable,
        }
    }
}

impl FillConfig {
    /// Default DNSE config with a [`FillModel::WorseSide`] fill.
    #[must_use]
    pub fn worse_side(half_spread_pts: f64) -> Self {
        Self {
            fill_model: FillModel::WorseSide { half_spread_pts },
            ..Self::default()
        }
    }

    /// Default DNSE config with a [`FillModel::Passive`] fill (optimistic upper bound).
    #[must_use]
    pub fn passive(half_spread_pts: f64) -> Self {
        Self {
            fill_model: FillModel::Passive { half_spread_pts },
            ..Self::default()
        }
    }

    /// Default config with a [`FillModel::PassiveQueue`] fill; `fill_prob` is clamped to `[0, 1]`.
    #[must_use]
    pub fn passive_queue(
        half_spread_pts: f64,
        fill_prob: f64,
        adverse_selection_pts: f64,
        chase_pts: f64,
    ) -> Self {
        Self {
            fill_model: FillModel::PassiveQueue {
                half_spread_pts,
                fill_prob: fill_prob.clamp(0.0, 1.0),
                adverse_selection_pts,
                chase_pts,
            },
            ..Self::default()
        }
    }

    /// Default DNSE config with the E1-calibrated queue fill (see [`e1`]).
    ///
    /// Adverse offset = `0.9 * (0.045 - 0.12) + 0.1 * 0.12` = -0.0555 pt per fill (favorable).
    #[must_use]
    pub fn passive_queue_e1() -> Self {
        Self::passive_queue(
            e1::HALF_SPREAD_PTS,
            e1::FILL_PROB,
            e1::ADVERSE_SELECTION_PTS,
            e1::CHASE_PTS,
        )
    }

    #[must_use]
    pub fn slippage_points(&self) -> f64 {
        self.slippage_ticks * self.tick_size
    }

    /// Signed price offset per fill in points for the active [`FillModel`].
    ///
    /// Positive is adverse, negative favorable (passive fills earn the spread).
    #[must_use]
    pub fn adverse_points(&self) -> f64 {
        match self.fill_model {
            FillModel::Marketable => self.slippage_points(),
            FillModel::WorseSide { half_spread_pts } => half_spread_pts,
            FillModel::Passive { half_spread_pts } => -half_spread_pts,
            FillModel::PassiveQueue {
                half_spread_pts,
                fill_prob,
                adverse_selection_pts,
                chase_pts,
            } => {
                let p = fill_prob.clamp(0.0, 1.0);
                p * (adverse_selection_pts - half_spread_pts) + (1.0 - p) * chase_pts
            }
        }
    }

    /// Fee in VND for one side: transaction fee plus derivatives PIT.
    ///
    /// PIT = `income_tax_rate × price × multiplier × qty × initial_margin_ratio / 2`.
    #[must_use]
    pub fn side_fee(&self, price: f64, qty: u32) -> f64 {
        let notional = price * self.multiplier * qty as f64;
        let pit = self.income_tax_rate * notional * (self.initial_margin_ratio / 2.0);
        self.txn_fee_vnd_per_side * qty as f64 + pit
    }

    /// Position-management fee in VND for holding `qty` contracts for one ICT day.
    #[must_use]
    pub fn position_fee(&self, qty: u32) -> f64 {
        self.position_fee_vnd_per_day * qty as f64
    }

    /// Fill price: `mid` plus [`Self::adverse_points`] for a buy, minus it for a sell.
    #[must_use]
    pub fn fill_price(&self, mid: f64, dir: Dir, action: Action) -> f64 {
        let buying = matches!(
            (dir, action),
            (Dir::Long, Action::Enter) | (Dir::Short, Action::Exit)
        );
        let adverse = self.adverse_points();
        if buying {
            mid + adverse
        } else {
            mid - adverse
        }
    }
}

/// Normalize a timestamp that may be in milliseconds down to seconds.
fn norm_secs(ts: i64) -> i64 {
    if ts > 1_000_000_000_000 {
        ts / 1000
    } else {
        ts
    }
}

/// Seconds since ICT midnight for a unix timestamp in seconds or milliseconds.
fn ict_seconds_of_day(ts: i64) -> i64 {
    (norm_secs(ts) + 7 * 3600).rem_euclid(86_400)
}

/// ICT calendar-day index of a unix timestamp (for multi-day hold fees).
fn ict_day(ts: i64) -> i64 {
    (norm_secs(ts) + 7 * 3600).div_euclid(86_400)
}

/// True if `ts` (unix seconds) is in a VN30F1M continuous session: 09:00–11:30 or 13:00–14:45 ICT.
#[must_use]
pub fn in_session(ts: i64) -> bool {
    let sod = ict_seconds_of_day(ts);
    // 09:00=32400, 11:30=41400 | 13:00=46800, 14:45=53100
    (32_400..41_400).contains(&sod) || (46_800..53_100).contains(&sod)
}

/// Stateless fill engine over a [`FillConfig`].
#[derive(Clone, Copy, Debug, Default)]
pub struct FillSim {
    pub cfg: FillConfig,
}

impl FillSim {
    #[must_use]
    pub fn new(cfg: FillConfig) -> Self {
        Self { cfg }
    }

    /// Execute `decision` at `mid` and `ts`, updating `pf`; returns the fill and trade record.
    ///
    /// Returns `None` for a hold, an entry while a position is open, a zero-size or
    /// out-of-session entry, and an exit while flat.
    #[must_use]
    pub fn execute(
        &self,
        pf: &mut Portfolio,
        decision: &BtDecision,
        mid: f64,
        ts: i64,
    ) -> Option<(Fill, TradeRecord)> {
        match decision {
            BtDecision::Hold => None,
            BtDecision::Enter {
                direction,
                qty,
                reason,
            } => {
                if pf.position.is_some() || *qty == 0 || !in_session(ts) {
                    return None; // no pyramiding; no zero-size; gate entries to session
                }
                let price = self.cfg.fill_price(mid, *direction, Action::Enter);
                let entry_fee_total = self.cfg.side_fee(price, *qty) + self.cfg.position_fee(*qty);
                pf.realized -= entry_fee_total;
                pf.position = Some(Position {
                    dir: *direction,
                    qty: *qty,
                    entry_price: price,
                    entry_fee_total,
                    entry_ts: ts,
                });
                let fill = Fill {
                    ts,
                    dir: *direction,
                    action: Action::Enter,
                    qty: *qty,
                    price,
                    fee_vnd: entry_fee_total,
                };
                let rec = TradeRecord {
                    ts,
                    side: dir_str(*direction),
                    action: "enter",
                    qty: *qty,
                    price,
                    fee_vnd: entry_fee_total,
                    pnl_points: None,
                    pnl_vnd: None,
                    reason: reason.clone(),
                };
                Some((fill, rec))
            }
            BtDecision::Exit { reason } => {
                let pos = pf.position.take()?;
                let price = self.cfg.fill_price(mid, pos.dir, Action::Exit);
                let sign = match pos.dir {
                    Dir::Long => 1.0,
                    Dir::Short => -1.0,
                };
                let pnl_points = (price - pos.entry_price) * sign;
                let gross = pnl_points * self.cfg.multiplier * pos.qty as f64;
                // day 1 of the position fee was paid at entry; charge each extra ICT day here
                let extra_days = (ict_day(ts) - ict_day(pos.entry_ts)).max(0) as f64;
                let exit_fee =
                    self.cfg.side_fee(price, pos.qty) + self.cfg.position_fee(pos.qty) * extra_days;
                pf.realized += gross - exit_fee;
                let net_vnd = gross - exit_fee - pos.entry_fee_total;
                let fill = Fill {
                    ts,
                    dir: pos.dir,
                    action: Action::Exit,
                    qty: pos.qty,
                    price,
                    fee_vnd: exit_fee,
                };
                let rec = TradeRecord {
                    ts,
                    side: dir_str(pos.dir),
                    action: "exit",
                    qty: pos.qty,
                    price,
                    fee_vnd: exit_fee,
                    pnl_points: Some(pnl_points),
                    pnl_vnd: Some(net_vnd),
                    reason: reason.clone(),
                };
                Some((fill, rec))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> FillConfig {
        FillConfig::default()
    }

    #[test]
    fn slippage_is_one_tick_default() {
        assert!((cfg().slippage_points() - 0.1).abs() < 1e-9);
    }

    #[test]
    fn default_fill_model_is_marketable_byte_identical() {
        let c = FillConfig::default();
        assert_eq!(c.fill_model, FillModel::Marketable);
        assert!((c.adverse_points() - c.slippage_points()).abs() < 1e-9);
    }

    #[test]
    fn worse_side_uses_half_spread_not_tick() {
        let c = FillConfig::worse_side(0.5);
        assert!((c.adverse_points() - 0.5).abs() < 1e-9);
        // buys pay up 0.5, sells give up 0.5
        assert!((c.fill_price(1250.0, Dir::Long, Action::Enter) - 1250.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Long, Action::Exit) - 1249.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Enter) - 1249.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Exit) - 1250.5).abs() < 1e-9);
    }

    #[test]
    fn passive_earns_half_spread_mirror_of_worse_side() {
        let c = FillConfig::passive(0.5);
        assert!((c.adverse_points() + 0.5).abs() < 1e-9, "favorable = -0.5");
        // maker buys below mid and sells above it, the mirror of worse_side
        assert!((c.fill_price(1250.0, Dir::Long, Action::Enter) - 1249.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Long, Action::Exit) - 1250.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Enter) - 1250.5).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Exit) - 1249.5).abs() < 1e-9);
    }

    #[test]
    fn passive_upper_bounds_worse_side_gross() {
        // long round trip, mid 1250 -> 1253: the maker earns 0.5 per leg, the taker pays it
        let (mid_in, mid_out) = (1250.0, 1253.0);
        let taker = FillConfig::worse_side(0.5);
        let maker = FillConfig::passive(0.5);
        let g = |c: &FillConfig| {
            c.fill_price(mid_out, Dir::Long, Action::Exit)
                - c.fill_price(mid_in, Dir::Long, Action::Enter)
        };
        let (taker_gross, maker_gross) = (g(&taker), g(&maker));
        assert!((taker_gross - 2.0).abs() < 1e-9, "taker {taker_gross}"); // 1252.5-1250.5
        assert!((maker_gross - 4.0).abs() < 1e-9, "maker {maker_gross}"); // 1253.5-1249.5
        assert!(maker_gross > taker_gross);
    }

    #[test]
    fn passive_queue_e1_matches_the_measured_calibration() {
        let c = FillConfig::passive_queue_e1();
        // 0.9 * (0.045 - 0.12) + 0.1 * 0.12 = -0.0675 + 0.012 = -0.0555.
        let a = c.adverse_points();
        assert!((a + 0.0555).abs() < 1e-12, "got {a}");
        // favorable, but less so than the E1 headline composite (-0.077 pt)
        assert!(a < 0.0 && a > -0.077, "got {a}");
    }

    #[test]
    fn passive_queue_sits_inside_the_maker_taker_bracket() {
        let optimistic = FillConfig::passive(0.12).adverse_points(); // -0.12
        let calibrated = FillConfig::passive_queue_e1().adverse_points(); // -0.0555
        let taker = FillConfig::default().adverse_points(); // +0.1
        let floor = FillConfig::worse_side(0.5).adverse_points(); // +0.5
        assert!(optimistic < calibrated, "{optimistic} !< {calibrated}");
        assert!(calibrated < taker, "{calibrated} !< {taker}");
        assert!(taker < floor);
        // The per-fill edge a maker design may claim over the taker default.
        assert!((taker - calibrated - 0.1555).abs() < 1e-12);
    }

    #[test]
    fn passive_queue_collapses_to_its_two_endpoints() {
        // p = 1 with no adverse selection: the Passive bound
        let always = FillConfig::passive_queue(0.5, 1.0, 0.0, 0.5).adverse_points();
        assert!((always - FillConfig::passive(0.5).adverse_points()).abs() < 1e-12);
        // p = 0 always chases: the WorseSide floor
        let never = FillConfig::passive_queue(0.5, 0.0, 0.0, 0.5).adverse_points();
        assert!((never - FillConfig::worse_side(0.5).adverse_points()).abs() < 1e-12);
    }

    #[test]
    fn passive_queue_clamps_an_out_of_range_fill_probability() {
        let hi = FillConfig::passive_queue(0.5, 1.7, 0.0, 0.5).adverse_points();
        assert!((hi - FillConfig::passive(0.5).adverse_points()).abs() < 1e-12);
        let lo = FillConfig::passive_queue(0.5, -0.4, 0.0, 0.5).adverse_points();
        assert!((lo - FillConfig::worse_side(0.5).adverse_points()).abs() < 1e-12);
    }

    #[test]
    fn passive_queue_buys_below_and_sells_above_mid() {
        let c = FillConfig::passive_queue_e1();
        assert!((c.fill_price(1250.0, Dir::Long, Action::Enter) - 1249.9445).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Long, Action::Exit) - 1250.0555).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Enter) - 1250.0555).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Exit) - 1249.9445).abs() < 1e-9);
    }

    #[test]
    fn side_fee_matches_dnse_schedule() {
        // 1 lot @ 1250.0: txn 3700 + PIT 0.001*(1250*100000)*(0.17/2)
        //              = 3700 + 0.001*125_000_000*0.085 = 3700 + 10_625 = 14_325.
        let f = cfg().side_fee(1250.0, 1);
        assert!((f - 14_325.0).abs() < 1e-6, "got {f}");
    }

    #[test]
    fn buy_pays_up_sell_gives_up() {
        let c = cfg();
        assert!((c.fill_price(1250.0, Dir::Long, Action::Enter) - 1250.1).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Long, Action::Exit) - 1249.9).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Enter) - 1249.9).abs() < 1e-9);
        assert!((c.fill_price(1250.0, Dir::Short, Action::Exit) - 1250.1).abs() < 1e-9);
    }

    #[test]
    fn session_gate_ict() {
        // 10:00 ICT = 03:00 UTC → unix 10800 in-session; 12:00 ICT (lunch) out.
        assert!(in_session(10_800)); // 10:00 ICT
        assert!(!in_session(18_000)); // 12:00 ICT (lunch)
        assert!(!in_session(0)); // 07:00 ICT pre-open
        assert!(in_session(23_400)); // 13:30 ICT (unix 23400 → sod 48600)
    }

    #[test]
    fn entry_blocked_outside_session() {
        let sim = FillSim::default();
        let mut pf = Portfolio::new(cfg().initial_cash);
        let d = BtDecision::Enter {
            direction: Dir::Long,
            qty: 1,
            reason: "t".into(),
        };
        assert!(sim.execute(&mut pf, &d, 1250.0, 18_000).is_none()); // lunch
        assert!(pf.position.is_none());
    }

    #[test]
    fn no_pyramiding() {
        let sim = FillSim::default();
        let mut pf = Portfolio::new(cfg().initial_cash);
        let d = BtDecision::Enter {
            direction: Dir::Long,
            qty: 1,
            reason: "t".into(),
        };
        assert!(sim.execute(&mut pf, &d, 1250.0, 10_800).is_some());
        assert!(sim.execute(&mut pf, &d, 1251.0, 10_806).is_none()); // already open
    }

    /// A hold spanning three ICT days pays two extra position fees at exit (day 1 at entry).
    #[test]
    fn multi_day_hold_charges_position_fee_per_extra_day() {
        let sim = FillSim::default();
        let mut pf = Portfolio::new(cfg().initial_cash);
        let enter = BtDecision::Enter {
            direction: Dir::Long,
            qty: 1,
            reason: "e".into(),
        };
        let exit = BtDecision::Exit { reason: "x".into() };
        // Enter at 10:00 ICT day 0, exit at 10:00 ICT day 2 → 2 extra days.
        sim.execute(&mut pf, &enter, 1250.0, 10_800).unwrap();
        let (_, rec) = sim
            .execute(&mut pf, &exit, 1250.0, 10_800 + 2 * 86_400)
            .unwrap();
        // Exit fee = side_fee(1249.9) + 2 * 2550 extra position fee.
        let expect = cfg().side_fee(1249.9, 1) + 2.0 * 2_550.0;
        assert!((rec.fee_vnd - expect).abs() < 1e-6, "got {}", rec.fee_vnd);
    }

    #[test]
    fn round_trip_pnl_net_of_all_fees() {
        let sim = FillSim::default();
        let mut pf = Portfolio::new(cfg().initial_cash);
        let enter = BtDecision::Enter {
            direction: Dir::Long,
            qty: 1,
            reason: "e".into(),
        };
        let exit = BtDecision::Exit { reason: "x".into() };
        // gross = (1252.9 - 1250.1) × 100,000 = 280,000 VND; PIT per side = 8.5 × price
        // entry fee = 3700 + 10625.85 + 2550 = 16875.85; exit fee = 3700 + 10649.65 = 14349.65
        // net = 280000 - 16875.85 - 14349.65 = 248774.5
        sim.execute(&mut pf, &enter, 1250.0, 10_800).unwrap(); // fill 1250.1
        let (_, rec) = sim.execute(&mut pf, &exit, 1253.0, 10_806).unwrap(); // fill 1252.9
        assert!((rec.pnl_points.unwrap() - 2.8).abs() < 1e-9);
        assert!(
            (rec.pnl_vnd.unwrap() - 248_774.5).abs() < 1e-3,
            "got {:?}",
            rec.pnl_vnd
        );
        // realized = -(entry) + gross - exit = -16875.85 + 280000 - 14349.65 = 248774.5.
        assert!((pf.realized - 248_774.5).abs() < 1e-3);
    }
}
