"""Stored/local deterministic S1 read integration; no builds or relaxation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
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
        pytest.fail("S1 must use the single-statement snapshot, not structure_load")

    def structure_positions_snapshot(self, _ref_id: int) -> Any:
        if self.ref is None:
            return None
        return {
            "ref_id": self.ref.id,
            "version": self.ref.meta.get("version"),
            "lattice": self.scene.cell.lattice.tolist(),
            "fractional": [a.frac.tolist() for a in self.scene.atoms.values()],
        }

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
    rows = {
        parts[0]: [float(v) for v in parts[1:]]
        for line in text.splitlines()
        if (parts := line.split("\t"))[0] in {"sheet", "tube"}
    }
    assert rows["tube"] == pytest.approx([2, 0.2, 0.29, 0.3])
    assert rows["sheet"] == pytest.approx([2, 0.4, 0.58, 0.6])
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


class VersionedReadStore(ReadStore):
    """Committed-save interleavings; no live writer or database required."""

    def __init__(self, change: str = "stable") -> None:
        super().__init__([[0, 0, 0.4]])
        self.change = change
        self.loads = 0
        self.design = SimpleNamespace(
            id=3, slug="source-design", title="Design", meta={}
        )

    def get_ref(self, **kwargs: Any) -> Any:
        if kwargs["kind"] == "se":
            return self.design
        assert kwargs["kind"] == "structure"
        return self.ref

    def _rewrite(self) -> None:
        assert self.ref is not None
        if self.change not in ("same_version", "after_snapshot"):
            self.ref.meta["version"] = 5
        self.scene = SimpleNamespace(
            cell=Cell(np.eye(3) * 20),
            atoms={"a": SimpleNamespace(frac=np.array([0, 0, 0.035]))},
        )

    def structure_load(self, _ref_id: int) -> Any:
        """Legacy mixed read demonstrating why equal version cannot guard it."""
        old_cell = self.scene.cell
        self._rewrite()
        return SimpleNamespace(cell=old_cell, atoms=self.scene.atoms), {}

    def structure_positions_snapshot(self, _ref_id: int) -> Any:
        self.loads += 1
        assert _ref_id == 7
        assert self.ref is not None
        if self.change in ("between_reads", "inside_load", "same_version"):
            self._rewrite()
        elif self.change == "lost_version":
            self.ref.meta = {}
        elif self.change == "deleted":
            self.ref = None
        elif self.change == "identity":
            self.ref.id = 8
        snapshot = super().structure_positions_snapshot(_ref_id)
        if self.change == "after_snapshot":
            self._rewrite()
        return snapshot


@pytest.fixture
def public_get(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Actual registered core.get -> invocation gate -> handler -> renderer.

    Replace IO only; never replace the get tool, gate or handler method.
    """
    from precis.protocol import Response
    from precis.runtime.dispatch import DispatchMixin
    from precis.tools import TOOL_REGISTRY, core

    tree = SeTree()
    tree.blocks["tube"] = node(uid=41)
    monkeypatch.setattr(persist, "load_tree", lambda *_args: tree)
    runtime = object.__new__(DispatchMixin)
    setattr(runtime, "default_tags_resolved", [])  # noqa: B010
    store = VersionedReadStore()
    handler = SeHandler(hub=cast(Hub, SimpleNamespace(store=store, embedder=None)))
    monkeypatch.setattr(
        handler, "_render_list", lambda: Response(body="DEFAULT-DESIGN-LIST-SENTINEL")
    )

    def dispatch(verb: str, payload: dict[str, Any]) -> str:
        return runtime._invoke_handler(
            handler,
            verb,
            kind=payload.pop("kind"),
            kind_was_defaulted=False,
            args=payload,
        ).body

    monkeypatch.setattr(core, "_dispatch", dispatch)
    fn = TOOL_REGISTRY["get"]["func"]
    assert fn is core.get
    return fn, store


@pytest.mark.parametrize("selector", [None, "", "   ", "/"])
def test_public_get_missing_design_rejects_deviation_only(
    public_get: Any, selector: Any
) -> None:
    fn, store = public_get
    args = {"name": "tube", "target": {"features": []}}
    with pytest.raises(BadInput, match="specific SE design id") as caught:
        fn(kind="se", id=selector, view=" Surface_Deviation ", args=args)
    correction = str(caught.value.next)
    assert all(
        token in correction
        for token in [
            "id='<design>'",
            "'name'",
            "'target'",
            "'features'",
            "view='surface_deviation'",
        ]
    )
    assert store.loads == 0
    assert fn(kind="se", id=selector).startswith("DEFAULT-DESIGN-LIST-SENTINEL")
    assert fn(kind="se", id=selector, view="tree").startswith(
        "DEFAULT-DESIGN-LIST-SENTINEL"
    )


def test_public_get_exact_ball12_call_scores_aC343_against_sphere(
    public_get: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/surface_deviation_ball12.json").read_text()
    )
    fn, store = public_get
    store.design.slug = "hexfold-dogfood-r4"
    tree = SeTree()
    tree.blocks["ball12"] = SeBlock(
        name="ball12",
        uid=207,
        bound_kind="structure",
        bound="hexfold-dogfood-r4-ball12",
    )
    monkeypatch.setattr(persist, "load_tree", lambda *_args: tree)
    point = np.array(fixture["atom"]["xyz_A"])
    store.scene.atoms = {"aC343": SimpleNamespace(frac=point / 10)}
    body = fn(**fixture["call"])
    row = next(line.split("\t") for line in body.splitlines() if line.startswith("q\t"))
    assert row[:2] == ["q", "1"]
    sphere = fixture["sphere"]
    expected = abs(np.linalg.norm(point - sphere["centre_A"]) - sphere["radius_A"])
    assert [float(v) for v in row[2:]] == pytest.approx([expected] * 3, abs=1e-12)
    assert "sheet\t" not in body
    assert "source: se:hexfold-dogfood-r4; block UID=#207" in body
    assert "original generation target provenance unverified" in body


def test_public_get_stable_version_and_design_uid_provenance(public_get: Any) -> None:
    fn, store = public_get
    body = fn(
        kind="se",
        id="source-design",
        view="surface_deviation",
        args={"name": "#41", "target": {"features": []}},
    )
    assert "source: se:source-design" in body and "block UID=#41" in body
    assert "version=4" in body
    row = next(
        line.split("\t") for line in body.splitlines() if line.startswith("sheet\t")
    )
    assert row[:2] == ["sheet", "1"]
    assert [float(v) for v in row[2:]] == pytest.approx([0.4] * 3)
    assert store.loads == 1


@pytest.mark.parametrize("change", ["lost_version", "deleted", "identity"])
def test_public_get_interleaved_cell_atoms_or_identity_is_unknown(
    public_get: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fn, store = public_get
    store.change = change
    import precis_se.atomic.surface_deviation as renderer

    monkeypatch.setattr(
        renderer,
        "surface_distance",
        lambda *_a, **_k: pytest.fail("unstable snapshot reached metrics"),
    )
    body = fn(
        kind="se",
        id="source-design",
        view="surface_deviation",
        args={"name": "tube", "target": {"features": []}},
    )
    assert "unknown:" in body and "retry this read" in body
    assert "version=4" not in body and "version=5" not in body
    assert "{region" not in body and store.loads == 1


@pytest.mark.parametrize("version", [None, "4", True, 0, -1])
def test_unverifiable_initial_version_unknown_without_loading(version: Any) -> None:
    store = VersionedReadStore()
    assert store.ref is not None
    store.ref.meta["version"] = version
    body = render_surface_deviation(store, node(), {"target": {"features": []}})
    assert "unknown: structure version identity unavailable" in body
    assert "retry this read" in body and store.loads == 1


@pytest.mark.parametrize(
    "change,version,value",
    [
        ("between_reads", 5, 0.7),
        ("inside_load", 5, 0.7),
        ("same_version", 4, 0.7),
        ("after_snapshot", 4, 0.4),
    ],
)
def test_public_get_one_snapshot_across_versioned_and_same_version_rewrites(
    public_get: Any, change: str, version: int, value: float
) -> None:
    fn, store = public_get
    store.change = change
    body = fn(
        kind="se",
        id="source-design",
        view="surface_deviation",
        args={"name": "tube", "target": {"features": []}},
    )
    assert f"version={version}" in body
    row = next(
        line.split("\t") for line in body.splitlines() if line.startswith("sheet\t")
    )
    assert [float(v) for v in row[2:]] == pytest.approx([value] * 3)
    assert store.loads == 1


def test_legacy_loader_can_mix_same_version_but_snapshot_does_not() -> None:
    store = VersionedReadStore("same_version")
    mixed, _ = store.structure_load(7)
    assert store.ref is not None and store.ref.meta["version"] == 4
    atom = next(iter(mixed.atoms.values()))
    assert mixed.cell.frac_to_cart(atom.frac)[2] == pytest.approx(0.35)
    coherent = store.structure_positions_snapshot(7)
    assert coherent["version"] == 4
    assert (np.asarray(coherent["fractional"]) @ np.asarray(coherent["lattice"]))[
        0, 2
    ] == pytest.approx(0.7)
