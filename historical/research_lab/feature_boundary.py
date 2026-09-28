"""Loader and fail-closed validator for the L2 feature registry, ``specs/features/<feature>.yaml``.

Hard rules raise :class:`FeatureError` in both modes (see ADR 0037):
1. the name matches ``<family>_<measure>[_<qualifier>]*[_<unit>]_v<N>`` (:data:`FEATURE_RE`);
2. no time-shift token (:data:`BANNED_TOKENS`) appears as a name token;
3. ``family`` is in :data:`FAMILIES` and matches the name prefix;
4. ``cadence`` is in :data:`CADENCES`; per-update, per-trade and per-tick are refused;
5. every parameter set carries the family's mandatory keys (:func:`required_param_keys`);
6. ``param_set_id`` is a canonical hash, so ``60``, ``60.0`` and ``"60"`` share one id;
7. a non-zero parity ``tolerance`` needs a ``tolerance_reason`` (the default is 0, bitwise).
Soft findings form one :class:`FeatureWarning`, or raise with ``strict=True``.
"""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

__all__ = [
    "Feature",
    "FeatureError",
    "FeatureWarning",
    "FAMILIES",
    "BANNED_TOKENS",
    "CADENCES",
    "BANNED_CADENCES",
    "FEATURE_STATUSES",
    "WRITEABLE_STATUSES",
    "READABLE_STATUSES",
    "COMMON_PARAM_KEYS",
    "FAMILY_PARAM_KEYS",
    "FORBIDDEN_PARAM_KEYS",
    "MAX_PARAM_SETS",
    "MAX_NAME_LEN",
    "FEATURE_RE",
    "FEATURE_RE_SHAPE",
    "FEATURES_DIR",
    "SCHEMA_ID",
    "check_feature_name",
    "name_family",
    "name_rev",
    "canon_param_value",
    "canon_params",
    "param_set_id",
    "required_param_keys",
    "spec_sha",
    "validate_feature",
    "load_feature",
    "load_all_features",
    "rust_formula_ids",
    "repo_root",
]


class FeatureError(Exception):
    """A registry entry that must not reach a writer."""


class FeatureWarning(UserWarning):
    """Lax-mode findings: one warning per feature, listing every problem."""


# --- vocabularies (closed sets; widening one needs an ADR) ---
#: Families: ``ob`` order book, ``tape`` signed trades, ``prof`` auction profile,
#: ``xform`` transforms of a bar series, ``vn`` VN-market specifics.
FAMILIES: tuple[str, ...] = ("ob", "tape", "prof", "xform", "vn")

#: Time-shift tokens refused in names; a lag belongs in the query. Matched per token, so
#: ``futures`` is a legal token and ``fut`` is not.
BANNED_TOKENS: tuple[str, ...] = (
    "prev",
    "yday",
    "next",
    "fut",
    "fwd",
    "ahead",
    "lookahead",
    "future",
    "tmr",
)

#: Write cadences L2 accepts.
CADENCES: tuple[str, ...] = ("m1", "m5", "m15", "m30", "h1", "session")

#: Refused cadences, each with its own error message (storage budget).
BANNED_CADENCES: dict[str, str] = {
    "per_update": (
        "cadence 'per_update' is FORBIDDEN at L2: 420,987 book updates/session x 250 "
        "sessions = 105M rows/year for ONE feature, against a 1 GiB/year ceiling "
        "(~134M rows for the WHOLE layer) and 12.21 GiB of free disk. Use a per-bar "
        "reduction (that is what the mandatory `agg` param is for), recompute from L0 "
        "on read, or write throttled JSONL at L3."
    ),
    "per_trade": (
        "cadence 'per_trade' is FORBIDDEN at L2: 84,764 prints/session x 250 = 21.2M "
        "rows/year for ONE feature, so five such features consume 79% of the layer "
        "budget. Reduce to a bar cadence."
    ),
    "per_tick": (
        "cadence 'per_tick' is FORBIDDEN at L2 for the same budget reason as 'per_update'."
    ),
}

#: Feature lifecycle states.
FEATURE_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "IMPLEMENTED",
    "PARITY",
    "BACKFILLED",
    "LIVE",
    "DEPRECATED",
    "RETIRED",
)

#: Statuses whose rows a writer may put in ``default.l2_observation``.
WRITEABLE_STATUSES: frozenset[str] = frozenset({"PARITY", "BACKFILLED", "LIVE"})

#: Statuses an observation reader returns by default.
READABLE_STATUSES: frozenset[str] = frozenset({"BACKFILLED", "LIVE"})

#: Parameter keys mandatory for every family.
COMMON_PARAM_KEYS: tuple[str, ...] = (
    "cadence",
    "symbol_scope",
    "session_reset",
    "seam_13h",
    "warmup_bars",
)

#: Additional mandatory parameter keys per family.
FAMILY_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    "ob": ("levels", "agg", "stale_ms"),
    "tape": ("sign_method", "unknown_side", "window"),
    "prof": (
        "session",
        "price_bucket_tick",
        "tpo_period_min",
        "va_pct",
        "va_tiebreak",
        "poc_tiebreak",
    ),
    "xform": ("source", "window"),
    "vn": ("reference", "expiry_rule"),
}

#: Column names and runtime facts; hashing them would give one definition many ids.
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "symbol",
        "event_time",
        "window_start",
        "code_version",
        "input_version",
        "feed_id",
        "path",
        "host",
        "computed_at",
        "producer",
    }
)

#: Maximum named parameter sets per feature; sweeps run in memory and are not written.
MAX_PARAM_SETS = 8

#: Keeps names readable and the ``LowCardinality(String)`` dictionary small.
MAX_NAME_LEN = 40

_PARAM_ENUMS: dict[str, tuple[str, ...]] = {
    "cadence": CADENCES,
    "symbol_scope": ("single", "continuous"),
    "session_reset": ("none", "open", "segment"),
    "seam_13h": ("none", "rollover", "reset"),
    "agg": ("last", "first", "max", "min", "mean", "sum"),
    "sign_method": ("venue_flag", "lee_ready", "tick_rule"),
    "unknown_side": ("zero", "drop"),
    "session": ("ict_full", "ict_am", "ict_pm", "calendar_day"),
    "va_tiebreak": ("lower", "upper", "volume"),
    "poc_tiebreak": ("mid_then_higher", "first", "last"),
    "source": ("close", "open", "high", "low", "mid", "vwap"),
    "expiry_rule": ("krx_code", "calendar_thursday"),
}

#: Units allowed in a ``window`` parameter.
WINDOW_UNITS: tuple[str, ...] = ("bar", "minute", "hour", "session")

#: ``(key, min, max)`` for integer parameters; ``None`` means unbounded.
_PARAM_INT_RANGES: tuple[tuple[str, int, int | None], ...] = (
    ("warmup_bars", 0, None),
    ("levels", 1, 10),
    ("stale_ms", 0, None),
    ("price_bucket_tick", 1, None),
    ("tpo_period_min", 1, None),
)

#: Validation states of the L3 trigger contract.
VALIDATION_STATES: tuple[str, ...] = (
    "literature-only",
    "offline-replayed",
    "paper-forward-tested",
    "market-open-verified",
)

_ROOT_KEYS = (
    "schema",
    "feature",
    "family",
    "title",
    "unit",
    "dtype",
    "cadence",
    "signal",
    "inputs",
    "depends_on",
    "params",
    "impl",
    "parity",
    "derives_trigger",
    "maps_to_validation_state",
    "status",
    "pinned_code_version",
    "supersedes",
    "superseded_by",
    "retention",
    "note",
    "next_action",
)
_SIGNAL_KEYS = (
    "causal",
    "lookahead_ms",
    "window_start_rule",
    "event_time_rule",
    "is_final_rule",
)
_IMPL_KEYS = ("rust", "python", "authority")
_PARITY_KEYS = ("tolerance", "tolerance_reason", "golden_tape", "test", "parity_waiver")

#: Fields excluded from :func:`spec_sha` so they stay editable after a freeze.
_SPEC_SHA_EXCLUDED = ("status", "note", "next_action")

SCHEMA_ID = "l2.feature/1"


def repo_root() -> Path:
    """Return the repository root, derived from this file's location (not the cwd)."""
    return Path(__file__).resolve().parents[2]


FEATURES_DIR = repo_root() / "specs" / "features"
_RUST_TYPES_RS = (
    repo_root()
    / "native_platform"
    / "apps"
    / "client-desktop"
    / "src"
    / "signal_engine"
    / "types.rs"
)


# --- naming grammar ---
# shape and banned tokens in one expression (negative lookahead)
FEATURE_RE = re.compile(
    r"^(?!.*(?:^|_)(?:prev|yday|next|fut|fwd|ahead|lookahead|future|tmr)(?:_|$))"
    r"(?:ob|tape|prof|xform|vn)"
    r"(?:_[a-z][a-z0-9]*)+"
    r"_v[1-9][0-9]*$"
)

# Lookahead-free shape check for the Rust `regex` crate, paired with a separate token scan;
# `test_l2_registry` asserts that both routes agree on every name in its table.
FEATURE_RE_SHAPE = re.compile(r"^(ob|tape|prof|xform|vn)(_[a-z][a-z0-9]*)+_v[1-9][0-9]*$")


def check_feature_name(name: str) -> list[str]:
    """Return every problem with ``name``; an empty list means the name is legal."""
    if not isinstance(name, str) or not name:
        return ["feature name must be a non-empty string"]
    errs: list[str] = []
    if len(name) > MAX_NAME_LEN:
        errs.append(f"name longer than {MAX_NAME_LEN} chars ({len(name)})")
    if "__" in name:
        errs.append("double underscore")
    if not FEATURE_RE_SHAPE.match(name):
        errs.append(
            "shape: <family>_<measure>[_<qualifier>]*[_<unit>]_v<N> "
            f"with family in {list(FAMILIES)}"
        )
    hits = [t for t in BANNED_TOKENS if t in name.split("_")]
    if hits:
        errs.append(f"time-shift token(s) {hits}; a lag belongs in the query, not the name")
    return errs


def name_family(name: str) -> str | None:
    """Return the family prefix of a legal name, or None if the name is illegal."""
    m = FEATURE_RE_SHAPE.match(name or "")
    return m.group(1) if m else None


def name_rev(name: str) -> int | None:
    """Return the ``_v<N>`` revision of a legal name, or None."""
    if not FEATURE_RE_SHAPE.match(name or ""):
        return None
    m = re.search(r"_v([1-9][0-9]*)$", name)
    return int(m.group(1)) if m else None


# --- canonical parameter hash ---
def canon_param_value(v: Any) -> Any:
    """Canonicalise one parameter value so that ``60``, ``60.0`` and ``"60"`` all give ``"60"``.

    Numbers go through :class:`~decimal.Decimal`, booleans become ``"true"``/``"false"``,
    mappings are key-sorted and lists keep their order. ``None`` is refused: declare an explicit
    enum value (``none``, ``unset``) instead.
    """
    if v is None:
        raise FeatureError(
            "None is not allowed in params: declare an explicit enum value "
            "('none'/'unset') so 'undeclared' and 'declared empty' cannot collide"
        )
    if isinstance(v, bool):  # before int: bool is a subclass of int
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        try:
            d = Decimal(str(v)).normalize()
        except InvalidOperation as exc:
            raise FeatureError(f"param value is not a finite number: {v!r}") from exc
        if not d.is_finite():
            raise FeatureError(f"param value is not a finite number: {v!r}")
        if d == d.to_integral_value():
            d = d.quantize(Decimal(1))
        return format(d, "f")  # never exponent notation
    if isinstance(v, str):
        return v
    if isinstance(v, Mapping):
        return {str(k): canon_param_value(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [canon_param_value(x) for x in v]  # list order is meaningful; never sorted
    raise FeatureError(f"param value type not allowed: {type(v).__name__} ({v!r})")


def canon_params(params: Mapping[str, Any]) -> str:
    """Return the canonical JSON of a parameter set: the input to :func:`param_set_id`."""
    if not isinstance(params, Mapping):
        raise FeatureError(f"params must be a mapping, got {type(params).__name__}")
    body = {str(k): canon_param_value(params[k]) for k in sorted(params, key=str)}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def param_set_id(params: Mapping[str, Any]) -> str:
    """Return the parameter-set id: ``p`` plus 12 hex chars of BLAKE2b over :func:`canon_params`."""
    digest = hashlib.blake2b(canon_params(params).encode("utf-8"), digest_size=6).hexdigest()
    return "p" + digest


def required_param_keys(family: str) -> tuple[str, ...]:
    """Return the mandatory parameter keys for ``family``: the common keys plus its own."""
    if family not in FAMILIES:
        raise FeatureError(f"unknown family {family!r}; the closed set is {list(FAMILIES)}")
    return tuple(COMMON_PARAM_KEYS) + FAMILY_PARAM_KEYS[family]


# --- spec hash ---
def _canon_doc(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _canon_doc(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canon_doc(x) for x in value]
    return str(value)


def spec_sha(data: Mapping[str, Any]) -> str:
    """Return the first 16 hex chars of SHA-256 over the entry, excluding the prose fields.

    Detects edits to a frozen entry; ``status``, ``note`` and ``next_action`` stay editable.
    """
    body = {k: _canon_doc(v) for k, v in data.items() if k not in _SPEC_SHA_EXCLUDED}
    blob = json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --- Rust introspection (read-only) ---
@lru_cache(maxsize=1)
def rust_formula_ids() -> frozenset[str]:
    """Return the L3 ``formula_id`` strings parsed from the signal engine's ``types.rs``.

    Returns an empty set when the file is unreadable; ``derives_trigger`` is then reported as
    unchecked instead of failing the load.
    """
    try:
        text = _RUST_TYPES_RS.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    m = re.search(
        r"fn\s+formula_id\s*\([^)]*\)\s*->\s*&'static\s+str\s*\{(.*?)\n\s{4}\}", text, re.DOTALL
    )
    body = m.group(1) if m else text
    return frozenset(re.findall(r'=>\s*"([a-z][a-z0-9_]*_v[0-9]+)"', body))


# --- typed entry ---
@dataclass(frozen=True)
class SignalBlock:
    causal: bool | None = None
    lookahead_ms: int = 0
    window_start_rule: str | None = None
    event_time_rule: str | None = None
    is_final_rule: str | None = None


@dataclass(frozen=True)
class ImplBlock:
    rust: str | None = None
    python: str | None = None
    authority: str | None = None


@dataclass(frozen=True)
class ParityBlock:
    tolerance: float = 0.0
    tolerance_reason: str | None = None
    golden_tape: str | None = None
    test: str | None = None
    parity_waiver: str | None = None


@dataclass(frozen=True)
class Feature:
    """One L2 feature, declared as data."""

    feature: str
    family: str
    status: str
    cadence: str
    unit: str
    title: str = ""
    dtype: str = "float64"
    signal: SignalBlock = field(default_factory=SignalBlock)
    inputs: tuple[dict, ...] = ()
    depends_on: tuple[str, ...] = ()
    param_sets: dict[str, dict] = field(default_factory=dict)
    param_set_ids: dict[str, str] = field(default_factory=dict)
    impl: ImplBlock = field(default_factory=ImplBlock)
    parity: ParityBlock = field(default_factory=ParityBlock)
    derives_trigger: str | None = None
    maps_to_validation_state: str | None = None
    pinned_code_version: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    retention: str = "unbounded"
    note: str = ""
    next_action: str = ""
    spec_sha: str = ""
    path: Path | None = field(default=None, repr=False)
    problems: tuple[str, ...] = field(default=(), repr=False)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def rev(self) -> int | None:
        return name_rev(self.feature)

    @property
    def is_writeable(self) -> bool:
        """True if a writer may put rows for this feature into ``default.l2_observation``."""
        return self.status in WRITEABLE_STATUSES

    def default_param_set_id(self) -> str:
        return self.param_set_ids["default"]


# --- validation ---
def _sub_map(section: str, got: Any, known: Iterable[str], problems: list[str]) -> dict:
    if got is None:
        problems.append(f"'{section}' is absent (declare it, even as an empty mapping)")
        return {}
    if not isinstance(got, Mapping):
        raise FeatureError(f"'{section}' must be a mapping, got {type(got).__name__}")
    data = dict(got)
    for key in sorted(set(data) - set(known)):
        problems.append(f"'{section}.{key}' is not a known key")
    return data


def _check_param_value(where: str, key: str, value: Any) -> list[str]:
    """Return the hard errors for one parameter value; empty when it is acceptable."""
    errs: list[str] = []
    if key in _PARAM_ENUMS:
        if not (isinstance(value, str) and value in _PARAM_ENUMS[key]):
            errs.append(f"{where}.{key} = {value!r} is not one of {list(_PARAM_ENUMS[key])}")
        return errs
    for int_key, lo, hi in _PARAM_INT_RANGES:
        if key != int_key:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errs.append(f"{where}.{key} must be an integer, got {value!r}")
        elif value < lo or (hi is not None and value > hi):
            bound = f">= {lo}" if hi is None else f"in {lo}..{hi}"
            errs.append(f"{where}.{key} must be {bound}, got {value!r}")
    if key == "va_pct":
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            errs.append(f"{where}.{key} must be a number, got {value!r}")
        elif not 0 < float(value) <= 1:
            errs.append(f"{where}.{key} must be in (0, 1], got {value!r}")
    if key == "window":
        if not isinstance(value, Mapping) or "value" not in value or "unit" not in value:
            errs.append(f"{where}.{key} must be a mapping with 'value' and 'unit', got {value!r}")
        elif value["unit"] not in WINDOW_UNITS:
            errs.append(f"{where}.{key}.unit {value['unit']!r} is not one of {list(WINDOW_UNITS)}")
    return errs


def _validate(data: Any, path: Path | None) -> Feature:
    where = f" [{path}]" if path is not None else ""
    if not isinstance(data, Mapping):
        raise FeatureError(f"feature entry root must be a mapping{where}")
    data = dict(data)
    problems: list[str] = []

    for key in sorted(set(data) - set(_ROOT_KEYS)):
        problems.append(f"'{key}' is not a known top-level key")

    if data.get("schema") != SCHEMA_ID:
        problems.append(f"'schema' should be {SCHEMA_ID!r}, got {data.get('schema')!r}")

    # --- rules 1-2: name ---
    name = data.get("feature")
    if not isinstance(name, str) or not name.strip():
        raise FeatureError(f"'feature' is required and must be a non-empty string{where}")
    name = name.strip()
    name_errs = check_feature_name(name)
    if name_errs:
        raise FeatureError(f"feature name {name!r} is illegal: " + "; ".join(name_errs) + where)
    if path is not None and path.stem != name:
        raise FeatureError(
            f"file name {path.name!r} must be {name}.yaml -- one file per feature, named "
            f"after it, so a missing entry shows up as a missing file{where}"
        )

    # --- rule 3: family ---
    family = data.get("family")
    if family not in FAMILIES:
        raise FeatureError(
            f"'family' {family!r} is not one of the closed set {list(FAMILIES)}; widening "
            f"it is an ADR change, not an edit{where}"
        )
    if family != name_family(name):
        raise FeatureError(
            f"'family' {family!r} disagrees with the name prefix {name_family(name)!r} in "
            f"{name!r}{where}"
        )

    # --- rule 4: cadence ---
    cadence = data.get("cadence")
    if isinstance(cadence, str) and cadence in BANNED_CADENCES:
        raise FeatureError(f"{BANNED_CADENCES[cadence]}{where}")
    if cadence not in CADENCES:
        raise FeatureError(f"'cadence' {cadence!r} is not one of {list(CADENCES)}{where}")

    status = data.get("status")
    if not isinstance(status, str) or status not in FEATURE_STATUSES:
        raise FeatureError(
            f"'status' must be one of {list(FEATURE_STATUSES)}, got {status!r}{where}"
        )

    unit = data.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise FeatureError(
            f"'unit' is required even when the name already carries a unit token{where}"
        )

    dtype = data.get("dtype", "float64")
    if dtype != "float64":
        problems.append(
            f"'dtype' {dtype!r}: default.l2_observation.value is Float64 and a long-format "
            f"SCALAR is the only shape L2 has"
        )

    # --- causality ---
    signal_raw = _sub_map("signal", data.get("signal"), _SIGNAL_KEYS, problems)
    causal = signal_raw.get("causal")
    lookahead = signal_raw.get("lookahead_ms", 0)
    if causal is not None and not isinstance(causal, bool):
        raise FeatureError(f"'signal.causal' must be a bool, got {causal!r}{where}")
    if isinstance(lookahead, bool) or not isinstance(lookahead, int):
        raise FeatureError(f"'signal.lookahead_ms' must be an integer, got {lookahead!r}{where}")
    if lookahead < 0:
        raise FeatureError(f"'signal.lookahead_ms' must be >= 0, got {lookahead}{where}")
    if causal and lookahead != 0:
        raise FeatureError(
            f"'signal.causal: true' with lookahead_ms={lookahead}: a causal feature may not "
            f"look forward. Set lookahead_ms: 0, or declare causal: false -- "
            f"load_observation filters non-causal rows out by DEFAULT{where}"
        )
    for key in ("window_start_rule", "event_time_rule", "is_final_rule"):
        if key not in signal_raw:
            problems.append(f"'signal.{key}' missing (declare it, or null)")

    # --- rules 5-6: parameter sets ---
    params_raw = data.get("params")
    if not isinstance(params_raw, Mapping) or not params_raw:
        raise FeatureError(
            f"'params' must be a non-empty mapping of set-name -> parameters, with at least "
            f"a 'default' set{where}"
        )
    if "default" not in params_raw:
        raise FeatureError(f"'params.default' is required{where}")
    if len(params_raw) > MAX_PARAM_SETS:
        raise FeatureError(
            f"{len(params_raw)} named param sets for one feature; the ceiling is "
            f"{MAX_PARAM_SETS}. L2 is not a parameter-sweep dump: a sweep runs in memory and "
            f"only NAMED sets are written{where}"
        )
    needed = required_param_keys(family)
    param_sets: dict[str, dict] = {}
    param_ids: dict[str, str] = {}
    for set_name in sorted(params_raw, key=str):
        pset = params_raw[set_name]
        if not isinstance(pset, Mapping):
            raise FeatureError(f"'params.{set_name}' must be a mapping{where}")
        pset = dict(pset)
        missing = [k for k in needed if k not in pset]
        if missing:
            raise FeatureError(
                f"'params.{set_name}' is missing mandatory key(s) {missing} for family "
                f"{family!r}. There are NO implicit defaults: a default that lives in code "
                f"changes every hash the day someone edits the constant{where}"
            )
        bad = sorted(set(pset) & FORBIDDEN_PARAM_KEYS) + sorted(
            k for k in pset if str(k).startswith("_")
        )
        if bad:
            raise FeatureError(
                f"'params.{set_name}' carries column/runtime key(s) {bad}; those are columns "
                f"of l2_observation, not parameters{where}"
            )
        if pset.get("cadence") != cadence:
            raise FeatureError(
                f"'params.{set_name}.cadence' = {pset.get('cadence')!r} disagrees with the "
                f"entry cadence {cadence!r}{where}"
            )
        value_errs: list[str] = []
        for key in sorted(pset, key=str):
            value_errs += _check_param_value(f"params.{set_name}", key, pset[key])
        if value_errs:
            raise FeatureError("; ".join(value_errs) + where)
        param_sets[str(set_name)] = pset
        param_ids[str(set_name)] = param_set_id(pset)  # raises on None / unhashable types
    by_id: dict[str, str] = {}
    for set_name, psid in sorted(param_ids.items()):
        if psid in by_id:
            raise FeatureError(
                f"param sets {by_id[psid]!r} and {set_name!r} hash to one param_set_id "
                f"({psid}); they are the same parameters under two names{where}"
            )
        by_id[psid] = set_name

    # --- rule 7: parity ---
    parity_raw = _sub_map("parity", data.get("parity"), _PARITY_KEYS, problems)
    tolerance = parity_raw.get("tolerance", 0)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float, Decimal)):
        raise FeatureError(f"'parity.tolerance' must be a number, got {tolerance!r}{where}")
    tolerance = float(tolerance)
    if tolerance < 0:
        raise FeatureError(f"'parity.tolerance' must be >= 0, got {tolerance}{where}")
    reason = parity_raw.get("tolerance_reason")
    if tolerance != 0 and not (isinstance(reason, str) and reason.strip()):
        raise FeatureError(
            f"'parity.tolerance' = {tolerance} with no 'tolerance_reason'. The default is 0 "
            f"(bitwise): CVD is a sum of integer volume and a POC is a price bucketed to "
            f"whole ticks, so there is no physical reason for the two stacks to differ. A "
            f"tolerance with no reason is a bug being allowed{where}"
        )
    if tolerance == 0 and isinstance(reason, str) and reason.strip():
        problems.append(
            "'parity.tolerance_reason' is set while tolerance is 0 (bitwise); the reason "
            "describes a tolerance that does not exist"
        )

    impl_raw = _sub_map("impl", data.get("impl"), _IMPL_KEYS, problems)
    authority = impl_raw.get("authority")
    if authority not in ("rust", "python"):
        raise FeatureError(
            f"'impl.authority' must be 'rust' or 'python' (who owns the DEFINITION), got "
            f"{authority!r}{where}"
        )
    if not impl_raw.get(authority):
        raise FeatureError(
            f"'impl.authority' is {authority!r} but 'impl.{authority}' is empty{where}"
        )
    if status in WRITEABLE_STATUSES:
        if not parity_raw.get("test") and not parity_raw.get("parity_waiver"):
            raise FeatureError(
                f"status {status} requires 'parity.test' or a 'parity_waiver' carrying a "
                f"reason: no feature reaches l2_observation before its parity gate is "
                f"green{where}"
            )
        if impl_raw.get("rust") and impl_raw.get("python") and not parity_raw.get("golden_tape"):
            problems.append("'parity.golden_tape' missing while both stacks implement this feature")
        waiver = parity_raw.get("parity_waiver")
        if waiver is not None:
            # a waiver needs a written reason and only covers a single-stack feature
            if not isinstance(waiver, str) or len(waiver.strip()) < 40:
                raise FeatureError(
                    f"'parity.parity_waiver' must be a written reason, not a flag: got "
                    f"{waiver!r}. The gate message already promised 'carrying a reason' "
                    f"and then accepted anything truthy{where}"
                )
            if impl_raw.get("rust") and impl_raw.get("python"):
                raise FeatureError(
                    f"'parity.parity_waiver' is set while BOTH stacks implement this "
                    f"feature. A waiver exists for a single-stack feature; waiving the "
                    f"comparison when two implementations exist is precisely the defect "
                    f"this layer was built to stop{where}"
                )
        # a declared golden tape or parity test must exist on disk
        for key in ("golden_tape", "test"):
            declared = parity_raw.get(key)
            if not declared:
                continue
            # `parity.test` is `path/to/file.py::test_name`; the path part must exist
            rel, _, node = str(declared).partition("::")
            if not (repo_root() / rel).exists():
                raise FeatureError(
                    f"'parity.{key}' points at {rel!r}, which does not exist on disk. "
                    f"A feature at status {status} is claiming a green parity gate; a "
                    f"path nobody can open is not one{where}"
                )
            # and the named test function must be defined in that file
            if node:
                src = (repo_root() / rel).read_text(encoding="utf-8", errors="replace")
                base = node.split("[", 1)[0]  # parametrised ids: name[param]
                if not re.search(rf"^\s*(async\s+)?def\s+{re.escape(base)}\s*\(", src, re.M):
                    raise FeatureError(
                        f"'parity.{key}' names {base!r}, which is not defined in "
                        f"{rel}{where}"
                    )

    trigger = data.get("derives_trigger")
    if trigger:
        known = rust_formula_ids()
        if known and trigger not in known:
            problems.append(
                f"'derives_trigger' {trigger!r} is not a formula_id in "
                f"the desktop client's signal-engine types (not in this repository)"
            )
        elif not known:
            problems.append(
                f"'derives_trigger' could not be checked: "
                f"{_RUST_TYPES_RS.relative_to(repo_root()).as_posix()} unreadable"
            )
    vstate = data.get("maps_to_validation_state")
    if vstate is not None and vstate not in VALIDATION_STATES:
        problems.append(
            f"'maps_to_validation_state' {vstate!r} is not one of {list(VALIDATION_STATES)}"
        )
    for key in ("title", "inputs", "depends_on", "retention"):
        if key not in data:
            problems.append(f"'{key}' missing (declare it, or null)")
    if status == "DEPRECATED" and not data.get("superseded_by"):
        problems.append("status DEPRECATED without 'superseded_by'")

    inputs = data.get("inputs") or []
    if not isinstance(inputs, (list, tuple)):
        problems.append("'inputs' must be a list")
        inputs = []
    depends = data.get("depends_on") or []
    if not isinstance(depends, (list, tuple)):
        problems.append("'depends_on' must be a list")
        depends = []

    return Feature(
        feature=name,
        family=family,
        status=status,
        cadence=cadence,
        unit=unit.strip(),
        title=str(data.get("title") or ""),
        dtype=str(dtype),
        signal=SignalBlock(
            causal=causal,
            lookahead_ms=int(lookahead),
            window_start_rule=signal_raw.get("window_start_rule"),
            event_time_rule=signal_raw.get("event_time_rule"),
            is_final_rule=signal_raw.get("is_final_rule"),
        ),
        inputs=tuple(dict(i) for i in inputs if isinstance(i, Mapping)),
        depends_on=tuple(str(d) for d in depends),
        param_sets=param_sets,
        param_set_ids=param_ids,
        impl=ImplBlock(
            rust=impl_raw.get("rust"),
            python=impl_raw.get("python"),
            authority=authority,
        ),
        parity=ParityBlock(
            tolerance=tolerance,
            tolerance_reason=reason,
            golden_tape=parity_raw.get("golden_tape"),
            test=parity_raw.get("test"),
            parity_waiver=parity_raw.get("parity_waiver"),
        ),
        derives_trigger=trigger,
        maps_to_validation_state=vstate,
        pinned_code_version=data.get("pinned_code_version"),
        supersedes=data.get("supersedes"),
        superseded_by=data.get("superseded_by"),
        retention=str(data.get("retention") or "unbounded"),
        note=str(data.get("note") or ""),
        next_action=str(data.get("next_action") or ""),
        spec_sha=spec_sha(data),
        path=path,
        problems=tuple(problems),
        raw=data,
    )


def validate_feature(data: Any, *, path: Path | None = None, strict: bool = False) -> Feature:
    """Validate a parsed entry; hard rules raise, soft findings warn (or raise if ``strict``)."""
    feat = _validate(data, path)
    if feat.problems:
        header = f"feature {feat.feature} ({path if path is not None else '<memory>'}): "
        body = header + "; ".join(feat.problems)
        if strict:
            raise FeatureError(body)
        warnings.warn(body, FeatureWarning, stacklevel=2)
    return feat


def load_feature(path: str | Path, strict: bool = False) -> Feature:
    """Load ``specs/features/<feature>.yaml`` and validate it."""
    path = Path(path)
    if not path.exists():
        raise FeatureError(f"feature registry file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        raise FeatureError(f"feature registry file is empty: {path}")
    return validate_feature(data, path=path, strict=strict)


def load_all_features(directory: str | Path = FEATURES_DIR, strict: bool = False) -> list[Feature]:
    """Load every ``*.yaml`` in ``directory``, sorted by feature name; duplicate names raise."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FeatureError(f"features directory not found: {directory}")
    feats = [load_feature(p, strict=strict) for p in sorted(directory.glob("*.yaml"))]
    names = [f.feature for f in feats]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise FeatureError(f"duplicate feature name(s) in {directory}: {dupes}")
    return sorted(feats, key=lambda f: f.feature)
