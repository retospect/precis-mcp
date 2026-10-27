"""Cross-schema run diffs distinguish added capture from changed behavior."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from precis_se.traceview import Trace, diff

_SCHEMA3_FIELDS: dict[str, Any] = {
    "ret": "<Geom abc>",
    "returns": {"count": 2, "first": 1, "last": 2, "digest": "abc"},
    "args_last": {"n": 2},
    "args_digest": "abc",
}


def _doc(schema: int) -> dict[str, Any]:
    frames = [
        {
            "id": 0,
            "key": "root",
            "parent": None,
            "qual": "",
            "args": {},
            "made": {},
            "touched": {},
        },
        {
            "id": 1,
            "key": "call",
            "parent": "root",
            "qual": "build",
            "args": {"n": 1},
            "made": {"atoms": 1},
            "touched": {"atoms": 1},
        },
    ]
    if schema == 1:
        frames[0]["key"] = "legacy-root"
        frames[1]["key"] = "legacy-call"
        frames[1]["parent"] = "legacy-root"
    if schema == 3:
        frames[1].update(deepcopy(_SCHEMA3_FIELDS))
    return {"schema": schema, "run": {}, "frames": frames}


def _pair(reverse: bool, legacy_schema: int) -> tuple[dict[str, Any], dict[str, Any]]:
    first, second = _doc(legacy_schema), _doc(3)
    return (second, first) if reverse else (first, second)


@pytest.mark.parametrize("legacy_schema", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
def test_schema_upgrade_alone_folds_unchanged_subtree(
    reverse: bool, legacy_schema: int
) -> None:
    old, new = _pair(reverse, legacy_schema)
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("unchanged", new["frames"][0]["key"], "2 frames")
    ]


@pytest.mark.parametrize("legacy_schema", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("field", ["args", "made", "touched"])
def test_cross_schema_common_changes_still_report(
    field: str, reverse: bool, legacy_schema: int
) -> None:
    old, new = _pair(reverse, legacy_schema)
    new["frames"][1][field] = {"n": 9} if field == "args" else {"atoms": 9}
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("changed", new["frames"][1]["key"], field)
    ]


@pytest.mark.parametrize("legacy_schema", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("field", list(_SCHEMA3_FIELDS))
def test_cross_schema_capture_changes_report_when_both_present(
    field: str, reverse: bool, legacy_schema: int
) -> None:
    old, new = _pair(reverse, legacy_schema)
    old["frames"][1][field] = "before"
    new["frames"][1][field] = "after"
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("changed", new["frames"][1]["key"], field)
    ]


@pytest.mark.parametrize("legacy_schema", [1, 2])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("missing_old", [False, True])
@pytest.mark.parametrize("field", list(_SCHEMA3_FIELDS))
def test_cross_schema_missing_capture_on_either_side_is_ignored(
    field: str, missing_old: bool, reverse: bool, legacy_schema: int
) -> None:
    old, new = _pair(reverse, legacy_schema)
    old["frames"][1].update(deepcopy(_SCHEMA3_FIELDS))
    new["frames"][1].update(deepcopy(_SCHEMA3_FIELDS))
    missing = old if missing_old else new
    del missing["frames"][1][field]
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("unchanged", new["frames"][0]["key"], "2 frames")
    ]


@pytest.mark.parametrize("missing_old", [False, True])
@pytest.mark.parametrize("field", list(_SCHEMA3_FIELDS))
def test_same_schema_missing_capture_remains_a_change(
    field: str, missing_old: bool
) -> None:
    old, new = _doc(3), _doc(3)
    missing = old if missing_old else new
    del missing["frames"][1][field]
    rows = diff(Trace(old), Trace(new))
    assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
        ("changed", new["frames"][1]["key"], field)
    ]


def _repeated_doc(schema: int) -> dict[str, Any]:
    doc = _doc(schema)
    root, prototype = doc["frames"]
    calls = []
    for ordinal in range(3):
        frame = deepcopy(prototype)
        frame.update(
            id=ordinal + 1,
            key=f"{prototype['key']}-{ordinal}",
            args={"n": ordinal},
            occ=ordinal if schema != 1 else (1 if ordinal == 2 else 0),
            line=20 if ordinal == 1 else 10,
        )
        calls.append(frame)
    # Schema 1 emitted each call site's siblings together. IDs still record
    # execution order: line 10, line 20, line 10. The second site's occ=0
    # must match modern per-qual occ=1, not the first call's occ=0.
    doc["frames"] = (
        [root, calls[0], calls[2], calls[1]] if schema == 1 else [root, *calls]
    )
    return doc


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("field", [None, "args", "made", "touched"])
def test_schema1_occurrences_match_across_call_sites(
    reverse: bool, field: str | None
) -> None:
    legacy, modern = _repeated_doc(1), _repeated_doc(3)
    old, new = (modern, legacy) if reverse else (legacy, modern)
    changed_frame = next(frame for frame in new["frames"] if frame["id"] == 2)
    if field is not None:
        changed_frame[field] = {"n": 9} if field == "args" else {"atoms": 9}
    rows = diff(Trace(old), Trace(new))
    if field is None:
        assert [(r.kind, r.frame["key"], r.note) for r in rows] == [
            ("unchanged", new["frames"][0]["key"], "4 frames")
        ]
    else:
        assert not any(r.kind in {"added", "removed"} for r in rows)
        assert [(r.frame["key"], r.note) for r in rows if r.kind == "changed"] == [
            (changed_frame["key"], field)
        ]
        assert sum(r.kind == "unchanged" for r in rows) == 2
