"""Trade forensics: path and excursion analytics for round-trip trades (definitions: ADR 0040).

``mfe_pts`` / ``mae_pts`` are signed in the trade's direction and not clamped (``mae_abs`` is the
positive magnitude). Excursions are gross; costs come only from ``costs.py``. Path bars should be
finer than the decision timeframe. OHLC cannot order a bar's high and low, so ties resolve
adversely and ``ambiguous_bars`` counts them: barrier results are conservative bounds. Design
comparisons return rows in declared order with no argmax; :func:`barrier_grid` and
:func:`entry_delay_curve` also score a direction-randomised twin (``edge_over_twin``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from historical.backtest_engine.costs import (
    TICK,
    CostScenario,
    round_trip_cost_points,
    round_trip_fee_points,
)

ICT_OFFSET = 7 * 3600
EXPLORATORY = "[EXPLORATORY]"

__all__ = [
    "PathConfig",
    "attach_reference_price",
    "attach_excursions",
    "excursion_profile",
    "stop_survival_table",
    "replay_barriers",
    "barrier_grid",
    "family_stats",
    "holding_time_curve",
    "entry_delay_curve",
    "pullback_entry_curve",
    "edge_ratio",
    "attach_context",
    "conditional_table",
    "block_bootstrap_mean_ci",
    "ict_day",
    "hold_days_between",
]


# --- conventions and small mirrors ---
def ict_day(ts: int | float) -> int:
    """Return the ICT calendar-day index of a unix second.

    Mirrors ``research_lab.lab.ict_day`` (and ``FillSim``); duplicated because ``lab`` imports
    this package. ``test_trade_forensics.py::test_ict_day_parity_with_lab`` checks parity.
    """
    return int((int(ts) + ICT_OFFSET) // 86_400)


def hold_days_between(entry_ts: int | float, exit_ts: int | float) -> int:
    """Return the ICT calendar-day boundaries crossed (0 = intraday); mirrors ``lab``."""
    return max(0, ict_day(exit_ts) - ict_day(entry_ts))


def cost_points(
    price: float,
    hold_days: int,
    scenario: CostScenario,
    n_slip_fills: int = 2,
) -> float:
    """Return the round-trip fee plus ``n_slip_fills`` slipped fills, in points.

    The slip count is explicit because fills from a run scored with ``FillConfig::worse_side``
    already embed slippage. Callers pass ``embedded_slip_fills``, the number of input fills that
    embed it: 0 = raw bar prices, 1 = entry only, 2 = both (a scored run directory).
    """
    return round_trip_fee_points(price, hold_days=hold_days) + max(0, int(n_slip_fills)) * (
        scenario.slip_per_fill_points
    )


def attach_reference_price(trips: pd.DataFrame, slip_pts: float) -> pd.DataFrame:
    """Add ``ref_price``, the market level at entry with the fill's slippage removed.

    Excursions are measured from ``ref_price``; realized P&L still uses the fill. It is computed
    from the original side, so it survives :func:`_twin_trips` unchanged and the twin does not
    turn the embedded slippage into a bonus.
    """
    out = trips.copy()
    out["ref_price"] = out["entry_price"].to_numpy(dtype=float) - _dir_array(out) * float(slip_pts)
    return out


def _ref_price(trips: pd.DataFrame) -> np.ndarray:
    if "ref_price" in trips.columns:
        return trips["ref_price"].to_numpy(dtype=float)
    return trips["entry_price"].to_numpy(dtype=float)


@dataclass(frozen=True)
class PathConfig:
    """How a trade's price path is cut from the bar tape.

    ``include_entry_bar``: False starts at the first bar with ``ts_open >= entry_ts``; True also
    takes the bar containing the entry, which inflates MAE unless the fill was at its open.
    ``adverse_first``: when one bar could set both extremes, the adverse one is assumed first.
    """

    include_entry_bar: bool = False
    adverse_first: bool = True


def _bar_arrays(bars: pd.DataFrame) -> tuple[np.ndarray, ...]:
    b = bars
    if "ts_open" not in b.columns:
        b = b.reset_index()
    missing = {"ts_open", "high", "low", "close"} - set(b.columns)
    if missing:
        raise ValueError(f"bars is missing columns {sorted(missing)}")
    ts = b["ts_open"].to_numpy()
    if np.issubdtype(ts.dtype, np.datetime64):
        ts = ts.astype("datetime64[s]").astype(np.int64)
    ts = ts.astype(np.int64)
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    o = (
        b["open"].to_numpy(dtype=float)[order]
        if "open" in b.columns
        else b["close"].to_numpy(dtype=float)[order]
    )
    return (
        ts,
        o,
        b["high"].to_numpy(dtype=float)[order],
        b["low"].to_numpy(dtype=float)[order],
        b["close"].to_numpy(dtype=float)[order],
    )


def _dir_array(trips: pd.DataFrame) -> np.ndarray:
    s = trips["side"].astype(str).str.lower()
    bad = ~s.isin({"long", "short"})
    if bad.any():
        raise ValueError(f"side must be long/short; got {sorted(set(s[bad]))}")
    return np.where(s.to_numpy() == "long", 1.0, -1.0)


def _window(ts: np.ndarray, entry_ts: int, exit_ts: int, cfg: PathConfig) -> tuple[int, int]:
    """Half-open [i0, i1) index range of path bars for one trade."""
    if cfg.include_entry_bar:
        i0 = max(0, int(np.searchsorted(ts, entry_ts, side="right")) - 1)
    else:
        i0 = int(np.searchsorted(ts, entry_ts, side="left"))
    i1 = int(np.searchsorted(ts, exit_ts, side="right"))
    return i0, max(i0, i1)


# --- 1. excursions (descriptive) ---
def attach_excursions(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    cfg: PathConfig = PathConfig(),
) -> pd.DataFrame:
    """Return a copy of ``trips`` with per-trade excursion columns, in points unless noted.

    ``trips`` needs ``entry_ts, exit_ts, side, entry_price, exit_price``; ``bars`` is the path
    lane. Adds ``realized_gross_pts`` (no costs), signed ``mfe_pts`` / ``mae_pts``, ``mae_abs``,
    ``t_mfe_min`` / ``t_mae_min`` (minutes to the first bar setting each extreme), ``mfe_first``,
    ``etd_pts`` (give-back, ``mfe - realized``), ``capture`` (``realized / mfe``, at most 1),
    ``path_range``, ``total_efficiency`` (``realized / path_range``), ``ambiguous_bars``,
    ``path_bars`` (0 = no path, excursions NaN), ``hold_days``, and ``implied_cost_pts``
    (``realized - pnl_points``) when the engine's net P&L is present.
    """
    out = trips.copy()
    if out.empty:
        for c in (
            "realized_gross_pts mfe_pts mae_pts mae_abs t_mfe_min t_mae_min etd_pts "
            "capture path_range total_efficiency"
        ).split():
            out[c] = pd.Series(dtype=float)
        for c in ("path_bars", "ambiguous_bars", "hold_days"):
            out[c] = pd.Series(dtype=int)
        out["mfe_first"] = pd.Series(dtype=bool)
        return out

    ts, _o, hi, lo, _c = _bar_arrays(bars)
    dirn = _dir_array(out)
    e_ts = out["entry_ts"].to_numpy(dtype=np.int64)
    x_ts = out["exit_ts"].to_numpy(dtype=np.int64)
    e_px = out["entry_price"].to_numpy(dtype=float)  # the fill: used for P&L
    r_px = _ref_price(out)  # market level at entry: used for excursions
    x_px = out["exit_price"].to_numpy(dtype=float)

    n = len(out)
    mfe = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    t_mfe = np.full(n, np.nan)
    t_mae = np.full(n, np.nan)
    rng_ = np.full(n, np.nan)
    amb = np.zeros(n, dtype=np.int64)
    nbars = np.zeros(n, dtype=np.int64)

    for k in range(n):
        i0, i1 = _window(ts, e_ts[k], x_ts[k], cfg)
        nbars[k] = i1 - i0
        if i1 <= i0:
            continue
        h = hi[i0:i1]
        low = lo[i0:i1]
        t = ts[i0:i1]
        if dirn[k] > 0:
            fav, adv = h - r_px[k], low - r_px[k]
        else:
            fav, adv = r_px[k] - low, r_px[k] - h
        j_f = int(np.argmax(fav))
        j_a = int(np.argmin(adv))
        mfe[k] = float(fav[j_f])
        mae[k] = float(adv[j_a])
        t_mfe[k] = (t[j_f] - e_ts[k]) / 60.0
        t_mae[k] = (t[j_a] - e_ts[k]) / 60.0
        rng_[k] = float(h.max() - low.min())
        # a bar is ambiguous if it extends both running extremes (seeded at the entry, 0.0):
        # its unknown high/low order decides which barrier a simulation would hit
        prev_f = np.concatenate(([0.0], np.maximum.accumulate(fav)[:-1]))
        prev_a = np.concatenate(([0.0], np.minimum.accumulate(adv)[:-1]))
        amb[k] = int(np.count_nonzero((fav > prev_f) & (adv < prev_a)))

    gross = dirn * (x_px - e_px)
    out["realized_gross_pts"] = gross
    out["mfe_pts"] = mfe
    out["mae_pts"] = mae
    out["mae_abs"] = np.where(np.isnan(mae), np.nan, -np.minimum(mae, 0.0))
    out["t_mfe_min"] = t_mfe
    out["t_mae_min"] = t_mae
    out["mfe_first"] = t_mfe < t_mae
    out["etd_pts"] = mfe - gross
    with np.errstate(divide="ignore", invalid="ignore"):
        # guarded at one tick: smaller denominators give meaningless, extreme ratios
        out["capture"] = np.where(mfe > TICK, gross / mfe, np.nan)
        out["total_efficiency"] = np.where(rng_ > TICK, gross / rng_, np.nan)
    out["path_range"] = rng_
    out["ambiguous_bars"] = amb
    out["path_bars"] = nbars
    out["hold_days"] = [hold_days_between(a, b) for a, b in zip(e_ts, x_ts)]
    if "pnl_points" in out.columns:
        out["implied_cost_pts"] = gross - out["pnl_points"].to_numpy(dtype=float)
    return out


def excursion_profile(
    trips_x: pd.DataFrame,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    quantiles: Sequence[float] = (0.05, 0.25, 0.50, 0.75, 0.95),
) -> dict:
    """Summarise an excursion frame overall and split into winners and losers.

    A stop is informative only if ``mae_abs`` of winners at high quantiles (heat taken by trades
    that worked) stays below ``mae_abs`` of losers at low quantiles; when they overlap, no stop
    separates them. ``frac_mfe_over_cost`` is the share of trades whose MFE exceeded the
    round-trip cost under ``scenario``.
    """
    d = trips_x.dropna(subset=["mfe_pts", "mae_pts"])
    if d.empty:
        return {"n": 0}
    holds = d["hold_days"] if "hold_days" in d.columns else pd.Series(0, index=d.index)
    cost = np.array(
        [
            round_trip_cost_points(p, hold_days=int(h), scenario=scenario)
            for p, h in zip(d["entry_price"], holds)
        ]
    )
    win = d["realized_gross_pts"] > 0

    def q(s: pd.Series) -> dict:
        return {f"p{int(x * 100):02d}": float(s.quantile(x)) for x in quantiles}

    return {
        "stamp": EXPLORATORY,
        "n": int(len(d)),
        "n_zero_path": int((d["path_bars"] == 0).sum()),
        "ambiguous_bars_total": int(d["ambiguous_bars"].sum()),
        "ambiguous_frac_of_trades": float((d["ambiguous_bars"] > 0).mean()),
        "scenario": scenario.value.name,
        "cost_pts_mean": float(cost.mean()),
        "mfe": {"mean": float(d["mfe_pts"].mean()), **q(d["mfe_pts"])},
        "mae_abs": {"mean": float(d["mae_abs"].mean()), **q(d["mae_abs"])},
        "etd": {"mean": float(d["etd_pts"].mean()), **q(d["etd_pts"])},
        # capture has small denominators, so only order statistics are reported
        "capture": {"mean": float("nan"), **q(d["capture"].dropna())},
        "capture_n_defined": int(d["capture"].notna().sum()),
        "t_mfe_min_median": float(d["t_mfe_min"].median()),
        "t_mae_min_median": float(d["t_mae_min"].median()),
        "mfe_first_frac": float(d["mfe_first"].mean()),
        "winners": {
            "n": int(win.sum()),
            "mae_abs": q(d.loc[win, "mae_abs"]) if win.any() else {},
            "mfe": q(d.loc[win, "mfe_pts"]) if win.any() else {},
        },
        "losers": {
            "n": int((~win).sum()),
            "mae_abs": q(d.loc[~win, "mae_abs"]) if (~win).any() else {},
            "mfe": q(d.loc[~win, "mfe_pts"]) if (~win).any() else {},
        },
        "frac_mfe_over_cost": float((d["mfe_pts"].to_numpy() > cost).mean()),
        "frac_realized_over_cost": float((d["realized_gross_pts"].to_numpy() > cost).mean()),
    }


def stop_survival_table(trips_x: pd.DataFrame, stops_pts: Sequence[float]) -> pd.DataFrame:
    """Return, per stop distance, the winners it would have killed and the losers it would have cut.

    Read off the MAE distribution without simulation; :func:`replay_barriers` is the simulation.
    """
    d = trips_x.dropna(subset=["mae_abs"])
    win = d["realized_gross_pts"] > 0
    rows = []
    for s in stops_pts:
        hit = d["mae_abs"] >= s
        rows.append(
            {
                "stop_pts": float(s),
                "n_hit": int(hit.sum()),
                "frac_hit": float(hit.mean()),
                "winners_killed": int((hit & win).sum()),
                "frac_winners_killed": float((hit & win).sum() / max(int(win.sum()), 1)),
                "losers_cut": int((hit & ~win).sum()),
                "frac_losers_cut": float((hit & ~win).sum() / max(int((~win).sum()), 1)),
                "gross_pts_of_winners_killed": float(d.loc[hit & win, "realized_gross_pts"].sum()),
                "gross_pts_of_losers_cut": float(d.loc[hit & ~win, "realized_gross_pts"].sum()),
            }
        )
    return pd.DataFrame(rows)


# --- 2. counterfactual exits (a family, not a choice) ---
def replay_barriers(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    stop_pts: Optional[float] = None,
    target_pts: Optional[float] = None,
    trail_pts: Optional[float] = None,
    stop_atr: Optional[float] = None,
    target_atr: Optional[float] = None,
    trail_atr: Optional[float] = None,
    time_stop_min: Optional[float] = None,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    embedded_slip_fills: int = 0,
    atr_col: str = "atr_entry",
    cfg: PathConfig = PathConfig(),
) -> pd.DataFrame:
    """Re-exit each trade under a barrier rule, holding the entries fixed.

    A barrier fills at its level, or at the bar open if the bar gaps through it; within one bar,
    priority is stop, trail, time, target (``cfg.adverse_first``). The trail ratchets on prior
    bars only, seeded at the entry, and with no trigger the original exit stands. Distances are
    ``*_pts`` (index points) or ``*_atr`` (multiples of ``atr_col``, from :func:`attach_context`),
    never both for one barrier. ``embedded_slip_fills`` is as in :func:`cost_points`; a replaced
    exit pays one slipped fill. Adds ``exit_kind`` (stop, target, trail, time or original), the
    new exit, ``gross_pts``, ``cost_pts``, ``net_pts`` and ``hold_days``.
    """
    if trips.empty:
        return trips.copy()
    ts, op, hi, lo, cl = _bar_arrays(bars)
    dirn = _dir_array(trips)
    e_ts = trips["entry_ts"].to_numpy(dtype=np.int64)
    x_ts = trips["exit_ts"].to_numpy(dtype=np.int64)
    e_px = trips["entry_price"].to_numpy(dtype=float)
    x_px = trips["exit_price"].to_numpy(dtype=float)

    n = len(trips)
    new_px = x_px.astype(float).copy()
    new_ts = x_ts.copy()
    kind = np.array(["original"] * n, dtype=object)

    def _levels(pts: Optional[float], mult: Optional[float], name: str) -> np.ndarray:
        if pts is not None and mult is not None:
            raise ValueError(f"{name}: give a points distance or an ATR multiple, not both")
        if pts is not None:
            return np.full(n, abs(float(pts)))
        if mult is None:
            return np.full(n, np.nan)
        if atr_col not in trips.columns:
            raise ValueError(
                f"{name} is an ATR multiple but {atr_col!r} is missing; "
                "run attach_context(trips, bars) first"
            )
        return np.abs(trips[atr_col].to_numpy(dtype=float) * float(mult))

    stop_lv = _levels(stop_pts, stop_atr, "stop")
    target_lv = _levels(target_pts, target_atr, "target")
    trail_lv = _levels(trail_pts, trail_atr, "trail")

    for k in range(n):
        i0, i1 = _window(ts, e_ts[k], x_ts[k], cfg)
        if i1 <= i0:
            continue
        d = dirn[k]
        h, low, o, t = hi[i0:i1], lo[i0:i1], op[i0:i1], ts[i0:i1]
        # favourable / adverse extremes of each bar, in trade coordinates
        fav = (h - e_px[k]) if d > 0 else (e_px[k] - low)
        adv = (low - e_px[k]) if d > 0 else (e_px[k] - h)
        o_pts = d * (o - e_px[k])

        cand: list[tuple[int, str, float]] = []
        s_lv, t_lv, r_lv = stop_lv[k], target_lv[k], trail_lv[k]
        if np.isfinite(s_lv):
            j = np.flatnonzero(adv <= -s_lv)
            if j.size:
                i = int(j[0])
                px = e_px[k] - d * s_lv
                if o_pts[i] <= -s_lv:  # gapped through the level
                    px = float(o[i])
                cand.append((i, "stop", float(px)))
        if np.isfinite(t_lv):
            j = np.flatnonzero(fav >= t_lv)
            if j.size:
                i = int(j[0])
                px = e_px[k] + d * t_lv
                if o_pts[i] >= t_lv:
                    px = float(o[i])
                cand.append((i, "target", float(px)))
        if np.isfinite(r_lv):
            peak = np.maximum.accumulate(fav)
            prev_peak = np.concatenate(([0.0], peak[:-1]))  # seeded at the entry level
            j = np.flatnonzero(adv <= prev_peak - r_lv)
            if j.size:
                i = int(j[0])
                lvl = float(prev_peak[i] - r_lv)
                px = e_px[k] + d * lvl
                if o_pts[i] <= lvl:
                    px = float(o[i])
                cand.append((i, "trail", float(px)))
        if time_stop_min is not None:
            j = np.flatnonzero(t >= e_ts[k] + time_stop_min * 60)
            if j.size:
                i = int(j[0])
                cand.append((i, "time", float(cl[i0 + i])))

        if not cand:
            continue
        first = min(c[0] for c in cand)
        same = [c for c in cand if c[0] == first]
        if len(same) > 1 and cfg.adverse_first:
            order = {"stop": 0, "trail": 1, "time": 2, "target": 3}
            same.sort(key=lambda c: order.get(c[1], 9))
        i, kd, px = same[0]
        new_ts[k] = int(t[i])
        new_px[k] = px
        kind[k] = kd

    gross = dirn * (new_px - e_px)
    hold = np.array([hold_days_between(a, b) for a, b in zip(e_ts, new_ts)])
    entry_slipped = int(embedded_slip_fills) >= 1
    cost = np.array(
        [
            cost_points(
                p,
                int(hd),
                scenario,
                n_slip_fills=(0 if entry_slipped else 1)
                + (0 if (int(embedded_slip_fills) >= 2 and kd == "original") else 1),
            )
            for p, hd, kd in zip(e_px, hold, kind)
        ]
    )
    out = trips.copy()
    out["exit_kind"] = kind
    out["new_exit_ts"] = new_ts
    out["new_exit_price"] = new_px
    out["gross_pts"] = gross
    out["cost_pts"] = cost
    out["net_pts"] = gross - cost
    out["hold_days"] = hold
    return out


def _twin_trips(trips: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Return the direction-randomised twin: the same trades with each side set by a coin flip.

    A rule that scores as well on the twin harvests the shape of the price series, not the signal.
    """
    rng = np.random.default_rng(int(seed))
    t = trips.copy()
    flip = rng.integers(0, 2, size=len(t)) == 1
    side = t["side"].astype(str).str.lower().to_numpy()
    t["side"] = np.where(flip, np.where(side == "long", "short", "long"), side)
    return t


def barrier_grid(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    grid: Sequence[Mapping[str, Optional[float]]],
    *,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    embedded_slip_fills: int = 0,
    twin_seed: int = 12345,
    cfg: PathConfig = PathConfig(),
) -> pd.DataFrame:
    """Score a declared grid of exit rules, each against its own twin.

    Rows keep the declared order, with no sort, argmax or ``best`` column: the grid is a family
    to summarise with :func:`family_stats`, not a menu to pick a cell from.
    """
    keys = (
        "stop_pts",
        "target_pts",
        "trail_pts",
        "stop_atr",
        "target_atr",
        "trail_atr",
        "time_stop_min",
    )
    twin = _twin_trips(trips, twin_seed)
    rows = []
    for cell in grid:
        kw = {k: cell.get(k) for k in keys}
        common = dict(scenario=scenario, embedded_slip_fills=embedded_slip_fills, cfg=cfg)
        real = replay_barriers(trips, bars, **common, **kw)
        tw = replay_barriers(twin, bars, **common, **kw)
        net = real["net_pts"].to_numpy()
        tnet = tw["net_pts"].to_numpy()
        rows.append(
            {
                **{k: cell.get(k) for k in keys},
                "n": int(len(real)),
                "net_pts_per_trade": float(net.mean()),
                "gross_pts_per_trade": float(real["gross_pts"].mean()),
                "hit_rate": float((real["gross_pts"] > 0).mean()),
                "se_cell": float(net.std(ddof=1) / np.sqrt(len(net))) if len(net) > 1 else np.nan,
                "twin_net_pts_per_trade": float(tnet.mean()),
                "edge_over_twin": float(net.mean() - tnet.mean()),
                "frac_exit_stop": float((real["exit_kind"] == "stop").mean()),
                "frac_exit_target": float((real["exit_kind"] == "target").mean()),
                "frac_exit_trail": float((real["exit_kind"] == "trail").mean()),
                "frac_exit_time": float((real["exit_kind"] == "time").mean()),
                "frac_exit_original": float((real["exit_kind"] == "original").mean()),
                "stamp": EXPLORATORY,
            }
        )
    return pd.DataFrame(rows)


def family_stats(grid_df: pd.DataFrame, score_col: str = "net_pts_per_trade") -> dict:
    """Return the family statistics of a grid, plus twin-adjusted means when available.

    ``F_mean``: mean score over all cells. ``F_frac_pos``: share of cells scoring above 0.
    ``F_spread_over_SE``: (max - min) over the median cell SE; the pre-registration treats a value
    above about 2 as a noise surface.
    """
    s = grid_df[score_col].to_numpy(dtype=float)
    se = float(np.nanmedian(grid_df["se_cell"].to_numpy(dtype=float)))
    out = {
        "stamp": EXPLORATORY,
        "n_cells": int(len(s)),
        "F_mean": float(np.nanmean(s)),
        "F_frac_pos": float(np.nanmean(s > 0)),
        "F_spread": float(np.nanmax(s) - np.nanmin(s)),
        "SE_cell_median": se,
        "F_spread_over_SE": float((np.nanmax(s) - np.nanmin(s)) / se) if se > 0 else float("nan"),
    }
    if "edge_over_twin" in grid_df.columns:
        e = grid_df["edge_over_twin"].to_numpy(dtype=float)
        out["F_mean_edge_over_twin"] = float(np.nanmean(e))
        out["F_frac_pos_edge_over_twin"] = float(np.nanmean(e > 0))
    return out


# --- 3. entry-side questions (exit-independent) ---
def holding_time_curve(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    horizons_min: Sequence[float] = (5, 15, 30, 60, 120, 240, 480),
    *,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    embedded_slip_fills: int = 0,
) -> pd.DataFrame:
    """Return gross, cost and net per trade if every trade closed ``h`` minutes after entry.

    Replacing the exit with a clock isolates the horizon at which the entry carries information
    and the horizon at which that information clears cost.
    """
    ts, _o, _h, _l, cl = _bar_arrays(bars)
    dirn = _dir_array(trips)
    e_ts = trips["entry_ts"].to_numpy(dtype=np.int64)
    e_px = trips["entry_price"].to_numpy(dtype=float)
    rows = []
    for hmin in horizons_min:
        g, c = [], []
        for k in range(len(trips)):
            i = int(np.searchsorted(ts, e_ts[k] + hmin * 60, side="left"))
            if i >= len(ts):
                continue
            g.append(dirn[k] * (cl[i] - e_px[k]))
            # the exit (a raw bar close) always pays a slipped fill; the entry pays one
            # only if its price does not already embed it
            c.append(
                cost_points(
                    e_px[k],
                    hold_days_between(e_ts[k], ts[i]),
                    scenario,
                    n_slip_fills=(0 if int(embedded_slip_fills) >= 1 else 1) + 1,
                )
            )
        if not g:
            continue
        g_arr, c_arr = np.array(g), np.array(c)
        rows.append(
            {
                "horizon_min": float(hmin),
                "n": len(g),
                "gross_pts_per_trade": float(g_arr.mean()),
                "cost_pts_per_trade": float(c_arr.mean()),
                "net_pts_per_trade": float((g_arr - c_arr).mean()),
                "hit_rate": float((g_arr > 0).mean()),
                "stamp": EXPLORATORY,
            }
        )
    return pd.DataFrame(rows)


def entry_delay_curve(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    delays_min: Sequence[float] = (0, 1, 2, 3, 5, 10, 15, 30),
    *,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    embedded_slip_fills: int = 0,
    twin_seed: int = 12345,
) -> pd.DataFrame:
    """Return net per trade with the exit held fixed and the entry delayed by ``d`` minutes.

    Each delay is also scored on the direction-randomised twin; a delay that helps the twin as
    much is a property of the tape, not a fix for the signal. ``n_dropped`` counts trades whose
    delayed entry would land at or after their exit.
    """
    ts, _o, _h, _l, cl = _bar_arrays(bars)
    twin = _twin_trips(trips, twin_seed)

    def _score(t: pd.DataFrame, dmin: float) -> tuple[float, int, int, float]:
        dirn = _dir_array(t)
        e_ts = t["entry_ts"].to_numpy(dtype=np.int64)
        x_ts = t["exit_ts"].to_numpy(dtype=np.int64)
        x_px = t["exit_price"].to_numpy(dtype=float)
        e_px = t["entry_price"].to_numpy(dtype=float)
        nets, dropped, hits = [], 0, 0
        # the exit is the original fill; a delayed entry is a raw bar close and pays its own slip
        exit_slipped = int(embedded_slip_fills) >= 2
        for k in range(len(t)):
            if dmin == 0:
                px, ets = float(e_px[k]), int(e_ts[k])
                entry_slipped = int(embedded_slip_fills) >= 1
            else:
                i = int(np.searchsorted(ts, e_ts[k] + dmin * 60, side="left"))
                if i >= len(ts) or ts[i] >= x_ts[k]:
                    dropped += 1
                    continue
                px, ets = float(cl[i]), int(ts[i])
                entry_slipped = False
            g = dirn[k] * (x_px[k] - px)
            nets.append(
                g
                - cost_points(
                    px,
                    hold_days_between(ets, x_ts[k]),
                    scenario,
                    n_slip_fills=(0 if entry_slipped else 1) + (0 if exit_slipped else 1),
                )
            )
            hits += int(g > 0)
        if not nets:
            return float("nan"), 0, dropped, float("nan")
        return float(np.mean(nets)), len(nets), dropped, hits / len(nets)

    rows = []
    for d in delays_min:
        net, n, dropped, hit = _score(trips, d)
        tnet, _, _, _ = _score(twin, d)
        rows.append(
            {
                "delay_min": float(d),
                "n": n,
                "n_dropped": dropped,
                "net_pts_per_trade": net,
                "hit_rate": hit,
                "twin_net_pts_per_trade": tnet,
                "edge_over_twin": net - tnet,
                "stamp": EXPLORATORY,
            }
        )
    return pd.DataFrame(rows)


def pullback_entry_curve(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    pullback_pts: Sequence[float] = (0.5, 1.0, 2.0, 3.0),
    *,
    window_min: float = 30.0,
    scenario: CostScenario = CostScenario.WORSE_SIDE,
    embedded_slip_fills: int = 0,
) -> pd.DataFrame:
    """Return net per trade when entering only on a ``p``-point retrace within ``window_min``.

    The rule is selective (it refuses trades that run away at once), so both books are reported:
    ``net_all_signals`` counts refused signals as 0 and is comparable with the unfiltered book;
    ``net_filled`` is the conditional average over filled trades.
    """
    ts, _o, hi, lo, _c = _bar_arrays(bars)
    dirn = _dir_array(trips)
    e_ts = trips["entry_ts"].to_numpy(dtype=np.int64)
    x_ts = trips["exit_ts"].to_numpy(dtype=np.int64)
    e_px = trips["entry_price"].to_numpy(dtype=float)
    x_px = trips["exit_price"].to_numpy(dtype=float)
    rows = []
    for p in pullback_pts:
        nets, filled_nets = [], []
        for k in range(len(trips)):
            i0 = int(np.searchsorted(ts, e_ts[k], side="left"))
            i1 = int(np.searchsorted(ts, min(e_ts[k] + window_min * 60, x_ts[k]), side="right"))
            if i1 <= i0:
                continue
            lvl = float(e_px[k] - dirn[k] * abs(p))
            adverse = lo[i0:i1] <= lvl if dirn[k] > 0 else hi[i0:i1] >= lvl
            j = np.flatnonzero(adverse)
            if not j.size:
                nets.append(0.0)  # signal refused: no trade, no cost
                continue
            fts = int(ts[i0 + int(j[0])])
            g = dirn[k] * (x_px[k] - lvl)
            # the resting limit at lvl is a maker fill, conservatively charged a taker slip
            net = g - cost_points(
                lvl,
                hold_days_between(fts, x_ts[k]),
                scenario,
                n_slip_fills=1 + (0 if int(embedded_slip_fills) >= 2 else 1),
            )
            nets.append(net)
            filled_nets.append(net)
        if not nets:
            continue
        arr = np.array(nets)
        rows.append(
            {
                "pullback_pts": float(p),
                "n_signals": int(len(arr)),
                "n_filled": int(len(filled_nets)),
                "fill_rate": float(len(filled_nets) / len(arr)),
                "net_all_signals": float(arr.mean()),
                "net_filled": float(np.mean(filled_nets)) if filled_nets else float("nan"),
                "stamp": EXPLORATORY,
            }
        )
    return pd.DataFrame(rows)


def edge_ratio(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    horizons_min: Sequence[float] = (15, 30, 60, 120, 240),
    *,
    normaliser: Optional[str] = None,
    twin_seed: int = 12345,
) -> pd.DataFrame:
    """Return Faith's E-ratio per horizon: mean favourable over mean adverse excursion from entry.

    Exit-independent, so it separates "no entry edge" from "the exit wastes it". ``normaliser``
    (e.g. an ATR column) scales each trade's excursions before averaging so volatility regimes
    are comparable. E > 1 means more favourable than adverse movement; the coin-flip twin sits
    near 1 on a symmetric tape, and a real entry edge must beat it.
    """
    ts, _o, hi, lo, _c = _bar_arrays(bars)

    def _e(t: pd.DataFrame, hmin: float) -> tuple[float, int]:
        dirn = _dir_array(t)
        e_ts = t["entry_ts"].to_numpy(dtype=np.int64)
        e_px = _ref_price(t)  # market at entry, so the twin is not handed the slip
        nrm = (
            t[normaliser].to_numpy(dtype=float)
            if normaliser and normaliser in t.columns
            else np.ones(len(t))
        )
        favs, advs = [], []
        for k in range(len(t)):
            i0 = int(np.searchsorted(ts, e_ts[k], side="left"))
            i1 = int(np.searchsorted(ts, e_ts[k] + hmin * 60, side="right"))
            if i1 <= i0 or not np.isfinite(nrm[k]) or nrm[k] <= 0:
                continue
            h, low = hi[i0:i1], lo[i0:i1]
            fav = (h.max() - e_px[k]) if dirn[k] > 0 else (e_px[k] - low.min())
            adv = (e_px[k] - low.min()) if dirn[k] > 0 else (h.max() - e_px[k])
            favs.append(max(fav, 0.0) / nrm[k])
            advs.append(max(adv, 0.0) / nrm[k])
        if not favs:
            return float("nan"), 0
        mfav, madv = float(np.mean(favs)), float(np.mean(advs))
        return (mfav / madv if madv > 0 else float("inf")), len(favs)

    twin = _twin_trips(trips, twin_seed)
    rows = []
    for hmin in horizons_min:
        e, n = _e(trips, hmin)
        te, _ = _e(twin, hmin)
        rows.append(
            {
                "horizon_min": float(hmin),
                "n": n,
                "e_ratio": e,
                "twin_e_ratio": te,
                "e_over_twin": e - te,
                "normalised_by": normaliser or "none",
                "stamp": EXPLORATORY,
            }
        )
    return pd.DataFrame(rows)


# --- 4. conditioning ---
def attach_context(
    trips: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    atr_period: int = 20,
    atr_bar_seconds: int = 60,
    n_vol_buckets: int = 4,
) -> pd.DataFrame:
    """Add causal context at each entry, computed from bars strictly before the entry bar.

    ``atr_entry`` is the Wilder ATR of the last completed ``atr_bar_seconds`` bar before entry;
    use the decision timeframe, not the path lane, or ATR-scaled barriers become tick-scale.
    ``atr_bucket`` is the ``n_vol_buckets`` quantile bucket of ``atr_entry`` within this trade set;
    ``sod``, ``hour``, ``segment``, ``weekday`` and ``ict_day`` are ICT clock context.
    """
    ts, _o, hi, lo, cl = _bar_arrays(bars)
    if atr_bar_seconds > 1:
        bucket = ts // int(atr_bar_seconds)
        agg = (
            pd.DataFrame({"b": bucket, "ts": ts, "high": hi, "low": lo, "close": cl})
            .groupby("b", sort=True)
            .agg(
                ts=("ts", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
            )
        )
        ts = agg["ts"].to_numpy(dtype=np.int64)
        hi = agg["high"].to_numpy(dtype=float)
        lo = agg["low"].to_numpy(dtype=float)
        cl = agg["close"].to_numpy(dtype=float)
    prev_close = np.concatenate(([cl[0]], cl[:-1]))
    tr = np.maximum(hi - lo, np.maximum(np.abs(hi - prev_close), np.abs(lo - prev_close)))
    atr = pd.Series(tr).ewm(alpha=1.0 / atr_period, adjust=False).mean().to_numpy()

    out = trips.copy()
    e_ts = out["entry_ts"].to_numpy(dtype=np.int64)
    idx = np.searchsorted(ts, e_ts, side="left") - 1  # strictly before the entry bar
    valid = idx >= 0
    a = np.full(len(out), np.nan)
    a[valid] = atr[idx[valid]]
    out["atr_entry"] = a
    if np.isfinite(a).sum() >= n_vol_buckets:
        out["atr_bucket"] = pd.qcut(
            pd.Series(a, index=out.index), n_vol_buckets, labels=False, duplicates="drop"
        )
    else:
        out["atr_bucket"] = np.nan
    sod = (e_ts + ICT_OFFSET) % 86_400
    out["sod"] = sod
    out["hour"] = sod // 3600
    out["segment"] = np.where(sod < 41_400, "morning", np.where(sod < 46_800, "lunch", "afternoon"))
    # day names rather than indices, so a conditioning table cannot be read off by one
    dow = ((e_ts + ICT_OFFSET) // 86_400 + 3) % 7  # 0 = Monday (epoch day 0 is a Thursday)
    out["weekday"] = np.array(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])[dow]
    out["ict_day"] = (e_ts + ICT_OFFSET) // 86_400
    return out


def block_bootstrap_mean_ci(
    values: np.ndarray,
    blocks: np.ndarray,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[float, float]:
    """Return the ``(lo, hi)`` block-bootstrap CI of a mean, resampling whole blocks.

    ``blocks`` labels each observation's block (the session day); an i.i.d. bootstrap over
    overlapping intraday trades gives intervals that are far too narrow.
    """
    v = np.asarray(values, dtype=float)
    b = np.asarray(blocks)
    ok = np.isfinite(v)
    v, b = v[ok], b[ok]
    if v.size == 0:
        return (float("nan"), float("nan"))
    uniq = np.unique(b)
    groups = [v[b == u] for u in uniq]
    rng = np.random.default_rng(int(seed))
    means = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.integers(0, len(groups), size=len(groups))
        means[i] = np.concatenate([groups[j] for j in pick]).mean()
    return (
        float(np.quantile(means, alpha / 2)),
        float(np.quantile(means, 1 - alpha / 2)),
    )


def block_bootstrap_contrast_ci(
    values: np.ndarray,
    in_cell: np.ndarray,
    blocks: np.ndarray,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[float, float, float]:
    """Return ``(diff, lo, hi)``: ``mean(cell) - mean(rest)`` and its block-bootstrap CI.

    A filter needs this contrast, not the cell's level: on a book with a positive edge every cell
    can exclude zero while none differs from the rest.
    """
    v = np.asarray(values, dtype=float)
    m = np.asarray(in_cell, dtype=bool)
    b = np.asarray(blocks)
    ok = np.isfinite(v)
    v, m, b = v[ok], m[ok], b[ok]
    if v.size == 0 or m.all() or not m.any():
        return (float("nan"), float("nan"), float("nan"))
    uniq = np.unique(b)
    gv = [v[b == u] for u in uniq]
    gm = [m[b == u] for u in uniq]
    rng = np.random.default_rng(int(seed))
    diffs = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(gv), size=len(gv))
        vv = np.concatenate([gv[j] for j in pick])
        mm = np.concatenate([gm[j] for j in pick])
        if mm.all() or not mm.any():
            continue
        diffs.append(vv[mm].mean() - vv[~mm].mean())
    if not diffs:
        return (float("nan"), float("nan"), float("nan"))
    d = np.asarray(diffs)
    return (
        float(v[m].mean() - v[~m].mean()),
        float(np.quantile(d, alpha / 2)),
        float(np.quantile(d, 1 - alpha / 2)),
    )


def conditional_table(
    trips: pd.DataFrame,
    by: str,
    *,
    value: str = "net_pts",
    block: str = "ict_day",
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> pd.DataFrame:
    """Return per-cell means of ``value`` with block-bootstrap CIs, plain and Sidak-corrected.

    Acting on a split selects among ``k`` cells, so the Sidak level
    ``alpha_adj = 1 - (1 - alpha)^(1/k)`` governs; ``vs_rest`` is the contrast with the other
    cells, and ``n`` / ``n_blocks`` show how thin each cell is.
    """
    d = trips.dropna(subset=[by, value]).copy()
    if d.empty:
        return pd.DataFrame()
    cells = sorted(d[by].dropna().unique(), key=str)
    k = max(len(cells), 1)
    alpha_adj = 1.0 - (1.0 - alpha) ** (1.0 / k)
    blk = d[block].to_numpy() if block in d.columns else np.arange(len(d))
    rows = []
    for c in cells:
        m = (d[by] == c).to_numpy()
        v = d.loc[m, value].to_numpy(dtype=float)
        lo_, hi_ = block_bootstrap_mean_ci(v, blk[m], n_boot=n_boot, alpha=alpha, seed=seed)
        lo_a, hi_a = block_bootstrap_mean_ci(v, blk[m], n_boot=n_boot, alpha=alpha_adj, seed=seed)
        all_v = d[value].to_numpy(dtype=float)
        diff, dlo, dhi = block_bootstrap_contrast_ci(
            all_v, m, blk, n_boot=n_boot, alpha=alpha, seed=seed
        )
        _, dlo_a, dhi_a = block_bootstrap_contrast_ci(
            all_v, m, blk, n_boot=n_boot, alpha=alpha_adj, seed=seed
        )
        rows.append(
            {
                by: c,
                "n": int(m.sum()),
                "n_blocks": int(len(np.unique(blk[m]))),
                "mean": float(v.mean()),
                "ci_lo": lo_,
                "ci_hi": hi_,
                "excludes_zero": bool(lo_ > 0 or hi_ < 0),
                "ci_lo_sidak": lo_a,
                "ci_hi_sidak": hi_a,
                "excludes_zero_sidak": bool(lo_a > 0 or hi_a < 0),
                # the contrast, not the level, justifies a filter
                "vs_rest": diff,
                "vs_rest_ci_lo": dlo,
                "vs_rest_ci_hi": dhi,
                "differs_from_rest": bool(dlo > 0 or dhi < 0),
                "differs_from_rest_sidak": bool(dlo_a > 0 or dhi_a < 0),
                "stamp": EXPLORATORY,
            }
        )
    out = pd.DataFrame(rows)
    out.attrs["k_cells"] = k
    out.attrs["alpha"] = alpha
    out.attrs["alpha_sidak"] = alpha_adj
    return out
