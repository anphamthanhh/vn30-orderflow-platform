# ADR 0043 — Sub-session horizons are closed as alpha; the execution-context lane is opened

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (research governance + code — proof is the test rows in docs/TEST_MATRIX.md)

**Status:** Accepted · **Date:** 2026-09-07 · **Workstream:** PH-DATA-002d
**Related:** 0013 (gate battery, fee hurdle),
[0020](0020-derivatives-pit-fee-correction.md) (the 1.42pt bar),
0030 + `PRE_REGISTRATION.md` §A15
(execution probes spend 0 trials), §A17 (substrate measurements spend 0 trials)

## Context

`trials.jsonl` records **22 KILL, 8 MARGINAL, 0 PASS**, with **14 of 32** trials spent on the
*trend* family. The programme kept concluding that individual ideas were bad. Measured on 1,649 DEV
sessions / 399,182 M1 bars, the more economical explanation is that a whole **horizon** is
arithmetically closed, and the ideas were fine.

Round-trip cost is **1.42pt** worse-side. Against the median absolute move:

| horizon | median \|move\| | cost as % of the move |
|---|---|---|
| 1 bar M1 | 0.30 pt | **473%** |
| 5 bar | 0.70 pt | **203%** |
| 15 bar | 1.20 pt | **118%** |
| 30 bar | 1.60 pt | 89% |
| 120 bar (2h) | 3.00 pt | 47% |
| **1 session** | 8.00 pt | **17.8%** |
| 5 sessions | 21.0 pt | 6.8% |
| 20 sessions | 41.1 pt | 3.5% |

A good directional signal captures perhaps 20–40% of the available move. At 15 bars the cost
**exceeds the entire median move**: you would have to be right about direction *and* capture more
than 100% of a typical excursion. No signal does that.

This is why S7 (229 round trips) cleared its bar at +6.86pt/trade while S8 — *the same trend
mechanism* at 1,205 round trips — did not. The ledger already said it: *"turnover × cost decides the
outcome."* The mechanism was never the problem; the frequency was.

## Decision

### 1. Sub-session horizons are closed as standalone alpha

**No new card may be registered whose holding horizon is shorter than one session**, and the
orderflow/scalping family (OFI, book-imbalance-as-signal, tape momentum, VPIN-as-signal) is closed
in that role. C2 (OFI) is already KILLED; this ADR states the general reason so the next session
does not re-derive it idea by idea.

This is a **horizon** rule, not a data rule. It says nothing about whether order-book data is
useful — see decision 2, which says it is.

**Reopening condition, stated so the closure is falsifiable:** the round-trip cost bar falls below
~0.6pt (measured, not assumed). At 0.62pt the 1-session cost share drops 17.8% → 7.8% and the
sub-session column becomes arguable again. Measuring that is the highest-value work available
(§A15, 0 trials).

### 2. The execution-context lane is opened — and defined by measurement, not intention

A signal that cannot clear the fee bar standalone may still be valuable **after a strategy has
already decided to trade**: how much slippage this entry will cost, whether to work it passively,
whether to wait. That information never has to clear 1.42pt, because it opens no position.

**This is the most abusable idea in the programme.** "It's only context" would exempt anything, and
an exemption that can be claimed by relabelling destroys the fee hurdle and the trial ledger
together. So the lane is entered by passing a **discriminant**, which is a falsifiable property of
the data:

| quantity | what it measures | rule |
|---|---|---|
| **directional IC** — Spearman vs *signed* outcome (favorable − adverse) | alpha content | `\|IC\| ≥ 0.05` → **ALPHA_CANDIDATE**: full battery + fee hurdle, spends a trial |
| **magnitude IC** — Spearman vs *adverse excursion* | execution content | `\|IC\| ≥ 0.10` and directional below the limit → **EXECUTION_CONTEXT**: §A15 lane, 0 trials |
| neither | — | **UNINFORMATIVE** |

The directional test runs **first**, so anything with alpha content is routed to the battery before
the exemption can be considered.

**Why this makes the exemption safe rather than convenient:** a feature with no directional content
*cannot be turned into a trade selector*. Exempting it from a hurdle about trade selection therefore
grants nothing. The exemption is not a favour; it is a statement that the hurdle does not apply.

Thresholds are frozen at module level and are **not function arguments** — a caller must not be able
to tune its way into the exemption. They are **materiality** bars, not significance bars: at
n ≈ 350,000 an IC of 0.005 is "statistically significant" and economically meaningless, so a p-value
here would certify noise.

### 3. The measurement that justifies the lane

Trailing 30-bar volatility, 349,681 DEV observations, 15-minute execution window:

- **magnitude IC +0.407**, **directional IC −0.011** — the exact signature the lane is for.

Conditioned on its own quintile:

| vol quintile | n | adverse median | adverse p90 | **adv/fav** |
|---|---|---|---|---|
| Q1 calmest | 69,937 | 0.50 pt | 1.60 pt | **1.00** |
| Q2 | 69,936 | 0.90 pt | 2.80 pt | 1.00 |
| Q3 | 69,936 | 1.20 pt | 4.00 pt | 1.00 |
| Q4 | 69,936 | 1.70 pt | 5.50 pt | 1.00 |
| Q5 busiest | 69,936 | 2.30 pt | 8.50 pt | 1.05 |

**4.6× between the extremes; an absolute spread of 1.80pt = 127% of the entire round-trip fee bar** —
available with no directional forecast whatsoever. And `adv/fav` pinned at 1.00 in every bucket is
the empirical statement that the feature knows the *size* of the coming move and nothing about its
*sign*.

Also scored, and reported in full (§A17 boundary 4): trailing range 30 (+0.392 / −0.007,
EXECUTION_CONTEXT), minutes-from-open (+0.158 / −0.012, EXECUTION_CONTEXT), volume ratio
(+0.062, **UNINFORMATIVE**), trailing return 30 (−0.047, **UNINFORMATIVE**). Two of five candidates
were rejected, which is the lane working.

## Consequences

- (+) A horizon is closed once, with a number, instead of being re-litigated one idea at a time.
  Fourteen trend trials and a KILLed OFI card were the cost of not having this written down.
- (+) The killed orderflow work is repurposed rather than discarded, which is what the operator
  asked for — and repurposed under a rule that cannot be gamed.
- (+) 1.80pt of conditional execution cost becomes addressable without any directional forecast.
- (−) The closure is a **horizon** rule and could be wrong if the cost bar falls. The reopening
  condition is stated numerically so that this is checkable rather than a matter of opinion.
- (−) The lane's thresholds (0.05 / 0.10) are declared, not derived. They are defensible *because*
  they are declared — but they are conventions, and no one should describe them as estimates.
- (−) The adverse-excursion outcome is an M1 **proxy** for execution cost. Real fill quality needs
  the book, which is on the VM. The proxy orders entries correctly; it does not price them.

## Alternatives rejected

- **Keep testing intraday ideas one at a time.** That is the status quo that produced 22 KILLs, and
  each one spends a trial that raises `SR0` for every other card.
- **Exempt "context" features by declaration.** The failure mode is obvious and terminal: every
  feature becomes context.
- **Use a p-value for the discriminant.** At n ≈ 350k everything is significant. A significance test
  here would admit noise with a certificate.
- **Make the thresholds arguments with defaults.** Then the exemption is tunable, which is the same
  hole as declaration in a different shape. `test_thresholds_are_module_level_constants_not_arguments`
  pins this.
- **Drop the killed families entirely.** They contain measured information about execution; throwing
  that away because it failed a *different* test is the error the operator identified.

## Links

- Code: `historical/research_lab/execution_context.py`
- Tests: `historical/tests/test_execution_context.py` (14) — mutation-verified: removing the
  directional gate reddens 2, letting the excursion window cross a session boundary reddens 1.
- Measurement: `plans/260906-1026-research-strategy-from-standardised-data/strategy_research.md`
