//! The [`Strategy`] trait and the value types exchanged with the engine.
//!
//! A strategy returns an intent ([`BtDecision`]) and never touches fills, fees or the portfolio,
//! so the same code runs in the backtester and live.

use market_types::{BookSnapshot, TickSide};
use std::borrow::Cow;

/// Label for a decision or fill: a static literal or a computed `String`.
pub type Reason = Cow<'static, str>;

/// Trade direction.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dir {
    Long,
    Short,
}

/// Which side of a round-trip a fill is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Enter,
    Exit,
}

/// A strategy's intent for the next qualifying tick.
///
/// A decision never both closes and opens: a reversal is `Exit`, then `Enter` on a later tick.
#[derive(Clone, Debug, PartialEq)]
pub enum BtDecision {
    /// Do nothing.
    Hold,
    /// Open a position; ignored if one is already open or the tick is outside a session.
    Enter {
        direction: Dir,
        qty: u32,
        reason: Reason,
    },
    /// Close the open position (ignored if flat).
    Exit { reason: Reason },
}

/// Read-only view of the open position.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PositionView {
    pub dir: Dir,
    pub qty: u32,
    pub entry_price: f64,
}

/// Read-only market context for one tick (or the boundary tick of a bar).
#[derive(Clone, Copy, Debug)]
pub struct MarketCtx {
    pub price: f64,
    pub volume: f64,
    pub side: Option<TickSide>,
    /// Unix seconds (event time).
    pub timestamp: i64,
    pub position: Option<PositionView>,
}

/// A completed OHLCV bar, for bar-driven strategies.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bar {
    pub ts_open: i64,
    pub ts_close: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

/// A confirmed fill passed back to the strategy, which updates state on fills, not on intent.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fill {
    pub ts: i64,
    pub dir: Dir,
    pub action: Action,
    pub qty: u32,
    pub price: f64,
    pub fee_vnd: f64,
}

/// Decision interface shared by tick-, bar- and book-driven strategies.
///
/// `Send` so a `Box<dyn Strategy>` can run inside a spawned multi-threaded task.
pub trait Strategy: Send {
    /// Tick-driven decision; holds by default.
    fn on_tick(&mut self, _ctx: &MarketCtx) -> BtDecision {
        BtDecision::Hold
    }

    /// Bar-driven decision, called when the driver completes a bar; holds by default.
    fn on_bar(&mut self, _bar: &Bar, _ctx: &MarketCtx) -> BtDecision {
        BtDecision::Hold
    }

    /// Order-book decision, called on each book snapshot; holds by default.
    ///
    /// The decision fills on the next trade tick, at a tick price.
    fn on_book(&mut self, _book: &BookSnapshot, _ctx: &MarketCtx) -> BtDecision {
        BtDecision::Hold
    }

    /// Confirmed-fill callback (optional).
    fn on_fill(&mut self, _fill: &Fill) {}

    /// Stable identifier used in output/labels.
    fn name(&self) -> &'static str;
}

/// Direction as written to JSONL and labels: `long` or `short`.
pub fn dir_str(dir: Dir) -> &'static str {
    match dir {
        Dir::Long => "long",
        Dir::Short => "short",
    }
}
