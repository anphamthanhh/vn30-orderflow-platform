# 0037 — The observation tier: a feature name is a contract, not a label

<!-- deployment-status: v1 - see docs/templates/decision.md -->
**Deployment:** **NOT APPLIED** · **Verified:** 2026-09-05 · **Verify:** `curl -s "http://<clickhouse-host>:8123/?readonly=1" --data-binary "SELECT count() FROM system.tables WHERE database='default' AND name='l2_observations' FORMAT TSV"` — `0` means migration 009 has not run

**Status:** Accepted (operator, 2026-09-05) · **Applied:** NO · **Date:** 2026-09-05 · **Workstream:** PH-DATA-002
**Relates:** [0032](0032-market-data-storage-standard.md) (four-layer standard — this ADR fills its L2 tier), [0021](0021-clickhouse-canonical-schema.md), 0028 (spec as the Python↔Rust boundary — the pattern reused here), 0034 (state is generated, never hand-copied), 0013 (frac-diff rejected — narrowed here, not overturned)

## Context

ADR 0032 named L2 OBSERVATIONS as the missing tier and sketched a nine-column row. A four-branch
audit on 2026-09-05 (`plans/260905-0737-l2-feature-standard/`) tried to build against that sketch and
found the sketch was not the hard part.

**The measured problem is that a feature name does not identify a computation.** Counted from source,
not from documentation: **9 implementations of ATR · 8 of value area · 7 of CVD · 5 of VPIN · 4
different formulas for order-book imbalance** — not four copies of one formula, four formulas.
Against that, exactly **one** Rust↔Python parity contract is enforced by a test
(`shared/quant_core/testdata/value_area_golden.json`). Everything else in the repo that calls itself
"parity" is a comment pointing at a file, a regex scanning Rust *source text*, an assertion against a
frozen JSON dump of one manual run, or a hand-typed vector — and three of those hand-typed vectors do
not match when recomputed (ATR agrees on 2 of 5 values; trailing-stop on 0 of 4).

Two consequences follow, and both are already real:

- `market-profile-engine` returns `(poc, vah, val)` while three other implementations return
  `(poc, val, vah)`. The golden fixture does not catch it because it compares by field name.
- `historical/strategies/trend.py::_atr_loop:130` back-fills its warmup with **future** ATR values,
  `volatility.py` fills 0.0, and Rust returns `None`. Supertrend — which backs live cards — uses the
  look-ahead one, and at least one strategy calls both variants on the same DataFrame.

**Storing features under bare names would make this permanent.** A stored value that looks like data
while being untraceable is worse than recomputing, which is ADR 0032 decision 3; a stored value under
an ambiguous *name* is worse still, because it silently unifies things that were never the same.

A second measured constraint shapes the tier's physical form. `vn30f_orderbook` receives **420,987
updates per session**; at 250 sessions that is **105 million rows per year for a single feature**,
against **12.21 GiB** free on the cluster. Fifty features at M1 cadence cost ~3 million rows/year.
The tier is cheap or ruinous depending entirely on cadence, not on how many features it holds.

## Decision

### 1. The name carries the revision. `cvd` is not a feature name; `tape_cvd_lot_v1` is

Grammar: `<family>_<measure>[_qual]*[_unit]_v<N>`, families closed to five — `ob`, `tape`, `prof`,
`xform`, `vn`. A regex and a token ban-list (tense words: `prev`, `next`, `fwd`, …) are enforced by
`feature_boundary.py`, not by review.

Two values that are **comparable** must share a name; two values computed differently must not, even
when the textbook calls them the same thing. Where the repo already holds incompatible
implementations, the canonical one takes `_v1` and the others take their own names or are deleted —
they do not quietly coexist under one label.

### 2. Parameters live in `param_set_id`, in the sorting key. `meta Map(String,String)` is deleted

A free-form map is where a standard goes to die: nothing stops `w=60` and `w=60.0` becoming two
things. `param_set_id` is a canonical hash over Decimal-normalised parameters. The repo already has
this exact bug in a different place — `robust_optimizer.py:222` hashes raw `json.dumps` output — so
this is a known failure mode being designed out, not a hypothetical.

### 3. `per_update` cadence is forbidden

From the arithmetic above. This is the constraint that shapes the tier, and it is what makes the
long-format scalar row affordable. Features with structure (TPO histograms, DOM matrices) keep only a
**scalar reduction** in L2; the distribution goes to a separate table.

### 4. Python batch is the only writer. Rust never INSERTs

Not an aesthetic preference. `signal_state_logger.rs:187-197` writes its file **synchronously inside
the `state.write()` guard** (`task.rs:157-164`); replacing that with a ClickHouse INSERT would put a
network round-trip inside the trading path's critical section. Rust keeps **authorship of the
definition**; the binding constraint is a parity test, not a shared write path. This preserves the
invariant already established for the state logger: **observation must never be able to block
trading.**

### 5. Parity tolerance is 0 — bitwise — unless a registry entry says otherwise and says why

CVD is a signed sum of integer lots; POC/VAH/VAL are price levels. There is no physical reason for
either to differ between two correct implementations. A non-zero tolerance requires a
`tolerance_reason` field. A feature reaches the tier only after its parity test is green; the test is
the admission gate, not documentation of one.

### 6. Feature state lives in a machine-readable registry, and the status page is generated

`specs/features/*.yaml` → `feature_boundary.py` (fail-closed) → `l2_status_page.py` →
`docs/L2_FEATURES.md`. This copies `specs/*.yaml` / `spec_boundary.py` / `status_page.py` deliberately
rather than inventing a second pattern. ADR 0034 (not distributed) recorded why: `openspec/` was adopted and abandoned
in two weeks because its only machine-readable file carried `schema` and `created` and no `status`.

### 7. Start at two features, and let the third wait

CVD and TPO/VA, each with a bitwise parity test, before any third is added — carried forward from ADR
0032 unchanged.

### 8. Operator decisions recorded (2026-09-05)

| # | Decision | Consequence accepted |
|---|---|---|
| Q1 | **Half the feature budget goes to predicting COST and FILL PROBABILITY, not direction** | The programme's largest measured edge is not an alpha: E1 measured passive − taker at **+0.196..+0.220 pt/entry**, against every directional edge ever measured here (C2 OFI 0.106pt; V3 level-magnet 0.124–0.200pt). Moving the bar from 1.42pt to 0.62pt revives a family of dead cards; finding 0.1pt of alpha does not |
| Q2 | `event_time` stays **naive, following L0** | Avoids creating a third timezone convention. Irreversible in practice — changing it after the first row means rewriting the tier |
| Q3 | Value-area `v1` = `market-profile-engine/src/lib.rs:349-415` | The other three implementations must be deleted or renamed `_v2`/`_v3`. **Touches live code** |
| Q4 | CVD `v1` = reset-open + rollover-13:00 (matches `cvd.rs:101-107`) | The non-resetting variant survives as `tape_cvd_norst_lot_v1` because `cvd_exhaustion` has a backtest standing on it. Its behaviour must not change |
| Q5 | ~1 GiB/year ceiling | This is what forbids `per_update`. A decision to expand the disk is a decision about the shape of this tier |
| Q6 | L2 serves the **8 gateway signals** for now | S1–S23 live in `client-desktop/src/signal_engine/types.rs:8` — **after** the wire — so a gateway-side writer cannot reach them. Moving them into a shared crate is a separate architecture change and gets its own ADR |
| Q7 | The `_atr_loop` look-ahead is **NOT fixed in this workstream** | Deliberate. Changing ATR warmup changes Supertrend's signals, and Supertrend backs S7/S8, both in FORWARD state with frozen designs. This is a research-integrity decision at card level, not a code cleanup. Recorded as owed |
| Q8 | Backfill runs **from the operator's machine over HTTP**, not on the VM | Slower; keeps batch load off the host that runs the live gateway |

## Consequences

- (+) A stored observation becomes traceable to one implementation, one parameter set, one build.
- (+) The admission gate is a test, so the count of "features that agree across stacks" can only go
  up, and is visible on a generated page rather than asserted in prose.
- (+) Naming the incompatible variants apart converts a class of silent wrong answers into either a
  deletion or an explicit second series.
- (−) Q3 forces a change to code that runs live, and Q4 pins a convention that an existing backtest
  depends on. Neither is free, and both were chosen over letting the ambiguity persist.
- (−) Forbidding `per_update` means the tier cannot hold tick-resolution series at all. Anything
  needing them recomputes from L0 — which is the correct answer at this data scale, and is why ADR
  0032 adopted recompute-on-read for L1.
- (−) One more tier to maintain, mitigated by decision 7.
- (−) Q6 means the tier initially serves 8 of 23 signals. Stated so nobody later reads L2 coverage as
  signal coverage.

## Alternatives rejected

- **`values Array(Float64)` or a table per feature.** Rejected once `per_update` was forbidden: with
  cadence bounded, the scalar long row is affordable, and the array form pushes a pivot into every
  reader while defeating `LowCardinality` on the feature column.
- **Bare feature names with the revision in `meta`.** This is the current situation with extra steps —
  a reader who forgets to filter `meta` silently mixes two definitions. The whole defect is that
  mixing is possible without anyone noticing.
- **Rust writes L2 directly.** Rejected on the measured shape of `signal_state_logger.rs` (decision 4).
- **Hashing source files for `code_version`.** Editing a comment would then invalidate all history.
  The build fingerprint (git sha) is forensic; the `_vN` in the name is selective. The repo already
  has the comment-sensitivity defect at `backtest_outputs.py:135`.
- **A feature store (Feast/Tecton/Chronon), Kafka, Iceberg/Delta, DVC.** Re-rejected, same reasoning
  as ADR 0032: they solve problems this project does not have at one instrument.
- **Fixing `_atr_loop` here (Q7).** Correct as code, wrong as process — see Q7 above.

## Decision vs deployment

Nothing in this ADR has run against the cluster. Migration `009_l2_observations.sql` is written and
**not applied**; the registry, the boundary gate and the parity harness are repo-side and gated by
CI. ADR 0032's own migrations 007 and 008 **were** applied on 2026-09-05 15:29–15:30 by a parallel
session — recorded in `default.schema_migrations`, which is the first time in this project's history
that "applied" was answerable by a query rather than by reading a comment. Migration 006
(`session_phase`) remains unapplied as of 2026-09-05.
