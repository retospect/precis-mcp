"""Seam-tube scene: the equal-120 k3 seam run along a tube axis with a
strip as the third sheet; admission, analytic graph, real test-DB mint."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from hexfold.check import _clash_pairs
from hexfold.fin120 import seam_tube, wall_radius
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler


def params(
    rows: int = 6, periods: int = 4, side: str = "out", strip_rows: int = 3
) -> dict[str, Any]:
    return {
        "tube": [rows, periods],
        "features": [
            {"name": "y", "type": "fin-k3-120-z", "side": side, "rows": strip_rows}
        ],
        "k_tether": 1.0,
    }


def test_wall_radius_closes_the_two_halves_across_one_bond() -> None:
    sigma = 1.42
    for rows in (4, 6, 10, 18):
        r, theta0 = wall_radius(rows, sigma)
        y_max = (1.5 * (rows - 1) + 0.5) * sigma
        # first row at the seam's 120-degree position, top rims one bond apart
        assert r * np.sin(theta0) == pytest.approx(0.5 * np.sqrt(3) * sigma, abs=1e-9)
        assert (np.pi - theta0) * r == pytest.approx(y_max + 0.5 * sigma, abs=1e-9)
    # about the (rows, rows) armchair family
    assert wall_radius(10, sigma)[0] == pytest.approx(6.95, abs=0.05)


@pytest.mark.parametrize(
    ("periods", "rows", "strip_rows", "side"),
    [(4, 6, 3, "out"), (20, 10, 4, "out"), (20, 18, 4, "in")],
)
def test_analytic_seam_tube_graph(
    periods: int, rows: int, strip_rows: int, side: str
) -> None:
    net, tube = seam_tube(periods, rows, strip_rows, side)
    again, repeat = seam_tube(periods, rows, strip_rows, side)
    assert net == again and np.array_equal(tube.coords, repeat.coords)
    sigma = net.lattice.sigma_A
    n_strip = 2 * (periods + 2) * strip_rows
    n_wall = 2 * (periods + 2) * rows
    assert len(net.atoms) == n_strip + 2 * n_wall + periods
    assert len(tube.seam_atoms) == periods
    assert len([r for r in net.rings if len(r) == 8]) == 3 * (periods - 1)
    assert len(tube.fuse_pairs) == periods + 2
    assert len(tube.fuse_rings) == periods + 1
    edges = {frozenset((i, j)) for i, j, _ in net.bonds}
    for ring in net.rings:
        assert len(set(ring)) == len(ring)
        assert all(
            frozenset((a, b)) in edges for a, b in zip(ring, ring[1:] + ring[:1])
        )
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)], minlength=len(net.atoms)
    )
    assert all(degrees[s] == 3 for s in tube.seam_atoms)
    assert all(degrees[p] >= 2 and degrees[q] >= 2 for p, q in tube.fuse_pairs)
    assert int((degrees < 2).sum()) == 4  # the strip's and walls' bottom guard stubs
    pos = tube.coords
    for s in tube.seam_atoms:
        for n in [j for i, j, _ in net.bonds if i == s] + [
            i for i, j, _ in net.bonds if j == s
        ]:
            assert np.linalg.norm(pos[s] - pos[n]) == pytest.approx(sigma, abs=1e-9)
    assert len(tube.pinned) == 4 * periods
    assert not _clash_pairs(pos, net.bonds, 1.8)
    strip = np.asarray(tube.strip_atoms)
    assert np.all(pos[strip, 1] == 0.0)
    if side == "out":
        assert pos[strip, 0].min() == pytest.approx(
            tube.seam_radius_A + sigma, abs=1e-9
        )
        assert tube.seam_radius_A > tube.radius_A
    else:
        assert pos[strip, 0].max() == pytest.approx(
            tube.seam_radius_A - sigma, abs=1e-9
        )
        assert pos[strip, 0].min() >= 2.0
        assert tube.seam_radius_A < tube.radius_A
    wall = np.asarray(tube.wall_a_atoms + tube.wall_b_atoms)
    rho = np.hypot(pos[wall, 0], pos[wall, 1])
    assert rho.min() == pytest.approx(tube.radius_A, abs=1e-6)
    assert rho.max() < tube.radius_A + sigma
    assert dict(net.regions)["seam"] == tube.seam_atoms


def test_inward_strip_that_reaches_the_axis_is_refused_naming_the_wall() -> None:
    with pytest.raises(
        ValueError, match=r"smallest wall that seats it is 17 row pairs"
    ):
        seam_tube(20, 10, 4, "in")


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [3, 4]},
        {"tube": [6, 61]},
        {"tube": [6.0, 4]},
        {"sheet": [6, 4]},
        {"k_tether": 0},
        {"features": [{"name": "y", "type": "fin-k3-120-z", "side": "up"}]},
        {"features": [{"name": "y", "type": "fin-k3-120-z"}]},
        {"features": [{"name": "y", "type": "fin-k3-120-z", "side": "out", "rows": 1}]},
        {"features": [{"name": "y", "type": "fin-k3-120-z", "side": "out", "n": 6}]},
        {
            "features": [
                {"name": "y", "type": "fin-k3-120-z", "side": "out"},
                {"name": "foot"},
            ]
        },
    ],
)
def test_seam_tube_invalid_input_refuses_before_build(
    change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid seam-tube input crossed into seam_tube")

    monkeypatch.setattr("precis_se.atomic.generators.fin120.seam_tube", forbidden)
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


def test_public_seam_tube_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub
) -> None:
    slug = "hexfold-seam-tube-test-6x4"
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "y",
        "params": params(),
    }
    handler = SeHandler(hub=hub_no_embedder)
    handler.put(id=slug, args={"ops": [op]})
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    node = persist.load_tree(store, se_ref.id).blocks["y"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == params()
    assert (
        record["plan"]["relax"] == "tethered"
        and record["plan"]["pinned_joint_atoms"] == 16
    )
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] in ("geom.clash", "geom.seed_overlap")]
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert (
        seam["angle_deviation_max_deg"] < 1e-9 and seam["normalized_triple_max"] < 1e-9
    )
    cross = next(f["data"] for f in findings if f["code"] == "tube.cross_section")
    assert cross["side"] == "out" and cross["wall_tether"] == "none"
    # the cusp: the seam line stands proud of the wall, the far side does not
    assert cross["seam_rho_A"] > cross["wall_rho_mean_A"] > cross["far_rho_A"] - 0.5
    rings = next(f["data"] for f in findings if f["code"] == "seam.rings")
    assert len(rings["rings"]) == 9 and len(rings["fuse_pairs"]) == 6
    assert record["terminated"]["count"] > 0
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "y"}).body
    assert "unknown: stored authored target unavailable" in unknown
    block = handler.get(id=slug, view="block", args={"name": "y"}).body
    assert "tube.cross_section" in block and "tethered" in block


def test_public_inward_misfit_refuses_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        type(store),
        "insert_ref",
        lambda *_a, **_k: pytest.fail("misfit seam tube crossed persistence"),
    )
    slug = "hexfold-seam-tube-inward-misfit"
    with pytest.raises(BadInput, match="smallest wall that seats it"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "y",
                        "params": params(10, 20, "in", 4),
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
