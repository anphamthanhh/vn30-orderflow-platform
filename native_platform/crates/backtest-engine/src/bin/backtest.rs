//! Offline backtest CLI: replays a JSONL tick or bar tape through a strategy and `FillSim`.
//!
//! Tick line: `{"ts":10800,"price":1250.3,"volume":10,"side":"B"}` (`ts` in unix seconds).
//! Writes `trades.jsonl`, `equity.jsonl` and `run_manifest.json`; `--help` lists the options.

use std::path::PathBuf;
use std::process::ExitCode;

use backtest_engine::{
    run_bars, run_prebuilt_bars, run_ticks, run_ticks_with_book, write_jsonl, AtrBreakout,
    AtrBreakoutConfig, Atrx, AtrxConfig, Avcd, AvcdConfig, Bar, BookSnapshot, BtDecision,
    CvdConfig, CvdExhaustion, Dir, DualRegime, DualRegimeConfig, FillConfig, FillSim, IbFade,
    IbFadeConfig, MarketCtx, Mphf, MphfConfig, RawBookSnapshot, Sig001Strategy, Srlcv, SrlcvConfig,
    Strategy, SupertrendConfig, SupertrendM30, VaEdgeConfig, VaEdgeRotation, VwapRevCvd,
    VwapRevCvdConfig,
};
use market_types::{EventTimestamp, Price, Symbol, Tick, TickSide, Volume};
use serde::Deserialize;
use signal_engine::evaluator::SignalConfig;

const USAGE: &str = "\
backtest — offline VN30F1M backtest runner (two lanes)

EXECUTION INVARIANT: entries/exits fill 100% at TICK price. The SIGNAL cadence
may be tick or minute, but fills are always at the next tick (like live).

LANES:
  TICK lane (--ticks): SIGNAL = tick (cvd/sig001) or minute (supertrend via
                       --bar-seconds). FILL = tick price → EXECUTION-FAITHFUL.
                       This is the real backtest. Data: raw ticks.
  BAR  lane (--bars):  SIGNAL-RESEARCH ONLY. Pre-built OHLCV, no ticks → fills at
                       bar OPEN (APPROX, NOT tick-faithful). Use to scan signal/
                       param behaviour over long bar history; NOT for go/no-go
                       P&L. Re-run in the tick lane once tick data exists.

USAGE:
  backtest --ticks <file.jsonl> --strategy {cvd|sig001|supertrend|atr_breakout|srlcv|dual_regime|avcd|mphf|atrx|ib_fade|vwap_reversion_cvd|va_edge_rotation|ofi_demo|buyhold} [opts]
  backtest --bars  <file.jsonl> --strategy {supertrend|atr_breakout|srlcv|dual_regime|mphf|atrx|ib_fade|va_edge_rotation|buyhold}                                    [opts]

ARGS:
  --ticks <file>       TICK lane. JSONL: {\"ts\":<unix_s>,\"price\":f,\"volume\":i,\"side\":\"B|S\"}
  --bars  <file>       BAR lane.  JSONL: {\"ts\":<open_unix_s>,\"open\":f,\"high\":f,\"low\":f,\"close\":f,\"volume\":i}
                       (open/high/low default to close if absent; ts_close optional)
  --strategy <name>    strategy id (default: buyhold)
  --out-dir <dir>      output dir for trades.jsonl + equity.jsonl (default: .)
  --bar-seconds N      TICK lane only: aggregate ticks into N-second bars (bar strategies)
  --equity-sample N    equity downsample seconds (default: 5)
  --exec <model>       fill model (default: marketable):
                         marketable          taker, mid ± 1 tick (legacy default)
                         worseside[:pts]     worse-side half-spread, mid ± pts per fill
                                             (default 0.5pt — the frozen forward floor,
                                             historical/research_lab/PRE_REGISTRATION.md §fill)
                         passive[:pts]       maker EARNS the half-spread. OPTIMISTIC upper
                                             bound: no queue, no non-fill. Pair with
                                             worseside to bracket a maker design.
                         passivequeue[:p]    E1-calibrated maker: fills with probability p
                                             (default 0.90, E1 measured 90-96%), earns the
                                             half-spread less adverse selection, and PAYS
                                             the chase on the unfilled remainder.
                                             = -0.0555pt/fill. Expectancy only (the mean is
                                             modelled, the dispersion is not), and NOT a
                                             promotion bar — the live DNSE resting-limit
                                             probe (ADR 0023) is still unrun.
  --st-period N        Supertrend ATR period override (default: 10) — for WFA sweeps
  --st-mult F          Supertrend band multiplier override (default: 3.0) — for WFA sweeps
  --bias-file <file>   sig001 only: 80%-Rule bias sidecar JSONL
                       {\"day\":<ict_day>,\"dir\":\"long|short\",\"prior_vah\":f,\"prior_val\":f}
                       (no file → sig001 makes no entries: Tier-2 bias gate)
  --book <file>        ofi_demo (TICK lane): L2 book tape, JSONL, one snapshot per line
                       {\"ts\":<unix_s.mmm>,\"bid_px\":[..],\"bid_sz\":[..],\"ask_px\":[..],\"ask_sz\":[..]}
                       SIGNAL = order book (on_book); FILL = next trade tick.

Exactly one of --ticks / --bars is required.
";

#[derive(Deserialize)]
struct InputTick {
    ts: i64,
    price: f64,
    volume: i64,
    #[serde(default)]
    side: Option<String>,
}

fn parse_side(s: &Option<String>) -> Option<TickSide> {
    match s.as_deref() {
        Some("B") | Some("b") | Some("Buy") => Some(TickSide::Buy),
        Some("S") | Some("s") | Some("Sell") => Some(TickSide::Sell),
        _ => None,
    }
}

/// Bar-lane input line; `open`, `high` and `low` default to `close` when absent.
///
/// On close-only bars, ATR-based indicators degrade to close-to-close ranges.
#[derive(Deserialize)]
struct InputBar {
    /// Bar open time in unix seconds; fills reference the open.
    ts: i64,
    #[serde(default)]
    ts_close: Option<i64>,
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    close: f64,
    #[serde(default)]
    volume: f64,
}

fn load_bars(path: &str) -> Result<Vec<Bar>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    let mut bars = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let ib: InputBar =
            serde_json::from_str(line).map_err(|e| format!("line {}: {e}", i + 1))?;
        let c = ib.close;
        bars.push(Bar {
            ts_open: ib.ts,
            ts_close: ib.ts_close.unwrap_or(ib.ts),
            open: ib.open.unwrap_or(c),
            high: ib.high.unwrap_or(c),
            low: ib.low.unwrap_or(c),
            close: c,
            volume: ib.volume,
        });
    }
    // bars must be ordered by open time
    bars.sort_by_key(|b| b.ts_open);
    Ok(bars)
}

fn load_ticks(path: &str) -> Result<Vec<Tick>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    let mut ticks = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let it: InputTick =
            serde_json::from_str(line).map_err(|e| format!("line {}: {e}", i + 1))?;
        let tick = Tick {
            symbol: Symbol::new("VN30F1M").map_err(|e| format!("symbol: {e:?}"))?,
            timestamp: EventTimestamp::new(it.ts).map_err(|e| format!("line {}: {e:?}", i + 1))?,
            price: Price::from_f64(it.price).map_err(|e| format!("line {}: {e:?}", i + 1))?,
            volume: Volume::new(it.volume).map_err(|e| format!("line {}: {e:?}", i + 1))?,
            side: parse_side(&it.side),
        };
        ticks.push(tick);
    }
    Ok(ticks)
}

/// Baseline for both lanes: enter long on the first tick or bar and never exit.
struct BuyHold;
impl BuyHold {
    fn enter_if_flat(position: Option<backtest_engine::PositionView>) -> BtDecision {
        if position.is_none() {
            BtDecision::Enter {
                direction: Dir::Long,
                qty: 1,
                reason: "buyhold".into(),
            }
        } else {
            BtDecision::Hold
        }
    }
}
impl Strategy for BuyHold {
    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        Self::enter_if_flat(ctx.position)
    }
    fn on_bar(&mut self, _bar: &Bar, ctx: &MarketCtx) -> BtDecision {
        Self::enter_if_flat(ctx.position)
    }
    fn name(&self) -> &'static str {
        "buyhold"
    }
}

/// Which data lane the run uses.
enum Lane {
    /// Raw ticks → tick-native strategies.
    Ticks(String),
    /// Pre-built OHLCV bars → bar-native strategies.
    Bars(String),
}

struct Args {
    lane: Lane,
    strategy: String,
    out_dir: PathBuf,
    bar_seconds: Option<i64>,
    equity_sample: i64,
    st_period: Option<usize>,
    st_mult: Option<f64>,
    bias_file: Option<String>,
    book_file: Option<String>,
    exec: String,
}

/// One line of the SIG-001 80%-Rule bias sidecar (per ICT trading day).
#[derive(Deserialize)]
struct BiasRow {
    /// ICT day index: `(unix_open + 7h) / 86400`.
    day: i64,
    /// "long"/"bullish" or "short"/"bearish".
    dir: String,
    prior_vah: f64,
    prior_val: f64,
    /// Target (opposite value-area edge); defaults to the far edge if absent.
    #[serde(default)]
    target: Option<f64>,
    #[serde(default)]
    confidence: Option<f64>,
}

/// Load the per-day bias sidecar; with no path the map is empty and SIG-001 makes no entries.
fn load_bias(
    path: Option<&str>,
) -> Result<std::collections::HashMap<i64, signal_engine::evaluator::SessionBias>, String> {
    use signal_engine::evaluator::SessionBias;
    use signal_engine::registry::Direction;
    let mut map = std::collections::HashMap::new();
    let Some(path) = path else {
        return Ok(map);
    };
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let r: BiasRow =
            serde_json::from_str(line).map_err(|e| format!("bias line {}: {e}", i + 1))?;
        let direction = match r.dir.to_ascii_lowercase().as_str() {
            "long" | "bullish" | "l" | "b" => Direction::Bullish,
            "short" | "bearish" | "s" => Direction::Bearish,
            other => return Err(format!("bias line {}: bad dir {other}", i + 1)),
        };
        let target = r.target.unwrap_or(match direction {
            Direction::Bullish => r.prior_vah,
            _ => r.prior_val,
        });
        map.insert(
            r.day,
            SessionBias {
                direction,
                prior_vah: r.prior_vah,
                prior_val: r.prior_val,
                target,
                confidence: r.confidence.unwrap_or(1.0),
                computed_at: r.day * 86_400,
            },
        );
    }
    Ok(map)
}

/// Load an L2 book tape (JSONL, one snapshot per line) into time-ordered snapshots.
fn load_books(path: &str) -> Result<Vec<BookSnapshot>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    let mut v = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let r: RawBookSnapshot =
            serde_json::from_str(line).map_err(|e| format!("book line {}: {e}", i + 1))?;
        v.push(BookSnapshot::from(r));
    }
    v.sort_by_key(|b| b.ts_ms);
    Ok(v)
}

/// Pipeline demo of the L2 `on_book` path using best-quote OFI (Cont, Kukanov and Stoikov).
///
/// Sums OFI over `window_secs`, enters when |OFI| > `threshold`, exits after `hold_secs`. The
/// edge is below the round-trip fee (`docs/knowledge/05-order-flow-features-and-parity.md`).
struct OfiDemo {
    window_secs: i64,
    threshold: f64,
    hold_secs: i64,
    prev: Option<(f64, f64, f64, f64)>,
    accum: f64,
    window: Option<i64>,
    entry_ts: Option<i64>,
}

impl OfiDemo {
    fn new() -> Self {
        Self {
            window_secs: 60,
            threshold: 500.0,
            hold_secs: 300,
            prev: None,
            accum: 0.0,
            window: None,
            entry_ts: None,
        }
    }
}

impl Strategy for OfiDemo {
    fn on_book(&mut self, b: &BookSnapshot, ctx: &MarketCtx) -> BtDecision {
        let (Some((pb, qb)), Some((pa, qa))) = (b.best_bid(), b.best_ask()) else {
            return BtDecision::Hold;
        };
        let w = b.ts_secs().div_euclid(self.window_secs.max(1));
        if self.window != Some(w) {
            self.window = Some(w);
            self.accum = 0.0;
        }
        if let Some((pb0, qb0, pa0, qa0)) = self.prev {
            let e = (if pb >= pb0 { qb } else { 0.0 })
                - (if pb <= pb0 { qb0 } else { 0.0 })
                - (if pa <= pa0 { qa } else { 0.0 })
                + (if pa >= pa0 { qa0 } else { 0.0 });
            self.accum += e;
        }
        self.prev = Some((pb, qb, pa, qa));
        if ctx.position.is_some() {
            return BtDecision::Hold;
        }
        if self.accum > self.threshold {
            BtDecision::Enter {
                direction: Dir::Long,
                qty: 1,
                reason: "ofi_demo_long".into(),
            }
        } else if self.accum < -self.threshold {
            BtDecision::Enter {
                direction: Dir::Short,
                qty: 1,
                reason: "ofi_demo_short".into(),
            }
        } else {
            BtDecision::Hold
        }
    }

    fn on_tick(&mut self, ctx: &MarketCtx) -> BtDecision {
        match ctx.position {
            Some(_) => {
                let et = *self.entry_ts.get_or_insert(ctx.timestamp);
                if ctx.timestamp - et >= self.hold_secs {
                    BtDecision::Exit {
                        reason: "ofi_demo_time".into(),
                    }
                } else {
                    BtDecision::Hold
                }
            }
            None => {
                self.entry_ts = None;
                BtDecision::Hold
            }
        }
    }

    fn name(&self) -> &'static str {
        "ofi_demo"
    }
}

fn parse_args() -> Result<Args, String> {
    let mut ticks = None;
    let mut bars = None;
    let mut strategy = "buyhold".to_string();
    let mut out_dir = PathBuf::from(".");
    let mut bar_seconds = None;
    let mut equity_sample = 5;
    let mut st_period = None;
    let mut st_mult = None;
    let mut bias_file = None;
    let mut book_file = None;
    let mut exec = "marketable".to_string();
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        let mut next = || it.next().ok_or_else(|| format!("missing value for {a}"));
        match a.as_str() {
            "--ticks" => ticks = Some(next()?),
            "--bars" => bars = Some(next()?),
            "--strategy" => strategy = next()?,
            "--out-dir" => out_dir = PathBuf::from(next()?),
            "--bar-seconds" => bar_seconds = Some(next()?.parse().map_err(|e| format!("{e}"))?),
            "--equity-sample" => equity_sample = next()?.parse().map_err(|e| format!("{e}"))?,
            "--st-period" => st_period = Some(next()?.parse().map_err(|e| format!("{e}"))?),
            "--st-mult" => st_mult = Some(next()?.parse().map_err(|e| format!("{e}"))?),
            "--bias-file" => bias_file = Some(next()?),
            "--book" => book_file = Some(next()?),
            "--exec" => exec = next()?,
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            other => return Err(format!("unknown arg: {other}")),
        }
    }
    let lane = match (ticks, bars) {
        (Some(_), Some(_)) => return Err("pass only ONE of --ticks / --bars".into()),
        (Some(t), None) => Lane::Ticks(t),
        (None, Some(b)) => Lane::Bars(b),
        (None, None) => return Err("one of --ticks / --bars is required".into()),
    };
    Ok(Args {
        lane,
        strategy,
        out_dir,
        bar_seconds,
        equity_sample,
        st_period,
        st_mult,
        bias_file,
        book_file,
        exec,
    })
}

/// Build the [`FillConfig`] for `--exec`: `marketable` (default), `worseside[:pts]`,
/// `passive[:pts]` or `passivequeue[:fill_prob]` (see `FillModel`).
fn fill_config(exec: &str) -> Result<FillConfig, String> {
    match exec {
        "marketable" => Ok(FillConfig::default()),
        "worseside" => Ok(FillConfig::worse_side(0.5)),
        s if s.starts_with("worseside:") => {
            let pts = s["worseside:".len()..]
                .parse::<f64>()
                .map_err(|e| format!("--exec worseside: bad half-spread: {e}"))?;
            Ok(FillConfig::worse_side(pts))
        }
        "passivequeue" => Ok(FillConfig::passive_queue_e1()),
        s if s.starts_with("passivequeue:") => {
            let p = s["passivequeue:".len()..]
                .parse::<f64>()
                .map_err(|e| format!("--exec passivequeue: bad fill probability: {e}"))?;
            if !(0.0..=1.0).contains(&p) {
                return Err(format!(
                    "--exec passivequeue: fill probability {p} outside [0,1]"
                ));
            }
            Ok(FillConfig::passive_queue(
                backtest_engine::e1::HALF_SPREAD_PTS,
                p,
                backtest_engine::e1::ADVERSE_SELECTION_PTS,
                backtest_engine::e1::CHASE_PTS,
            ))
        }
        "passive" => Ok(FillConfig::passive(0.5)),
        s if s.starts_with("passive:") => {
            let pts = s["passive:".len()..]
                .parse::<f64>()
                .map_err(|e| format!("--exec passive: bad half-spread: {e}"))?;
            Ok(FillConfig::passive(pts))
        }
        other => Err(format!(
            "bad --exec {other} (use: marketable | worseside[:pts] | passive[:pts] \
             | passivequeue[:fill_prob])"
        )),
    }
}

/// Default `SupertrendConfig` with the optional `--st-period` / `--st-mult` overrides.
fn supertrend_cfg(args: &Args, bar_seconds: i64) -> SupertrendConfig {
    let base = SupertrendConfig::default();
    SupertrendConfig {
        bar_seconds,
        period: args.st_period.unwrap_or(base.period),
        multiplier: args.st_mult.unwrap_or(base.multiplier),
        ..base
    }
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    let sim = FillSim::new(fill_config(&args.exec)?);

    // the tick lane (including bars built from ticks) fills at tick prices;
    // prebuilt bars fill at the bar open, an approximation for signal research
    let (result, lane_name, exec_model, input_count) = match &args.lane {
        // ---- tick lane -------------------------------------------------
        Lane::Ticks(path) => {
            let ticks = load_ticks(path)?;
            let n = ticks.len();
            let result = match args.strategy.as_str() {
                "buyhold" => match args.bar_seconds {
                    Some(bs) => run_bars(&mut BuyHold, &ticks, &sim, bs, args.equity_sample),
                    None => run_ticks(&mut BuyHold, &ticks, &sim, args.equity_sample),
                },
                "cvd" => {
                    // tick-driven; --bar-seconds sets the divergence bar (default 15 min)
                    let mut s = CvdExhaustion::new(CvdConfig {
                        bar_seconds: args.bar_seconds.unwrap_or(900),
                        ..CvdConfig::default()
                    });
                    run_ticks(&mut s, &ticks, &sim, args.equity_sample)
                }
                "ofi_demo" => {
                    // signal from the order book (--book), fill on the next trade tick
                    let book_path = args.book_file.as_deref().ok_or_else(|| {
                        "ofi_demo needs an L2 book tape: --book <l2.jsonl> (one JSON snapshot per line)"
                            .to_string()
                    })?;
                    let books = load_books(book_path)?;
                    let mut s = OfiDemo::new();
                    run_ticks_with_book(&mut s, &ticks, &books, &sim, args.equity_sample)
                }
                "supertrend" => {
                    // ticks aggregated into bars; --bar-seconds sets the size (default 30 min)
                    let bs = args.bar_seconds.unwrap_or(1800);
                    let mut s = SupertrendM30::new(supertrend_cfg(&args, bs));
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "sig001" => {
                    // wraps signal-engine's `SignalEvaluator`; entries need the --bias-file sidecar
                    let bias = load_bias(args.bias_file.as_deref())?;
                    let mut s = Sig001Strategy::new(SignalConfig::default()).with_bias(bias);
                    run_ticks(&mut s, &ticks, &sim, args.equity_sample)
                }
                // bar strategies on bars built from ticks, filled at tick prices
                "atr_breakout" => {
                    let bs = args.bar_seconds.unwrap_or(60);
                    let mut s = AtrBreakout::new(AtrBreakoutConfig {
                        bar_seconds: bs,
                        ..AtrBreakoutConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "srlcv" => {
                    let bs = args.bar_seconds.unwrap_or(60);
                    let mut s = Srlcv::new(SrlcvConfig {
                        bar_seconds: bs,
                        ..SrlcvConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "dual_regime" => {
                    let bs = args.bar_seconds.unwrap_or(300);
                    let mut s = DualRegime::new(DualRegimeConfig {
                        bar_seconds: bs,
                        ..DualRegimeConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "avcd" => {
                    // tick-driven; builds its own bars internally
                    let mut s = Avcd::new(AvcdConfig {
                        bar_seconds: args.bar_seconds.unwrap_or(300),
                        ..AvcdConfig::default()
                    });
                    run_ticks(&mut s, &ticks, &sim, args.equity_sample)
                }
                "mphf" => {
                    let bs = args.bar_seconds.unwrap_or(60);
                    let mut s = Mphf::new(MphfConfig {
                        bar_seconds: bs,
                        ..MphfConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "atrx" => {
                    let bs = args.bar_seconds.unwrap_or(300);
                    let mut s = Atrx::new(AtrxConfig {
                        bar_seconds: bs,
                        ..AtrxConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                // session-structure strategies
                "ib_fade" => {
                    let bs = args.bar_seconds.unwrap_or(300);
                    let mut s = IbFade::new(IbFadeConfig {
                        bar_seconds: bs,
                        ..IbFadeConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                "vwap_reversion_cvd" => {
                    // tick-driven: per-tick VWAP and CVD divergence at extremes
                    let mut s = VwapRevCvd::new(VwapRevCvdConfig::default());
                    run_ticks(&mut s, &ticks, &sim, args.equity_sample)
                }
                "va_edge_rotation" => {
                    let bs = args.bar_seconds.unwrap_or(300);
                    let mut s = VaEdgeRotation::new(VaEdgeConfig {
                        bar_seconds: bs,
                        ..VaEdgeConfig::default()
                    });
                    run_bars(&mut s, &ticks, &sim, bs, args.equity_sample)
                }
                other => {
                    return Err(format!(
                        "unknown tick-lane strategy: {other} (available: cvd, sig001, supertrend, atr_breakout, srlcv, dual_regime, avcd, mphf, atrx, ib_fade, vwap_reversion_cvd, va_edge_rotation, ofi_demo, buyhold)"
                    ))
                }
            };
            (result, "tick", "tick-price(faithful)", n)
        }
        // ---- bar lane --------------------------------------------------
        Lane::Bars(path) => {
            let bars = load_bars(path)?;
            let n = bars.len();
            let result = match args.strategy.as_str() {
                "buyhold" => run_prebuilt_bars(&mut BuyHold, &bars, &sim, args.equity_sample),
                "supertrend" => {
                    // one on_bar per supplied bar; bar_seconds is informational here
                    let mut s = SupertrendM30::new(supertrend_cfg(&args, 1800));
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                // srlcv needs real bar volume for its spike filter;
                // atr_breakout and dual_regime need real bar timestamps
                "atr_breakout" => {
                    let mut s = AtrBreakout::new(AtrBreakoutConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "srlcv" => {
                    let mut s = Srlcv::new(SrlcvConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "dual_regime" => {
                    let mut s = DualRegime::new(DualRegimeConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "mphf" => {
                    let mut s = Mphf::new(MphfConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "atrx" => {
                    let mut s = Atrx::new(AtrxConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                // session-structure strategies
                "ib_fade" => {
                    let mut s = IbFade::new(IbFadeConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "va_edge_rotation" => {
                    let mut s = VaEdgeRotation::new(VaEdgeConfig::default());
                    run_prebuilt_bars(&mut s, &bars, &sim, args.equity_sample)
                }
                "vwap_reversion_cvd" => {
                    return Err(
                        "vwap_reversion_cvd is a TICK-lane strategy (needs per-tick CVD + \
                         session VWAP); use --ticks, not --bars"
                            .into(),
                    )
                }
                "avcd" => {
                    return Err(
                        "avcd is a TICK-lane strategy (needs per-tick CVD + anchored VWAP); \
                         use --ticks, not --bars"
                            .into(),
                    )
                }
                "cvd" => {
                    return Err(
                        "cvd is a TICK-lane strategy (needs per-tick aggressor deltas); \
                         use --ticks, not --bars"
                            .into(),
                    )
                }
                other => {
                    return Err(format!(
                        "unknown bar-lane strategy: {other} (available: supertrend, atr_breakout, srlcv, dual_regime, mphf, atrx, ib_fade, va_edge_rotation, buyhold)"
                    ))
                }
            };
            eprintln!(
                "WARN: --bars uses BAR-OPEN fills (signal-research, NOT tick-faithful). \
                 For real execution use the tick lane: --ticks --strategy {} --bar-seconds <N>.",
                args.strategy
            );
            (result, "bar", "bar-open(APPROX,not-tick-faithful)", n)
        }
    };

    std::fs::create_dir_all(&args.out_dir).map_err(|e| format!("mkdir out-dir: {e}"))?;
    let trades_path = args.out_dir.join("trades.jsonl");
    let equity_path = args.out_dir.join("equity.jsonl");
    write_jsonl(&trades_path, &result.trades).map_err(|e| format!("write trades: {e}"))?;
    write_jsonl(&equity_path, &result.equity).map_err(|e| format!("write equity: {e}"))?;
    write_run_manifest(
        &args,
        &sim.cfg,
        input_count,
        result.trades.len(),
        lane_name,
        exec_model,
    )
    .map_err(|e| format!("write run_manifest: {e}"))?;

    let net = result.final_equity - sim.cfg.initial_cash;
    println!(
        "SUMMARY lane={} exec={} fill={} strategy={} input={} trades={} final_equity={:.0} net_vnd={:.0} -> {} {}",
        lane_name,
        exec_model,
        args.exec,
        args.strategy,
        input_count,
        result.trades.len(),
        result.final_equity,
        net,
        trades_path.display(),
        equity_path.display(),
    );
    Ok(())
}

/// Write `run_manifest.json` (binary, input, `FillConfig`, run arguments) beside the outputs.
///
/// `fee_model_probe` is the fee this binary charges at a reference price, so a run made with
/// an outdated fee model can be detected and rejected from the artifact alone.
fn write_run_manifest(
    args: &Args,
    cfg: &backtest_engine::FillConfig,
    input_count: usize,
    n_trades: usize,
    lane_name: &str,
    exec_model: &str,
) -> std::io::Result<()> {
    use std::io::Write;

    let (lane_kind, input_path) = match &args.lane {
        Lane::Ticks(p) => ("ticks", p.clone()),
        Lane::Bars(p) => ("bars", p.clone()),
    };
    // input provenance by size and mtime; a content hash would need another dependency
    let (input_bytes, input_mtime_unix) = match std::fs::metadata(&input_path) {
        Ok(m) => {
            let mt = m
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(-1);
            (m.len() as i64, mt)
        }
        Err(_) => (-1, -1),
    };

    // a fixed reference price keeps probes comparable across runs
    const PROBE_PRICE: f64 = 1000.0;
    let side_fee_probe = cfg.side_fee(PROBE_PRICE, 1);
    let position_fee_probe = cfg.position_fee(1);
    // the retired full-notional PIT model (before ADR 0020) at the same price
    let retired_side_fee = 3_700.0 + 0.0005 * PROBE_PRICE * cfg.multiplier;

    let argv: Vec<String> = std::env::args().collect();
    let manifest = serde_json::json!({
        "schema": "backtest_engine.run_manifest/1",
        "binary": {
            "crate_version": env!("CARGO_PKG_VERSION"),
            "profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        },
        "fee_model_probe": {
            "probe_price": PROBE_PRICE,
            "side_fee_vnd": side_fee_probe,
            "position_fee_vnd_per_day": position_fee_probe,
            "retired_pre_adr0023_side_fee_vnd_would_be": retired_side_fee,
            "note": "If side_fee_vnd is close to retired_pre_adr0023_side_fee_vnd_would_be, this run \
                     was produced by a STALE binary — discard it. See docs ADR 0023."
        },
        "fill_config": {
            "tick_size": cfg.tick_size,
            "multiplier": cfg.multiplier,
            "txn_fee_vnd_per_side": cfg.txn_fee_vnd_per_side,
            "position_fee_vnd_per_day": cfg.position_fee_vnd_per_day,
            "income_tax_rate": cfg.income_tax_rate,
            "initial_margin_ratio": cfg.initial_margin_ratio,
            "slippage_ticks": cfg.slippage_ticks,
            "initial_cash": cfg.initial_cash,
            // the fill model and its signed offset per fill (negative = maker credit)
            "fill_model": format!("{:?}", cfg.fill_model),
            "adverse_points": cfg.adverse_points(),
        },
        "run": {
            "lane": lane_name,
            "lane_kind": lane_kind,
            "exec_model": exec_model,
            "exec_arg": args.exec,
            "strategy": args.strategy,
            "bar_seconds": args.bar_seconds,
            "st_period": args.st_period,
            "st_mult": args.st_mult,
            "equity_sample": args.equity_sample,
            "input_count": input_count,
            "n_trade_records": n_trades,
        },
        "input": {
            "path": input_path,
            "bytes": input_bytes,
            "mtime_unix": input_mtime_unix,
        },
        "argv": argv,
    });

    let path = args.out_dir.join("run_manifest.json");
    let mut f = std::fs::File::create(path)?;
    writeln!(f, "{}", serde_json::to_string_pretty(&manifest)?)?;
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("backtest: {e}");
            ExitCode::FAILURE
        }
    }
}
