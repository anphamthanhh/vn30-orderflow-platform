# 02 · Transaction cost model and the derivatives PIT correction

> **Desk relevance.** Every index-arbitrage or market-making decision on VN30F1M is a decision
> about cost. This unit shows the full retail cost stack in index points, why it is
> price-dependent, and how a mis-read of the tax base inflated it — and hence killed ideas
> that the correct law would not have killed.

## Problem

The per-trade cost decides every net-of-fee verdict in thin-data VN30F1M research. The first
model charged the derivatives personal income tax (PIT, *thuế TNCN*) as **0.05 % of full
notional per side**. Thông tư 87/2026/TT-BTC (and broker fee schedules since 2024) levy it on
a **reduced base**: notional × (VSDC initial-margin ratio ÷ 2).

The rule is Article 5 of Thông tư 87/2026/TT-BTC (issued 30 June 2026, in force 1 July 2026): the
taxable transfer price of a futures trade is settlement price × multiplier × number of contracts ×
initial-margin ratio ÷ 2, taxed at 0.1 % on each transfer (each matched buy or sell order, or
expiry). A public summary:
[EY Vietnam, July 2026](https://www.ey.com/vi_vn/technical/tax/tax-and-law-updates/thong-tu-87-2026-tt-btc-huong-dan-moi-ve-nguoi-phu-thuoc-va-thue-thu-nhap-ca-nhan-doi-voi-chung-khoan-phai-sinh).

## Model (one contract, index points; multiplier 100,000 VND/point)

| Component | Rule | VND |
|---|---|---|
| Transaction fee | per side | 3,700 (broker 1,000 + exchange 2,700) |
| Position fee | per contract per ICT calendar day held | 2,550 |
| PIT | per side: `0.1 % × price × 100,000 × qty × (IM / 2)` | at IM = 17 %: `8.5 × price` |
| Slippage | per fill, by frozen scenario | see below |

```
round-trip fee (points) = 0.0995 + 0.00017 × price + 0.0255 × extra_days
all-in cost    (points) = fee + 2 × slippage_per_fill(scenario)
```

| Price | Old fee | Corrected fee | Total ratio | Tax-component ratio |
|---|---|---|---|---|
| 1,242 | 1.341 | 0.311 | 4.3× | 5.9× |
| 1,900 | 2.000 | 0.422 | 4.7× | 5.9× |

The **tax component** was ~5.9× too high; the **total** round-trip fee ~4–4.7×. Both numbers are
reproduced by `demos/01-cost-model.py`.

## Frozen cost scenarios (pre-registered bars, not tunables)

| Scenario | Slip / fill | Meaning |
|---|---|---|
| `STATUTORY` | 0.0 | fees only — the absolute floor |
| `REALISTIC` | 0.1 | one tick, the E1-measured taker slippage (median spread 0.2 pt) — default research bar |
| `WORSE_SIDE` | 0.5 | conservative half-spread every fill — the bar a keeper must survive before capital |
| `PASSIVE_QUEUE_E1` | −0.0555 | **maker design scenario**: fill p = 0.90, half-spread 0.12 earned, adverse selection 0.045, chase 0.12 on the unfilled rest. Prices a maker design; never used to re-score a taker card |

Moving any of these numbers requires a new pre-registration: quoting a looser bar after seeing a
result is goalpost-moving (see `07-research-integrity.md`).

## Design points worth noticing

- **Single source of truth.** The Rust `FillSim` (`native_platform/crates/backtest-engine/src/fill.rs`)
  owns realised P&L; `historical/backtest_engine/costs.py` mirrors it and nothing else may declare a
  cost constant. `historical/tests/test_fee_parity.py` hard-codes values derived from the Rust source
  so drift on either side fails.
- **Price dependence.** Because PIT scales with price, a scalar fee is always wrong (by up to ~35 %
  across the 2017–2026 price range). Break-even slippage `s* = (gross − fee) / 2` is therefore
  published as a curve over price (`break_even_slippage_curve`).
- **Multi-day holds** pay the position fee per ICT day; omitting it under-charges exactly the
  low-turnover strategies a research programme tends to keep.
- **Stage-0 fee hurdle** (`fee_hurdle.py`): the share of signals whose gross favourable excursion
  within 45 minutes clears the fee. It is a *necessary* gate only — on the synthetic tape 99.7 % of
  **random** entries clear the corrected fee, so passing it says nothing about edge.

## Consequences recorded in ADR 0020

Historical verdicts were **not rewritten** (they are point-in-time records) but flagged: every
pre-correction "fee-death" kill needs re-evaluation, and the binding constraint on intraday ideas
becomes slippage, not tax.

## Code, tests, demo

| What | Where |
|---|---|
| Cost model (Python mirror) | `historical/backtest_engine/costs.py` |
| Stage-0 gate | `historical/backtest_engine/fee_hurdle.py` |
| Cost model (authoritative, Rust) | `native_platform/crates/backtest-engine/src/fill.rs` |
| Decision record | `docs/decisions/0020-derivatives-pit-fee-correction.md` |
| Tests | `historical/tests/test_fee_parity.py`, `test_fee_hurdle.py`, Rust `fill.rs` unit tests |
| Demo | `python demos/01-cost-model.py` → `samples/outputs/01-cost-model/` |

## Limits

- Retail cost stack (DNSE schedule). A proprietary desk has different exchange/clearing economics,
  no broker commission and can trade the cash basket — the reason basis convergence is "prop-only"
  (`04-basis-and-index-arbitrage.md`).
- `PASSIVE_QUEUE_E1` models the **mean** maker outcome from a simulation over venue snapshots;
  the live resting-limit probe was still unrun at the time of writing.
- The IM ratio is a parameter (VSDC has moved it between 13 % and 24 %); the tax law references
  the VSDC ratio, not a broker's house margin.
