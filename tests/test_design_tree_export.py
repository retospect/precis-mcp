"""``design_rules.json`` export: stays in sync with the packaged manifests
(empty until S1 lands them), and ``joints.params.leaf`` validates against
it (not the live graph) — exercised against the fixture export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis import design_tree
from precis_se import joints

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "design_tree"
EXPORT = Path(design_tree.packaged_data_dir()) / "design_rules.json"


def _manifest_dir() -> Path | None:
    """The export's source of truth: the packaged manifests once S1 lands
    them; ``None`` until then (the shipped export is the empty graph)."""
    packaged = Path(design_tree.packaged_data_dir()) / "design-tree"
    if packaged.is_dir() and list(packaged.glob("*.yaml")):
        return packaged
    return None


def _empty_export_json() -> str:
    return design_tree.export_json(design_tree.DesignTree({}, [], []))


@pytest.fixture()
def fixture_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The leaf vocabulary ``joints`` sees — the fixture forest's export,
    standing in for the packaged file (empty until S1)."""
    export = design_tree.export_rules(design_tree.load_dir(FIXTURE_DIR))
    monkeypatch.setattr(design_tree, "load_export", lambda: export)
    return export


def test_design_rules_json_in_sync() -> None:
    manifest_dir = _manifest_dir()
    expected = (
        design_tree.export_json(design_tree.load_dir(manifest_dir))
        if manifest_dir is not None
        else _empty_export_json()
    )
    shipped = EXPORT.read_text(encoding="utf-8")
    assert shipped == expected, (
        f"design_rules.json drifted from {manifest_dir or 'the empty graph'} — "
        "regenerate: uv run python -m precis.design_tree "
        "src/precis/data/design-tree > src/precis/data/design_rules.json"
    )


def test_export_is_valid_leaf_source(fixture_export: dict[str, Any]) -> None:
    assert set(design_tree.leaf_ids()) == set(fixture_export["leaves"])
    for leaf in fixture_export["leaves"]:
        assert fixture_export["nodes"][leaf]["role"] == "leaf"


def test_joint_leaf_param_accepted(fixture_export: dict[str, Any]) -> None:
    joint = joints.validate_joint(
        {
            "class": "rigid",
            "mechanism": "screw",
            "params": {"leaf": "join/service/threaded/clearance-tapped"},
        }
    )
    assert joint["params"]["leaf"] == "join/service/threaded/clearance-tapped"


def test_joint_leaf_param_unknown_refused(fixture_export: dict[str, Any]) -> None:
    with pytest.raises(joints.JointError, match="leaf") as exc:
        joints.validate_joint({"class": "rigid", "params": {"leaf": "join/bogus"}})
    msg = str(exc.value)
    assert "options:" in msg and "next:" in msg
    assert "join/service/pinned" in msg  # known leaves listed
    # the lookup hint names the meta.dtree.id search, not a bare get
    assert "search(kind='finding'" in msg


def test_joint_leaf_param_options_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The options list never dumps the whole vocabulary — 10 nearest."""
    leaves = [f"join/l{i:02d}" for i in range(20)]
    monkeypatch.setattr(
        design_tree,
        "load_export",
        lambda: {
            "version": 1,
            "nodes": {leaf: {"role": "leaf"} for leaf in leaves},
            "leaves": leaves,
            "axes": [],
            "principles": [],
        },
    )
    with pytest.raises(joints.JointError, match="leaf") as exc:
        joints.validate_joint({"class": "rigid", "params": {"leaf": "join/l19x"}})
    msg = str(exc.value)
    options = msg.split("options:", 1)[1].split(";", 1)[0]
    assert options.count("join/l") <= 10
    assert "join/l19" in options  # the nearest match is listed


def test_joint_leaf_param_non_string_refused(
    fixture_export: dict[str, Any],
) -> None:
    with pytest.raises(joints.JointError, match="leaf"):
        joints.validate_joint({"class": "rigid", "params": {"leaf": 3}})


def test_joint_leaf_param_empty_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty export refuses every leaf with the honest 'no leaves'
    options line — the shipped state until S1 lands manifests."""
    monkeypatch.setattr(
        design_tree,
        "load_export",
        lambda: {"version": 1, "nodes": {}, "leaves": [], "axes": [], "principles": []},
    )
    with pytest.raises(joints.JointError, match="leaf") as exc:
        joints.validate_joint({"class": "rigid", "params": {"leaf": "x"}})
    assert "(none" in str(exc.value)
