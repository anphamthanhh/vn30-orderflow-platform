"""Per-session regime labels (trend or range, high or low volatility) for segmenting a backtest.

Trend axis: directional efficiency ``|close - open| / (high - low)`` against a fixed threshold.
Volatility axis: range ``(high - low) / open`` against a percentile over the whole supplied
window. Sessions are keyed by ICT date. Because the percentile uses the full window, the labels
describe a backtest and must not be used as a live signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

import numpy as np
import pandas as pd

PathLike = Union[str, Path]

ICT_OFFSET_SECONDS = 7 * 3600

# Default cut-offs; fixed, not fitted to P&L.
DEFAULT_TREND_THRESHOLD = 0.40  # directional efficiency >= 0.40 -> TREND
DEFAULT_VOL_PERCENTILE = 50.0  # realised range >= median -> HIGHVOL

TREND = "TREND"
RANGE = "RANGE"
HIGHVOL = "HIGHVOL"
LOWVOL = "LOWVOL"


def session_key_of_ts(ts_unix: float) -> str:
    """ICT calendar date (``YYYY-MM-DD``) for a unix-seconds timestamp."""
    ict = pd.to_datetime(int(ts_unix) + ICT_OFFSET_SECONDS, unit="s")
    return ict.strftime("%Y-%m-%d")


def sessions_from_bars(
    bars: pd.DataFrame,
    ts_col: str = "ts",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> pd.DataFrame:
    """Aggregate intraday OHLC bars (unix-seconds timestamps) into one row per ICT session.

    Returns columns ``session_key, open, high, low, close`` ordered by session: first open,
    highest high, lowest low and last close.
    """
    if bars.empty:
        return pd.DataFrame(columns=["session_key", "open", "high", "low", "close"])
    df = bars.copy()
    df["session_key"] = [session_key_of_ts(t) for t in df[ts_col].to_numpy()]
    grouped = df.groupby("session_key", sort=True)
    out = pd.DataFrame(
        {
            "open": grouped[open_col].first(),
            "high": grouped[high_col].max(),
            "low": grouped[low_col].min(),
            "close": grouped[close_col].last(),
        }
    ).reset_index()
    return out


def label_sessions(
    sessions: pd.DataFrame,
    trend_threshold: float = DEFAULT_TREND_THRESHOLD,
    vol_percentile: float = DEFAULT_VOL_PERCENTILE,
) -> pd.DataFrame:
    """Attach deterministic regime tags to a per-session OHLC frame.

    ``sessions`` needs columns ``session_key, open, high, low, close``. Adds:
    ``trend_strength, realised_vol, regime_trend, regime_vol, regime`` where
    ``regime`` is the combined ``"<TREND|RANGE>_<HIGHVOL|LOWVOL>"`` tag.
    """
    required = {"session_key", "open", "high", "low", "close"}
    missing = required - set(sessions.columns)
    if missing:
        raise ValueError(f"sessions missing columns: {sorted(missing)}")
    df = sessions.copy().reset_index(drop=True)
    if df.empty:
        for c in ["trend_strength", "realised_vol", "regime_trend", "regime_vol", "regime"]:
            df[c] = pd.Series(dtype="object" if "regime" in c else "float64")
        return df

    rng = (df["high"] - df["low"]).astype(float)
    net = (df["close"] - df["open"]).abs().astype(float)
    # a zero-range session gets efficiency 0, i.e. RANGE
    df["trend_strength"] = np.where(rng > 0, net / rng, 0.0)
    df["realised_vol"] = np.where(df["open"].astype(float) > 0, rng / df["open"].astype(float), 0.0)

    vol_cut = float(np.percentile(df["realised_vol"].to_numpy(), vol_percentile))
    df["regime_trend"] = np.where(df["trend_strength"] >= trend_threshold, TREND, RANGE)
    df["regime_vol"] = np.where(df["realised_vol"] >= vol_cut, HIGHVOL, LOWVOL)
    df["regime"] = df["regime_trend"].astype(str) + "_" + df["regime_vol"].astype(str)
    return df


@dataclass(frozen=True)
class RegimeLabel:
    """One session's regime label; the fields match the records of :func:`emit_regime_labels`."""

    session_key: str
    regime: str
    regime_trend: str
    regime_vol: str
    trend_strength: float
    realised_vol: float


def emit_regime_labels(labelled: pd.DataFrame, out_path: PathLike) -> Path:
    """Write one JSON line per labelled session to ``out_path`` and return the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["session_key", "regime", "regime_trend", "regime_vol", "trend_strength", "realised_vol"]
    with open(out_path, "w", encoding="utf-8") as fh:
        for _, row in labelled[cols].iterrows():
            fh.write(
                json.dumps(
                    {
                        "session_key": str(row["session_key"]),
                        "regime": str(row["regime"]),
                        "regime_trend": str(row["regime_trend"]),
                        "regime_vol": str(row["regime_vol"]),
                        "trend_strength": float(row["trend_strength"]),
                        "realised_vol": float(row["realised_vol"]),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    return out_path


def load_regime_labels(path: PathLike) -> Dict[str, Dict[str, object]]:
    """Load a regime-label JSONL file into ``{session_key: record}``."""
    labels: Dict[str, Dict[str, object]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            labels[str(rec["session_key"])] = rec
    return labels


def regime_for_ts(ts_unix: float, labels: Dict[str, Dict[str, object]]) -> Optional[str]:
    """Map a trade timestamp to its session's regime tag (None if unlabelled)."""
    rec = labels.get(session_key_of_ts(ts_unix))
    return str(rec["regime"]) if rec else None


def regimes_for_timestamps(
    timestamps: Iterable[float], labels: Dict[str, Dict[str, object]]
) -> List[Optional[str]]:
    """Apply :func:`regime_for_ts` to each timestamp."""
    return [regime_for_ts(t, labels) for t in timestamps]
