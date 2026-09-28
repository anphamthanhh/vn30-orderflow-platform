"""Daily bars, calendar tags and the overnight/intraday return split for the daily screens.

Aggregates the M1 history (``data_provider/VN30F1M_Final.csv``, the same data as ClickHouse
``default.ohlcv_m1``) to one row per ICT trading day. VN30F1M is a front-month continuous series,
so the overnight return after an expiry day includes the contract roll; such rows are flagged
``roll_boundary`` and the screens exclude them. Pure pandas, no network access.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
M1_CSV = REPO_ROOT / "data_provider" / "VN30F1M_Final.csv"


def load_m1(path: Path | str = M1_CSV) -> pd.DataFrame:
    """Load the M1 CSV (timestamp, ticker, OHLCV) and add ``ts`` and ``date`` columns."""
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["ts"].dt.date
    return df


def assert_dev_only_daily(daily: pd.DataFrame, allow_holdout: bool = False) -> None:
    """Raise ``AssertionError`` if any daily bar falls in a reserved holdout window.

    ``load_m1`` reads the full CSV, which spans the reserved range, so every
    :func:`daily_bars` call is checked. ``allow_holdout=True`` is for a final verdict only
    (see ``PRE_REGISTRATION.md``).
    """
    if allow_holdout or daily.empty:
        return
    # lazy import keeps this module independent of the lab toolkit
    from historical.research_lab import lab as _lab

    idx = pd.DatetimeIndex(daily.index)
    # epoch subtraction gives exact day numbers at any datetime resolution (pandas 3 uses µs)
    days = ((idx.normalize() - pd.Timestamp("1970-01-01")).days).to_numpy(dtype="int64")
    for f, t in _lab.holdout_ranges():
        hi = t if t is not None else np.iinfo(np.int64).max
        hit = (days >= f) & (days <= hi)
        if hit.any():
            first = idx[hit][0].date()
            raise AssertionError(
                f"daily bars include HOLDOUT data (first offending session {first}, "
                f"reserved range [{f}, {t}]). This lane reads the full CSV, which "
                "spans the reserved window. Reserved sessions are for the final "
                "verdict only — see PRE_REGISTRATION §A12. Pass allow_holdout=True "
                "only with an operator-authorised decision-log entry (§A12.4)."
            )


def daily_bars(m1: pd.DataFrame, *, allow_holdout: bool = False) -> pd.DataFrame:
    """Aggregate M1 bars to one OHLCV row per ICT trading day (date index, ascending).

    Raises ``AssertionError`` on reserved holdout dates unless ``allow_holdout=True``.
    """
    g = m1.sort_values("ts").groupby("date")
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "n_bars": g.size(),
        }
    )
    out.index = pd.to_datetime(out.index)
    out = out.sort_index()
    assert_dev_only_daily(out, allow_holdout=allow_holdout)
    return out


def third_thursday(year: int, month: int) -> pd.Timestamp:
    """Return the nominal VN30F1M expiry: the third Thursday of the month."""
    d = pd.Timestamp(year=year, month=month, day=1)
    # weekday(): Mon=0 .. Thu=3.
    first_thu = d + pd.Timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + pd.Timedelta(days=14)


def expiry_days(daily: pd.DataFrame) -> pd.Series:
    """Return a boolean Series on ``daily.index`` marking each month's effective expiry day.

    The effective day is the last date in ``daily`` on or before the third Thursday, so an
    expiry that falls on a holiday moves to the previous session.
    """
    trading = pd.DatetimeIndex(daily.index)
    flags = pd.Series(False, index=daily.index)
    months = sorted({(t.year, t.month) for t in trading})
    for y, m in months:
        nominal = third_thursday(y, m)
        prior = trading[trading <= nominal]
        if len(prior) == 0:
            continue
        eff = prior.max()
        # more than 7 days early means a data gap, not a holiday shift
        if (nominal - eff).days <= 7:
            flags.loc[eff] = True
    return flags


def tag_calendar(daily: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``daily`` with calendar tags and the overnight/intraday split in points.

    Adds ``dow``, ``is_expiry``, ``expiry_week`` (the 5 sessions ending on an expiry day),
    ``tom`` (first and last 3 sessions of a month), ``overnight`` (open_t - close_{t-1}),
    ``intraday`` (close_t - open_t) and ``roll_boundary`` (previous session was an expiry day).
    """
    df = daily.copy()
    df["dow"] = df.index.dayofweek
    df["is_expiry"] = expiry_days(df)
    ew = pd.Series(False, index=df.index)
    pos = {d: i for i, d in enumerate(df.index)}
    for d in df.index[df["is_expiry"]]:
        i = pos[d]
        ew.iloc[max(0, i - 4) : i + 1] = True
    df["expiry_week"] = ew
    month = df.index.to_period("M")
    rank_in_month = df.groupby(month).cumcount()
    days_in_month = df.groupby(month)["open"].transform("size")
    df["tom"] = (rank_in_month < 3) | (rank_in_month >= days_in_month - 3)
    df["overnight"] = df["open"] - df["close"].shift(1)
    df["intraday"] = df["close"] - df["open"]
    df["roll_boundary"] = df["is_expiry"].shift(1).fillna(False).astype(bool)
    return df


def window_stat(x: pd.Series) -> dict:
    """Return the count, mean and t-statistic of one screen window (NaNs dropped)."""
    v = x.dropna().to_numpy(dtype=float)
    n = v.size
    if n < 2:
        return {"n": int(n), "mean": 0.0, "t": 0.0}
    mean = float(v.mean())
    se = float(v.std(ddof=1) / np.sqrt(n))
    return {"n": int(n), "mean": mean, "t": mean / se if se > 0 else 0.0}


def halves(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split rows into two contiguous halves for sign-stability checks."""
    mid = len(df) // 2
    return df.iloc[:mid], df.iloc[mid:]
