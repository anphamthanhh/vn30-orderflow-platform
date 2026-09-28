# ADR 0038 — Non-adoptions: meta-labeling, MDI, and MDA

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (governance — code-free by design; there is nothing to deploy)

**Status:** Accepted · **Date:** 2026-09-06 · **Workstream:** PH-DATA-002b
**Related:** 0013 (the ADR that was said to contain this),
0036 (the same gap, fixed for fracdiff),
`METHODOLOGY.md` Stage-3/Stage-5 (a research-planning memo, not distributed)

## Context

`METHODOLOGY.md` has asserted since 2026-08-13 that three non-adoptions are *"documented in ADR
0013 (code-free)"*: fractional differentiation, meta-labeling, and the MDI ban. **ADR 0013 contains
none of them.** Its own line 280 lists writing that entry as pending item #32, *"write when
touched"* — so a planned governance record was described in prose as already done, and stayed that
way for 24 days.

That is not a filing error. On 2026-09-05 a research report reasoned *from* the unrecorded fracdiff
rejection, treating it as the fixed point a fresh verdict had to respect. An unwritten decision
became load-bearing. ADR 0036 closed the fracdiff third of the gap; this ADR closes the rest, so
that nothing is left in the repo that a future reader can cite as settled without being able to read
it.

The three are recorded together because they share a cause: **N is too small for any of them**, and
each fails at a different point in the same argument.

## Decision

### 1. Meta-labeling — OUT OF SCOPE for this data horizon

A secondary classifier that only ever *removes* trades (López de Prado 2018, ch.3). It is the one
piece of AFML machinery a rule-based researcher would most plausibly borrow, because it does not
invent entries — it sizes or vetoes them.

**Rejected, and the reason is a number, not a preference.** Fitting a classifier needs events. The
programme's cards have 19–324 trades each, the MBL policy in ADR 0013 already demands ≥50
*independent* trades before a net Sharpe is treated as credible, and the three end-to-end cards sit
below or barely at that line.

**The framing matters more than the verdict.** The earlier phrasing — "unlock behind ≥200 events
across ≥3 regimes" — reads as a milestone someone could work toward. It is not one. VN30F1M is a
single instrument; you do not observe three distinct regimes by working harder, you observe them by
waiting quarters to years. Stating a numeric unlock that the data horizon cannot deliver invites a
future session to believe it is close and start fitting.

So the record says **out-of-scope-for-data-horizon**, not "unlocks at N=200". If that ever changes,
it changes because years passed, and the honest trigger is a calendar, not a threshold.

### 2. MDI (mean decrease impurity) — BANNED

In-sample, impurity-based feature importance from a fitted tree ensemble.

**Banned outright, not deferred.** It is computed on the same data the model fitted, it is biased
toward high-cardinality and continuous features regardless of predictive content, and it produces a
confident ranking from pure noise. On this programme's N it would rank noise features and look
respectable doing it. There is no sample size at which this becomes the right tool here, which is
why it gets a ban rather than a park.

### 3. MDA (permutation importance) — DEFERRED, and with a caveat that is not theoretical

Out-of-sample permutation importance is the defensible member of the family, and it stays
**deferred** for the same reason as meta-labeling: at ~21–39 independent observations, no feature
ranking is trustworthy, and a ranking that is not trustworthy is worse than none because it gets
cited.

Two things must travel with the deferral, so that whoever lifts it does not walk into them:

**(a) Unconstrained permutation is known-biased on correlated features.** Hooker & Mentch, *"Please
Stop Permuting Features"* (arXiv:1905.03151; journal version Hooker, Mentch & Zhou, *Statistics and
Computing*, 2021): permuting a feature independently breaks the dependence structure and forces the
model to extrapolate into regions with no data, which **inflates the apparent importance of
correlated features**. For an order-flow feature layer — where OBI, microprice, weighted-mid and
level-1 OFI are near-restatements of one quantity — this is not a theoretical risk, it is the
expected outcome. The remedies the same authors propose are conditional permutation, or
permute-and-relearn (k× the cost, and correct).

**(b) Cluster before counting.** Correlated features are not independent bets. Reporting the number
of *clusters* rather than the number of features is the honest statement of breadth — the same logic
`mechanism_class` already applies at strategy level, where it established that three Tier-1
strategies are one bet.

**The trap in (b), stated because it is easy to fall into:** cluster counts are for *reporting
independence*. They may **never** be used to reduce `N` in the Deflated Sharpe denominator. `N` is
the number of ledger rows, full stop. Using clusters to shrink it would be laundering a trial count.

## Consequences

- (+) The three non-adoptions METHODOLOGY has claimed for 24 days now actually exist as a record,
  so a future session can read the reasoning instead of inferring it from a citation that resolves
  to nothing.
- (+) Meta-labeling's fake numeric unlock is replaced by an honest scope statement, removing an
  invitation to fit a classifier on 21 events.
- (+) The MDA deferral now carries the correlated-feature bias with it, so lifting it later does not
  reintroduce a published failure mode.
- (−) Three techniques stay unavailable, and the binding constraint on all three is data the
  programme cannot manufacture. Nothing here can be worked around; it can only be outlasted.
- (−) This ADR is governance, not code. Nothing enforces it mechanically. The mitigation is that
  METHODOLOGY now links here rather than at ADR 0013, so the citation resolves.

## Alternatives rejected

- **Leave them in METHODOLOGY prose.** That is the status quo that produced the defect: prose said
  "documented", nothing was, and a later report reasoned from the gap.
- **Amend ADR 0013 in place.** ADR 0013 is Accepted and dated 2026-08-13; retrofitting decisions
  into it would make the record claim they were taken then, which is the same falsehood in a tidier
  form.
- **Defer MDI rather than ban it.** A deferral implies a future in which it becomes appropriate.
  For in-sample impurity importance on this substrate there is none.
- **Keep the "≥200 events across ≥3 regimes" unlock for meta-labeling.** It reads as reachable and
  is not; that is precisely how a future session talks itself into fitting one.

## Links

- Defect record: `docs/harness_defects.jsonl`, row dated 2026-09-05 (ADR-0013 misattribution).
- METHODOLOGY Stage-3 (MDA/MDI) and Stage-5 (meta-labeling), both now pointing here.
- Hooker & Mentch, arXiv:1905.03151 → *Statistics and Computing* 31, 82 (2021).
- López de Prado (2018) *Advances in Financial Machine Learning*, ch.3 (meta-labeling), ch.8
  (feature importance); (2020) *Machine Learning for Asset Managers*, clustered importance.
