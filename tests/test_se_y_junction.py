"""Equal-120 Y scene: admission, analytic graph and real test-DB tethered mint."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from hexfold.y_junction import straight_y
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler


def params(periods: int = 6, rows: int = 4) -> dict[str, Any]:
    return {
        "sheet": [periods, rows],
        "features": [
            {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [120, 120, 120]}
        ],
        "k_tether": 1.0,
    }


@pytest.mark.parametrize("sheet", [[2, 2], [6, 4], [30, 30]])
def test_analytic_open_y_graph_is_deterministic(sheet: list[int]) -> None:
    net, result = straight_y(*sheet)
    again, repeat = straight_y(*sheet)
    assert net == again
    assert np.array_equal(result.coords, repeat.coords)
    assert len(net.atoms) == 3 * 2 * (sheet[0] + 2) * sheet[1] + sheet[0]
    assert len([r for r in net.rings if len(r) == 8]) == 3 * (sheet[0] - 1)
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        assert len(set(ring)) == len(ring)
        assert all(
            frozenset((a, b)) in edges for a, b in zip(ring, ring[1:] + ring[:1])
        )
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    assert all(degrees[i] == 3 for i in result.seam_atoms)
    assert np.any(degrees < 3)  # deliberately open outer rims
    lengths = [
        np.linalg.norm(result.coords[i] - result.coords[j]) for i, j, _ in net.bonds
    ]
    assert lengths == pytest.approx([1.42] * len(lengths), abs=1e-12)


@pytest.mark.parametrize(
    "change",
    [
        {"sheet": [True, 4]},
        {"sheet": [31, 30]},
        {"sheet": [6.0, 4]},
        {"extra": "do not mix"},
        {"k_tether": 0},
        {"k_tether": float("nan")},
        {
            "features": [
                {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [180, 90, 90]}
            ]
        },
        {"features": [{"name": "y", "type": "k5", "dihedrals_deg": [120, 120, 120]}]},
        {"features": [{"name": "y", "type": "k3-sp2-120-z"}]},
        {
            "features": [
                {"name": "y", "type": "k3-sp2-120-z", "dihedrals_deg": [120, 120, 120]},
                {"name": "foot"},
            ]
        },
    ],
)
def test_y_invalid_input_refuses_before_build_or_relax(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid Y reached build/relax")

    monkeypatch.setattr("precis_se.atomic.generators.y_junction.straight_y", forbidden)
    monkeypatch.setattr(
        "precis_se.atomic.generators.y_junction.stick_relax_pinned", forbidden
    )
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


@pytest.mark.parametrize(
    "sheet", [[6, 4], pytest.param([30, 30], marks=pytest.mark.slow)]
)
def test_public_y_scene_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub, sheet: list[int]
) -> None:
    """Actual put/build/relax/store/get, with an isolated test DB (not prod)."""
    slug = f"hexfold-y-junction-test-{sheet[0]}x{sheet[1]}"
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "y",
        "params": params(*sheet),
    }
    handler = SeHandler(hub=hub_no_embedder)
    before = copy.deepcopy(op)
    put = handler.put(id=slug, args={"ops": [op]}).body
    assert op == before
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    node = tree.blocks["y"]
    assert node.bound_kind == "structure" and node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == params(*sheet)
    assert record["plan"]["passes"] == 1 and record["plan"]["relax"] == "tethered"
    assert "spec" not in record and "surface_target" not in record
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] == "geom.clash"]
    rings = next(f["data"] for f in findings if f["code"] == "seam.rings")
    assert len(rings["rings"]) == 3 * (sheet[0] - 1)
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert seam["degree"] == 3 and seam["hybridisation"] == "sp2"
    assert seam["normalized_triple_max"] < 1e-12
    assert seam["angle_deviation_max_deg"] < 1e-10
    assert record["plan"]["pinned_joint_atoms"] == 4 * sheet[0]
    seed_net, seed = straight_y(*sheet)
    seam_set = set(seed.seam_atoms)
    pinned = set(seam_set)
    for i, j, _ in seed_net.bonds:
        if i in seam_set:
            pinned.add(j)
        if j in seam_set:
            pinned.add(i)
    assert len(pinned) == 4 * sheet[0]
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None
    relaxed = np.asarray(snap["fractional"]) @ np.asarray(snap["lattice"])
    keep = sorted(pinned)

    # Stored frame may be re-origined, so compare the pinned set's pairwise
    # distances: any pinned atom that moved changes them.
    def pair_distances(xyz: np.ndarray) -> np.ndarray:
        return np.linalg.norm(xyz[:, None] - xyz[None], axis=-1)

    assert pair_distances(relaxed[keep]) == pytest.approx(
        pair_distances(seed.coords[keep]), abs=1e-6
    )
    assert 1.32 <= seam["bond_min_A"] <= seam["bond_max_A"] <= 1.52
    if sheet == [6, 4]:
        again = build_hexfold_scene(params(*sheet))
        assert "spec" not in again.topology and "canonical_json" not in again.topology
        assert relaxed == pytest.approx(again.coords, abs=1e-10)
        assert again.topology["report"] == record["report"]
    block = handler.get(id=slug, view="block", args={"name": "y"}).body
    assert "seam.rings" in block and "geom.summary" in block and "tethered" in block
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "y"}).body
    assert "unknown: stored authored target unavailable" in unknown


def test_public_unequal_y_refuses_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unequal public Y crossed build/persistence boundary")

    monkeypatch.setattr("precis_se.atomic.generators.y_junction.straight_y", forbidden)
    monkeypatch.setattr(type(store), "insert_ref", forbidden)
    bad = params()
    bad["features"][0]["dihedrals_deg"] = [180, 90, 90]
    slug = "hexfold-y-junction-unequal-refusal"
    assert store.get_ref(kind="se", id=slug) is None
    with pytest.raises(BadInput, match="fit.unsolvable"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "y",
                        "params": bad,
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
    assert store.get_ref(kind="structure", id=f"{slug}-y") is None
