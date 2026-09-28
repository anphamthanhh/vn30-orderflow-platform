# L2 OBSERVATIONS - feature registry status

<!-- GENERATED FILE - do not edit by hand.
     Regenerate: python historical/research_lab/l2_status_page.py
     Sources: specs/features/*.yaml (authority), default.l2_observation (coverage,
     read-only, only with --clickhouse) -->

Generated at: 2026-09-28T03:52:09Z

Authority for everything below is `specs/features/*.yaml` in git, loaded through `historical/research_lab/feature_boundary.py`. The ClickHouse tables are a PROJECTION of it, never the other way round: a parameter set is a human decision and belongs where review and blame live.

## Layer

| | |
|---|---|
| Registered features | 12 in `specs/features/` |
| By status | IMPLEMENTED=5, PARITY=7 |
| DDL | `data_provider/orderflow/sql/migrations/009_l2_observations.sql` - written, NOT applied |
| Writeable statuses | BACKFILLED, LIVE, PARITY |
| Returned by `load_observation` by default | BACKFILLED, LIVE |
| Forbidden cadence | `per_update` (~1 GiB/year storage ceiling) |
| Param sets per feature | at most 8 |
| Default parity tolerance | 0 (bitwise); non-zero requires `tolerance_reason` |
| Cluster coverage probe | unreachable |

## Features

| feature | status | cadence | unit | param sets | coverage (sessions) | first/last event_time | code_version present | parity | supersedes |
|---|---|---|---|---|---|---|---|---|---|
| `ob_depth_resid_imb_ratio_v1` | PARITY | m1 | ratio | `default`=p99af7c3da400 | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation of this quantity... | - |
| `ob_depth_resid_lot_v1` | PARITY | m1 | lot | `default`=p99af7c3da400 | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation exists, and no R... | - |
| `ob_depth_top10_lot_v1` | PARITY | m1 | lot | `default`=p99af7c3da400 | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation of THIS quantity... | - |
| `ob_illiq_amihud_v1` | IMPLEMENTED | session | ret_per_mpts | `default`=p1f152b925125 | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation exists and none... | - |
| `ob_microprice_pts_v1` | PARITY | m1 | pts | `default`=p98da64ae7f4e | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation exists to compar... | - |
| `ob_spread_cs_pts_v1` | IMPLEMENTED | session | pts | `default`=p61012cf91810 | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation exists and none... | - |
| `ob_spread_pts_v1` | IMPLEMENTED | m1 | pts | `default`=p98da64ae7f4e | unreachable | unreachable | unreachable | bitwise (0), historical/tests/test_l2_book_cost.py::test_spread_matches_hand_computed_golden_vector | - |
| `ob_spread_roll_pts_v1` | IMPLEMENTED | session | pts | `default`=p4b7f8920b17e | unreachable | unreachable | unreachable | bitwise (0), WAIVED: No Rust implementation exists and none... | - |
| `ob_spread_tod_ratio_v1` | IMPLEMENTED | m1 | ratio | `default`=pd76db2058a1a, `short`=p6647e3fe5b7a | unreachable | unreachable | unreachable | bitwise (0), historical/tests/test_l2_book_cost.py::test_spread_tod_ratio_matches_hand_computed_golden_vector | - |
| `prof_tpo_poc_pts_v1` | PARITY | session | pts | `default`=pdfcc18ee0465 | unreachable | unreachable | unreachable | bitwise (0), historical/tests/test_l2_parity_tpo.py::test_rust_and_python_agree_bitwise | - |
| `tape_cvd_lot_v1` | PARITY | m1 | lot | `default`=p2ea1bc554894 | unreachable | unreachable | unreachable | bitwise (0), historical/tests/test_l2_parity_cvd.py::test_rust_and_python_agree_bitwise | - |
| `tape_cvd_norst_lot_v1` | PARITY | m1 | lot | `default`=p3e0a4498f227 | unreachable | unreachable | unreachable | bitwise (0), historical/tests/test_l2_parity_cvd.py::test_rust_and_python_agree_bitwise | - |

## Feature detail

### ob_depth_resid_imb_ratio_v1 (PARITY)

- **Title:** Signed bid-vs-offer tilt of the depth beyond the ten published levels
- **Family / unit / dtype:** ob / ratio / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** python - rust `-` / python `historical/l2/producers/book_cost.py::hidden_imb_ratio`
- **Parity:** bitwise (0), WAIVED: No Rust implementation of this quantity... (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `8b59c6a8aa7c6764`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p99af7c3da400` | agg='last', cadence='m1', levels=10, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_volumes, offer_volumes, total_bid_qty, total_offer_qty |

**Note.** Returns 0.0 when neither side has any hidden size at all, so "no evidence" and "balanced evidence" read alike. The alternative - NaN - would silently drop the row out of any downstream mean and turn a measurable state into a missing one. Never observed on real data (hidden > 0 on 6,581/6,581 rows of the original real fixture).

**Next action.** Owed: `agg: median` needs a feature_boundary vocabulary change. Whether this tilt relates to cost or fill probability is NOT measured here and no such claim is made - that is a card's question, not a feature spec's.

### ob_depth_resid_lot_v1 (PARITY)

- **Title:** Per-side depth beyond the ten published levels, end-of-minute
- **Family / unit / dtype:** ob / lot / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** python - rust `-` / python `historical/l2/producers/book_cost.py::hidden_lot`
- **Parity:** bitwise (0), WAIVED: No Rust implementation exists, and no R... (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `2f91037e7630d8d3`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p99af7c3da400` | agg='last', cadence='m1', levels=10, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_volumes, offer_volumes, total_bid_qty, total_offer_qty |

**Note.** NOT clamped at zero. A negative value would mean the venue published a whole-book total smaller than the ten levels it also published - a feed contradicting itself - and max(0, ...) would turn that into a plausible number. It has never been observed (0 occurrences in 420,987 rows, 0 in the fixture), which is exactly why the day it happens it must be visible.

**Next action.** Owed: `agg: median` needs a feature_boundary vocabulary change. Also worth measuring separately, and deliberately NOT measured here: whether hidden depth moves with cost or with fill probability. This branch builds features and makes no predictive claim; that question belongs to a card with a registered spec, not to a spec file.

### ob_depth_top10_lot_v1 (PARITY)

- **Title:** Per-side visible depth over the ten published levels, end-of-minute
- **Family / unit / dtype:** ob / lot / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** python - rust `-` / python `historical/l2/producers/book_cost.py::depth_top10_lot`
- **Parity:** bitwise (0), WAIVED: No Rust implementation of THIS quantity... (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `e405599700d2dbdd`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p99af7c3da400` | agg='last', cadence='m1', levels=10, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_volumes, offer_volumes |

**Note.** Visible depth only. The size resting BEYOND these ten levels is a separate feature, ob_depth_resid_lot_v1, and it is the larger of the two by roughly 18x on the fixture - so reading this number as "how much size is there" would be wrong by an order of magnitude.

**Next action.** Owed: `agg: median` needs a feature_boundary vocabulary change before it can be declared here. If an engine-side depth is ever written in Rust, this waiver must be deleted and a real bitwise parity test put in its place.

### ob_illiq_amihud_v1 (IMPLEMENTED)

- **Title:** Amihud illiquidity, mean of |daily return| per million points of turnover
- **Family / unit / dtype:** ob / ret_per_mpts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=trailing_window, event_time=session_close, is_final=session_close
- **Authority:** python - rust `-` / python `historical/l2/producers/daily_micro.py::produce`
- **Parity:** bitwise (0), WAIVED: No Rust implementation exists and none... (golden tape: `historical/tests/fixtures/daily_micro_golden_v1.json`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `a35bc0189785eb4c`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p1f152b925125` | agg='mean', cadence='session', gap_policy='drop_data_gap', levels=1, min_obs=10, return_type='simple', roll_boundary='exclude', seam_13h='none', session='calendar_day', session_reset='open', stale_ms=0, symbol_scope='single', turnover_scale=1000000, warmup_bars=11, window={'value': 21, 'unit': 'session'}, zero_turnover='skip' |

| input layer | table | columns |
|---|---|---|
| L1 | default.ohlcv_m1 | time_1m, close, volume |

**Note.** Ships with ob_spread_cs_pts_v1 and ob_spread_roll_pts_v1 as three INDEPENDENT estimators of liquidity; agreement is evidence, disagreement localises a mis-specification. Amihud is defined on every session with a usable return, so it is the widest-coverage of the three and is the natural reference when the other two disagree about a period.

**Next action.** Status is IMPLEMENTED, not PARITY: nothing is backfilled and migration 009_l2_observations.sql is unapplied, so this feature writes no rows. test_registry_entry_passes_the_writeable_gate asserts the entry would pass the writeable-status gate as written.

### ob_microprice_pts_v1 (PARITY)

- **Title:** Size-weighted mid price (Stoikov micro-price), end-of-minute
- **Family / unit / dtype:** ob / pts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** python - rust `-` / python `historical/l2/producers/book_cost.py::microprice_pts`
- **Parity:** bitwise (0), WAIVED: No Rust implementation exists to compar... (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `31481462acc0109e`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p98da64ae7f4e` | agg='last', cadence='m1', levels=1, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_prices, bid_volumes, offer_prices, offer_volumes |

**Note.** Falls back to the arithmetic mid when BOTH top-of-book queues are empty. That cannot happen on this feed - 0 zero-volume levels in 22,012,608 rows - but a division by zero is not an acceptable answer to a case that cannot happen, because "cannot happen" is a claim about a feed, not about arithmetic.

**Next action.** Owed: `agg: median` is the analytically better within-minute reduction for a skewed distribution and is not in feature_boundary's closed `agg` vocabulary; adding it is a gate change, not a feature-branch edit. Also owed: if a Rust micro-price is ever written, this waiver must be deleted and replaced by a real bitwise parity test - the gate refuses a waiver as soon as both stacks implement a feature.

### ob_spread_cs_pts_v1 (IMPLEMENTED)

- **Title:** Corwin-Schultz two-day high-low effective spread, in index points
- **Family / unit / dtype:** ob / pts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=prior_session_open, event_time=session_close, is_final=session_close
- **Authority:** python - rust `-` / python `historical/l2/producers/daily_micro.py::produce`
- **Parity:** bitwise (0), WAIVED: No Rust implementation exists and none... (golden tape: `historical/tests/fixtures/daily_micro_golden_v1.json`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `603f756a3a30be69`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p61012cf91810` | agg='last', cadence='session', gap_policy='drop_data_gap', levels=1, negative_rule='zero', price_ref='close_d2', roll_boundary='exclude', seam_13h='none', session='calendar_day', session_reset='open', stale_ms=0, symbol_scope='single', warmup_bars=1, window={'value': 1, 'unit': 'session'} |

| input layer | table | columns |
|---|---|---|
| L1 | default.ohlcv_m1 | time_1m, high, low, close |

**Note.** Three estimators of one quantity ship together on purpose (this one, Roll, Amihud): they are independent, so agreement is evidence the measurement is real and disagreement localises a mis-specification. Reading any of them alone throws that away. The Python producer reads data_provider/VN30F1M_Final.csv, validated 2026-08-14 as identical to default.ohlcv_m1; the DEV filter is derived from holdout_lock.json and re-asserted by daily.assert_dev_only_daily.

**Next action.** Status is IMPLEMENTED, not PARITY: the evidence package is green but nothing has been backfilled and migration 009_l2_observations.sql is still unapplied, so this feature writes no rows. test_registry_entry_passes_the_writeable_gate asserts the entry would pass the writeable-status gate as written, so promotion is a one-word edit once an operator decides a waiver-backed feature may be stored.

### ob_spread_pts_v1 (IMPLEMENTED)

- **Title:** Quoted best-offer minus best-bid spread, end-of-minute
- **Family / unit / dtype:** ob / pts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** rust - rust `native_platform/crates/market-dom-engine/src/lib.rs::spread_from_best_levels` / python `historical/l2/producers/book_cost.py::spread_pts`
- **Parity:** bitwise (0), historical/tests/test_l2_book_cost.py::test_spread_matches_hand_computed_golden_vector (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `31c6fffc53971048`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p98da64ae7f4e` | agg='last', cadence='m1', levels=1, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_prices, offer_prices |

**Note.** `agg: last` is the end-of-minute book state - what a once-a-minute subscriber sees, and what the Signal Atlas book-family detector (not distributed) already takes per second. For a STATE quantity that is a definition rather than an average, so the measured skew of the spread distribution (1 tick 32%, 2 ticks 28%, 3 ticks 22%, 4 ticks 11%; median 0.2 pt) does not bias it the way a within-minute mean would. The analytically better reduction for that skew is the within-minute MEDIAN, and feature_boundary's `agg` vocabulary (last/first/max/min/mean/sum) does not contain it; widening the gate to fit a feature is not something a feature branch does. Recorded in next_action.

**Next action.** TWO items owed, neither of them done here. (1) PARITY: market-dom-engine computes this exact quantity, so a waiver is forbidden and the feature stays at IMPLEMENTED until a Rust dump exists to diff against. Emitting one means editing orderflow-engine/src/bin/l2_dump.rs, which a parallel branch holds; this branch did not contend for the file. (2) `agg: median` needs a feature_boundary vocabulary change (an ADR-level edit to a closed set), after which a `median` param set can be added here without changing what `default` means.

### ob_spread_roll_pts_v1 (IMPLEMENTED)

- **Title:** Roll effective spread from the serial covariance of daily price changes
- **Family / unit / dtype:** ob / pts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=trailing_window, event_time=session_close, is_final=session_close
- **Authority:** python - rust `-` / python `historical/l2/producers/daily_micro.py::produce`
- **Parity:** bitwise (0), WAIVED: No Rust implementation exists and none... (golden tape: `historical/tests/fixtures/daily_micro_golden_v1.json`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `afdae6d98f7aa042`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p4b7f8920b17e` | agg='last', cadence='session', cov_ddof=1, gap_policy='drop_data_gap', levels=1, min_pairs=10, roll_boundary='exclude', seam_13h='none', session='calendar_day', session_reset='open', stale_ms=0, symbol_scope='single', warmup_bars=12, window={'value': 21, 'unit': 'session'} |

| input layer | table | columns |
|---|---|---|
| L1 | default.ohlcv_m1 | time_1m, close |

**Note.** Ships with ob_spread_cs_pts_v1 and ob_illiq_amihud_v1 as three INDEPENDENT estimators of one quantity: convergence is evidence, divergence localises a mis-specification, and reading one alone discards both. Roll answers on strictly fewer sessions than CS by construction, so a coverage difference between the two is expected and is not a defect.

**Next action.** Status is IMPLEMENTED, not PARITY: nothing is backfilled and migration 009_l2_observations.sql is unapplied, so this feature writes no rows. test_registry_entry_passes_the_writeable_gate asserts the entry would pass the writeable-status gate as written.

### ob_spread_tod_ratio_v1 (IMPLEMENTED)

- **Title:** Quoted spread divided by its trailing baseline for that minute of session
- **Family / unit / dtype:** ob / ratio / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** python - rust `-` / python `historical/l2/producers/book_cost.py::spread_tod_ratio`
- **Parity:** bitwise (0), historical/tests/test_l2_book_cost.py::test_spread_tod_ratio_matches_hand_computed_golden_vector (golden tape: `historical/tests/fixtures/book_cost_book_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `a7c3e2fa92863040`

| param set | param_set_id | parameters |
|---|---|---|
| default | `pd76db2058a1a` | agg='last', baseline_key='minute_of_session', baseline_min_sessions=5, baseline_sessions=20, baseline_stat='median', cadence='m1', levels=1, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |
| short | `p6647e3fe5b7a` | agg='last', baseline_key='minute_of_session', baseline_min_sessions=1, baseline_sessions=5, baseline_stat='median', cadence='m1', levels=1, seam_13h='none', session_reset='none', stale_ms=0, symbol_scope='single', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_orderbook | event_timestamp, bid_prices, offer_prices |

**Note.** Emits no row when fewer than `baseline_min_sessions` earlier sessions carry that minute, or when the baseline is <= 0 (a locked or crossed book divides into nonsense). Both are silences, not zeros: a fabricated 1.0 would read as "normal" on exactly the days there is no evidence of what normal is.

**Next action.** Held at IMPLEMENTED on purpose. A waiver would be legal here (no Rust implements this ratio) but would be claiming a green gate for a number built on ob_spread_pts_v1, whose own Rust twin - market-dom-engine::spread_from_best_levels - has never been diffed against the Python. Emitting that dump means editing orderflow-engine/src/bin/l2_dump.rs, which a parallel branch holds; this branch did not contend for the file. Promote both together once the dump exists. Also owed: `agg: median` needs a feature_boundary vocabulary change.

### prof_tpo_poc_pts_v1 (PARITY)

- **Title:** TPO point of control, canonical value-area v1
- **Family / unit / dtype:** prof / pts / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=session_open, event_time=session_close, is_final=session_close
- **Authority:** rust - rust `native_platform/crates/market-profile-engine/src/lib.rs::value_area_from_pairs` / python `historical/l2/producers/prof_tpo.py::produce`
- **Parity:** bitwise (0), historical/tests/test_l2_parity_tpo.py::test_rust_and_python_agree_bitwise (golden tape: `historical/tests/fixtures/l2_prof_tpo_synthetic_20210304_20210305.csv`)
- **Derives trigger:** poc_reaction_v1 - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `a36b07bf2f679160`

| param set | param_set_id | parameters |
|---|---|---|
| default | `pdfcc18ee0465` | cadence='session', poc_tiebreak='mid_then_higher', price_bucket_tick=1, seam_13h='none', session='ict_full', session_reset='open', symbol_scope='single', tpo_period_min=30, va_pct=0.7, va_tiebreak='lower', warmup_bars=0 |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_trades | event_timestamp, price |

**Note.** Choosing lib.rs as v1 leaves orderflow-engine/src/volume_profile.rs and the Signal Atlas profile module (not distributed) as two implementations that must be deleted or renamed _v2/_v3. That touches live code and is tracked outside this file. The scalar reductions (poc, vah, val, va_width) live in default.l2_observation; the price-by-price DISTRIBUTION they are reduced from goes to default.l2_profile, keyed by integer ticks. Both shapes are produced (--emit observation | profile) and both are compared bitwise; checking only the scalars would let the two stacks build different histograms that happen to reduce to the same three numbers on this tape. THE TUPLE-ORDER TRAP IS TESTED, NOT JUST DOCUMENTED: market-profile-engine returns (poc, VAH, VAL) and three other implementations return (poc, VAL, VAH), which value_area_golden.json cannot see because it compares by field NAME. The parity test compares named COLUMNS, asserts val <= poc <= vah on every observation on BOTH sides, and carries a mutation that swaps VAH/VAL; injecting that swap into the producer on 2026-09-06 turned 7 of the 16 tests red, so the assertion is load-bearing.

**Next action.** Parity is green (2026-09-06): the Rust l2_dump and the Python producer are byte-identical on both emitted shapes - 2 observation rows and 294 profile rows over two sessions - at tolerance 0. Mutations proven to change the answer on this tape: VAH/VAL swap, va_pct 0.70 -> 0.68, tpo_period_min 30 -> 60 and -> 15, and the first-argmax POC tie-break that the real-time viewer's profile metrics (not distributed) serves live today. Next gate is LIVE, which needs migration 009 applied and a backfill run - neither has happened, so this feature writes nothing yet. Still owed and NOT part of this gate: renaming or deleting the two non-canonical value areas per Q3.

### tape_cvd_lot_v1 (PARITY)

- **Title:** Cumulative volume delta, session-reset with a 13:00 rollover
- **Family / unit / dtype:** tape / lot / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** rust - rust `native_platform/crates/orderflow-engine/src/cvd.rs::CvdTracker` / python `historical/l2/producers/tape_cvd.py::produce`
- **Parity:** bitwise (0), historical/tests/test_l2_parity_cvd.py::test_rust_and_python_agree_bitwise (golden tape: `historical/tests/fixtures/l2_tape_cvd_synthetic_20210304_20210305.csv`)
- **Derives trigger:** cvd_divergence_v1 - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `d8f7873f52eba3e7`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p2ea1bc554894` | cadence='m1', seam_13h='rollover', session_reset='open', sign_method='venue_flag', symbol_scope='single', unknown_side='zero', warmup_bars=0, window={'value': 1, 'unit': 'bar'} |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_trades | event_timestamp, price, volume, aggressor |

**Note.** Reset at session open plus a 13:00 rollover (keep the LEVEL, drop the history) is the sig001/pure_cvd behaviour and matches CvdTracker::rollover in cvd.rs. cvd_exhaustion - a strategy that already has a backtest - uses the NO-reset variant, which is why that variant gets its own name rather than a second meaning for this one.

**Next action.** Parity is green (2026-09-05): the Rust l2_dump dump and the Python producer are byte-identical over 10,345 steps of the original real tape at tolerance 0. Next gate is LIVE, which needs migration 009 applied and a backfill run - neither has happened, so this feature writes nothing yet.

### tape_cvd_norst_lot_v1 (PARITY)

- **Title:** Cumulative volume delta, no reset, cumulative over the whole tape
- **Family / unit / dtype:** tape / lot / float64
- **Causality:** causal=True lookahead_ms=0 - window_start=bar_open, event_time=bar_close, is_final=every_bar
- **Authority:** rust - rust `native_platform/crates/orderflow-engine/src/cvd.rs::CvdTracker` / python `historical/l2/producers/tape_cvd.py::produce`
- **Parity:** bitwise (0), historical/tests/test_l2_parity_cvd.py::test_rust_and_python_agree_bitwise (golden tape: `historical/tests/fixtures/l2_tape_cvd_synthetic_20210304_20210305.csv`)
- **Derives trigger:** - - maps to validation state: offline-replayed
- **Retention:** unbounded - pinned code_version: None
- **spec_sha:** `185881fd5f800465`

| param set | param_set_id | parameters |
|---|---|---|
| default | `p3e0a4498f227` | cadence='m1', seam_13h='none', session_reset='none', sign_method='venue_flag', symbol_scope='single', unknown_side='zero', warmup_bars=0, window={'value': 1, 'unit': 'bar'} |

| input layer | table | columns |
|---|---|---|
| L0 | default.vn30f_trades | event_timestamp, price, volume, aggressor |

**Note.** Bitwise-identical across Rust and Python over the same fixture (10,345 real steps in the original) as tape_cvd_lot_v1. test_the_two_features_are_actually_different_series asserts the two dumps DIFFER, so this name cannot decay into a cosmetic alias of the other, and test_the_day_boundary_does_reset pins that this one does NOT reset where _v1 does.

**Next action.** Same gate as tape_cvd_lot_v1: LIVE needs migration 009 applied and a backfill run. Neither has happened, so this feature writes nothing yet.

## Registry vs cluster consistency

No registry-only disagreement. Cluster checks were not run (no `--clickhouse`), so coverage findings are unknown, not absent.

## Registry load findings (advisory)

| feature | problem |
|---|---|
| prof_tpo_poc_pts_v1 | 'derives_trigger' could not be checked: native_platform/apps/client-desktop/src/signal_engine/types.rs unreadable |
| tape_cvd_lot_v1 | 'derives_trigger' could not be checked: native_platform/apps/client-desktop/src/signal_engine/types.rs unreadable |

