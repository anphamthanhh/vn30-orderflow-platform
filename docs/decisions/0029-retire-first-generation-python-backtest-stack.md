# ADR 0029 — Retire the first-generation Python backtest stack

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (code-only — deployment is the merge; proof is the test row in docs/TEST_MATRIX.md)


**Status:** Accepted, staged (phase 1 done 2026-09-02) · **Date:** 2026-09-02 · **Workstream:** PH-ARCH-001
**Related:** [0008](0008-parity-first-backtest-engine.md), [0023](0023-fillsim-single-fill-authority.md), 0028

## Context

Three generations of tooling coexist in this repo and none was ever declared dead:

| Gen | What | When |
|---|---|---|
| G1 | 15 Python strategies on `BaseStrategy`, plus `custom_engine`, the `backtesting.py` adapter, the `vectorbt` adapter, and `microstructure_simulator` | 2025 |
| G2 | ADR 0013 gate battery + Rust `strategy-core` (12 strategies) + `auto_trade` | 08/2026 |
| G3 | `research_lab` cards, holdout lock, forward registry | 08–09/2026 |

G1 is where the repo's hard-coded fees live (`custom_engine.py:37,570` and `trade_ledger.py:70`
default `fee_points=0.30`, the exact scalar the `costs.py` docstring exists to forbid), and it is
what a new session's first search hits. It is also barely used: 11 of `custom_engine`'s 13 callers
were throwaway `historical/debug_*.py` scripts, and `vectorbt_adapter` is reachable only through one
CLI flag with no test and no caller. `microstructure_simulator` was already quarantined by ADR 0023
as contradicting the fill authority, and kept importable solely so one end-to-end determinism guard
would not break.

Nothing in G1 feeds a decision any more: realized P&L is the Rust engine's (ADR 0008), fees are
`FillSim`'s (ADR 0023), and research verdicts come from `research_lab`.

## Decision

Retire G1, in stages, so regression coverage is replaced before it is removed.

**Phase 1 — done 2026-09-02.** Move the 12 `historical/debug_*.py` scripts and
`historical/test_custom_engine_full.py` to `_legacy/historical_debug_scripts/`. Verified beforehand
that nothing imports them. This removes most of `custom_engine`'s caller graph without touching the
module.

**Phase 2 — gated on ADR 0028 parity fixtures.** Once a spec-driven parity test covers the
strategies G1 duplicates, move `custom_engine.py`, `custom_backtest_func.py`,
`hybrid_backtest_func.py`, `backtestingpy_adapter.py` and `vectorbt_adapter.py` to `_legacy/`, and
repoint or retire `historical/tests/test_e2e_golden_path.py`, which is the only real dependent of
`microstructure_simulator`. Retire the 15 `BaseStrategy` implementations at the same time; the Rust
`Strategy` trait becomes the sole strategy contract.

**Phase 3.** Delete `microstructure_simulator.py` once its guard is retired, closing ADR 0023's
open item.

Ordering is the point: the parity fixture has to exist first, or retiring G1 silently deletes the
only comparison the repo has between two implementations of the same idea.

## Consequences

- (+) One strategy contract, one engine, one cost authority — the thing ADR 0008 intended and
  ADR 0023 assumed.
- (+) `grep fee_points=0.30` reaching zero becomes a checkable end-state.
- (−) The `backtesting.py` and `vectorbt` adapters go away. Neither has a caller, and the plan's
  benchmark found the vectorized screening idea worth keeping while the *library* is not: the same
  signal-array sweep is ~100 lines of numpy over a `Forecast` (ADR 0027).
- (−) `_legacy/` grows and is frozen by convention, so these files stop receiving fixes. That is the
  intent.

## Alternatives rejected

- **Delete outright now** — would remove `test_e2e_golden_path`'s dependency and the only
  Python-side regression on engine semantics before a replacement exists.
- **Keep G1 "just in case"** — it is not free: it is the source of the scalar-fee defect class and a
  standing invitation for a new session to extend the wrong generation.

## Links

- Audit: `plans/260902-0637-pipeline-architecture-framework-research/research/audit_codebase.md` §2, §7
- Operator decision Q3, 2026-09-02.
