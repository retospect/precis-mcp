"""Rail-tube scene: a closed tube of N arcs on equal-120 seams with
inward strips, authored pores and the stored lumen site list; admission,
analytic graph, real test-DB mint."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from hexfold.railtube import rail_tube
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.handler import SeHandler


def params(
    rows: int = 14,
    periods: int = 4,
    rails: int = 2,
    strip_rows: int = 3,
    pores: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    feature: dict[str, Any] = {
        "name": "y",
        "type": "rail-k3-120-z",
        "rails": rails,
        "rows": strip_rows,
    }
    if pores is not None:
        feature["pores"] = pores
    return {
        "tube": [rows, periods],
        "features": [feature],
        "k_tether": 1.0,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [3, 4]},
        {"tube": [7, 4]},
        {"tube": [6, 61]},
        {"tube": [6.0, 4]},
        {"sheet": [6, 4]},
        {"k_tether": 0},
        {
            "features": [
                {"name": "y", "type": "rail-k3-120-z", "rails": 2, "side": "out"}
            ]
        },
        {
            "features": [
                {"name": "y", "type": "rail-k3-120-z", "rails": 2, "side": "up"}
            ]
        },
        {"features": [{"name": "y", "type": "rail-k3-120-z"}]},
        {"features": [{"name": "y", "type": "rail-k3-120-z", "rails": 7}]},
        {"features": [{"name": "y", "type": "rail-k3-120-z", "rails": "3"}]},
        {"features": [{"name": "y", "type": "rail-k3-120-z", "rails": 2, "rows": 1}]},
        {"features": [{"name": "y", "type": "rail-k3-120-z", "rails": 2, "n": 6}]},
        {
            "features": [
                {"name": "y", "type": "rail-k3-120-z", "rails": 2, "pores": "x"}
            ]
        },
        {
            "features": [
                {
                    "name": "y",
                    "type": "rail-k3-120-z",
                    "rails": 2,
                    "pores": [{"arc": 0, "at": [0, 2, 1]}],
                }
            ]
        },
        {
            "features": [
                {"name": "y", "type": "rail-k3-120-z", "rails": 2},
                {"name": "foot"},
            ]
        },
    ],
)
def test_rail_tube_invalid_input_refuses_before_build(
    change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid rail-tube input crossed into rail_tube")

    monkeypatch.setattr("precis_se.atomic.generators.rail120.rail_tube", forbidden)
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


def test_side_out_points_at_the_fin_route() -> None:
    with pytest.raises(GeneratorError, match="fin-k3-120-z"):
        build_hexfold_scene(
            params(pores=None)
            | {
                "features": [
                    {"name": "y", "type": "rail-k3-120-z", "rails": 2, "side": "out"}
                ]
            }
        )


def test_public_rail_tube_tethered_test_db_roundtrip(
    store: Store, hub_no_embedder: Hub
) -> None:
    slug = "hexfold-rail-tube-test"
    p = params(pores=[{"arc": 0, "at": [1, 2]}])
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "y",
        "params": p,
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
    assert record["scene"] == p
    assert (
        record["plan"]["relax"] == "tethered"
        and record["plan"]["pinned_joint_atoms"] == 4 * 2 * 4
    )
    findings = record["report"]["findings"]
    assert not [f for f in findings if f["code"] in ("geom.clash", "geom.seed_overlap")]
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert (
        seam["degree"] == 3
        and seam["angle_deviation_max_deg"] < 1e-9
        and seam["normalized_triple_max"] < 1e-9
    )
    cross = next(f["data"] for f in findings if f["code"] == "tube.cross_section")
    assert len(cross["arcs"]) == 2 and cross["arc_tether"] == "none"
    for arc in cross["arcs"]:
        assert arc["rho_min_A"] <= arc["rho_mean_A"] <= arc["rho_max_A"]
        assert arc["seam_rho_A"] < arc["rho_mean_A"]
    pore = next(f["data"] for f in findings if f["code"] == "pore.rings")
    assert pore["count"] == 1 and pore["rim_sizes"] == [6]
    # the resolved carbon net's open valences are the H caps: converging
    # seam-end rims bond-close first (gr486406), which changes valences, so
    # count the slots on the minted block's carbon graph
    _net, tube = rail_tube(4, 14, 3, 2, pores=[(0, 1, 2)])
    direct = build_hexfold_scene(p)
    n_c = direct.elements.count("C")
    carbon_bonds = [(i, j) for i, j, *_ in direct.bonds if i < n_c and j < n_c]
    block_degrees = np.bincount(
        [v for i, j in carbon_bonds for v in (i, j)], minlength=n_c
    )
    expected_h = int(sum(min(3 - int(d), 2) for d in block_degrees if d < 3))
    assert record["terminated"]["count"] == expected_h
    sites = record["sites"]
    strip_set = {o for strip in tube.strip_atoms for o in strip}
    degrees = np.bincount(
        [v for i, j, _ in _net.bonds for v in (i, j)], minlength=len(_net.atoms)
    )
    expected_sites = sum(1 for o in strip_set if degrees[o] < 3) + 2 * 4
    assert len(sites["sites"]) == expected_sites
    snap = store.structure_positions_snapshot(ref.id)
    assert snap is not None
    relaxed = np.asarray(snap["fractional"]) @ np.asarray(snap["lattice"])
    by_ord = {e["ordinal"]: e for e in sites["sites"]}
    rot = np.asarray(sites["frame"]["to_stored"]["rot"])
    shift = np.asarray(sites["frame"]["to_stored"]["shift_A"])
    # the termination polish (gr486406) may move carbons near a closed rim
    # pair — within 3 hops, matching _TERMINATE_POLISH_HOPS in hexfold_spec —
    # so those sites legitimately sit off the authored lattice position
    # (measured up to 0.18 A here); every other site keeps the tight 0.1 A
    # bound, where the frame fit itself is sub-0.01
    polished = {o for pair in direct.topology["terminated"]["closed"] for o in pair}
    adj: list[set[int]] = [set() for _ in range(n_c)]
    for i, j in carbon_bonds:
        adj[i].add(j)
        adj[j].add(i)
    for _hop in range(3):
        polished |= {x for o in polished for x in adj[o]}
    for entry in sites["sites"]:
        theta = np.radians(entry["theta_deg"])
        xyz = np.array(
            [
                entry["r_A"] * np.cos(theta),
                entry["r_A"] * np.sin(theta),
                entry["z_A"],
            ]
        )
        bound = 0.25 if entry["ordinal"] in polished else 0.1
        assert np.linalg.norm(xyz @ rot + shift - relaxed[entry["ordinal"]]) < bound
        n_hat = np.asarray(entry["normal"])
        assert np.linalg.norm(n_hat) == pytest.approx(1.0, abs=1e-9)
        # normals are inward/toward-axis in the recorded build frame
        assert n_hat @ xyz == pytest.approx(-entry["r_A"], abs=1e-9)
        if entry["region"].startswith("strip"):
            phi = 2.0 * np.pi * entry["seam"] / 2
            assert n_hat @ np.array([-np.cos(phi), -np.sin(phi), 0.0]) > 0.9
    # seam 0 sits on the authored theta-zero meridian, pore or not
    assert all(
        abs(e["theta_deg"]) < 1.0 for e in sites["sites"] if e["region"] == "seam0"
    )
    for a, b in sites["pairs"]:
        d = float(np.linalg.norm(relaxed[a] - relaxed[b]))
        assert 2.5 <= d <= 3.0
        assert by_ord[a]["region"] == by_ord[b]["region"]
        assert by_ord[a]["region"].startswith("strip")
    block = handler.get(id=slug, view="block", args={"name": "y"}).body
    assert "tube.cross_section" in block and "tethered" in block
    unknown = handler.get(id=slug, view="surface_deviation", args={"name": "y"}).body
    assert "unknown: no stored or spec-derivable authored target" in unknown


def test_sites_live_in_the_authored_frame_with_or_without_a_pore() -> None:
    for pores in (None, [{"arc": 0, "at": [1, 2]}]):
        block = build_hexfold_scene(params(pores=pores))
        sites = block.topology["sites"]
        assert "frame" in sites and "to_stored" in sites["frame"]
        for entry in sites["sites"]:
            if entry["region"] == "seam0":
                assert abs(entry["theta_deg"]) < 1.0


def test_public_inward_misfit_refuses_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        type(store),
        "insert_ref",
        lambda *_a, **_k: pytest.fail("misfit rail tube crossed persistence"),
    )
    slug = "hexfold-rail-tube-misfit"
    with pytest.raises(BadInput, match="smallest wall that seats it"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "y",
                        "params": params(10, 6, 3, 4),
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None
