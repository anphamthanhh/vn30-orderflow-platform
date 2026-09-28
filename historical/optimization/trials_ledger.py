"""Append-only trials ledger: the trial count and Sharpe dispersion behind the deflated Sharpe.

Each line of ``historical/research_lab/trials.jsonl`` is one hypothesis or parameter sweep
(schema v2; v1 rows load with defaults)::

    {"v":2,"ts":"YYYY-MM-DD","hypothesis_id":str,"family":str,"n_combos":int>=1,
     "is_sharpe":float|null,"sharpe_footing":"per_trade"|"per_daily"|"per_bar",
     "estimate":bool,"supersedes":[uid,...],"rescreen_of":str,"verdict":str,"notes":str}

Superseded rows stay in the file but leave the effective set; a repeated ``hypothesis_id``
must declare ``rescreen_of``. Estimated Sharpes count as trials but not toward the dispersion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from scipy import stats

from historical.optimization.dsr import deflated_sharpe_ratio, expected_max_sharpe

PathLike = Union[str, Path]

LEDGER_SCHEMA_VERSION = 2

# --- Sharpe footing: the observation frequency a Sharpe is measured on ------
FOOTING_PER_TRADE = "per_trade"
FOOTING_PER_DAILY = "per_daily"
FOOTING_PER_BAR = "per_bar"
VALID_FOOTINGS = (FOOTING_PER_TRADE, FOOTING_PER_DAILY, FOOTING_PER_BAR)

#: Footing assumed for rows without the field (v1 rows are per-daily Sharpes).
LEGACY_DEFAULT_FOOTING = FOOTING_PER_DAILY


class LedgerError(ValueError):
    """The ledger is malformed or violates the v2 schema; the message names the line."""


class FootingMismatch(LedgerError):
    """The candidate's Sharpe footing is invalid or absent from the ledger."""


@dataclass
class TrialRecord:
    """One evaluated hypothesis / parameter sweep in the trials ledger."""

    hypothesis_id: str
    family: str
    n_combos: int
    is_sharpe: float  # per-observation on ``sharpe_footing``; NaN when unrecorded
    verdict: str
    ts: str = ""
    notes: str = ""
    sharpe_footing: str = LEGACY_DEFAULT_FOOTING
    estimate: bool = False
    supersedes: Tuple[str, ...] = ()
    rescreen_of: str = ""
    uid: str = ""
    v: int = LEDGER_SCHEMA_VERSION
    line_no: int = 0

    # False only for an execution measurement (fill rate, queue position, latency) that
    # selects no alpha: such rows need `notes` and are left out of `total_trials`.
    # See historical/research_lab/PRE_REGISTRATION.md for when a measurement counts as a trial.
    selects_alpha: bool = True


def default_ledger_path() -> Path:
    """Canonical ledger location: ``historical/research_lab/trials.jsonl``."""
    return Path(__file__).resolve().parents[1] / "research_lab" / "trials.jsonl"


def make_uid(hypothesis_id: str, ts: str, ordinal: int = 0) -> str:
    """Record UID ``"<hypothesis_id>@<ts>#<ordinal>"``.

    ``ordinal`` counts earlier records with the same id and date, so UIDs stay stable while the
    file is only appended to.
    """
    return f"{hypothesis_id}@{ts}#{int(ordinal)}"


def append_trial(record: TrialRecord, path: PathLike) -> None:
    """Append one record to the ledger as a single JSON line.

    One write in append mode, never a read-modify-write, so concurrent appends do not lose
    records. The caller sets ``record.ts`` (ICT date). Raises :class:`LedgerError` for an
    invalid footing, ``n_combos < 1``, or ``selects_alpha=False`` without ``notes``.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if str(record.sharpe_footing) not in VALID_FOOTINGS:
        raise LedgerError(f"sharpe_footing {record.sharpe_footing!r} not one of {VALID_FOOTINGS}")
    if int(record.n_combos) < 1:
        raise LedgerError(
            f"n_combos must be >= 1 (got {record.n_combos}) for "
            f"{record.hypothesis_id!r}: a trial that consumed no configuration is "
            "not a trial; a re-screen of seen data costs at least one"
        )
    obj = {
        "v": LEDGER_SCHEMA_VERSION,
        "ts": record.ts,
        "hypothesis_id": record.hypothesis_id,
        "family": record.family,
        "n_combos": int(record.n_combos),
        "is_sharpe": None if not np.isfinite(record.is_sharpe) else float(record.is_sharpe),
        "sharpe_footing": str(record.sharpe_footing),
        "estimate": bool(record.estimate),
        "verdict": record.verdict,
        "notes": record.notes,
    }
    if record.supersedes:
        obj["supersedes"] = list(record.supersedes)
    if record.rescreen_of:
        obj["rescreen_of"] = str(record.rescreen_of)
    if not record.selects_alpha:
        # written only when False; an absent field means True
        if not record.notes.strip():
            raise LedgerError(
                f"{record.hypothesis_id!r}: selects_alpha=False needs 'notes' saying what "
                "was measured and why it selects no alpha (ADR 0030). An exclusion from "
                "the trial count makes every card look better; it may not be silent."
            )
        obj["selects_alpha"] = False
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)  # one write() call -> atomic under O_APPEND
        fh.flush()


def _parse_line(d: dict, path: Path, line_no: int, ordinal: int) -> TrialRecord:
    raw_sr = d.get("is_sharpe", None)
    try:
        sr = float(raw_sr) if raw_sr is not None else float("nan")
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{path}:{line_no}: is_sharpe is not a number: {raw_sr!r}") from exc
    try:
        n_combos = int(d.get("n_combos", 1))
    except (TypeError, ValueError) as exc:
        raise LedgerError(
            f"{path}:{line_no}: n_combos is not an integer: {d.get('n_combos')!r}"
        ) from exc
    hid = str(d.get("hypothesis_id", ""))
    ts = str(d.get("ts", ""))
    sup = d.get("supersedes", ()) or ()
    if isinstance(sup, str):
        sup = (sup,)
    return TrialRecord(
        hypothesis_id=hid,
        family=str(d.get("family", "")),
        n_combos=n_combos,
        is_sharpe=sr,
        verdict=str(d.get("verdict", "")),
        ts=ts,
        notes=str(d.get("notes", "")),
        sharpe_footing=str(d.get("sharpe_footing", LEGACY_DEFAULT_FOOTING)),
        estimate=bool(d.get("estimate", False)),
        supersedes=tuple(str(s) for s in sup),
        rescreen_of=str(d.get("rescreen_of", "")),
        uid=str(d.get("uid", "") or make_uid(hid, ts, ordinal)),
        v=int(d.get("v", 1)),
        line_no=line_no,
        # absent means True: an unmarked row counts as a trial
        selects_alpha=bool(d.get("selects_alpha", True)),
    )


def _read_records(path: PathLike) -> List[TrialRecord]:
    """Parse every record, skipping blank and ``#`` lines; errors name the file and line."""
    records: List[TrialRecord] = []
    p = Path(path)
    if not p.exists():
        return records
    seen: Dict[Tuple[str, str], int] = {}
    with open(p, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(
                    f"{p}:{line_no}: invalid JSON ({exc.msg} at col {exc.colno}). "
                    f"Offending line: {line[:160]!r}. The ledger is hand-annotated — "
                    "fix this line; do NOT delete it (it is a pre-registration artifact)."
                ) from exc
            if not isinstance(d, dict):
                raise LedgerError(f"{p}:{line_no}: expected a JSON object, got {type(d).__name__}")
            key = (str(d.get("hypothesis_id", "")), str(d.get("ts", "")))
            ordinal = seen.get(key, 0)
            seen[key] = ordinal + 1
            records.append(_parse_line(d, p, line_no, ordinal))
    return records


def validate_ledger(records: Sequence[TrialRecord]) -> List[str]:
    """Return the v2 schema violations as readable strings; empty when the ledger is clean.

    Pass the effective (post-supersede) set, so that a correction row can fix a violation in
    the row it supersedes.
    """
    issues: List[str] = []
    for r in records:
        if r.n_combos < 1:
            issues.append(
                f"line {r.line_no}: {r.hypothesis_id!r} has n_combos={r.n_combos} "
                "(must be >= 1; a re-screen of already-seen data still costs a trial)"
            )
        if r.sharpe_footing not in VALID_FOOTINGS:
            issues.append(
                f"line {r.line_no}: {r.hypothesis_id!r} has sharpe_footing="
                f"{r.sharpe_footing!r}, not one of {VALID_FOOTINGS}"
            )
        if not r.selects_alpha and not r.notes.strip():
            # an excluded row can lower the DSR benchmark for every candidate, so it must say why
            issues.append(
                f"line {r.line_no}: {r.hypothesis_id!r} sets selects_alpha=false but "
                "gives no 'notes'. A row excluded from the trial count must say what it "
                "measured and why that measurement selects no alpha (ADR 0030)"
            )
    by_id: Dict[str, List[TrialRecord]] = {}
    for r in records:
        by_id.setdefault(r.hypothesis_id, []).append(r)
    for hid, rows in by_id.items():
        if len(rows) > 1:
            undeclared = [r for r in rows[1:] if not r.rescreen_of]
            if undeclared:
                lines = ", ".join(str(r.line_no) for r in rows)
                issues.append(
                    f"hypothesis_id {hid!r} appears {len(rows)}x (lines {lines}) without "
                    "'rescreen_of' on the repeats: either give each record a distinct id, "
                    "declare it a re-screen, or supersede the stale one"
                )
    return issues


def load_trials(path: PathLike, strict: bool = True) -> List[TrialRecord]:
    """Load the effective record set from a ledger file; a missing file gives ``[]``.

    Blank and ``#`` lines are skipped. Records named in another record's ``supersedes`` are
    dropped from the result but stay in the file. With ``strict=True`` an unknown
    ``supersedes`` target or any schema violation raises :class:`LedgerError`.
    """
    raw = _read_records(path)
    superseded: Dict[str, str] = {}
    known = {r.uid for r in raw}
    for r in raw:
        for target in r.supersedes:
            if target not in known and strict:
                raise LedgerError(
                    f"line {r.line_no}: supersedes unknown record uid {target!r}. "
                    f"UIDs are '<hypothesis_id>@<ts>#<ordinal>'."
                )
            superseded[target] = r.uid
    effective = [r for r in raw if r.uid not in superseded]
    issues = validate_ledger(effective)
    if issues and strict:
        raise LedgerError(
            f"{Path(path)} violates trials-ledger schema v{LEDGER_SCHEMA_VERSION}:\n  - "
            + "\n  - ".join(issues)
        )
    return effective


def superseded_records(path: PathLike) -> List[TrialRecord]:
    """Records retired by a later record's ``supersedes``."""
    raw = _read_records(path)
    superseded = {t for r in raw for t in r.supersedes}
    return [r for r in raw if r.uid in superseded]


def total_trials(records: Sequence[TrialRecord]) -> int:
    """Trial count ``N``: the sum of ``n_combos`` over records with ``selects_alpha=True``.

    Schema v2 requires ``n_combos >= 1``, so every re-screen of seen data adds to the count.
    """
    return int(sum(max(int(r.n_combos), 0) for r in records if r.selects_alpha))


def excluded_from_trial_count(records: Sequence[TrialRecord]) -> List[TrialRecord]:
    """Records left out of :func:`total_trials` (``selects_alpha=False``), for reporting."""
    return [r for r in records if not r.selects_alpha]


def trial_sharpes(
    records: Sequence[TrialRecord],
    sharpe_footing: Optional[str] = None,
    include_estimates: bool = False,
) -> List[float]:
    """Finite recorded Sharpes for the dispersion estimate ``var_sr``.

    Skips ``estimate=True`` rows (not measurements) unless ``include_estimates`` is set, and
    keeps only records on ``sharpe_footing`` when it is given.
    """
    out: List[float] = []
    for r in records:
        if not np.isfinite(r.is_sharpe):
            continue
        if r.estimate and not include_estimates:
            continue
        if sharpe_footing is not None and r.sharpe_footing != sharpe_footing:
            continue
        out.append(float(r.is_sharpe))
    return out


def ledger_footings(records: Sequence[TrialRecord]) -> List[str]:
    """Distinct footings present among records that carry a finite Sharpe."""
    return sorted({r.sharpe_footing for r in records if np.isfinite(r.is_sharpe)})


def candidate_sharpe_stats(returns: Sequence[float]) -> Tuple[float, float, float]:
    """(per-observation Sharpe, skew, non-excess kurtosis) of the finite returns."""
    a = np.asarray(list(returns), dtype=float)
    a = a[np.isfinite(a)]
    if a.size < 2:
        return 0.0, 0.0, 3.0
    sd = float(a.std(ddof=1))
    sr = float(a.mean() / sd) if sd > 0 else 0.0
    return sr, float(stats.skew(a, bias=False)), float(stats.kurtosis(a, fisher=False, bias=False))


@dataclass(frozen=True)
class Deflation:
    """Trials-deflated Sharpe result, or the reason it is unavailable (``available=False``)."""

    available: bool
    reason: str = ""
    dsr: float = float("nan")
    n_trials: int = 0
    var_sr: float = float("nan")
    sr0: float = float("nan")
    n_sharpes: int = 0
    sharpe_footing: str = ""
    candidate_sharpe: float = float("nan")
    n_obs: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "available": bool(self.available),
            "reason": self.reason,
            "dsr": float(self.dsr),
            "n_trials": int(self.n_trials),
            "var_sr": float(self.var_sr),
            "sr0": float(self.sr0),
            "n_sharpes": int(self.n_sharpes),
            "sharpe_footing": self.sharpe_footing,
            "candidate_sharpe": float(self.candidate_sharpe),
            "n_obs": int(self.n_obs),
        }


def deflated_sharpe_for_returns(
    returns: Sequence[float],
    records: Sequence[TrialRecord],
    sharpe_footing: str,
) -> Deflation:
    """Deflated Sharpe of a candidate's returns, with ``N`` and ``var_sr`` from the ledger.

    Only ledger Sharpes on ``sharpe_footing`` (the frequency of ``returns``) feed ``var_sr``,
    and ``N = max(total_trials, number of those Sharpes)``. Raises :class:`FootingMismatch`
    for an unknown footing or when the ledger's Sharpes are all on other footings. Returns
    ``available=False`` with a reason when the data cannot support deflation; callers must
    not treat that as a pass.
    """
    if sharpe_footing not in VALID_FOOTINGS:
        raise FootingMismatch(f"sharpe_footing {sharpe_footing!r} not one of {VALID_FOOTINGS}")
    present = ledger_footings(records)
    if present and sharpe_footing not in present:
        raise FootingMismatch(
            f"REFUSING to deflate: candidate returns are on footing "
            f"{sharpe_footing!r} but the ledger records Sharpes on {present!r}. "
            "Deflating a Sharpe against dispersion measured on a different "
            "observation frequency is not a test of anything (the units differ by "
            "~sqrt(obs-per-day)). Feed the candidate's returns on the ledger's "
            "footing, or record the trials on the candidate's footing."
        )

    sharpes = trial_sharpes(records, sharpe_footing=sharpe_footing)
    n_trials = max(total_trials(records), len(sharpes))
    if len(sharpes) < 2:
        return Deflation(
            available=False,
            reason=(
                f"ledger has {len(sharpes)} measured Sharpes on footing "
                f"{sharpe_footing!r} (<2): no dispersion to deflate against"
            ),
            n_trials=n_trials,
            n_sharpes=len(sharpes),
            sharpe_footing=sharpe_footing,
        )
    var_sr = float(np.var(np.asarray(sharpes, dtype=float), ddof=1))
    if var_sr <= 0:
        return Deflation(
            available=False,
            reason="zero dispersion across recorded trial Sharpes",
            n_trials=n_trials,
            var_sr=var_sr,
            n_sharpes=len(sharpes),
            sharpe_footing=sharpe_footing,
        )
    a = np.asarray(list(returns), dtype=float)
    a = a[np.isfinite(a)]
    n_obs = int(a.size)
    if n_obs < 2:
        return Deflation(
            available=False,
            reason=f"candidate has {n_obs} finite observations (<2)",
            n_trials=n_trials,
            var_sr=var_sr,
            n_sharpes=len(sharpes),
            sharpe_footing=sharpe_footing,
        )
    sr, skew, kurt = candidate_sharpe_stats(a)
    sr0 = expected_max_sharpe(var_sr, n_trials)
    dsr = deflated_sharpe_ratio(sr, n_obs, var_sr, n_trials, skew, kurt)
    return Deflation(
        available=True,
        dsr=float(dsr),
        n_trials=int(n_trials),
        var_sr=float(var_sr),
        sr0=float(sr0),
        n_sharpes=len(sharpes),
        sharpe_footing=sharpe_footing,
        candidate_sharpe=float(sr),
        n_obs=n_obs,
    )
