# 03 · Execution realism: taker vs maker, fill models, and measured slippage

> **Desk relevance.** Market making lives on the difference between paying the spread and earning
> it, net of adverse selection. This unit shows how the project priced both sides from recorded
> market data instead of assuming them — and what happened when a measurement disagreed with an
> assumption.

## Taker side: one tick assumed, 0.2 point measured

The default research bar charged one tick (0.1 point) of adverse slippage per fill. Measured later
on 5.77 M market prints from the recorded tape (not on the project's own orders), taker slippage
was **0.2007 point per fill** — twice the assumption — and several
research cards changed sign on that single input (PRE_REGISTRATION amendment A18). The frozen bars
were not re-edited; the measurement became the reason the programme moved to "measure the venue
at one lot" (`08-validation-evolution.md`).

## Maker side: the E1 passive-fill estimate

The E1 study replayed the recorded order book (a queue walk, not live orders) at 7,499 decision
points in two windows three months apart and asked: if we had rested at the touch, how often would
we have filled, and what would it have cost?

| Input | Value | Note |
|---|---|---|
| Half-spread earned on a fill | 0.12 pt | half the measured mean spread (0.24) |
| Fill probability | 0.90 | the **low** end of a measured 90–96 % band; the queue walk cannot see cancellations ahead, so it is already a lower bound |
| Adverse selection given a fill | 0.045 pt | the passive leg kept 0.077 of the 0.12 half-spread |
| Chase on the unfilled rest | 0.12 pt | the exposure was still wanted, so pay the half-spread at the horizon |

`adverse = p·(adverse_selection − half_spread) + (1 − p)·chase = −0.0555 pt per fill` — a maker
**credit**, deliberately less favourable than E1's own headline (−0.077). It is a *design* scenario
(`CostScenario.PASSIVE_QUEUE_E1` in `historical/backtest_engine/costs.py`) used to price a maker
design before building it — never to re-score a strategy registered against a taker bar.

## Fill models in the Rust backtester

`native_platform/crates/backtest-engine/src/fill.rs` is the P&L authority (Python mirrors it and is
parity-tested). The engine is byte-deterministic (no wall clock, no RNG, stable sort); a decision on
tick N fills on tick N+1, always at a tick price.

| Model | Meaning |
|---|---|
| `Marketable` | taker, 1 adverse tick |
| `WorseSide{half_spread}` | conservative: every fill pays the half-spread |
| `Passive{half_spread}` | optimistic upper bound: every fill earns it |
| `PassiveQueue{p, adverse, chase}` | the E1 expectancy above — mean only, not dispersion |

## Order legality at the session edges

The exchange still reported the opening auction at 09:00:00.249 and rejected a market-to-limit
order with "cannot place MTL when trading session is ATO". The session-phase guard therefore allows
MTL only in continuous phases **plus 30 seconds** after 09:00 and 13:00, and new entries stop at
14:43 (`data_provider/session_phase.py` in the source; mirrored in the Rust auto-trade consumer).

## Code, tests, demo

| What | Where |
|---|---|
| Cost scenarios and maker formula | `historical/backtest_engine/costs.py` |
| Fill models | `native_platform/crates/backtest-engine/src/fill.rs` |
| Tests | `historical/tests/test_fee_parity.py`; Rust `cargo test -p backtest-engine` |
| Demo | `python demos/01-cost-model.py` (scenario table), `demos/06-rust-backtest-fill-models.py` |

## Limits

The maker model prices the **mean**; it says nothing about the tail of adverse selection in a fast
market, and the live resting-limit probe that would validate it had not been run at the time of
writing.
