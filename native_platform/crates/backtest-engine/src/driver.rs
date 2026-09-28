//! Deterministic replay drivers: stable time order, single-threaded, bucket-last equity sampling.
//!
//! Look-ahead-free: a decision made on one tick or bar executes on the next tick (or the next
//! bar's open), never on the event that produced it.

use market_types::{BookSnapshot, Tick};

use crate::emit::{EquityPoint, TradeRecord};
use crate::fill::FillSim;
use crate::portfolio::Portfolio;
use crate::strategy::{Bar, BtDecision, MarketCtx, Strategy};

/// Outcome of a backtest run.
#[derive(Clone, Debug)]
pub struct BacktestResult {
    pub trades: Vec<TradeRecord>,
    pub equity: Vec<EquityPoint>,
    pub final_equity: f64,
}

/// Equity downsampler that keeps the last point of each `sample`-second bucket.
struct EquityDownsampler {
    sample: i64,
    out: Vec<EquityPoint>,
    pending: Option<EquityPoint>,
    bucket: Option<i64>,
}

impl EquityDownsampler {
    fn new(sample: i64) -> Self {
        Self {
            sample: sample.max(1),
            out: Vec::new(),
            pending: None,
            bucket: None,
        }
    }

    fn push(&mut self, p: EquityPoint) {
        let b = p.ts.div_euclid(self.sample);
        match self.bucket {
            Some(x) if x == b => self.pending = Some(p),
            Some(_) => {
                if let Some(prev) = self.pending.take() {
                    self.out.push(prev);
                }
                self.pending = Some(p);
                self.bucket = Some(b);
            }
            None => {
                self.pending = Some(p);
                self.bucket = Some(b);
            }
        }
    }

    fn finish(mut self) -> Vec<EquityPoint> {
        if let Some(p) = self.pending.take() {
            self.out.push(p);
        }
        self.out
    }
}

fn finalize(trades: Vec<TradeRecord>, ds: EquityDownsampler, initial: f64) -> BacktestResult {
    let equity = ds.finish();
    let final_equity = equity.last().map_or(initial, |e| e.equity);
    BacktestResult {
        trades,
        equity,
        final_equity,
    }
}

/// Ticks stable-sorted by timestamp, keeping the input order within a second.
fn ordered(ticks: &[Tick]) -> Vec<&Tick> {
    let mut v: Vec<&Tick> = ticks.iter().collect();
    v.sort_by_key(|t| t.timestamp.0);
    v
}

/// Tick-driven replay: one trade record per fill, equity sampled every `equity_sample_secs`.
#[must_use]
pub fn run_ticks<S: Strategy>(
    strat: &mut S,
    ticks: &[Tick],
    sim: &FillSim,
    equity_sample_secs: i64,
) -> BacktestResult {
    let mut pf = Portfolio::new(sim.cfg.initial_cash);
    let mut trades = Vec::new();
    let mut ds = EquityDownsampler::new(equity_sample_secs);
    let mut pending: Option<BtDecision> = None;

    for t in ordered(ticks) {
        let ts = t.timestamp.0;
        let mid = t.price.as_f64();
        // 1. Execute the previous tick's decision at this tick.
        if let Some(d) = pending.take() {
            if let Some((fill, rec)) = sim.execute(&mut pf, &d, mid, ts) {
                strat.on_fill(&fill);
                trades.push(rec);
            }
        }
        // 2. Strategy observes this tick, emits a decision for the next tick.
        let ctx = MarketCtx {
            price: mid,
            volume: t.volume.0 as f64,
            side: t.side,
            timestamp: ts,
            position: pf.position_view(),
        };
        pending = Some(strat.on_tick(&ctx));
        // 3. Mark equity.
        ds.push(pf.equity_point(mid, ts, sim.cfg.multiplier));
    }
    finalize(trades, ds, sim.cfg.initial_cash)
}

/// One merged event in the book-aware lane, ordered by unix milliseconds.
enum Ev<'a> {
    Tick(&'a Tick),
    Book(&'a BookSnapshot),
}

/// L2 replay: signals from book snapshots (`on_book`), fills at the next trade tick's price.
///
/// Ticks and snapshots merge into one stable millisecond-ordered stream; a `Hold` keeps any
/// pending decision until a tick fills it. Equity is marked on trade ticks only.
#[must_use]
pub fn run_ticks_with_book<S: Strategy>(
    strat: &mut S,
    ticks: &[Tick],
    books: &[BookSnapshot],
    sim: &FillSim,
    equity_sample_secs: i64,
) -> BacktestResult {
    // ticks carry whole seconds and books milliseconds; `sort_by_key` is stable
    let mut evs: Vec<Ev> = Vec::with_capacity(ticks.len() + books.len());
    evs.extend(ticks.iter().map(Ev::Tick));
    evs.extend(books.iter().map(Ev::Book));
    evs.sort_by_key(|e| match e {
        Ev::Tick(t) => t.timestamp.0 * 1000,
        Ev::Book(b) => b.ts_ms,
    });

    let mut pf = Portfolio::new(sim.cfg.initial_cash);
    let mut trades = Vec::new();
    let mut ds = EquityDownsampler::new(equity_sample_secs);
    let mut pending: Option<BtDecision> = None;

    for ev in evs {
        match ev {
            Ev::Book(b) => {
                let ctx = MarketCtx {
                    price: b.mid().unwrap_or(0.0),
                    volume: 0.0,
                    side: None,
                    timestamp: b.ts_secs(),
                    position: pf.position_view(),
                };
                let d = strat.on_book(b, &ctx);
                if !matches!(d, BtDecision::Hold) {
                    pending = Some(d); // book intent fills on the next tick
                }
            }
            Ev::Tick(t) => {
                let ts = t.timestamp.0;
                let mid = t.price.as_f64();
                if let Some(d) = pending.take() {
                    if let Some((fill, rec)) = sim.execute(&mut pf, &d, mid, ts) {
                        strat.on_fill(&fill);
                        trades.push(rec);
                    }
                }
                let ctx = MarketCtx {
                    price: mid,
                    volume: t.volume.0 as f64,
                    side: t.side,
                    timestamp: ts,
                    position: pf.position_view(),
                };
                let d = strat.on_tick(&ctx);
                if !matches!(d, BtDecision::Hold) {
                    pending = Some(d);
                }
                ds.push(pf.equity_point(mid, ts, sim.cfg.multiplier));
            }
        }
    }
    finalize(trades, ds, sim.cfg.initial_cash)
}

/// Epoch-floored OHLCV bar accumulator.
struct BarBuilder {
    index: i64,
    ts_open: i64,
    ts_close: i64,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
}

impl BarBuilder {
    fn new(index: i64, ts: i64, price: f64, volume: f64) -> Self {
        Self {
            index,
            ts_open: ts,
            ts_close: ts,
            open: price,
            high: price,
            low: price,
            close: price,
            volume,
        }
    }

    fn push(&mut self, ts: i64, price: f64, volume: f64) {
        self.high = self.high.max(price);
        self.low = self.low.min(price);
        self.close = price;
        self.ts_close = ts;
        self.volume += volume;
    }

    fn finish(&self) -> Bar {
        Bar {
            ts_open: self.ts_open,
            ts_close: self.ts_close,
            open: self.open,
            high: self.high,
            low: self.low,
            close: self.close,
            volume: self.volume,
        }
    }
}

/// Bar-signal replay on a tick tape: ticks form `bar_seconds` bars, fills are at tick prices.
///
/// A completed bar's decision fills at the first tick of the next bar, never at a bar price.
/// The final incomplete bar is not emitted. For bars without ticks see [`run_prebuilt_bars`].
#[must_use]
pub fn run_bars<S: Strategy>(
    strat: &mut S,
    ticks: &[Tick],
    sim: &FillSim,
    bar_seconds: i64,
    equity_sample_secs: i64,
) -> BacktestResult {
    let bar_seconds = bar_seconds.max(1);
    let mut pf = Portfolio::new(sim.cfg.initial_cash);
    let mut trades = Vec::new();
    let mut ds = EquityDownsampler::new(equity_sample_secs);
    let mut builder: Option<BarBuilder> = None;

    for t in ordered(ticks) {
        let ts = t.timestamp.0;
        let mid = t.price.as_f64();
        let vol = t.volume.0 as f64;
        let idx = ts.div_euclid(bar_seconds);

        // Bar boundary crossed → complete the prior bar and act on it now.
        if let Some(b) = &builder {
            if b.index != idx {
                let bar = b.finish();
                let ctx = MarketCtx {
                    price: mid,
                    volume: vol,
                    side: t.side,
                    timestamp: ts,
                    position: pf.position_view(),
                };
                let decision = strat.on_bar(&bar, &ctx);
                if let Some((fill, rec)) = sim.execute(&mut pf, &decision, mid, ts) {
                    strat.on_fill(&fill);
                    trades.push(rec);
                }
                builder = None;
            }
        }
        match &mut builder {
            Some(b) => b.push(ts, mid, vol),
            None => builder = Some(BarBuilder::new(idx, ts, mid, vol)),
        }
        ds.push(pf.equity_point(mid, ts, sim.cfg.multiplier));
    }
    finalize(trades, ds, sim.cfg.initial_cash)
}

/// Signal-research replay of pre-built bars: a decision on bar `N` fills at bar `N+1`'s open.
///
/// Bar-open fills approximate execution, so use [`run_bars`] for realised P&L. Bars must be
/// sorted by `ts_open`; equity is marked at bar close and the last bar's decision is dropped.
#[must_use]
pub fn run_prebuilt_bars<S: Strategy>(
    strat: &mut S,
    bars: &[Bar],
    sim: &FillSim,
    equity_sample_secs: i64,
) -> BacktestResult {
    let mut pf = Portfolio::new(sim.cfg.initial_cash);
    let mut trades = Vec::new();
    let mut ds = EquityDownsampler::new(equity_sample_secs);
    let mut pending: Option<BtDecision> = None;

    for bar in bars {
        // 1. Execute the previous bar's decision at this bar's open.
        if let Some(d) = pending.take() {
            if let Some((fill, rec)) = sim.execute(&mut pf, &d, bar.open, bar.ts_open) {
                strat.on_fill(&fill);
                trades.push(rec);
            }
        }
        // 2. Strategy observes the completed bar, emits a decision for the next bar.
        let ctx = MarketCtx {
            price: bar.close,
            volume: bar.volume,
            side: None,
            timestamp: bar.ts_close,
            position: pf.position_view(),
        };
        pending = Some(strat.on_bar(bar, &ctx));
        // 3. Mark equity at bar close.
        ds.push(pf.equity_point(bar.close, bar.ts_close, sim.cfg.multiplier));
    }
    finalize(trades, ds, sim.cfg.initial_cash)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strategy::Dir;
    use market_types::{EventTimestamp, Price, Symbol, TickSide, Volume};

    fn tk(ts: i64, price: f64, vol: i64) -> Tick {
        Tick {
            symbol: Symbol::new("VN30F1M").unwrap(),
            timestamp: EventTimestamp(ts),
            price: Price::from_f64(price).unwrap(),
            volume: Volume::new(vol).unwrap(),
            side: Some(TickSide::Buy),
        }
    }

    /// Enters long on the first observed tick, holds forever.
    struct EnterOnce {
        armed: bool,
    }
    impl Strategy for EnterOnce {
        fn on_tick(&mut self, _ctx: &MarketCtx) -> BtDecision {
            if self.armed {
                self.armed = false;
                BtDecision::Enter {
                    direction: Dir::Long,
                    qty: 1,
                    reason: "test".into(),
                }
            } else {
                BtDecision::Hold
            }
        }
        fn name(&self) -> &'static str {
            "enter_once"
        }
    }

    // 10:00 ICT baseline (unix 10800) so ticks are in-session.
    const BASE: i64 = 10_800;

    #[test]
    fn fill_lands_on_next_tick_not_signal_tick() {
        let ticks = vec![
            tk(BASE, 1250.0, 1),
            tk(BASE + 6, 1251.0, 1),
            tk(BASE + 12, 1252.0, 1),
        ];
        let sim = FillSim::default();
        let r = run_ticks(&mut EnterOnce { armed: true }, &ticks, &sim, 1);
        assert_eq!(r.trades.len(), 1);
        // Decision emitted on tick[0] (ts BASE) must fill on tick[1] (BASE+6).
        assert_eq!(r.trades[0].ts, BASE + 6);
        assert_eq!(r.trades[0].action, "enter");
    }

    #[test]
    fn pending_entry_dropped_when_fill_tick_leaves_session() {
        // the fill tick falls in the lunch break, so the entry is dropped (exits are never gated)
        let ticks = vec![
            tk(BASE, 1250.0, 1),   // 10:00 ICT, in-session → Enter decided here
            tk(18_000, 1251.0, 1), // 12:00 ICT lunch → pending Enter blocked
        ];
        let sim = FillSim::default();
        let r = run_ticks(&mut EnterOnce { armed: true }, &ticks, &sim, 1);
        assert!(r.trades.is_empty());
        assert!(r.equity.iter().all(|e| e.position == 0));
    }

    #[test]
    fn deterministic_same_input_same_output() {
        let ticks = vec![
            tk(BASE, 1250.0, 1),
            tk(BASE + 6, 1251.0, 2),
            tk(BASE + 12, 1249.0, 1),
        ];
        let sim = FillSim::default();
        let a = run_ticks(&mut EnterOnce { armed: true }, &ticks, &sim, 5);
        let b = run_ticks(&mut EnterOnce { armed: true }, &ticks, &sim, 5);
        assert_eq!(a.trades, b.trades);
        assert_eq!(a.equity, b.equity);
    }

    #[test]
    fn equity_downsample_keeps_last_per_bucket() {
        // Three ticks in one 5s bucket → a single equity sample (the last).
        let ticks = vec![
            tk(BASE, 1250.0, 1),
            tk(BASE + 1, 1250.0, 1),
            tk(BASE + 2, 1250.0, 1),
        ];
        let sim = FillSim::default();
        let r = run_ticks(&mut EnterOnce { armed: false }, &ticks, &sim, 5);
        assert_eq!(r.equity.len(), 1);
        assert_eq!(r.equity[0].ts, BASE + 2);
    }

    /// Enters long the first time a bar completes.
    struct EnterOnFirstBar {
        armed: bool,
    }
    impl Strategy for EnterOnFirstBar {
        fn on_bar(&mut self, _bar: &Bar, _ctx: &MarketCtx) -> BtDecision {
            if self.armed {
                self.armed = false;
                BtDecision::Enter {
                    direction: Dir::Long,
                    qty: 1,
                    reason: "bar".into(),
                }
            } else {
                BtDecision::Hold
            }
        }
        fn name(&self) -> &'static str {
            "enter_on_first_bar"
        }
    }

    #[test]
    fn bar_aggregation_and_boundary_fill() {
        // 60 s bars: bar 0 spans `BASE..BASE + 60`; the tick at `BASE + 60` completes it
        let ticks = vec![
            tk(BASE, 1250.0, 1),
            tk(BASE + 30, 1252.0, 1),
            tk(BASE + 60, 1253.0, 1), // completes bar 0, fills here
            tk(BASE + 120, 1254.0, 1),
        ];
        let sim = FillSim::default();
        let r = run_bars(&mut EnterOnFirstBar { armed: true }, &ticks, &sim, 60, 5);
        assert_eq!(r.trades.len(), 1);
        // First completed bar's decision fills on the boundary tick (BASE+60).
        assert_eq!(r.trades[0].ts, BASE + 60);
    }

    fn mk_bar(ts_open: i64, o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar {
            ts_open,
            ts_close: ts_open + 1799,
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 100.0,
        }
    }

    #[test]
    fn prebuilt_bars_fill_on_next_bar_open() {
        // bars at 10:00, 10:30 and 11:00 ICT; the bar[0] decision fills at bar[1]'s open
        let bars = vec![
            mk_bar(BASE, 1250.0, 1252.0, 1249.0, 1251.0),
            mk_bar(BASE + 1800, 1251.5, 1255.0, 1251.0, 1254.0),
            mk_bar(BASE + 3600, 1254.0, 1256.0, 1253.0, 1255.0),
        ];
        let sim = FillSim::default();
        let r = run_prebuilt_bars(&mut EnterOnFirstBar { armed: true }, &bars, &sim, 1);
        assert_eq!(r.trades.len(), 1);
        assert_eq!(r.trades[0].action, "enter");
        // bar[1] open time and price (1251.5) plus slippage
        assert_eq!(r.trades[0].ts, BASE + 1800);
        assert!((r.trades[0].price - (1251.5 + sim.cfg.slippage_points())).abs() < 1e-9);
    }

    #[test]
    fn prebuilt_bars_deterministic() {
        let bars = vec![
            mk_bar(BASE, 1250.0, 1252.0, 1249.0, 1251.0),
            mk_bar(BASE + 1800, 1251.5, 1255.0, 1251.0, 1254.0),
        ];
        let sim = FillSim::default();
        let a = run_prebuilt_bars(&mut EnterOnFirstBar { armed: true }, &bars, &sim, 5);
        let b = run_prebuilt_bars(&mut EnterOnFirstBar { armed: true }, &bars, &sim, 5);
        assert_eq!(a.trades, b.trades);
        assert_eq!(a.equity, b.equity);
    }

    fn bk(ts_ms: i64, pb: f64, qb: f64, pa: f64, qa: f64) -> BookSnapshot {
        BookSnapshot {
            ts_ms,
            bid_px: vec![pb],
            bid_sz: vec![qb],
            ask_px: vec![pa],
            ask_sz: vec![qa],
        }
    }

    /// Enters long on the first book snapshot (book-driven), holds after.
    struct EnterOnBook {
        armed: bool,
    }
    impl Strategy for EnterOnBook {
        fn on_book(&mut self, _b: &BookSnapshot, _ctx: &MarketCtx) -> BtDecision {
            if self.armed {
                self.armed = false;
                BtDecision::Enter {
                    direction: Dir::Long,
                    qty: 1,
                    reason: "book".into(),
                }
            } else {
                BtDecision::Hold
            }
        }
        fn name(&self) -> &'static str {
            "enter_on_book"
        }
    }

    #[test]
    fn book_intent_fills_on_next_tick() {
        // a snapshot 0.1 s after the first tick emits Enter, which fills at the next tick
        let ticks = vec![tk(BASE, 1250.0, 1), tk(BASE + 6, 1251.0, 1)];
        let books = vec![bk(BASE * 1000 + 100, 1249.9, 5.0, 1250.1, 4.0)];
        let sim = FillSim::default();
        let r = run_ticks_with_book(&mut EnterOnBook { armed: true }, &ticks, &books, &sim, 1);
        assert_eq!(r.trades.len(), 1);
        assert_eq!(r.trades[0].action, "enter");
        assert_eq!(r.trades[0].ts, BASE + 6); // filled at the tick, not the book
    }

    #[test]
    fn book_lane_deterministic_and_no_book_no_trades() {
        let ticks = vec![tk(BASE, 1250.0, 1), tk(BASE + 6, 1251.0, 1)];
        let books = vec![bk(BASE * 1000 + 100, 1249.9, 5.0, 1250.1, 4.0)];
        let sim = FillSim::default();
        let a = run_ticks_with_book(&mut EnterOnBook { armed: true }, &ticks, &books, &sim, 5);
        let b = run_ticks_with_book(&mut EnterOnBook { armed: true }, &ticks, &books, &sim, 5);
        assert_eq!(a.trades, b.trades);
        assert_eq!(a.equity, b.equity);
        // With no book snapshots, a book-only strategy makes no trades.
        let none = run_ticks_with_book(&mut EnterOnBook { armed: true }, &ticks, &[], &sim, 5);
        assert!(none.trades.is_empty());
    }
}
