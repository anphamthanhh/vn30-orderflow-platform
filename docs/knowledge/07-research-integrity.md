# 07 · Research integrity: pre-registration, holdout lock, trial accounting — and retiring a gate

> **Desk relevance.** A proprietary desk loses money on strategies that were never real. This unit
> is the project's answer to "how do you stop fooling yourself?" — written rules that were frozen
> *before* results were seen, a data lock enforced in code, a ledger of every trial, and a record
> of the times the rules were broken or proved wrong.

## The three instruments

| Instrument | File | What it enforces |
|---|---|---|
| Pre-registration | `historical/research_lab/PRE_REGISTRATION.md` | Fill policy, cost bars, sample sizes and kill thresholds are frozen per card **before** evidence accrues; any change is a dated amendment (A1–A19), never an edit |
| Holdout lock | `historical/research_lab/holdout_lock.json` + guards in `lab.py`, `basis_screen.py`, `daily.py` | 2024-01-01 → 2026-03-04 (~26 % of the bar history) and every session after the cutoff are reserved: no plotting, no gate run, no parameter choice; spent **once**, at a final verdict |
| Trial ledger | `historical/research_lab/trials.jsonl` | Every screen that selects anything is a costed trial; the ledger feeds the trials-deflated Sharpe and the Bonferroni search ceiling |

## Gates, as frozen

**Two-tier forward gate** (the only permission to put a lot on a book):

| Tier | Min sessions | Keep / promote | Demote |
|---|---|---|---|
| P — paper / demo | ≥ 20 | keep accruing | forward expectancy clearly negative, or capture < 40 % |
| L — live capital | ≥ 40 | capture ≥ 60 % of backtest | capture < 60 % |

`capture = expectancy_forward / expectancy_backtest`. **Parity guard (both tiers):** forward trade
count deviating > 30 % from the backtest under the same conditions ⇒ HALT and debug before any
number is trusted.

**Overfitting stop-rules** — trials-deflated Sharpe (Bailey & López de Prado 2014) against the
best-of-N benchmark from the ledger; PBO > 0.15 rejects (Bailey, Borwein, López de Prado & Zhu 2017);
a Bonferroni-style search ceiling; minimum backtest length (Bailey et al. 2014) — if the required
track record exceeds the data, stamp `INSUFFICIENT_DATA` and accrue forward instead of building more
gates.

## The part that is rare: the record of failure

1. **Two holdout burns found and recorded.** A basis screen and a daily-bar lane had read reserved
   sessions without calling the guard. The second was proved arithmetically (2,073 published
   sessions = 1,537 dev + 536 reserved). Consequence: every figure from those runs is
   *hypothesis-generation only*, the guards were added, and guard coverage became a standing
   precondition (amendment A12.6).
2. **A gate retired on measurement (A18).** A8 had made `DSR ≥ 0.95` the promotion condition. The
   deepening programme measured what that requires — a Sharpe/day of 0.080 — against the best ever
   measured, 0.027 (DSR 0.31). Fifty more trials would move the minimum backtest length by 0.018
   years. No card could pass at any sample size, because the shortfall is the effect size the venue
   offers. The bar was kept as **reporting** and removed as **permission**; the two-tier forward gate
   took precedence, bounded by capital instead of confidence: 1 lot, at most 2 concurrent
   deployments, a kill line in points and a session cap written **before** the first order.
3. **A measured input overturned estimated ones.** Taker slippage measured on 5.77 M prints was
   0.2007 pt per fill, not the 0.1 the `REALISTIC` bar assumed; several cards changed sign on that
   single input. A19 followed: a card is only *killed* on measured inputs with a written reopen
   condition; otherwise it is `PARKED-REOPENABLE`.

Programme totals at A18: **43 cards, ~300 trials, zero promotions**. The ledger (`trials.jsonl`)
records 46 screen-level entries: 33 `KILL`, 10 `MARGINAL`, 3 `MEASUREMENT`.

## Cards reach the engine code

Research cards are not only documents. The Rust strategy registry forces every strategy kind to
declare a research verdict at compile time (Killed / Forward / Registered / Underpowered / NoCard),
so a strategy cannot be wired into a backtest or a deployment without its card status travelling
with it; several are recorded there as KILLED with the reason. Strategies such as `ib_fade`,
`va_edge_rotation` and `vwap_reversion_cvd` were implemented against pre-registered specifications.

## What forward evidence may claim (A18, verbatim intent)

- **Can:** fill quality against the measured slippage, cadence parity against backtest, survival on
  the venue (order types, auctions, roll boundaries), implementation = card.
- **Cannot:** that an edge exists. At one lot and a Tier-P sample, forward P&L is not evidence of
  expectancy in either direction.

## Code, docs, demo

| What | Where |
|---|---|
| Protocol and amendments | `historical/research_lab/PRE_REGISTRATION.md` |
| Lock + guards | `holdout_lock.json`, `lab.py` (guard excerpt), `basis_screen.py::assert_dev_only_range` |
| Ledger | `trials.jsonl` |
| Demo | `demos/03-basis-screen.py` §2 (guard accepts DEV dates, refuses the reserved range) and §4 (a screen's own error rates) |

## Limits

The protocol is written for one researcher with AI assistance; the "operator decision" entries are
the human sign-offs. It measures the venue with one lot — it cannot measure capacity or market
impact at desk size.
