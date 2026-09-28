# 06 · Market profile: TPO, point of control, value area — with the tie rules written down

> **Desk relevance.** Auction structure (where the session accepted value, where it rejected
> price) is standard context for an index-futures desk. The hard part is not the idea but making
> two implementations agree on every edge case, so a research figure and a live screen never show
> different value areas.

## Rules (Rust `market-profile-engine`, mirrored in Python `shared/quant_core/profile.py` and `historical/l2/producers/prof_tpo.py`)

| Element | Rule |
|---|---|
| Prices | fixed-point `i64 × 10⁴`; bucket step = tick × grouping; round half away from zero |
| TPO period | 30 minutes; period ids zero-padded so lexical order = time order |
| Point of control | the price with the most TPOs (or volume). **Ties → the level nearest the mid of the range, then the higher price** |
| Value area | 70 % of the total. Grow **one level per step** toward the heavier neighbour; on an equal-weight tie take the **lower** side; the band stays contiguous. `VAL ≤ POC ≤ VAH` is asserted, catching the `(poc, vah, val)` vs `(poc, val, vah)` tuple-order trap |
| Initial balance | 09:00–10:00 ICT |
| References | poor high/low (extreme row with ≤ 2 TPOs), single prints, excess |
| 80 % Rule | the session opens outside the prior value area and the first N 15-minute closes re-enter it → bias toward the opposite edge |

The tie rules are not cosmetic: ADR 0037 records eight different value-area implementations in the
codebase before this one was made canonical and pinned by a shared golden fixture
(`value_area_golden.json`, 9 hand-computed cases) that both stacks test against.

## Tests

Rust: 69 tests — 5 golden fixtures, property tests (POC inside the value area, value area covers the
70 % threshold and is minimal, determinism, volume preserved) and an 80 %-rule property test.
Python: `test_value_area_parity.py`, `test_l2_parity_tpo.py` (bitwise against `l2_dump`,
mutation tests on the 70 % threshold and the period length).

## Demo

`python demos/02-order-flow-parity.py` computes the TPO profile and value area of the synthetic
sessions in both stacks and plots the profile with POC/VAH/VAL.

## Limits

The profile is built from trade prints; a volume-at-price profile from the book would need depth
history that the synthetic fixtures do not model.
