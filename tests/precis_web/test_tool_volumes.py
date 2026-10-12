"""The swept tool/hand volumes of a failing insertion-path finding as a
viewer ``Shapes`` group (docs/backlog/se-mechanical-drc.md rulings 5/6).

The blocked tree is the hand-checkable case from
tests/test_se_insertion_path.py: an M4 cap screw at the origin driving +z
into a plate, with a cover box over its head on the -z side.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from precis_se import catalog, fasten
from precis_se.ops import ConnectSpec, SeBlock, SeTree
from precis_web.blocktree_3d import TOOL_VOLUMES_GROUP, tool_volume_group

M4 = {
    "thread_size": "M4",
    "thread_pitch": 0.0007,
    "outer_diameter": 0.004,
    "head_diameter": 0.007,
    "head_height": 0.004,
    "length": 0.020,
    "drive_size": 0.003,
    "drive_type": "socket",
    "head_form": "cap",
    "point_type": "machine",
}

ROOT = "Structural envelopes"


def _blocked_tree() -> SeTree:
    tree = SeTree()
    screw = SeBlock(name="screw", pose=[0, 0, 0])
    screw.bound_kind = "component"
    screw.bound = "iso-4762-m4x20"
    screw.derived = catalog.derive("fastener", dict(M4))
    tree.blocks["screw"] = screw
    plate = SeBlock(name="plate", pose=[0, 0, 0.006], envelope="box:w0.05d0.05h0.010")
    plate.mode = "cnc-2.5ax/aluminium"
    tree.blocks["plate"] = plate
    tree.blocks["cover"] = SeBlock(
        name="cover", pose=[0, 0, -0.014], envelope="box:w0.03d0.03h0.004"
    )
    tree.connects.append(
        ConnectSpec(
            a_block="screw",
            a_port="thread",
            b_block="plate",
            b_port="hole",
            joint={"class": "rigid", "mechanism": "screw"},
        )
    )
    return tree


def _found(tree: SeTree) -> list[dict[str, Any]]:
    return [
        {
            "rule": f.rule,
            "subject": f.subject,
            "severity": f.severity,
            "detail": f.detail,
            "geometry": f.geometry,
        }
        for f in fasten.findings(fasten.fasten(tree))
        if f.geometry
    ]


def _leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    if "parts" not in node:
        return [node]
    return [leaf for part in node["parts"] for leaf in _leaves(part)]


def test_blocked_screw_renders_translucent_volumes_under_the_root() -> None:
    found = _found(_blocked_tree())
    assert [f["rule"] for f in found] == ["fastener_insertion_path"]
    group = tool_volume_group(found, ROOT, 1.0)
    assert group is not None
    assert group["id"] == f"/{ROOT}/{TOOL_VOLUMES_GROUP}"
    (finding_group,) = group["parts"]
    assert finding_group["id"].startswith(group["id"] + "/1 fastener_insertion_path ")
    leaves = _leaves(group)
    assert leaves
    for leaf in leaves:
        assert leaf["id"].startswith(finding_group["id"] + "/")
        assert leaf["alpha"] < 1.0 and leaf["uid"] < 0
    body = [leaf for leaf in leaves if leaf["name"].startswith("body")]
    assert body and any("cover" in leaf["name"] for leaf in body)
    assert all(leaf["color"] == "#dc2626" for leaf in body)


def test_volumes_follow_the_scene_display_scale() -> None:
    found = _found(_blocked_tree())
    one = tool_volume_group(found, ROOT, 1.0)
    two = tool_volume_group(found, ROOT, 2.0)
    assert one is not None and two is not None
    v1 = np.asarray(_leaves(one)[0]["shape"]["vertices"], dtype=float)
    v2 = np.asarray(_leaves(two)[0]["shape"]["vertices"], dtype=float)
    assert np.allclose(v2, 2.0 * v1)


def test_clear_screw_has_no_volumes() -> None:
    tree = _blocked_tree()
    del tree.blocks["cover"]
    assert tool_volume_group(_found(tree), ROOT, 1.0) is None


def test_subject_slash_cannot_split_the_viewer_path() -> None:
    rec = {
        "envelope": "cyl:r0.002h0.01",
        "pose": [0.0, 0.0, 0.0],
        "rot": [0.0, 0.0, 0.0],
        "role": "tool",
        "blocker": "a/b",
    }
    found = [{"rule": "fastener_insertion_path", "subject": "x/y", "geometry": [rec]}]
    group = tool_volume_group(found, ROOT, 1.0)
    assert group is not None
    (leaf,) = _leaves(group)
    assert leaf["id"].count("/") == 4  # root / group / finding / leaf
    assert leaf["color"] == "#f59e0b"
