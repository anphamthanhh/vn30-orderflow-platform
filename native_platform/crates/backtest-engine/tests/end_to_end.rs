//! End-to-end: a strategy that opens then closes a position, driven through the
//! public API, with JSONL round-tripped to disk.

use backtest_engine::{run_ticks, write_jsonl, BtDecision, Dir, FillSim, MarketCtx, Strategy};
use market_types::{EventTimestamp, Price, Symbol, Tick, TickSide, Volume};

fn tk(ts: i64, price: f64) -> Tick {
    Tick {
        symbol: Symbol::new("VN30F1M").unwrap(),
        timestamp: EventTimestamp(ts),
        price: Price::from_f64(price).unwrap(),
        volume: Volume::new(1).unwrap(),
        side: Some(TickSide::Buy),
    }
}

/// Enter long on tick 0, exit on tick 2 (indices count on_tick calls).
struct EnterThenExit {
    seen: u32,
}
impl Strategy for EnterThenExit {
    fn on_tick(&mut self, _ctx: &MarketCtx) -> BtDecision {
        let d = match self.seen {
            0 => BtDecision::Enter {
                direction: Dir::Long,
                qty: 1,
                reason: "e2e".into(),
            },
            2 => BtDecision::Exit {
                reason: "e2e".into(),
            },
            _ => BtDecision::Hold,
        };
        self.seen += 1;
        d
    }
    fn name(&self) -> &'static str {
        "enter_then_exit"
    }
}

const BASE: i64 = 10_800; // 10:00 ICT, in-session

#[test]
fn round_trip_produces_enter_exit_and_valid_jsonl() {
    let ticks = vec![
        tk(BASE, 1250.0),
        tk(BASE + 6, 1251.0),
        tk(BASE + 12, 1255.0),
        tk(BASE + 18, 1256.0),
    ];
    let sim = FillSim::default();
    let r = run_ticks(&mut EnterThenExit { seen: 0 }, &ticks, &sim, 1);

    // Enter decision (tick idx 0) fills at tick 1; exit decision (idx 2) at tick 3.
    assert_eq!(r.trades.len(), 2);
    assert_eq!(r.trades[0].action, "enter");
    assert_eq!(r.trades[0].ts, BASE + 6);
    assert_eq!(r.trades[1].action, "exit");
    assert_eq!(r.trades[1].ts, BASE + 18);
    // Long from 1251.1 (buy+slip) to 1255.9 (sell-slip) → +4.8 points gross.
    assert!((r.trades[1].pnl_points.unwrap() - 4.8).abs() < 1e-9);
    // Net must be positive but below gross (fees).
    let net = r.trades[1].pnl_vnd.unwrap();
    assert!(net > 0.0 && net < 4.8 * 100_000.0, "net={net}");

    // JSONL round-trips to disk and parses back.
    let dir = std::env::temp_dir().join("bt_engine_e2e");
    std::fs::create_dir_all(&dir).unwrap();
    let tp = dir.join("trades.jsonl");
    write_jsonl(&tp, &r.trades).unwrap();
    let text = std::fs::read_to_string(&tp).unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines.len(), 2);
    let first: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
    assert_eq!(first["action"], "enter");
    // enter row has no pnl fields (skipped when None).
    assert!(first.get("pnl_vnd").is_none());
    let second: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
    assert_eq!(second["action"], "exit");
    assert!(second.get("pnl_vnd").is_some());
}
