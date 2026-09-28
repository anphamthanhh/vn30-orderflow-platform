# 0021 — ClickHouse-Canonical Store + Optimized Schema Standard

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** APPLIED (partitioning/codecs on the 3 ingestion tables) — contradicts this ADR's own "deferred to the VM maintenance window" note, which is the stale artefact · **Verified:** 2026-09-04 · **Verify:** `docker exec <clickhouse-container> clickhouse-client -q "SHOW CREATE TABLE default.vn30f_trades"`


## Date

2026-08-17

## Status

accepted

## Context

SQL Server is retired. All market data (ticks, order book, foreign flow, OHLCV,
derived basis/term-structure/OI) is stored in ClickHouse `default` on the VM
(<clickhouse-host>), which is already the de-facto single source of truth — the
data_provider service has run "Redis+ClickHouse only mode" whenever SQL Server was
unreachable. Before ingesting the new multi-symbol feeds (VN100F1M, VN30F2M) the
schema needed to be made canonical and storage/retrieval optimized.

Audit (2026-08-17) found the ingestion tables were sub-optimally typed:
`symbol String` (should be low-cardinality — only 3-5 distinct values across
millions of rows), no compression codecs (default LZ4), and the three tick/
orderbook/foreign_flow tables had **no `PARTITION BY`** at all (monolithic). The
tables are written LIVE during market hours and carry dependent materialized views,
so the key-column and partitioning changes require a create-v2 → copy → atomic
RENAME swap with the writer stopped — not in-place ALTERs.

## Decision

1. **ClickHouse is the single source of truth.** SQL Server is retired. The
   data_provider service skips the SQL connection by default; `DP_ENABLE_SQL_SERVER=1`
   re-enables a legacy dual-write if ever needed. `sql_helper/migrations/001_*`
   (SQL Server OI/session columns) is obsolete and will not be run.

2. **Optimized schema standard** for every market-data table:
   - `symbol` → `LowCardinality(String)`.
   - `DateTime64` timestamps → `CODEC(DoubleDelta, ZSTD(1))`.
   - Float prices → `CODEC(Gorilla, ZSTD(1))`.
   - Integer volumes/quantities → `CODEC(T64, ZSTD(1))`.
   - Arrays → `CODEC(ZSTD(1))`.
   - `PARTITION BY toYYYYMM(<event ts>)`, `ORDER BY (symbol, <event ts>)`.
   - Categorical → `Enum8` (aggressor already is).
   - Multi-symbol is a `symbol`-keyed single table per data type — NOT per-symbol
     tables (constraint 6 of the derivatives-coverage plan).

3. **Relationships** are expressed as raw → aggregate ClickHouse objects, not FKs:
   raw `vn30f_trades` → MV `trades_m5_mv`→`trades_m5_agg`, MV `profile_tpo_mv`→
   `profile_tpo_agg`; raw `ohlcv_m1`/`ohlcv_d1` → VIEWs `basis_m1`/`basis_d1`/
   `term_structure_m1`; EOD `oi_daily`. Cross-symbol metrics (basis, term structure)
   are query-time VIEWs (a ClickHouse MV cannot join across symbols at insert).

## Consequences

- **Applied now (safe — not live-written):** `ohlcv_m1` rebuilt to the standard
  (10.37→5.46 MiB, −47%); `ohlcv_d1` and `oi_daily` likewise. Dependent VIEWs
  (basis_m1/d1, term_structure_m1) survive the atomic RENAME (name-resolved).
- **Deferred to the VM maintenance window** (writer stopped, bundled with the VN100
  enable restart): the three live ingestion tables via
  `orderflow/sql/migrations/004_optimize_ingestion_schema.sql` (create-v2 → copy →
  drop+recreate MVs → atomic RENAME → verify → drop *_old).
- Naming debt: the `vn30f_*` tables are multi-symbol (symbol-keyed) but keep the
  legacy name to avoid breaking Rust consumers; a rename is a Phase-4 cutover item.
- Table `session_state` and column `vn30f_foreign_flow.open_interest` were added
  additively (migration 003) and already follow the standard.
