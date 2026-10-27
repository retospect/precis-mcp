"""gr521582: migration diffs retain identity despite line-bearing v1 keys."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis_se.traceview import Trace, diff


def _doc(schema: int) -> dict[str, Any]:
    frames = [
        ("root", None, "main", 0),
        ("build", "root", "main.build", 0),
        ("first", "build", "main.part", 0),
        ("first-cut", "first", "cut", 0),
        # v1 restarts occ at a different call site for the same callee.
        ("second", "build", "main.part", 0 if schema == 1 else 1),
        ("second-cut", "second", "cut", 0),
    ]
    return {
        "schema": schema,
        "frames": [
            {
                "id": i,
                "key": f"v{schema}-{key}",
                "parent": f"v{schema}-{parent}" if parent else None,
                "qual": qual,
                "occ": occ,
                "kind": "root" if parent is None else "user",
                "line": i + schema * 10,
                "args": {},
                "made": {"atoms": 2} if qual == "cut" else {},
                "touched": {},
            }
            for i, (key, parent, qual, occ) in enumerate(frames)
        ],
    }


@pytest.mark.parametrize("old_schema,new_schema", [(1, 2), (2, 1), (1, 3), (3, 1)])
def test_cross_schema_unchanged_tree_folds(old_schema: int, new_schema: int) -> None:
    rows = diff(Trace(_doc(old_schema)), Trace(_doc(new_schema)))
    assert [(r.kind, r.frame["qual"], r.note) for r in rows] == [
        ("unchanged", "main", "6 frames")
    ]


def test_cross_schema_repeated_parent_matches_the_right_child() -> None:
    old, new = _doc(1), _doc(2)
    new["frames"][-1]["made"] = {"atoms": 3}
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("unchanged", "v2-first", "2 frames"),
        ("changed", "v2-second-cut", "made"),
    ]


def test_cross_schema_interleaved_call_sites_follow_execution_order() -> None:
    old, new = _doc(1), _doc(2)
    for doc in (old, new):
        # Two iterations, each calling part() at two different sites.
        first, second = doc["frames"][2], doc["frames"][4]
        third, fourth = deepcopy(first), deepcopy(second)
        third.update(id=6, key=first["key"] + "-again", occ=1)
        fourth.update(id=7, key=second["key"] + "-again", occ=1)
        doc["frames"].extend([third, fourth])
    # v1 emission groups a site's iterations, hiding their execution order.
    old["frames"] = [old["frames"][i] for i in (0, 1, 2, 3, 6, 4, 5, 7)]
    new["frames"][6]["occ"] = 2
    new["frames"][7]["occ"] = 3
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.note) for r in rows] == [("unchanged", "8 frames")]


def test_cross_schema_added_and_removed_children() -> None:
    old, new = _doc(1), _doc(2)
    new["frames"][-1].update(key="v2-hole", qual="hole")
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.depth) for r in rows] == [
        ("unchanged", "v2-first", 3),
        ("added", "v2-hole", 4),
        ("removed", "v1-second-cut", 4),
    ]


def test_same_schema_still_matches_keys() -> None:
    old, new = _doc(2), _doc(2)
    new["frames"][-1]["key"] = "different-key"
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"]) for r in rows] == [
        ("unchanged", "v2-first"),
        ("added", "different-key"),
        ("removed", "v2-second-cut"),
    ]


@pytest.mark.parametrize("old_schema,new_schema", [(2, 3), (3, 2)])
def test_structural_schema_upgrade_keeps_key_identity(
    old_schema: int, new_schema: int
) -> None:
    old, new = _doc(2), _doc(2)
    old["schema"], new["schema"] = old_schema, new_schema
    new["frames"][-1]["key"] = "different-key"
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"]) for r in rows] == [
        ("unchanged", "v2-first"),
        ("added", "different-key"),
        ("removed", "v2-second-cut"),
    ]


def test_structural_schema_upgrade_matches_keys_despite_qual_change() -> None:
    old, new = _doc(2), _doc(2)
    new["schema"] = 3
    new["frames"][-1]["qual"] = "renamed"
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.note) for r in rows] == [("unchanged", "6 frames")]


def test_cross_schema_collapsed_tail_folds_without_mutating_history() -> None:
    old, new = _doc(1), _doc(2)
    for doc in (old, new):
        template = doc["frames"][2]
        doc["frames"] = doc["frames"][:2]
        for occ in range(65):
            frame = deepcopy(template)
            frame.update(id=occ + 2, key=f"{template['key']}-{occ}", occ=occ)
            if occ == 64:
                frame.update(collapsed=True, occ_end=99, id_end=101)
            doc["frames"].append(frame)
    before = deepcopy(old), deepcopy(new)
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.note) for r in rows] == [("unchanged", "67 frames")]
    assert (old, new) == before  # Historical keys and maker stamps are untouched.


def test_cross_schema_old_root_removed() -> None:
    rows = diff(Trace(_doc(1)), Trace({"schema": 2, "frames": []}))
    assert [(r.kind, r.frame["key"]) for r in rows] == [("removed", "v1-root")]
