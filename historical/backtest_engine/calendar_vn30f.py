"""VN30F trading-session helpers: session filters, session info and Plotly rangebreaks.

Sessions (ICT, bounds inclusive): morning 09:00–11:30, afternoon 13:00–14:45.
"""

import pandas as pd
import numpy as np
from typing import Optional, List, Dict, Any
from datetime import time, datetime


MORNING_SESSION_START = time(9, 0)
MORNING_SESSION_END = time(11, 30)
AFTERNOON_SESSION_START = time(13, 0)
AFTERNOON_SESSION_END = time(14, 45)  # includes the 14:30–14:45 ATC auction


def is_trading_time(ts: pd.Timestamp) -> bool:
    """Return True if ``ts`` falls in the morning or afternoon session."""
    if ts is None:
        return False

    if isinstance(ts, pd.Timestamp):
        t = ts.time()
    elif isinstance(ts, datetime):
        t = ts.time()
    else:
        return False

    if MORNING_SESSION_START <= t <= MORNING_SESSION_END:
        return True

    if AFTERNOON_SESSION_START <= t <= AFTERNOON_SESSION_END:
        return True

    return False


def is_trading_day(ts: pd.Timestamp) -> bool:
    """Return True for Monday to Friday; public holidays are not excluded."""
    if ts.dayofweek >= 5:
        return False

    return True


def filter_trading_sessions(
    df: pd.DataFrame,
    ts_col: Optional[str] = None,
    index_is_ts: bool = True,
) -> pd.DataFrame:
    """Return the rows of ``df`` whose timestamp falls in a trading session.

    Reads the DatetimeIndex when ``index_is_ts``, else column ``ts_col``; returns ``df`` unchanged
    if that source is missing.
    """
    if df is None or len(df) == 0:
        return df

    if index_is_ts:
        if not isinstance(df.index, pd.DatetimeIndex):
            return df
        mask = df.index.map(is_trading_time)
    elif ts_col and ts_col in df.columns:
        mask = df[ts_col].map(is_trading_time)
    else:
        return df

    return df[mask].copy()


def plotly_rangebreaks_vn30f(
    include_weekends: bool = True,
    include_lunch: bool = True,
    include_after_hours: bool = True,
) -> List[Dict[str, Any]]:
    """Return Plotly x-axis ``rangebreaks`` hiding weekends, lunch and off-hours; data is untouched.

    Usage: ``fig.update_xaxes(rangebreaks=plotly_rangebreaks_vn30f())``.
    """
    rangebreaks = []

    if include_weekends:
        rangebreaks.append(
            {
                "bounds": ["sat", "mon"],
                "pattern": "day of week",
                "name": "Weekend",
            }
        )

    if include_lunch:
        rangebreaks.append(
            {
                "bounds": [11.5, 13],  # 11:30 - 13:00
                "pattern": "hour",
                "name": "Lunch",
            }
        )

    if include_after_hours:
        rangebreaks.append(
            {
                "bounds": [0, 9],  # 00:00 - 09:00
                "pattern": "hour",
                "name": "Pre-Market",
            }
        )

    if include_after_hours:
        rangebreaks.append(
            {
                "bounds": [14.75, 24],  # 14:45 - 24:00
                "pattern": "hour",
                "name": "After-Hours",
            }
        )

    return rangebreaks


def plotly_rangebreaks_session_only() -> List[Dict[str, Any]]:
    """Return rangebreaks for the default "Session Breaks" view (weekends, lunch, off-hours)."""
    return plotly_rangebreaks_vn30f(
        include_weekends=True,
        include_lunch=True,
        include_after_hours=True,
    )


def plotly_rangebreaks_filled() -> List[Dict[str, Any]]:
    """Return no rangebreaks, for the "Full Timeline" view that shows every timestamp."""
    return []


def get_session_info(ts: pd.Timestamp) -> Dict[str, Any]:
    """Return ``is_trading``, ``session`` and ``time_in_session`` for ``ts``.

    ``session`` is "morning", "afternoon" or "none"; ``time_in_session`` is whole minutes since
    the session opened, or None outside trading hours.
    """
    result = {
        "is_trading": is_trading_time(ts),
        "session": "none",
        "time_in_session": None,
    }

    if not result["is_trading"]:
        return result

    t = ts.time()

    if MORNING_SESSION_START <= t <= MORNING_SESSION_END:
        result["session"] = "morning"
        session_start = datetime.combine(ts.date(), MORNING_SESSION_START)
        result["time_in_session"] = int(
            (ts - pd.Timestamp(session_start)).total_seconds() / 60
        )
    elif AFTERNOON_SESSION_START <= t <= AFTERNOON_SESSION_END:
        result["session"] = "afternoon"
        session_start = datetime.combine(ts.date(), AFTERNOON_SESSION_START)
        result["time_in_session"] = int(
            (ts - pd.Timestamp(session_start)).total_seconds() / 60
        )

    return result


def resample_to_trading_bars(
    df: pd.DataFrame,
    freq: str = "1min",
    ts_col: Optional[str] = None,
) -> pd.DataFrame:
    """Filter ``df`` to trading sessions and resample it to ``freq`` bars (last value per bar)."""
    df_filtered = filter_trading_sessions(df, ts_col=ts_col)

    if len(df_filtered) == 0:
        return df_filtered

    if isinstance(df_filtered.index, pd.DatetimeIndex):
        return df_filtered.resample(freq).last()
    elif ts_col and ts_col in df_filtered.columns:
        df_filtered = df_filtered.set_index(ts_col)
        return df_filtered.resample(freq).last()

    return df_filtered


__all__ = [
    "is_trading_time",
    "is_trading_day",
    "filter_trading_sessions",
    "plotly_rangebreaks_vn30f",
    "plotly_rangebreaks_session_only",
    "plotly_rangebreaks_filled",
    "get_session_info",
    "resample_to_trading_bars",
    "MORNING_SESSION_START",
    "MORNING_SESSION_END",
    "AFTERNOON_SESSION_START",
    "AFTERNOON_SESSION_END",
]
