"""Stored/local deterministic S1 read integration; no builds or relaxation."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.structure.cell import Cell
from precis_se import persist
from precis_se.atomic.surface_deviation import render_surface_deviation
from precis_se.handler import SeHandler, _vet_view_args
from precis_se.ops import SeBlock, SeTree

TARGET: dict[str, Any] = {
    "features": [
        {
            "name": "tube",
            "centre_A": [0, 0],
            "r0_A": 5,
            "pieces": [["arc", 1, -90], ["line", 3]],
        }
    ]
}


class ReadStore:
    def __init__(self, points: Any, bound: bool = True) -> None:
        self.ref = (
            SimpleNamespace(id=7, slug="stored", title="stored", meta={"version": 4})
            if bound
            else None
        )
        self.scene = SimpleNamespace(
            cell=Cell(np.eye(3) * 10),
            atoms={
                str(i): SimpleNamespace(frac=np.asarray(p) / 10)
                for i, p in enumerate(points)
            },
        )

    def get_ref(self, **_kwargs: Any) -> Any:
        return self.ref

    def structure_load(self, _ref_id: int) -> Any:
        return self.scene, {}

    def __getattr__(self, name: str) -> Any:
        pytest.fail(
            f"read-only deviation tried unexpected store/write operation {name}"
        )


def node(**overrides: Any) -> SeBlock:
    return SeBlock(name="tube", bound_kind="structure", bound="stored", **overrides)


def test_metrics_in_angstrom_and_only_supplied_rigid_z_removed() -> None:
    points = np.array([[4.1, 0, 4], [4.3, 0, 5], [10, 0, 2.2], [10, 0, 2.6]])
    store = ReadStore(points)
    before = copy.deepcopy(store.scene.atoms)
    target = copy.deepcopy(TARGET)
    text = render_surface_deviation(store, node(), {"target": target, "z_offset_A": 2})
    assert "structure-local Å" in text and "version=4" in text
    assert "original generation target provenance unverified" in text
    assert "z_offset_A=2.0" in text and "no fitted rotation/scale" in text
    assert "tube" in text and "sheet" in text and "p95" in text
    assert "0.58" in text and "0.29" in text and "0.6" in text
    assert target == TARGET
    assert all(
        np.array_equal(store.scene.atoms[k].frac, v.frac) for k, v in before.items()
    )
    unaligned = render_surface_deviation(store, node(), {"target": target})
    assert unaligned != text and "z_offset_A=0.0" in unaligned


def test_no_target_is_unknown_not_inferred_from_atoms() -> None:
    assert "unknown: authored target absent" in render_surface_deviation(
        ReadStore([[0, 0, 0]]), node(), {}
    )


@pytest.mark.parametrize("points", [[], [[0, 0, np.nan]], [[0, 0]]])
def test_absent_or_bad_coordinates_unknown(points: Any) -> None:
    assert "unknown:" in render_surface_deviation(
        ReadStore(points), node(), {"target": TARGET}
    )


def test_dangling_unbound_and_instance_unknown() -> None:
    args = {"target": TARGET}
    assert "bound structure is missing" in render_surface_deviation(
        ReadStore([], False), node(), args
    )
    assert "no bound structure" in render_surface_deviation(
        ReadStore([]), SeBlock(name="empty"), args
    )
    assert "instance frame" in render_surface_deviation(
        ReadStore([]), node(template="x"), args
    )


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"features": "x"},
        {"features": [3]},
        {"features": [{**TARGET["features"][0], "name": "sheet"}]},
        {"features": TARGET["features"] * 2},
        {"features": [{**TARGET["features"][0], "centre_A": [0]}]},
        {"features": [{**TARGET["features"][0], "r0_A": True}]},
        {"features": [{**TARGET["features"][0], "r0_A": np.inf}]},
        {"features": [{**TARGET["features"][0], "r0_A": 0}]},
        {"features": [{**TARGET["features"][0], "pieces": []}]},
        {"features": [{**TARGET["features"][0], "pieces": [["line", -1]]}]},
        {"features": [{**TARGET["features"][0], "pieces": [["line", 6]]}]},
        {"features": [{**TARGET["features"][0], "pieces": [["catenoid", 3]]}]},
        {"features": [{**TARGET["features"][0], "pieces": [3]}]},
    ],
)
def test_invalid_target_is_an_input_error(target: Any) -> None:
    with pytest.raises(BadInput):
        render_surface_deviation(ReadStore([[0, 0, 0]]), node(), {"target": target})


def test_overlap_is_not_silently_assigned_and_explicit_sheet_is_valid() -> None:
    target = {
        "features": [TARGET["features"][0], {**TARGET["features"][0], "name": "other"}]
    }
    with pytest.raises(BadInput, match="overlap"):
        render_surface_deviation(ReadStore([[10, 0, 0]]), node(), {"target": target})
    text = render_surface_deviation(
        ReadStore([[0, 0, 0.4]]), node(), {"target": {"features": []}}
    )
    assert "sheet" in text and "0.4" in text


def test_get_dispatch_and_args_gate_are_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReadStore([[4.1, 0, 2]])
    tree = SeTree()
    tree.blocks["tube"] = node()
    monkeypatch.setattr(persist, "load_tree", lambda *_args: tree)
    handler = SeHandler(hub=cast(Hub, SimpleNamespace(store=store, embedder=None)))
    body = handler.get(
        id="stored", view="surface_deviation", args={"name": "tube", "target": TARGET}
    ).body
    assert "authored-surface deviation" in body
    row = body.splitlines()[-1].split("\t")
    assert row[:2] == ["tube", "1"]
    assert [float(value) for value in row[2:]] == pytest.approx([0.1] * 3)
    with pytest.raises(BadInput, match="requires args"):
        handler.get(id="stored", view="surface_deviation", args={"target": TARGET})
    for key in ("state", "rotation", "scale"):
        with pytest.raises(BadInput):
            _vet_view_args("surface_deviation", {"name": "tube", key: 0})


@pytest.mark.parametrize("offset", [True, "2", np.inf, np.nan])
def test_bad_z_offset_refused(offset: Any) -> None:
    with pytest.raises(BadInput, match="z_offset_A"):
        render_surface_deviation(
            ReadStore([]), node(), {"target": TARGET, "z_offset_A": offset}
        )
