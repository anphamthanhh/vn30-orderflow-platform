"""Stage-0 fee hurdle: the share of a strategy's signals whose gross favorable move clears cost.

Runs before any statistical test. Cost functions delegate to
:mod:`historical.backtest_engine.costs`, the mirror of the Rust ``FillSim``; this module
declares no cost constant of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

PathLike = Union[str, Path]

# cost constants are re-exported from costs.py, the only module that declares them
from historical.backtest_engine.costs import (  # noqa: E402
    INCOME_TAX_RATE,
    INITIAL_MARGIN_RATIO,
    MULTIPLIER,
    POSITION_FEE_VND_PER_DAY,
    SLIPPAGE_TICKS,
    TICK_SIZE,
    TXN_FEE_VND_PER_SIDE,
    CostScenario,
)
from historical.backtest_engine import costs as _costs  # noqa: E402


def slippage_points(scenario: CostScenario = CostScenario.REALISTIC) -> float:
    """Return the adverse slippage per fill in points (default: one tick, as in fill.rs)."""
    return _costs.slippage_points(scenario)


def round_trip_fee_points(price: float, qty: int = 1, hold_days: int = 0) -> float:
    """Return the round-trip fee in index points at ``price``, excluding slippage.

    Both sides are priced at ``price``; the entry/exit price difference is second order.
    ``hold_days`` counts ICT day boundaries crossed; each adds 0.0255 points per contract.
    """
    return _costs.round_trip_fee_points(price, qty=qty, hold_days=hold_days)


def round_trip_cost_points(
    price: float,
    qty: int = 1,
    hold_days: int = 0,
    scenario: CostScenario = CostScenario.REALISTIC,
) -> float:
    """Return the all-in round-trip hurdle in points: fee plus two adverse fills.

    The default ``REALISTIC`` scenario charges one tick per fill; pass
    ``CostScenario.WORSE_SIDE`` for the conservative 0.5-point floor.
    """
    return _costs.round_trip_cost(price, hold_days=hold_days, scenario=scenario, qty=qty)


def gross_favorable_excursion(
    bars: pd.DataFrame,
    entries: pd.DataFrame,
    horizon_min: int = 45,
) -> pd.DataFrame:
    """Return ``entries`` with the best gross favorable move within ``horizon_min`` minutes.

    ``bars`` needs ``ts_open``, ``high`` and ``low``; ``entries`` needs ``entry_ts`` (unix s),
    ``dir`` ('long'/'short') and ``entry_price``. Long MFE is ``max(high) - entry_price``, short
    MFE ``entry_price - min(low)``. Adds ``mfe_pts``, ``fee_pts``, ``cost_pts``, ``clears_fee``
    and ``clears_cost``.
    """
    out = entries.copy().reset_index(drop=True)
    ts_open = bars["ts_open"].to_numpy()
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    mfe = np.full(len(out), np.nan)
    for i, row in out.iterrows():
        lo_ts = int(row["entry_ts"])
        hi_ts = lo_ts + horizon_min * 60
        m = (ts_open >= lo_ts) & (ts_open <= hi_ts)
        if not m.any():
            continue
        if str(row["dir"]).lower() == "long":
            mfe[i] = float(highs[m].max() - row["entry_price"])
        else:
            mfe[i] = float(row["entry_price"] - lows[m].min())
    out["mfe_pts"] = mfe
    out["fee_pts"] = out["entry_price"].map(round_trip_fee_points)
    out["cost_pts"] = out["entry_price"].map(round_trip_cost_points)
    out["clears_fee"] = out["mfe_pts"] >= out["fee_pts"]
    out["clears_cost"] = out["mfe_pts"] >= out["cost_pts"]
    return out


def fee_hurdle(entries_with_mfe: pd.DataFrame) -> dict:
    """Summarise a :func:`gross_favorable_excursion` frame into the Stage-0 verdict.

    ``PASS_STAGE0`` when at least half the signals reach the fee, else ``DEAD_SUB_FEE``.
    """
    d = entries_with_mfe.dropna(subset=["mfe_pts"])
    n = int(len(d))
    if n == 0:
        return {"n_signals": 0, "verdict": "NO_SIGNALS"}
    frac_fee = float(d["clears_fee"].mean())
    return {
        "n_signals": n,
        "median_mfe_pts": float(d["mfe_pts"].median()),
        "mean_mfe_pts": float(d["mfe_pts"].mean()),
        "median_fee_pts": float(d["fee_pts"].median()),
        "median_cost_pts": float(d["cost_pts"].median()),
        "frac_clearing_fee": frac_fee,
        "frac_clearing_cost": float(d["clears_cost"].mean()),
        # a necessary, not sufficient, gate: passing does not confirm an edge
        "verdict": "PASS_STAGE0" if frac_fee >= 0.5 else "DEAD_SUB_FEE",
    }


def assert_fee_parity(run_dir: PathLike, tol_pct: float = 2.0) -> dict:
    """Check the Python fee mirror against a Rust run's ``trades.jsonl``.

    Compares :func:`round_trip_fee_points` at the mean exit price with the realized fee
    (mean entry plus mean exit ``fee_vnd``). Raises ``AssertionError`` if the drift exceeds
    ``tol_pct`` percent; returns the reconciliation numbers.
    """
    import json

    rows = [
        json.loads(l) for l in Path(run_dir, "trades.jsonl").read_text().splitlines() if l.strip()
    ]
    exits = [r for r in rows if r.get("action") == "exit"]
    enters = [r for r in rows if r.get("action") == "enter"]
    if not exits or not enters:
        raise ValueError(f"{run_dir}: need both enter and exit rows for parity")
    realized_rt_vnd = float(
        np.mean([r["fee_vnd"] for r in enters]) + np.mean([r["fee_vnd"] for r in exits])
    )
    realized_rt_pts = realized_rt_vnd / MULTIPLIER
    mean_price = float(np.mean([r["price"] for r in exits]))
    mirror_rt_pts = round_trip_fee_points(mean_price)
    drift_pct = abs(mirror_rt_pts - realized_rt_pts) / realized_rt_pts * 100.0
    if drift_pct > tol_pct:
        raise AssertionError(
            f"fee mirror drift {drift_pct:.2f}% > {tol_pct}% — reconcile fee_hurdle.py to fill.rs "
            f"(realized {realized_rt_pts:.4f}pt vs mirror {mirror_rt_pts:.4f}pt @ price {mean_price:.1f})"
        )
    return {
        "realized_rt_fee_pts": realized_rt_pts,
        "mirror_rt_fee_pts": mirror_rt_pts,
        "mean_exit_price": mean_price,
        "drift_pct": drift_pct,
    }
