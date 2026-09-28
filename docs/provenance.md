# Provenance and scope

This repository is a **distilled edition** of the author's working repository, prepared for technical
evaluation. It starts from a fresh git history: the working history contains material that must not
be published (secrets in early commits, account identifiers, infrastructure addresses, raw market
data).

## The source, as verified on 2026-09-23

| Item | Value |
|---|---|
| Active period | 2025-11-22 → 2026-09-18 |
| Commits | 373 (275 written by an AI coding agent under the author's direction — see `knowledge/12`) |
| Python (own code, excluding vendored SDK and legacy folders) | ~251,000 lines in 848 files; ~2,076 test functions |
| Rust workspace | 17 library crates + 2 applications; 106,947 lines in 198 files; 1,366 tests (1,347 offline tests pass) |
| Decision records | 40 in the research repository + 11 in the Rust workspace (a few are upstream harness templates) |

Commits per month: Nov 2025 8 · Dec 5 · Jan 2026 2 · Feb 35 · Mar 3 · Apr 56 · May 37 · Jun 0 ·
Jul 40 · Aug 92 · Sep 95.

## What this edition contains

| Area | Content | Origin |
|---|---|---|
| `historical/`, `shared/` | cost model, research lab (basis screen, calendar, holdout lock, protocol, trials ledger, forward registry), L2 producers, validation statistics, trade forensics, their tests | original code; edits listed below |
| `data_provider/`, `specs/features/` | session phases, market calendar, data-quality checks, CSV validator, the L2 observation DDL; the 12 feature specifications | original code; edits listed below |
| `native_platform/` | 15 Rust crates that build without broker, database or GPU | original code; edits listed below |
| `docs/decisions/` | 13 decision records | copied; hostnames/paths replaced, ADR 0031's host commands removed, links to unpublished records turned into plain text, a few references to unpublished records marked "(not distributed)" |
| `docs/knowledge/`, `docs/architecture.md`, `README.md` | the knowledge map | written for this edition |
| `samples/`, `demos/` | synthetic market generator, fixture generator, demos, committed outputs | written for this edition |
| `.github/workflows/ci.yml` | CI: Rust fmt/clippy/tests, Python tests with the Rust parity half, fixture check, demos, full-history secret scan | written for this edition |
| `docs/media/terminal/` | screenshots of the private terminal on the live feed (paper account), with two crops | captured by the author; images only |

This edition was prepared the same way the project was built: with an AI coding agent under the
author's direction (`knowledge/12`). The parts written for this edition are the ones marked so above.

## What is excluded, and why

| Excluded | Reason |
|---|---|
| Broker adapter, Redis/ClickHouse/SQL adapters, ingest services | need live infrastructure and credentials; described in `knowledge/09` |
| Gateway application and its auto-trade reconciler | depends on the broker adapter; described in `knowledge/11` |
| GPU desktop client | large UI code of little evaluation value without the live feed |
| First-generation walk-forward scripts and the retired backtest stack | superseded, with known defects (`knowledge/08`) |
| Literature survey and data-vendor notes | several citations could not be verified; vendor contact details |
| Real market data (tick, book, bar exports, forward ledgers, notebooks with outputs) | exchange/broker data is not redistributable — replaced by seeded synthetic data |
| Environment files, cached tokens, account identifiers, deployment scripts | secrets and infrastructure details |
| Agent-harness operational files (session notes, hooks, trace database) | operational material; the method is described in `knowledge/12` |
| An AI-generated research-planning memo (`METHODOLOGY.md`) and the decision record about the agent tooling (ADR 0034) | written by or about the AI workflow rather than as research; decisions taken from the memo are recorded in `PRE_REGISTRATION.md` and the decision records, and the code comments that cite it name it as such |

## Kinds of edits made to kept code

### Python — 94 kept files, ~22,700 lines

Each verbatim copy's source hash was recorded when copying.

1. **Excerpts.** `lab.py` keeps 518 of its 3,212 lines — the holdout guards and the few helpers its
   callers use — with the code of every kept function unchanged; two fixture tools keep only their hashing
   helpers.
2. **Package initialisers.** Made lazy (PEP 562) or trimmed, so one module imports without pulling a
   JIT compiler or unpublished modules.
3. **Scrubs.** Internal hostnames/IPs → `localhost` or a neutral name; a database container name
   removed; the account in the research probe's guard → a placeholder number; the probe's error message names
   "the ingest service" instead of its private service name; one diagnostic message
   now prints a repo-relative path instead of an absolute one.
4. **Fixtures.** The three real-tape parity fixtures replaced by synthetic ones
   (`samples/generate-fixtures.py`, SHA-256 pinned); tests re-pointed by constant only; prose that
   quoted the real fixtures' row counts now says "original real fixture".
5. **Tests removed (18 functions)**, each needing something not distributed: forward-recorder and
   accrual tests that read real forward ledgers (9); checks that mirror the unpublished gateway
   consumer (2); a backfill-calendar check (2); a plotting registry (1); a database export (1); three
   strict-mode registry checks that read trigger ids from the unpublished desktop client (3).
   **Kept tests were not weakened**; 4 tests skip at run time because they need real data files.
6. **Comments and docstrings** condensed to one-line summaries plus the units, formulas and
   contracts a reader needs; history, dates, internal ids, emphasis and references to unpublished
   material removed (about 8,700 comment and docstring lines became about 3,600). A few docstrings
   that contradicted the code were corrected, and the example rows in the `historical/l2/book.py` and
   `tape.py` docstrings now come from the synthetic fixtures. The code is unchanged: the Python ASTs
   and every code line were compared with the previous version.
7. **A stale header.** The paper-trading adapter in
   `historical/research_lab/probes/resting_limit_probe.py` described itself as "SKELETON / NOT
   IMPLEMENTED" above implemented methods; that header is gone.
8. **Generated page.** `docs/L2_FEATURES.md` regenerated from the kept registry
   (`python historical/research_lab/l2_status_page.py`); one spec note names `CvdTracker::rollover`
   instead of source line numbers, and the spec files' comments were condensed like the code's.
9. **Line endings** normalised to LF (including `trials.jsonl`, which mixed both in the source).
10. **Test configuration.** `pyproject.toml` requires Python 3.13, the version everything was run
    with, and filters one expected registry warning: two feature specs name a trigger defined in the
    unpublished desktop client.

Known, not changed: two fingerprint helpers (`historical/signal_eval/metrics.py`,
`shared/models/forecast.py`) hash raw datetime integers, so their digests change with pandas'
datetime resolution (nanoseconds in pandas 2, microseconds by default in pandas 3); the versions are
pinned in `requirements.txt`.

### Rust — 15 crates

Source: the private workspace at its last commit, plus the then-uncommitted passive-queue fill model.

1. trimmed to 15 crates; the lock file pruned from 630 to 118 packages (same versions);
2. manifests: author, evaluation licence, `publish = false`; unused dependencies removed (one of
   them pulled the whole database/broker stack into `gateway-service`); the two order-flow crates
   now inherit the workspace lints;
3. hygiene: a real account number in a test replaced by `DEMO-0001`; line endings normalised;
   about 70 comments that pointed at unpublished files, plans or decision records rephrased or
   redirected to `docs/knowledge/`;
4. defects fixed, each with a test that fails on the old code: anchored-VWAP double count; the
   frame decoder now refuses payloads above 16 MiB before allocating (this changes the decoder's
   signature);
5. the shared value-area golden fixture vendored into the workspace, with a drift test;
6. a new loopback latency probe (`native_platform/crates/client-proto/examples/loopback_latency.rs`);
7. the test modules of `gateway-service`, `market-profile-engine` and `client-proto` moved from
   each crate's `lib.rs` into a sibling `tests.rs` — token-identical, with `rustfmt` re-wrapping lines that now fit;
8. comments and doc comments condensed the same way as the Python ones; the token streams without
   comments, and every code block inside doc comments (the doctests), are unchanged.

Nothing in the kept code was rewritten to change a research result; frozen cost bars, thresholds
and registered rules are untouched.

## Data

Apart from the terminal screenshots in `docs/media/terminal/`, all market data in this repository is synthetic: `samples/synthetic/` generates an index, a
front-month future with carry, a basis deviation and expiry convergence, a trade tape and a 10-level
book, all seeded and schema-identical to the real feeds. Synthetic results illustrate mechanisms;
they are not evidence about the market.
