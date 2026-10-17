"""Retirement test 4c (docs/backlog/memory-native-authoring.md): conventions.

The convention prose in ``docs/conventions/`` is imported into the graph as
rule findings; this test pins that every convention file is in the manifest,
every enforcer it names exists, and that the rules with NO inbound ``tests``
edge are exactly the allowlisted ones (equality, so the list can only shrink).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from precis.cli.conventions import default_manifest, import_conventions
from precis.store import Store

ROOT = Path(__file__).resolve().parent.parent

#: Rule ids with no enforcing test/lint, each with the reason it stays prose.
#: Exact-equality checked: fixing a rule means deleting its entry here.
_ALLOWLIST: dict[str, str] = {
    "code-anchors/cite-by-symbol": "no test, hook or lint checks it (audit 2026-10-16)",
    "code-anchors/citation-surfaces-only": "no test, hook or lint checks it (audit 2026-10-16)",
    "code-anchors/ast-resolution-not-index": "no test, hook or lint checks it (audit 2026-10-16)",
    "container-ops/wide-docker-address-pool": "no test, hook or lint checks it (audit 2026-10-16)",
    "container-ops/deploy-is-opt-in": "no test, hook or lint checks it (audit 2026-10-16)",
    "corpus-normalization/dry-run-before-ship": "no test, hook or lint checks it (audit 2026-10-16)",
    "corpus-normalization/co-firing-not-redundant": "no test, hook or lint checks it (audit 2026-10-16)",
    "corpus-normalization/probe-interface-on-disagreement": "no test, hook or lint checks it (audit 2026-10-16)",
    "dark-switches/dark-flag-needs-owner": "no test, hook or lint checks it (audit 2026-10-16)",
    "dark-switches/rearm-only-on-ruling": "no test, hook or lint checks it (audit 2026-10-16)",
    "discovery-layer-policy/render-from-store": "no test, hook or lint checks it (audit 2026-10-16)",
    "discovery-layer-policy/keywords-version-bump": "no test, hook or lint checks it (audit 2026-10-16)",
    "env-vars/pick-one-of-three-tiers": "no test, hook or lint checks it (audit 2026-10-16)",
    "env-vars/tier2-one-reader": "no test, hook or lint checks it (audit 2026-10-16)",
    "env-vars/tier3-raw-at-call-site": "no test, hook or lint checks it (audit 2026-10-16)",
    "invariants/safe-get-for-agent-urls": "no test, hook or lint checks it (audit 2026-10-16)",
    "kind-enablement/declare-requires-env": "no test, hook or lint checks it (audit 2026-10-16)",
    "kind-enablement/requires-secret-for-credentials": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/terse-no-warmup": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/dont-justify-choices": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/name-internals-in-dev-docs": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/point-dont-copy": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/present-tense-invariants": "no test, hook or lint checks it (audit 2026-10-16)",
    "llm-facing-prose/update-doc-same-commit": "no test, hook or lint checks it (audit 2026-10-16)",
    "poster-typography/citations-do-not-wrap": "no test, hook or lint checks it (audit 2026-10-16)",
    "poster-typography/band-sizing": "no test, hook or lint checks it (audit 2026-10-16)",
    "poster-typography/math-and-units": "no test, hook or lint checks it (audit 2026-10-16)",
    "poster-typography/layout-and-face": "no test, hook or lint checks it (audit 2026-10-16)",
    "reviewed-non-problems/dont-relitigate-adjudicated": "no test, hook or lint checks it (audit 2026-10-16)",
    "reviewed-non-problems/no-release-branch": "no test, hook or lint checks it (audit 2026-10-16)",
    "reviewed-non-problems/accepted-risks": "no test, hook or lint checks it (audit 2026-10-16)",
    "rtk/digest-not-evidence-of-absence": "no test, hook or lint checks it (audit 2026-10-16)",
    "rtk/no-blanket-stderr-suppression": "no test, hook or lint checks it (audit 2026-10-16)",
    "rtk/prefix-manually-outside-dev-mac": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/gripe-sha-on-backlog-delete": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/take-agent-by-fast-forward": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/release-owner-doesnt-wait": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/qland-lint-and-drift-guard": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/read-gate-failure-above-x": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/mark-round-from-your-tree": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/no-deploy-window": "no test, hook or lint checks it (audit 2026-10-16)",
    "shipping/peer-ping-cadence": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-evals/smallest-criterion-live-check": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-authoring-style/contract-not-implementation": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-authoring-style/cut-internal-names": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-authoring-style/drop-decorative-frontmatter": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-authoring-style/goal-voice-h2": "no test, hook or lint checks it (audit 2026-10-16)",
    "skill-authoring-style/examples-and-skeleton": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/long-runs-bg-await": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/narrow-lanes-dont-substitute": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/raw-sql-needs-real-pg-test": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/rehome-db-tests-not-delete": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/note-what-caught-a-bug": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/measured-budgets": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/no-float-equality": "no test, hook or lint checks it (audit 2026-10-16)",
    "testing/main-sha-not-guaranteed-gated": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/schema-stop-and-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/api-stop-and-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/performance-stop-and-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/cross-package-stop-and-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/ingest-stop-and-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/destructive-ops-ask": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/prod-writes-default-go": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/look-locally-first": "no test, hook or lint checks it (audit 2026-10-16)",
    "thresholds/when-a-threshold-trips": "no test, hook or lint checks it (audit 2026-10-16)",
    "time/coerce-at-boundary": "no test, hook or lint checks it (audit 2026-10-16)",
}


def _manifest() -> list[dict]:
    data = yaml.safe_load(default_manifest().read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def test_every_convention_file_is_in_the_manifest() -> None:
    covered = {e["file"] for e in _manifest()}
    missing = sorted(
        f"docs/conventions/{p.name}"
        for p in (ROOT / "docs" / "conventions").glob("*.md")
        if f"docs/conventions/{p.name}" not in covered
    )
    assert not missing, f"convention files absent from rules.yaml: {missing}"


def test_every_enforcer_exists() -> None:
    lint = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    selected = lint["tool"]["ruff"]["lint"].get("select", []) + lint["tool"]["ruff"][
        "lint"
    ].get("extend-select", [])
    gone: list[str] = []
    for entry in _manifest():
        for enf in entry.get("enforcers") or []:
            path = enf["path"]
            if path.startswith("ruff:"):
                code = path.removeprefix("ruff:")
                if not any(code.startswith(s) for s in selected):
                    gone.append(f"{entry['id']}: {path} not in ruff select")
            elif not (ROOT / path).exists():
                gone.append(f"{entry['id']}: {path}")
    assert not gone, gone


def test_unenforced_rules_equal_allowlist(store: Store) -> None:
    import_conventions(store, default_manifest())
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT r.meta->>'convention_rule' FROM refs r "
            "WHERE r.kind='finding' AND r.retired_at IS NULL "
            "AND r.meta->>'source'='conventions-import' "
            "AND r.meta ? 'convention_rule' AND NOT EXISTS ("
            " SELECT 1 FROM links l WHERE l.dst_ref_id=r.ref_id "
            " AND l.relation='tests')"
        ).fetchall()
    unenforced = {str(r[0]) for r in rows}
    assert unenforced == set(_ALLOWLIST), (
        f"unenforced but not allowlisted: {sorted(unenforced - set(_ALLOWLIST))}; "
        f"allowlisted but now enforced: {sorted(set(_ALLOWLIST) - unenforced)}"
    )
