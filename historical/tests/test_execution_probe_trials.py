"""Execution probes (``selects_alpha=False``) do not add to the trial count N."""

from __future__ import annotations

import json

import pytest

from historical.optimization import trials_ledger as tl


def _probe(**kw) -> tl.TrialRecord:
    base = dict(
        hypothesis_id="E1_passive_fill_probe",
        family="execution",
        n_combos=6,
        is_sharpe=float("nan"),
        verdict="MEASURED",
        ts="2026-09-02",
        notes="resting-limit fill rate by offset; calibrates a cost input, selects no alpha",
        selects_alpha=False,
    )
    base.update(kw)
    return tl.TrialRecord(**base)


def _card(**kw) -> tl.TrialRecord:
    base = dict(
        hypothesis_id="Z9_some_card",
        family="trend",
        n_combos=4,
        is_sharpe=0.02,
        verdict="KILL",
        ts="2026-09-02",
        notes="a card screen",
    )
    base.update(kw)
    return tl.TrialRecord(**base)


# --- defaults and the shipped ledger ---
def test_selects_alpha_defaults_true():
    assert _card().selects_alpha is True, (
        "an unmarked row must count. Every row on disk today is a card screen, and a "
        "card screen selects."
    )


def test_rows_on_disk_are_unaffected():
    """Excluded rows add nothing to N, and every exclusion states a reason."""
    records = tl.load_trials(tl.default_ledger_path(), strict=False)
    assert records, "ledger did not load; the rest of this file would pass vacuously"

    selecting = [r for r in records if r.selects_alpha]
    excluded = tl.excluded_from_trial_count(records)
    assert len(selecting) + len(excluded) == len(records)

    # N counts only the alpha-selecting rows
    assert tl.total_trials(records) == sum(max(int(r.n_combos), 0) for r in selecting)
    # dropping the excluded rows leaves N unchanged
    assert tl.total_trials(selecting) == tl.total_trials(records)

    # an exclusion lowers every card's bar, so each one must give a reason
    for r in excluded:
        assert r.notes.strip(), f"{r.hypothesis_id!r} is excluded from N without saying why"


def test_the_disk_invariant_is_not_vacuous():
    """The shipped ledger has excluded rows, so the exclusion path runs on real data."""
    records = tl.load_trials(tl.default_ledger_path(), strict=False)
    excluded = tl.excluded_from_trial_count(records)
    assert excluded, (
        "no row on disk sets selects_alpha=false, so the exclusion path is untested "
        "against real data. Either a probe/measurement row is missing, or this test "
        "and its sibling should be retired together."
    )
    # the excluded rows carry real weight
    assert sum(max(int(r.n_combos), 0) for r in excluded) > 0


def test_a_probe_row_does_not_raise_n():
    records = [_card(), _card(hypothesis_id="Z8_other", rescreen_of="")]
    before = tl.total_trials(records)
    after = tl.total_trials(records + [_probe()])
    assert after == before, "an execution measurement must not raise the bar for cards"


def test_a_card_row_does_raise_n():
    records = [_card()]
    assert tl.total_trials(records + [_card(hypothesis_id="Z8_other")]) > tl.total_trials(records)


# --- exclusions need a reason ---
def test_exclusion_without_a_reason_is_a_validation_error():
    issues = tl.validate_ledger([_probe(notes="")])
    assert issues and "selects_alpha" in issues[0]


def test_exclusion_with_a_reason_validates():
    assert tl.validate_ledger([_probe()]) == []


def test_writer_refuses_an_unexplained_exclusion(tmp_path):
    with pytest.raises(tl.LedgerError, match="selects_alpha"):
        tl.append_trial(_probe(notes="   "), tmp_path / "trials.jsonl")


def test_excluded_rows_are_reportable():
    records = [_card(), _probe()]
    excluded = tl.excluded_from_trial_count(records)
    assert [r.hypothesis_id for r in excluded] == ["E1_passive_fill_probe"], (
        "an exclusion must be listable, so a status line can show it rather than "
        "leaving a reader to infer it from a number that got smaller"
    )


# --- round trip ---
def test_probe_row_round_trips_through_the_file(tmp_path):
    path = tmp_path / "trials.jsonl"
    tl.append_trial(_card(), path)
    tl.append_trial(_probe(), path)
    back = tl.load_trials(path, strict=False)
    assert [r.selects_alpha for r in back] == [True, False]
    assert tl.total_trials(back) == 4  # the card's 4 combos; the probe's 6 excluded


def test_true_is_not_written_to_the_file(tmp_path):
    """An absent field means True, so ordinary rows omit it and ledger diffs stay small."""
    path = tmp_path / "trials.jsonl"
    tl.append_trial(_card(), path)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert "selects_alpha" not in row


def test_false_is_written_to_the_file(tmp_path):
    path = tmp_path / "trials.jsonl"
    tl.append_trial(_probe(), path)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["selects_alpha"] is False
