"""L2 feature registry: gate mutation tests, canonical parameter hash, DDL and status page."""

from __future__ import annotations

import copy
import re
import warnings
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from historical.research_lab import feature_boundary as fb
from historical.research_lab import l2_status_page as sp

REPO_ROOT = fb.repo_root()
FEATURES_DIR = REPO_ROOT / "specs" / "features"
DDL_PATH = (
    REPO_ROOT / "data_provider" / "orderflow" / "sql" / "migrations" / "009_l2_observations.sql"
)
PAGE_PATH = REPO_ROOT / "docs" / "L2_FEATURES.md"


# --- baseline: the smallest legal `tape` entry ---
def _baseline() -> dict:
    return {
        "schema": "l2.feature/1",
        "feature": "tape_cvd_lot_v1",
        "family": "tape",
        "title": "Cumulative volume delta",
        "unit": "lot",
        "dtype": "float64",
        "cadence": "m1",
        "signal": {
            "causal": True,
            "lookahead_ms": 0,
            "window_start_rule": "bar_open",
            "event_time_rule": "bar_close",
            "is_final_rule": "every_bar",
        },
        "inputs": [{"layer": "L0", "table": "default.vn30f_trades", "columns": ["price"]}],
        "depends_on": [],
        "params": {
            "default": {
                "cadence": "m1",
                "symbol_scope": "single",
                "session_reset": "open",
                "seam_13h": "rollover",
                "warmup_bars": 0,
                "sign_method": "venue_flag",
                "unknown_side": "zero",
                "window": {"value": 1, "unit": "bar"},
            }
        },
        "impl": {
            "rust": "native_platform/crates/orderflow-engine/src/cvd.rs::CvdTracker",
            "python": "historical/l2/producers/tape_cvd.py::produce",
            "authority": "rust",
        },
        "parity": {
            "tolerance": 0,
            "tolerance_reason": None,
            "golden_tape": "historical/tests/fixtures/l2_tape_cvd_synthetic_20210304_20210305.csv",
            "test": "historical/tests/test_l2_parity_cvd.py::test_cvd_lot_v1_matches_bitwise",
        },
        "derives_trigger": "cvd_divergence_v1",
        "maps_to_validation_state": "offline-replayed",
        "status": "PROPOSED",
        "pinned_code_version": None,
        "supersedes": None,
        "superseded_by": None,
        "retention": "unbounded",
        "note": "",
        "next_action": "",
    }


def _load(data: dict, *, strict: bool = False) -> fb.Feature:
    """Validate an entry in lax mode, ignoring warnings: gate rules must fire without ``strict``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", fb.FeatureWarning)
        return fb.validate_feature(data, path=None, strict=strict)


# --- mutation table: one row per rule the gate enforces ---
def _set(path: str, value):
    """Mutator that assigns ``value`` at a dotted path."""

    def apply(d: dict) -> None:
        keys = path.split(".")
        node = d
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value

    return apply


def _drop(path: str):
    def apply(d: dict) -> None:
        keys = path.split(".")
        node = d
        for k in keys[:-1]:
            node = node[k]
        del node[keys[-1]]

    return apply


def _many_param_sets(d: dict) -> None:
    base = d["params"]["default"]
    for i in range(fb.MAX_PARAM_SETS + 1):
        variant = dict(base)
        variant["warmup_bars"] = i + 1
        d["params"][f"set{i}"] = variant


def _duplicate_param_set(d: dict) -> None:
    d["params"]["also_default"] = dict(d["params"]["default"])


def _rename_default_set(d: dict) -> None:
    """A registry with parameter sets but no `default` one."""
    d["params"] = {"alt": d["params"]["default"]}


MUTATIONS: list[tuple[str, object, str]] = [
    # rule 1: naming grammar
    ("name_no_revision", _set("feature", "tape_cvd_lot"), "shape"),
    ("name_uppercase", _set("feature", "Tape_cvd_lot_v1"), "shape"),
    ("name_double_underscore", _set("feature", "tape__cvd_lot_v1"), "double underscore"),
    ("name_rev_zero", _set("feature", "tape_cvd_lot_v0"), "shape"),
    ("name_too_long", _set("feature", "tape_" + "a" * 40 + "_v1"), "longer than 40"),
    ("name_empty", _set("feature", "  "), "non-empty string"),
    # rule 2: banned time-shift tokens
    ("token_prev", _set("feature", "tape_prev_cvd_lot_v1"), "time-shift token"),
    ("token_fwd", _set("feature", "tape_cvd_fwd_lot_v1"), "time-shift token"),
    ("token_lookahead", _set("feature", "tape_lookahead_cvd_v1"), "time-shift token"),
    # rule 3: the five closed families
    ("family_unknown", _set("family", "flow"), "closed set"),
    ("family_not_in_name", _set("feature", "flow_cvd_lot_v1"), "shape"),
    ("family_mismatch", _set("family", "ob"), "disagrees with the name prefix"),
    # rule 4: cadence
    ("cadence_per_update", _set("cadence", "per_update"), "FORBIDDEN"),
    ("cadence_per_trade", _set("cadence", "per_trade"), "FORBIDDEN"),
    ("cadence_bogus", _set("cadence", "m2"), "is not one of"),
    (
        "cadence_param_disagrees",
        _set("params.default.cadence", "m5"),
        "disagrees with the entry cadence",
    ),
    # rule 5: mandatory parameter keys
    ("param_missing_common", _drop("params.default.seam_13h"), "missing mandatory key"),
    ("param_missing_family", _drop("params.default.sign_method"), "missing mandatory key"),
    ("param_missing_warmup", _drop("params.default.warmup_bars"), "missing mandatory key"),
    ("param_forbidden_key", _set("params.default.symbol", "VN30F1M"), "column/runtime key"),
    ("param_private_key", _set("params.default._hidden", 1), "column/runtime key"),
    ("param_bad_enum", _set("params.default.seam_13h", "lunch"), "is not one of"),
    ("param_bad_window_unit", _set("params.default.window", {"value": 1, "unit": "tick"}), "unit"),
    ("param_negative_warmup", _set("params.default.warmup_bars", -1), "must be >= 0"),
    # rule 6: canonical hash; `zwin` has no enum/range rule, so its null must be refused there
    ("param_none_value", _set("params.default.zwin", None), "None is not allowed"),
    ("param_sets_over_ceiling", _many_param_sets, "ceiling"),
    ("param_sets_duplicate", _duplicate_param_set, "same parameters under two names"),
    ("params_empty", _set("params", {}), "non-empty mapping"),
    ("params_missing_default", _rename_default_set, "'params.default' is required"),
    # rule 7: parity tolerance
    ("tolerance_without_reason", _set("parity.tolerance", 1e-9), "tolerance_reason"),
    (
        "tolerance_blank_reason",
        lambda d: (
            _set("parity.tolerance", 1e-9)(d),
            _set("parity.tolerance_reason", "   ")(d),
        ),
        "tolerance_reason",
    ),
    ("tolerance_negative", _set("parity.tolerance", -1), "must be >= 0"),
    # causality
    ("causal_with_lookahead", _set("signal.lookahead_ms", 5), "may not look forward"),
    ("negative_lookahead", _set("signal.lookahead_ms", -1), "must be >= 0"),
    # lifecycle and authority
    (
        "writeable_without_parity_test",
        lambda d: (_set("status", "LIVE")(d), _set("parity.test", None)(d)),
        "parity gate",
    ),
    ("status_unknown", _set("status", "GREEN"), "status"),
    ("authority_unknown", _set("impl.authority", "either"), "impl.authority"),
    ("authority_without_impl", _set("impl.rust", None), "is empty"),
    ("unit_missing", _set("unit", ""), "unit"),
    # declared parity artefacts must exist on disk
    ("golden_tape_missing_file",
     lambda d: (_set("status", "PARITY")(d),
                _set("parity.golden_tape", "historical/tests/fixtures/nope.csv")(d)),
     "does not exist on disk"),
    ("parity_test_missing_file",
     lambda d: (_set("status", "PARITY")(d),
                _set("parity.test", "historical/tests/no_such_test.py::t")(d)),
     "does not exist on disk"),
    # parity waivers, and a parity test that names a function its file does not define
    ("waiver_is_a_flag_not_a_reason",
     lambda d: (_set("status", "PARITY")(d), _set("parity.test", None)(d),
                _set("impl.rust", None)(d), _set("impl.authority", "python")(d),
                _set("parity.parity_waiver", "n/a")(d)),
     "written reason, not a flag"),
    ("waiver_while_both_stacks_implement",
     lambda d: (_set("status", "PARITY")(d),
                _set("parity.parity_waiver",
                     "there is no Rust implementation of this estimator and the live "
                     "engine does not compute it")(d)),
     "BOTH stacks implement"),
    ("parity_test_missing_function",
     lambda d: (_set("status", "PARITY")(d),
                _set("parity.test",
                     "historical/tests/test_l2_parity_cvd.py::test_never_written")(d)),
     "is not defined in"),
]


@pytest.mark.parametrize("case", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_mutation_is_caught(case):
    """Each mutation is rejected, and for the expected reason."""
    name, mutate, expected = case
    data = _baseline()
    mutate(data)
    with pytest.raises(fb.FeatureError) as exc:
        _load(data)
    assert expected in str(
        exc.value
    ), f"mutation {name!r} was rejected, but for the wrong reason: {exc.value}"


# --- rules 1-2: the one-regex and two-step name checks agree ---
_NAME_TABLE: tuple[str, ...] = (
    "tape_cvd_lot_v1",
    "tape_cvd_norst_lot_v1",
    "ob_obi_l1_frac_v1",
    "ob_microprice_pts_v1",
    "prof_tpo_poc_pts_v1",
    "prof_naked_vpoc_pts_v1",
    "xform_fracdiff_close_v1",
    "vn_days_to_expiry_ct_v1",
    "vn_basis_z_v12",
    "prof_futures_poc_pts_v1",
    "cvd",
    "tape_cvd_lot",
    "tape_cvd_lot_v0",
    "tape_cvd_lot_v01",
    "TAPE_CVD_LOT_V1",
    "tape__cvd_lot_v1",
    "zz_cvd_lot_v1",
    "tape_prev_cvd_lot_v1",
    "tape_cvd_yday_lot_v1",
    "next_cvd_lot_v1",
    "tape_cvd_lot_fwd_v1",
    "",
)


@pytest.mark.parametrize("name", _NAME_TABLE)
def test_python_lookahead_regex_and_rust_two_step_agree(name):
    """``FEATURE_RE`` (with lookahead) agrees with the shape regex plus a token scan.

    The Rust ``regex`` crate has no lookahead, so Rust uses the two-step form.
    """
    one_shot = fb.FEATURE_RE.match(name) is not None
    two_step = fb.FEATURE_RE_SHAPE.match(name) is not None and not any(
        t in name.split("_") for t in fb.BANNED_TOKENS
    )
    assert one_shot == two_step, f"{name!r}: one-regex says {one_shot}, two-step says {two_step}"


def test_name_table_covers_both_verdicts():
    """The name table contains both legal and illegal names."""
    verdicts = {fb.FEATURE_RE.match(n) is not None for n in _NAME_TABLE}
    assert verdicts == {True, False}, "the name table exercises only one verdict"


# --- rule 6: canonical parameter hash ---
def test_numeric_forms_of_one_value_collapse_to_one_param_set_id():
    """60, 60.0, "60" and Decimal("60.00") hash to one id, unlike a raw ``json.dumps``."""
    ids = {fb.param_set_id({"window": v}) for v in (60, 60.0, "60", Decimal("60.00"))}
    assert len(ids) == 1, f"numeric forms did not collapse: {sorted(ids)}"


def test_the_naive_hash_really_does_disagree():
    """Raw ``json.dumps`` does separate 60 and 60.0, so the collapse test is not vacuous."""
    import json

    assert json.dumps({"window": 60}) != json.dumps({"window": 60.0})


def test_param_set_id_shape_and_stability():
    psid = fb.param_set_id({"a": 1, "b": "x"})
    assert re.fullmatch(r"p[0-9a-f]{12}", psid), psid
    assert psid == fb.param_set_id({"b": "x", "a": 1}), "key order must not matter"


def test_list_order_is_meaningful_but_key_order_is_not():
    assert fb.param_set_id({"lv": [1, 2]}) != fb.param_set_id({"lv": [2, 1]})
    assert fb.param_set_id({"x": 1, "y": 2}) == fb.param_set_id({"y": 2, "x": 1})


def test_bool_is_not_an_integer_in_the_hash():
    """``bool`` subclasses ``int``; a wrong branch order makes True and 1 collide."""
    assert fb.canon_param_value(True) == "true"
    assert fb.canon_param_value(1) == "1"
    assert fb.param_set_id({"f": True}) != fb.param_set_id({"f": 1})


def test_none_and_unsupported_types_are_refused():
    with pytest.raises(fb.FeatureError, match="None is not allowed"):
        fb.param_set_id({"x": None})
    with pytest.raises(fb.FeatureError, match="type not allowed"):
        fb.param_set_id({"x": {1, 2}})


def test_no_exponent_notation_in_canonical_form():
    """``format(d, 'f')`` and not ``str(d)``: 1e-9 must not hash as '1E-9'."""
    assert "e" not in fb.canon_params({"tol": 1e-9}).lower()


def test_required_param_keys_covers_every_family():
    for family in fb.FAMILIES:
        keys = fb.required_param_keys(family)
        assert set(fb.COMMON_PARAM_KEYS) <= set(keys)
        assert set(fb.FAMILY_PARAM_KEYS[family]) <= set(keys)
    with pytest.raises(fb.FeatureError):
        fb.required_param_keys("flow")


# --- file-level rules ---
def test_file_name_must_match_the_feature_name(tmp_path):
    path = tmp_path / "cvd.yaml"
    path.write_text(yaml.safe_dump(_baseline(), sort_keys=False), encoding="utf-8")
    with pytest.raises(fb.FeatureError, match="must be tape_cvd_lot_v1.yaml"):
        fb.load_feature(path)


def test_empty_and_missing_files_are_refused(tmp_path):
    with pytest.raises(fb.FeatureError, match="not found"):
        fb.load_feature(tmp_path / "nope.yaml")
    (tmp_path / "tape_cvd_lot_v1.yaml").write_text("", encoding="utf-8")
    with pytest.raises(fb.FeatureError, match="empty"):
        fb.load_feature(tmp_path / "tape_cvd_lot_v1.yaml")


def test_spec_sha_ignores_prose_but_not_parameters():
    base = _baseline()
    sha = fb.spec_sha(base)
    prose = copy.deepcopy(base)
    prose["note"] = "edited"
    prose["status"] = "PARITY"
    prose["next_action"] = "changed"
    assert fb.spec_sha(prose) == sha, "prose edits must not break a freeze"
    changed = copy.deepcopy(base)
    changed["params"]["default"]["warmup_bars"] = 5
    assert fb.spec_sha(changed) != sha, "a parameter edit MUST change spec_sha"


# --- shipped registry ---


def test_every_shipped_file_is_named_after_its_feature():
    for path in sorted(FEATURES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["feature"] == path.stem
        assert fb.check_feature_name(data["feature"]) == []


#: Pinned default ids, so a parameter edit cannot pass unnoticed. Rust must reproduce
#: them from the same canonical string.
GOLDEN_PARAM_SET_IDS = {
    "tape_cvd_lot_v1": "p2ea1bc554894",
    "prof_tpo_poc_pts_v1": "pdfcc18ee0465",
}


@pytest.mark.parametrize("name,expected", sorted(GOLDEN_PARAM_SET_IDS.items()))
def test_default_param_set_ids_are_pinned(name, expected):
    feat = fb.load_feature(FEATURES_DIR / f"{name}.yaml")
    assert feat.default_param_set_id() == expected, (
        "the default parameter set changed. That is allowed, but it is a NEW "
        "param_set_id and therefore a new data set: update this pin deliberately."
    )


def test_shipped_features_declare_a_writer_and_a_definition_owner():
    for feat in fb.load_all_features(FEATURES_DIR):
        assert feat.impl.authority in ("rust", "python")
        assert feat.parity.tolerance == 0, "the first two features are bitwise by decision"
        assert feat.signal.causal is True and feat.signal.lookahead_ms == 0
        assert feat.cadence in fb.CADENCES


# --- DDL (migration 009), checked as text ---
def _ddl_statements() -> str:
    """The DDL without comment lines, so text in a comment can neither satisfy nor break a rule."""
    sql = DDL_PATH.read_text(encoding="utf-8")
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_ddl_exists_and_is_additive_only():
    assert DDL_PATH.exists(), f"{DDL_PATH} is missing"
    body = _ddl_statements().lower()
    for table in (
        "default.l2_observation",
        "default.l2_profile",
        "default.l2_param_set",
        "default.l2_feature_registry",
        "default.l2_input_manifest",
        "default.l2_backfill_ledger",
    ):
        assert f"create table if not exists {table}" in body, f"{table} not created idempotently"
    assert "create view if not exists default.l2_observation_causal" in body
    # statements must be additive so the migration is safe to re-run
    for verb in ("alter table", "drop table", "rename table", "truncate", "insert into"):
        assert verb not in body, f"non-additive statement {verb!r} in 009 outside a comment"


def test_ddl_carries_the_four_causality_constraints():
    sql = DDL_PATH.read_text(encoding="utf-8")
    for constraint in (
        "CONSTRAINT c_causal     CHECK window_start <= event_time",
        "CONSTRAINT c_not_future CHECK computed_at  >= event_time",
        "CONSTRAINT c_lookahead  CHECK lookahead_ms >= 0",
        "CONSTRAINT c_finite     CHECK isFinite(value)",
    ):
        assert constraint in sql, f"missing {constraint}"


def test_ddl_sorting_key_carries_param_set_and_code_version():
    """``param_set_id`` in the sort key allows pruning; ``code_version`` keeps builds apart."""
    sql = _ddl_statements()
    assert "ORDER BY (feature, symbol, param_set_id, code_version, event_time)" in sql
    assert "ENGINE = ReplacingMergeTree(computed_at)" in sql
    assert "PARTITION BY toYYYYMM(event_time)" in sql
    assert "DateTime64(3," not in sql, (
        "event_time is naive, as in L0; a timezone here would "
        "create a third convention on top of the two that already disagree"
    )


def test_ddl_follows_the_007_008_house_style():
    sql = DDL_PATH.read_text(encoding="utf-8")
    for section in ("WHERE TO RUN IT", "ADDITIVE + IDEMPOTENT", "Verify", "Rollback"):
        assert section in sql, f"009 is missing the {section!r} section 007/008 both carry"


# --- generated status page ---
def test_status_page_is_deterministic_and_marked_generated():
    features = fb.load_all_features(FEATURES_DIR)
    page = sp.render(features, "2026-09-05T00:00:00Z")
    assert page == sp.render(features, "2026-09-05T00:00:00Z")
    assert "GENERATED FILE - do not edit by hand." in page
    assert "python historical/research_lab/l2_status_page.py" in page
    for feat in features:
        assert feat.feature in page
        assert feat.default_param_set_id() in page


def test_status_page_prints_unreachable_rather_than_dropping_a_column():
    """Without cluster facts the page prints the unreachable marker instead of dropping columns."""
    features = fb.load_all_features(FEATURES_DIR)
    page = sp.render(features, "2026-09-05T00:00:00Z", facts=None)
    assert page.count(sp.UNREACHABLE) >= len(features)


def test_generated_page_on_disk_matches_the_registry():
    """``docs/L2_FEATURES.md`` matches a fresh rendering of the registry, stamp aside."""
    assert PAGE_PATH.exists(), f"{PAGE_PATH} has not been generated"
    on_disk = PAGE_PATH.read_text(encoding="utf-8")
    stamp = re.search(r"^Generated at: (.+)$", on_disk, re.MULTILINE)
    assert stamp, "the page carries no generation stamp"
    fresh = sp.render(fb.load_all_features(FEATURES_DIR), stamp.group(1))
    assert on_disk == fresh, (
        "docs/L2_FEATURES.md is stale or was hand-edited; regenerate with "
        "python historical/research_lab/l2_status_page.py"
    )


def test_consistency_problems_are_not_vacuous():
    """Feed the reconciler known-bad cluster facts and assert it names each one."""
    features = fb.load_all_features(FEATURES_DIR)
    assert sp.consistency_problems(features, None) == [], "the shipped registry is self-consistent"
    facts = {
        "tape_cvd_lot_v1": {
            "sessions": 10,
            "first": "2026-08-01",
            "last": "2026-08-31",
            "code_versions": ["py-aaaaaaa", "py-bbbbbbb"],
            "param_sets": fb.MAX_PARAM_SETS + 1,
            "non_causal": 7,
        },
        "cvd": {
            "sessions": 1,
            "first": "x",
            "last": "y",
            "code_versions": ["py-aaaaaaa"],
            "param_sets": 1,
            "non_causal": 0,
        },
    }
    found = " | ".join(msg for _name, msg in sp.consistency_problems(features, facts))
    assert "NO registry entry" in found, "an unregistered feature name went unreported"
    assert "lookahead_ms != 0" in found, "causal:true with non-causal rows went unreported"
    assert "ceiling" in found, "param_set_id explosion went unreported"
    assert "MultipleCodeVersions" in found, "two coexisting builds went unreported"


def test_the_registry_is_the_authority_not_the_cluster():
    """``cluster_facts`` degrades to ``None`` rather than raising when no cluster is reachable."""
    assert sp.cluster_facts(host="203.0.113.1") is None or isinstance(
        sp.cluster_facts(host="203.0.113.1"), dict
    )


def test_rust_formula_ids_parse_or_degrade():
    ids = fb.rust_formula_ids()
    assert isinstance(ids, frozenset)
    if ids:
        assert "cvd_divergence_v1" in ids and "poc_reaction_v1" in ids
        assert all(re.fullmatch(r"[a-z][a-z0-9_]*_v[0-9]+", i) for i in ids)
