# ADR 0032 — Four-layer market-data standard, and the two columns that make it possible

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** **PARTIALLY APPLIED** (decisions 2 and 7 live since 2026-09-05; 1/L2, 3, 4, 5, 6, 6b outstanding) · **Verified:** 2026-09-05 · **Verify:** `curl -s http://<clickhouse-host>:8123/ --data-binary "SELECT filename FROM default.schema_migrations WHERE filename IN ('007_schema_migrations_ledger.sql','008_l0_ingest_provenance.sql') FORMAT TSV"` — two rows means the ledger and the provenance columns are in place


**Status:** Accepted (operator, 2026-09-04) · **Applied:** PARTIAL — decisions 2 + 7 on 2026-09-05; see "Decision vs deployment" · **Date:** 2026-09-04 · **Workstream:** PH-DATA-001
**Relates:** 0007 (engine→viewmodel, no persistence tier), 0017 (§3 funds retention), [0021](0021-clickhouse-canonical-schema.md) (ClickHouse canonical + schema standard), 0026 (L2 retention — **decision 1 never applied**), [0031](0031-auction-phase-depth-capture.md)

## Context

Every backtest is supposed to read one standardised frame. It does not. A read-from-code audit plus
**23 read-only queries against the live cluster on 2026-09-04** found that the storage layer is not
short of capacity or of feeds — it is short of *identity* and of a *derived tier*.

**The structural cause is one measurable fact:** `grep -rni "insert into" --include=*.rs
native_platform/` returns zero hits. No Rust crate writes to ClickHouse. ADR 0007 designed the
pipeline as `engine → viewmodel → wire → client`, and a persistence tier was never part of it. So
every market observation the gateway computes — CVD, VPIN, order-flow imbalance, TPO value areas,
DOM state, and 23 signal triggers — has exactly one sink: a JSON frame to a desktop client. With no
client attached it is computed and discarded. Eight of those signals (S16–S23) are recorded in
`docs/academic/trigger-evidence-matrix.md` as `validation: L` — literature-only, never backtested —
because there has never been anything to backtest them against.

**The most expensive loss is not a missing feed; it is missing order.** `vn30f_orderbook.event_timestamp`
is declared `DateTime64(3)`, but 0 of 420,987 rows on 2026-09-03 carry a millisecond component, and
14,954 distinct stamps cover those 420,987 rows: **28.2 updates share each second with nothing to
order them**. Every quantity that is path-dependent through the update sequence — sweep, pull/stack,
absorption, queue position, OFI — is permanently coarsened for all history collected so far.

**And the tiebreak is already being produced and thrown away.** `_local_ts_ms` is stamped at
millisecond precision at MQTT arrival (`base_subscriber.py:153`), carried into the Redis quote stream
at index 13 (`market_sinks.py:139`), then dropped: the gateway parses only indices 0–12, and no
ClickHouse column exists to hold it. The column that looks like an ingestion time, `system_time`, is
`DEFAULT now64(3)` — the *insert* time, not the receive time.

Retention is not the constraint either. Measured: order book 5.04 MiB/session (1.23 GiB/yr), trades
269 KiB/session (66 MiB/yr), dom_snapshots 1.46 MiB/session (364 MiB/yr) — **1.65 GiB/year for
everything**, against 12.21 GiB free. Yet `dom_snapshots` still carries `TTL quoted_at + INTERVAL 90
DAY`, which ADR 0026 decided to remove and which was never applied; the table starts at 2026-06-08,
exactly 88 days back, because everything older has been deleted.

## Decision

### 1. Four layers, each with a contract

```
L0  RAW EVENTS      immutable, append-only, as the venue sent it
      key: (symbol, event_time, ingest_seq)
      mandatory: event_time · recv_time · ingest_seq · feed_id
      forbidden: derived columns

L1  CANONICAL BARS  deterministically derived from L0, ONE mechanism
      m1 · m5 · m15 · m30 · h1 · d1 — materialized views or a scheduled job
      forbidden: hand-run scripts, TRUNCATE-and-repopulate

L2  OBSERVATIONS    derived series, stored WITH their version   <- the missing tier
      key: (feature, symbol, event_time, code_version)

L3  DECISIONS       what the live engine actually emitted, and when
      signal firings · gates · vetoes · decisions with reasons
```

### 2. Two columns are mandatory on every L0 table

`recv_time DateTime64(3)` and `ingest_seq UInt64`.

This is the load-bearing clause. The venue's `sendingTime` is second-resolution (ADR 0026), so the
exchange cannot supply intra-second order — the tiebreak has to be ours. `recv_time` already exists
in the process and only needs somewhere to land; `ingest_seq` is a monotonic counter assigned at the
subscriber, with `feed_id` distinguishing sources. Without these two, L2 and L3 are built on a
substrate whose ordering is undefined, and no amount of later work recovers it.

`system_time` is kept but must be *named* what it is: storage time, not receive time. Three
timestamps, not two.

**Constraint found in implementation (2026-09-04), and the resulting deferral.** The layer's key is
written above as `(symbol, event_time, ingest_seq)`, but **that key is not reachable by an additive
migration**. ClickHouse requires a column newly added to the sorting key to be added in the *same*
`ALTER` and *without* a default value
([ALTER ORDER BY](https://clickhouse.com/docs/sql-reference/statements/alter/order-by)), while
`ingest_seq` needs `DEFAULT 0` precisely so that pre-migration rows read as "not recorded" rather
than being back-dated to a fabricated sequence. The combined statement is also not re-runnable.

So `ingest_seq` ships as a **plain column, not part of the sorting key**, and is used as a
query-time tiebreak. Nothing scans by it, so the physical order costs nothing today, and extending a
sort key is close to irreversible — the wrong thing to do quickly. The full key remains the target
and is revisited only if a query pattern actually needs it.

A second, smaller consequence: `ingest_seq` is **per process** and restarts at 1. It orders within a
run, not across a restart. `recv_time` and `feed_id` are the discriminators across that boundary, and
a reader that treats the sequence as globally monotonic will be wrong at every gateway restart.

### 3. An observation carries its own provenance

Every L2 row carries `code_version` (hash of the implementation) and `input_version` (manifest of the
data it consumed). A stored feature without them is worse than recomputing, because it looks like
data while being untraceable.

### 4. One implementation per observation

Either the live engine writes L2 and the backtest reads L2, or both call the same function. Never two
hand-maintained implementations. Today the Rust gateway computes one version, the two-day-old Python
`historical/signal_atlas/` re-derives another, and nothing compares them — the worst of the three
possible arrangements.

### 5. One authority per logical series, enforced by a test

M1 currently has two authorities with two independent writers and no reconciliation: the CSV
(529,326 rows to 2026-08-14) and `default.ohlcv_m1` (603,861 rows to 2026-09-03). ClickHouse is the
authority per ADR 0021; the CSV becomes a checked export. `feed_id` plus a reconciliation test is what
makes "one standardised frame" a property rather than a hope.

### 6. Retention is a named policy with an alarm — on the table that matters

`vn30f_orderbook` is the depth substrate and already has no TTL. Retention on it is unbounded until a
named policy says otherwise, with a disk alarm at **85%**. Deleting at this data scale saves nothing
measurable (1.65 GiB/year for every stream combined), but the disk is 74% used, so "unbounded" needs
monitoring rather than optimism.

**Correction to this ADR's own first draft.** It said "remove the 90-day TTL (ADR 0026 decision 1,
still unapplied)", framing the TTL as ongoing loss of an irreplaceable series. Direct verification on
2026-09-04 shows that is wrong, and the correct action is the opposite of repair:

- Every `dom_snapshots` timestamp exists in `vn30f_orderbook` (0 orphan timestamps on 2026-09-03),
  and all 83,614 rows that session have an exact-match row there — same instant, same bid and offer
  prices, same volumes.
- Across the full table, 6.10M of 6.62M rows match exactly. The 523,632 that do not are concentrated
  in one place: **hour 14 holds 5.63M of 6.62M rows (85%)** across 126,177 distinct timestamps —
  **44.6 rows per timestamp** — and the single minute 14:45 holds 64,017 rows against ~45 in every
  other minute.

So `dom_snapshots` is not an archive being eroded; it is a 5x-sparser duplicate of the raw table plus
a close-of-session write burst. **Removing its TTL to preserve it would have been solving the wrong
problem.** It is retired (decision below), and the TTL becomes moot. What survives from ADR 0026 is
its *procedural* finding — a decision recorded as Accepted and never executed, with nothing able to
tell the difference — which decision 7 addresses.

### 6b. `dom_snapshots` is retired, not repaired

Verified redundant above. Sequence: stop the writer, freeze the table for one retention cycle, then
`DROP`. `vn30f_orderbook` becomes the single depth store. Two archives of one feed was the defect.

### 7. A migration ledger

A `schema_migrations` table in ClickHouse recording what has actually been applied. Applied state is
currently tracked in SQL comments across three overlapping migration directories, one of which
targets a dropped database. **That absence is the mechanism that produced this ADR's worst finding:**
ADR 0026 was accepted, migration 005 was never run, and nothing could tell the difference for three
months.

## Consequences

- (+) The order-book substrate becomes orderable, which is the precondition for queue-position and
  fill-quality modelling — the measurement all three end-to-end cards hinge on.
- (+) Derived observations become backtestable at all. Eight literature-only signals become testable
  rather than permanently unfalsifiable.
- (+) "Which bytes did this backtest read" becomes answerable.
- (−) Schema changes touch live ingestion tables, which is a maintenance-window operation with the
  writer stopped, and `dom_snapshots` cannot be partitioned in place.
- (−) `ingest_seq` fixes ordering **forward only**. History collected before it lands stays coarse,
  and no later work recovers it. That asymmetry is the entire argument for doing it now.
- (−) L2 adds a tier to maintain. Mitigated by keeping it small: two features first (CVD and
  TPO/VA), each with a Rust↔Python parity test, before any third is added.

## Alternatives rejected

- **Recompute everything on read, store no derived series.** Correct for cheap features on stored
  inputs, and it is what the external research recommends at this scale. Rejected as a *complete*
  answer because it cannot record what the live engine actually emitted — a restart, a feed gap, or
  the `"$"`-cursor amnesia leaves no trace, so live-vs-backtest reconciliation stays impossible.
  Adopted for L1, rejected for L3.
- **A feature store (Feast/Tecton/Chronon), Kafka, Iceberg/Delta, DVC.** Solve problems this project
  does not have at one instrument and ~250 sessions/year. Immutable partitions plus a hashed manifest
  give the reproducibility without the operational surface.
- **Fix `dom_snapshots` to match ADR 0026.** Rejected in favour of retiring it: measured at 83,614
  rows against the raw table's 420,987 on the same session, unpartitioned, with no ingestion time,
  and every column derivable from `vn30f_orderbook`. Two archives of one feed is the defect, not the
  fidelity of the second one.
- **Treat this as new data acquisition (blocked by ADR 0017).** It is not. ADR 0017 §3 explicitly
  funds *retention of an existing feed* as the exception. This work retains and identifies what is
  already arriving.

## Decision vs deployment

This ADR is **Accepted and partially Applied**, and keeping the two states distinguishable is the
very defect decision 7 exists to fix, so this record adopts the convention it prescribes.

| Item | State on 2026-09-05 |
|---|---|
| `schema_migrations` ledger (decision 7) | **APPLIED 2026-09-05 15:29 ICT** — migration 007. Ledger backfilled with the five files verified against the live schema; 005 and 006 correctly absent |
| `recv_time` / `ingest_seq` / `feed_id` columns (decision 2) | **APPLIED 2026-09-05 15:30 ICT** — migration 008, six `ADD COLUMN`, metadata-only. Row counts unchanged: 22,134,060 book / 7,083,617 trade |
| `recv_time` / `ingest_seq` / `feed_id` **producer** code | **DEPLOYED 2026-09-06** — release `6a09bab30587` (`DEPLOY_SHA`, `dirty=false`). Verified in the running tree at `current/data_provider/service/market_sinks.py`: `PROVENANCE_COLUMNS` `:37`, a one-shot `system.columns` probe `:27`/`:39` behind `_book_has_provenance` `:391`, and the real values entering the INSERT at `:411` (tick) and `:440` (book). **Not yet demonstrated on live rows** — the market has been closed since the deploy |
| Loader contract (`historical/data`) extended to ticks and book | code landed in the repo |
| One authority per series, enforced by a test (decision 5) | **no such test exists** |
| Retention policy + alarm (decision 6) | **inverted** — the only TTL on the cluster is on `dom_snapshots`, the table being retired; `vn30f_orderbook` (422 MiB, largest, growing) has none |
| `dom_snapshots` retirement (decision 6b) | **not executed** — still written, 6,631,369 rows |
| L2 observation tier (decisions 1, 3, 4) | **does not exist** — no table, no `load_observation` |
| Higher-timeframe bar views | SQL written, **not created** |

**What "applied" bought, updated 2026-09-06.** When this section was first written the producer was
not on the VM, so the columns existed and nothing filled them. That is no longer true: the deploy on
2026-09-06 put the producer in the running tree, and the writer's own `system.columns` probe means it
will select the six-column INSERT the moment it next writes. Measured on 2026-09-06:
`countIf(ingest_seq > 0) = 0` of 694,007 rows since 2026-09-01 — as expected, because the last row
predates the deploy (2026-09-04 13:40:39) and the market has been closed since.

**So the claim is now "deployed but not demonstrated", and those are different states.** The check
that the fix actually landed is still the post-session query at the foot of
`008_l0_ingest_provenance.sql`, run after the first session with the new build — the first trading
session after the deploy. Presence of the columns was never the test; neither is presence of the code.

**Safety evidence for the 2026-09-05 apply.** Saturday, market closed, writer idle. The deployed
writer was confirmed to use explicit column lists (`e027c87` and `c65c667` both list all six trade
columns by name), so added columns with defaults cannot break it; this was then demonstrated rather
than assumed, by cloning the post-008 table and inserting through the old six-column list — the row
landed and the three new columns took their "not recorded" defaults. `system.mutations` stayed empty,
confirming the ALTERs were metadata-only. Pre-change DDL was snapshotted first; post-change DDL is in
`native_platform/deploy/ubuntu-gateway/live-ddl/`.

**An ADR that changes live infrastructure is not finished when it is merged.** The dates above are the
record.

## How to apply it

`docs/runbooks/data-storage-standard-rollout.md` — ordered, with the verification command for each
step and the rollback where one exists.

**Read its §0 first.** The migration ledger this ADR asks for reports every file as NOT APPLIED on a
cluster that has no ledger yet, and five of the nine have in fact already run. Three of those five
(`004`, `004a`, `004b`) are a create-copy-**rename** swap: re-running them against the current schema
would destroy data. So the ledger must be created *and backfilled with history* before anything else
is applied — which is a neat demonstration of why decision 7 exists at all.

## Links

- Audit + design + prioritised roadmap: `plans/260904-0243-market-data-storage-standard/plan.md`
- Corrections this ADR forces on `ARCHITECTURE.md`, ADR 0021, ADR 0026 and `DATA.md`: plan §7
