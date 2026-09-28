"""Ratchet: fee calls on paths that can hold overnight must pass ``hold_days`` explicitly.

``round_trip_fee_points`` defaults to intraday; the position fee accrues 0.0255 points per
ICT calendar day held.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Paths that can hold a position through an ICT day boundary; add new ones here.
OVERNIGHT_PATH_FILES = (
    "historical/research_lab/forward_registry.py",  # n1_plans + the shared scorer
    "historical/research_lab/lab.py",  # C5 overnight pairs, bar driver, mc_null
)

_CALL = re.compile(r"round_trip_fee_points\s*\(")


def _strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string literals so a call quoted in prose is not read as code."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            kind, text, (srow, scol), (erow, ecol), _ = tok
            if kind in (tokenize.COMMENT, tokenize.STRING):
                out.append(("\n" * (erow - srow)) + " " * (ecol if erow > srow else len(text)))
            else:
                out.append(text)
    except tokenize.TokenError:  # pragma: no cover - only on unparsable source
        return source
    return "".join(out)


def _calls_missing_hold_days(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    code = _strip_comments_and_strings(source)
    missing: list[tuple[int, str]] = []
    for match in _CALL.finditer(code):
        # skip the function definition itself
        if "def " in code[max(0, match.start() - 40) : match.start()]:
            continue
        depth, i = 1, match.end()
        while i < len(code) and depth:
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
            i += 1
        args = code[match.end() : i - 1]
        if "hold_days" not in args:
            line = code[: match.start()].count("\n") + 1
            missing.append((line, " ".join(args.split())[:80]))
    return missing


@pytest.mark.parametrize("rel", OVERNIGHT_PATH_FILES)
def test_overnight_paths_state_their_fee_basis(rel):
    path = REPO_ROOT / rel
    assert path.exists(), f"{rel} moved; update OVERNIGHT_PATH_FILES"
    missing = _calls_missing_hold_days(path)
    assert not missing, (
        f"{rel} charges a fee without naming hold_days at {missing}. On a path that can "
        "hold overnight the default (intraday) is a silent under-charge of 0.0255pt per "
        "ICT day. Pass hold_days explicitly - `hold_days=0` is a fine answer when the "
        "hold is intraday by construction, and it says so."
    )


def test_the_stripper_does_not_hide_real_calls():
    """The stripper blanks the comment but keeps the real call."""
    src = "x = round_trip_fee_points(p)  # round_trip_fee_points(q, hold_days=1)\n"
    code = _strip_comments_and_strings(src)
    assert "round_trip_fee_points(p)" in code
    assert "hold_days" not in code


def test_a_documented_bug_does_not_fail_the_ratchet():
    """A call quoted inside a string literal is not counted."""
    src = '"""Until 2026-09-02 this called round_trip_fee_points(price)."""\n'
    assert "round_trip_fee_points" not in _strip_comments_and_strings(src)


def test_hold_days_between_counts_ict_calendar_boundaries():
    from historical.research_lab import lab

    ict = 7 * 3600
    import datetime as dt

    def ts(s: str) -> int:
        return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()) - ict

    friday_close = ts("2026-08-14T14:45:00")
    monday_open = ts("2026-08-17T09:15:00")
    friday_open = ts("2026-08-14T09:15:00")

    assert lab.hold_days_between(friday_open, friday_close) == 0
    assert lab.hold_days_between(friday_close, monday_open) == 3, "Fri->Mon crosses three days"
    assert lab.hold_days_between(monday_open, friday_close) == 0, "never negative"
