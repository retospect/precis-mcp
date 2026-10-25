"""``design_rules.json`` export: stays in sync with the packaged manifests,
and ``joints.params.leaf`` validates against it (not the live graph) —
exercised against the fixture export and the packaged one."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from precis import design_tree
from precis_se import joints

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "design_tree"
EXPORT = Path(design_tree.packaged_data_dir()) / "design_rules.json"


def _manifest_dir() -> Path | None:
    """The export's source of truth: the packaged manifests; ``None``
    when absent (the shipped export is then the empty graph)."""
    packaged = Path(design_tree.packaged_data_dir()) / "design-tree"
    if packaged.is_dir() and list(packaged.glob("*.yaml")):
        return packaged
    return None


def _empty_export_json() -> str:
    return design_tree.export_json(design_tree.DesignTree({}, [], []))


@pytest.fixture()
def fixture_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The leaf vocabulary ``joints`` sees — the fixture forest's export,
    standing in for the packaged file."""
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


def test_iso_2768_1_table_shape() -> None:
    """The shared ISO 2768-1 table the manifests cite via
    ``standard: ISO 2768-1``: every range row carries the four tolerance
    classes, ranges tile, and a few transcription-sensitive values are
    pinned (the file replaced nine per-node copies, four of them wrong)."""
    data = json.loads(
        (Path(design_tree.packaged_data_dir()) / "iso_2768_1.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["standard"] == "ISO 2768-1:1989"
    classes = set(data["classes"])
    assert classes == {"f", "m", "c", "v"}
    for table in ("linear_mm", "radii_chamfer_mm", "angular_arcmin"):
        ranges = data[table]["ranges"]
        for row in ranges:
            assert {"min", "max"} | classes <= set(row)
        for prev, nxt in itertools.pairwise(ranges):
            assert prev["max"] == nxt["min"]
    # spot values: ISO 2768-m is +-0.1 mm across 0.5-6 mm
    lin = data["linear_mm"]["ranges"]
    assert lin[0]["m"] == 0.1 and lin[1]["m"] == 0.1
    # Table 1 gives no 'v' below 3 mm and no 'f' above 2000 mm
    assert lin[0]["v"] is None and lin[7]["f"] is None
    assert lin[7]["m"] == 2.0
    ang = data["angular_arcmin"]["ranges"]
    assert ang[0]["m"] == 60 and ang[0]["v"] == 180


def test_joint_leaf_param_packaged_leaf() -> None:
    """A real S1 leaf resolves against the shipped export — no
    monkeypatch, so ``validate_joint`` reads ``design_rules.json``."""
    joint = joints.validate_joint(
        {
            "class": "rigid",
            "mechanism": "screw",
            "params": {"leaf": "join/integrate/one-body-print-mould"},
        }
    )
    assert joint["params"]["leaf"] == "join/integrate/one-body-print-mould"


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
    options line — the fallback when the packaged file is absent."""
    monkeypatch.setattr(
        design_tree,
        "load_export",
        lambda: {"version": 1, "nodes": {}, "leaves": [], "axes": [], "principles": []},
    )
    with pytest.raises(joints.JointError, match="leaf") as exc:
        joints.validate_joint({"class": "rigid", "params": {"leaf": "x"}})
    assert "(none" in str(exc.value)
