"""Design-tree manifest schema, validation and export (slice S0, pure)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from precis import design_tree
from precis.design_tree import (
    DesignTree,
    DesignTreeError,
    export_rules,
    load_dir,
    load_files,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "design_tree"


@pytest.fixture(scope="module")
def tree() -> DesignTree:
    return load_dir(FIXTURE_DIR)


def _edge_set(tree: DesignTree) -> set[tuple[str, str, str]]:
    return {(e.src, e.dst, e.relation) for e in tree.edges}


def test_fixture_parses_roles_normalized(tree: DesignTree) -> None:
    assert tree.nodes["join"].role == "goal"  # 'function' alias
    assert tree.nodes["join/service"].role == "method"  # 'branch' alias
    roles = {n.role for n in tree.nodes.values()}
    assert roles == {
        "goal",
        "method",
        "leaf",
        "process",
        "system",
        "axis",
        "principle",
        "failure-mode",
        "criterion",
    }


def test_parents_multi_parent_dag(tree: DesignTree) -> None:
    tapped = tree.nodes["join/service/threaded/clearance-tapped"]
    assert list(tapped.parents) == ["join/service/threaded", "join/frequent"]
    pinned = tree.nodes["join/service/pinned"]
    # primary parent + the also_under second parent under join/frequent
    assert list(pinned.parents) == ["join/service", "join/frequent"]


def test_edges_cover_every_relation(tree: DesignTree) -> None:
    edges = _edge_set(tree)
    tapped = "join/service/threaded/clearance-tapped"
    assert (tapped, "join/service/threaded", "specialises") in edges
    assert (tapped, "join/frequent", "specialises") in edges
    assert ("join/service/pinned", "join/frequent", "specialises") in edges
    assert (f"{tapped}#i1", tapped, "qualifies") in edges
    assert (f"{tapped}#f1", f"{tapped}#i1", "fixes") in edges
    assert (f"{tapped}#c1", tapped, "part-of") in edges
    assert (
        f"{tapped}#c1",
        "src:https://example.invalid/machinery-handbook",
        "cites",
    ) in edges
    assert (tapped, "src:https://example.invalid/machinery-handbook", "cites") in edges
    assert ("enf:src/precis_se/fasten.py::grip", f"{tapped}#c1", "tests") in edges


def test_related_to_as_meta(tree: DesignTree) -> None:
    meta = {
        (e.src, e.dst): e.meta.get("as")
        for e in tree.edges
        if e.relation == "related-to"
    }
    tapped = "join/service/threaded/clearance-tapped"
    assert meta[(tapped, "axis/locking")] == "axis"
    assert meta[(tapped, "failure/galvanic-corrosion")] == "mode"
    assert meta[(tapped, "principle/retention-all-dof")] == "principle"
    assert meta[(tapped, "process/desktop-fdm")] == "process"
    assert meta[(tapped, "system/t-slot-20")] == "system"
    assert meta[(tapped, "join/service/pinned")] is None  # leaf -> leaf
    assert meta[("join/frequent", "close")] is None  # cross-file link


def test_items_cover_every_mint_role(tree: DesignTree) -> None:
    roles = {i.role for i in tree.items}
    assert roles >= {
        "goal",
        "method",
        "leaf",
        "process",
        "system",
        "axis",
        "principle",
        "failure-mode",
        "criterion",
        "rule",
        "issue",
        "fix",
        "source",
        "enforcer",
    }
    keys = {i.key for i in tree.items}
    assert len(keys) == len(tree.items)  # keys unique
    assert "join/service/threaded/clearance-tapped#c2" in keys
    assert "join/service/pinned#i1" in keys
    assert "src:perplexity-reasoning:000000" in keys


def test_export_shape(tree: DesignTree) -> None:
    out = export_rules(tree)
    assert out["version"] == 1
    assert out["leaves"] == [
        "join/service/pinned",
        "join/service/threaded/clearance-tapped",
    ]
    assert out["axes"] == ["axis/locking"]
    assert out["principles"] == ["principle/retention-all-dof"]
    tapped = out["nodes"]["join/service/threaded/clearance-tapped"]
    assert tapped["role"] == "leaf"
    assert tapped["implies"] == {"class": "rigid", "mechanism": "screw"}
    assert tapped["parents"] == ["join/service/threaded", "join/frequent"]
    rule_ids = [r["id"] for r in tapped["rules"]]
    assert rule_ids == [
        "join/service/threaded/clearance-tapped#c1",
        "join/service/threaded/clearance-tapped#c2",
    ]
    c1, c2 = tapped["rules"]
    assert c2["equipment"] == "desktop-fdm"
    assert c2["confidence"] == "secondary"
    assert c2["table_ref"] == "fdm/pla"
    assert "enforcers" not in c2  # link-side data, not export payload


def test_export_marks_conflicted_rules(tree: DesignTree) -> None:
    """A node ``conflicts`` record rides the export verbatim; the rules
    its ``rules:`` names are marked ``conflicted`` so DRC never enforces
    them — plus every rule in the forest sharing a marked rule's
    ``table_ref`` (pinned#c1 shares c2's fdm/pla row)."""
    out = export_rules(tree)
    tapped = out["nodes"]["join/service/threaded/clearance-tapped"]
    assert tapped["conflicts"] == [
        {
            "topic": "fdm hole compensation",
            "values": ["0.2 mm (Prusa KB)", "0.4 mm (Bambu wiki)"],
            "reading": "slicer profiles disagree on printed hole compensation",
            "rules": ["c2"],
        }
    ]
    c1, c2 = tapped["rules"]
    assert c2["conflicted"] is True  # named by the conflict's rules:
    assert "conflicted" not in c1
    # forest-wide propagation through the shared table_ref
    assert out["nodes"]["join/service/pinned"]["rules"][0]["conflicted"] is True
    # a non-conflicted node carries no conflicts key at all
    assert "conflicts" not in out["nodes"]["join/service/pinned"]


def test_export_rule_equipment_defaults(tree: DesignTree) -> None:
    """A process node's ``equipment`` is the export default for rules
    that don't declare their own (and rides the node entry)."""
    proc = export_rules(tree)["nodes"]["process/desktop-fdm"]
    assert proc["equipment"] == "desktop-fdm"
    assert proc["rules"][0]["equipment"] == "desktop-fdm"  # defaulted
    # explicit rule equipment wins over the node default
    tapped = export_rules(tree)["nodes"]["join/service/threaded/clearance-tapped"]
    assert "equipment" not in tapped  # no process ancestor
    assert tapped["rules"][0]["equipment"] == "any"


def _tmp_manifest(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_rejects_cycle(tmp_path: Path) -> None:
    path = _tmp_manifest(
        tmp_path,
        {
            "a": {"id": "a", "role": "goal", "also_under": ["b"]},
            "b": {"id": "b", "role": "method", "also_under": ["a"]},
        },
    )
    with pytest.raises(DesignTreeError, match="cycle"):
        load_files((path,))


def test_rejects_dangling_targets(tmp_path: Path) -> None:
    for key, val in (
        ("also_under", ["nope"]),
        ("links", ["nope"]),
    ):
        path = _tmp_manifest(tmp_path, {"a": {"id": "a", "role": "goal", key: val}})
        with pytest.raises(DesignTreeError, match="does not exist"):
            load_files((path,))


def test_rejects_retired_fields(tmp_path: Path) -> None:
    """``see``, ``ref`` and node-level ``enforcers`` were cut from the
    schema: they surface as unknown fields, not dangling-target errors."""
    cases: list[dict[str, Any]] = [
        {"a": {"id": "a", "role": "goal", "see": "b"}},
        {"a": {"id": "a", "role": "goal", "enforcers": []}},
        {
            "a": {
                "id": "a",
                "role": "goal",
                "children": {"stub": {"id": "a/stub", "ref": "a", "role": "leaf"}},
            }
        },
    ]
    for data in cases:
        with pytest.raises(DesignTreeError, match="unknown field"):
            load_files((_tmp_manifest(tmp_path, data),))


def test_rejects_bad_nodes(tmp_path: Path) -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"a": {"role": "goal"}}, "id"),
        ({"a": {"id": "a", "role": "bogus"}}, "role"),
        ({"a": {"id": "a", "role": "goal", "coverage": "vast"}}, "coverage"),
        ({"a": {"id": "a", "role": "goal", "surprise": 1}}, "unknown field"),
        ({"a": {"id": "a", "role": "goal", "asks": "separable"}}, "list"),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "children": {"x": {"id": "elsewhere/x", "role": "leaf"}},
                }
            },
            "stable path slug",
        ),
        (
            {
                "a": {"id": "a", "role": "goal"},
                "b": {"id": "a", "role": "leaf"},
            },
            "duplicate id",
        ),
        (
            {"a": {"id": "a", "role": "goal", "conflicts": "nope"}},
            "conflicts must be a list",
        ),
        (
            {"a": {"id": "a", "role": "goal", "conflicts": ["prose"]}},
            "must be a mapping",
        ),
        (
            {"a": {"id": "a", "role": "goal", "conflicts": [{"values": []}]}},
            "'topic'",
        ),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "conflicts": [{"topic": "t", "reading": "r"}],
                }
            },
            "values must be a list",
        ),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "conflicts": [{"topic": "t", "values": []}],
                }
            },
            "'reading'",
        ),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "rules": [{"id": "c1", "text": "t"}],
                    "conflicts": [{"topic": "t", "values": [], "reading": "r"}],
                }
            },
            "nonempty 'rules'",
        ),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "rules": [{"id": "c1", "text": "t"}],
                    "conflicts": [
                        {
                            "topic": "t",
                            "values": [],
                            "reading": "r",
                            "rules": ["nope"],
                        }
                    ],
                }
            },
            "name no rule",
        ),
        (
            {
                "a": {
                    "id": "a",
                    "role": "goal",
                    "conflicts": [
                        {
                            "topic": "t",
                            "values": [],
                            "reading": "r",
                            "rules": ["c1"],
                            "sources": ["x"],
                        }
                    ],
                }
            },
            "unknown field",
        ),
        (
            {"a": {"id": "a", "role": "goal", "equipment": "any"}},
            "only allowed on role=process",
        ),
        (
            {"a": {"id": "a", "role": "process", "equipment": "warp"}},
            "equipment must be one of",
        ),
        (
            {"a": {"id": "a", "role": "goal", "domain": "not a tag"}},
            "tag-safe",
        ),
        (
            {"a": {"id": "a", "role": "goal", "scale": "<bad>"}},
            "tag-safe",
        ),
        (
            {"a": {"id": "a", "role": "goal", "sources": [""]}},
            "nonempty string",
        ),
        (
            {"a": {"id": "a", "role": "goal", "issues": [{"issue": ""}]}},
            "nonempty string 'issue'",
        ),
    ]
    for data, match in cases:
        with pytest.raises(DesignTreeError, match=match):
            load_files((_tmp_manifest(tmp_path, data),))


def test_rejects_bad_rules(tmp_path: Path) -> None:
    base = {"a": {"id": "a", "role": "leaf"}}
    cases: list[tuple[dict[str, Any], str]] = [
        ({"rules": [{"text": "t"}]}, "'id'"),
        ({"rules": [{"id": "c1"}]}, "'text'"),
        (
            {"rules": [{"id": "c1", "text": "t"}, {"id": "c1", "text": "u"}]},
            "duplicate rule id",
        ),
        (
            {"rules": [{"id": "c1", "text": "t", "equipment": "warp"}]},
            "equipment",
        ),
        (
            {"rules": [{"id": "c1", "text": "t", "confidence": "vibes"}]},
            "confidence",
        ),
        (
            {"rules": [{"id": "c1", "text": "t", "source_quality": "meh"}]},
            "source_quality",
        ),
        # i<n>/f<n> collide with the minted issue/fix item keys
        ({"rules": [{"id": "i1", "text": "t"}]}, "reserved"),
        ({"rules": [{"id": "f2", "text": "t"}]}, "reserved"),
        (
            {"rules": [{"id": "c1", "text": "t", "table_ref": "fdm/unobtainium"}]},
            "no se_capabilities row",
        ),
        (
            {"rules": [{"id": "c1", "text": "t", "table_ref": 3}]},
            "table_ref must be a string",
        ),
    ]
    for extra, match in cases:
        data = {"a": {**base["a"], **extra}}
        with pytest.raises(DesignTreeError, match=match):
            load_files((_tmp_manifest(tmp_path, data),))


def test_conflicts_mark_named_rules_forest_wide(tmp_path: Path) -> None:
    """Marking is explicit, not value-matched: a conflict's ``rules:``
    names local rule ids; a rule on *another* node is marked only via a
    shared table_ref, never by carrying a disputed value."""
    path = _tmp_manifest(
        tmp_path,
        {
            "a": {
                "id": "a",
                "role": "leaf",
                "conflicts": [
                    {
                        "topic": "bridge limit",
                        "values": ["5 mm (src X)", "10 mm (src Y)"],
                        "reading": "pick by printer",
                        "rules": ["c1"],
                    }
                ],
                "rules": [
                    {"id": "c1", "text": "t", "value": "5", "table_ref": "fdm/pla"},
                    {"id": "c2", "text": "u", "value": "5"},
                ],
            },
            "b": {
                "id": "b",
                "role": "leaf",
                "rules": [
                    {"id": "c1", "text": "v", "table_ref": "fdm/pla"},
                    {"id": "c2", "text": "w", "value": "5"},
                ],
            },
        },
    )
    nodes = export_rules(load_files((path,)))["nodes"]
    assert nodes["a"]["rules"][0]["conflicted"] is True  # named by rules:
    assert "conflicted" not in nodes["a"]["rules"][1]  # same value, unnamed
    assert nodes["b"]["rules"][0]["conflicted"] is True  # shared table_ref
    assert "conflicted" not in nodes["b"]["rules"][1]  # same value, no ref


def test_rejects_inherited_equipment_disagreement(tmp_path: Path) -> None:
    """Two parents resolving to different equipment refuse the node and
    ask for an explicit declaration."""
    path = _tmp_manifest(
        tmp_path,
        {
            "p1": {"id": "p1", "role": "process", "equipment": "desktop-fdm"},
            "p2": {"id": "p2", "role": "process", "equipment": "sla"},
            "n": {"id": "n", "role": "leaf", "also_under": ["p1", "p2"]},
        },
    )
    with pytest.raises(DesignTreeError, match="disagree on equipment"):
        load_files((path,))


def test_related_to_cycle_is_legal(tmp_path: Path) -> None:
    path = _tmp_manifest(
        tmp_path,
        {
            "a": {"id": "a", "role": "leaf", "links": ["b"]},
            "b": {"id": "b", "role": "leaf", "links": ["a"]},
        },
    )
    tree = load_files((path,))
    assert _edge_set(tree) == {
        ("a", "b", "related-to"),
        ("b", "a", "related-to"),
    }


def test_load_dir_empty(tmp_path: Path) -> None:
    with pytest.raises(DesignTreeError, match="no \\*.yaml"):
        load_dir(tmp_path)


def test_real_manifests_validate() -> None:
    """The draft manifests destined for S1 already satisfy the schema
    (they use the function/branch pilot names and one ref stub)."""
    drafts = sorted(Path("/tmp/design-tree").glob("*.yaml"))
    if not drafts:
        pytest.skip("/tmp/design-tree drafts not present")
    tree = load_files(tuple(drafts))
    assert len(tree.nodes) > 50


def test_leaf_ids_from_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export = export_rules(load_dir(FIXTURE_DIR))
    monkeypatch.setattr(design_tree, "load_export", lambda: export)
    assert design_tree.leaf_ids() == frozenset(export["leaves"])
