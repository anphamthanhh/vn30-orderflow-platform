"""Rust/Python parity for ``prof_tpo_poc_pts_v1``, with POC/VAL/VAH compared by column name."""

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

from historical.l2.producers.prof_tpo import (  # noqa: E402
    DEFAULT_TPO_PERIOD_MIN,
    DEFAULT_VA_PCT,
    Observation,
    assert_band_invariants,
    bucket_tick,
    group_by_session,
    observations,
    produce,
    profile_rows,
    tpo_histogram,
    tpo_period_id,
    write_observations,
    write_profile,
)
from historical.l2.prof_tape import read_price_tape  # noqa: E402

FIXTURE = _REPO_ROOT / "historical" / "tests" / "fixtures" / "l2_prof_tpo_synthetic_20210304_20210305.csv"
MANIFEST = FIXTURE.with_suffix(".manifest.json")
NATIVE = _REPO_ROOT / "native_platform"

FEATURE = "prof_tpo_poc_pts_v1"
EMITS = ("observation", "profile")

OBSERVATION_COLUMNS = (
    "session",
    "prints",
    "levels",
    "tpo_total",
    "poc_tick",
    "val_tick",
    "vah_tick",
    "va_width_ticks",
)
PROFILE_COLUMNS = ("session", "price_tick", "tpo_count")


# --- Rust side ---


def _rust_dump(emit: str) -> str:
    """Run ``cargo run --bin l2_dump``; skip without cargo, but fail if the build or run fails."""
    if shutil.which("cargo") is None:
        pytest.skip(
            "cargo not on PATH: the Rust half of the TPO/value-area parity contract "
            "was NOT checked in this run. Install the Rust toolchain, or run this "
            "test in the CI stage that has it. This is a gap in coverage, not a pass."
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
        FEATURE,
        "--emit",
        emit,
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
    return {emit: _rust_dump(emit) for emit in EMITS}


@pytest.fixture(scope="module")
def prints():
    return read_price_tape(FIXTURE)


def _python_dump(emit: str) -> str:
    buf = io.StringIO()
    if emit == "observation":
        write_observations(produce(FIXTURE, FEATURE, "observation"), buf)
    else:
        write_profile(produce(FIXTURE, FEATURE, "profile"), buf)
    return buf.getvalue()


def _parse(dump: str, columns: tuple[str, ...]) -> list[dict]:
    """Rows as ``{column_name: value}`` dicts, so every comparison goes by name, not position."""
    lines = dump.strip().split("\n")
    assert lines[0].split("\t") == list(columns), f"unexpected dump header {lines[0]!r}"
    rows = []
    for line in lines[1:]:
        fields = line.split("\t")
        assert len(fields) == len(columns), f"row {line!r} has {len(fields)} fields"
        row = {columns[0]: fields[0]}
        row.update({name: int(value) for name, value in zip(columns[1:], fields[1:])})
        rows.append(row)
    return rows


def _first_difference(a: list[dict], b: list[dict]) -> str | None:
    """Describe the first disagreement by row and column name, or return ``None``."""
    if len(a) != len(b):
        return f"row counts differ: rust={len(a)} python={len(b)}"
    for i, (ra, rb) in enumerate(zip(a, b)):
        for column in ra:
            if ra[column] != rb[column]:
                return (
                    f"first divergence at row {i} (session {ra['session']}), "
                    f"column {column!r}: rust={ra[column]} python={rb[column]}"
                )
    return None


# --- fixture checks ---


def test_fixture_matches_its_manifest():
    """The frozen tape's hash, symbol and feature match its manifest."""
    from historical.l2.tools.make_profile_fixture import sha256_of

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert sha256_of(FIXTURE) == manifest["sha256_lf"], (
        "the frozen tape no longer hashes to its manifest; regenerate the manifest "
        "deliberately (`python samples/generate-fixtures.py`) rather "
        "than editing the hash"
    )
    assert manifest["symbol"] == "VN30F1M", (
        "vn30f_trades also holds VN100F1M rows; a tape without the symbol filter is "
        "contaminated, and a contaminated tape produces a plausible wrong POC"
    )
    assert manifest["feature"] == FEATURE


def test_the_reduction_lost_no_tpo_cell(prints):
    """Keeping distinct ``(event_timestamp, price)`` pairs loses no (period, price) TPO cell.

    Cell counts are compared with the manifest's ``raw_measurements`` of the un-deduplicated tape.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw = manifest["raw_measurements"]
    assert raw, "the manifest carries no server-side measurement to check against"

    for session, rows in group_by_session(prints):
        measured = raw[session]
        cells = {(tpo_period_id(p.epoch_s), bucket_tick(p.price)) for p in rows}
        assert len(cells) == measured["tpo_cells"], (
            f"{session}: the frozen tape has {len(cells)} (period, price) cells but the "
            f"raw table had {measured['tpo_cells']}. The fixture reduction LOST data, so "
            f"its POC/VAH/VAL are not the session's."
        )
        assert len(rows) == measured["rows_kept"]
        assert measured["rows_raw"] > measured["rows_kept"], (
            f"{session}: nothing was actually deduplicated, so this test is not "
            f"checking the property it claims to"
        )


def test_the_two_sessions_have_different_profile_shapes(prints):
    """A balance session and a trend session, so the expansion is exercised on two shapes."""
    sessions = group_by_session(prints)
    assert len(sessions) >= 2, f"need at least two sessions, got {len(sessions)}"

    shapes = []
    for session, rows in sessions:
        histogram = tpo_histogram(rows)
        assert len(set(tpo_period_id(p.epoch_s) for p in rows)) >= 2, (
            f"{session} spans fewer than two TPO periods, so tpo_period_min is not "
            f"exercised at all"
        )
        shapes.append((session, len(histogram), max(histogram.values())))

    levels = [levels for _, levels, _ in shapes]
    assert max(levels) >= 2 * min(levels), (
        f"the sessions are too alike to be two shapes: price-level counts {shapes}. "
        f"Pick a trend session and a balance session, not two of a kind."
    )


# --- parity contract ---


@pytest.mark.parametrize(
    "emit,columns", [("observation", OBSERVATION_COLUMNS), ("profile", PROFILE_COLUMNS)]
)
def test_rust_and_python_agree_bitwise(emit, columns, rust_dumps):
    """Both dumps match column for column at tolerance 0, and byte for byte.

    The per-price ``l2_profile`` rows are compared too, not only the ``l2_observation`` scalars.
    """
    rust = _parse(rust_dumps[emit], columns)
    python = _parse(_python_dump(emit), columns)
    diff = _first_difference(rust, python)
    assert diff is None, (
        f"{emit}: the Rust engine and the Python producer disagree. {diff}. "
        f"Do NOT widen a tolerance — POC, VAH and VAL are price levels already "
        f"bucketed to whole ticks, so there is no rounding to blame and one of the "
        f"two implementations is wrong."
    )
    assert rust_dumps[emit] == _python_dump(emit), (
        f"{emit}: values agree but the dumps are not byte-identical; the two sides "
        f"are formatting the same numbers differently"
    )


def test_the_python_cli_emits_the_same_bytes_as_the_library():
    """The CLI, used by the parity tests and the backfill job, emits the library's bytes."""
    for emit in EMITS:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "historical.l2.producers.prof_tpo",
                "--feature",
                FEATURE,
                "--emit",
                emit,
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
        assert proc.stdout.replace("\r\n", "\n") == _python_dump(emit)


# --- value-area invariants, checked by column name ---


@pytest.mark.parametrize("side", ["rust", "python"])
def test_every_observation_satisfies_val_le_poc_le_vah(side, rust_dumps):
    """Checked on each side's dump, read by column name, since the dump crosses the boundary."""
    dump = rust_dumps["observation"] if side == "rust" else _python_dump("observation")
    rows = _parse(dump, OBSERVATION_COLUMNS)
    assert rows, "no observations to check"
    for row in rows:
        assert row["val_tick"] <= row["poc_tick"] <= row["vah_tick"], (
            f"{side} {row['session']}: value area is not val <= poc <= vah "
            f"(val={row['val_tick']} poc={row['poc_tick']} vah={row['vah_tick']}). "
            f"market-profile-engine returns (poc, vah, val) while three other "
            f"implementations return (poc, val, vah); this is what that swap looks like."
        )
        assert row["vah_tick"] >= row["val_tick"]
        assert row["va_width_ticks"] == row["vah_tick"] - row["val_tick"]


def test_the_scalars_are_the_reduction_of_the_distribution(rust_dumps):
    """``l2_observation`` and ``l2_profile`` describe the same profile.

    The POC is an argmax of the histogram, VAL..VAH is a contiguous band inside it and the
    counts agree; this also catches a POC taken from the wrong session.
    """
    observations_rows = _parse(rust_dumps["observation"], OBSERVATION_COLUMNS)
    profile = _parse(rust_dumps["profile"], PROFILE_COLUMNS)

    by_session: dict[str, dict[int, int]] = {}
    for row in profile:
        by_session.setdefault(row["session"], {})[row["price_tick"]] = row["tpo_count"]

    assert set(by_session) == {row["session"] for row in observations_rows}
    for row in observations_rows:
        histogram = by_session[row["session"]]
        assert row["levels"] == len(histogram)
        assert row["tpo_total"] == sum(histogram.values())
        assert histogram[row["poc_tick"]] == max(histogram.values()), (
            f"{row['session']}: the POC is not an argmax of the distribution it is "
            f"supposed to reduce"
        )
        assert row["val_tick"] in histogram and row["vah_tick"] in histogram
        # the value area is contiguous: every level from VAL to VAH is occupied
        for tick in range(row["val_tick"], row["vah_tick"] + 1):
            assert tick in histogram, (
                f"{row['session']}: tick {tick} lies inside the value area but has no "
                f"TPO count; the band is not contiguous and is therefore not a value area"
            )


# --- mutations: each must change the answer ---


def _dump_observations(rows) -> str:
    buf = io.StringIO()
    write_observations(rows, buf)
    return buf.getvalue()


def _swap_vah_val(rows: list[Observation]) -> list[Observation]:
    """Swap VAL and VAH, as reading a ``(poc, vah, val)`` tuple in ``(poc, val, vah)`` order would.

    ``market-profile-engine`` returns ``(poc, vah, val)``; ``shared/quant_core/profile.py``
    returns ``(poc, val, vah)``.
    """
    return [row._replace(val_tick=row.vah_tick, vah_tick=row.val_tick) for row in rows]


def test_swapping_vah_and_val_turns_the_comparison_red(rust_dumps):
    """A VAL/VAH swap is caught three ways: bytes, the named-column diff and the band invariant."""
    canonical = observations(read_price_tape(FIXTURE))
    swapped = _swap_vah_val(canonical)

    # 1. the bytes differ
    assert _dump_observations(swapped) != rust_dumps["observation"]

    # 2. the named-column diff reports val_tick or vah_tick
    diff = _first_difference(
        _parse(rust_dumps["observation"], OBSERVATION_COLUMNS),
        _parse(_dump_observations(swapped), OBSERVATION_COLUMNS),
    )
    assert diff is not None and ("val_tick" in diff or "vah_tick" in diff), diff

    # 3. the band invariant rejects it without a reference dump
    for row in swapped:
        if row.val_tick != row.vah_tick:
            with pytest.raises(ValueError, match="val <= poc <= vah"):
                assert_band_invariants(row)
            break
    else:  # pragma: no cover - would mean every session has a zero-width band
        pytest.fail(
            "every session's VAH equals its VAL, so a swap is invisible on this tape "
            "and this fixture cannot test the trap"
        )


def test_a_different_va_pct_is_a_different_answer(rust_dumps):
    """The registry pins ``va_pct: 0.70``; even 0.68 must change the output on this fixture."""
    mutated = _dump_observations(observations(read_price_tape(FIXTURE), va_pct=0.68))
    assert mutated != rust_dumps["observation"], (
        "va_pct 0.68 is indistinguishable from 0.70 on this tape; the entry's claim "
        "to pin va_pct is not being checked by anything"
    )
    assert DEFAULT_VA_PCT == 0.70


def test_a_different_tpo_period_is_a_different_answer(rust_dumps):
    """``tpo_period_min`` of 60 (merged periods) and 15 (split periods) both differ from 30."""
    for period in (60, 15):
        mutated = _dump_observations(observations(read_price_tape(FIXTURE), tpo_period_min=period))
        assert mutated != rust_dumps["observation"], (
            f"tpo_period_min={period} is indistinguishable from 30 on this tape; the "
            f"time bucketing is not being exercised"
        )
    assert DEFAULT_TPO_PERIOD_MIN == 30


def _value_area_first_argmax(histogram: dict[int, int], va_pct: float = DEFAULT_VA_PCT):
    """Value area with a first-argmax POC tie-break (lowest tied price); expansion as canonical.

    The canonical rule in ``market-profile-engine`` picks the tied level nearest the mid-range,
    then the higher price.
    """
    pairs = sorted(histogram.items())
    total = sum(weight for _, weight in pairs)
    poc_index = max(range(len(pairs)), key=lambda i: (pairs[i][1], -pairs[i][0]))

    target = total * va_pct
    covered = float(pairs[poc_index][1])
    left = poc_index - 1 if poc_index > 0 else None
    right = poc_index + 1
    lo_index = hi_index = poc_index
    while covered < target and (left is not None or right < len(pairs)):
        left_weight = pairs[left][1] if left is not None else -1
        right_weight = pairs[right][1] if right < len(pairs) else -1
        if right_weight > left_weight:
            covered += pairs[right][1]
            hi_index = right
            right += 1
        elif left is not None:
            covered += pairs[left][1]
            lo_index = left
            left = left - 1 if left > 0 else None
        elif right < len(pairs):
            covered += pairs[right][1]
            hi_index = right
            right += 1
    return pairs[poc_index][0], pairs[lo_index][0], pairs[hi_index][0]


def test_a_different_poc_tiebreak_is_a_different_answer(prints, rust_dumps):
    """The first-argmax tie-break gives a different dump from the canonical rule."""
    ties_seen = False
    mutated_rows = []
    for session, rows in group_by_session(prints):
        histogram = tpo_histogram(rows)
        top = max(histogram.values())
        if sum(1 for count in histogram.values() if count == top) > 1:
            ties_seen = True
        poc, val, vah = _value_area_first_argmax(histogram)
        mutated_rows.append(
            Observation(
                session=session,
                prints=len(rows),
                levels=len(histogram),
                tpo_total=sum(histogram.values()),
                poc_tick=poc,
                val_tick=val,
                vah_tick=vah,
                va_width_ticks=vah - val,
            )
        )
    assert ties_seen, (
        "no session on this tape has tied maximum TPO counts, so the POC tie-break "
        "cannot possibly show up and this mutation is vacuous"
    )
    assert _dump_observations(mutated_rows) != rust_dumps["observation"], (
        "the first-argmax POC tie-break (the real-time viewer's former rule) is "
        "indistinguishable from the canonical "
        "nearest-mid-range-then-higher rule on this tape"
    )


def test_the_comparator_itself_is_not_vacuous():
    """A one-tick change is reported in its column, and a truncated series is not equal."""
    canonical = _parse(_python_dump("observation"), OBSERVATION_COLUMNS)
    tampered = [dict(row) for row in canonical]
    tampered[0]["poc_tick"] += 1
    diff = _first_difference(canonical, tampered)
    assert diff is not None and "poc_tick" in diff, diff
    assert (
        _first_difference(canonical, canonical[:-1]) is not None
    ), "a truncated series compared equal; the comparator ignores row counts"


def test_an_empty_profile_has_no_value_area_rather_than_a_default_one():
    """An empty tape yields no rows and an empty histogram raises, rather than ``(0, 0, 0)``."""
    from historical.l2.producers.prof_tpo import value_area_ticks

    assert observations([]) == []
    assert profile_rows([]) == []
    with pytest.raises(ValueError, match="empty TPO profile"):
        value_area_ticks({})


def test_the_distribution_is_keyed_by_integer_ticks(prints):
    """Integer tick keys (``price_bucket_tick: 1``), not float prices, so stacks compare bitwise."""
    rows = profile_rows(prints)
    assert rows
    for row in rows:
        assert isinstance(row.price_tick, int)
        assert isinstance(row.tpo_count, int) and row.tpo_count >= 1
