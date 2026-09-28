# 12 · Engineering method: AI-assisted, specification-first, gated by evidence

> **Desk relevance.** Research and trading code written with AI assistance is only as trustworthy
> as the controls around it. This unit describes those controls — the part a reviewer should hold
> the author accountable for.

## How the project was built

The working repository has 373 commits (Nov 2025 → Sep 2026); **275 of them were authored by an AI
coding agent** working under the controls below, with the author setting direction, making the
decisions recorded as "operator decision" in the research protocol, and owning the gates. This
repository is a distilled edition with a fresh history (`docs/provenance.md`).

## The controls

| Control | What it does | Evidence (private source; **shipped here** in bold) |
|---|---|---|
| Intake lanes | Every change is classified *tiny / normal / high-risk*; auth, data loss, security, external-provider behaviour and removing validation are always high-risk | `FEATURE_INTAKE.md`, story packets |
| Decision records | Architecture and research decisions are written down with context and consequences | 40 ADRs + 11 in the Rust workspace; **13 of them in `docs/decisions/`** (e.g. 0020 cost correction, 0029 retire the first Python backtest stack, 0037 one-name-one-formula) |
| Test matrix | Each story names the tests that prove it | `TEST_MATRIX.md` |
| Generated status pages | Research status and the feature registry page are generated from their sources; CI regenerates and fails on any hand edit | **`historical/research_lab/l2_status_page.py` + `historical/tests/test_l2_registry.py`**, which fails if `docs/L2_FEATURES.md` is stale |
| Process-defect ledger | Every time the process itself breaks, a row records who found it — an automated check or a human audit | `harness_defects.jsonl` |
| Cross-implementation parity | Critical formulas are implemented twice, in Python and Rust, and must agree bit for bit — a control that does not depend on trusting either author, human or AI | **`05-order-flow-features-and-parity.md`, demo 02** |
| Pre-registration | Research rules are frozen before evidence; changes are dated amendments | **`historical/research_lab/PRE_REGISTRATION.md`, `07-research-integrity.md`** |

## Three examples of the controls catching errors

1. **Reserved data read without the guard.** A basis screen and a daily-bar lane had read holdout
   sessions without calling the guard. Both "burns" were recorded — one proved arithmetically
   (2,073 published sessions = 1,537 dev + 536 reserved) — their results were downgraded to
   hypothesis generation, and guard coverage became a standing precondition (`07-research-integrity.md`).
2. **One name, several formulas.** An audit of the working tree found four incompatible value-area
   implementations, three CVD reset policies and nine ATR implementations behind shared names, and a
   desktop "OFI" that was not the Cont–Kukanov–Stoikov formula. The L2 feature registry now fails
   closed: every feature has a versioned, grammar-checked name and a canonical parameter hash, so one
   name means one formula (ADR 0037, `historical/research_lab/feature_boundary.py`).
3. **A generated page went stale.** While distilling, the committed research-status page no longer
   matched its generator — the test that exists to catch this failed, as intended.

## What this repository keeps from it

Selected decision records in `docs/decisions/`, the research protocol, the feature registry and the
parity tests. The agent tooling itself (hooks, session files, trace database) is operational
material and is not published.
