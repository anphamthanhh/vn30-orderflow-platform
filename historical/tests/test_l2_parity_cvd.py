"""Bitwise Rust/Python parity for the CVD features, compared print by print."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from historical.l2.producers.tape_cvd import (  # noqa: E402
    LUNCH_RETURN_SECONDS,
    Step,
    cvd_steps,
    produce,
    write_tsv,
)
from historical.l2.tape import ict_day_key, ict_second_of_day, read_tape  # noqa: E402

FIXTURE = _REPO_ROOT / "historical" / "tests" / "fixtures" / "l2_tape_cvd_synthetic_20210304_20210305.csv"
MANIFEST = FIXTURE.with_suffix(".manifest.json")
NATIVE = _REPO_ROOT / "native_platform"

FEATURES = ("tape_cvd_lot_v1", "tape_cvd_norst_lot_v1")


# --- Rust side ---


def _rust_dump(feature: str) -> str:
    """Run ``cargo run --bin l2_dump``; skip without cargo, but fail if the build or run fails."""
    if shutil.which("cargo") is None:
        pytest.skip(
            "cargo not on PATH: the Rust half of the CVD parity contract was NOT "
            "checked in this run. Install the Rust toolchain, or run this test in "
            "the CI stage that has it. This is a gap in coverage, not a pass."
        )
    cmd = [
        "cargo",
        "run",
        "--quiet",
        "-p",
        "orderflow-engine",
        "--bin",
        "l2_dump",
        "--",
        "--feature",
        feature,
        "--tape",
        str(FIXTURE),
    ]
    proc = subprocess.run(
        cmd, cwd=NATIVE, capture_output=True, text=True, timeout=1800, check=False
    )
    if proc.returncode != 0:
        if "os error 4551" in proc.stderr:
            pytest.fail(
                "cargo build hit os error 4551 — Windows Smart App Control is "
                "blocking build scripts. Turn SAC off (Windows Security > App & "
                "browser control); do not work around it. stderr:\n" + proc.stderr[-2000:]
            )
        pytest.fail(
            f"`{' '.join(cmd)}` failed with exit {proc.returncode}.\n"
            f"stderr:\n{proc.stderr[-4000:]}"
        )
    return proc.stdout.replace("\r\n", "\n")


@pytest.fixture(scope="module")
def rust_dumps() -> dict[str, str]:
    return {feature: _rust_dump(feature) for feature in FEATURES}


def _python_dump(feature: str) -> str:
    buf = io.StringIO()
    write_tsv(produce(FIXTURE, feature), buf)
    return buf.getvalue()


def _parse(dump: str) -> list[tuple[int, int, int]]:
    lines = dump.strip().split("\n")
    assert lines[0] == "idx\tepoch_s\tcvd", f"unexpected dump header {lines[0]!r}"
    return [tuple(int(v) for v in line.split("\t")) for line in lines[1:]]  # type: ignore[misc]


def _first_difference(a: list, b: list) -> str | None:
    if len(a) != len(b):
        return f"row counts differ: rust={len(a)} python={len(b)}"
    for ra, rb in zip(a, b):
        if ra != rb:
            return (
                f"first divergence at idx={ra[0]} epoch_s={ra[1]}: "
                f"rust cvd={ra[2]} python cvd={rb[2]}"
            )
    return None


# --- fixture checks ---


def test_fixture_matches_its_manifest():
    """The frozen tape's hash and symbol match its manifest."""
    from historical.l2.tools.make_tape_fixture import sha256_of

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert sha256_of(FIXTURE) == manifest["sha256_lf"], (
        "the frozen tape no longer hashes to its manifest; regenerate the manifest "
        "deliberately (`python samples/generate-fixtures.py`) rather "
        "than editing the hash"
    )
    assert manifest["symbol"] == "VN30F1M", (
        "vn30f_trades also holds VN100F1M rows; a tape without the symbol filter " "is contaminated"
    )


def test_the_tape_covers_the_cases_the_policy_can_be_wrong_about():
    """The tape spans a day boundary and the 13:00 seam, and contains 'U' prints."""
    prints = read_tape(FIXTURE)
    assert len(prints) >= 5000, f"only {len(prints)} prints"

    days = {ict_day_key(p.epoch_s) for p in prints}
    assert len(days) == 2, f"need two ICT days to exercise the day reset, got {len(days)}"

    unknown = [p for p in prints if p.aggressor == "U"]
    assert unknown, "no 'U' prints: the unknown-aggressor convention would be untested"

    for day in days:
        of_day = [p for p in prints if ict_day_key(p.epoch_s) == day]
        before = [p for p in of_day if ict_second_of_day(p.epoch_s) < LUNCH_RETURN_SECONDS]
        after = [p for p in of_day if ict_second_of_day(p.epoch_s) >= LUNCH_RETURN_SECONDS]
        assert (
            before and after
        ), f"ICT day {day} does not straddle 13:00; the lunch seam would be untested"
        assert any(
            ict_second_of_day(p.epoch_s) == LUNCH_RETURN_SECONDS for p in after
        ), f"ICT day {day} has no print exactly at 13:00:00"


# --- parity contract ---


@pytest.mark.parametrize("feature", FEATURES)
def test_rust_and_python_agree_bitwise(feature, rust_dumps):
    """Both dumps match print for print at tolerance 0, and byte for byte."""
    rust = _parse(rust_dumps[feature])
    python = _parse(_python_dump(feature))
    diff = _first_difference(rust, python)
    assert diff is None, (
        f"{feature}: the Rust engine and the Python producer disagree. {diff}. "
        f"Do NOT widen a tolerance — one of the two implementations is wrong, and "
        f"CVD is a sum of integers, so there is no rounding to blame."
    )
    assert rust_dumps[feature] == _python_dump(feature), (
        f"{feature}: values agree but the dumps are not byte-identical; the two "
        f"sides are formatting the same numbers differently"
    )


def test_the_python_cli_emits_the_same_bytes_as_the_library():
    """The CLI, used by the parity tests and the backfill job, emits the library's bytes."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "historical.l2.producers.tape_cvd",
            "--feature",
            "tape_cvd_lot_v1",
            "--tape",
            str(FIXTURE),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.replace("\r\n", "\n") == _python_dump("tape_cvd_lot_v1")


def test_the_two_features_are_actually_different_series(rust_dumps):
    """``tape_cvd_norst_lot_v1`` differs from ``tape_cvd_lot_v1``, so both reset policies apply."""
    assert rust_dumps["tape_cvd_lot_v1"] != rust_dumps["tape_cvd_norst_lot_v1"]


# --- 13:00 seam and day boundary ---


def test_the_lunch_seam_carries_the_level_rather_than_resetting(rust_dumps):
    """The first 13:00 print of each day adds its signed volume to the morning value; no reset.

    ``CvdTracker::rollover`` keeps the level, so values cannot tell a rollover from no action.
    """
    prints = read_tape(FIXTURE)
    rust = _parse(rust_dumps["tape_cvd_lot_v1"])
    seams = []
    for i in range(1, len(prints)):
        prev, cur = prints[i - 1], prints[i]
        if ict_day_key(prev.epoch_s) != ict_day_key(cur.epoch_s):
            continue
        if ict_second_of_day(prev.epoch_s) < LUNCH_RETURN_SECONDS <= ict_second_of_day(cur.epoch_s):
            seams.append(i)
    assert len(seams) == 2, f"expected one 13:00 seam per day, found {seams}"

    for i in seams:
        signed = prints[i].volume if prints[i].aggressor == "B" else -prints[i].volume
        if prints[i].aggressor not in ("B", "S"):
            signed = 0.0
        assert rust[i][2] == rust[i - 1][2] + int(signed), (
            f"the 13:00 seam at idx={i} is not continuous: "
            f"before={rust[i - 1][2]} after={rust[i][2]} print={signed:+g}. "
            f"Something reset the accumulator across lunch."
        )
        assert rust[i - 1][2] != 0, (
            "the morning CVD happens to be exactly zero at the seam, so this test "
            "cannot distinguish rollover from reset on this tape"
        )


def test_the_day_boundary_does_reset(rust_dumps):
    """``_v1`` starts each ICT day from zero; ``_norst`` carries the level across days."""
    prints = read_tape(FIXTURE)
    v1 = _parse(rust_dumps["tape_cvd_lot_v1"])
    norst = _parse(rust_dumps["tape_cvd_norst_lot_v1"])
    boundary = next(
        i
        for i in range(1, len(prints))
        if ict_day_key(prints[i].epoch_s) != ict_day_key(prints[i - 1].epoch_s)
    )
    signed = int(prints[boundary].volume) * (1 if prints[boundary].aggressor == "B" else -1)
    if prints[boundary].aggressor not in ("B", "S"):
        signed = 0
    assert v1[boundary][2] == signed, (
        f"the first print of the second ICT day reads {v1[boundary][2]}, not {signed}; "
        f"the day reset did not happen"
    )
    assert norst[boundary][2] == norst[boundary - 1][2] + signed, (
        "tape_cvd_norst_lot_v1 reset at the day boundary; it must not — "
        "the first-generation CVD-exhaustion strategy had backtests standing on the "
        "un-reset series"
    )


# --- mutation tests ---


def _mutated_steps(prints, *, lunch: str, day_reset: bool, unknown: str) -> list[Step]:
    """Reference CVD with selectable lunch, day-reset and unknown-aggressor policies."""
    cumulative = 0.0
    last_day_key = None
    last_sec = 0
    out = []
    for idx, p in enumerate(prints):
        day_key = ict_day_key(p.epoch_s)
        sec = ict_second_of_day(p.epoch_s)
        if last_day_key != day_key:
            if last_day_key is not None and day_reset:
                cumulative = 0.0
            last_day_key = day_key
        elif lunch == "reset" and last_sec < LUNCH_RETURN_SECONDS <= sec:
            cumulative = 0.0
        last_sec = sec

        if p.aggressor == "B":
            delta = p.volume
        elif p.aggressor == "S":
            delta = -p.volume
        elif unknown == "sell":
            delta = -p.volume
        else:
            delta = 0.0
        cumulative += delta
        out.append(Step(idx, p.epoch_s, int(cumulative)))
    return out


def _dump(steps) -> str:
    buf = io.StringIO()
    write_tsv(steps, buf)
    return buf.getvalue()


@pytest.mark.parametrize(
    "mutation,why",
    [
        (
            dict(lunch="reset", day_reset=True, unknown="zero"),
            "13:00 resets to zero instead of rolling over (the lab.py session_id policy)",
        ),
        (
            dict(lunch="rollover", day_reset=False, unknown="zero"),
            "the ICT day boundary no longer resets (the desktop viewer's policy)",
        ),
        (
            dict(lunch="rollover", day_reset=True, unknown="sell"),
            "'U' is counted as a SELL instead of zero (a numpy where() variant)",
        ),
    ],
)
def test_a_changed_policy_fails_the_comparison(mutation, why, rust_dumps):
    """Each alternative policy produces a dump different from the canonical one."""
    prints = read_tape(FIXTURE)
    mutated = _dump(_mutated_steps(prints, **mutation))
    assert mutated != rust_dumps["tape_cvd_lot_v1"], (
        f"a producer where {why} is indistinguishable from tape_cvd_lot_v1 on this "
        f"tape; the parity test is vacuous for that policy"
    )


def test_the_comparator_itself_is_not_vacuous():
    """A one-lot change is reported at its row, and a truncated series is not equal."""
    canonical = _parse(_python_dump("tape_cvd_lot_v1"))
    tampered = list(canonical)
    victim = len(tampered) // 2
    tampered[victim] = (
        canonical[victim][0],
        canonical[victim][1],
        canonical[victim][2] + 1,
    )
    diff = _first_difference(canonical, tampered)
    assert diff is not None and f"idx={victim}" in diff, diff
    assert (
        _first_difference(canonical, canonical[:-1]) is not None
    ), "a truncated series compared equal; the comparator ignores row counts"


def test_the_unknown_aggressor_convention_is_the_engines(rust_dumps):
    """'U' prints contribute zero, as in the engine, and the tape has enough to show it."""
    prints = read_tape(FIXTURE)
    unknown_volume = sum(p.volume for p in prints if p.aggressor == "U")
    assert unknown_volume > 0

    as_sell = _dump(_mutated_steps(prints, lunch="rollover", day_reset=True, unknown="sell"))
    as_zero = _dump(_mutated_steps(prints, lunch="rollover", day_reset=True, unknown="zero"))
    assert as_zero == rust_dumps["tape_cvd_lot_v1"], (
        "the reference reimplementation in this test file disagrees with the engine "
        "under the declared policy; one of the three now differs"
    )
    assert as_sell != as_zero, "the 'U' convention makes no difference on this tape"


def test_the_python_producer_refuses_a_fractional_cvd():
    """A fractional cumulative value raises instead of being rounded."""
    from historical.l2.tape import Print

    with pytest.raises(ValueError, match="not an exact integer"):
        list(cvd_steps([Print(epoch_s=1785981600, price=1900.0, volume=1.5, aggressor="B")]))
