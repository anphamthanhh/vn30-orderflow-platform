# 04 · Basis and index arbitrage: why convergence is "prop-only"

> **Desk relevance.** VN30 index arbitrage (VN30F1M vs the cash basket) is a proprietary-desk
> business. This unit records how the project approached it from a *retail* seat, why it
> concluded the classic trade does not clear a retail cost base, and how it then tested the
> basis as a signal for the only leg a retail account can trade — the future.

## Definitions

- **Basis** `B_t = F_t − S_t`: front-month VN30F1M minus the VN30 cash index (index points).
- **Fair basis (cost of carry)** `≈ S_t × (r − q) × τ`, with financing rate `r`, dividend yield
  `q`, and `τ` the time to expiry in years. Deviations from it are what an arbitrageur trades.
- **Convergence.** At expiry the future settles to the **final settlement price**: the simple
  average of VN30 over the last 30 minutes of the expiry session with the 3 highest and 3 lowest
  values removed (HNX rule). Because it is an average, the expiry-day basis measured against the
  closing index is not exactly zero — an unwind that does not track that average carries risk.

## Stage 1 — literature (why the classic trade is prop-only)

Before any code, a literature-stage survey scored ten return mechanisms against a fee hurdle
(the survey itself is not published here: it used the pre-correction 2-point fee and several of its
citations could not be verified). For basis reversion the conclusion was **not viable from a retail
account — a proprietary-desk trade** — and the structural reasons survive the fee correction:

1. The retail account cannot short the VN30 cash basket (see the header of
   `historical/research_lab/basis_screen.py`), so only one side of the arbitrage band is tradeable.
2. The cash leg (30 stocks) carries its own fees, taxes and execution slippage on top of the
   futures cost in `02-transaction-cost-and-pit-tax.md`.

A proprietary desk removes much of both constraints — lower cost base, basket execution and
inventory. That is the sense of "prop-only". Whether the size of the convergence clears a
**desk** cost base is an open measurement: the source records a desk-cost scenario as a placeholder.

## What the project measured about the basis (development window, hypothesis-generation only)

| Observation | Value |
|---|---|
| Intraday basis half-life | 9.66 minutes (AR(1) φ = 0.9308 on 24,639 M1 pairs) |
| Futures lead cash | corr(ΔF_t, ΔS_{t+k}) = 0.496, 0.241, 0.149 for k = 0, 1, 2 minutes |
| Carry convergence by days-to-expiry | no clear pattern at this sample size |
| Session edges | the daily "open" is really 09:15 (first cash print); the "close" is the 14:45 cross |

These are descriptive statistics from the source's research notes, reported to show what the
screen is built on — not validated results.

## Stage 2 — empirical screen (PH-STRAT-002 / card X1)

`basis_screen.py` asks a different, tradeable question: does the end-of-day basis predict the
**future's** next-session return by more than the cost bar?

| Step | Rule |
|---|---|
| Causal feature | basis at the D close and its trailing 20-session z-score |
| Target | enter D+1 open, exit D+1 close (known at the D close — no look-ahead) |
| Roll | D→D+1 pairs where D is the effective expiry (third Thursday, holiday-shifted) are **excluded** |
| Evidence | Spearman IC sign (negative = reversion), quintile spread, frozen fade rule on the top-quintile `|z|` |
| Cost bars | fee-only, all-in (fee + 2 × 0.1), worse-side (fee + 2 × 0.5) |
| Verdict | `PULSE` (clears worse-side), `MARGINAL` (clears fee only), `KILL` |

**Status.** After the fee correction the screen's verdict on its first window moved from MARGINAL
to PULSE — but that window was later found to overlap the reserved holdout range: the screen had
never called the holdout guard. The burn is recorded in `historical/research_lab/holdout_lock.json`
and every historical X1 figure is reclassified **hypothesis-generation only**. The guard is now wired
in (`assert_dev_only_range`); the only admissible evidence is a frozen forward card judged on
post-cutoff sessions (≥ 60 signals and ≥ 6 months of accrued sessions, gross above the worse-side
cost, hit rate above 52 %, net positive). Stage 3 below shows why a single PULSE at this sample size
is weak evidence either way.

## Stage 3 — what this portfolio adds: the screen's own error rates

`demos/03-basis-screen.py` runs the unchanged screen on synthetic markets with a known planted
effect (`samples/synthetic/index_basis.py`):

- **Null pass rate ≈ 40 %.** With ~220 clean daily pairs and ~44 fade trades against a ~23-point
  session noise, the frozen screen behaves as a direction filter.
- **Minimum detectable effect ≈ 8.5 points per trade** at 5 % / 80 % power.
- A **proposed amendment** — additionally require the fade t-statistic ≥ 1.645 — brings the null
  pass rate to ~3 % at the cost of power for small effects. It is proposed, not applied: changing a
  registered screen after seeing results is exactly what the research protocol forbids.
- With an overnight-persistent basis deviation, the fade rule captures a **mechanical convergence**
  gain on the futures leg — the synthetic analogue of the Stage-1 mechanism.

These numbers describe the screen's behaviour on synthetic data; they say nothing about the real
market except that one year of daily data cannot validate a few-point daily edge.

## Code, tests, demo

| What | Where |
|---|---|
| Screen | `historical/research_lab/basis_screen.py` |
| Expiry rule | `historical/research_lab/daily.py::third_thursday`, calendar `calendar_vn.py` |
| Holdout lock + guard | `historical/research_lab/holdout_lock.json`, `lab.py` (guard excerpt) |
| Cost bars | `historical/backtest_engine/costs.py` |
| Demo | `python demos/03-basis-screen.py` → `samples/outputs/03-basis-screen/` |

## Limits

- Daily horizon only; an intraday basis study would raise the sample size by two orders of
  magnitude and is the natural next step.
- The fair-basis model in the synthetic market uses a constant carry; real VN30 dividends are
  seasonal, which moves fair value inside the year.
