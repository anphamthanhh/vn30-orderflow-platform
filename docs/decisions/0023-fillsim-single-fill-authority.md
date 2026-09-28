# ADR 0023 — FillSim is the single fill authority; quarantine legacy microstructure_simulator

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (code-only — deployment is the merge; proof is the test row in docs/TEST_MATRIX.md)


> Renumbered 0019 → 0023 (2026-08-21): 0019 collided with
> `0019-autotrade-attribution-persistence` (committed first); 0020–0022 also taken.

- **Status:** Accepted
- **Date:** 2026-08-16
- **Workstream:** PH-STRAT-002 (execution realism, Item-3)
- **Related:** ADR 0008 (parity-first backtest engine, next-tick execution invariant),
  `historical/backtest_engine/fee_hurdle.py`, `historical/research_lab/PRE_REGISTRATION.md`

## Context

Making the backtest "sát live" (execution-realistic) is Item-3 of the research-bottleneck
work. Before adding any realism, we found the repo already carries **four** cost models,
one of which is wrong and inconsistent:

1. **Rust `FillSim`** (`native_platform/crates/backtest-engine/src/fill.rs`) — the authority:
   byte-deterministic, real DNSE fee schedule, 1-tick adverse slippage, fills at the **real
   next tick** (ADR 0008 invariant).
2. **Python `fee_hurdle.py`** — a MIRROR of FillSim (price-dependent round-trip fee),
   reconciled via `assert_fee_parity`.
3. **`PRE_REGISTRATION.md` worse-side 0.5pt** — the frozen forward-accrual fill floor
   (S7/C7/X1); used in `c7_recorder.py` / `basis_screen.py`.
4. **`historical/backtest_engine/microstructure_simulator.py`** — a legacy toy that
   **contradicts** the authority: slippage in `bps` (% of price, not the VND-fixed fees +
   0.05% notional tax → the exact 6.7x error fee_hurdle warns about), fills at a **synthetic**
   `order_price*(1±slippage)` (violates the ADR 0008 next-tick invariant), a made-up
   partial-fill heuristic, an unimplemented "queue position", and RNG non-determinism.

Four models = drift and a live footgun: a junior wiring `microstructure_simulator` into a
go/no-go backtest gets numbers wrong by multiples.

## Decision

1. **`FillSim` (Rust) + its Python mirror `fee_hurdle.py` are the SINGLE fill authority.**
   All fill/cost logic — marketable, worse-side, and future maker/limit — lives there. Any
   Python-side fill (e.g. forward recorders) is a mirror that MUST reconcile with FillSim.
2. **Quarantine `microstructure_simulator.py`** (legacy banner + import-time
   `DeprecationWarning`). It MUST NOT be used for any go/no-go P&L. Retained only so the
   historical e2e determinism guard keeps importing; delete once that guard is retired.
3. **Execution-realism roadmap extends FillSim**, on a fidelity ladder — build the lowest
   tier that changes a decision, then stop:
   - **T0 Marketable** (next-tick + 1-tick) — exists.
   - **T1 Worse-side** (±`half_spread_pts` per fill; frozen PRE_REGISTRATION floor) —
     promoted to a first-class `FillSim` mode so backtest = forward = live use ONE rule.
   - **T2 Limit/maker** (resting limit fills iff the tape trades through; conservative, no
     queue credit) — gated on a **DNSE demo resting-limit probe** answering the external
     unknown "do resting limits fill on VN30F1M?".
   - **T3 queue/partial/latency** — YAGNI until a maker edge appears AND L2 data (currently
     71 sessions) grows. Model complexity must match calibration data.
4. **The load-bearing unknown is external** (do limits fill?), answered by a live demo probe,
   not a simulator. Calibrate any maker model to the probe before trusting an A/B.

## Consequences

- One place to reason about fills; parity-testable across Rust/Python; no silent 6.7x error.
- Worse-side becomes selectable in the backtest lane (`--exec worseside`) so a marginal
  candidate's cost (e.g. X1 basis net −0.14pt under worse-side) is measured with the same
  rule the forward recorders use.
- Legacy `microstructure_simulator` stays importable (no hard break of the e2e guard) but is
  loud and forbidden for decisions. `examples/backtest_supertrend_raw.py` is legacy-noted.
- T2/T3 are explicitly deferred behind the demo probe — no premature queue modelling on
  thin data.

---

## Addendum 2026-08-31 — T2 landed as `PassiveQueue`; the live-probe gate is UNCHANGED

Decision 3 deferred T2/T3 behind the demo probe and called T3 (queue) YAGNI "until a maker
edge appears AND L2 data grows". Two of those three conditions have since been met by
measurement. One has not, and it is the load-bearing one.

- **A maker edge appeared.** E1's passive leg (2026-08-27,
  `historical/research_lab/research/screens/E1_passive_fill_probability.py`; two windows three
  months apart, 7,499 decision points) measured fill rate **90-96%**, median time-to-fill 3-4s,
  and **PASSIVE − TAKER = +0.196..+0.220pt** with non-fills charged the chase (policy vs policy,
  not a cherry-pick of the orders that filled).
- **The data grew.** `default.vn30f_orderbook` now covers two windows a quarter apart, not the
  71 sessions this ADR was written against.
- **The live probe is still UNRUN.** `docs/runbooks/demo-resting-limit-probe.md` is still a SPEC.
  E1 walks a queue over venue SNAPSHOTS: it never placed an order and cannot see cancellations
  ahead of it in the queue. The external unknown this ADR named — *do OUR resting limits fill?* —
  remains open, and no simulator can close it.

**What was added.** `FillModel::PassiveQueue` + `FillConfig::passive_queue_e1()` (`fill.rs`),
mirrored in Python by `costs.passive_queue_slip_points()` / `CostScenario.PASSIVE_QUEUE_E1`,
selectable as `--exec passivequeue[:fill_prob]`. It charges

    adverse = p * (adverse_selection - half_spread) + (1 - p) * chase

= **−0.0555pt/fill** at the E1 calibration (p = 0.90, half-spread 0.12, adverse selection 0.045,
chase 0.12) — a maker CREDIT that is deliberately smaller than E1's own headline composite
(−0.077pt/fill), because the calibration takes the low end of the measured fill band.

**What did NOT change — and must not.**

1. `Marketable` is still the default. Every pre-existing run is byte-identical.
2. **The pre-registered forward floor stays `WorseSide`.** `PRE_REGISTRATION.md` §A1 forbids
   loosening a bar mid-accrual. `PASSIVE_QUEUE_E1` is a maker DESIGN scenario, not a fourth bar;
   its rationale string says "NOT A PROMOTION BAR" where a caller will actually read it, and a
   test asserts that sentence is still there.
3. The demo resting-limit probe remains the go/no-go gate for any maker strategy. When it runs,
   THIS model is what gets re-calibrated — it is not a substitute for the probe.
4. Determinism holds: `fill_prob` enters as an expectation, never as an RNG draw. The cost is
   recorded in the type's own docs — the MEAN is modelled and the DISPERSION is not, so no
   drawdown or tail statistic may be quoted off this model.

**Provenance.** The queue-ahead accounting is the single transferable idea from a survey of
`joaquinbejar/OrderBook-rs` (exchange-side lock-free CLOB, MIT, v0.12). The crate itself was
evaluated and REJECTED as a dependency: it matches orders, whereas we consume DNSE's aggregated
L2 snapshots and never run a book; without order-by-order data its true queue position is
unknowable here anyway; its concurrency + `uuid` trade ids conflict with this engine's
byte-determinism; and a third cost model would re-open exactly what this ADR closed.

