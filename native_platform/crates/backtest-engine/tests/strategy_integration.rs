//! Strategy tests that need the backtest driver (`run_ticks`, `FillSim`).
//!
//! They live here because `strategy-core` cannot depend on this crate.

use backtest_engine::{run_ticks, CvdConfig, CvdExhaustion, FillSim, Sig001Strategy};
use market_types::{EventTimestamp, Price, Symbol, Tick, TickSide, Volume};
use signal_engine::evaluator::SignalConfig;

fn tick(ts: i64, price: f64, vol: i64, side: Option<TickSide>) -> Tick {
    Tick {
        symbol: Symbol::new("VN30F1M").unwrap(),
        timestamp: EventTimestamp(ts),
        price: Price::from_f64(price).unwrap(),
        volume: Volume::new(vol).unwrap(),
        side,
    }
}

fn cvd_cfg_1s() -> CvdConfig {
    CvdConfig {
        bar_seconds: 1,
        ..CvdConfig::default()
    }
}

// 10:00 ICT baseline so ticks are in-session; 1s bars for a compact test.
const BASE: i64 = 10_800;

/// Price makes new highs while CVD collapses (one tick per 1 s bar). The signal bar b4 is
/// completed by tick b5, so the short entry fills on tick b6.
#[test]
fn cvd_bearish_exhaustion_enters_short() {
    let ticks = vec![
        tick(BASE, 1250.0, 100, Some(TickSide::Buy)), // b0 cvd 100
        tick(BASE + 1, 1251.0, 100, Some(TickSide::Buy)), // b1 cvd 200
        tick(BASE + 2, 1252.0, 100, Some(TickSide::Buy)), // b2 cvd 300 (peak)
        tick(BASE + 3, 1253.0, 100, Some(TickSide::Sell)), // b3 new high, cvd 200
        tick(BASE + 4, 1254.0, 150, Some(TickSide::Sell)), // b4 new high, cvd 50 (collapse)
        tick(BASE + 5, 1254.0, 1, Some(TickSide::Sell)), // completes b4 → bearish signal
        tick(BASE + 6, 1254.0, 1, Some(TickSide::Sell)), // entry fills here
        tick(BASE + 7, 1254.0, 1, Some(TickSide::Sell)),
    ];
    let sim = FillSim::default();
    let mut strat = CvdExhaustion::new(cvd_cfg_1s());
    let r = run_ticks(&mut strat, &ticks, &sim, 1);
    assert!(!r.trades.is_empty(), "expected a short entry");
    assert_eq!(r.trades[0].side, "short");
    assert_eq!(r.trades[0].action, "enter");
    assert_eq!(r.trades[0].ts, BASE + 6, "entry fills on tick after signal");
}

#[test]
fn cvd_no_signal_no_trade() {
    // flat price: no divergence, no trade
    let ticks: Vec<Tick> = (0..8)
        .map(|i| tick(BASE + i, 1250.0, 10, Some(TickSide::Buy)))
        .collect();
    let sim = FillSim::default();
    let mut strat = CvdExhaustion::new(cvd_cfg_1s());
    let r = run_ticks(&mut strat, &ticks, &sim, 1);
    assert!(r.trades.is_empty());
}

#[test]
fn sig001_no_bias_means_no_entries() {
    // without a session bias the tier-2 gate blocks every entry
    let mut s = Sig001Strategy::new(SignalConfig::default());
    // A burst of one-sided flow that would trip CVD, but no bias → no trade.
    let ticks: Vec<Tick> = (0..400)
        .map(|i| {
            tick(
                32_400 + i,
                1250.0 + (i as f64) * 0.1,
                10,
                Some(TickSide::Buy),
            )
        })
        .collect();
    let sim = FillSim::default();
    let r = run_ticks(&mut s, &ticks, &sim, 5);
    assert!(r.trades.is_empty(), "no bias → no entries");
}
