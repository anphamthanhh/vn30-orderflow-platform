//! Single-position portfolio with mark-to-market P&L.
//!
//! At most one open position (no pyramiding). Entry fees are realised at open; gross P&L and
//! exit fees at close.

use crate::emit::EquityPoint;
use crate::strategy::{Dir, PositionView};

/// An open position and the entry fees paid, so the exit record can report the round-trip net.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Position {
    pub dir: Dir,
    pub qty: u32,
    pub entry_price: f64,
    pub entry_fee_total: f64,
    /// Entry fill time (unix seconds); the exit charges a position fee per extra ICT day held.
    pub entry_ts: i64,
}

/// Account state during a backtest run. `equity = initial + realized + unrealized`.
#[derive(Clone, Debug)]
pub struct Portfolio {
    pub initial: f64,
    /// Closed P&L in VND, net of all fees paid so far.
    pub realized: f64,
    pub position: Option<Position>,
}

impl Portfolio {
    #[must_use]
    pub fn new(initial: f64) -> Self {
        Self {
            initial,
            realized: 0.0,
            position: None,
        }
    }

    fn dir_sign(dir: Dir) -> f64 {
        match dir {
            Dir::Long => 1.0,
            Dir::Short => -1.0,
        }
    }

    /// Open-position mark-to-market gain in VND (gross of exit fee).
    #[must_use]
    pub fn unrealized(&self, mark: f64, multiplier: f64) -> f64 {
        match &self.position {
            Some(p) => (mark - p.entry_price) * Self::dir_sign(p.dir) * multiplier * p.qty as f64,
            None => 0.0,
        }
    }

    #[must_use]
    pub fn equity(&self, mark: f64, multiplier: f64) -> f64 {
        self.initial + self.realized + self.unrealized(mark, multiplier)
    }

    /// Signed position size (+qty long, -qty short, 0 flat).
    #[must_use]
    pub fn signed_position(&self) -> i32 {
        match &self.position {
            Some(p) => {
                let q = p.qty as i32;
                match p.dir {
                    Dir::Long => q,
                    Dir::Short => -q,
                }
            }
            None => 0,
        }
    }

    #[must_use]
    pub fn position_view(&self) -> Option<PositionView> {
        self.position.as_ref().map(|p| PositionView {
            dir: p.dir,
            qty: p.qty,
            entry_price: p.entry_price,
        })
    }

    #[must_use]
    pub fn equity_point(&self, mark: f64, ts: i64, multiplier: f64) -> EquityPoint {
        EquityPoint {
            ts,
            equity: self.equity(mark, multiplier),
            position: self.signed_position(),
            unrealized: self.unrealized(mark, multiplier),
            realized: self.realized,
        }
    }
}
