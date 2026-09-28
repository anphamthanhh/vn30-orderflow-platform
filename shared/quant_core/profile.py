"""Market/volume-profile primitives: the canonical value area shared with the Rust engine.

Mirrors ``value_area_from_pairs`` in ``native_platform/crates/market-profile-engine/src/lib.rs``
rule for rule; both stacks test against ``shared/quant_core/testdata/value_area_golden.json``.

- POC: the highest weight; ties go to the level nearest the mid-range, then to the higher price.
- Expansion: one level per step (not the classic two-row TPO method) toward the heavier
  neighbour; an equal-weight tie takes the lower side. The result is always contiguous.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple

DEFAULT_VALUE_AREA_PCT = 0.70


def value_area(
    rows: Iterable[Tuple[float, float]] | dict,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> Optional[Tuple[float, float, float]]:
    """Canonical value area over ``(level, weight)`` pairs or a ``{level: weight}`` mapping.

    Levels may be prices or integer grid indices; weight is volume or TPO count. Returns
    ``(poc, val, vah)``, VAL before VAH (the Rust engine returns ``(poc, vah, val)``), or ``None``
    for an empty profile. Raises ``ValueError`` if ``value_area_pct`` is outside ``(0, 1]`` or the
    total weight is not positive (Rust ``InvalidValueAreaPct`` / ``ZeroTotalVolume``).
    """
    if not 0.0 < value_area_pct <= 1.0:
        raise ValueError(f"value_area_pct must be in (0, 1], got {value_area_pct}")

    pairs: Sequence[Tuple[float, float]]
    if isinstance(rows, dict):
        pairs = sorted(rows.items())
    else:
        pairs = sorted(rows)
    if not pairs:
        return None

    total = sum(weight for _, weight in pairs)
    if total <= 0:
        raise ValueError("value area cannot be calculated when total weight is zero")

    # mid-range distance as |2*price - (low + high)| avoids halving, as in the Rust engine
    two_mid = pairs[0][0] + pairs[-1][0]

    def midrange_distance(price: float) -> float:
        return abs(2.0 * price - two_mid)

    poc_index = 0
    for idx in range(1, len(pairs)):
        cur_price, cur_weight = pairs[idx]
        best_price, best_weight = pairs[poc_index]
        better = cur_weight > best_weight or (
            cur_weight == best_weight
            and (
                midrange_distance(cur_price) < midrange_distance(best_price)
                or (
                    midrange_distance(cur_price) == midrange_distance(best_price)
                    and cur_price > best_price
                )
            )
        )
        if better:
            poc_index = idx

    target = total * value_area_pct
    covered = float(pairs[poc_index][1])
    left: Optional[int] = poc_index - 1 if poc_index > 0 else None
    right = poc_index + 1
    lo_index = hi_index = poc_index

    while covered < target and (left is not None or right < len(pairs)):
        left_weight = pairs[left][1] if left is not None else -1
        right_weight = pairs[right][1] if right < len(pairs) else -1
        # strict `>`: an equal-weight tie takes the lower side, as in the Rust engine
        if right_weight > left_weight:
            covered += pairs[right][1]
            hi_index = right
            right += 1
        elif left is not None:
            covered += pairs[left][1]
            lo_index = left
            left = left - 1 if left > 0 else None
        elif right < len(pairs):
            covered += pairs[right][1]
            hi_index = right
            right += 1

    return pairs[poc_index][0], pairs[lo_index][0], pairs[hi_index][0]


def point_of_control(rows: Iterable[Tuple[float, float]] | dict) -> Optional[float]:
    """The POC alone, under the canonical tie-break."""
    area = value_area(rows, DEFAULT_VALUE_AREA_PCT)
    return None if area is None else area[0]
