"""Data-quality checks over ClickHouse OHLCV tables: short trading days and M1 gaps.

``flag_low_days`` and ``find_minute_gaps`` are pure functions over plain data. The CLI
queries ClickHouse over HTTP (stdlib only) and lists the days of one symbol whose bar count
is below ``--min-ratio`` x the median. Exit status: 0 clean, 1 short days found, 2 no rows.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from statistics import median
from typing import Dict, List, Optional, Tuple

_DP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _DP_ROOT not in sys.path:
    sys.path.insert(0, _DP_ROOT)


def flag_low_days(counts: Dict[str, int], min_ratio: float = 0.8) -> List[Tuple[str, int, float]]:
    """Return ``(date, count, ratio)`` for days below ``min_ratio`` x the median, by date.

    Fewer than three days, or a non-positive median, gives ``[]``.
    """
    if len(counts) < 3:
        return []
    med = median(sorted(counts.values()))
    if med <= 0:
        return []
    flagged = [(d, c, round(c / med, 3)) for d, c in counts.items() if c < min_ratio * med]
    return sorted(flagged, key=lambda x: x[0])


def find_minute_gaps(
    sorted_ts: List[int],
    max_gap_sec: int = 60,
    allowed_boundaries: Optional[List[Tuple[int, int]]] = None,
) -> List[Tuple[int, int, int]]:
    """Return ``(prev_ts, next_ts, missing_bars)`` for each step longer than ``max_gap_sec``.

    A gap equal to an ``allowed_boundaries`` pair (e.g. the lunch break) is skipped;
    ``missing_bars = gap // max_gap_sec - 1``.
    """
    allowed = set(allowed_boundaries or [])
    gaps: List[Tuple[int, int, int]] = []
    for a, b in zip(sorted_ts, sorted_ts[1:]):
        delta = b - a
        if delta > max_gap_sec and (a, b) not in allowed:
            gaps.append((a, b, delta // max_gap_sec - 1))
    return gaps


# --------------------------------------------------------------------------- CLI


def _ch(host: str, port: int, user: str, password: str, query: str) -> str:
    url = f"http://{host}:{port}/?" + urllib.parse.urlencode({"user": user, "password": password})
    req = urllib.request.Request(url, data=query.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ClickHouse HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def _load_ch_env() -> None:
    root = os.path.dirname(_DP_ROOT)
    try:
        with open(os.path.join(root, ".env"), "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().startswith("CLICKHOUSE_"):
                        os.environ[k.strip()] = v.strip()
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="VN30F1M")
    ap.add_argument("--table", default="ohlcv_m1")
    ap.add_argument("--time-col", default="time_1m")
    ap.add_argument("--min-ratio", type=float, default=0.8)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=8123)
    args = ap.parse_args(argv)

    _load_ch_env()
    host = args.host or os.getenv("CLICKHOUSE_HOST", "localhost")
    user = os.getenv("CLICKHOUSE_USER", "default")
    password = os.getenv("CLICKHOUSE_PASSWORD", "")

    q = (
        f"SELECT toDate({args.time_col}) d, count() c FROM default.{args.table} FINAL "
        f"WHERE symbol='{args.symbol}' GROUP BY d ORDER BY d FORMAT TSV"
    )
    rows = [ln.split("\t") for ln in _ch(host, args.port, user, password, q).splitlines() if ln]
    counts = {d: int(c) for d, c in rows}
    if not counts:
        print(f"[dq] no rows for symbol={args.symbol} in default.{args.table}")
        return 2

    med = int(median(sorted(counts.values())))
    low = flag_low_days(counts, args.min_ratio)
    print(
        f"[dq] symbol={args.symbol} days={len(counts)} median_bars/day={med} "
        f"span={min(counts)}..{max(counts)} low_days(<{args.min_ratio}xmed)={len(low)}"
    )
    for d, c, r in low[:40]:
        print(f"    {d}: {c} bars ({r}x median)")
    if len(low) > 40:
        print(f"    ... +{len(low) - 40} more")
    return 0 if not low else 1


if __name__ == "__main__":
    sys.exit(main())
