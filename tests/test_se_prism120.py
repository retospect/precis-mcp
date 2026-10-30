"""Prism-tube scene: a closed polygonal tube of flat wall strips on
signed equal-120 seams -- admission, analytic graph, stored site list,
junction strain bookkeeping, real test-DB mint."""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.structure import Atom as StructAtom
from precis.structure import Bond as StructBond
from precis.structure import Scene as StructScene
from precis_se import persist
from precis_se.atomic.generate import generated_cell
from precis_se.atomic.generators import GeneratorError
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.relax_block import relax_block
from precis_se.handler import SeHandler

HEX6: dict[str, Any] = {
    "name": "p",
    "type": "prism-k3-120-z",
    "rows": 6,
    "periods": 8,
    "strip_rows": 4,
    "seams": "++++++",
}
TRI12: dict[str, Any] = {**HEX6, "seams": "+++-+++-+++-"}


def params(feature: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "features": [feature or dict(HEX6)],
        "k_tether": 1.0,
    }
    p.update(over)
    return p


@pytest.mark.parametrize(
    "change",
    [
        {"tube": [6, 8]},  # no tube key: the feature carries the geometry
        {"sheet": [6, 4]},
        {"extra": ""},
        {"k_tether": 0},
        {"features": []},
        {"features": [{"name": "p", "type": "prism-k3-120-z"}]},
        {"features": [dict(HEX6, n=6)]},
        {"features": [dict(HEX6, type="rail-k3-120-z")]},
        {"features": [dict(HEX6, rows=5)]},
        {"features": [dict(HEX6, rows=2)]},
        {"features": [dict(HEX6, periods=61)]},
        {"features": [dict(HEX6, strip_rows=13)]},
        {"features": [dict(HEX6, seams=6)]},
        {"features": [dict(HEX6, seams="++")]},
        {"features": [dict(HEX6, seams="+" * 25)]},
        {"features": [dict(HEX6, seams="++x+++")]},
        {"features": [dict(HEX6, pores="x")]},
        {"features": [dict(HEX6, pores=[[0, 1]])]},
        {"features": [dict(HEX6, pores=[[0.5, 1, 2]])]},
        {"features": [dict(HEX6), {"name": "q"}]},
    ],
)
def test_prism_invalid_input_refuses_before_build(
    change: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("invalid prism input crossed into prism_tube")

    monkeypatch.setattr("precis_se.atomic.generators.prism120.prism_tube", forbidden)
    with pytest.raises(GeneratorError):
        build_hexfold_scene({**params(), **change})


def test_odd_rows_refusal_carries_code_and_fix() -> None:
    """gr503030: the odd-row refusal names a branchable code and the fix."""
    with pytest.raises(GeneratorError, match=r"\[prism\.rows\.odd\]") as exc:
        build_hexfold_scene(params(feature=dict(HEX6, rows=5)))
    assert exc.value.code == "prism.rows.odd"
    assert exc.value.fix == "rows is odd — nearest legal: rows=4 or rows=6"


def test_odd_rows_refusal_surfaces_next(store: Store, hub_no_embedder: Hub) -> None:
    """The same refusal crossing the se handler keeps the fix in next=."""
    with pytest.raises(BadInput, match=r"\[prism\.rows\.odd\]") as exc:
        SeHandler(hub=hub_no_embedder).put(
            id="hexfold-prism-odd",
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "p",
                        "params": params(feature=dict(HEX6, rows=5)),
                    }
                ]
            },
        )
    assert exc.value.next == "rows is odd — nearest legal: rows=4 or rows=6"
    assert store.get_ref(kind="se", id="hexfold-prism-odd") is None


def test_net_turn_and_open_polygon_refused_by_name() -> None:
    with pytest.raises(GeneratorError, match=r"prism\.closure: net turn 180°"):
        build_hexfold_scene(params(feature=dict(HEX6, seams="++-++-++-")))
    with pytest.raises(GeneratorError, match=r"prism\.closure: open polygon"):
        build_hexfold_scene(params(feature=dict(HEX6, seams="+++++++-")))
    with pytest.raises(GeneratorError, match=r"prism\.closure: self-intersecting"):
        build_hexfold_scene(params(feature=dict(HEX6, seams="+++++--+++++--")))


def test_pore_positions_refused_by_name() -> None:
    # well-formed cells whose position is illegal land in prism_tube
    with pytest.raises(GeneratorError, match="pore wall"):
        build_hexfold_scene(params(feature=dict(HEX6, pores=[[6, 1, 2]])))
    with pytest.raises(GeneratorError, match="seam rim"):
        build_hexfold_scene(params(feature=dict(HEX6, pores=[[0, 0, 0]])))


def test_rib_clearance_refused_by_name() -> None:
    with pytest.raises(GeneratorError, match=r"prism\.rib\.clearance"):
        build_hexfold_scene(
            params(
                feature=dict(HEX6, rows=4, periods=4, strip_rows=2, seams="++++-++++-")
            )
        )


def test_u2212_seams_parse_and_echo() -> None:
    block = build_hexfold_scene(
        params(feature=dict(HEX6, periods=4, seams="+++−+++−+++−"))
    )
    assert block.topology["scene"]["features"][0]["seams"] == "+++-+++-+++-"
    signs = [j["name"].rsplit(":", 1)[1] for j in block.topology["junctions"]]
    assert signs.count("-") == 3


def test_hex6_block_reports() -> None:
    block = build_hexfold_scene(params())
    findings = block.topology["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"]
    seam = next(f["data"] for f in findings if f["code"] == "seam.geometry")
    assert (
        seam["degree"] == 3
        and seam["signs"] == ["+"] * 6
        and seam["angle_deviation_max_deg"] < 1e-6
        and seam["normalized_triple_max"] < 1e-6
    )
    cross = next(f["data"] for f in findings if f["code"] == "prism.cross_section")
    assert len(cross["walls"]) == 6 and cross["wall_tether"] == "none"
    for w in cross["walls"]:
        assert w["off_plane_max_A"] < 0.05  # flat walls stay flat
    # H3 bookkeeping: one junction per seam, tagged with its sign
    junctions = block.topology["junctions"]
    assert [j["name"] for j in junctions] == [f"seam:{k}:+" for k in range(6)]
    n_at = len(block.coords)
    for j in junctions:
        assert j["atoms"] and j["fused"] and j["faces"]
        assert all(0 <= a < n_at for a in j["atoms"])
        assert all(0 <= a < n_at and 0 <= b < n_at for a, b in j["fused"])
    assert len(block.topology["seam_atoms"]) == 6
    assert block.topology["sigma_A"] == pytest.approx(1.42)


def test_tri12_block_reports() -> None:
    block = build_hexfold_scene(params(feature=TRI12))
    findings = block.topology["report"]["findings"]
    assert not [f for f in findings if f["severity"] == "ERROR"]
    junctions = block.topology["junctions"]
    names = [j["name"] for j in junctions]
    assert names[3::4] == ["seam:3:-", "seam:7:-", "seam:11:-"]
    sites = block.topology["sites"]["sites"]
    signs = {e["ordinal"]: e["sign"] for e in sites if "sign" in e}
    assert set(signs.values()) == {"+", "-"}
    # the rib strips (seams 3, 7, 11) point at the axis: every open-valence
    # rib site sits inside its seam line, and its inward normal meets the
    # radial at better than ~25 deg
    seam_r = {e["seam"]: e["r_A"] for e in sites if e["region"].startswith("seam")}
    for k in (3, 7, 11):
        rib = [e for e in sites if e["region"] == f"strip{k}"]
        assert rib
        for e in rib:
            assert e["r_A"] < seam_r[k]
            theta = np.radians(e["theta_deg"])
            radial = np.array([np.cos(theta), np.sin(theta), 0.0])
            assert np.asarray(e["normal"]) @ radial < -0.9


def test_sites_round_trip_and_seam_sign() -> None:
    from hexfold.prism import prism_tube

    block = build_hexfold_scene(params())
    _net, _tube = prism_tube(8, 6, 4, "++++++")
    sites = block.topology["sites"]
    assert "frame" in sites and "to_stored" in sites["frame"]
    rot = np.asarray(sites["frame"]["to_stored"]["rot"])
    shift = np.asarray(sites["frame"]["to_stored"]["shift_A"])
    coords = np.asarray(block.coords)
    by_ord = {e["ordinal"]: e for e in sites["sites"]}
    for entry in sites["sites"]:
        theta = np.radians(entry["theta_deg"])
        xyz = np.array(
            [
                entry["r_A"] * np.cos(theta),
                entry["r_A"] * np.sin(theta),
                entry["z_A"],
            ]
        )
        stored = coords[entry["ordinal"]]
        assert np.linalg.norm(xyz @ rot + shift - stored) < 0.1
        n_hat = np.asarray(entry["normal"])
        assert np.linalg.norm(n_hat) == pytest.approx(1.0, abs=1e-9)
        assert n_hat @ xyz == pytest.approx(-entry["r_A"], abs=1e-9)
        if entry["region"].startswith("seam"):
            assert entry["sign"] == "+"
    assert all(
        abs(e["theta_deg"]) < 1.0 for e in sites["sites"] if e["region"] == "seam0"
    )
    for a, b in sites["pairs"]:
        d = float(np.linalg.norm(coords[a] - coords[b]))
        assert 2.5 <= d <= 3.0
        assert by_ord[a]["region"] == by_ord[b]["region"]
        assert by_ord[a]["region"].startswith("strip")


def test_public_prism_test_db_roundtrip(store: Store, hub_no_embedder: Hub) -> None:
    slug = "hexfold-prism-test"
    p = params(feature=dict(HEX6, pores=[[0, 1, 2]]))
    op = {
        "op": "generate",
        "generator": "hexfold_scene",
        "name": "p",
        "params": p,
    }
    handler = SeHandler(hub=hub_no_embedder)
    handler.put(id=slug, args={"ops": [op]})
    se_ref = store.get_ref(kind="se", id=slug)
    assert se_ref is not None
    node = persist.load_tree(store, se_ref.id).blocks["p"]
    assert node.bound is not None
    ref = store.get_ref(kind="structure", id=node.bound)
    assert ref is not None
    record = (ref.meta or {})["generated"]
    assert record["scene"] == {
        "features": [
            {
                "name": "p",
                "type": "prism-k3-120-z",
                "rows": 6,
                "periods": 8,
                "strip_rows": 4,
                "seams": "++++++",
                "pores": [[0, 1, 2]],
            }
        ],
        "k_tether": 1.0,
    }
    assert record["plan"]["relax"] == "tethered"
    pore = next(
        f["data"] for f in record["report"]["findings"] if f["code"] == "pore.rings"
    )
    assert pore["count"] == 1 and pore["rim_sizes"] == [6]
    assert [j["name"] for j in record["junctions"]] == [f"seam:{k}:+" for k in range(6)]


def test_public_closure_refusal_without_persistence(
    store: Store, hub_no_embedder: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        type(store),
        "insert_ref",
        lambda *_a, **_k: pytest.fail("non-closing prism crossed persistence"),
    )
    slug = "hexfold-prism-open"
    with pytest.raises(BadInput, match="net turn"):
        SeHandler(hub=hub_no_embedder).put(
            id=slug,
            args={
                "ops": [
                    {
                        "op": "generate",
                        "generator": "hexfold_scene",
                        "name": "p",
                        "params": params(feature=dict(HEX6, seams="++-++-++-")),
                    }
                ]
            },
        )
    assert store.get_ref(kind="se", id=slug) is None


def _scene_of(block: Any) -> StructScene:
    scene = StructScene(cell=generated_cell(block.coords))
    labels: list[str] = []
    for i, (element, cart) in enumerate(zip(block.elements, block.coords, strict=True)):
        label = scene.next_label(element)
        scene.atoms[label] = StructAtom(
            label=label,
            element=element,
            frac=scene.cell.wrap(
                scene.cell.cart_to_frac(np.asarray(cart, dtype=float))
            ),
            hybridization=(block.hybridizations[i] if block.hybridizations else None),
        )
        labels.append(label)
    for i, j, order, kind in block.bonds:
        scene.bonds.append(StructBond(i=labels[i], j=labels[j], order=order, kind=kind))
    return scene


def _stub_xtb_with_emt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``xtb`` rung's calculator replaced by ASE-EMT -- same relax
    path, no tblite needed (the wheel is absent on the Mac)."""
    from ase.calculators.emt import EMT

    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", lambda **kw: EMT())  # ModuleType has no attr
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)


@pytest.mark.slow
def test_relax_block_reports_junction_strain_per_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EMT polish + stubbed-xtb junction fragments on a short hexagon:
    ``junction.strain`` re-scores once per seam, named by the minted
    ``seam:<i>:<sign>`` records, before and after."""
    _stub_xtb_with_emt(monkeypatch)
    block = build_hexfold_scene(params(feature=dict(HEX6, periods=2)))
    scene = _scene_of(block)
    report = relax_block(
        scene,
        block.topology,
        polish="emt",
        fragment="xtb",
        buffer_A=5.0,
        core_A=2.0,
        steps=30,
    )
    assert report.polish.rung == "emt"
    assert [f.name for f in report.fragments] == [f"seam:{k}:+" for k in range(6)]
    for side in (report.before, report.after):
        strain = [f for f in side if f["code"] == "junction.strain"]
        assert {f["where"] for f in strain} == {f"seam:{k}:+" for k in range(6)}
        assert {f["code"] for f in side} >= {"junction.strain", "seam.strain"}
