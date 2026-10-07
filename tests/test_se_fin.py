"""Fin-on-tube scene: admission, analytic graft graph and real test-DB tethered mint."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from hexfold.check import _clash_pairs
from hexfold.fin import fin_height, fin_tube, smallest_inward_tube
from hexfold.lattice import tube_radius
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler


def params(
    n: int = 6, periods: int = 4, side: str = "out", rows: int = 3
) -> dict[str, Any]:
    return {
        "tube": [n, periods],
        "features": [{"name": "f", "type": "fin-sp3-z", "side": side, "rows": rows}],
        "k_tether": 1.0,
    }


@pytest.mark.parametrize(
    ("n", "periods", "rows", "side"),
    [(6, 4, 3, "out"), (10, 20, 4, "out"), (18, 20, 4, "in")],
)
def test_analytic_graft_graph_is_deterministic_and_sigma_exact(
    n: int, periods: int, rows: int, side: str
) -> None:
    net, graft = fin_tube(n, periods, rows, side)
    again, repeat = fin_tube(n, periods, rows, side)
    assert net == again
    assert np.array_equal(graft.coords, repeat.coords)
    sigma = net.lattice.sigma_A
    # the two open end rims stay ungrafted: one graft per interior period
    assert len(graft.wall_atoms) == len(graft.rim_atoms) == periods - 1
    assert len(graft.graft_rings) == periods - 2
    assert graft.radius_A == pytest.approx(tube_radius(n, n), abs=1e-6)
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        assert len(set(ring)) == len(ring)
        assert all(
            frozenset((a, b)) in edges for a, b in zip(ring, ring[1:] + ring[:1])
        )
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    sp3 = {a.ord for a in net.atoms if a.hyb == "sp3"}
    assert sp3 == set(graft.wall_atoms)
    assert all(degrees[w] == 4 for w in graft.wall_atoms)
    assert int((degrees == 4).sum()) == len(graft.wall_atoms)
    assert all(degrees[r] == 3 for r in graft.rim_atoms)
    assert not np.any(degrees < 2)  # no stub left by the strip's guard trim
    assert net.attach == tuple(zip(graft.wall_atoms, graft.rim_atoms))
    pos = graft.coords
    for w, r in zip(graft.wall_atoms, graft.rim_atoms):
        assert np.linalg.norm(pos[w] - pos[r]) == pytest.approx(sigma, abs=1e-12)
        assert pos[w, 1] == pytest.approx(0.0, abs=1e-9)  # on the generatrix
    fin = np.asarray(graft.fin_atoms)
    assert np.all(pos[fin, 1] == 0.0)
    radial = pos[fin, 0]
    if side == "out":
        assert radial.min() == pytest.approx(graft.radius_A + sigma, abs=1e-9)
        assert radial.max() == pytest.approx(
            graft.radius_A + sigma + fin_height(rows, sigma), abs=1e-9
        )
    else:
        assert radial.max() == pytest.approx(graft.radius_A - sigma, abs=1e-9)
        assert radial.min() >= 2.0
    strip_bonds = [(i, j) for i, j, _ in net.bonds if i in set(fin) and j in set(fin)]
    for i, j in strip_bonds:
        assert np.linalg.norm(pos[i] - pos[j]) == pytest.approx(sigma, abs=1e-12)
    assert not _clash_pairs(pos, net.bonds, 1.8)
    assert dict(net.regions)["tube"] == graft.tube_atoms
    assert dict(net.regions)["fin"] == graft.fin_atoms


def test_inward_fin_that_reaches_the_axis_is_refused_naming_the_tube() -> None:
    with pytest.raises(ValueError, match=r"smallest armchair tube .* \(16,16\)"):
        fin_tube(10, 20, 4, "in")
    assert smallest_inward_tube(4, 1.42, 2.46) == 16
    net, graft = fin_tube(16, 4, 4, "in")
    assert graft.coords[list(graft.fin_atoms), 0].min() >= 2.0


def test_too_short_tube_has_no_interior_graft_sites() -> None:
    with pytest.raises(ValueError, match="interior graft sites"):
        fin_tube(6, 2, 3, "out")


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [3, 4]},
        {"tube": [6, 61]},
        {"tube": [6.0, 4]},
        {"tube": [True, 4]},
        {"sheet": [6, 4]},
        {"extra": "do not mix"},
        {"k_tether": 0},
        {"k_tether": float("nan")},
        {"features": [{"name": "f", "type": "fin-sp3-z", "side": "up"}]},
        {"features": [{"name": "f", "type": "fin-sp3-z"}]},
        {"features": [{"name": "f", "type": "fin-sp3-z", "side": "out", "rows": 1}]},
        {"features": [{"name": "f", "type": "fin-sp3-z", "side": "out", "n": 6}]},
        {"features": [{"name": "", "type": "fin-sp3-z", "side": "out"}]},
        {
            "features": [
                {"name": "f", "type": "fin-sp3-z", "side": "out"},
                {"name": "foot"},
            ]
        },
    ],
)
def test_fin_invalid_input_refuses_before_build_or_relax(
    change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid fin input crossed into fin_tube")

    monkeypatch.setattr("precis_se.atomic.generators.fin.fin_tube", forbidden)
    bad = {**params(), **change}
    with pytest.raises(GeneratorError):
        build_hexfold_scene(bad)


def test_default_rows_is_four_and_scene_records_it() -> None:
    block = build_hexfold_scene(
        {
            "tube": [6, 4],
            "features": [{"name": "f", "type": "fin-sp3-z", "side": "out"}],
        }
    )
    assert block.topology["scene"] == params(6, 4, "out", 4)
    assert block.topology["plan"]["grafts"] == 3
    assert "spec" not in block.topology and "canonical_json" not in block.topology


def test_public_fin_scene_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub
) -> None:
    """Actual put/build/relax/store/get, with an isolated test DB (not prod)."""
    slug = "hexfold-fin-test-6x4"
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "f",
        "params": params(),
    }
    handler = SeHandler(hub=hub_no_embedder)
    before = copy.deepcopy(op)
    handler.put(id=slug, args={"ops": [op]})
    assert op == before
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    node = tree.blocks["f"]
    assert node.bound_kind == "structure" and node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == params()
    assert record["plan"]["passes"] == 1 and record["plan"]["relax"] == "tethered"
    assert record["plan"]["grafts"] == 3 and record["plan"]["pinned_joint_atoms"] == 6
    assert "spec" not in record and "surface_target" not in record
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] in ("geom.clash", "geom.seed_overlap")]
    rings = next(f["data"] for f in findings if f["code"] == "graft.rings")
    assert len(rings["rings"]) == 2 and rings["grafts"] == 3
    graft = next(f["data"] for f in findings if f["code"] == "graft.geometry")
    assert graft["degree"] == 4 and graft["hybridisation"] == "sp3"
    assert graft["side"] == "out"
    assert 1.40 <= graft["radial_bond_min_A"] <= graft["radial_bond_max_A"] <= 1.44
    assert 80.0 <= graft["angle_min_deg"] <= graft["angle_max_deg"] <= 130.0
    seed_net, seed = fin_tube(6, 4, 3, "out")
    pinned = sorted({*seed.wall_atoms, *seed.rim_atoms})
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None
    relaxed = np.asarray(snap["fractional"]) @ np.asarray(snap["lattice"])

    def pair_distances(xyz: np.ndarray) -> np.ndarray:
        return np.linalg.norm(xyz[:, None] - xyz[None], axis=-1)

    assert pair_distances(relaxed[pinned]) == pytest.approx(
        pair_distances(seed.coords[pinned]), abs=1e-6
    )
    again = build_hexfold_scene(params())
    assert relaxed == pytest.approx(again.coords, abs=1e-10)
    assert again.topology["report"] == record["report"]
    block = handler.get(id=slug, view="block", args={"name": "f"}).body
    assert "graft.rings" in block and "geom.summary" in block and "tethered" in block
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "f"}).body
    assert "unknown: stored authored target unavailable" in unknown


def test_public_inward_misfit_refuses_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        type(store),
        "insert_ref",
        lambda *_a, **_k: pytest.fail("misfit fin crossed the persistence boundary"),
    )
    slug = "hexfold-fin-inward-misfit"
    with pytest.raises(BadInput, match=r"smallest armchair tube"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "f",
                        "params": params(10, 20, "in", 4),
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
