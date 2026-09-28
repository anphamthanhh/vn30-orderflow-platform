-- 009_l2_observations.sql: the L2 observation tier (ADR 0032 decision 3, ADR 0037).
-- Derived features, one scalar per row, identified by formula revision (`feature`),
-- parameters (`param_set_id`), build (`code_version`) and input rows (`input_version`).

-- ADDITIVE + IDEMPOTENT: only CREATE ... IF NOT EXISTS on new objects. No existing table
-- is altered or locked, so the file is safe to re-run while the L0 writer is running.

-- WHERE TO RUN IT: ClickHouse (target server 26.3), database `default`, after migrations 007
-- (schema_migrations ledger) and 008 (L0 ingest provenance).
-- `feature` is a free string to ClickHouse: the naming grammar and the cadence rules
-- (no `per_update`) are enforced by the registry, historical/research_lab/feature_boundary.py.

-- Timestamps are naive DateTime64(3), matching L0 (ADR 0037 §8).
-- Codecs follow ADR 0021: DateTime64 DoubleDelta, integers T64, floats Gorilla, each + ZSTD(1).

-- ============================================================ l2_observation
-- Scalar observations; structured features (TPO histogram, DOM matrix) go to l2_profile.
-- event_time is when the value became knowable (session D's POC: D's close), so a lag such
-- as "yesterday's POC" is taken at query time over a causal series.
CREATE TABLE IF NOT EXISTS default.l2_observation
(
    feature        LowCardinality(String)
                   COMMENT 'name INCLUDES the revision: tape_cvd_lot_v1, never cvd',
    symbol         LowCardinality(String),
    param_set_id   LowCardinality(String)
                   COMMENT 'p + 12 hex, canonical param hash; joins default.l2_param_set',
    code_version   LowCardinality(String)
                   COMMENT '{py|rs}-{git sha7}[+dirty] — FORENSIC, not a selector',
    event_time     DateTime64(3)           CODEC(DoubleDelta, ZSTD(1))
                   COMMENT 'when the value BECAME KNOWABLE (bar close / session close)',
    value          Float64                 CODEC(Gorilla, ZSTD(1)),
    window_start   DateTime64(3)           CODEC(DoubleDelta, ZSTD(1))
                   COMMENT 'earliest input that contributed (bar open / session open)',
    lookahead_ms   Int32     DEFAULT 0     CODEC(T64, ZSTD(1))
                   COMMENT '>0 = NOT causal; load_observation filters these out by default',
    is_final       UInt8     DEFAULT 1     CODEC(ZSTD(1))
                   COMMENT 'last value of this window; developing values carry 0',
    producer       LowCardinality(String)  DEFAULT 'py-batch'
                   COMMENT 'Python batch is the ONLY writer; Rust never INSERTs',
    input_version  LowCardinality(String)
                   COMMENT 'im + 12 hex; the full manifest is in default.l2_input_manifest',
    computed_at    DateTime64(3)           CODEC(DoubleDelta, ZSTD(1)),
    src_feed_id    LowCardinality(String)  DEFAULT '',
    src_ingest_seq UInt64    DEFAULT 0     CODEC(DoubleDelta, ZSTD(1))
                   COMMENT 'max ingest_seq of the L0 rows consumed. PER-PROCESS, resets on restart: forensic only, NEVER a backfill cursor',

    -- A violated CONSTRAINT makes ClickHouse reject the whole insert block.
    -- c_finite keeps NaN out; it would otherwise propagate silently through later arithmetic.
    CONSTRAINT c_causal     CHECK window_start <= event_time,
    CONSTRAINT c_not_future CHECK computed_at  >= event_time,
    CONSTRAINT c_lookahead  CHECK lookahead_ms >= 0,
    CONSTRAINT c_finite     CHECK isFinite(value)
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (feature, symbol, param_set_id, code_version, event_time)
SETTINGS index_granularity = 8192;
-- param_set_id in the sort key makes parameter filters prunable. With code_version there too,
-- a new build over an old range keeps both rows for comparison; the same build collapses.
-- No TTL: retention is a named per-feature policy in the registry (ADR 0032 §6).

-- ================================================================ l2_profile
-- Structured features: a distribution over price, one row per price level. price_ticks is
-- an integer tick count, so the key never depends on float rounding; a different bucketing
-- rule is a different feature name.
CREATE TABLE IF NOT EXISTS default.l2_profile
(
    feature        LowCardinality(String),
    symbol         LowCardinality(String),
    param_set_id   LowCardinality(String),
    code_version   LowCardinality(String),
    event_time     DateTime64(3)  CODEC(DoubleDelta, ZSTD(1)),
    window_start   DateTime64(3)  CODEC(DoubleDelta, ZSTD(1)),
    price_ticks    Int32          CODEC(DoubleDelta, ZSTD(1))
                   COMMENT 'price / 0.1 as an INTEGER (costs.py:52 TICK = 0.1). Never key on a float',
    tpo_count      UInt16         CODEC(T64, ZSTD(1)),
    volume         UInt32         CODEC(T64, ZSTD(1)),
    is_final       UInt8 DEFAULT 1,
    input_version  LowCardinality(String),
    computed_at    DateTime64(3)  CODEC(DoubleDelta, ZSTD(1)),

    CONSTRAINT c_causal     CHECK window_start <= event_time,
    CONSTRAINT c_not_future CHECK computed_at  >= event_time
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(event_time)
ORDER BY (feature, symbol, param_set_id, code_version, event_time, price_ticks)
SETTINGS index_granularity = 8192;

-- ============================================================== l2_param_set
-- Projection of specs/features/*.yaml so SQL can resolve a param_set_id. Git is the
-- authority: rows are generated from the YAML, never written by hand.
CREATE TABLE IF NOT EXISTS default.l2_param_set
(
    param_set_id  LowCardinality(String),
    feature       LowCardinality(String),
    set_name      LowCardinality(String)
                  COMMENT '"default", or the registered name of the set',
    canon_json    String
                  COMMENT 'the EXACT string that was hashed, verbatim — Rust reads it, does not re-derive it',
    spec_sha      LowCardinality(String),
    synced_at     DateTime64(3)
)
ENGINE = ReplacingMergeTree(synced_at)
ORDER BY (feature, param_set_id);

-- ======================================================== l2_feature_registry
-- One row per registry entry (status, causality, authority), projected from the YAML
-- like l2_param_set.
CREATE TABLE IF NOT EXISTS default.l2_feature_registry
(
    feature          LowCardinality(String),
    family           LowCardinality(String)  COMMENT 'ob | tape | prof | xform | vn',
    status           LowCardinality(String)
                     COMMENT 'PROPOSED IMPLEMENTED PARITY BACKFILLED LIVE DEPRECATED RETIRED',
    cadence          LowCardinality(String)  COMMENT 'per_update is refused by the registry',
    unit             LowCardinality(String),
    causal           UInt8,
    authority        LowCardinality(String)  COMMENT 'rust | python — who owns the DEFINITION',
    parity_tolerance Float64                 COMMENT '0 = bitwise; non-zero requires a declared reason',
    spec_sha         LowCardinality(String),
    synced_at        DateTime64(3)
)
ENGINE = ReplacingMergeTree(synced_at)
ORDER BY (feature);

-- ========================================================= l2_input_manifest
-- input_version -> the L0 ranges read (partitions, row count, max recv_time and ingest_seq
-- per feed). Contents are not hashed: the summary detects appends, the only change an
-- append-only L0 allows, without a second full scan.
CREATE TABLE IF NOT EXISTS default.l2_input_manifest
(
    input_version  LowCardinality(String),
    manifest_json  String  COMMENT 'schema "l2.input_manifest/1"',
    created_at     DateTime64(3)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (input_version);

-- ======================================================== l2_backfill_ledger
-- One row per backfill run of (feature, param set, symbol, session, build).
CREATE TABLE IF NOT EXISTS default.l2_backfill_ledger
(
    feature       LowCardinality(String),
    param_set_id  LowCardinality(String),
    symbol        LowCardinality(String),
    session_date  Date,
    code_version  LowCardinality(String),
    input_version LowCardinality(String),
    row_count     UInt32,
    status        LowCardinality(String)  COMMENT 'PENDING | OK | FAIL | SKIPPED',
    reason        String DEFAULT '',
    host          LowCardinality(String) DEFAULT '',
    started_at    DateTime64(3),
    finished_at   DateTime64(3)
)
ENGINE = ReplacingMergeTree(finished_at)
PARTITION BY toYYYYMM(session_date)
ORDER BY (feature, param_set_id, symbol, session_date, code_version);

-- ==================================================== l2_observation_causal
-- Default read path: deduplicated (FINAL), causal rows only.
CREATE VIEW IF NOT EXISTS default.l2_observation_causal AS
SELECT * FROM default.l2_observation FINAL WHERE lookahead_ms = 0;

-- Verify: SELECT name, engine FROM system.tables WHERE database = 'default' AND name LIKE 'l2\\_%'
-- lists six tables and one view. An INSERT with window_start > event_time, or with value nan,
-- must be rejected; if it succeeds, the CHECK constraints are not attached.

-- Rollback: drop the view default.l2_observation_causal, then the six l2_* tables. Nothing
-- else reads them, and every value is recomputable from L0.
