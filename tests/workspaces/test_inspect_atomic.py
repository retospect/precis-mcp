"""Molecular-workspace-code slice 6 acceptance — one inspect family at Å.

- the field-provider seam leaves ``cad.inspect`` untouched (its own tests,
  ``tests/test_cad_inspect.py``, are the golden) and the cad provider is
  the same marcher the atomic one plugs into;
- on a (10, 10) tube the axis profile minimum equals tube radius − C vdW
  within 0.05 Å and names the wall frame (the atom when it has no maker);
- a C60 in a too-narrow tube reports ``overlap.volume_m3 > 0`` naming both
  frames; in a wide one, none;
- a benzene ring-plane ``section`` is one loop (the ring centre is inside
  the carbon vdW union, 1.39 Å < 1.70 Å — no hole); shrink the radii and
  the same code reports ``hole_count`` 1; a tube cross-section has the
  outer and inner loop, ``hole_count`` 1;
- ``view='inspect'`` ``probe='section'`` renders ≤ 400 tokens on both
  kinds, and refuses with ``next=``/``options=``.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

import precis.cad.inspect as ci
from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.inspect import (
    _loop_distance,
    cad_field,
    ray,
    ray_field,
    thickness,
)
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.store import Store
from precis.structure import inspectview as iv
from precis.structure import probe as st_probe
from precis.structure.cell import Cell
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis.utils.llm.admit import estimate_tokens
from precis_geom import assembly as pga
from precis_geom import inspect as pgi
from precis_geom import trace
from precis_se.atomic.generators import GENERATORS
from precis_se.handler import SeHandler

A = 1e-10  # Å in metres


@trace.traced(name="wall")
def _wall(n: int, length_A: float = 20.0) -> pga.Geom:
    """A (n, n) nanotube whose atoms cite the ``wall`` frame."""
    b = GENERATORS["cnt"]({"n": n, "m": n, "length_A": length_A})
    bonds = [(i, j, float(o)) for i, j, o, _k in b.bonds]
    scene = pga._scene_from_parts(
        list(b.elements), np.asarray(b.coords), bonds, maker=trace.leaf_id()
    )
    return pga.geom(scene)


@trace.traced(name="guest")
def _guest() -> pga.Geom:
    """A C60 centred at the origin whose atoms cite the ``guest`` frame."""
    b = GENERATORS["fullerene"]({"atoms": 60})
    carts = np.asarray(b.coords) - np.asarray(b.coords).mean(axis=0)
    bonds = [(i, j, float(o)) for i, j, o, _k in b.bonds]
    return pga.geom(
        pga._scene_from_parts(list(b.elements), carts, bonds, maker=trace.leaf_id())
    )


def _frame_key(doc: dict, qual: str) -> str:
    keys = [f["key"] for f in doc["frames"] if f["qual"] == qual]
    assert len(keys) == 1, (qual, keys)
    return keys[0]


def _centred(g: pga.Geom) -> np.ndarray:
    carts = np.asarray(
        [g.scene.cell.frac_to_cart(a.frac) for a in g.scene.atoms.values()]
    )
    return carts.mean(axis=0)


# ── bottleneck ───────────────────────────────────────────────────────────


def test_tube_axis_profile_minimum_is_radius_minus_vdw_and_names_the_wall() -> None:
    with trace.run() as tr:
        tube = _wall(10)
    doc = tr.dump()
    wall = _frame_key(doc, "wall")
    c = _centred(tube)
    carts = np.asarray(
        [tube.scene.cell.frac_to_cart(a.frac) for a in tube.scene.atoms.values()]
    )
    radius = float(np.linalg.norm((carts - c)[:, :2], axis=1).mean())
    expect = radius - st_probe.VDW_RADII_A["C"]

    res = pgi.ray(
        tube,
        origin=tuple((c + np.array([0, 0, -3.0])) * A),
        axis=(0, 0, 1),
        t_max=27e-10,
        profile_samples=271,
        events=(),
        frames=doc,
    )
    assert res.pinch_m is not None
    assert abs(res.pinch_m / A - expect) < 0.05, (res.pinch_m / A, expect)
    assert [f.label for f in res.pinch_features] == [wall]
    assert all(f.role == "owner" for f in res.pinch_features)
    # the free radius is the whole profile's floor, in metres
    assert res.profile is not None
    assert min(v for _t, v in res.profile) == res.pinch_m


def test_unmade_atoms_are_labelled_by_the_atom() -> None:
    b = GENERATORS["cnt"]({"n": 6, "m": 6, "length_A": 8.0})
    scene = pga._scene_from_parts(
        list(b.elements),
        np.asarray(b.coords),
        [(i, j, float(o)) for i, j, o, _k in b.bonds],
    )
    c = _centred(pga.geom(scene))
    res = pgi.ray(
        scene,
        origin=tuple((c + np.array([0, 0, -2.0])) * A),
        axis=(0, 0, 1),
        t_max=12e-10,
        profile_samples=61,
        events=(),
    )
    (ref,) = res.pinch_features
    assert ref.label in scene.atoms and ref.role == "owner"


def test_beam_radius_sweeps_a_cylinder_through_the_channel() -> None:
    tube = _wall(10)
    c = _centred(tube)
    free = 6.784 - 1.7
    common: dict[str, Any] = dict(
        origin=tuple((c + np.array([0, 0, -3.0])) * A),
        axis=(0, 0, 1),
        t_max=27e-10,
        events=(),
    )
    narrow = pgi.ray(tube, radius=(free - 1.0) * A, **common)
    wide = pgi.ray(tube, radius=(free + 0.5) * A, **common)
    assert narrow.swept is not None and not narrow.swept.hit
    assert narrow.swept.volume_m3 == 0.0
    assert wide.swept is not None and wide.swept.hit
    assert wide.swept.volume_m3 > 0.0
    assert wide.radius_m == pytest.approx((free + 0.5) * A)


# ── overlap ──────────────────────────────────────────────────────────────


def test_c60_in_a_too_narrow_tube_overlaps_and_names_both_frames() -> None:
    with trace.run() as tr:
        tube = _wall(8)  # radius 5.4 Å: free 3.7 Å < C60's 5.25 Å vdW radius
        guest = _guest()
    doc = tr.dump()
    wall, g = _frame_key(doc, "wall"), _frame_key(doc, "guest")
    shift = _centred(tube) - _centred(guest)
    moved = pga.translate(guest, tuple(shift * A))
    res = pgi.overlap(tube, moved, frames=doc, probe_frames=doc)
    assert res.hit and res.volume_m3 > 0.0
    assert {f.label for f in res.features} >= {wall, g}
    assert res.aabb_m[0][2] < res.aabb_m[1][2]


def test_c60_in_a_wide_tube_does_not_touch_the_wall() -> None:
    with trace.run() as tr:
        tube = _wall(12)  # radius 8.1 Å: free 6.4 Å > 5.25 Å
        guest = _guest()
    shift = _centred(tube) - _centred(guest)
    res = pgi.overlap(tube, pga.translate(guest, tuple(shift * A)), frames=tr.dump())
    assert not res.hit and res.volume_m3 == 0.0 and res.features == ()


# ── section ──────────────────────────────────────────────────────────────


def _benzene() -> Scene:
    scene = Scene(Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)))
    apply_ops(scene, [{"op": "from_smiles", "smiles": "c1ccccc1"}])
    return scene


def _ring_plane(scene: Scene) -> tuple[np.ndarray, np.ndarray]:
    carts = np.asarray(
        [
            scene.cell.frac_to_cart(a.frac)
            for a in scene.atoms.values()
            if a.element == "C"
        ]
    )
    c = carts.mean(axis=0)
    return c, np.linalg.svd(carts - c)[2][2]


def test_benzene_ring_plane_section_is_one_loop() -> None:
    scene = _benzene()
    c, n = _ring_plane(scene)
    res = pgi.section(scene, origin=tuple(c * A), normal=tuple(n), pitch=0.04e-10)
    assert len(res.loops) == 1
    # the ring centre is 1.39 Å from each C — inside the 1.70 Å vdW union
    assert res.hole_count == 0
    assert res.loops[0].area_m2 > 0.0
    # the carbon rim: outer diameter ≈ 2 × (1.39 + 1.70) ≈ 6.2 Å
    pts = np.asarray(res.loops[0].points_m) / A
    assert 5.5 < np.ptp(pts, axis=0).max() < 8.0
    assert res.crossings == ()  # a planar ring: no bond crosses its own plane


def test_benzene_with_shrunk_radii_has_a_ring_hole() -> None:
    scene = _benzene()
    c, n = _ring_plane(scene)
    res = iv.atomic_section(
        scene, origin=c, normal=n, pitch=0.04, radii={"C": 0.9, "H": 0.5}
    )
    assert len(res.loops) == 2 and res.hole_count == 1
    assert sorted(lp.area_m2 > 0 for lp in res.loops) == [False, True]


def test_tube_cross_section_is_outer_and_inner_loop() -> None:
    tube = _wall(10)
    c = _centred(tube)
    res = pgi.section(tube, origin=tuple(c * A), normal=(0, 0, 1), pitch=0.05e-10)
    # the outer rim and the free circle; the plane also clips a few
    # sub-Å cusps between near-tangent atom discs (dust, not walls)
    big = sorted(
        (lp for lp in res.loops if lp.length_m > 5e-10), key=lambda lp: lp.length_m
    )
    assert len(big) == 2, [lp.length_m / A for lp in res.loops]
    inner, outer = big
    assert inner.area_m2 < 0.0 < outer.area_m2
    assert res.hole_count >= 1
    # the free circle is radius − C vdW, the rim radius + C vdW; both
    # boundaries scallop between the discs a plane clips off the spheres,
    # so the lengths run a few percent over the ideal circles
    ideal_in, ideal_out = (2 * np.pi * (6.784 - s) for s in (1.7, -1.7))
    assert ideal_in <= inner.length_m / A < 1.1 * ideal_in
    assert ideal_out * 0.98 <= outer.length_m / A < 1.1 * ideal_out
    # wall = rim to free circle: at most 2 × C vdW (a plane clips most
    # spheres off-centre, so the discs — and the wall — are thinner)
    assert 2.5 < _loop_distance(inner, outer) / A <= 2 * 1.7 + 0.05


def test_longitudinal_section_lists_the_bonds_that_cross_the_plane() -> None:
    tube = _wall(6)
    c = _centred(tube)
    res = pgi.section(tube, origin=tuple(c * A), normal=(1, 0, 0), pitch=0.1e-10)
    assert res.crossings  # a plane through the axis cuts C–C bonds
    assert all(0.0 <= x.angle_deg <= 90.0 and x.length_m > 0 for x in res.crossings)


# ── atomic events + thickness ────────────────────────────────────────────


def test_atomic_events_merge_beside_the_runs() -> None:
    scene = _benzene()
    c, n = _ring_plane(scene)
    res = pgi.ray(
        scene,
        origin=tuple((c - 4.0 * n) * A),
        axis=tuple(n),
        t_max=8e-10,
        events=("pi",),
    )
    kinds = [(e.field, e.event) for e in res.events]
    assert kinds == [("pi", "enter"), ("pi", "leave")]
    # the π cylinder is the ring plane ± 1.7 Å, s in metres
    assert res.events[0].s / A == pytest.approx(4.0 - 1.7, abs=0.05)
    assert res.hits  # the vdW material run is there too
    assert pgi.ray(scene, origin=(0, 0, 0), axis=(0, 0, 1), events=()).events == ()


def test_thickness_map_reads_vdw_columns() -> None:
    scene = _benzene()
    c, n = _ring_plane(scene)
    res = pgi.thickness(
        scene, origin=tuple((c + 4.0 * n) * A), normal=tuple(n), pitch=0.5e-10
    )
    assert res.samples
    # viewed face-on a carbon is a 2 × 1.70 Å column; the 0.5 Å grid
    # lands within ~0.4 Å of an atom centre, so the thickest column is
    # most of that, never more
    top = max(s.thickness_m for s in res.samples) / A
    assert 2.5 < top <= 3.4 + 1e-6, top


# ── the seam: cad provider == the public cad probes ──────────────────────


def test_cad_provider_is_the_public_cad_ray() -> None:
    design = Design()
    expr = design.prim("plate", build_config("box:w20d20h10"))
    kw: dict[str, Any] = dict(
        origin_m=(0.0, 0.0, -5.0), axis_m=(0.0, 0.0, 1.0), profile_samples=9
    )
    a = ray(design, expr, **kw)
    b = ray_field(cad_field(design, expr), **kw)
    assert a == b
    assert a.hits and a.pinch_m is not None
    assert [f.label for f in a.hits[0].entry_features] == ["plate"]


# ── view='inspect' ───────────────────────────────────────────────────────


def _put_structure(store: Store, slug: str, ops: list[dict]) -> StructureHandler:
    sh = StructureHandler(hub=Hub(store=store))
    sh.put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 30, "b": 30, "c": 30, "pbc": [False, False, False]},
                "ops": ops,
            }
        ),
    )
    return sh


def test_structure_view_inspect_section_is_compact(store: Store) -> None:
    sh = _put_structure(store, "bz-insp", [{"op": "from_smiles", "smiles": "c1ccccc1"}])
    ref = store.get_ref(kind="structure", id="bz-insp")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    c, n = _ring_plane(scene)
    body = sh.get(
        id="bz-insp",
        view="inspect",
        args={"probe": "section", "origin": c.tolist(), "normal": n.tolist()},
    ).body
    assert estimate_tokens(len(body)) <= 400, estimate_tokens(len(body))
    assert body.startswith("inspect section")
    assert "loops 1 · holes 0" in body, body
    # computed on demand: the design's measures are untouched
    scene2, _h2 = store.structure_load(ref.id)
    assert not scene2.measures

    ray_body = sh.get(
        id="bz-insp",
        view="inspect",
        args={"probe": "ray", "origin": (c - 4.0 * n).tolist(), "axis": n.tolist()},
    ).body
    assert "pi.enter" in ray_body and "pi.leave" in ray_body, ray_body


def test_structure_view_inspect_refuses_with_next_and_options(store: Store) -> None:
    sh = _put_structure(
        store, "bz-insp2", [{"op": "from_smiles", "smiles": "c1ccccc1"}]
    )
    with pytest.raises(BadInput) as e:
        sh.get(id="bz-insp2", view="inspect", args={"probe": "slice"})
    assert e.value.options == list(iv.PROBES) and e.value.next
    with pytest.raises(BadInput) as e2:
        sh.get(id="bz-insp2", view="inspect", args={"probe": "overlap"})
    assert "second solid" in str(e2.value) and e2.value.next
    with pytest.raises(BadInput) as e3:
        sh.get(
            id="bz-insp2",
            view="inspect",
            args={"probe": "section", "target": "frame:abc"},
        )
    assert "trace" in str(e3.value) and e3.value.next


def test_se_view_inspect_on_a_bound_block(store: Store) -> None:
    _put_structure(store, "bz-se", [{"op": "from_smiles", "smiles": "c1ccccc1"}])
    ref = store.get_ref(kind="structure", id="bz-se")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    c, n = _ring_plane(scene)
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id="insp-se",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "b", "envelope": "sphere:r2e-9"},
                    {"op": "bind_structure", "block": "b", "design": "bz-se"},
                ]
            }
        ),
    )
    body = se.get(
        id="insp-se",
        view="inspect",
        args={
            "block": "b",
            "probe": "section",
            "origin": (c * A).tolist(),  # metres, like view='ray'
            "normal": n.tolist(),
        },
    ).body
    assert estimate_tokens(len(body)) <= 400
    assert "loops 1 · holes 0" in body and "block b" in body, body
    # a macro block: the envelope's own section, in mm
    se.edit(
        id="insp-se",
        ops=[{"op": "add_block", "name": "m", "envelope": "box:w0.01d0.01h0.01"}],
    )
    mbody = se.get(
        id="insp-se",
        view="inspect",
        args={
            "block": "m",
            "probe": "section",
            "origin": [0, 0, 0.005],
            "normal": [0, 0, 1],
        },
    ).body
    assert "loops 1" in mbody and "mm" in mbody, mbody
    with pytest.raises(BadInput) as e:
        se.get(id="insp-se", view="inspect", args={"block": "b"})
    assert e.value.options == list(iv.PROBES)
    with pytest.raises(BadInput) as e2:
        se.get(id="insp-se", view="inspect", args={"probe": "ray"})
    assert "block" in str(e2.value) and e2.value.next


def test_view_inspect_ray_stall_renders_not_a_clean_miss(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A march that spent its step budget used to read as a clean miss —
    the shared render names the bail on both kinds' ``view='inspect'``.
    A 1-step budget forces the stall (``parse_spec`` reads the constant
    live, so the monkeypatch reaches both legs)."""
    monkeypatch.setattr(ci, "_MAX_RAY_STEPS", 1)
    sh = _put_structure(
        store, "bz-stall", [{"op": "from_smiles", "smiles": "c1ccccc1"}]
    )
    ref = store.get_ref(kind="structure", id="bz-stall")
    assert ref is not None
    scene, _h = store.structure_load(ref.id)
    c, n = _ring_plane(scene)
    body = sh.get(
        id="bz-stall",
        view="inspect",
        args={
            "probe": "ray",
            "origin": (c - 4.0 * n).tolist(),
            "axis": n.tolist(),
        },
    ).body
    assert "stalled at step budget" in body, body
    # the se kind's macro block view — the same shared render, in mm
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id="stall-se",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "m",
                        "envelope": "box:w0.01d0.01h0.01",
                    }
                ]
            }
        ),
    )
    mbody = se.get(
        id="stall-se",
        view="inspect",
        args={
            "block": "m",
            "probe": "ray",
            "origin": [0, 0, -0.02],
            "axis": [0, 0, 1],
            "length": 0.05,
        },
    ).body
    assert "stalled at step budget" in mbody, mbody


def test_thickness_render_reports_nudged_and_refused_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall retry's bookkeeping renders beside the column count —
    ``N nudged, M refused`` plus the refused nodes' coordinates, so a
    node dropped by an unrecoverable stall is never silent."""
    d = Design()
    wall = d.prim("wall", build_config("box:w50d98h10"))
    kw: dict[str, Any] = {
        "origin_m": (0, 0, 10),
        "normal_m": (0, 0, 1),
        "pitch_m": 4.0,
    }
    ok = thickness(d, wall, **kw)
    assert ok.nudged_m and ok.refused_m == ()
    body = iv.render("thickness", ok, head="wall")
    assert f"{len(ok.nudged_m)} nudged, 0 refused" in body, body
    # a one-step retry budget leaves the stall unrecovered → refused
    monkeypatch.setattr(ci, "_STALL_RETRY_MAX_STEPS", 1)
    t = thickness(d, wall, **kw)
    assert t.refused_m and t.nudged_m == ()
    body = iv.render("thickness", t, head="wall")
    assert f"0 nudged, {len(t.refused_m)} refused" in body, body
    assert "refused nodes" in body and iv._v(t.refused_m[0]) in body, body
    # the coordinate list caps at four with a "+N more" overflow
    many = ci.ThicknessInspection(
        plane_origin_m=(0.0, 0.0, 0.0),
        plane_normal_m=(0.0, 0.0, 1.0),
        frame_u_m=(0.0, 1.0, 0.0),
        frame_v_m=(-1.0, 0.0, 0.0),
        pitch_m=1.0,
        nominal_m=None,
        ratio=1.5,
        samples=(),
        refused_m=tuple((float(i), 0.0, 0.0) for i in range(6)),
        nudged_m=((9.0, 0.0, 0.0),),
    )
    body = iv.render("thickness", many, head="x")
    assert "1 nudged, 6 refused" in body and "+2 more" in body, body
