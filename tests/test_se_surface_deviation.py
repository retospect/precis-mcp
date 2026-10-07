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
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se import persist
from precis_se.atomic.generate import PendingGenerate, finish_generate, generated_cell
from precis_se.atomic.generators.authored_foot import _top_meridian
from precis_se.atomic.generators.hexfold_spec import _canonical_frame
from precis_se.atomic.surface_deviation import render_surface_deviation
from precis_se.atomic.surface_target import (
    bind_target,
    capture_target,
    geometry_binding,
)
from precis_se.handler import SeHandler, _vet_view_args
from precis_se.ops import SeBlock, SeTree
from precis_surface.deviation import Feature, summary, surface_distance
from precis_surface.revolution import authored_meridian

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
    assert "unknown: stored authored target unavailable" in render_surface_deviation(
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


def receipt_fixture(
    kind: str = "sphere", *, actual_r: float = 10.0
) -> tuple[dict[str, Any], list[Feature], np.ndarray]:
    """Cheap evaluated analytic target + asymmetric stored-frame geometry."""
    meridian = (
        _top_meridian("sphere", 4.7, 3.0, 12.0, actual_r, 4.0)
        if kind == "sphere"
        else authored_meridian(9.2, [("line", 1.5), ("arc", 3.0, -90), ("line", 60)])
    )
    features = [Feature("q", (2.0, -3.0), meridian)]
    points = np.array([[6.7, -3.0, 7], [6.8, -3.0, 8], [12, 1, 0.2], [-12, 2, -0.3]])
    if kind == "sphere":
        r, z = meridian.segments[-1].at(np.array([0.5]))[0]
        # On/near the actual sphere bulge, so wrong requested R changes
        # the numerical result, not merely a provenance label.
        points[:2] = [[2 + r, -3, z], [2 + r + 0.2, -3, z + 0.1]]
    # Actual applied PCA isometry; raw generation frame has z reversed.
    raw = points * np.array([1.0, 1.0, -1.0])
    inverse: dict[str, Any] = {}
    framed, _ = _canonical_frame(raw, inverse=inverse)
    f = np.diag([1.0, 1.0, -1.0])
    target = capture_target(
        tuple(features),
        {"q": {"R_A": actual_r, "dome_start_A": 12.0}} if kind == "sphere" else {},
    )
    target["map"] = {
        "convention": "row-vector y=x@Q+b",
        "Q": (inverse["Q"] @ f).tolist(),
        "b_A": (inverse["b"] @ f).tolist(),
    }
    cell = generated_cell(framed)
    snapshot = {
        "ref_id": 7,
        "version": 4,
        "lattice": cell.lattice.tolist(),
        "pbc": [False] * 3,
        "has_lattice": True,
        "fractional": [cell.cart_to_frac(p).tolist() for p in framed],
        "atom_ids": [11, 12, 13, 14],
    }
    snapshot["generated"] = {
        "generator": "hexfold_scene",
        "scene": {"features": [{"top_R": 11.0}]},
        "surface_target": bind_target(target, snapshot),
    }
    return snapshot, features, points


@pytest.mark.parametrize(
    ("kind", "radius"), [("sphere", 10.0), ("sphere", 10.7), ("open", 0.0)]
)
def test_public_get_stored_target_actual_radius_and_reflected_map(
    public_get: Any, monkeypatch: pytest.MonkeyPatch, kind: str, radius: float
) -> None:
    fn, store = public_get
    snapshot, features, points = receipt_fixture(kind, actual_r=radius)

    def no_compute(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("stored target read attempted generation or canonicalization")

    monkeypatch.setattr(
        "precis_se.atomic.generators.hexfold_scene.plan_scene", no_compute
    )
    monkeypatch.setattr(
        "precis_se.atomic.generators.hexfold_spec._canonical_frame", no_compute
    )
    monkeypatch.setattr(
        store, "structure_positions_snapshot", lambda _id: copy.deepcopy(snapshot)
    )
    # Independent mutable ref metadata is deliberately wrong: the receipt is
    # read only from the atom snapshot, never from this separately fetched ref.
    store.ref.meta = {"version": 99, "generated": {"surface_target": None}}
    body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    distances, owners = surface_distance(points, features, ds=1)
    expected = summary(distances, owners, features)
    for name, row in expected.items():
        actual = next(
            line.split("\t")
            for line in body.splitlines()
            if line.startswith(name + "\t")
        )
        assert int(actual[1]) == row["atoms"]
        assert [float(v) for v in actual[2:]] == pytest.approx(
            [row[k] for k in ("mean", "p95", "max")], abs=1e-12
        )
    assert "generated-exact receipt format=1" in body and "version=4" in body
    assert float(body.split("determinant=", 1)[1].split(";", 1)[0]) == pytest.approx(-1)
    assert "SE pose/rotation/scale not applied" in body
    assert snapshot["generated"]["scene"]["features"][0]["top_R"] == 11.0
    if kind == "sphere":
        assert (
            snapshot["generated"]["surface_target"]["actual_tops"]["q"]["R_A"] == radius
        )
        requested = [
            Feature(
                "q", (2.0, -3.0), _top_meridian("sphere", 4.7, 3.0, 12.0, 11.0, 4.0)
            )
        ]
        wrong_distances, _ = surface_distance(points, requested, ds=1)
        assert not np.allclose(distances, wrong_distances, atol=1e-6)


def test_binding_encoding_is_order_independent_exact_and_normalizes_zero() -> None:
    snap, _, _ = receipt_fixture()
    record = snap["generated"]["surface_target"]
    expected = geometry_binding(record, snap)
    reordered = dict(reversed(list(record.items())))
    assert geometry_binding(reordered, snap) == expected
    zero = copy.deepcopy(snap)
    zero["fractional"][0][0] = 0.0
    positive = geometry_binding(record, zero)
    zero["fractional"][0][0] = -0.0
    assert geometry_binding(record, zero) == positive
    adjacent = copy.deepcopy(snap)
    adjacent["fractional"][0][0] = float(
        np.nextafter(adjacent["fractional"][0][0], np.inf)
    )
    assert geometry_binding(record, adjacent) != expected


@pytest.mark.parametrize(
    "change",
    [
        "row",
        "coordinate",
        "cell",
        "version",
        "ref",
        "empty",
        "missing",
        "format",
        "nan",
        "nonorthogonal",
        "primitive",
        "target",
        "frame",
        "tops",
        "binding_bool",
        "binding_float",
    ],
)
def test_public_get_invalid_stored_receipt_is_unavailable(
    public_get: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fn, store = public_get
    snap, _, _ = receipt_fixture()
    receipt = snap["generated"]["surface_target"]
    if change == "row":
        snap["atom_ids"][0] = 1  # byte-identical same-version geometry
    elif change == "coordinate":
        snap["fractional"][0][0] += 0.01
    elif change == "cell":
        snap["lattice"][0][0] = 90.0
    elif change == "version":
        snap["version"] = 5
    elif change == "ref":
        receipt["binding"]["ref_id"] = 8
    elif change == "empty":
        snap["atom_ids"] = []
        snap["fractional"] = []
    elif change == "missing":
        snap.pop("generated")
    elif change == "format":
        receipt["format"] = 2
    elif change == "nan":
        receipt["map"]["Q"][0][0] = float("nan")
    elif change == "nonorthogonal":
        receipt["map"]["Q"][0][0] = 3.0
    elif change == "primitive":
        receipt["features"][0]["segments"][0]["kind"] = "catenoid"
    elif change == "target":
        receipt["features"][0]["centre_A"][0] += 0.1
    elif change == "frame":
        receipt.pop("map")
    elif change == "tops":
        receipt.pop("actual_tops")
    elif change == "binding_bool":
        receipt["binding"]["format"] = True
    elif change == "binding_float":
        receipt["binding"]["ref_id"] = 7.0
    monkeypatch.setattr(store, "structure_positions_snapshot", lambda _id: snap)
    body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert "unknown: stored authored target unavailable" in body
    assert "target.features" in body and "legacy data is never fitted" in body
    assert "{region" not in body and "generated-exact" not in body


def test_public_get_stored_mode_null_offset_and_explicit_override(
    public_get: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fn, store = public_get
    snap, _, _ = receipt_fixture()
    snap["generated"]["surface_target"]["binding"]["sha256"] = "stale"
    monkeypatch.setattr(store, "structure_positions_snapshot", lambda _id: snap)
    with pytest.raises(BadInput, match="target requires"):
        fn(
            kind="se",
            id="source-design",
            view="surface_deviation",
            args={"name": "tube", "target": None},
        )
    with pytest.raises(BadInput, match="nonzero z_offset_A") as caught:
        fn(
            kind="se",
            id="source-design",
            view="surface_deviation",
            args={"name": "tube", "z_offset_A": 1},
        )
    assert "explicit" in str(caught.value) and "target" in str(caught.value.next)
    body = fn(
        kind="se",
        id="source-design",
        view="surface_deviation",
        args={"name": "tube", "target": {"features": []}, "z_offset_A": 1},
    )
    assert "caller-authored request" in body and "sheet\t4\t" in body


@pytest.mark.parametrize("distance", [np.inf, 1e308])
def test_public_get_nonfinite_stored_evaluation_is_unknown(
    public_get: Any, monkeypatch: pytest.MonkeyPatch, distance: float
) -> None:
    fn, store = public_get
    snap, _, _ = receipt_fixture()
    monkeypatch.setattr(store, "structure_positions_snapshot", lambda _id: snap)
    monkeypatch.setattr(
        "precis_se.atomic.surface_deviation.surface_distance",
        lambda *_a, **_kw: (np.full(4, distance), np.zeros(4, dtype=int)),
    )
    body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert (
        "unknown: stored target evaluation is nonfinite" in body
        and "{region" not in body
    )


def test_public_get_receipt_and_atoms_stay_on_same_snapshot(
    public_get: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fn, store = public_get
    snap, _, _ = receipt_fixture()

    def interleaved(_id: int) -> dict[str, Any]:
        old = copy.deepcopy(snap)
        # Commit after this snapshot: equal version and identical geometry,
        # but replacement row IDs make the OLD receipt invalid for new rows.
        snap["atom_ids"] = [i + 100 for i in snap["atom_ids"]]
        store.ref.meta = {"version": 4, "generated": None}
        return old

    monkeypatch.setattr(store, "structure_positions_snapshot", interleaved)
    first = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert "generated-exact" in first and "{region" in first
    second = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert "unknown: stored authored target unavailable" in second


@pytest.mark.parametrize(
    "points", [np.eye(3), np.zeros((4, 3)), np.vstack((np.eye(3), -np.eye(3)))]
)
def test_actual_canonical_inverse_handles_symmetric_and_degenerate_inputs(
    points: np.ndarray,
) -> None:
    inverse: dict[str, Any] = {}
    stored, _ = _canonical_frame(points, inverse=inverse)
    # Inverse emitted by the applied transform, including translation.
    np.testing.assert_allclose(stored @ inverse["Q"] + inverse["b"], points, atol=1e-14)


def pending_target_fixture(
    slug: str = "stored-target-fixture", actual_r: float = 10.0
) -> tuple[SeTree, PendingGenerate]:
    snap, _, _ = receipt_fixture(actual_r=actual_r)
    scene = Scene(cell=Cell(np.array(snap["lattice"]), pbc=(False, False, False)))
    for i, frac in enumerate(snap["fractional"]):
        label = f"aC{i + 1}"
        scene.atoms[label] = Atom(label=label, element="C", frac=np.array(frac))
    generated = copy.deepcopy(snap["generated"])
    generated["surface_target"].pop("binding")
    tree = SeTree()
    tree.blocks["tube"] = SeBlock(name="tube", uid=41)
    return tree, PendingGenerate(
        block_name="tube",
        struct_slug=slug,
        title="Fixture",
        scene=scene,
        card_text="deterministic fixture",
        provenance="test only",
        ports_map={},
        generated=generated,
    )


@pytest.mark.parametrize("replacement", ["identical", "altered", "in_place"])
def test_persisted_row_receipt_rejects_same_version_replacement(
    store: Store, replacement: str, public_get: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis_se.atomic.surface_target import stored_target

    tree, pending = pending_target_fixture(actual_r=10.7)
    finish_generate(store, tree, pending)
    ref = store.get_ref(kind="structure", id=pending.struct_slug)
    assert ref is not None
    old = store.structure_positions_snapshot(ref.id)
    assert old is not None
    stored_target(old)
    fn, reader = public_get
    reader.ref = ref
    monkeypatch.setattr(persist, "load_tree", lambda *_args: tree)
    monkeypatch.setattr(
        reader, "structure_positions_snapshot", store.structure_positions_snapshot
    )
    before_body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert (
        "generated-exact receipt format=1" in before_body and "version=1" in before_body
    )
    _, features, points = receipt_fixture(actual_r=10.7)
    distances, owners = surface_distance(points, features, ds=1)
    for name, expected in summary(distances, owners, features).items():
        row = next(
            line.split("\t")
            for line in before_body.splitlines()
            if line.startswith(name + "\t")
        )
        assert [float(v) for v in row[2:]] == pytest.approx(
            [expected[k] for k in ("mean", "p95", "max")], abs=1e-12
        )
    assert old["generated"]["surface_target"]["binding"]["ref_id"] == ref.id
    if replacement == "in_place":
        with store.tx() as conn:
            conn.execute(
                "UPDATE struct_atoms SET fa = fa + 0.001 WHERE id = %s",
                (old["atom_ids"][0],),
            )
    else:
        if replacement == "altered":
            pending.scene.atoms["aC1"].frac[0] += 0.001
        store.structure_save(
            slug=pending.struct_slug,
            title="Replaced",
            scene=pending.scene,
            version=1,
            card_text="replacement",
        )
    new = store.structure_positions_snapshot(ref.id)
    assert new is not None and old["version"] == new["version"] == 1
    if replacement != "in_place":
        assert old["atom_ids"] != new["atom_ids"]
    if replacement == "identical":
        assert old["fractional"] == new["fractional"]
    with pytest.raises(ValueError, match="binding is stale"):
        stored_target(new)
    after_body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert (
        "unknown: stored authored target unavailable" in after_body
        and "{region" not in after_body
    )


@pytest.mark.parametrize("existing", [False, True])
def test_generated_target_stamping_rolls_back_structure_and_receipt(
    store: Store, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    tree, pending = pending_target_fixture()
    before = None
    if existing:
        finish_generate(store, tree, pending)
        ref = store.get_ref(kind="structure", id=pending.struct_slug)
        assert ref is not None
        before = store.structure_positions_snapshot(ref.id)
    captured = []

    def fail(ref_id: int, _updates: Any, *, conn: Any) -> None:
        assert conn is not None
        captured.append(ref_id)
        raise RuntimeError("receipt-stamp failure")

    monkeypatch.setattr(store, "stamp_ref_meta", fail)
    with pytest.raises(RuntimeError, match="receipt-stamp failure"):
        finish_generate(store, tree, pending)
    ref = store.get_ref(kind="structure", id=pending.struct_slug)
    if existing:
        assert ref is not None
        assert store.structure_positions_snapshot(ref.id) == before
    else:
        assert ref is None and tree.blocks["tube"].bound is None
        with store.pool.connection() as conn:
            assert conn.execute(
                "SELECT count(*) FROM struct_atoms WHERE ref_id = %s", (captured[0],)
            ).fetchone() == (0,)


def test_ambiguous_sheet_feature_preserves_generation_but_read_is_unknown(
    store: Store, public_get: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree, pending = pending_target_fixture()
    assert pending.generated is not None
    receipt = pending.generated["surface_target"]
    receipt["features"][0]["name"] = "sheet"
    receipt["actual_tops"] = {"sheet": receipt["actual_tops"]["q"]}
    finish_generate(store, tree, pending)
    fn, reader = public_get
    reader.ref = store.get_ref(kind="structure", id=pending.struct_slug)
    monkeypatch.setattr(persist, "load_tree", lambda *_args: tree)
    monkeypatch.setattr(
        reader, "structure_positions_snapshot", store.structure_positions_snapshot
    )
    body = fn(
        kind="se", id="source-design", view="surface_deviation", args={"name": "tube"}
    )
    assert tree.blocks["tube"].bound == pending.struct_slug
    assert "unknown: stored authored target unavailable" in body
    assert "ambiguous generated feature name" in body and "{region" not in body


def test_scene_generator_captures_selected_target_not_requested_radius(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hexfold.report import Report
    from precis_se.atomic.generators import hexfold_scene as gen

    snap, features, points = receipt_fixture(actual_r=10.7)
    plan = SimpleNamespace(
        text="fixture",
        positions=points,
        ks={},
        rows={},
        tops={},
        findings=(),
        passes=1,
        top_plans={"q": SimpleNamespace(R=10.7)},
        top_rows={"q": None},
        target_features=tuple(features),
        dome_starts={"q": 12.0},
    )
    monkeypatch.setattr(gen, "plan_scene", lambda *_a, **_kw: plan)
    monkeypatch.setattr(
        gen, "build", lambda *_a, **_kw: SimpleNamespace(report=Report())
    )
    monkeypatch.setattr(gen, "_scene_findings", lambda *_a: [])
    monkeypatch.setattr(gen, "_top_record", lambda tp, _row: {"R": tp.R})
    captured: dict[str, Any] = {}

    def block(*_a: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return None

    monkeypatch.setattr(gen, "_block_from_net", block)
    gen.build_hexfold_scene(
        {
            "sheet": [30, 30],
            "features": [
                {
                    "name": "q",
                    "at": [15, 15],
                    "n": 12,
                    "radius": 5,
                    "tube_len": 3,
                    "top": "sphere",
                    "top_R": 11,
                }
            ],
        }
    )
    assert (
        captured["target"]["features"]
        == snap["generated"]["surface_target"]["features"]
    )
    assert captured["target"]["actual_tops"] == {
        "q": {"R_A": 10.7, "dome_start_A": 12.0}
    }
    assert captured["extra_topology"]["scene"]["features"][0]["top_R"] == 11.0
    np.testing.assert_array_equal(captured["target_flip"], [1, 1, -1])


def test_block_captures_inverse_from_same_canonicalization() -> None:
    from hexfold.build import build
    from precis_se.atomic.generators.hexfold_spec import _block_from_net

    net = build("hexfold 0.2\norigin s\ns: sheet(4,4)\n", strict=False)
    raw = np.asarray(net.seed3, dtype=float)
    # Topology-only seed fixture; no stick or model relaxation.
    _, features, _ = receipt_fixture("open")
    target = capture_target(tuple(features), {})
    result = _block_from_net(
        net,
        raw,
        spec="fixture",
        report=net.report,
        fidelity="stick",
        target=target,
        target_flip=np.array([1, 1, -1]),
    )
    mapping = result.topology["surface_target"]["map"]
    unchanged = _block_from_net(
        net, raw, spec="fixture", report=net.report, fidelity="stick"
    )
    np.testing.assert_array_equal(result.coords, unchanged.coords)
    assert result.envelope == unchanged.envelope
    assert result.topology["canonical_json"] == unchanged.topology["canonical_json"]
    np.testing.assert_allclose(
        result.coords @ np.array(mapping["Q"]) + mapping["b_A"],
        raw * [1, 1, -1],
        atol=1e-12,
    )


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
        (Path(__file__).parent / "fixtures/surface_deviation_ball12.json").read_text(
            encoding="utf-8"
        )
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
