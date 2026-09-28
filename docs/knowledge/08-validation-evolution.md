# 08 · Strategy validation: three generations and why each replaced the last

> **Desk relevance.** A validation rule is itself a model that can be wrong. This unit shows the
> project's validation framework changing twice, each time because a measurement showed the
> previous rule was answering the wrong question.

## Generation 1 — walk-forward with a numeric promotion gate (Feb 2026)

The first generation (not distributed; see below) ran a **non-anchored, session-aware
walk-forward**: 60 trading days in-sample, 20 out-of-sample, stepping 20, over a 20-point
Supertrend grid (`period ∈ {7, 10, 14, 20}` × `multiplier ∈ {2.0 … 4.0}`), selected on OOS Sharpe.
Its gate object, `WFAGateThresholds`, promoted a candidate only if all hold:

| Gate | Default | M15 run |
|---|---|---|
| OOS Sharpe | ≥ 0.5 | ≥ 0.5 |
| OOS profit factor | ≥ 1.2 | ≥ 1.2 |
| OOS max drawdown | ≤ 15 % | ≤ 20 % |
| Stability / consistency ratio | ≥ 0.8 / ≥ 0.6 | same |
| OOS trades | ≥ 30 | ≥ 500 |
| Cost budget | 0.30 pt fee + 0.30 pt slippage | same |
| Largest single-fold P&L share | ≤ 40 % | ≤ 35 % |

**What was wrong with it** (found by the project's own later audits, and the reason gen-1 code is
not published here): the run script set commission to zero and read an input file that no longer
existed; the gate compared a percentage-per-trade edge against a threshold in index points; the
spec block carried a 0.5-point tick (the real tick is 0.1); and costs were scalars. No candidate
reached the 500-OOS-trade floor, so none was promoted, and the preliminary single-fold figures were
never presented as results.

## Generation 2 — a research programme with frozen rules (Aug 2026)

`historical/research_lab/` replaced "tune then gate" with **register then measure**:

1. **Stage-0 fee hurdle** before any statistics (`02-transaction-cost-and-pit-tax.md`).
2. **Hypothesis cards** with frozen parameters, cost bar and kill thresholds; every selection is a
   costed row in `trials.jsonl`.
3. **Holdout lock** on ~26 % of the bar history, guarded in code (`07-research-integrity.md`).
4. **Two-tier forward gate**: paper ≥ 20 sessions, live ≥ 40 sessions with capture ≥ 60 %, and a
   parity halt if forward trade counts drift > 30 % from backtest.
5. **Overfitting stop-rules**: trials-deflated Sharpe, PBO, a search ceiling, minimum backtest
   length.
6. **One cost truth** (`costs.py`, mirrored from the Rust fill simulator and parity-tested).

## Generation 3 — measure the venue, bound the risk with capital (Sep 2026, amendment A18)

The programme then measured what its own promotion bar demanded: a Sharpe/day of 0.080 against a
best-ever 0.027, with 50 more trials moving the minimum backtest length by 0.018 years. The bar could
not be passed at any sample size, and measured taker slippage (0.2007 pt on 5.77 M prints) had
flipped several verdicts on its own. So the deflated Sharpe and MinBTL stay as **reporting**, the
two-tier forward gate becomes the permission, and error is bounded by **capital**: one lot, at most
two concurrent deployments, a kill line and session cap written before the first order. A19 adds
that a card is only killed on measured inputs with a written reopen condition.

## The statistical toolkit behind generations 2–3

| Tool | Definition / rule | Source |
|---|---|---|
| Deflated Sharpe | PSR against a benchmark `SR₀ = √Var[SR]·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne)))`, γ = 0.5772 | Bailey & López de Prado (2014); `historical/optimization/dsr.py` |
| Effective trials | `N_eff = N / (1 + (N−1)·ρ̄)`; the ledger feeds N = 659 from 713 configurations | same |
| Minimum backtest length | `MinBTL = z² / SR²` (annualised) | Bailey, Borwein, López de Prado & Zhu (2014) |
| PBO | rank-logit over combinatorial splits; > 0.15 rejects | Bailey et al. (2017); `overfitting.py` |
| Bootstrap | Politis–White block length, session-aligned stationary bootstrap — replacing an earlier i.i.d. bootstrap on autocorrelated data and two tests that turned out to be identities | `overfitting.py` header |
| CPCV | **stamped inapplicable** (fewer than 12 groups) instead of shipping a decorative implementation | `cpcv.py` |
| Look-ahead detector | a feature is causal only if `f(bars[:k]).iloc[-1] == f(bars).loc[k−1]` for every k; it caught a whole-sample quantile that flipped one card's effect from +0.151 to −0.117 point | `historical/signal_eval/causal.py` |
| Twin control | an exit or filter is worth `E[variant] − E[direction-randomised twin]`; one stop worth +0.333 point had a twin worth +0.301, so ~90 % of its value was fat tails, not signal | `trade_forensics.py` |
| Power | `N_required = 6.18·σ²/δ²` (80 % power, one-sided 5 %), UNDEFINED when δ ≤ 0 | `forward_registry` |

## What to take from it

| Generation | Question it answered | Why it was replaced |
|---|---|---|
| 1 | "Does the tuned strategy look good out of sample?" | scalar costs, no trial accounting, no data lock |
| 2 | "Does a pre-registered idea survive costs, the lock and multiple testing?" | the deflation bar was unreachable for this venue's effect sizes |
| 3 | "What does the venue actually cost, and does the card survive contact with it — at bounded risk?" | current |

## Code, docs

| What | Where |
|---|---|
| Walk-forward engine and gate (gen 1) | described here only — superseded, not published |
| Statistics (gen 2–3) | `historical/optimization/dsr.py`, `overfitting.py`, `cpcv.py`, `historical/signal_eval/`, `historical/backtest_engine/trade_forensics.py` |
| Research programme (gen 2–3) | `historical/research_lab/PRE_REGISTRATION.md`, `trials.jsonl`, `holdout_lock.json` |
| Cost truth | `historical/backtest_engine/costs.py` |
