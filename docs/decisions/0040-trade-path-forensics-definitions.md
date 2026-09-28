# ADR 0040 — Trade-path forensics: one definition of MAE/MFE, and what may be concluded from it

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (research-stack library; nothing runs on the VM)

**Status:** Accepted · **Date:** 2026-09-06 · **Workstream:** PH-BT-001 / PH-STRAT-001
**Related:** [0008](0008-parity-first-backtest-engine.md) (Rust owns P&L),
0030, [0038](0038-non-adoptions-metalabeling-and-feature-importance.md),
`historical/research_lab/deepening/BRIEF.md` §2 (FDVS, §A11.7 twin rule),
`plans/260906-0436-trade-forensics/report.md` (the measurements this ADR generalises)

## Context

The operator's report on 2026-09-06: the research framework does not answer the questions a
practitioner asks of a strategy — MAE/MFE, whether a trailing stop is worth it, whether losses cluster
in a volatility regime, whether delaying entry avoids busted signals.

The audit found the ideas present and the *definition* absent. Five implementations of MAE/MFE existed
(`lab.py:624`, `fee_hurdle.py:107`, `cards/D1.py:386`, `cards/D4.py:356`,
`build_c7_masterclass.py:1097`) with **two contradictory sign conventions**, and the canonical
pipeline `backtest_engine/backtest_outputs.py:482` wrote `mae_points = NaN` under the comment *"For
now, mark as N/A"* — a schema that promised excursions and never filled them. Numbers produced this
way cannot be compared between cards, which is why the exit question was re-derived from scratch every
time it came up.

Three bugs found while building the replacement each changed a conclusion, and all three are the kind
that produce plausible output:

1. run directories scored at `WORSE_SIDE` carry slippage **inside the fill prices**; charging a full
   round trip on top double-charges 1.0 pt/trade and turns S8's actual −0.0285 into −1.0285, after
   which every counterfactual exit looks like an improvement;
2. excursions measured from a slipped fill overstate MAE and understate MFE by the slip, and the
   direction-randomised twin turns that penalty into a bonus — S8's 15-minute E-ratio reads **0.71**
   from the fill and **1.15** from the market level, i.e. the sign of the conclusion flips;
3. an "ATR-scaled" barrier built on the M1 path lane (ATR 0.73 pt) rather than the M30 decision lane
   (ATR 4.28 pt) stopped out 91% of the book and produced a false family verdict.

## Decision

### 1. One definition, in `historical/backtest_engine/trade_forensics.py`

`mfe_pts` and `mae_pts` are **signed with respect to the trade's own direction** and **not clamped**;
`mae_abs` is the positive-magnitude alias and is never primary. Excursions are **gross**; costs enter
only through `costs.py`, priced at the entry level with the true `hold_days`. New forensics code
imports this module; it does not re-derive excursions. The five prior implementations stay where they
are (they are cited in published dossiers) but are superseded for new work.

### 2. Excursions are measured from the MARKET at entry, not from the fill

`attach_reference_price(trips, slip_pts)` removes the embedded slippage, computing `ref_price` from
the **original** side so it survives the twin's flip. P&L continues to use the actual fill. The gap
between the two is execution cost, and the programme keeps it apart from the signal.

### 3. Costs are charged per fill, not per round trip, when prices already carry slippage

`embedded_slip_fills ∈ {0,1,2}` states how many of the two fills in the input prices are already
slipped. An unchanged exit on a scored run is charged fees only; a replaced exit pays exactly one
slipped fill. Verified: S8 reproduces the registry's −0.0285 pt/trade, and price-derived gross agrees
with the engine's `pnl_points` to 0.000000 pt/trade.

### 4. Barriers are declared in ATR multiples at the DECISION timeframe

`attach_context(atr_bar_seconds=...)`; the ATR is taken from the last **completed** bar before entry
(causal). A fixed-point grid asks a different question of every card it is pointed at and is therefore
not the default unit.

### 5. Bar-lane barrier results are CONSERVATIVE BOUNDS, not measurements

With OHLC the intrabar order of high and low is unknowable. Every function resolves it adversely and
reports `ambiguous_bars`. On the two cards measured, **36.7%** (S8) and **45.4%** (S7) of trades
contain at least one ambiguous bar, so the assumption is load-bearing. Above ~25%, a card owes a
tick-lane confirm before any barrier number is treated as a measurement.

### 6. A conditioning claim needs a CONTRAST, not a level

`conditional_table` reports the per-cell CI (block bootstrap over sessions), the Šidák-corrected CI for
`k` cells, **and** `mean(cell) − mean(rest)` with its own CI. "This cell is profitable" is not "this
cell is different"; only the second licenses a filter. On a book with a positive unconditional edge
every cell excludes zero — S7's lowest volatility quartile excludes zero even after Šidák, while its
contrast is +4.20 with CI [−4.74, +13.10].

### 7. Every exit variant and every gate carries its twin (§A11.7 restated in code)

`barrier_grid` returns rows in declared order, exposes no argmax and no `best` column, and reports
`edge_over_twin` per cell. Surfacing a family is bucket D; selecting a cell out of it is FDVS Phase 2
and needs the operator. Two independent confirmations now exist that the twin is not a formality: C7's
frozen stop (+0.333 vs twin +0.301) and S8's tightest stop (+0.2169 vs twin **+0.3160**, i.e. a
coin-flip book earns more from the same stop).

## Consequences

- The exit/gate question becomes answerable in one command per run directory, in the same units for
  every card, at zero trial cost — as long as no cell is selected.
- `backtest_outputs.py` still writes `NaN`; wiring it needs a path lane finer than the decision
  timeframe, which that pipeline does not currently pass. Named as owed, not done.
- One Gate-1 condition is still not computable here: the block-bootstrap CI of `F_mean` across the
  grid. Family verdicts from this layer must be reported as **3 of 4 conditions**.
- Adding Stage 2b to the deepening protocol (report §6) is proposed, not adopted: it changes
  `BRIEF.md`, which is an operator decision.
