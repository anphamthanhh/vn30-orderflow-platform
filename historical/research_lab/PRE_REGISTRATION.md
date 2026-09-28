# Pre-registration protocol — forward accrual (PH-STRAT-002 P00)

**Frozen:** 2026-08-16 (BEFORE any post-cutoff forward tape is scored)
**Rule:** this file is the single anti-p-hacking gate. Any edit invalidates the
timestamp for records accrued after the edit — forward numbers are only valid when
the policy that scored them was frozen *before* the tape existed. Applies to BOTH
tiers (paper + live). Cross-ref: `plans/20260816-0915-research-bottleneck-alpha-forward-execution/`
(P00 + P01), `holdout_lock.json` (cutoff 2026-08-12), hypothesis cards `S7_*`, `C7_*`.

## Why

Forward out-of-sample earns credibility only if the decision rule is fixed in
advance. Picking windows, thresholds, or fill policy after seeing forward tape
turns the "fresh OOS" into another over-fit. Freeze everything measurable now.

## Candidates under forward accrual

Only the two marginal survivors accrue forward (historical well declared dry;
0/9 full survivors). No trend re-mining — see ADR 0017.

| id | family | mechanism | source card |
|----|--------|-----------|-------------|
| S7 | trend | slow-trend h1 p20 m3 | `hypotheses/S7_slow_trend_forward_holdout.md` |
| C7 | mean-reversion | fade prior-day extreme, 1xATR stop | `hypotheses/C7_short_term_reversal.md` |
| N1 | overnight | high-vol-night overnight drift, side=sign(CLV−0.5) | `hypotheses/N1_overnight_drift_conditioned.md` |

N1 added 2026-08-17 from its DEV screen (MARGINAL: cond gross 1.427pt, net +0.151pt vs worse-side,
per-fold regime-robust). Frozen cell/side fixed by that screen — do NOT re-tune while accruing.
Forward gate (from card): net gross/trade > worse-side cost; IC(CLV,overnight) stays positive;
sign-stable across sub-periods; ≥60 post-2026-08-14 high-vol nights.

## Frozen fill policy (both candidates)

- **Sim-fill = worse-side best quote:** long fills at `ask + 0.5pt`, short at
  `bid - 0.5pt`; fallback `last_px` when the book is empty/crossed. Log `mid` to
  measure assumed spread cost. NO queue-position credit (that is P02; worse-side
  is the honest floor).
- **Costs:** FillSim / `fee_hurdle.py` at the traded price. Round-trip FEE+tax ≈
  **0.3–0.5pt** (corrected 2026-08-16, ADR 0020 — the old "~2pt" conflated a 6×-high
  tax bug with slippage). The worse-side 0.5pt/side above is SLIPPAGE, separate from fee.
- P01 reads THIS policy as the single source; the Rust ShadowLogger must not
  hard-code a different rule.

## Frozen sample sizes & kill thresholds (two-tier gate)

Operator decision 2026-08-16: relax the PAPER tier only; the live-capital tier is
unchanged. Relaxing Tier P lowers the *promotion bar*, never the pre-registration
discipline in this file.

| Tier | Purpose | Min sessions | Promote / keep | Demote |
|------|---------|--------------|----------------|--------|
| P (paper) | fast iterate on demo, zero capital | first read >= 20 | keep accruing | forward expectancy clearly negative OR capture < 40% |
| L (live capital) | eligibility for real orders | >= 40 | forward capture >= 60% of backtest | capture < 60% |

- **Capture** = `expectancy_forward_pts / expectancy_backtest_pts`
  (`gate_report.py --forward-gap`).
- **Parity guard (both tiers):** forward trade count deviating > 30% from backtest
  under the same conditions => signal-generation mismatch => HALT + debug parity
  before trusting any number.
- C7 additional (from its card): net gross/trade > round-trip fee; hit-rate > 52%
  over >= 60 signals; corr(strategy, buy&hold) < 0.5.

## Overfitting stop-rules (documented, reuse existing code)

No new code — these govern when to STOP, per researcher-01:

- **Trials-deflated Sharpe** (`dsr.py` via `trials_ledger.py`): candidate must clear
  `DSR_TRIALS_MIN = 0.95` against the best-of-N benchmark built from `trials.jsonl`.
- **PBO** (`overfitting.pbo`): > 0.15 => overfit (reject); 0.05–0.15 => size-down/MARGINAL.
- **Bonferroni / search-ceiling:** expected survivors ~= log(pool). Pool ~= sum of
  `n_combos` in `trials.jsonl`; at the ceiling, stop screening (do not add trend combos).
- **MinBTL** (`dsr.min_backtest_length`): if required track record > available, stamp
  INSUFFICIENT_DATA and accrue forward rather than build more gates.

## Revalidation schedule

- Forward-gap evaluated per **50 post-cutoff sessions**, **max 2** holdout touches
  (each = one `trials.jsonl` line). Between checkpoints: accrue only, do not peek to
  re-tune. Holdout (`holdout_lock.json`) spent only at a candidate's FINAL verdict.

## Re-screen registration (ADR-0020 corrected cost) — frozen 2026-08-17

The old cost model over-charged the derivatives PIT ~6× (ADR 0020). Several kills were judged
against a wrong (~2pt) bar. This section pre-registers a ONE-TIME re-screen of those kills at the
corrected cost (`research/COST_MODEL.md`), frozen BEFORE any re-run, so the correction cannot become
p-hacking. Governs plan `20260817-0257-screen-rescreen-execution-forward/` phase-02.

**Frozen re-screen list + verdict thresholds** (bar = worse-side ~1.3–1.5pt per COST_MODEL.md; the
statutory ~0.4pt floor is the ceiling, NOT a pass):

| id | known gross/trade | re-screen verdict rule |
|----|-------------------|------------------------|
| X1 basis reversion | 2.898pt | REVIVE if net vs worse-side > 0 AND IC still negative (reversion sign) |
| S3 dual_regime | +0.83/trade, 66 flips | REVIVE only if gross > round-trips × worse-side cost (churn-bound; likely execution-gated) |
| S2 srlcv | median MFE 1.65 @4min | REVIVE if median favorable move > worse-side at the 4-min hold |
| C7 (confirm) | median 1.50, mean 0.24 | already forward; re-confirm median > worse-side; mean-fail noted |
| C2 OFI (checkpoint) | move 0.1–0.7pt | low-prior checkpoint; REVIVE only if any horizon clears worse-side |

**Binding rules (violating any = the re-screen is void):**
1. **Frozen design.** Re-run each idea's ORIGINAL screen design unchanged. NO re-tuning of params,
   thresholds, or horizon — re-tuning is fresh p-hacking, not a bug correction.
2. **Supersede, not append.** Record each result in `trials.jsonl` with the SAME `hypothesis_id`,
   note `"ADR-0020 re-screen; supersedes buggy-fee trial"`. Do NOT add a new pool entry (that would
   inflate the search count and deflate every survivor's DSR).
3. **One bite.** Each listed idea is re-screened exactly ONCE at the corrected cost. Still fails the
   worse-side bar → stays killed, closed.
4. **Holdout locked.** DEV data only (`holdout_lock.json`). A REVIVE earns a FORWARD card (frozen,
   judged post-cutoff), never a live claim from the DEV re-screen.
5. **No new ideas.** The candidate pool stays frozen at the 17 PH-STRAT-002 cards during this pass.

**Re-screen RESULTS (all 5 run 2026-08-17; frozen design, one-bite, supersede n_combos=0):**
- **X1 — REVIVED → PULSE.** `basis_screen.py` unchanged: IC −0.095, fade gross 2.898pt, worse-side
  bar 1.429pt → **net +1.468pt** (was −0.14 at buggy fee). The fee bug was the SOLE killer. FORWARD.
- **S3 dual_regime — CONFIRM-KILL @ worse-side; EXECUTION-GATED.** Rust engine (ticks_full, M5,
  corrected fee): gross +0.830/trade reproduced (66 RT), net **+0.411 statutory / −0.589 worse-side**.
  66 RT × 1.0pt slippage swamps the 55pt gross. Break-even needs cost ≤ 0.83pt/RT → at 0.42 statutory
  the slippage budget is 0.41pt/RT = **≤0.2pt/fill** — the E1 passive-fill target. Revive only via E-series.
- **S2 srlcv — CONFIRM-KILL @ worse-side; execution-gated (tighter).** Rust engine (M1): gross
  +0.639/trade (36 RT, hit 47%), net **+0.219 statutory / −0.781 worse-side**. Budget ≤0.11pt/fill.
- **C7 — re-confirm, forward STRENGTHENED.** Median payoff 1.50 > corrected worse-side ~1.31–1.36 →
  median CLEARS (was < 2pt buggy); mean 0.24 still fails (fat tail → the frozen 1×ATR stop's job). FORWARD.
- **C2 OFI — CONFIRM-KILL.** ofi_demo engine at corrected fee: gross +0.106/trade < even the 0.42
  statutory floor; net negative on both bars. Order-flow is execution-timing, not alpha — holds at true fee.

**P02 conclusion:** the ADR-0020 correction revived the ONE low-turnover idea (X1); the high-turnover
ideas (S3/S2/C2) are now SLIPPAGE-bound, not fee-bound → their fate lives in the E-series (execution),
not the fee. Turnover × worse-side slippage is the real wall at the true fee.

## Change log

- 2026-08-16 — initial freeze (S7, C7; worse-side fill; two-tier thresholds).
- 2026-08-17 — ADR-0020 re-screen registration (X1, S3, S2, C7, C2; worse-side bar per COST_MODEL.md;
  supersede-not-append; one-bite; pool frozen). Additive — does NOT alter the S7/C7 forward policy above.
- 2026-08-17 — N1 added to forward accrual (MARGINAL dev screen; frozen cell/side; overnight high-vol
  cadence). Additive; does not alter S7/C7 policy.

## AMENDMENT 2026-08-25 — cost-bar re-freeze, power gate, α-allocation (PH-STRAT-004)

> **Why amending is legitimate right now, and only right now.** This file's own rule says an edit
> invalidates the timestamp for records accrued *after* the edit. As of 2026-08-25 the entire forward
> record is **one row** (`forward/c7_reversal_ledger.jsonl`, 2026-08-17, net −8.02pt); N1, X1 and the
> S7 shadow log are empty. So re-freezing costs ~1 observation today and would cost the whole record
> at any later date. This is the last free moment. Every change below is registered BEFORE the tape
> that will be scored by it exists.

### Findings that forced the amendment (all verified on the working tree, 2026-08-25)

1. **The scoring bar drifted outside this protocol.** §Frozen fill policy fixes worse-side 0.5pt/side,
   and the 2026-08-17 re-screen section states "the statutory ~0.4pt floor is the ceiling, NOT a pass".
   The E1 probe (2026-08-20, **5 sessions, one regime**, measured *after* the kills) produced a 0.63pt
   "realistic" round-trip that was then used as a pass bar — never registered here. The change log ends
   at 2026-08-17; E1 and S8 never entered it.
2. **S8 (`dual_regime@M30`) was admitted to forward accrual on a bar it fails**, and is absent from the
   candidate table above.
3. **The measurement itself was wrong.** Every Rust run dir dated 08-20/08-21 was produced by a binary
   predating the ADR-0023 fee correction. Rebuilt 2026-08-25 and re-run: on the identical tape and the
   identical 1205 round-trips, first-trade fee **48,560 → 13,443 VND** (3.61×).
4. **The ≥60-signal gate has power ≈ α.** `N = 6.18·σ²/δ²` at 80% power / one-sided α=0.05.
5. **No α is allocated across ≥5 simultaneous candidates** → `P(≥1 false pass) ≈ 1 − 0.78⁵ ≈ 71%`.

### A1 — DUAL-BOOK COST SCORING (frozen 2026-08-25)

Every forward candidate is scored **simultaneously and permanently** at two frozen bars:

| Bar | Definition | Role |
|-----|------------|------|
| **WORSE_SIDE** | fee(price, hold_days) + 2 × 0.5pt | **The promotion bar.** Unchanged from the 2026-08-16 freeze. Nothing is promoted without clearing it. |
| **REALISTIC** | fee(price, hold_days) + 2 × 0.1pt (E1-measured taker slip) | **Reported, never promotive.** Its job is to say whether an execution investment could change a verdict. |

Both are stored on every ledger row and reported together. Neither may be selected after seeing a
result. Rationale: the E1 measurement is genuine information and suppressing it wastes it; promoting
on it is goalpost-moving. Carrying both, with promotion gated on the conservative bar, is the only
use of it that is neither wasteful nor dishonest. Single source of the constants:
`historical/backtest_engine/costs.py`. Changing a value requires a new dated amendment here.

### A2 — S8 `dual_regime@M30`: registered, NOT a keeper

Re-scored 2026-08-25 on the rebuilt binary (bar lane, `m30_dev.jsonl`, 2017-2023, 1205 RT):

| fill | gross/trade | net/trade |
|------|-------------|-----------|
| marketable (≈REALISTIC) | +1.0822pt | **+0.7715pt** |
| **WORSE_SIDE** | +0.2822pt | **−0.0285pt** |

Under the *stale* binary the same run read net **−0.1021pt** — i.e. the fee bug alone made it look
dead. The corrected worse-side figure (−0.0285, was −0.202 on the card) is **at the waterline, still
negative**. Therefore: S8 is **added to the candidate table for forward accrual**, and is explicitly
**NOT a keeper and NOT promotable** unless it clears WORSE_SIDE on forward data. Its question is now
an *execution* question, not a signal question. The `trials.jsonl` note calling it a "NEW keeper" is
superseded.

### A3 — X1 basis reversion: historical numbers reclassified, card retained

`basis_screen.py` queries ClickHouse directly and never calls `assert_export_range_allowed`; the guard
exists only in `export_clickhouse.py` / `export_orderbook.py`. `holdout_lock.json.enforced_by` names
only those two. Of X1's 227 daily pairs, ~145 (2025-08-01..2026-03-04) fall inside the reserved range,
and the design was screened three times on that window.

Decision: **re-scope, do not void.** (a) All X1 *historical* figures — IC −0.095, gross 2.898pt, net
+1.468pt — are hereby **HYPOTHESIS-GENERATION ONLY**; they may not be cited as validation anywhere.
(b) The burn is recorded in `holdout_lock.json.spent`. (c) `basis_screen.py` is wired through the
guard so this cannot recur. (d) The **forward** card stands — the forward record is what would
validate X1 and it has not started, so nothing about it is contaminated.

### A4 — POWER GATE at registration (frozen 2026-08-25)

Every card entering or continuing forward accrual must publish `N_required` (80% power, one-sided
α=0.05, from its own declared δ and observed σ) and the implied years-to-verdict at its own cadence.

| card | δ (pt/trade) | σ (pt) | N_required | cadence | years | status |
|------|--------------|--------|-----------|---------|-------|--------|
| C7 | +0.24 (mean) | ~20 | ~42,900 | ~4/mo | ~890 | **UNDERPOWERED-OBSERVATIONAL** |
| N1 | +0.151 | 6.9 | ~12,900 | ~25/mo | ~43 | **UNDERPOWERED-OBSERVATIONAL** |
| X1 | +1.468 | ~8 | ~184 | ~4/mo | ~3.8 | ACCRUING (feasible) |
| S8 | +0.598 | ~8 | ~1,108 | ~170 RT/yr | ~6.5 | ACCRUING (not promotable, A2) |
| S7 | Sharpe 0.4 ann. | — | ~39 yr equiv. | daily | ~39 | judged on regime + Sharpe CI, not on N |

**Rule:** a card whose years-to-verdict exceeds **3 years** is labelled `UNDERPOWERED-OBSERVATIONAL`.
It may continue to accrue as a *log*, but it may never be described as "accruing evidence", and it
may not be promoted on reaching N. No card is deleted by this amendment — deletion is an operator
decision; this rule only makes the arithmetic unavoidable.

**C7 carries an additional, separate defect.** Its survival criterion is *median payoff 1.50 > cost*
while *mean 0.24 < cost*. P&L accrues on the mean, so as specified C7 is a **negative-expectancy**
design whose rescue (the frozen 1×ATR stop) is untestable by construction, since re-tuning it is
correctly forbidden. C7 therefore cannot be validated by accrual **as currently frozen**; a genuine
test requires a NEW pre-registered card with a mean-based criterion, not more C7 rows.

### A5 — α ALLOCATION AND ALPHA-SPENDING (frozen 2026-08-25)

- Family-wise α = **0.05** across the whole forward family, allocated equally across the `k` candidates
  live at the moment of the first promotion decision (α_i = 0.05/k).
- Exactly **2 interim looks**, O'Brien-Fleming boundaries **z = 2.96 / 2.36 / 2.01**. Touch counter
  maintained in `holdout_lock.json`.
- Below its pre-registered minimum, an evaluator returns `{"n": k, "verdict": "ACCRUING"}` and
  **nothing else** — no hit-rate, no net, no per-trade. A deliberate look requires an explicit
  `--force-peek`, which stamps the output as unblinded and increments the touch counter.

### A6 — S7 paper start: NOT self-authorised, and mis-diagnosed as a decision block

Escalated to the operator (it changes a running system). But the premise that S7 has been "blocked 11
days on a decision, not data" is **wrong**: `StrategyRunner` builds bars only from ticks seen after
process start, with no warmup seeding anywhere, so supertrend p20 @H1 needs ~20 H1 bars ≈ 4-5
sessions of unbroken uptime and resets on every restart. **Starting S7 paper today would produce zero
rows.** Correct sequencing: build bar-history seeding first, then start. Same root cause as the live
auto-trade lane's 0 decisions in 3 sessions.

### A7 — Reproducibility of the daily substrate

`data_provider/VN30F1M_Final.csv` (31MB) is gitignored, so no screen depending on it is re-runnable
from a clean checkout. Decision: **checksum + regeneration from ClickHouse**, recorded in
`historical/research_lab/data_manifest.json` — not Git LFS (ClickHouse is already the canonical
store; LFS adds infrastructure for a derived artifact).

### A8 — POWER GATE: A4's labels contradicted A4's own rule; σ re-measured (frozen 2026-08-25)

> Registered the same day as A1–A7, from a full end-to-end re-run of S8 in
> `notebooks/04_s8_dual_regime_m30_masterclass.ipynb` (Python replica proved trade-for-trade
> identical to the Rust engine: 2,411 trade rows × 2 cost books, 0 mismatches). **No forward row
> exists that was scored under the superseded labels**, so this costs nothing to fix now and would
> cost the whole record later. A4's table is **not edited** — this amendment supersedes its `status`
> column and leaves the frozen text intact as the record of what was believed on the day.

#### Defect 1 — the labels contradict the rule stated directly above them

A4 fixes the rule: *"a card whose years-to-verdict exceeds **3 years** is labelled
`UNDERPOWERED-OBSERVATIONAL`"*. Its own table then labels **S8 (~6.5 yr)** and **X1 (~3.8 yr)** as
`ACCRUING`. Both exceed 3. Applying the rule as written is not a judgement call and requires no new
information: **S8 → `UNDERPOWERED-OBSERVATIONAL`, X1 → `UNDERPOWERED-OBSERVATIONAL`.**

#### Defect 2 — σ was an estimate; measured directly it is larger

A4 used σ ≈ 8 pt for S8. Measured on the 1,205 round trips of the re-scored run:

| quantity | A4 (2026-08-25, estimated) | measured (same day, same tape) |
|---|---|---|
| δ (pt/trade, REALISTIC) | +0.598 | **+0.7715** (the re-scored figure from A2) |
| σ (pt/trade) | ~8 | **12.5604** |
| N_required (80% power, one-sided α=0.05) | ~1,108 | **1,639** |
| cadence | ~170 RT/yr | **196 RT/yr** |
| years-to-verdict | ~6.5 | **8.4** |

N scales with σ², so an under-estimated σ under-states the wait. The direction of the correction is
**against** the card, which is the direction that matters: it moves S8 further from, not closer to,
promotability.

#### Defect 3 (new rule) — the power gate must be computed at the PROMOTION bar

A4's arithmetic was run on the **reported** book (`REALISTIC`). That is the same goalpost move A1
exists to prevent, arriving through a side door: a card can look "feasibly accruing" purely because
the favourable book was used to define δ. At the promotion bar (`WORSE_SIDE`) S8's δ is **−0.0285**,
so `N = 6.18σ²/δ²` is not a meaningful sample size for a positive-effect test at all.

**Rule added (frozen 2026-08-25):** every card under forward accrual publishes `N_required` computed
at the **promotion bar**. Where δ ≤ 0 at that bar, the card publishes
`N_required = UNDEFINED (δ≤0 at promotion bar)` and is **automatically `UNDERPOWERED-OBSERVATIONAL`**.
It may still report the reported-book figure alongside, labelled as such, exactly as A1 requires for
every other number. Rationale: "how long until we know?" is only answerable against the bar that
would actually promote the card; asked against the other bar it is a question nobody is waiting on.

#### Superseding status table

| card | years-to-verdict (promotion bar) | status under A8 |
|------|----------------------------------|-----------------|
| C7 | ~890 | `UNDERPOWERED-OBSERVATIONAL` (unchanged) |
| N1 | ~43 | `UNDERPOWERED-OBSERVATIONAL` (unchanged) |
| X1 | ~3.8 | **`UNDERPOWERED-OBSERVATIONAL`** (was `ACCRUING (feasible)` — rule applied as written) |
| S8 | `UNDEFINED` at `WORSE_SIDE` (δ≤0); 8.4 yr at the reported bar | **`UNDERPOWERED-OBSERVATIONAL`** (was `ACCRUING`) |
| S7 | judged on regime + Sharpe CI, not on N | unchanged |

#### What this changes, and what it does not

- **Does not change any verdict.** S8 stays `FORWARD-ONLY, not a keeper, not promotable` (A2). X1's
  forward card stands (A3). No card is deleted — deletion remains an operator decision.
- **Does change what may be said.** Per A4, an `UNDERPOWERED-OBSERVATIONAL` card accrues as a **log**;
  it may **not** be described as "accruing evidence" and may **not** be promoted on reaching N.
  Every place that currently calls S8 or X1 "accruing evidence" is now wrong and must be reworded.
- **Does change the exit route.** With `N_required` undefined at the promotion bar, waiting cannot
  resolve S8 **in principle**, not merely slowly. The only routes to a verdict are (a) **measure**
  realised fill quality against the break-even slippage of **0.4858 pt/fill** (computed in the same
  notebook, Layer E.9), or (b) **improve** execution and measure again. This is now stated so that
  "still accruing" cannot serve as an indefinite holding pattern.

**Sources:** `notebooks/04_s8_dual_regime_m30_masterclass.ipynb` Layers E.5/E.6/E.9 (re-runnable:
`LAB_HEADLESS=1 python -m jupyter nbconvert --to notebook --execute …`), regression-guarded by
`historical/tests/test_s8_notebook.py`.

### A9 — C7: the licensing number belongs to another design; power inputs corrected; A4's replacement card has no viable design (frozen 2026-08-31)

> From a full end-to-end re-run of C7 in `notebooks/05_c7_short_term_reversal_masterclass.ipynb`.
> That notebook's trade resolver is proved **1:1 against `forward_registry.c7_plans` /
> `c7_recorder.build_ledger`** (324/324 rows, 0 disagreements), i.e. against the exact code that
> will produce C7's forward verdict — so these are not commentary, they are measurements of the
> verdict path. Dev tape only (2017-11-06..2023-12-29); **the holdout was not touched**. As with
> A8: **the forward record holds one row**, so nothing scored under the superseded figures.
> Nothing in A4 is edited; this amendment supersedes what it names and leaves A4's frozen text as
> the record of what was believed on 2026-08-25.

#### Defect 1 — C7's screen numbers were produced under a different design

A4, the card, and `CardSpec` all lean on `mean +0.24 / median +1.50 / hit 55%`. Reproduced on dev as
a ladder, each rung one convention change:

| rung | convention | n | mean | median | hit |
|---|---|---|---|---|---|
| 1 | SCREEN: **fixed whole-sample 17.4pt** threshold, close-to-close, gross | 301 | +1.364 | +3.000 | 57.8% |
| 2 | + **causal trailing-60 quantile** (what the card actually froze) | 324 | +0.572 | +1.800 | 55.2% |
| 3 | + enter at **D+1 open** (what is actually traded) | 324 | +1.086 | +1.300 | 55.9% |
| 4 | + worse-side 0.5/fill + fee, no stop | 324 | −0.195 | +0.045 | 50.3% |
| 5 | + frozen 1.0×ATR stop = **the frozen design** | 324 | **+0.139** | **−0.376** | **48.8%** |

Rung 1→2 is a **defect, not a presentation difference**: a fixed point threshold is a disguised
regime filter (it fires on 3.6% of 2019 sessions vs 29.6% of 2021 — an 8× spread; the causal
quantile spreads 0.101 vs 0.260 like-for-like) computed from a whole-sample quantile the decision
date does not have. This is the same class of error **N1 disclosed about itself**, where the causal
correction flipped the sign (+0.151 → −0.117). C7 never ran that comparison.

The residual gap to the printed `+0.24` is **not checkable without spending the holdout** — the
screen ran on 2,073 sessions, which includes `2024-01-01..2026-03-04`. It stays unresolved by
choice, and is recorded as such.

**Rule reaffirmed (no new rule needed):** a card's screen figures must be computed under the design
the card freezes. Where they are not, the card carries a banner saying so. Applied to
`hypotheses/C7_short_term_reversal.md` on 2026-08-31.

#### Defect 2 — C7's declared power inputs violated A8, silently

`CardSpec('C7')` declared `effect_pts = 0.24` — rung 1 of the ladder, i.e. the Screen's **gross
close-to-close mean under the non-causal threshold**. A8 requires `N_required` at the **promotion**
bar on the frozen design. Measured there:

| quantity | declared (pre-A9) | measured (frozen design, `WORSE_SIDE`) |
|---|---|---|
| δ (pt/trade) | 0.24 | **+0.138486** |
| σ (pt/trade) | 20.0 | **15.990569** |
| signals/yr | 48.2 (= (1−pct)×241) | **54.97** (measured) |
| N_required (80% power, one-sided α=0.05) | ~42,900 | **82,373** |
| years-to-verdict | ~890 | **~1,498** |

The declaration was optimistic by ~1.9×, and it lived in a field `forward_eval` **prints on every
status line** — the place where an optimistic number does the most quiet damage, because it makes
"ACCRUING" read as a plan. Corrected in `forward_registry.py` on 2026-08-31; the notebook now
**asserts** the CardSpec against its own measurement, so restoring the old figure fails a test
rather than passing silently (`historical/tests/test_c7_notebook.py`,
`test_forward_registry.py::test_n_required_matches_the_preregistered_arithmetic`).

#### Defect 3 — A4 described C7's mean/median defect in the wrong direction

A4 says C7's criterion is *"median payoff 1.50 > cost while mean 0.24 < cost"*, i.e. a positive
median hiding a negative mean. On the frozen design it is **the reverse**: `median(net) = −0.376`
and `mean(net) = +0.1385`. A4's **principle stands unchanged** — P&L accrues on the mean, so a
median-based criterion is not a criterion — but the direction in its wording is wrong and would
mislead anyone applying the same reasoning to another card. Wording superseded here; A4's frozen
text untouched.

#### Finding — A4's demanded replacement card has no viable design

A4 concludes: *"C7 cannot be validated by accrual as currently frozen; a genuine test requires a NEW
pre-registered card with a mean-based criterion, not more C7 rows."* That card needs a larger δ or a
smaller σ. Larger δ is **not available honestly**: the (threshold × stop) surface spans 2.29 pt/trade
against an effect of 0.14 with a single-cell standard error of 0.89 — it is noise, and selecting a
cell from it is the overfit DSR exists to punish. That leaves **variance reduction**, which is the
orthodox lever and is pre-registerable without touching the signal: size each trade to constant risk.

Measured (one configuration, no search; `TN-E.13`, and it **spends one trial**): dividing each
trade's P&L by its own stop distance gives

- **δ_R = −0.0247 R/trade** — the sign **inverts**;
- **both directions negative** (long −0.0197, short −0.0295);
- bootstrap 95% CI [−0.0996, +0.0528], P(mean>0) = 0.265;
- mechanism: `corr(|net|, ATR) = +0.439` but `corr(net, ATR) = +0.075`, and the five trades carrying
  the entire result have median ATR **37.7 vs 19.8** for the sample (1.9×). Trade *magnitude* tracks
  volatility; trade *sign* does not. The winners were more **levered**, not more **right**.

**Decision.** The Unresolved item *"C7 replacement card with a mean-based criterion (A4) — needs
registering before any C7 verdict claim"* is **closed by a finding, not by a card.** Registering a
replacement whose only principled variance-reduction route has been measured to invert the sign
would be manufacturing a card to satisfy a checklist — the exact failure this pre-registration
exists to prevent. **Recommendation: CLOSE C7** (operator decision; no card is deleted by an
amendment). If it is kept open it remains a **log** under A4/A8 and may not be called accruing
evidence.

#### What this changes, and what it does not

- **Does not change the frozen design.** Not the threshold, not the stop, not the fills. It also
  does not change C7's label — `UNDERPOWERED-OBSERVATIONAL` either way (δ > 0 at the promotion bar
  in points, so `N_required` is defined here, unlike S8; it is just absurd).
- **Does not delete anything.** The card's screen section is banner-ed, not removed.
- **Does change what may be said.** `median 1.50 / hit 55%` may no longer be cited as evidence for
  C7. The 52% hit-rate gate in C7's own criterion 2 was calibrated against a quantity the design
  does not produce, and the design fails that gate (48.8%) on the very tape that produced it.
- **Does change the portfolio picture.** C7's long/short asymmetry is entirely the overnight leg —
  the premium N1 measures — so C7 and N1 are **not independent** sleeves. `book_v1` gives the C7
  reversal sleeve the largest risk-parity weight (0.48) on a no-fee, points-per-contract MTM series
  whose positive mean is a leverage artefact. Recorded in
  `research/portfolio/PORTFOLIO_EXPERIMENTS.md`; PF4/PF5 must consume it before running.
- **Does correct a production docstring.** `c7_recorder`'s claim of a conservative stop-first bias
  is wrong twice: there is no stop-vs-close ambiguity without a target, and the real unmeasured risk
  (gap-through, 11.8% of stops, −0.0065 pt/trade) makes the daily resolver **flattering**.

**Sources:** `notebooks/05_c7_short_term_reversal_masterclass.ipynb` (TN-E.1 ladder, TN-E.4
asymmetry, TN-E.8 surface, TN-E.10 power gate, TN-E.13 constant-risk), re-runnable headless and
regression-guarded by `historical/tests/test_c7_notebook.py`. Trial ledger: one row,
`C7_constant_risk_accounting`, `n_combos = 1`.

### A10 — BREAK-EVEN SLIPPAGE IS A FUNCTION OF PRICE, NOT A SCALAR (frozen 2026-09-01)

> A measurement correction, not a new rule about what may be claimed. It supersedes the *units* of
> two figures that A2/A8/A9 rely on. No card's frozen design is edited; no cost bar is moved.
> Re-runnable: `historical/research_lab/research/screens/BE_price_ledger.py` (0 trials, reads no
> tape — it is arithmetic over already-published figures).

#### The defect

`costs.py` states, in its own module docstring:

> *"The fee is PRICE-DEPENDENT (the PIT scales with price). A scalar fee constant is always wrong;
> at VN30F1M's 2017-2026 price range it is wrong by up to ~35%."*

The warning was never applied to the two **break-even slippage** specs derived from it. S8's
`0.4858 pt/fill` (A8, notebook 04 E.9) and C7's `0.5750 pt/fill` (A9, notebook 05 TN-E.12) are
quantities **of the tape they were measured on**, quoted as if they were properties of the design.
The derivatives PIT adds `0.00017 pt` per index point to the round-trip fee, and VN30F1M roughly
doubled across the sample.

#### An engine semantic that was load-bearing and undocumented

Verified from A2's own published figures: `gross − net = 0.3107` at **both** cost books, and that
equals `round_trip_fee_points(1242.4)` exactly; also `gross_marketable − gross_worse_side = 0.8000
= 2×(0.5−0.1)`. Therefore the engine's **"gross" is POST-slippage** (the fill price already carries
the adverse tick) and **`net = gross − fee`**. It is not "gross = before all costs". The
zero-slippage gross that a break-even is a function of is `G_raw = gross_at_book + 2·slip(book)`,
and `s*(price) = (G_raw − fee(price, hold_days)) / 2`. This chain reproduces S8's published 0.4858
to 1e-4 and N1's 0.4415 exactly, which is what licenses the restatement below.

#### The restatement

| card | tape mean price | s* @ tape | **s* @ 1900** | net@WORSE_SIDE @ tape | **net@WORSE_SIDE @ 1900** | gap to the 0.5 bar |
|---|---|---|---|---|---|---|
| **S8** | ~1242 (back-solved from its own fee) | 0.4857 | **0.4299** | −0.0285 | **−0.1403** | +0.0143 → **+0.0701 (4.9× wider)** |
| **C7** | ~1036 | 0.5692 (linear) / 0.5750 (bisection) | **0.4958 (linear)** | +0.1385 | **−0.0084 (linear)** | −0.0692 → **≈0.000** |
| **N1** (causal) | ~1036 | 0.4415 | **0.3681** | −0.117 | **−0.2639** | +0.0585 → **+0.1319 (2.3× wider)** |

Two consequences that change how existing verdicts read:

1. **S8's gap to the promotion bar is ~5× larger than published** at the prices it would actually
   trade. "0.0142 pt/fill — 0.14 of one tick" understates it; the honest figure is ~0.07 pt/fill.
2. **C7 crosses the promotion bar from positive to negative between its tape and today's prices.**
   The linear estimate gives net −0.0084; correcting for the observed linear-vs-bisection bias at
   C7's own tape (+0.0058) puts it at ≈+0.003. Either way C7's margin collapses from ~15% of the
   bar to **~0%**, i.e. far inside its own bootstrap CI of [−1.57, +1.89]. C7's positive verdict at
   the promotion bar was, in material part, **an artifact of the price level of its measurement
   tape**. (A9 already recommends closing C7 on separate grounds; this is an independent second
   reason, arriving through the cost model rather than through constant-risk accounting.)

A third, smaller finding: **C7's tape price (~1036) is lower than S8's (~1242)**, so the two cards'
break-evens were never comparable to each other as scalars either.

#### Rule (frozen)

- **A break-even slippage MUST be published at the price level the card would actually trade at**,
  or as a curve over price. A bare scalar is not an acceptable form for this quantity. Use
  `costs.break_even_slippage_points` / `break_even_slippage_curve`.
- Any card doc or amendment quoting a scalar `s*` must state the price it was evaluated at.
- Where a design has a **path-dependent barrier** (C7's 1.0×ATR stop), the linear formula is a
  first-order estimate only, because moving the fill price changes which trades stop out. The
  honest restatement requires re-running the original bisection at the target price. This is
  recorded as **owed for C7** and is not done here.

**Not changed:** the frozen bars themselves (0.1 / 0.5 / −0.0555) are untouched; `WORSE_SIDE`
remains the promotion bar; no card is re-scored at a friendlier scenario.

### A11 — DEEPENING-PHASE PROTOCOL: activity buckets, family-distribution variant studies, retroactive conversion (frozen 2026-09-01)

> Registered **before** the first deepening notebook is written, per the A5 discipline of freezing
> the rule ahead of the run it governs.

#### Why now — the budget, measured

Recomputed from the repo's own code (`trials_ledger.total_trials`, `dsr.expected_max_sharpe`,
`dsr.min_backtest_length`), 2026-09-01:

| quantity | value |
|---|---|
| `N` (sum of `n_combos`, 29 effective records) | **297** |
| `MinBTL(297, SR_target=1.0)` | **8.37 years** vs dev **6.10 years** |
| `SR0 = E[max Sharpe by chance]` | **0.039353** per-daily (0.625 annualized) |
| best Sharpe ever **measured** in the ledger | **0.027** per-daily (0.429 annualized) |
| trend family's share of `N` | **239 / 297 = 80.5%** |

> **The best-of-297 chance bar exceeds the best result the programme has ever measured.** Every
> existing card sits below the noise ceiling before deflation begins.

Solving for the lines: `MinBTL(N) = 6.10 yr` at `N ≈ 84`; the DSR-0.95 line for the best card is at
`N ≈ 4`. **The selective trial budget is `max(0, min(84, 4) − 297) = 0`**, and has been for roughly
213 trials.

**The marginal-deflation argument is retired as the reason for rationing.** 50 more trials move DSR
by ~0.008 and `MinBTL` from 8.37 to 8.65 years; 300 more move it to 9.64. That is not a real
constraint and a rule resting on it would be disbelieved and then ignored. The binding constraint is
different and is not arithmetic:

> **The dev tape can still answer DESCRIPTIVE questions and can no longer answer SELECTIVE ones.**
> Deepening research must be built out of the former.

#### A11.1 — Bucket declaration is mandatory and precedes the run

Every deepening activity declares `bucket ∈ {D, S, R}` in its notebook Layer A and in its
decision-log entry, **before execution**. The rule, one sentence:

> An activity costs a trial **if and only if**, after it runs, a number that was not fixed
> beforehand becomes fixed **because of what the data showed**.

| bucket | meaning | cost |
|---|---|---|
| **D — descriptive** | measures a property of the tape, or of an *already-frozen* design. Output is a distribution, base rate, decomposition, correlation, or measurement. No alternative is ranked for adoption. | **0 trials** |
| **S — selective** | output **is**, or licenses, a chosen configuration. | **≥1 trial per family**, `n_combos` = the full pre-declared grid |
| **R — re-specification** | a design different enough to be a new bet: new mechanism, criterion, cadence, or instrument slice. | **new card + new `trials.jsonl` id** |

Undeclared ⇒ treated as **S**.

#### A11.2 — R-CONV: retroactive conversion

A bucket-D output later cited in support of a configuration change, a card amendment, an engine
parameter, or a promotion argument **converts to a costed trial**, backdated to its original run
date, with `n_combos` = the grid size visible at that run and `notes` prefixed
`"RETROACTIVE CONVERSION"`. Every gate result published between those dates is stamped
`SUPERSEDED-BY-CONVERSION`. **There is no route by which a look becomes free after the fact.**

Precedent: the five ADR-0020 re-screens were written `n_combos: 0` and corrected to `1` in the
schema-v2 batch, on the stated ground that *a second look at already-seen data is the textbook
multiple-testing sin and must cost at least one trial*.

#### A11.3 — FDVS: the family-distribution variant study

The only sanctioned way to explore a variant space (TP / SL / trailing / thresholds / windows).

```
Phase 0  DECLARE  (free)    grid + family statistic + Gate-1 condition + falsifier
                            + a mechanism sentence, hashed and printed
Phase 1  SURFACE  (free)    run the FULL grid; report ONLY family-level statistics;
                            no cell may be named          -> GATE 1
Phase 2  SELECT   (costed)  1 trial at n_combos = |grid|, max-statistic null,
                            mechanism pre-condition, FORWARD-ONLY verdict
```

Family statistics, all three reported, all pre-declared:

| statistic | definition | reads as |
|---|---|---|
| `F_mean` | mean net/trade across all cells, at `WORSE_SIDE` | does the *mechanism* pay, independent of tuning? |
| `F_frac_pos` | fraction of cells with net > 0 | is the region positive, or is one cell carrying it? |
| `F_spread / SE_cell` | (max−min) ÷ SE of a single cell | > ~2 ⇒ the surface is noise; report and stop |

**Gate 1 passes only if all four hold:** `F_mean > 0` at `WORSE_SIDE`; `F_frac_pos ≥ 0.75`; the
stationary-block-bootstrap CI of `F_mean` excludes 0; and the frozen configuration's cell is not an
outlier of its own family. Otherwise the verdict is **`FAMILY-IS-NOISE`, 0 trials**, and that is a
complete, publishable result — the expected outcome, not a failure.

`F_mean` is a **single pre-specified functional** of the grid: it carries no best-of-N inflation,
needs no deflation, and therefore does not interact with the saturated DSR at all. That is why FDVS
survives a zero budget — it is not a selection.

Precedent: C7's TN-E.8 surface has spread 2.29 pt against an effect of 0.14 pt and a one-cell SE of
0.888 — Gate 1 fails, cost 0. The notebook already refused to pick from it.

#### A11.4 — Phase 1 hygiene (what the notebook may not contain)

Within a Phase-1 surface: **no `argmax`, `idxmax`, `nlargest`, `sort_values` on a score column, and
no cell labelled "best".** The frozen cell is reported without emphasis. The declared grid is hashed
in Layer A and the hash **re-asserted after the grid has run**; `n_combos` is a computed expression,
never a typed integer. Every such output carries `[EXPLORATORY]`.

#### A11.5 — Phase 2 requires operator authorisation

Never self-authorised by a notebook or an agent. The selected cell's null is the **max-statistic**
under a stationary block bootstrap of the family (session-aligned blocks, never straddling the
11:30–13:00 break or an overnight boundary), not the per-cell null. The selected cell's direction on
every axis must match the Phase-0 mechanism sentence: **a cell that wins against its own pre-written
mechanism is rejected regardless of its number.** The result is FORWARD-ONLY and inherits
`UNDERPOWERED-OBSERVATIONAL` unless it publishes `N_required` at the promotion bar per A8.

#### A11.6 — Anti-laundering

A family that fails Gate 1 may **not** be re-run with a narrowed grid, a different statistic, a
different cost book, or a different cadence. A genuinely different family is a **new card**
(bucket R) and inherits the one-bite rule of the 2026-08-17 re-screen section.

#### A11.7 — The twin rule for exit research

Exit variants are a special case and get a stronger requirement, because the repo has already
measured the trap. C7's frozen stop is worth +0.333 pt/trade, and a cost-null book with **random
direction and the same stop** gains **+0.301** (A9 / TN-E.7) — ~90% of the stop's apparent value is
a property of a fat-tailed price series, not a harvest of signal.

> **Every exit variant is scored as `E[variant] − E[the variant's own direction-randomised twin]`**,
> on the same days, the same cadence, the same cost book. Comparing exit variants against the frozen
> design, or against each other on the same directional signal, confounds signal harvesting with the
> mechanical tail-clip and manufactures a false winner every time.

Corollary that must be printed, not assumed: MFE statistics (`median MFE +7.2pt`, `82.4% of trades
exceed the cost bar in MFE`) are the max of a path in the chosen direction on a symmetric fat-tailed
series, and are expected to be largely reproduced by the twin. **Until the twin figure is printed
beside it, "the signal produces abundant movement" is not established.**

#### A11.8 — `N_eff` is frozen out

`dsr.effective_trials` would reduce deflation for correlated grids and is, on today's numbers, the
single change most able to flip a card from fail to pass (S8@`REALISTIC` would need `N ≈ 4`).
Adopting it mid-deepening would be indistinguishable from choosing the denominator that passes. It
may be adopted **only** by a separate dated amendment, registered **before** the run whose verdict it
would change, and only with the aligned per-trial return matrix `effective_trials` actually requires
— which `trials.jsonl` does not store (it holds a scalar `is_sharpe`). That is a schema v3.

#### A11.9 — Budget statement, republished on every deepening verdict

`N`, `SR0`, `MinBTL`, `history_years`, and `budget = max(0, min(N_crit^MinBTL, N_crit^DSR) − N)`.
Today: **297 / 0.03935 / 8.37 yr / 6.10 yr / 0**.

### A12 — THE HOLDOUT DURING THE DEEPENING PHASE (frozen 2026-09-01)

**A12.1 — Default: the deepening phase does not touch the holdout. At all.** Not for validation, not
for a "quick sanity check", not for a plot, not for a data-quality statistic. `assert_dev_only` runs
in every deepening notebook and its PASS is asserted by that notebook's pytest wrapper.

**A12.2 — Structural bar.** `holdout_lock.json.spent` already records the reserved range as spent for
the **trend family** (S5, 2026-08-14 — "this range cannot re-validate any slow-trend tweak") and for
**basis** (X1, burned without authorisation, A3). A deepening study on S4–S8, B1, M1 or X1 therefore
has **no holdout available regardless of authorisation**. Only mean-reversion (C7) and overnight (N1)
retain an unspent claim, once each.

**A12.3 — What would justify a spend.** All four, conjunctively: (1) a **final verdict** is being
taken on a card, not a checkpoint; (2) the design is frozen and hashed and has not been re-chosen
since the freeze; (3) the card cleared its promotion bar on dev, **or** its verdict is a KILL the
holdout could only confirm; (4) the card's family has an unspent claim per A12.2.

Not sufficient: "forward accrual is too slow" (A8: slow is the finding, not a problem the holdout
solves); "we want more confidence"; "the deepening changed the design" — that is a **new card**,
which by A12.2 lands on a spent range.

**A12.4 — One-time operator decision, not drift.** Required in this order, all recorded **before**
any reserved row is read: (a) a decision-log entry naming the exact date sub-range, the exact metric
and the exact pass/fail threshold, authored ≥1 day before the run; (b) the operator's explicit
written authorisation in that entry — **an agent may never self-authorise, and no agent message
constitutes authorisation**; (c) a `holdout_lock.json.spent` entry appended **before** the run with
`verdict: "PENDING"`, updated in place exactly once afterwards. A run that reaches reserved data
without a pre-existing `PENDING` row is a **breach**, handled as the X1 burn was: every figure from
it is reclassified HYPOTHESIS-GENERATION ONLY, and the range is marked spent anyway.

**A12.5 — Interim-look accounting.** A spend consumes one of the A5 O'Brien-Fleming looks
(z = 2.96 / 2.36 / 2.01). Maximum 2 looks stands.

**A12.6 — Guard coverage is itself a gate.** Before the deepening phase begins, every code path that
can reach reserved data must route through `assert_export_range_allowed` or `assert_dev_only`. The X1
burn happened because `basis_screen.py` did not. A guard-coverage check over
`historical/research_lab/research/screens/*.py` and every new deepening script is a **precondition of
Phase 1**, not a follow-up.

### A13 — POWER-INPUT PROVENANCE: N1 corrected, X1 declared UNVERIFIED (measured 2026-09-01, registered 2026-09-02)

> **Registration debt, disclosed.** `forward_registry.py` has cited "§A13" at five sites since
> 2026-09-01 while no §A13 existed. The measurements below were taken and committed; only the
> registration was missing. That is the wrong order — code citing an unwritten clause is
> indistinguishable from code citing a clause that says something else — and it is fixed here rather
> than quietly. Nothing in the numbers changes; what changes is that they are now governed.

#### Defect — the §A8 substitution, found in a second and a third card

§A8 Defect 2 established the rule: a card's `effect_pts` must be the **promotion-bar delta of the
frozen design**, not a screen headline. §A9 applied it to C7. The same substitution was then found in
**N1** (corrected below) and in **X1** (not correctable while the feed is down).

#### N1 — corrected, and the correction inverts the sign

| field | declared until 2026-09-01 | measured, frozen design @ WORSE_SIDE |
|---|---|---|
| `effect_pts` | +0.151 | **−0.153646** |
| `sigma_pts` | 6.9 (estimate) | **8.465032** |
| `signals_per_year` | 48.2 | **50.78** |
| `N_required` | 12,904 (~268 yr) | **UNDEFINED** (§A8 Defect 3) |

The +0.151 was the screen's **non-causal whole-window** figure (`rv_z >= rv_z.quantile(.80)` over the
whole sample), computed on a **third** daily lane (`N1_overnight_drift_screen.load_tape_daily`, no
session filter) and netted **cohort-wise** — mean gross minus one bar at the median price — rather
than per trade. The frozen forward design gates **causally**
(`rolling(60).quantile(.80).shift(1)`) on `lab.daily_bars` and fills per trade. The card disclosed the
causal gap on 2026-08-17 (−0.117, hit 44%) and the governance field was never corrected: the same
silent-optimism failure §A9 found in C7, now in a second card.

Measurement: dev tape 2017-11-06..2023-12-29 (1,537 sessions, 303 nights), frozen design at
WORSE_SIDE, 1:1 parity-checked against **both** `n1_plans` and
`n1_recorder._build_ledger_v1_reference` (303/303 rows, 0 disagreements). Fee charged per ICT day
held (Fri→Mon = 3 days); at the registry's own `hold_days=0` it reproduces the card's disclosed
−0.117 exactly. Reported alongside, **not** the promotion bar: REALISTIC δ +0.646351, σ 8.465033 →
N = 1,060 (~20.9 yr). Break-even half-spread **0.4232 pt/fill**, i.e. between the 0.1 and 0.5 bars —
**N1's sign is decided by execution, not by signal.** Source
`research/screens/N1_power_remeasure.py` (+ `_results.json`). **Trials spent: 0** (bucket D: it
measures an already-frozen design and ranks no alternative).

**Rule (frozen).** δ ≤ 0 at the promotion bar ⇒ `N_required` is UNDEFINED and the card is
automatically `UNDERPOWERED-OBSERVATIONAL`. `n_required()` returns `UNDEFINED_N` rather than the
plausible 2,858 the squared formula would otherwise produce. A negative `effect_pts` in a `CardSpec`
is a **declaration of what the frozen design does**, never a claim.

#### X1 — cannot be corrected, so it is labelled

X1's substrate `default.basis_m1` needs a live ClickHouse feed and the VM was unreachable. Its fields
are **left as declared** and must not be read as a promotion-bar measurement:

- `effect_pts = 1.468` is the net figure **§A3 reclassified HYPOTHESIS-GENERATION ONLY**, because
  `basis_screen.py` read the reserved range without a guard (~145 of X1's 227 daily pairs sit inside
  2025-08-01..2026-03-04).
- `sigma_pts = 8.0` is an **estimate, never measured** — the class of estimate §A8 Defect 2 caught for
  S8, where the measured σ (12.56 vs ~8) moved `N_required` by 1.6× against the card.

Consequence on a status line: X1 renders N = 184 (~3.8 yr), by a wide margin the most attractive card
in the registry, **on numbers the programme's own amendment says are not evidence**.

**Rule (frozen).** `power_summary()` for X1 is `UNVERIFIED`, not a plan. **Owed:** re-measure δ/σ on
the frozen design at WORSE_SIDE, dev-only, parity-checked against `x1_plans`, exactly as N1 was —
now unblocked, the VM returned 2026-09-02. Until that runs, no X1 figure may be cited in a
prioritisation, a roadmap, or a portfolio weight.

#### What this changes, and what it does not

Changes: two governance fields and one label. Does **not** change: any forward row, any ledger, any
verdict. N1 was already `UNDERPOWERED-OBSERVATIONAL`; this states why in the registry rather than only
in the card.

---

### A14 — §A9's MECHANISM CLAIM IS WRONG: C7 AND N1 ARE NEAR-ORTHOGONAL (frozen 2026-09-02)

> Amends **§A9**, an amendment of my own authorship. §A9's decomposition arithmetic is correct and
> stands; the quantity it was applied to was the wrong one, and the conclusion drawn from it is
> withdrawn.

#### The claim being withdrawn

§A9 held that C7's per-side asymmetry comes from the overnight drift, and therefore that "C7 and N1
share one factor" — two cards measuring one premium. That claim propagated: it is the basis of the
`Unresolved` bullet asking whether N1's forward accrual should be re-scoped as double counting, and it
was cited when weighing which of the two to close first.

#### Measured (notebook 06, dev tape, 1:1 parity with `forward_registry.n1_plans`, 303/303 rows)

| quantity | value |
|---|---|
| Overnight leg's contribution to C7 P&L | **0%** — C7 enters at the **D+1 open**, after the gap |
| C7's asymmetry, decomposed | **74% stop geometry + 26% intraday path** |
| Shared decision days | 113 |
| Days the two cards take **opposite** sides | **94.7%** |
| Correlation of daily P&L | **+0.10** |

The two designs are opposed **by construction**: N1 takes `side = sign(CLV − 0.5)`, C7 takes
`side = −sign(return)`. §A9 applied its decomposition to the close-to-close payoff of the decision
day, not to C7's P&L, which begins the next morning.

#### What they DO share, and it is a stronger statement

Both are **negative in risk units** at the promotion bar — C7 −0.0247 R/trade, N1 −0.0268 — despite
being near-orthogonal. Two independent designs failing at the same place is evidence about the
**venue**, not about either mechanism: apparent gross comes from selecting **larger** observations,
not **righter** ones. Orthogonality means this is two failures, not one failure counted twice.

#### Consequences (frozen)

1. The `Unresolved` bullet "whether N1's forward accrual should be re-scoped now that C7's asymmetry
   is shown to BE the N1 factor" is **struck**: the premise is false. If N1 is re-scoped it must be
   for its own δ ≤ 0 (§A13), not for redundancy.
2. Any portfolio construction may treat C7 and N1 as **near-orthogonal sleeves** (ρ ≈ +0.10) — while
   noting §A9/TN-E.13: both are negative in R, so orthogonality is a diversification property of two
   losing series and is not a reason to hold either.
3. §A9's other three findings (C7 power inputs, the mean-vs-median direction, the closure of A4's
   replacement card) are **unaffected** and stand.

**Trials spent: 0** (bucket D).

---

### A15 — E-SERIES PROBES DO NOT SPEND TRIALS (frozen 2026-09-02, operator decision)

#### The contradiction being resolved

Three sources gave three answers: `EXECUTION_EXPERIMENTS.md` said a killed E-probe **spends** a trial;
`trials.jsonl` contains **zero** E rows after two E1 probes ran (2026-08-20, 2026-08-27); the
2026-09-02 plans proposed it should spend **none**. A rule that the ledger has never once obeyed is
not a rule.

#### The rule (frozen)

> **An E-series execution probe spends 0 trials, because it selects no alpha parameter.**

It follows directly from §A11.1, whose test is whether *a number that was not fixed beforehand becomes
fixed because of what the data showed*. An execution probe fixes a **cost**, and cost is an input to
every card's verdict rather than a chosen configuration of any one card. Charging it a trial deflates
every card in the programme for a measurement that makes all of them **more** honest — precisely
backwards.

#### Boundaries, so this does not become a loophole

An E-item spends **≥1 trial**, or becomes a card, the moment any of these is true:

1. Its output **selects an entry or exit rule** for a specific card, not just a fill price.
2. It sweeps a grid whose winner is then **adopted into a card's frozen design** (that is bucket S,
   and `n_combos` is the full pre-declared grid).
3. It re-scores an **existing** card at a new cost scenario in order to change that card's verdict.
   Re-scoring for **reporting** is free; re-scoring to **promote** is a new bet, and §A1's dual-book
   rule still forbids loosening a bar mid-accrual regardless.

Probing *fill quality* — fill rate, queue position, time-to-fill, adverse selection, spread by
time-of-day, cancellation rate — is always free under this rule, at any number of configurations,
because none of it names a trade the strategy would not otherwise have taken.

#### Operator context (2026-09-02)

Recorded verbatim because it sets the intent this clause is to be read with: *"nới giới hạn nhất có
thể, vì chúng ta đang test, nếu quá khắt khe có thể loại bỏ đi mất những chiến lược phù hợp"* —
loosen as far as is defensible while the programme is still measuring, because an over-strict rule
discards designs that would have worked. §A15 loosens the **execution** lane, where the hurdle is zero
and the measurement helps every card. It deliberately does **not** loosen §A1 (the promotion bar),
§A11 (the selective budget), or §A12 (the holdout): those three are what keep the loosened lane
meaningful. The asymmetry is the point — **measuring is cheap, selecting is not.**

#### Retroactive application

The two E1 probes already run are **confirmed at 0 trials**; no back-fill row is owed and N is
unchanged at ~297 + whatever the V-wave reconciliation adds. `EXECUTION_EXPERIMENTS.md` is corrected
to match this clause, which is the authority.

---

### A16 — THE SESSION HAS TWO CLOSES, AND THE LAB HAS BEEN USING BOTH (frozen 2026-09-02)

> Registered because a semantic change to `mp_profile` moves numbers under cards that are already
> registered. The change is small and bounded (23 of 1,537 dev sessions, §A16.3); the finding it
> uncovered is not.

#### A16.1 — Measured structure of the close

The phase boundaries were known (ADR 0024: continuous PM 13:00-14:30, ATC 14:30-14:45); where the
prints actually land was not. Measured on the 2,073-session M1 tape:

| minute | bars | median volume |
|---|---:|---:|
| 14:29 | 2,052 | — |
| **14:30** | 1,837 | **102** |
| 14:31-14:44 | ~25 total | — |
| **14:45** | 2,037 | **4,997** |

Continuous trading runs **through** the 14:30 minute. 14:31-14:44 is the auction's order-collection
window and prints almost nothing. The closing cross lands in the **14:45** bar carrying **~49×** a
normal minute's volume. It is, by volume, the single most liquid execution of the day.

#### A16.2 — The defect: two lanes, two closes, never reconciled

| lane | how it takes "the close" | what it gets |
|---|---|---|
| daily (`daily.daily_bars`, `g["close"].last()`) | last bar of the ICT day | the **14:45 cross** |
| profile (`mp_profile.build_session_profiles`) | last bar of its own window | the **14:30 continuous close** |

Cards on the daily lane (N1, C7, X1) and cards on the profile lane (the V-family; V11 uses
`entry = cur.close`, `exit = nxt.close`) have been transacting at **different prices for the same
session**, and nothing in either lane said so.

Verified directly rather than inferred, over the 1,537 dev sessions: the daily lane's close equals
the **14:45 cross on 98.0%** of sessions (1,507; the residual 30 are sessions whose tape carries no
cross bar, where it falls back to the continuous close) and equals the **profile lane's close on
5.5%**. The two lanes are not approximately the same number with occasional disagreement — they are
different quantities that coincide by accident.

Measured over the 1,507 dev sessions carrying both:

| statistic | value |
|---|---|
| median \|gap\| | **1.50 pt** |
| mean \|gap\| | 2.35 pt |
| mean signed gap | −0.010 pt (unbiased — noise, not drift) |
| sessions where the two are identical | **5.0%** |
| sessions where \|gap\| exceeds the 1.28pt cost bar | **53.4%** |
| p90 / p99 / max | 5.40 / 12.40 / 56.00 pt |

The gap is **an order of magnitude larger than any effect this programme has measured** (V6 lift
+0.240, V3 lift 0.12–0.20, N1 δ −0.154, C7 δ +0.139). It is the same class of defect as the two daily
lanes that moved N1 by 4.4× its own effect, and it was invisible for the same reason: two reasonable
implementations of one English word.

#### A16.3 — What changed in the code, and what it moved

`mp_profile` now separates three things that one window used to do:

- `AFTERNOON` ends at **14:31** (continuous, including the 14:30 bar);
- `AUCTION_COLLECT` 14:31-14:45 contributes **no** TPO time and no volume;
- `ATC_PRINT` 14:45 is retained by `in_session` but excluded from `bracket_id`, and surfaces as the
  new field `SessionProfile.atc_close`. `SessionProfile.close` remains the last **continuous** close —
  byte-identical to the previous behaviour, because the old window was end-exclusive at 14:45 and so
  never contained the cross.

**Impact, measured old-vs-new across the 1,537-session dev tape:** the two builds return the same
1,537 sessions, and **23 sessions (1.50%)** differ in any field — every one of them a session with a
straggler print in the 14:31-14:44 collection window, which the old code admitted into bracket A3.
Per field: `close` 19, `session_volume` 23, `n_bars` 23, `vah` 5, `val` 5, `high` 4, `vah_vol` 6,
`val_vol` 4, `poc` 2, `vpoc` 2, `low` 1.

**A correction to an earlier claim of my own, made the same day:** an audit note asserted that
"bracket A3 *was* the auction" and that V5/V10 were entering and exiting at the ATC print. Measurement
refutes both. The old window's end-exclusivity kept the cross out, so the profile was never
contaminated by the auction and the V-family's execution prices were the continuous close throughout.
The real defect was narrower and different: the code could not *express* the distinction, so the
collection-window stragglers had nowhere to go, and the daily lane silently disagreed with the
profile lane about what "the close" means.

#### A16.4 — Rules (frozen)

1. **A card that says "at the close" must name which close** — `close` (continuous) or `atc_close`
   (the cross) — in its frozen design. A design that does not name one is read as `close`, which is
   what every existing card has been getting on the profile lane.
2. The **profile** never contains a batch cross. TPO counts, volume buckets, the value area and the
   profile extremes are continuous discovery only. Excess, tails and poor high/low are defined against
   those extremes, so a cross must not be able to manufacture one.
3. The cross **is** a legitimate execution price and may be chosen deliberately. It carries ~49× a
   normal minute's volume, which makes it the most liquid fill of the session — relevant to every
   card whose exit is "the close" and to the execution programme generally.
4. Re-stating a phase boundary outside `mp_profile` (Python) or `market_time` / `consumer.rs` (Rust)
   is forbidden. Three IB definitions and one triple-duty afternoon window is what this clause exists
   to prevent recurring.

#### A16.5 — What this does NOT do

It does not re-open any verdict. No card's `close` changed (except the 19 straggler sessions), no
ledger row is touched, no holdout is read, and **0 trials** are spent — this is bucket D throughout:
it measures the tape and an already-frozen design, and ranks no alternative for adoption. Whether any
card should switch to `atc_close` is a **new design question** and would be a new registration.

---

### A17 — SUBSTRATE MEASUREMENTS DO NOT SPEND TRIALS EITHER (frozen 2026-09-06, operator decision)

#### What was open

§A15 freed the **execution** lane. It was written for fill rate, queue position, latency — measurements
that fix a *cost*. It says nothing about a third category that has since appeared: a measurement of the
**data substrate itself**. Two such rows were recorded 2026-09-05 pending this ruling
(`XF1_fracdiff_dstar_scan_dev_daily`, `XF2_bar_clock_distribution_diagnostic`), flagged in ADR 0036 Q1
as applying §A15's *rationale* to a category §A15 does not name. A parallel session recorded the same
question independently, which is why it needed one ruling rather than two conventions.

#### The rule (frozen)

> **A substrate measurement spends 0 trials, because it produces no performance statistic.**

The test is sharper than §A15's and is the reason this is safe. Deflated Sharpe deflates a **maximum
Sharpe over N trials**. A row that never computes a Sharpe — no signal, no position, no return series,
no P&L — **cannot enter that maximum**, so charging it inflates `SR0` for every card in exchange for
nothing. §A11.1 gives the same answer from the other side: nothing was fixed because of what the data
showed, because nothing was fixed at all.

Qualifying measurements answer *"what is this data like?"*: stationarity and integration order,
distribution shape, autocorrelation, sampling-clock properties, coverage, missingness, boundary
integrity, cross-source parity.

#### Boundaries, identical in spirit to §A15

A substrate row spends **≥1 trial**, retroactively, the moment any of these is true:

1. A **parameter it swept is adopted** into a card's frozen design — a `d`, a `thresh`, a bar
   threshold `V*`. Freezing a value **a priori** is free; adopting a value **because the scan
   preferred it** is a selection, and `n_combos` is then the full pre-declared grid.
2. Its output **selects an entry or exit rule**, or is used to argue a card past its bar.
3. It is re-run on a **different data window in order to change** an existing card's verdict.

And one boundary §A15 did not need, because a substrate scan can look like a search:

4. **The full table must be reported, every cell, including the ones that failed to compute.** A sweep
   that publishes only its best cell has selected, whatever it calls itself. `scan_d` and
   `threshold_sensitivity` return the complete grid by construction and have no "winner only" mode;
   that is the enforcement, not a convention.

#### Why this direction, stated plainly

The measured selection budget is already **zero** — at N=298 the MinBTL exceeds the dev history and
`SR0` exceeds the best Sharpe the programme has ever recorded. Against that, charging substrate
measurements would move `N` 298 → 338 and MinBTL barely at all. So the rule buys almost no statistical
protection and costs the one behaviour the programme most needs: **measuring the ground it stands on
before building on it.** The 2026-09-02 operator intent recorded in §A15 applies unchanged —
*measuring is cheap, selecting is not.*

The asymmetry survives: §A1 (promotion bar), §A11 (selective budget) and §A12 (holdout) are untouched,
and a substrate study still reads **DEV only** through a guard that raises.

#### Retroactive application

`XF1` and `XF2` are **confirmed at 0 trials**; `selects_alpha: false` stands on both and `N` remains
**298**. ADR 0036 Q1 is closed by this clause, which is the authority. `trials_ledger.validate_ledger`
already refuses a `selects_alpha: false` row that gives no reason, so the exclusion cannot be silent.

---

### A18 — THE PROMOTION BAR IS RETIRED AS A GATE; DEPLOY-SMALL-AND-MEASURE-FORWARD REPLACES IT (frozen 2026-09-06, operator decision)

#### What was open, and the measurement that forced it

§A8 made `DSR >= 0.95` plus the power gate the condition for promotion. The deepening programme
(2026-09-03, 17 dossiers) measured what that condition actually requires:

| quantity | value |
|---|---|
| Sharpe/day required for `DSR >= 0.95` | **0.07999** |
| best Sharpe/day the programme has EVER measured | **0.027** |
| that best, expressed as DSR | **0.3072** = **32% of the bar** |
| effect of 50 more trials on `MinBTL` | **+0.018 years** |

The consequence is not "the bar is strict". It is that **no card can pass at any N**, and no amount of
additional evidence changes that, because the shortfall is in the *effect size the venue offers*, not
in the sample. `dsr.py` was being asked a question whose answer was fixed before any card was written.

A gate that cannot be passed is not a decision rule. It is a refusal to decide, wearing the costume of
rigour. The programme has run 43 cards and ~300 trials to a verdict of zero, and the last of those
verdicts was determined by arithmetic that predates the first card.

**Second reason, from the same programme:** every card's verdict turned out to be a function of an
execution-cost assumption that had never been measured. Measured on 5.77M prints, taker slip is
**0.2007 pt/fill**, not the 0.1 the `REALISTIC` book assumed, and E1's published 0.095 does not
reproduce. Cards flipped sign on that one input alone (V11 at 0.1618, V12 at -0.0378, i.e. both already
negative at real cost; C7 at 0.4958, i.e. 0.0042 from the bar). **The binding uncertainty is the venue,
and the venue is only measurable by trading it.**

#### The rule (frozen)

> **The gate for putting one lot on a paper or demo book is the two-tier gate that already exists in
> this file, not the promotion bar. `DSR`, `MinBTL` and `N_required` are RETAINED and still computed,
> but as REPORTING, never as PERMISSION.**

No new machinery. The "Frozen sample sizes & kill thresholds (two-tier gate)" section above already
specifies Tier P (paper, >= 20 sessions, demote on clearly-negative forward expectancy or capture
< 40%) and Tier L (live capital, >= 40 sessions, capture >= 60%), plus the parity guard (forward trade
count deviating > 30% => HALT). That gate was written 2026-08-16 and then subordinated to a bar nothing
could clear. This amendment inverts the precedence.

#### What does NOT change — read this before treating A18 as a licence

1. **Trial accounting stands (§A11).** Deploying a card does not make selection free. Choosing a
   configuration still costs a trial, `FAMILY-IS-NOISE` is still the expected FDVS outcome, and
   §A11.2 R-CONV still converts a look into a costed trial retroactively.
2. **The holdout lock stands (§A12).** Deployment is not a reason to peek, and forward P&L is not a
   substitute for the reserved range. The two unspent claims (mean-reversion, overnight) stay unspent
   until their own conditions are met.
3. **The twin rule stands (§A11.7).** An exit's or a filter's value is still `E[variant] - E[twin]`.
   Forward data does not exempt anything from its control.
4. **The card must be frozen and hashed BEFORE deployment**, exactly as before. Deploying an unfrozen
   design is how a forward record becomes unreadable.
5. **`WORSE_SIDE` remains the reporting bar.** The deployment *decision* may use measured cost; the
   published number stays on the conservative book so it remains comparable to every prior card.

#### The bound that replaces statistical confidence

The old frame bounded error with confidence. At one lot, confidence is unobtainable — S7 needs 212
round trips (5.68 years) for a statistical verdict, and it is the *best-powered* card in the corpus.
So the new frame bounds error with **capital at risk and a pre-registered stop**, which is obtainable
immediately:

| bound | value |
|---|---|
| Size | **1 lot.** The venue minimum. There is no smaller unit, so this is a floor, not a choice. |
| Concurrent deployments | **at most 2**, so a failure is attributable to a card rather than to a book |
| Kill line | **pre-registered per card, in points, BEFORE the first order**, plus a maximum session count. Reaching either ends the deployment. No extension, no "one more month". |
| Mandatory halt | the existing parity guard: forward trade count deviating > 30% from backtest |

A deployment without a written kill line and a session cap is a **breach**, handled as the X1 holdout
burn was: the record is reclassified HYPOTHESIS-GENERATION ONLY.

#### What forward evidence can and cannot claim

This is the clause most likely to be violated in six months, so it is stated flatly.

**CAN claim:** realised fill quality against the 0.2007 measurement · cadence parity against backtest ·
whether the card survives contact with the venue at all (order types, rejections, auction behaviour,
roll boundaries) · whether the implementation and the card are the same artefact.

**CANNOT claim:** that an edge exists. At one lot and a Tier-P sample, forward P&L is **not** evidence
of expectancy in either direction, and a positive forward run is **not** a promotion argument. Anyone
quoting forward P&L as edge is quoting noise with a timestamp. The purpose of deploying is to measure
the **venue**, not to audition the card.

#### Boundaries, so A18 does not become the loophole A8 was the overcorrection for

A deployment converts into a **costed trial**, or must stop, the moment any of these is true:

1. A parameter is changed **because of what the forward record showed** (that is §A11.1, and the grid
   is the full space considered).
2. A second variant of the same card is deployed **alongside or after** the first in order to compare
   them. Comparing two deployments is a selection over N=2 and is charged as such.
3. Forward P&L is cited to argue a card past any gate, including this one.
4. The kill line is reached and the deployment continues.

#### What this does not authorise

**A18 sets the frame. It does not start anything.** Per §A6, no agent may self-authorise a paper or
demo start, and that is unchanged. Each first order still needs an explicit operator go, recorded in
`decision_log/` with the card's frozen hash, its kill line, and its session cap.

#### Consequence for the open item this closes

The "Unresolved" entry stating that A8's promotion-bar rule *"is stated but its consequence is
unfunded"* is closed by this amendment: the consequence was that the rule funded nothing, and it is
now retired as a gate. The `UNDERPOWERED-OBSERVATIONAL` labels stay accurate as descriptions of
statistical power; they are no longer statements about eligibility.

---

### A19 — AT THIS STAGE A GATE MAY PARK A CARD, NOT BURY IT (frozen 2026-09-06, operator decision)

#### What was open

§A18 retired the *promotion* bar. It said nothing about the *rejection* gates, and those had been doing
the heavier work: of 43 cards, ~30 carry a terminal KILL. The operator's instruction is to stop losing
cards that could still be developed.

The instruction is right, and the reason is visible in the kill list itself. Several deaths rest on
inputs the corpus marks `(est.)` rather than measured, and the deepening programme then showed how
badly an unmeasured input can read: taker slip was assumed 0.1 and measured **0.2007**; S8's break-even
curve was published at 0.42985 and is **0.4171**; V12's exit-convention exposure was estimated at
**41% of gross** and measured at **0.6%**. A gate fed by an estimate is not a gate, it is a guess with
a threshold attached.

#### The rule (frozen)

> **At this development stage the default verdict is `PARKED-REOPENABLE`, not `KILLED`. A terminal kill
> requires MEASURED inputs and a written reopen condition. No card may be closed on estimates alone.**

Three consequences, each aimed at a specific way cards were being lost:

1. **`KILLED-AT-DEFINITION` on `(est.)` arithmetic downgrades to `PARKED-REOPENABLE`.** The pre-screen
   still runs and still publishes its number; it just no longer closes the file. The reopen condition
   is the measurement the estimate stood in for.
2. **`FAMILY-IS-NOISE` does not close a card.** It is a statement about the *variant surface*, and
   §A11.3 already scopes it that way: `F_mean` is a functional of the grid, not a verdict on the frozen
   cell. A card whose family is noise but whose frozen cell is intact stays `PARKED-REOPENABLE`, with
   the standing prohibition of §A11.6 (no re-running the family with a narrower grid) untouched.
3. **Every kill, old or new, must carry a reopen condition** — the specific measurement, data window,
   or venue fact that would make it live again. A kill without one is a `PARKED-REOPENABLE` that has
   not been written up yet.

#### What is NOT relaxed

This amendment widens what stays open. It widens nothing about what may be **claimed**.

- **Holdout lock (§A12) is untouched.** Keeping a card open is not a reason to look at reserved data.
- **Trial accounting (§A11) is untouched.** A parked card that is re-examined and then re-specified is
  bucket R and costs a new card; R-CONV (§A11.2) still applies retroactively.
- **The twin rule (§A11.7) is untouched**, and after SG1 it explicitly covers filters as well as exits.
- **Cost is still stated at `WORSE_SIDE` for reporting**, and a card that is negative at *measured*
  cost is still negative — `PARKED-REOPENABLE` is not a licence to describe it as promising.
- **§A18's deployment bounds are untouched.** More open cards does **not** mean more deployments: the
  ceiling stays **2 concurrent**, and the queue is an operator decision.

#### The failure mode this creates, named in advance

Parking everything makes the corpus a graveyard that pretends to be a nursery. The guard against that
is the reopen condition: a card with no writable reopen condition is genuinely dead and should say so.
If the reopen condition is *"more data"* and the data does not exist and is not coming, that is also
dead. **`PARKED-REOPENABLE` obliges someone to name what would change their mind; a card nobody can
answer that for is closed.**

---

### Change log (continued)

- 2026-09-06 — **A19** (operator ruling, same day as A18). At this stage a gate **parks** rather than
  buries: default verdict `PARKED-REOPENABLE`, terminal kills need **measured** inputs plus a written
  reopen condition, `KILLED-AT-DEFINITION` on `(est.)` arithmetic downgrades, and `FAMILY-IS-NOISE`
  stops closing cards because it describes the variant surface and not the frozen cell. Holdout, trial
  accounting, the twin rule, cost reporting and A18's 2-deployment ceiling are all unchanged.
- 2026-09-06 — **A18** (deepening programme close-out; operator ruling). The **promotion bar is retired
  as a gate**: the existing two-tier gate becomes the operating gate, `DSR`/`MinBTL`/`N_required` stay
  as reporting. Forced by the measurement that `DSR >= 0.95` needs Sharpe 0.07999/day while the
  programme's measured best is 0.027 (32% of the bar), so nothing could pass at any N. Error is now
  bounded by **1 lot, <= 2 concurrent deployments, and a pre-registered kill line**, not by confidence.
  Trial accounting, the holdout lock, the twin rule and the freeze-before-deploy requirement all stand.
  Closes the "A8's consequence is unfunded" entry under Unresolved.
- 2026-09-06 — **A17** (PH-DATA-002b, standalone transform studies; operator ruling). A **substrate
  measurement spends 0 trials**, extending §A15's rationale from the execution lane to measurements of
  the data itself. The test is sharper than §A15's: DSR deflates a maximum **Sharpe**, and these rows
  produce no performance statistic at all — no signal, no return series, no P&L — so they cannot enter
  that maximum. `XF1`/`XF2` confirmed at 0 trials, `N` stays **298**. Closes ADR 0036 Q1, which two
  parallel sessions had both flagged. A fourth boundary is added beyond §A15's three, because a
  substrate scan can *look* like a search: **the full table must be reported, every cell** — enforced
  in code (`scan_d`, `threshold_sensitivity` have no winner-only mode), not by convention. Adopting a
  swept value (`d`, `thresh`, `V*`) into a card's frozen design converts the row retroactively;
  freezing a value a priori does not.

- 2026-09-02 — **A16** (same programme, later the same day). The session has **two closes** and the lab
  has been using both without noticing: the daily lane takes the 14:45 batch cross, the profile lane
  takes the 14:30 continuous close, and over 1,507 dev sessions they differ by a **median 1.50pt**,
  exceed the cost bar on **53.4%**, and coincide on **5.0%** — a gap an order of magnitude larger than
  any effect the programme has measured. `mp_profile` now separates continuous trading (to 14:31), the
  auction's order-collection window (14:31-14:45, no prints), and the cross (14:45, median 4,997
  contracts vs 102 at 14:30). `SessionProfile.close` is unchanged; `atc_close` is new. Old-vs-new over
  the dev tape moves **23 of 1,537 sessions (1.50%)**, all of them sessions with a straggler print in
  the collection window. Also corrects an audit claim made earlier the same day that bracket A3 "was
  the auction" — it was not, and the V-family's execution prices were never the ATC print. 0 trials.
- 2026-09-02 — **A13/A14/A15** (PH-STRAT-006, MP × orderflow audit; operator rulings of the same day).
  **A13:** registers the power-input provenance clause that `forward_registry.py` had already been
  citing at five sites since 2026-09-01 while no such clause existed — N1's declared +0.151/6.9/48.2
  corrected to the measured **−0.153646 / 8.465032 / 50.78** (δ ≤ 0 ⇒ `N_required` UNDEFINED), and X1
  declared **UNVERIFIED** because its δ is §A3 hypothesis-generation-only and its σ was never measured.
  **A14:** withdraws §A9's claim that C7 and N1 share one factor — C7 enters at the D+1 open so the
  overnight leg is 0% of its P&L, the two take opposite sides on 94.7% of 113 shared days, and their
  daily P&L correlates **+0.10**. What they share is a failure MODE (both negative in R units despite
  near-orthogonality), which is evidence about the venue rather than about either mechanism; the
  corresponding `Unresolved` bullet is struck. **A15:** an E-series execution probe spends **0 trials**
  (operator decision: loosen the execution lane as far as is defensible while the programme is still
  measuring), with three named boundaries so it cannot become a loophole; §A1, §A11 and §A12 are
  deliberately NOT loosened. No forward row, ledger or verdict changes under any of the three.
- 2026-09-01 — A10/A11/A12 (PH-STRAT-005, strategy deepening). **A10:** break-even slippage is a
  function of price, not a scalar; the engine's "gross" is post-slippage and `net = gross − fee`
  (verified against A2's own figures); restated at 2026 price levels S8's gap to the promotion bar
  widens 4.9× (0.0143 → 0.0701 pt/fill) and **C7 crosses from +0.1385 to ≈0 / fractionally negative**,
  an independent second reason to close C7 alongside A9's constant-risk finding. **A11:** the
  selective trial budget is measured at **zero** (N=297, MinBTL 8.37 yr vs 6.10 yr of dev,
  SR0 = 0.0394 > the best Sharpe ever measured, 0.027); the marginal-deflation argument is retired
  as the reason for rationing and replaced by a procedural rule (bucket D/S/R + R-CONV + FDVS +
  the direction-randomised twin rule for exit research + `N_eff` frozen out). **A12:** holdout rules
  for the deepening phase; the reserved range is already spent for trend and basis, so most
  deepening targets have no holdout regardless of authorisation. Registered before the first
  deepening notebook was written. Forward record unchanged.
- 2026-08-25 — PH-STRAT-004 amendment: dual-book cost scoring (A1); S8 registered but not promotable
  after re-score on the rebuilt binary (A2); X1 historical numbers reclassified hypothesis-generation
  and the holdout burn recorded (A3); power gate with `UNDERPOWERED-OBSERVATIONAL` label (A4);
  family-wise α allocation + O'Brien-Fleming interim boundaries + peek suppression (A5). Registered
  while the forward record contained exactly one row.
- 2026-08-25 (later, same day) — A8: A4's `ACCRUING` labels for S8 and X1 contradicted A4's own
  ">3 years" rule; rule applied as written, both relabelled `UNDERPOWERED-OBSERVATIONAL`. σ for S8
  re-measured on the 1,205 re-scored round trips (12.56, not the ~8 estimate) → N 1,639 and 8.4 yr,
  not ~1,108 and ~6.5. New rule: `N_required` is published at the **promotion** bar, and δ≤0 there
  means `UNDEFINED` + automatic `UNDERPOWERED-OBSERVATIONAL`. Sourced from the end-to-end S8 notebook
  whose Python replica is trade-for-trade identical to the Rust engine. Still exactly one forward row
  at the time of registration.
- 2026-08-31 — A9: C7's screen figures shown to belong to a different design (fixed whole-sample
  threshold + close-to-close + gross + a window including the locked holdout); `CardSpec('C7')`
  power inputs corrected to the promotion-bar measurement per the A8 rule (0.24/20.0/48.2 →
  0.1385/15.99/55.0, i.e. ~42,900/~890yr → 82,373/~1,498yr); A4's description of C7's mean-vs-median
  defect corrected in direction (median is the negative one); and A4's demanded replacement card
  **closed by a finding** — constant-risk accounting inverts δ to −0.0247 R/trade with both
  directions negative, so the only principled variance-reduction route does not exist here.
  Recommendation to CLOSE C7 recorded; the decision is the operator's. Sourced from the C7
  end-to-end notebook, 1:1 parity-checked against the forward recorder. Still exactly one forward
  row at the time of registration.

## Unresolved

- Per-regime forward-gap label frequency (daily/weekly/calendar) for stable splits
  at ~20–30 trades/mo — to fix before the first checkpoint.
- ~~C7 replacement card with a mean-based criterion (A4) — needs registering before any C7 verdict
  claim.~~ **CLOSED 2026-08-31 by A9, with a finding rather than a card:** the only principled
  variance-reduction route (constant-risk sizing) inverts δ to −0.0247 R/trade with both directions
  negative, so no replacement design is supported by the evidence. What remains open is the operator
  decision A9 recommends: **close C7**, or keep it open explicitly as a log.
- Whether `book_v1`'s risk-parity weights survive a constant-risk, net-of-cost re-derivation of the
  C7 reversal sleeve (currently the largest weight, 0.48, on a no-fee points-per-contract MTM
  series). A9/TN-E.13 says the sleeve is negative in R units; PF4/PF5 are blocked on this.
- ~~Whether N1's own forward accrual should be re-scoped now that C7's asymmetry is shown to BE the
  N1 factor. Two cards measuring one premium is not double evidence; it may be double counting.~~
  **STRUCK 2026-09-02 by A14 — the premise is false.** C7 enters at the D+1 open, so the overnight leg
  contributes 0% of its P&L; on 113 shared decision days the two cards take opposite sides 94.7% of the
  time and their daily P&L correlates +0.10. They are near-orthogonal, not redundant. If N1 is
  re-scoped it is for its own δ ≤ 0 (§A13), not for double counting.
- Whether the 3-year years-to-verdict threshold in A4 is the operator's actual horizon. **Sharper
  after A8:** applying the rule as written relabels S8 and X1, which is most of the live forward book.
  If 3 years is not the real horizon, the fix is to re-register the threshold with a stated reason —
  not to keep labelling cards against the rule.
- ~~A8's promotion-bar power rule is stated but its consequence is unfunded: S8's only route to a verdict
  is an execution-quality **measurement** against 0.4858 pt/fill, and no one is currently assigned to
  take it.~~ **CLOSED 2026-09-06 by A18, on both halves.** (a) The rule funded nothing because nothing
  could pass it, so it is retired as a gate. (b) The measurement was taken: taker slip is **0.2007
  pt/fill** on 5.77M prints, and S8's own threshold is **0.4171** at 2026 prices, not 0.4858 — the
  scalar was right but the price curve was not. S8 therefore still has execution headroom; what it
  lacks is a route to significance, which A18 says is not the gate any more.
