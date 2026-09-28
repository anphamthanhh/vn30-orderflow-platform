"""Research-lab helpers: holdout-lock guards, ICT sessions, daily and basis signals, best-quote OFI.

Timestamps are unix seconds; days and sessions are grouped in ICT (UTC+7).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

# make `historical.*` importable from any working directory
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from historical.backtest_engine.fee_hurdle import round_trip_fee_points  # noqa: E402

ICT_OFFSET = 7 * 3600
VN30F_MULTIPLIER = 100_000  # VND per index point


def ict_day(ts: int | float) -> int:
    """Return the ICT calendar-day index of a unix timestamp; mirrors ``ict_day`` in ``fill.rs``."""
    return int((int(ts) + ICT_OFFSET) // 86_400)


def hold_days_between(entry_ts: int | float, exit_ts: int | float) -> int:
    """Return the number of ICT day boundaries crossed between entry and exit (never negative).

    This is ``hold_days``: the extra days charged the position fee.
    """
    return max(0, ict_day(exit_ts) - ict_day(entry_ts))


# VN30F1M session windows in ICT seconds of day (as in atr_breakout.rs).
MORNING = (32_400, 41_400)  # 09:00–11:30
AFTERNOON = (46_800, 53_100)  # 13:00–14:45


# --- holdout lock ---
# Reserved windows in holdout_lock.json are spent once, at a final verdict. Call
# assert_dev_only() (or a sibling guard) before any exploratory analysis.
import datetime as _dt  # noqa: E402

_EPOCH = _dt.date(1970, 1, 1)


def _load_holdout_lock() -> dict:
    import json as _json

    with open(Path(__file__).resolve().parent / "holdout_lock.json", encoding="utf-8") as fh:
        return _json.load(fh)


def holdout_cutoff_day() -> int:
    """Return the ICT day index (days since the Unix epoch) of the cutoff, the last dev day."""
    d = _dt.date.fromisoformat(_load_holdout_lock()["cutoff_ict_date"])
    return (d - _EPOCH).days


def holdout_ranges() -> list[tuple[int, Optional[int]]]:
    """Return the reserved windows as inclusive ICT day-index ranges ``(from, to)``.

    ``to=None`` is open-ended. The list holds the lock's ``reserved_ranges`` plus every day
    after the cutoff, ``(cutoff + 1, None)``.
    """
    lock = _load_holdout_lock()
    out: list[tuple[int, Optional[int]]] = []
    for r in lock.get("reserved_ranges", []):
        f = (_dt.date.fromisoformat(r["from_ict_date"]) - _EPOCH).days
        t = (
            (_dt.date.fromisoformat(r["to_ict_date"]) - _EPOCH).days
            if r.get("to_ict_date")
            else None
        )
        out.append((f, t))
    out.append((holdout_cutoff_day() + 1, None))
    return out


def is_holdout(ts: int) -> bool:
    """Return True if a unix timestamp falls in any reserved window."""
    day = (int(ts) + ICT_OFFSET) // 86_400
    return any(f <= day and (t is None or day <= t) for f, t in holdout_ranges())


def assert_dev_only(bars: pd.DataFrame) -> None:
    """Raise ``AssertionError`` if any bar (by ``ts_close``) falls in a reserved window."""
    if bars.empty:
        return
    days = ((bars["ts_close"].to_numpy() + ICT_OFFSET) // 86_400).astype("int64")
    for f, t in holdout_ranges():
        hit = (days >= f) & (days <= (t if t is not None else np.iinfo(np.int64).max))
        if hit.any():
            d = int(days[hit][0])
            raise AssertionError(
                f"bars include HOLDOUT data (day {d} in reserved range [{f}, {t}]). "
                "Reserved sessions are for the final verdict only — do not explore them."
            )


def dev_ranges() -> list[tuple[int, int]]:
    """Return the lock's development windows (allowed data) as inclusive ICT day-index ranges."""
    out: list[tuple[int, int]] = []
    for r in _load_holdout_lock().get("dev_ranges", []):
        f = (_dt.date.fromisoformat(r["from_ict_date"]) - _EPOCH).days
        t = (_dt.date.fromisoformat(r["to_ict_date"]) - _EPOCH).days
        out.append((f, t))
    return out


def source_lock(source: str) -> dict:
    """Return the lock entry for one data source; raise ``KeyError`` if it is not registered."""
    rows = {s["source"]: s for s in _load_holdout_lock().get("sources", [])}
    if source not in rows:
        raise KeyError(
            f"unknown data source {source!r}: register it in holdout_lock.json sources[] "
            f"(with its own dev/reserved boundary) before reading it. Known: {sorted(rows)}"
        )
    return rows[source]


def _parse_range(spec: str) -> tuple[int, Optional[int]]:
    """Parse ``"YYYY-MM-DD..YYYY-MM-DD"`` (open end allowed) into ICT day indices."""
    lo, _, hi = spec.partition("..")
    f = (_dt.date.fromisoformat(lo.strip()) - _EPOCH).days
    t = (_dt.date.fromisoformat(hi.strip()) - _EPOCH).days if hi.strip() else None
    return f, t


def source_dev_ranges(source: str) -> list[tuple[int, int]]:
    """Return one source's own development day-index ranges; an open end stops at the cutoff."""
    return [
        (f, t if t is not None else holdout_cutoff_day())
        for f, t in (_parse_range(s) for s in source_lock(source).get("dev_ranges", []))
    ]


def assert_dev_only_source(source: str, days) -> None:
    """Raise ``AssertionError`` if any ICT day index lies outside the source's own dev ranges."""
    arr = np.asarray(list(days), dtype="int64")
    if arr.size == 0:
        return
    allowed = source_dev_ranges(source)
    if not allowed:
        raise AssertionError(f"source {source!r} declares no dev range — it may not be read.")
    ok = np.zeros(arr.shape, dtype=bool)
    for f, t in allowed:
        ok |= (arr >= f) & (arr <= t)
    if not ok.all():
        bad = int(arr[~ok][0])
        pretty = ", ".join(f"{s}" for s in source_lock(source).get("dev_ranges", []))
        raise AssertionError(
            f"{source}: day {bad} ({_EPOCH + _dt.timedelta(days=bad)}) is OUTSIDE the "
            f"source's DEV window [{pretty}]. Reserved sessions are for the final verdict "
            "only — see PRE_REGISTRATION §A12 and holdout_lock.json sources[]."
        )


def assert_export_range_allowed(
    start_date: str, end_date_exclusive: str, allow: bool = False
) -> None:
    """Raise ``SystemExit`` if the ICT dates ``[start, end)`` overlap a reserved window.

    ``allow=True`` skips the check (final verdict only).
    """
    if allow:
        return
    f = (_dt.date.fromisoformat(start_date) - _EPOCH).days
    l = (_dt.date.fromisoformat(end_date_exclusive) - _EPOCH).days - 1
    for rf, rt in holdout_ranges():
        hi = rt if rt is not None else 10**9
        if f <= hi and l >= rf:
            raise SystemExit(
                f"REFUSED: export [{start_date}, {end_date_exclusive}) overlaps reserved "
                f"holdout days [{rf}, {rt}]. Reserved for final verdicts — pass --allow-holdout only then."
            )


def _session_of(ts_close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(segment, minute, ict_day)`` arrays for bar-close timestamps.

    segment: 0 morning, 1 afternoon, -1 outside a session; minute: minutes since the segment
    open (-1 outside). Mirrors ``session_pos`` in atr_breakout.rs.
    """
    tod = np.mod(ts_close + ICT_OFFSET, 86_400)
    seg = np.full(tod.shape, -1, dtype=int)
    minute = np.full(tod.shape, -1, dtype=int)
    m = (tod >= MORNING[0]) & (tod < MORNING[1])
    a = (tod >= AFTERNOON[0]) & (tod < AFTERNOON[1])
    seg[m] = 0
    minute[m] = (tod[m] - MORNING[0]) // 60
    seg[a] = 1
    minute[a] = (tod[a] - AFTERNOON[0]) // 60
    ict_day = np.floor_divide(ts_close + ICT_OFFSET, 86_400)
    return seg, minute, ict_day


def tag_sessions(bars: pd.DataFrame) -> pd.DataFrame:
    """Add ``segment``, ``session_minute``, ``ict_day``, ``session_id`` columns."""
    seg, minute, day = _session_of(bars["ts_close"].to_numpy())
    out = bars.copy()
    out["segment"] = seg
    out["session_minute"] = minute
    out["ict_day"] = day
    out["session_id"] = np.where(seg >= 0, day * 2 + seg, -1)
    return out


# --- indicators ---
def wilder_atr(bars: pd.DataFrame, period: int = 20) -> pd.Series:
    """Return Wilder's ATR, matching ``orderflow_engine::atr`` (continuous, no session reset).

    Seed = mean of the first ``period`` true ranges (the first bar's TR is high - low), then
    ``ATR = (ATR * (p - 1) + TR) / p``. ``NaN`` until warm; invalid bars carry the last value.
    """
    high = bars["high"].to_numpy()
    low = bars["low"].to_numpy()
    close = bars["close"].to_numpy()
    n = len(bars)
    atr = np.full(n, np.nan)
    prev_close: Optional[float] = None
    val: Optional[float] = None
    seed_sum = 0.0
    seed_cnt = 0
    for i in range(n):
        h, lo, c = high[i], low[i], close[i]
        if not (np.isfinite(h) and np.isfinite(lo) and np.isfinite(c)) or h < lo:
            atr[i] = val if val is not None else np.nan
            continue
        tr = (
            (h - lo)
            if prev_close is None
            else max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        )
        prev_close = c
        if val is not None:
            val = (val * (period - 1) + tr) / period
        else:
            seed_sum += tr
            seed_cnt += 1
            if seed_cnt >= period:
                val = seed_sum / period
        atr[i] = val if val is not None else np.nan
    return pd.Series(atr, index=bars.index, name=f"atr{period}")


# --- daily bars and the C7 reversal signal ---
def daily_bars(m1_bars: pd.DataFrame) -> pd.DataFrame:
    """Collapse intraday bars to one in-session OHLCV bar per ICT day, indexed by ``ict_day``.

    Adds close-to-close ``ret`` in points and an ICT ``dt``. Accepts ``ts_close``, ``ts`` or
    ``ts_open`` timestamps.
    """
    b = m1_bars.copy()
    if "ts_close" not in b.columns:
        b["ts_close"] = b["ts"] if "ts" in b.columns else b["ts_open"]
    b = tag_sessions(b)
    b = b[b["segment"] >= 0]
    g = b.groupby("ict_day")
    d = pd.DataFrame(
        {
            "ts": g["ts_close"].first(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).sort_index()
    d["ret"] = d["close"].diff()
    d["dt"] = pd.to_datetime(d["ts"] + ICT_OFFSET, unit="s")
    return d


def reversal_daily_signal(
    daily: pd.DataFrame, lookback: int = 60, pct: float = 0.80
) -> pd.DataFrame:
    """Return a copy of ``daily`` with the C7 reversal signal: fade day D's large move on D+1.

    The signal fires when ``|ret_D|`` reaches the ``pct`` quantile of the previous ``lookback``
    days, so it uses only data known at the D close. Adds ``thresh``, ``fire`` and ``fade_dir``
    (+1 long, -1 short, 0 none).
    """
    d = daily.copy()
    d["absr"] = d["ret"].abs()
    # prior days only (shift 1): no look-ahead
    d["thresh"] = d["absr"].rolling(lookback).quantile(pct).shift(1)
    d["fire"] = (d["absr"] >= d["thresh"]).fillna(False)
    raw = -np.sign(d["ret"].to_numpy())
    d["fade_dir"] = np.where(d["fire"].to_numpy() & np.isfinite(raw), raw, 0.0).astype(int)
    return d


# --- best-quote order-flow imbalance (Cont, Kukanov and Stoikov) from the L2 tape ---
# OFI is the signed change in best bid/ask queue size produced by trades and quote revisions.


def load_book(path: Path | str) -> pd.DataFrame:
    """Load an L2 JSONL tape as a best-quote frame (``ts, pb, qb, pa, qa, mid``), ICT-indexed."""
    df = pd.read_json(path, lines=True)
    df["pb"] = df["bid_px"].str[0].astype(float)
    df["qb"] = df["bid_sz"].str[0].astype(float)
    df["pa"] = df["ask_px"].str[0].astype(float)
    df["qa"] = df["ask_sz"].str[0].astype(float)
    df = df[["ts", "pb", "qb", "pa", "qa"]].dropna()
    df["mid"] = (df["pb"] + df["pa"]) / 2.0
    df["dt"] = pd.to_datetime(df["ts"] + ICT_OFFSET, unit="s")
    return df.set_index("dt")


def ofi_events(book: pd.DataFrame) -> np.ndarray:
    """Return the per-update best-quote OFI contribution e_n (0 for the first update).

    e_n =  1{Pb_n>=Pb_{n-1}}*qb_n - 1{Pb_n<=Pb_{n-1}}*qb_{n-1}
         - 1{Pa_n<=Pa_{n-1}}*qa_n + 1{Pa_n>=Pa_{n-1}}*qa_{n-1}
    """
    pb, qb, pa, qa = (book[c].to_numpy() for c in ("pb", "qb", "pa", "qa"))
    pb0, qb0, pa0, qa0 = pb[:-1], qb[:-1], pa[:-1], qa[:-1]
    pb1, qb1, pa1, qa1 = pb[1:], qb[1:], pa[1:], qa[1:]
    e = (pb1 >= pb0) * qb1 - (pb1 <= pb0) * qb0 - (pa1 <= pa0) * qa1 + (pa1 >= pa0) * qa0
    return np.concatenate([[0.0], e])  # first event has no predecessor


def ofi_bars(book: pd.DataFrame, bar_seconds: int = 60) -> pd.DataFrame:
    """Aggregate OFI into ``bar_seconds`` buckets (summed ``ofi``, last ``mid``), session-tagged."""
    b = book.copy()
    b["e"] = ofi_events(b)
    bucket = (b["ts"] // bar_seconds).astype("int64")
    g = b.groupby(bucket, sort=True)
    bars = pd.DataFrame(
        {
            "ts_open": g["ts"].first().to_numpy(),
            "ts_close": g["ts"].max().to_numpy(),
            "ofi": g["e"].sum().to_numpy(),
            "mid": g["mid"].last().to_numpy(),
            "n_updates": g.size().to_numpy(),
        }
    )
    bars["close"] = bars["mid"]  # alias for code that expects a close column
    bars["dt"] = pd.to_datetime(bars["ts_open"] + ICT_OFFSET, unit="s")
    bars = bars.set_index("dt")
    return tag_sessions(bars)


def ofi_predictivity(ob: pd.DataFrame, horizons: Sequence[int] = (1, 5, 15)) -> pd.DataFrame:
    """Measure whether bar OFI predicts the forward mid change, per horizon in bars.

    Reports the Spearman IC and the top-minus-bottom OFI-tercile forward spread against the
    round-trip fee at the median mid. Forward changes never cross a session boundary.
    """
    from scipy import stats

    b = ob[ob["segment"] >= 0].copy()
    rows = []
    for h in horizons:
        fwd = b.groupby("session_id")["mid"].shift(-h) - b["mid"]
        d = pd.DataFrame(
            {"ofi": b["ofi"].to_numpy(), "fwd": fwd.to_numpy(), "mid": b["mid"].to_numpy()}
        ).dropna()
        if len(d) < 30:
            continue
        ic = float(stats.spearmanr(d["ofi"], d["fwd"]).statistic)
        q = d["ofi"].quantile([1 / 3, 2 / 3]).to_numpy()
        spread = float(d[d["ofi"] >= q[1]]["fwd"].mean() - d[d["ofi"] <= q[0]]["fwd"].mean())
        # the forward change stays within a session, so the fee is intraday
        fee = round_trip_fee_points(float(d["mid"].median()), hold_days=0)
        rows.append(
            {
                "horizon_min": h,
                "n_bars": int(len(d)),
                "ic_ofi": round(ic, 4),
                "tercile_spread_pts": round(spread, 3),
                "fee_pts": round(fee, 3),
                "exploitable": bool(spread > fee),
            }
        )
    return pd.DataFrame(rows)


# --- per-day (date-keyed) exports ---
def assert_dev_only_days(days) -> None:
    """Raise ``AssertionError`` if any date in ``days`` falls in a reserved window."""
    idx = [int((d - _EPOCH).days) for d in days]
    if not idx:
        return
    for f, t in holdout_ranges():
        hi = t if t is not None else 10**9
        bad = [d for d in idx if f <= d <= hi]
        if bad:
            raise AssertionError(
                f"frame includes HOLDOUT days (day {bad[0]} in reserved range [{f}, {t}]). "
                "Reserved sessions are for the final verdict only."
            )


def load_daily(path: Path | str, allow_holdout: bool = False) -> pd.DataFrame:
    """Load a per-day JSONL export sorted by ``day``; holdout-guarded unless ``allow_holdout``."""
    df = pd.read_json(path, lines=True)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    if not allow_holdout:
        assert_dev_only_days(df["day"])
    return df.sort_values("day").reset_index(drop=True)


# --- X1 basis lane (forward accrual only) ---
# The VN30 cash index cannot be shorted, so the basis is a directional conditioner for a
# futures trade, not an arbitrage, and it must clear the same round-trip cost.
def load_basis_daily(ch: Optional[str] = None, *, allow_holdout: bool = False) -> pd.DataFrame:
    """Load the per-ICT-day basis frame from ClickHouse ``default.basis_m1``.

    Columns ``d, f_open, f_close, basis_eod, basis_avg`` (via ``basis_screen.load_daily_ch``);
    raises if ClickHouse is unreachable. Checked against the ``basis_m1`` source lock unless
    ``allow_holdout`` (final verdict only).
    """
    from historical.research_lab.basis_screen import CH_DEFAULT, load_daily_ch

    out = load_daily_ch(ch or CH_DEFAULT)
    if not allow_holdout and not out.empty and "d" in out.columns:
        # cast via datetime64[s] so the epoch day is right at any pandas datetime resolution
        days = pd.to_datetime(out["d"]).astype("datetime64[s]").astype("int64") // 86_400
        assert_dev_only_source("basis_m1", days.to_numpy())
    return out


def basis_signals(daily: pd.DataFrame, lookback: int = 20, extreme_q: float = 0.80) -> pd.DataFrame:
    """Return ``daily`` sorted by ``d`` with the causal X1 basis-reversion signal added.

    Day D fires when the ``lookback``-day z-score of the end-of-day basis reaches the
    ``extreme_q`` quantile of ``|z|`` over the previous ``lookback`` days (``basis_screen`` uses
    a whole-window quantile). ``would_signal`` fades it ("long" when cheap, "short" when rich)
    for a D+1 open-to-close trade. Adds ``basis_z``, ``thresh``, ``fire``, ``would_signal`` and
    ``first_of_session``.
    """
    d = (daily.sort_values("d") if "d" in daily.columns else daily).reset_index(drop=True).copy()
    b = d["basis_eod"].astype(float)
    d["basis_z"] = (b - b.rolling(lookback).mean()) / b.rolling(lookback).std()
    d["thresh"] = d["basis_z"].abs().rolling(lookback).quantile(extreme_q).shift(1)
    d["fire"] = (d["basis_z"].abs() >= d["thresh"]).fillna(False)
    raw = -np.sign(d["basis_z"].to_numpy())  # fade: cheap (z<0) -> long, rich (z>0) -> short
    fire = d["fire"].to_numpy() & np.isfinite(raw) & (raw != 0)
    d["would_signal"] = np.where(fire, np.where(raw > 0, "long", "short"), "")
    d["first_of_session"] = d["would_signal"] != ""
    return d
