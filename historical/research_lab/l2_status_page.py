#!/usr/bin/env python
"""Generate ``docs/L2_FEATURES.md`` from the feature registry in ``specs/features/*.yaml``.

Output is deterministic: on unchanged inputs only the ``Generated at`` line differs. Without
``--clickhouse`` the three coverage columns read ``unreachable``; with it the cluster probe runs
read-only (``readonly=1``).

    python historical/research_lab/l2_status_page.py               # write docs/L2_FEATURES.md
    python historical/research_lab/l2_status_page.py --stdout      # print, write nothing
    python historical/research_lab/l2_status_page.py --clickhouse  # fill the coverage columns
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from historical.research_lab.feature_boundary import (  # noqa: E402
    FEATURES_DIR,
    MAX_PARAM_SETS,
    READABLE_STATUSES,
    WRITEABLE_STATUSES,
    Feature,
    load_all_features,
    repo_root,
)

OUT_PATH = repo_root() / "docs" / "L2_FEATURES.md"
MIGRATION = repo_root() / "data_provider" / "orderflow" / "sql" / "migrations"
DDL_PATH = MIGRATION / "009_l2_observations.sql"

#: Rendered wherever a cluster fact is wanted and no probe was run.
UNREACHABLE = "unreachable"


# --- optional cluster probe (read-only) ---
def cluster_facts(host: str | None = None) -> dict[str, dict] | None:
    """Return per-feature coverage from ``default.l2_observation``, or ``None`` on any failure.

    Failures (driver missing, cluster down, table absent) render as :data:`UNREACHABLE` cells.
    """
    try:  # pragma: no cover - needs the cluster on the LAN
        import os

        import clickhouse_connect  # type: ignore
    except Exception:
        return None
    try:  # pragma: no cover
        client = clickhouse_connect.get_client(
            host=host or os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("CLICKHOUSE_DATABASE", "default"),
            settings={"readonly": 1},
        )
        rows = client.query(
            "SELECT feature, uniqExact(toDate(event_time)) AS sessions, "
            "min(event_time) AS first_et, max(event_time) AS last_et, "
            "groupUniqArray(code_version) AS code_versions, "
            "uniqExact(param_set_id) AS param_sets, "
            "countIf(lookahead_ms != 0) AS non_causal "
            "FROM default.l2_observation GROUP BY feature ORDER BY feature"
        ).result_rows
    except Exception:
        return None
    out: dict[str, dict] = {}
    for feature, sessions, first_et, last_et, versions, psets, non_causal in rows:
        out[str(feature)] = {
            "sessions": int(sessions),
            "first": str(first_et),
            "last": str(last_et),
            "code_versions": sorted(str(v) for v in versions),
            "param_sets": int(psets),
            "non_causal": int(non_causal),
        }
    return out


# --- rendering helpers ---
def _cell(text: object) -> str:
    """Format one table cell: single line, pipes escaped, a dash when empty."""
    s = " ".join(str(text).split())
    return s.replace("|", "\\|") if s else "-"


def _clip(text: str, width: int = 90) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= width else s[: width - 1].rstrip() + "..."


def _params_cell(feat: Feature) -> str:
    return ", ".join(f"`{name}`={feat.param_set_ids[name]}" for name in sorted(feat.param_set_ids))


def _parity_cell(feat: Feature) -> str:
    tol = "bitwise (0)" if feat.parity.tolerance == 0 else f"tol={feat.parity.tolerance:g}"
    if feat.parity.parity_waiver:
        return f"{tol}, WAIVED: {_clip(feat.parity.parity_waiver, 40)}"
    return f"{tol}, {feat.parity.test or 'no test declared'}"


# --- page ---
def render(features: list[Feature], generated_at: str, facts: dict[str, dict] | None = None) -> str:
    """Render the page; ``generated_at`` is a parameter so tests can pin it."""
    out: list[str] = []
    w = out.append

    w("# L2 OBSERVATIONS - feature registry status")
    w("")
    w("<!-- GENERATED FILE - do not edit by hand.")
    w("     Regenerate: python historical/research_lab/l2_status_page.py")
    w("     Sources: specs/features/*.yaml (authority), default.l2_observation (coverage,")
    w("     read-only, only with --clickhouse) -->")
    w("")
    w(f"Generated at: {generated_at}")
    w("")
    w(
        "Authority for everything below is `specs/features/*.yaml` in git, loaded through "
        "`historical/research_lab/feature_boundary.py`. The ClickHouse tables are a "
        "PROJECTION of it, never the other way round: a parameter set is a human decision "
        "and belongs where review and blame live."
    )
    w("")

    # --- layer header ---
    ddl_state = "written, NOT applied" if DDL_PATH.exists() else "MISSING"
    by_status: dict[str, int] = {}
    for feat in features:
        by_status[feat.status] = by_status.get(feat.status, 0) + 1
    w("## Layer")
    w("")
    w("| | |")
    w("|---|---|")
    w(f"| Registered features | {len(features)} in `specs/features/` |")
    w("| By status | " + _cell(", ".join(f"{k}={by_status[k]}" for k in sorted(by_status))) + " |")
    w(f"| DDL | `{DDL_PATH.relative_to(repo_root()).as_posix()}` - {ddl_state} |")
    w(f"| Writeable statuses | {', '.join(sorted(WRITEABLE_STATUSES))} |")
    w(f"| Returned by `load_observation` by default | {', '.join(sorted(READABLE_STATUSES))} |")
    w("| Forbidden cadence | `per_update` (~1 GiB/year storage ceiling) |")
    w(f"| Param sets per feature | at most {MAX_PARAM_SETS} |")
    w("| Default parity tolerance | 0 (bitwise); non-zero requires `tolerance_reason` |")
    w(f"| Cluster coverage probe | {'live read' if facts is not None else UNREACHABLE} |")
    w("")

    # --- feature table ---
    w("## Features")
    w("")
    w(
        "| feature | status | cadence | unit | param sets | coverage (sessions) | "
        "first/last event_time | code_version present | parity | supersedes |"
    )
    w("|---|---|---|---|---|---|---|---|---|---|")
    for feat in features:
        fact = (facts or {}).get(feat.feature)
        if facts is None:
            coverage = span = versions = UNREACHABLE
        elif fact is None:
            coverage = span = versions = "0 rows"
        else:
            coverage = f"{fact['sessions']}"
            span = f"{fact['first']} .. {fact['last']}"
            versions = ", ".join(fact["code_versions"]) or "-"
        w(
            "| "
            + " | ".join(
                [
                    f"`{feat.feature}`",
                    _cell(feat.status),
                    _cell(feat.cadence),
                    _cell(feat.unit),
                    _cell(_params_cell(feat)),
                    _cell(coverage),
                    _cell(span),
                    _cell(versions),
                    _cell(_parity_cell(feat)),
                    _cell(feat.supersedes or ""),
                ]
            )
            + " |"
        )
    w("")

    # --- per-feature detail ---
    w("## Feature detail")
    w("")
    for feat in features:
        w(f"### {feat.feature} ({feat.status})")
        w("")
        w(f"- **Title:** {feat.title or '-'}")
        w(f"- **Family / unit / dtype:** {feat.family} / {feat.unit} / {feat.dtype}")
        w(
            f"- **Causality:** causal={feat.signal.causal} lookahead_ms="
            f"{feat.signal.lookahead_ms} - window_start={feat.signal.window_start_rule}, "
            f"event_time={feat.signal.event_time_rule}, is_final={feat.signal.is_final_rule}"
        )
        w(
            f"- **Authority:** {feat.impl.authority} - rust `{feat.impl.rust or '-'}` / "
            f"python `{feat.impl.python or '-'}`"
        )
        w(f"- **Parity:** {_parity_cell(feat)} (golden tape: `{feat.parity.golden_tape or '-'}`)")
        w(
            f"- **Derives trigger:** {feat.derives_trigger or '-'} - maps to validation "
            f"state: {feat.maps_to_validation_state or '-'}"
        )
        w(f"- **Retention:** {feat.retention} - pinned code_version: {feat.pinned_code_version}")
        w(f"- **spec_sha:** `{feat.spec_sha}`")
        w("")
        w("| param set | param_set_id | parameters |")
        w("|---|---|---|")
        for set_name in sorted(feat.param_sets):
            params = feat.param_sets[set_name]
            body = ", ".join(f"{k}={params[k]!r}" for k in sorted(params))
            w(f"| {_cell(set_name)} | `{feat.param_set_ids[set_name]}` | {_cell(body)} |")
        w("")
        if feat.inputs:
            w("| input layer | table | columns |")
            w("|---|---|---|")
            for src in feat.inputs:
                w(
                    f"| {_cell(src.get('layer'))} | {_cell(src.get('table'))} | "
                    f"{_cell(', '.join(src.get('columns') or []))} |"
                )
            w("")
        if feat.note:
            w(f"**Note.** {' '.join(feat.note.split())}")
            w("")
        if feat.next_action:
            w(f"**Next action.** {' '.join(feat.next_action.split())}")
            w("")

    # --- registry vs cluster consistency ---
    w("## Registry vs cluster consistency")
    w("")
    problems = consistency_problems(features, facts)
    if problems:
        w("| feature | finding |")
        w("|---|---|")
        for name, msg in problems:
            w(f"| {_cell(name)} | {_cell(msg)} |")
    else:
        w(
            "No disagreement found."
            if facts is not None
            else "No registry-only disagreement. Cluster checks were not run "
            "(no `--clickhouse`), so coverage findings are unknown, not absent."
        )
    w("")

    # --- registry load findings ---
    w("## Registry load findings (advisory)")
    w("")
    flagged = [f for f in features if f.problems]
    if flagged:
        w("| feature | problem |")
        w("|---|---|")
        for feat in flagged:
            for problem in feat.problems:
                w(f"| {_cell(feat.feature)} | {_cell(problem)} |")
    else:
        w("All registry entries load clean in strict mode.")
    w("")
    return "\n".join(out) + "\n"


def consistency_problems(
    features: list[Feature], facts: dict[str, dict] | None = None
) -> list[tuple[str, str]]:
    """Return sorted ``(feature, finding)`` pairs where the registry or the table disagree.

    Registry-only checks always run; cluster checks run only when ``facts`` is given.
    """
    out: list[tuple[str, str]] = []
    names = {f.feature for f in features}
    for feat in features:
        if feat.superseded_by and feat.superseded_by not in names:
            out.append(
                (feat.feature, f"superseded_by {feat.superseded_by!r} has no registry entry")
            )
        if feat.supersedes and feat.supersedes not in names:
            out.append((feat.feature, f"supersedes {feat.supersedes!r} has no registry entry"))
        if feat.status in WRITEABLE_STATUSES and feat.impl.rust and not feat.impl.python:
            out.append(
                (
                    feat.feature,
                    f"status {feat.status} with a Rust implementation and no Python one, "
                    f"while Python batch is the ONLY writer",
                )
            )
        for dep in feat.depends_on:
            if dep not in names:
                out.append((feat.feature, f"depends_on {dep!r} has no registry entry"))
    if facts is None:
        return sorted(out)
    for name in sorted(facts):
        fact = facts[name]
        if name not in names:
            out.append((name, "present in default.l2_observation but has NO registry entry"))
            continue
        feat = next(f for f in features if f.feature == name)
        if fact["non_causal"] and feat.signal.causal:
            out.append(
                (
                    name,
                    f"registry declares causal: true but {fact['non_causal']} rows carry "
                    f"lookahead_ms != 0",
                )
            )
        if fact["param_sets"] > MAX_PARAM_SETS:
            out.append(
                (
                    name,
                    f"{fact['param_sets']} distinct param_set_id in the table; the ceiling "
                    f"is {MAX_PARAM_SETS}",
                )
            )
        if len(fact["code_versions"]) > 1:
            out.append(
                (
                    name,
                    f"{len(fact['code_versions'])} code_version values coexist "
                    f"({', '.join(fact['code_versions'])}); load_observation will raise "
                    f"MultipleCodeVersions",
                )
            )
        unknown = set(fact.get("param_set_ids") or []) - set(feat.param_set_ids.values())
        if unknown:
            out.append((name, f"param_set_id(s) {sorted(unknown)} are not in the registry"))
    for feat in features:
        if feat.status == "LIVE" and feat.feature not in facts:
            out.append((feat.feature, "status LIVE but no rows in default.l2_observation"))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write docs/L2_FEATURES.md from specs/features/*.yaml")
    ap.add_argument("--features-dir", default=str(FEATURES_DIR))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument("--strict", action="store_true", help="fail on any registry warning")
    ap.add_argument(
        "--clickhouse",
        action="store_true",
        help="fill the coverage columns from default.l2_observation (READ-ONLY)",
    )
    ap.add_argument("--host", default=None, help="ClickHouse host override")
    args = ap.parse_args(argv)

    features = load_all_features(args.features_dir, strict=args.strict)
    facts = cluster_facts(args.host) if args.clickhouse else None
    generated_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    page = render(features, generated_at, facts)
    if args.stdout:
        sys.stdout.write(page)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page.splitlines())} lines, {len(features)} features)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
