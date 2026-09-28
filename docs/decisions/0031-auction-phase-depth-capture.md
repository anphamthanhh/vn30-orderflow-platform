# ADR 0031 — Call-auction depth: ATC was always there, ATO was never captured, neither was labelled

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** **NOT APPLIED** · **Verified:** 2026-09-05 · **Verify:** the ingest service's log on the first session after deployment — a `02:00:00 UTC` start stamp means 09:00 ICT, i.e. the 08:45 collection window is NOT deployed (host check, not distributed)


**Status:** Accepted · **Date:** 2026-09-02 · **Workstream:** PH-DATA / P02 (execution realism)
**Extends:** ADR 0026 (L2 depth archive retention) — same archive, same probe-and-fallback pattern
**Related:** ADR 0021 (ClickHouse canonical schema), ADR 0015 (opportunistic OI capture),
`native_platform/apps/gateway-server/src/auto_trade/consumer.rs` (boundary source of truth)

## Deployment status measured 2026-09-05

`COLLECTION_OPEN = dt_time(8, 45)` is in the repo at `api/market_calendar.py:89` and has been since
2026-09-02. It is **not on the VM**, and the ATO gap this ADR exists to close is therefore still open.

Measured, two independent ways:

- The ingest service's log for 2026-09-03 shows `01:58:50 UTC Market is CLOSED. Waiting
  0.02 hours` followed by `02:00:00 UTC Market is OPEN - Starting collection`. 02:00 UTC is 09:00
  ICT. A build carrying this ADR would have started at 01:45 UTC.
- A search of the deployed Python tree for `ingest_seq` returns nothing, so the deployed
  tree also predates ADR 0032's producer side. Same deploy gap, two ADRs.

This resolves an open question that had been recorded as ambiguous ("service not deployed, or venue
not publishing?"): it is **the deploy**, not the venue. The single ATO-window row per session, always
stamped 08:59:59, is what a 09:00 collection start looks like from the outside.

The VM tree is not a git checkout and its `data_provider/` was last written 2026-08-17, so the gap is
wider than these two ADRs. `scripts/deploy/preflight.py` passes on the repo tree as of 2026-09-05
(77 modules, 3 interface contracts, exit 0), so the code side is ready; what is missing is the deploy
itself, which needs an operator with access to the database host.

## Context

The operator's premise was: trades and ticks are fully stored, but there is *no mechanism
describing how the order book is stored during the call-auction phases*, and that gap blocks the
research question **"does auction order imbalance predict the close / the overnight move?"**

The premise turned out to be half right, and the two halves have different causes. Measured on
`<clickhouse-host>` ClickHouse (`default`, all figures `symbol = 'VN30F1M'`; VN100F1M is present in
`vn30f_orderbook` — 100,219 rows in 30 days — and contaminates any unfiltered query).

### Rows per session phase per day, `default.vn30f_orderbook`

Phase windows are ICT seconds-of-day per `consumer.rs`: ATO `[08:45, 09:00)`, continuous AM
`[09:00, 11:30)`, lunch `[11:30, 13:00)`, continuous PM `[13:00, 14:30)`, ATC `[14:30, 14:45)`.

| Day | ATO | Cont. AM | Lunch | Cont. PM | **ATC** | Outside | Total |
|---|---|---|---|---|---|---|---|
| 2026-08-10 | 1 | 134,762 | 0 | 79,853 | **1,305** | 1 | 215,922 |
| 2026-08-11 | 1 | 126,200 | 0 | 84,523 | **1,196** | 1 | 211,921 |
| 2026-08-12 | 1 | 238,668 | 0 | 126,429 | **1,382** | 1 | 366,481 |
| 2026-08-13 | 1 | 155,473 | 0 | 152,595 | **1,572** | 1 | 309,642 |
| 2026-08-14 | 0 | 242,435 | 0 | 194,110 | **1,442** | 1 | 437,988 |
| 2026-08-17 | 1 | 150,686 | 0 | 134,941 | **1,225** | 1 | 286,854 |
| 2026-08-18 | 1 | 196,719 | 0 | 148,494 | **1,272** | 1 | 346,487 |
| 2026-08-19 | 1 | 182,010 | 0 | 134,302 | **1,259** | 1 | 317,573 |
| 2026-08-20 | 1 | 194,247 | 0 | 123,314 | **703** | 7 | 318,272 |
| 2026-08-21 | 0 | 0 | 0 | 71,953 | **1,519** | 1 | 73,473 |
| 2026-08-24 | 1 | 207,504 | 0 | 131,418 | **1,116** | 1 | 340,040 |
| 2026-08-25 | 1 | 198,624 | 0 | 167,053 | **1,210** | 1 | 366,889 |
| 2026-08-26 | 1 | 209,215 | 0 | 164,569 | **1,448** | 1 | 375,234 |
| 2026-08-27 | 1 | 190,754 | 0 | 147,997 | **1,285** | 1 | 340,038 |
| 2026-08-28 | 1 | 163,932 | 0 | 162,718 | **1,530** | 2 | 328,183 |

(2026-08-21 lost its morning to an unrelated outage. Table max is 2026-08-28: the National Day
holiday closed 09-01/09-02.)

### Same phases, `default.vn30f_trades` and `default.dom_snapshots`

| Table | ATO | Cont. AM | Lunch | Cont. PM | ATC | Outside |
|---|---|---|---|---|---|---|
| `vn30f_trades` (2026-08-27) | 0 | 50,398 | 0 | 38,509 | **0** | 1,406 |
| `dom_snapshots` (2026-08-27) | 0 | 13,587 | 0 | 5,099 | **713** | 64,028 |

Three findings fall straight out of those two rows and are load-bearing:

1. **ATC depth is fully captured and always has been.** ~700–1,600 book updates per session
   inside `[14:30, 14:45)`, 10 levels a side, two-sided, changing throughout: on 2026-08-27,
   1,285 rows carrying 900 *distinct* books, with `total_bid_qty` moving 3,122 → 8,509 and
   `total_offer_qty` 3,576 → 7,948 across the window. The venue publishes a continuously
   updating auction book, not a single indicative print, and no level is empty or null. Nothing
   was missing here except the ability to *find* it.

2. **ATO depth has never been captured, at all, in the table's entire life.** Across
   2026-05-04 → 2026-08-28 the `[08:45, 09:00)` window holds **74 rows over 73 distinct days**,
   and every one of them is stamped `08:59:59.000`. That is not auction depth; it is one
   last-pre-open snapshot per day, delivered as the first message after the subscriber connects.
   The `0`/`1` in the ATO column above is that single row, and 2026-08-14 is a day it missed.

3. **The `vn30f_trades` "outside" column is the ATC uncross.** All 1,406 of 2026-08-27's
   out-of-window trades are stamped **exactly `14:45:00.000`** — the venue prints the whole
   auction match as one batch on the closing second. Trades in `[14:30, 14:45)` are genuinely
   zero, which is correct for a call auction: orders accumulate, nothing executes until the
   uncross.

### Diagnosis, with evidence

**(a) The ATO gap is an ingestion gate, not a venue limitation.**
`data_provider/api/market_calendar.py:33` defined `MARKET_OPEN = dt_time(9, 0)`, and
`is_market_hours` (`market_calendar.py:59`) gated on it. `ServiceScheduler` wrapped it
(`data_provider/service/manager.py:69-73`), and `DNSETickServiceManager.run_scheduled`
(`manager.py:372-384`) only calls `start_collection()` while that returns true, sleeping until
`time_until_market_open()` otherwise. So the MQTT subscribers were not connected during
08:45–09:00 and could not record a book that may well have been published. There is **no session
filter anywhere below that point** — `subscribers/top_price_subscriber.py:17-53` processes and
forwards whatever arrives, and `service/market_sinks.py:312` enqueues it unconditionally. The
uniform `08:59:59` stamp is the signature of a connection opening at 09:00, not of a quiet venue.

**(b) The labelling gap is a schema gap.** Neither depth table carried any session-phase
information: `vn30f_orderbook` is
`(symbol, event_timestamp, total_bid_qty, total_offer_qty, bid_prices, bid_volumes, offer_prices,
offer_volumes, system_time)` and `dom_snapshots` is `(symbol, quoted_at, …, snapshot_id)`.
Auction depth and continuous depth were therefore indistinguishable in storage even where both
were present, and every consumer had to re-derive ICT time-of-day arithmetic for itself — the
same class of error that let VN100F1M rows into VN30F1M queries.

**(c) The mechanism that was *supposed* to record the venue's own phase exists and is dead.**
`default.session_state` is created, `BoardEventSubscriber.process_message`
(`data_provider/subscribers/board_event_subscriber.py:43-47`) enqueues into it, and
`ClickHouseWriter.enqueue_session_state` (`market_sinks.py:337`) writes it. The table contains
**zero rows, ever**. Either BOARD_EVENT is not being delivered on the subscribed topic or its
payload carries none of the five field names the extractor tries (`market_sinks.py:343-349`).
Not fixed here — see *Not decided*.

### Two defects found while measuring, neither addressed by this ADR

- **`dom_snapshots` replays a stale book overnight.** 64,028 of 2026-08-27's 83,427 rows (77%)
  are byte-identical copies of the final book, all stamped `14:45:01.000`, `snapshot_id`
  88,645→152,672, `mid_price` 1980.4 throughout. 64,028 seconds ≈ 17h47m — precisely
  14:45 → the next morning. The deployed writer keeps sampling `_latest_quote` at 1 Hz after the
  feed stops. Any density/volume statistic over this table is currently wrong.
- **ADR 0026 is not deployed.** Live `default.dom_snapshots` still has
  `TTL quoted_at + toIntervalDay(90)`, no `local_ts`, `ORDER BY (symbol, quoted_at)`, and is
  sampled at ~1 Hz. Migration 005 has not been applied and the matching build is not running.
  Note also that 005's header says the writer targets database `orderflow`; it does not —
  `manager.py:57` sets `CLICKHOUSE_DATABASE = "default"`, and `orderflow` has no `dom_snapshots`
  table at all.

## Decision

1. **One session-phase classifier on the Python side**, `data_provider/session_phase.py`, pure
   and dependency-free. Phases `preopen` / `ato` / `continuous_am` / `lunch` / `continuous_pm` /
   `atc` / `closed`, named byte-for-byte as `SessionPhase::as_str()` in `consumer.rs`.
2. **`consumer.rs` is the source of truth for the boundaries.** The Python constants are a
   mirror, marked as such in the module. `historical/tests/test_auction_capture.py` parses the
   Rust file and asserts every constant and every phase name matches, so the two cannot drift
   apart silently. A boundary moves in `consumer.rs` first.
3. **Persist the phase with each stored book row** as a `session_phase LowCardinality(String)`
   column on `default.vn30f_orderbook` and `default.dom_snapshots`, derived from the **exchange**
   timestamp, never from local receive time or a wall clock, so a delayed or replayed row keeps
   the phase it belongs to.
4. **Runtime probe with a fallback**, exactly the ADR 0026 §4 pattern. Each writer probes
   `system.columns` once, lazily; when the column is absent it writes the pre-ADR-0031 column
   list unchanged. `session_phase` is deliberately the LAST element of the row tuple so the
   fallback is a truncation and can drop nothing else. A probe that fails for a *transport*
   reason is not cached, so a dead socket cannot pin a writer to the legacy path for the life of
   the process.
5. **Open the ingestion window at the auction, not at continuous trading.** A new
   `COLLECTION_OPEN = 08:45` with `is_collection_hours()` / `time_until_collection_open()`;
   `ServiceScheduler` uses those. `MARKET_OPEN`/`is_market_hours` stay at 09:00 and keep their
   only other caller — `notification/monitor.py`, which picks alert thresholds — so a legitimately
   quiet auction does not trip a "no data for 2 minutes" alert.
6. **`''` means "not recorded", never a real phase.** A row written by a build predating the
   column, or one with no usable timestamp, carries the empty string and must never be read as
   `closed`. Same rule as `local_ts = 0` in ADR 0026 §5.

### Why store a column for something derivable from the timestamp

The phase is a pure function of ICT time-of-day, so a screen could always compute it inline. The
column earns its place for three reasons: it pins the boundaries to what the writer believed at
ingest time, so a later boundary change cannot silently retro-relabel history; it removes the ICT
arithmetic from every consumer; and it is the slot a *venue-reported* phase can occupy once
BOARD_EVENT is fixed. For the same reason the historical backfill in migration 006 is written but
left **commented out** — running it would make derived labels indistinguishable from observed
ones, which is the audit trail the column exists for.

## Consequences

- **The ATC research question is answerable now, on existing history**, without waiting for any
  deployment: ~80 sessions from 2026-05-04, ~700–1,600 book updates per auction, 10 levels a
  side. The closing imbalance is real and varies — final-book
  `(bid−ask)/(bid+ask)` over the last 15 sessions ranges −0.271 (2026-08-13) to +0.265
  (2026-08-21), with 566–1,136 distinct books per auction. The uncross price and size are in
  `vn30f_trades` at `14:45:00.000`. Nothing in the venue's publication blocks the study.
- **The ATO half is answerable only forward, and only after this deploys.** Four months of ATO
  order imbalance do not exist and cannot be reconstructed — the same asymmetry ADR 0026 hit with
  `local_ts`. An ATO study cannot start accumulating until the widened collection window is
  running, and its first usable sample is months away. Whether the venue publishes an ATO book at
  all is still **unverified**: this ADR removes the gate that made the question unanswerable, it
  does not answer it. First evidence arrives on the first session after deployment.
- **`SESSION_CLOSE_SOD = 14:45` is exclusive, so the ATC uncross prints are labelled `closed`.**
  This mirrors `consumer.rs` deliberately rather than "fixing" it: a screen pairing auction
  imbalance with the auction result must select `session_phase = 'atc'` for the book **and**
  include the `14:45:00.000` trade batch explicitly. Written down here because it will otherwise
  be rediscovered as a bug.
- **Ingestion starts 15 minutes earlier each day.** More connected time, marginal extra load, and
  the monitor's thresholds are untouched. If the venue does publish an ATO book, the morning row
  count rises by whatever it publishes.
- **Write volume rises trivially** — one `LowCardinality(String)` per row, seven distinct values.
- **Nothing currently captured can be lost.** The fallback path writes byte-identically to
  today's; the window only widens; no sampling, batching, retry or queue behaviour is touched.
  This is asserted by test, not just by inspection
  (`data_provider/tests/test_session_phase_capture.py`).

## Explicitly NOT decided here

- **Why `default.session_state` is empty.** BOARD_EVENT delivery/payload is not diagnosed and not
  fixed. When it is, the venue's own label is authoritative over this clock-derived one, and this
  column becomes the comparison baseline rather than the answer.
- **The `dom_snapshots` overnight replay bug** (77% of that table's rows). Reported, not fixed;
  it lives in the deployed pre-ADR-0026 writer, and the ADR 0026 build supersedes that code path.
  Until 005 + the matching build are deployed, prefer `vn30f_orderbook` for any depth study.
- **Deploying ADR 0026.** Migration 005 and its build remain the operator's call; this ADR only
  records that they are not live.
- **Any hypothesis, screen, or promotion claim about auction imbalance.** This ADR changes what
  is written down and what can be selected. It is not a result, not a pre-registration, and
  consumes no selection budget (ADR 0030).
- **The historical backfill.** Written into migration 006 and left commented out; running it is
  an operator decision.
- **Backfilling `session_phase` into SQL Server's `tick_data_top_price`.** ClickHouse is the
  depth archive of record (ADR 0021); the SQL Server side is untouched.

## Applying it

**Do not let a build that expects the column reach production before the migration.** The probe
makes the reverse order degrade rather than fail, but the ordering below is the intended one.

The order is: apply migration `006_session_phase_on_depth.sql` (additive and idempotent), verify
that `session_phase` exists on both tables through `system.columns`, then deploy the ingest service
and count rows per `session_phase` on the next session. (The host-specific commands of the private
deployment are not part of this edition.)

Migration 005 (ADR 0026) must be applied **before** 006 for `dom_snapshots`; 006 is independent of
it for `vn30f_orderbook`. Rollback is `ALTER TABLE … DROP COLUMN IF EXISTS session_phase` on both
tables, which loses labels only and touches no row.

Success criterion, first session after deployment: a non-zero `atc` bucket (expected ~700–1,600),
and an `ato` bucket that is either non-zero — the venue does publish an ATO book, and the study is
unblocked — or still ~1, which would settle the opposite way and is itself the answer.

## References

- `data_provider/session_phase.py` — the classifier (mirror of `consumer.rs`)
- `data_provider/api/market_calendar.py` — `COLLECTION_OPEN`, `is_collection_hours`
- `data_provider/service/manager.py` — `ServiceScheduler`
- `data_provider/service/market_sinks.py` — `vn30f_orderbook` sink + probe
- `data_provider/orderflow/clickhouse_dom_writer.py`, `data_provider/orderflow/dom_schema.py`
- `data_provider/orderflow/sql/migrations/006_session_phase_on_depth.sql`
- `historical/tests/test_auction_capture.py`, `data_provider/tests/test_session_phase_capture.py`
- `native_platform/apps/gateway-server/src/auto_trade/consumer.rs` (lines 54–161) — source of truth
- ADR 0026 (probe-and-fallback pattern, `local_ts`), ADR 0021 (canonical schema), ADR 0030
