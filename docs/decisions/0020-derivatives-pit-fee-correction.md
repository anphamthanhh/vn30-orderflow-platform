# 0020 — Derivatives PIT fee correction (~6× overcharge fixed)

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (code-only — deployment is the merge; proof is the test row in docs/TEST_MATRIX.md)


## Date

2026-08-16

## Status

accepted

## Context

The per-trade cost model (`FillSim` / `fee_hurdle.py`) is the single most
load-bearing number in thin-data VN30F1M research — it decides every net-of-fee
verdict. It was **wrong**: it charged the personal income tax (thuế TNCN on
derivatives) as **0.05% × full notional per side**.

The correct rule (**Thông tư 87/2026/TT-BTC**, and already in broker fee schedules
since 2024) is:

```
PIT per side = 0.1% × giá thanh toán × hệ số nhân × số HĐ × (tỷ lệ ký quỹ ban đầu VSDC ÷ 2)
```

The taxable base is **notional × (initial-margin-ratio ÷ 2)**, NOT full notional.
At the VSDC VN30F initial-margin ratio of 17%, the effective PIT is **0.0085% of
notional per side** — the old model was **~6× too high** on the tax component.

Impact in points (round-trip, 1 lot, excl. slippage):

| price | OLD (0.05%×notional) | CORRECT (0.1%×notional×IM/2, IM=17%) |
|------|----------------------|--------------------------------------|
| 1,250 | ~1.40 pt | **~0.31 pt** |
| 1,915 | ~2.01 pt | **~0.43 pt** |

So the real regulatory cost floor is ~0.3–0.5 pt round-trip, not ~1.4–2.0 pt. The
"~2pt fee hurdle / fee-law kills everything" narrative was **partly an artifact of
this bug**; the true round-trip execution hurdle is dominated by **slippage**, not
fees.

Sources: LuatVietnam (TT 87/2026), Pinetree & ThưViệnChứngKhoán fee schedules
(worked example, IM 17%, multiplier 100,000), VSDC initial-margin notice
(eff. 19/12/2025).

## Decision

1. **Fix the source of truth (both stacks, kept in parity):**
   - `native_platform/crates/backtest-engine/src/fill.rs` — `FillConfig` now carries
     `income_tax_rate = 0.001` + `initial_margin_ratio = 0.17`; `side_fee` applies
     `rate × notional × (IM/2)`.
   - `historical/backtest_engine/fee_hurdle.py` — mirrors it (`INCOME_TAX_RATE=0.001`,
     `INITIAL_MARGIN_RATIO=0.17`).
   - Tests updated to the corrected numbers (Rust `fill.rs`, Python `test_fee_hurdle.py`).
2. **IM ratio is a parameter** (VSDC changes it over time: 13%–17%–24%). Default 17%;
   the tax law references the **VSDC** ratio, not a broker's higher house ratio.
3. **Historical verdict records are NOT rewritten** (they are point-in-time records of
   what was computed). They are flagged: any pre-2026-08-16 net-of-fee / "fee-death"
   verdict was computed against an inflated fee and **needs re-evaluation** before it
   is trusted.

## Consequences

- **Fee-gated kills must be revisited.** Candidates rejected mainly on the fee hurdle
  (e.g. `mphf` fee-death; C7's "mean +0.24pt < ~1.24pt fee") may look materially
  different at the true ~0.3pt fee. C7's median +1.5pt now clears the fee with a wide
  margin; the live question becomes slippage, not tax.
- **Forward accrual is already correct going forward:** `c7_recorder.py` and the Rust
  `ShadowLogger` both single-source the fee, so post-fix accrual uses the right cost.
- **PRE_REGISTRATION.md** fee references corrected; the frozen worse-side 0.5pt fill
  policy is unchanged (that is slippage, not fee).
- Reinforces the plan thesis: **execution realism (P02), not fee law, is the real
  hurdle.** Slippage now dominates the round-trip cost.

## Follow-ups

- Re-run the fee-sensitive screens (C7 + prior fee-death kills) with the corrected fee
  when data access permits; record new verdicts in `trials.jsonl`.
- Sweep remaining historical docs that quote "~1.35pt/~2pt fee" — flagged, not yet all
  edited (archival records).

## References

- `docs/decisions/0008-*`, `0013-*` (R&D pipeline), `0023-fillsim-single-fill-authority`
- `historical/research_lab/PRE_REGISTRATION.md`; an internal fee-schedule note (not distributed)
