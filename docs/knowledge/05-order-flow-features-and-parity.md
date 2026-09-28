# 05 · Order-flow and book features — one name, one formula, two stacks

> **Desk relevance.** Quoting and hedging decisions run on microstructure: who is aggressing,
> how thin the touch is, whether flow is toxic. This unit defines those features exactly and
> proves the Python research implementation and the Rust engine compute the same numbers.

## Why a registry was needed

Before this work the codebase answered to 9 ATR, 8 value-area, 7 CVD, 5 VPIN and 4 OBI
formulas under the same names (ADR 0037). The fix was a registry (`specs/features/*.yaml`) with a
naming grammar `<family>_<measure>[_q]*[_unit]_v<N>`, a ban on time-shift words in names, and a
fail-closed boundary check (`historical/research_lab/feature_boundary.py`): a feature is only
PARITY once both stacks agree **bit for bit** on a frozen fixture.

## Definitions (as implemented)

| Feature | Definition | Where |
|---|---|---|
| Delta | per print: buyer-initiated `+vol`, seller-initiated `−vol`, unclassified (`U`, auction prints) **0** | `delta.rs`, `tape_cvd.py` |
| CVD `tape_cvd_lot_v1` | running Σ delta; **reset at the ICT day start, roll over at 13:00** (level kept, history cleared); must stay an exact integer | `cvd.rs`, `tape_cvd.py` |
| VPIN | volume buckets of 500 lots; per bucket `|B − S| / V`; rolling mean over 50 buckets in O(1); Normal ≤ 0.5 < Elevated ≤ 0.7 < Toxic | `vpin.rs`, `flow.py` |
| OBI | `(Σ wᵢ·Q_bidᵢ − Σ wᵢ·Q_askᵢ) / Σ wᵢ·(Q_bidᵢ + Q_askᵢ)`, `wᵢ = e^(−β·i)`, bounded [−1, 1] | `obi.rs` |
| OFI (multi-level) | Cont, Kukanov & Stoikov (2014): `eᵢ = 1{Pb≥Pb₋}·Qb − 1{Pb≤Pb₋}·Qb₋ − 1{Pa≤Pa₋}·Qa + 1{Pa≥Pa₋}·Qa₋`, weighted over levels | `ofi.rs` (best-quote version in the research lab) |
| Microprice | `(Q_ask·P_bid + Q_bid·P_ask) / (Q_ask + Q_bid)`, falls back to mid on an empty book | `book_cost.py` |
| Spread and its clock | `ask₀ − bid₀`; ratio to the median at the same minute-of-session over the prior 20 sessions — the spread widens ~2.6× into the closing auction | `book_cost.py`, `spread.rs` |
| Hidden depth imbalance | `(hb − ha)/(hb + ha)` with hidden = total queue − visible top-10 | `book_cost.py` |
| Low-frequency liquidity | Corwin–Schultz (2012), Roll (1984), Amihud (2002); golden values computed at 50 significant digits with `decimal`, sharing no code with the float implementation | `daily_micro.py`, `daily_micro_golden_v1.json` |

Measured on the development window (reported in the source, not re-derived here): the spread
sits at one tick 32 % of the time, median 0.2 point; about 28 book updates share each second, which
is why every row carries an `ingest_seq` (see `09-data-platform-and-provenance.md`).

## The parity contract

`historical/tests/test_l2_parity_cvd.py` states it plainly: *one CVD definition, two stacks, zero
tolerance.* The Python producer and the Rust binary (`cargo run -p orderflow-engine --bin l2_dump`)
read the same frozen tape; the test compares them step by step. Mutation tests prove the value
series can tell apart day-reset vs no-reset, 13:00 rollover vs 13:00 reset-to-zero, and `U → 0`
vs `U → SELL`, and the file records what they *cannot* prove (rollover vs doing nothing at 13:00 —
only observable in the tracker's history). The same pattern pins the TPO/value area
(`test_l2_parity_tpo.py`) and the fee arithmetic (`test_fee_parity.py`).

In this repository the fixtures are **synthetic** (`samples/synthetic/microstructure.py`), pinned by
SHA-256 in their manifests; the original real-tape fixtures are not redistributed.

## Code, tests, demo

| What | Where |
|---|---|
| Python producers | `historical/l2/producers/` (`tape_cvd.py`, `prof_tpo.py`, `book_cost.py`, `daily_micro.py`), `historical/l2/tape.py` |
| Rust engine | `native_platform/crates/orderflow-engine/` (+ `l2_dump`), 31 property tests |
| Registry | `specs/features/*.yaml`, `historical/research_lab/feature_boundary.py`; generated status page `docs/L2_FEATURES.md` |
| Tests | `historical/tests/test_l2_parity_*.py`, `test_l2_book_cost.py`, Rust `cargo test -p orderflow-engine` |
| Demo | `python demos/02-order-flow-parity.py` |

## Findings

- **Best-quote OFI is predictive but fee-walled.** Its information coefficient against the next
  move is about 0.065 — real, and too small to pay the round-trip cost on its own; it is kept as a
  feature, not a strategy (`backtest-engine`'s `ofi_demo` lane exists to show exactly that).
- **An "OFI" that was not OFI.** An audit found the desktop client's order-flow-imbalance signal
  was not the Cont–Kukanov–Stoikov formula; the canonical multi-level version was then written into
  the engine (`ofi.rs`) and the client pointed at it.
- **CVD-exhaustion divergence** in Rust (`cvd.rs`) is a line-by-line port of the Python strategy's
  rolling-window logic, including the pandas `rolling` window semantics.

## Limits

- Microprice and a Kyle-λ-style impact estimate exist in the Rust client, not yet in the engine
  crates; multi-level OFI and the canonical OBI exist in Rust only (no Python parity yet).
- Trade sign comes from the venue's aggressor flag; the Lee-Ready classifier in the source tree is
  tick-rule-first and not wired in.
