# ADR 0008 — Parity-First Backtest Engine (Rust), Hybrid Metrics

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (code-only — deployment is the merge; proof is the test row in docs/TEST_MATRIX.md)


**Status:** Accepted (2026-08-11) · **Story:** PH-BT-001 · **Supersedes:** none · **Relates:** [0005 Python/Rust split](0005-python-rust-split.md)

## Context
Path to auto-trading VN30F1M needs a trustworthy backtest. The live signal path is Rust (`SignalEvaluator`, `orderflow-engine`). Python `historical/` has a mature backtest+WFA stack but its own re-implementation of CVD/VPIN. Backtesting the Python impl while trading the Rust impl = silent divergence ("backtest green, live red"). We also must not throw away the working Python WFA/metrics.

## Decision
1. **Parity-first Rust engine.** New `crates/backtest-engine` runs the SAME Rust signal code as live (from Phase 03, wrapping `SignalEvaluator`). The engine that computes signals in backtest IS the one that trades.
2. **Two-tier model.** Python = throwaway exploration tier; Rust `backtest-engine` = prove tier. A strategy may auto-trade only after passing the Rust harness.
3. **Hybrid metrics (Rust→JSONL→Python).** Rust emits `trades.jsonl`/`equity.jsonl`; existing Python WFA/metrics consume as-is. No signal logic duplicated in Python; no WFA duplicated in Rust (DRY both directions).
4. **Honest fees single-source in Rust FillSim.** Real DNSE schedule; Python does NOT re-charge.
5. **KISS interop = JSONL + subprocess/file handoff.** Output volume is tiny; WFA is offline batch.

## Rejected
- **Adopt NautilusTrader as dependency** — would reintroduce parity gap (its fill model ≠ our gateway executor). Kept as design reference only.
- **Parquet + PyO3** — premature; output is small, no latency need; PyO3 adds two-toolchain link coupling.
- **Python-side backtest for auto-trade candidates** — parity-breaking vs Rust live path.
- **All-Rust metrics/WFA** — duplicates a mature Python stack.

## Consequences
- (+) Backtest results are trustworthy for go-live decisions; new strategies inherit the `Strategy` trait + harness.
- (+) Determinism + look-ahead safety enforced structurally.
- (−) Requires extracting `SignalEvaluator` into a shared crate (Phase 00) — a live-gateway refactor; parity break risk mitigated by differential test (Phase 02) + unchanged gateway tests. **REALIZED 2026-08-12:** `crates/signal-engine` created (evaluator/registry/trailing/triggers moved verbatim); gateway-server 105 tests pass unchanged; `backtest-engine::Sig001Strategy` now wraps the SAME `SignalEvaluator` the gateway runs (Phase 03 core). Parity with LIVE is now by construction, not a re-port.
- (−) Cross-language JSONL seam + two-toolchain CI (accepted for DRY).

## Addendum — Two data lanes (2026-08-12)
The engine separates two execution lanes so strategy cadence is decoupled from data availability:
- **TICK lane** (`--ticks`, `run_ticks`): tick-native strategies (CVD/VPIN/SIG-001) that need per-tick aggressor deltas; fill at next tick. Ceiling = tick history (ClickHouse `orderflow.trades_raw` ≈ 1 month).
- **BAR lane** (`--bars`, `run_prebuilt_bars`): bar-native strategies (Supertrend) over ready-made OHLCV; fill = decision at bar N close → bar N+1 open. NO tick tape required → runs over multi-month/year bar history (CH `ohlcv_m30`, local CSV, and — pending — SQL Server `DP_VNF1M`).

**Why:** bar strategies do not require ticks, so forcing them through the tick lane needlessly caps them at ~1 month. Both lanes are file-driven + data-source-agnostic; loaders/exporters live at the boundary (DRY with the JSONL seam above). Verified: CVD is rejected in the bar lane (needs tick deltas); Supertrend backtested over 8 months in the bar lane (50 trades) vs 4 trades/1mo in the tick lane.

### Execution invariant (operator, 2026-08-12)
**Entries/exits fill 100% at TICK price** — the signal cadence may be tick or minute-bar, but fills always occur at the real next tick (mirrors live: an order fills at the market's next traded tick), never at a synthetic bar OHLC.
- TICK lane fills at tick (`run_ticks`, and `run_bars` fills at the boundary tick even for minute-signal strategies) → **execution-faithful**; this is the real backtest.
- BAR lane (`run_prebuilt_bars`, no tick tape) fills at bar OPEN → **signal-research ONLY, non-faithful P&L**; the CLI emits a `WARN` and `exec=bar-open(APPROX)`. Bar-only long history is for scanning signal/param behaviour; realized-return decisions require a tick-lane re-run.
- Consequence: the earlier 8-month bar-lane WFA (bar-open fills) is a signal/param-stability probe — its overfitting VERDICT (IS Sharpe ≫ OOS) stands (fill-model-independent in direction), but its absolute P&L is NOT execution-faithful and must be re-run in the tick lane when tick history exists.
