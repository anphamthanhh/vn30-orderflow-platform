"""Offline tests for jobs/data_quality.py pure detectors."""

import sys
from pathlib import Path

_DP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DP_ROOT.parent))
sys.path.insert(0, str(_DP_ROOT))

from jobs.data_quality import find_minute_gaps, flag_low_days  # noqa: E402


def test_flag_low_days_flags_partial_sessions():
    counts = {f"2026-08-{d:02d}": 240 for d in range(1, 11)}
    counts["2026-08-05"] = 120  # half session
    counts["2026-08-07"] = 239  # normal-ish, not flagged
    flagged = flag_low_days(counts, min_ratio=0.8)
    assert [d for d, _, _ in flagged] == ["2026-08-05"]
    assert flagged[0][1] == 120 and flagged[0][2] == 0.5


def test_flag_low_days_degenerate_inputs():
    assert flag_low_days({}, 0.8) == []
    assert flag_low_days({"a": 1, "b": 2}, 0.8) == []  # <3 days → no baseline


def test_find_minute_gaps_detects_and_counts_missing():
    base = 1785000000
    # minutes 0,1,2 then a 5-minute hole, then 8
    ts = [base, base + 60, base + 120, base + 480]
    gaps = find_minute_gaps(ts, max_gap_sec=60)
    assert len(gaps) == 1
    prev, nxt, missing = gaps[0]
    assert prev == base + 120 and nxt == base + 480 and missing == 5


def test_find_minute_gaps_skips_allowed_boundary():
    base = 1785000000
    lunch_start, lunch_end = base + 120, base + 6000  # a big "lunch" gap
    ts = [base, base + 60, lunch_start, lunch_end, lunch_end + 60]
    # Without allowance → flagged; with allowance → skipped.
    assert find_minute_gaps(ts, 60) != []
    assert find_minute_gaps(ts, 60, allowed_boundaries=[(lunch_start, lunch_end)]) == []
