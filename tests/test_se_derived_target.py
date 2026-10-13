"""Spec-derived target refusal paths: every unreadable stored record raises
ValueError, never a guessed placement (gr479994)."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis_se.atomic import derived_target as dt
from precis_se.atomic.derived_target import _frame, _scene_feature, _top_record
from precis_se.atomic.generators.authored_foot import SceneFeature
from tests.test_se_surface_deviation import derived_fixture

FEATURE = SceneFeature("q", (8, 8), 12, 3.0, 3, "open")
TOP_FEATURE = SceneFeature("q", (8, 8), 12, 3.0, 3, "lid", top_fillet=2.0)


def snapshot() -> dict[str, Any]:
    return copy.deepcopy(derived_fixture()[0])


def snapshot_feature() -> dict[str, Any]:
    return {
        "name": "q",
        "at": [8, 8],
        "n": 12,
        "radius": 3.0,
        "tube_len": 3,
        "top": "open",
    }


@pytest.mark.parametrize(
    "raw",
    [{"bogus": 1}, {"name": "q"}, {**snapshot_feature(), "radius": "wide"}],
)
def test_scene_feature_parse_failure_is_a_value_error(raw: Any) -> None:
    with pytest.raises(ValueError):
        _scene_feature(raw, 0)


@pytest.mark.parametrize(
    ("plan", "match"),
    [
        (None, "no authored-top record"),
        ({"top_plans": {"q": {"kind": "ball"}}}, "no authored-top record"),
        (
            {"top_plans": {"q": {"kind": "lid", "R": "x", "fillet": 2, "drop": 1}}},
            "malformed",
        ),
        (
            {"top_plans": {"q": {"kind": "lid", "R": 9, "fillet": -2, "drop": 1}}},
            "nonfinite",
        ),
        (
            {"top_plans": {"q": {"kind": "lid", "R": np.inf, "fillet": 2, "drop": 1}}},
            "nonfinite",
        ),
    ],
)
def test_top_record_refuses_missing_malformed_and_nonfinite(
    plan: Any, match: str
) -> None:
    generated = {"plan": plan} if plan is not None else {}
    with pytest.raises(ValueError, match=match):
        _top_record(generated, TOP_FEATURE)


def test_frame_refuses_a_rebuilt_spec_without_a_sheet() -> None:
    inst = np.array(["q", "q"])
    points = np.array([[0.0, 0, 5], [1.0, 0, 5]])
    with pytest.raises(ValueError, match="no sheet instance 's'"):
        _frame(inst, points, (FEATURE,))


def test_frame_refuses_a_feature_absent_from_the_rebuilt_spec() -> None:
    inst = np.array(["s", "s"])
    points = np.array([[0.0, 0, 0], [1.0, 0, 0.2]])
    with pytest.raises(ValueError, match="absent from the rebuilt spec"):
        _frame(inst, points, (FEATURE,))


def test_frame_refuses_a_feature_sitting_on_the_sheet_plane() -> None:
    inst = np.array(["s", "s", "q", "q"])
    points = np.array([[0.0, 0, 0.0], [10.0, 0, 0.2], [5.0, 0, 0.5], [5.0, 1, 0.7]])
    with pytest.raises(ValueError, match="grow side is ambiguous"):
        _frame(inst, points, (FEATURE,))


def test_frame_refuses_features_on_both_sides_of_the_sheet() -> None:
    other = SceneFeature("r", (4, 4), 12, 3.0, 3, "open")
    inst = np.array(["s", "s", "q", "r"])
    points = np.array([[0.0, 0, 0.0], [10.0, 0, 0.2], [5.0, 0, 8.0], [5.0, 1, -8.0]])
    with pytest.raises(ValueError, match="both sides of the sheet"):
        _frame(inst, points, (FEATURE, other))


def test_missing_or_unparseable_stored_scene_features_refuse() -> None:
    snap = snapshot()
    snap["generated"]["scene"] = {}
    with pytest.raises(ValueError, match="features missing"):
        dt.derived_target(snap)
    snap = snapshot()
    snap["generated"]["scene"]["features"] = [{"bogus": 1}]
    with pytest.raises(ValueError, match="unknown key"):
        dt.derived_target(snap)


@pytest.mark.parametrize("spec", ["garbage", "hexfold 0.2\norigin s\ns: bogus(\n"])
def test_a_spec_that_does_not_rebuild_refuses(spec: str) -> None:
    snap = snapshot()
    snap["generated"]["spec"] = spec
    with pytest.raises(ValueError, match="does not rebuild"):
        dt.derived_target(snap)


def test_a_rebuild_raising_a_non_hexfold_error_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_spec: str, **_kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(dt, "build", boom)
    with pytest.raises(ValueError, match="does not rebuild: boom"):
        dt.derived_target(snapshot())


def test_a_rebuild_without_seed_positions_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dt, "build", lambda *_a, **_kw: SimpleNamespace(seed3=None))
    with pytest.raises(ValueError, match="no seed positions"):
        dt.derived_target(snapshot())


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda g, s: g.update(n_atoms=g["n_atoms"] + 1), "atom count"),
        (lambda g, s: g.update(n_atoms="5"), "atom count"),
        (
            lambda g, s: g["terminated"].update(count="x"),
            "termination record is malformed",
        ),
        (
            lambda g, s: g["terminated"].update(count=-1),
            "termination record is malformed",
        ),
        (lambda g, s: s["fractional"].pop(), "atom layout"),
        (lambda g, s: s.update(elements="x"), "atom layout"),
    ],
)
def test_loose_row_binding_refuses_layout_mismatches(mutate: Any, match: str) -> None:
    snap = snapshot()
    mutate(snap["generated"], snap)
    with pytest.raises(ValueError, match=match):
        dt.derived_target(snap)


def test_malformed_and_nonfinite_stored_coordinates_refuse() -> None:
    snap = snapshot()
    snap["lattice"] = [[10.0, 0, 0], [0, 10.0]]
    with pytest.raises(ValueError, match="malformed"):
        dt.derived_target(snap)
    snap = snapshot()
    snap["fractional"][0][0] = float("nan")
    with pytest.raises(ValueError, match="empty/nonfinite"):
        dt.derived_target(snap)


def test_a_nonpositive_stored_fillet_radius_refuses() -> None:
    snap = snapshot()
    snap["generated"]["scene"]["features"][0]["radius"] = 0
    with pytest.raises(ValueError, match="fillet radius must be positive"):
        dt.derived_target(snap)
