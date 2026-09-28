"""Transaction-cost model for VN30F1M research: a Python mirror of the Rust fill simulator.

All tick, fee and slippage constants live here; other modules import them. The Rust
``FillSim`` (``backtest-engine/src/fill.rs``) owns realised P&L, and
``historical/tests/test_fee_parity.py`` keeps the two implementations in step.

Per contract: transaction fee 3,700 VND and PIT 0.1 % of ``price × multiplier × IM / 2`` per side
(Thông tư 87/2026/TT-BTC); position fee 2,550 VND per ICT calendar day held. Round trip in points:
``0.0995 + 0.00017 × price + 0.0255 × extra_days``. The scenario bars in :class:`CostScenario`
are pre-registered; changing any value here needs a new pre-registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# --- mirror of fill.rs FillConfig::default (parity-tested) ---
TICK = 0.1  # VN30F1M tick in index points (fill.rs `tick_size`)
TICK_SIZE = TICK  # alias; re-exported by fee_hurdle
MULTIPLIER = 100_000.0  # VND per index point
TXN_FEE_VND_PER_SIDE = 3_700.0
POSITION_FEE_VND_PER_DAY = 2_550.0
INCOME_TAX_RATE = 0.001  # derivatives PIT rate, charged on notional × IM / 2
INITIAL_MARGIN_RATIO = 0.17  # VSDC VN30F initial margin ratio (IM)
SLIPPAGE_TICKS = 1.0  # fill.rs `Marketable`: 1 adverse tick per fill

# --- pre-registered slippage bars; see CostScenario ---
E1_TAKER_SLIP_PTS = 0.1
"""Taker slippage per fill in points (one tick)."""

WORSE_SIDE_HALF_SPREAD_PTS = 0.5
"""Conservative worse-side half-spread per fill in points (``FillConfig::worse_side(0.5)``)."""

# --- passive-queue inputs; mirror Rust `fill::e1` and `FillModel::PassiveQueue` ---
E1_PASSIVE_HALF_SPREAD_PTS = 0.12
"""Half-spread earned by a fill at the touch, in points (half the measured 0.24 mean spread)."""

E1_PASSIVE_FILL_PROB = 0.90
"""Fill probability of a resting order: the low end of the 90–96 % band estimated from recorded books."""

E1_PASSIVE_ADVERSE_SELECTION_PTS = 0.045
"""Adverse selection given a fill, in points; it reduces the half-spread earned."""

E1_PASSIVE_CHASE_PTS = 0.12
"""Cost of chasing the unfilled remainder at the horizon, in points (one half-spread)."""


def passive_queue_slip_points(
    half_spread_pts: float = E1_PASSIVE_HALF_SPREAD_PTS,
    fill_prob: float = E1_PASSIVE_FILL_PROB,
    adverse_selection_pts: float = E1_PASSIVE_ADVERSE_SELECTION_PTS,
    chase_pts: float = E1_PASSIVE_CHASE_PTS,
) -> float:
    """Return the expected adverse offset per passive fill in points (negative = credit).

    Mirrors Rust ``FillModel::PassiveQueue``, clamping ``fill_prob`` to ``[0, 1]``:
    ``p * (adverse_selection - half_spread) + (1 - p) * chase``. ``fill_prob`` is an
    expectation, not a random draw, so this models the mean only, not tail risk.
    """
    p = min(max(float(fill_prob), 0.0), 1.0)
    return p * (adverse_selection_pts - half_spread_pts) + (1.0 - p) * chase_pts


PASSIVE_QUEUE_E1_SLIP_PTS = passive_queue_slip_points()
"""Calibrated passive-queue offset, -0.0555 points per fill (a maker credit).

Mirrors ``FillConfig::passive_queue_e1()``: ``0.9 * (0.045 - 0.12) + 0.1 * 0.12``.
"""


REFERENCE_PRICE = 1900.0
"""Fallback index level for callers that do not know the trade price.

The fee is price-dependent, so callers that know the price pass it to :func:`round_trip_cost`.
1900 is near the top of the 2017–2026 range, so the fallback does not understate the PIT.
"""


@dataclass(frozen=True)
class ScenarioSpec:
    """Frozen definition of one cost scenario (slippage per fill in points)."""

    name: str
    slip_per_fill_pts: float
    rationale: str


class CostScenario(Enum):
    """Pre-registered cost scenarios: three research bars and one maker design scenario.

    ``STATUTORY`` < ``REALISTIC`` < ``WORSE_SIDE`` are the bars; a result survives cost only if it
    clears the bar named in its pre-registration. ``PASSIVE_QUEUE_E1`` carries a maker credit and
    prices a maker design; it is not a bar and must not re-score a result registered against one.
    """

    STATUTORY = ScenarioSpec(
        "statutory",
        0.0,
        "Fees only (txn + position + PIT). The absolute floor: no strategy can "
        "trade below this. Use only to isolate the fee component.",
    )
    REALISTIC = ScenarioSpec(
        "realistic",
        E1_TAKER_SLIP_PTS,
        "Fees + 2 x 0.1pt E1-measured taker slippage. Mirrors fill.rs default "
        "(`Marketable`, 1 adverse tick per fill). The default research bar.",
    )
    WORSE_SIDE = ScenarioSpec(
        "worse_side",
        WORSE_SIDE_HALF_SPREAD_PTS,
        "Fees + 2 x 0.5pt worse-side half-spread. Mirrors "
        "`FillConfig::worse_side(0.5)`; the conservative floor a keeper must "
        "survive before any capital is committed.",
    )
    PASSIVE_QUEUE_E1 = ScenarioSpec(
        "passive_queue_e1",
        PASSIVE_QUEUE_E1_SLIP_PTS,
        "MAKER DESIGN SCENARIO — NOT A PROMOTION BAR. E1-calibrated resting "
        "limit: fills with p=0.90 earning the half-spread less adverse "
        "selection, pays the chase on the rest => -0.0555pt/fill (a CREDIT). "
        "Mirrors `FillConfig::passive_queue_e1()`. Two limits: it models the "
        "MEAN and not the dispersion, and it is calibrated to a SIMULATION over "
        "venue snapshots — the live DNSE resting-limit probe (ADR 0023) is "
        "still unrun. Price maker designs with it; never re-score a card.",
    )

    @property
    def slip_per_fill_points(self) -> float:
        """Adverse slippage charged on each fill, in index points."""
        return self.value.slip_per_fill_pts

    @property
    def round_trip_slippage_points(self) -> float:
        """Adverse slippage over a round trip (2 fills), in index points."""
        return 2.0 * self.value.slip_per_fill_pts

    @property
    def rationale(self) -> str:
        return self.value.rationale


DEFAULT_SCENARIO = CostScenario.REALISTIC


def _extra_days(hold_days: int) -> float:
    """Return ``hold_days`` (ICT day boundaries crossed) clamped at 0, as fill.rs ``.max(0)``."""
    return float(max(int(hold_days), 0))


def side_fee_vnd(price: float, qty: int = 1) -> float:
    """Return the one-side fee in VND (transaction fee + PIT); mirrors ``FillConfig::side_fee``."""
    notional = price * MULTIPLIER * float(qty)
    pit = INCOME_TAX_RATE * notional * (INITIAL_MARGIN_RATIO / 2.0)
    return TXN_FEE_VND_PER_SIDE * float(qty) + pit


def position_fee_vnd(qty: int = 1) -> float:
    """Return the position-management fee for one day in VND. Mirrors ``position_fee``."""
    return POSITION_FEE_VND_PER_DAY * float(qty)


def round_trip_fee_vnd(price: float, qty: int = 1, hold_days: int = 0) -> float:
    """Return the round-trip fee in VND, excluding slippage.

    Entry: ``side_fee + position_fee`` (day 1); exit: ``side_fee + position_fee * extra_days``.
    """
    return 2.0 * side_fee_vnd(price, qty) + position_fee_vnd(qty) * (1.0 + _extra_days(hold_days))


def round_trip_fee_points(price: float, qty: int = 1, hold_days: int = 0) -> float:
    """Return the round-trip fee in index points, excluding slippage.

    Each extra ICT day held adds 0.0255 points per contract.
    """
    return round_trip_fee_vnd(price, qty, hold_days) / MULTIPLIER


def slippage_points(scenario: CostScenario = DEFAULT_SCENARIO) -> float:
    """Return the adverse slippage per fill in points for ``scenario``."""
    return scenario.slip_per_fill_points


def round_trip_cost(
    price: float,
    hold_days: int = 0,
    scenario: CostScenario = DEFAULT_SCENARIO,
    qty: int = 1,
) -> float:
    """Return the all-in round-trip cost in index points: fee plus two adverse fills.

    Args:
        price: index level of the round trip; the PIT scales with it.
        hold_days: ICT calendar days crossed (0 = intraday).
        scenario: cost scenario to charge (see :class:`CostScenario`).
        qty: number of contracts.
    """
    return (
        round_trip_fee_points(price, qty=qty, hold_days=hold_days)
        + float(qty) * scenario.round_trip_slippage_points
    )


def round_trip_cost_points(
    price: float,
    qty: int = 1,
    hold_days: int = 0,
    scenario: CostScenario = DEFAULT_SCENARIO,
) -> float:
    """Alias of :func:`round_trip_cost` taking ``(price, qty, hold_days, scenario)``."""
    return round_trip_cost(price, hold_days=hold_days, scenario=scenario, qty=qty)


def break_even_slippage_points(
    gross_points: float,
    price: float,
    hold_days: int = 0,
    qty: int = 1,
) -> float:
    """Return the adverse slippage per fill, in points, at which net P&L is zero.

    ``s* = (gross_points - round_trip_fee(price, hold_days)) / 2``, where ``gross_points`` is
    the gross P&L per round trip before fee and slippage; compare it with the per-fill bars
    (0.1 realistic, 0.5 worse-side). The fee is price-dependent, so quote ``s*`` at the price
    the design trades at, or as a curve (:func:`break_even_slippage_curve`). A negative value
    means the design cannot clear its fee at any fill quality. Exact only when P&L is linear
    in the fill price; for path-dependent exits such as a stop it is a first-order estimate.
    """
    fee = round_trip_fee_points(price, qty=qty, hold_days=hold_days)
    return (gross_points - fee) / 2.0


def break_even_slippage_curve(
    gross_points: float,
    prices: "tuple[float, ...]" = (900.0, 1242.0, 1500.0, 1900.0),
    hold_days: int = 0,
    qty: int = 1,
) -> "dict[float, float]":
    """Return ``{price: s*}`` over ``prices``; see :func:`break_even_slippage_points`.

    The default prices span the 2017–2026 VN30F1M range.
    """
    return {
        p: break_even_slippage_points(gross_points, p, hold_days=hold_days, qty=qty) for p in prices
    }


__all__ = [
    "TICK",
    "TICK_SIZE",
    "MULTIPLIER",
    "TXN_FEE_VND_PER_SIDE",
    "POSITION_FEE_VND_PER_DAY",
    "INCOME_TAX_RATE",
    "INITIAL_MARGIN_RATIO",
    "SLIPPAGE_TICKS",
    "REFERENCE_PRICE",
    "E1_TAKER_SLIP_PTS",
    "WORSE_SIDE_HALF_SPREAD_PTS",
    "E1_PASSIVE_HALF_SPREAD_PTS",
    "E1_PASSIVE_FILL_PROB",
    "E1_PASSIVE_ADVERSE_SELECTION_PTS",
    "E1_PASSIVE_CHASE_PTS",
    "PASSIVE_QUEUE_E1_SLIP_PTS",
    "passive_queue_slip_points",
    "ScenarioSpec",
    "CostScenario",
    "DEFAULT_SCENARIO",
    "side_fee_vnd",
    "position_fee_vnd",
    "round_trip_fee_vnd",
    "round_trip_fee_points",
    "slippage_points",
    "round_trip_cost",
    "round_trip_cost_points",
    "break_even_slippage_points",
    "break_even_slippage_curve",
]
