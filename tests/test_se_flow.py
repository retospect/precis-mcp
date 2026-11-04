"""precis_se.flow — the se adapter of docs/backlog/se-flow-kernel.md
slice 2: cad envelope → Domain (thin axis, depth map, patch: ports),
view='flow' payload, the ``flow:`` chamber-spec pocket and the four
flow COMPUTERS.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.primitives import CircularFrustum, Placed, PolyFrustum
from precis.cad.vec import identity, vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis_flow import Domain, Ports, Seed, rasterize_holes, solve, trace
from precis_flow.stats import nonuniformity  # noqa: F401  (parity anchor)
from precis_geom import out as geom_out
from precis_geom.physics import evaluate
from precis_se import datums as se_datums
from precis_se import persist, workspace_import
from precis_se.flow import (
    FLOW_COMPUTERS,
    PAYLOAD_CAP_BYTES,
    Chamber,
    FlowSpec,
    PortPatch,
    domain_from_solid,
    flow_payload,
    flow_pockets,
    parse_flow_shape,
    patch_port,
    render_flow,
)
from precis_se.measures import MeasureError, MeasureSpec
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.workspace_import import import_workspace_run
from tests._flow_hex import seed_flow_measurands
from tests.workspaces import flow_chamber


@pytest.fixture
def store(store: Any) -> Any:
    seed_flow_measurands()
    return store


# cfdtest geometry (metres): L × W hexagon, tip end-caps, gap H.
L, W, TIP, H = 0.1, 0.03, 0.015, 1e-3
HOLE_R = 2.5e-3
PORT_W = 0.006
# cfdtest's mix_coeff 0.06 made dimensionless at L/h0 = 100 → D_v* = 6.
D_V_STAR = 0.06 * (L / H)
HOLES = np.array(
    [
        [0.030, 0.015, 2.5e-3],
        [0.045, 0.008, 2.0e-3],
        [0.045, 0.022, 2.0e-3],
        [0.060, 0.015, 3.0e-3],
        [0.075, 0.009, 1.5e-3],
        [0.075, 0.021, 1.5e-3],
    ]
)


def _hex_design() -> tuple[Design, Expr]:
    """The cfdtest chamber void as a cad solid: the elongated-hex prism
    (ring in metres) union the deeper hole cylinders."""
    ring = [
        (TIP, 0.0),
        (L - TIP, 0.0),
        (L, W / 2),
        (L - TIP, W),
        (TIP, W),
        (0.0, W / 2),
    ]
    d = Design()
    parts = [d.prim("chamber", PolyFrustum(ring, ring, H))]
    for i, (hx, hy, hd) in enumerate(HOLES):
        parts.append(
            d.prim(
                f"hole{i}",
                CircularFrustum(HOLE_R, HOLE_R, hd + 5e-4),
                cad_pose(vec3(hx, hy, -hd), vec3(0, 0, 0)),
            )
        )
    return d, d.merge(*parts)


def _tip_patch(x: float, sign: float) -> PortPatch:
    """A port rectangle standing on the chamber's tip (normal ±x)."""
    return PortPatch(
        point=np.array([x, W / 2, H / 2]),
        normal=np.array([sign, 0.0, 0.0]),
        u=np.array([0.0, 1.0, 0.0]),
        v=np.array([0.0, 0.0, 1.0]),
        half_u=PORT_W / 2,
        half_v=H / 2,
    )


def _hex_domain(nx: int = 200) -> Chamber:
    design, expr = _hex_design()
    return domain_from_solid(
        (design, expr), _tip_patch(0.0, -1.0), _tip_patch(L, 1.0), nx=nx
    )


def _analytic_domain(nx: int = 200) -> Domain:
    """The cfdtest-style domain the adapter should reproduce: hexagon
    cell-centre mask, rasterised holes, tip-band port columns."""
    ny = round(nx * W / L)
    dx = 1.0 / nx
    xc = (np.arange(nx) + 0.5) * dx * L
    yc = (np.arange(ny) + 0.5) * dx * L
    X, Y = np.meshgrid(xc, yc)
    half = W / 2
    inside = (X >= 0) & (X <= L) & (Y >= 0) & (Y <= W)
    left = X < TIP
    inside &= ~left | (np.abs(Y - half) <= half * (X / TIP))
    right = X > L - TIP
    inside &= ~right | (np.abs(Y - half) <= half * ((L - X) / TIP))
    holes_star = np.column_stack([HOLES[:, 0] / L, HOLES[:, 1] / L, HOLES[:, 2] / H])
    depth = rasterize_holes(inside, holes_star, radius_star=HOLE_R / L)
    band = np.abs(Y - half) <= PORT_W / 2
    inlet = inside & band & (xc[0] + dx * L >= X)
    outlet = inside & band & (xc[-1] - dx * L <= X)
    return Domain(
        mask=inside,
        h_star=1.0 + depth,
        ports=Ports(inlet=inlet, outlet=outlet),
        z_plane_star=0.99,
    )


def test_hex_chamber_reproduces_cfdtest_plug() -> None:
    """The cad-envelope domain, run with profile='plug', reproduces
    cfdtest's breakthrough fraction within its own seed scatter."""
    chamber = _hex_domain()
    ref = _analytic_domain()
    # The footprint and depth map agree with the analytic rasterisation.
    both = chamber.domain.mask & ref.mask
    union = chamber.domain.mask | ref.mask
    assert both.sum() / union.sum() > 0.95
    assert np.allclose(chamber.domain.h_star[both], ref.h_star[both], atol=0.05)
    bt_a = np.asarray(
        [
            trace(
                solve(chamber.domain),
                Seed.across_inlet(chamber.domain),
                400,
                D_V_STAR,
                s,
                profile="plug",
            ).breakthrough
            for s in range(6)
        ]
    )
    bt_r = np.asarray(
        [
            trace(
                solve(ref), Seed.across_inlet(ref), 400, D_V_STAR, s, profile="plug"
            ).breakthrough
            for s in range(6)
        ]
    )
    # breakthrough must be earned by the holes (non-trivial) and the two
    # geometries must agree within the seed-to-seed scatter.
    assert bt_a.mean() > 0.02
    sem = max(bt_a.std(), bt_r.std(), 1e-9) / math.sqrt(len(bt_a))
    assert abs(bt_a.mean() - bt_r.mean()) <= max(3.0 * sem, 0.04)


def test_parabolic_wider_than_plug() -> None:
    """Q4 direction: the parabolic gap shear spreads the tracer pulse —
    a wider residence spread than plug on the same chamber."""
    chamber = _hex_domain()
    seed = Seed.across_inlet(chamber.domain)
    plug = trace(
        solve(chamber.domain), seed, 400, D_V_STAR, 3, profile="plug"
    ).residence
    para = trace(
        solve(chamber.domain), seed, 400, D_V_STAR, 3, profile="parabolic"
    ).residence
    s_plug = np.nanstd(plug[np.isfinite(plug)])
    s_para = np.nanstd(para[np.isfinite(para)])
    assert s_para > s_plug


def _box_tree() -> SeTree:
    tree = SeTree()
    apply_ops(
        tree, [{"op": "add_block", "name": "c", "envelope": "box:w0.1d0.03h0.001"}]
    )
    return tree


def test_domain_refuses_non_thin() -> None:
    """Thin-axis detection refuses a chamber without a unique thin axis
    (Q3: 3-D chambers out), naming the way out."""
    cube = Placed(prim=PolyFrustum(*_square(0.01), 0.01), xform=identity())
    with pytest.raises(BadInput, match="not thin") as exc:
        domain_from_solid(cube, _tip_patch(0.0, -1.0), _tip_patch(0.01, 1.0), nx=40)
    assert exc.value.next
    tube = Placed(prim=PolyFrustum(*_square(0.001), 0.01), xform=identity())
    with pytest.raises(BadInput, match="not thin"):
        domain_from_solid(tube, _tip_patch(0.0, -1.0), _tip_patch(0.01, 1.0), nx=40)


def _square(s: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    h = s / 2
    ring = [(-h, -h), (h, -h), (h, h), (-h, h)]
    return ring, list(ring)


def test_patch_ports_on_box() -> None:
    """patch: selectors resolve to port cell masks — the tip ribbon
    claims the boundary column band."""
    tree = _box_tree()
    placed = se_datums._placed(tree, "c")
    inlet = patch_port(tree, "c", "patch:c.side3@0,0+0.006x0.001")
    outlet = patch_port(tree, "c", "patch:c.side1@0,0+0.006x0.001")
    ch = domain_from_solid(placed, inlet, outlet, nx=200)
    d = ch.domain
    assert d.mask.shape == (60, 200)
    # side3 is the −x face: its band claims left-edge cells only.
    assert d.ports.inlet.sum() > 0 and d.ports.inlet[:, 5:].sum() == 0
    assert d.ports.outlet.sum() > 0 and d.ports.outlet[:, :-5].sum() == 0
    assert not (d.ports.inlet & d.ports.outlet).any()


def test_flow_view_text_and_json() -> None:
    """view='flow' serves the text summary and the 2b player payload."""
    tree = _box_tree()
    args = {
        "block": "c",
        "inlet": "patch:c.side3@0,0+0.006x0.001",
        "outlet": "patch:c.side1@0,0+0.006x0.001",
        "dv": 6.0,
        "n": 60,
        "frames": 8,
        "profile": "plug",
        "mu": 1e-3,
        "rho": 1000.0,
        "u0": 0.01,
    }
    body = render_flow(None, tree, args)
    assert "R* =" in body and "regime card" in body and "breakthrough" in body
    payload = json.loads(render_flow(None, tree, {**args, "format": "json"}))
    assert payload["scale"]["L"] == pytest.approx(0.1)
    assert payload["scale"]["h0"] == pytest.approx(0.001)
    assert payload["scale"]["u0"] == pytest.approx(0.01)
    assert payload["card"]["ok"]
    fr = payload["frames"]
    assert fr["shape"] == [8, 60, 3]
    assert fr["dtype"] == "float32"
    blob = np.frombuffer(base64.b64decode(fr["data_b64"]), dtype="<f4")
    frames = blob.reshape(fr["shape"])
    assert frames[:, :, 2].min() >= 0.0 and frames[:, :, 2].max() <= 1.0
    assert fr["frame_t"] is not None and len(fr["frame_t"]) == 8
    mesh = payload["mesh"]
    assert len(mesh["outline"]) >= 4
    depth = np.frombuffer(
        base64.b64decode(mesh["floor_depth"]["data_b64"]), dtype="<f4"
    )
    assert depth.size == 60 * 200
    mask = np.frombuffer(base64.b64decode(mesh["mask"]["data_b64"]), dtype=np.uint8)
    assert mask.sum() == 60 * 200  # flat box: every cell inside
    assert payload["subsampled"]["particles_stride"] == 1
    # 2b player extras: port masks, tracking plane, per-particle outcomes
    ports = mesh["ports"]
    inlet = np.frombuffer(base64.b64decode(ports["inlet_b64"]), dtype=np.uint8)
    assert inlet.size == 60 * 200 and inlet.sum() > 0
    assert payload["z_plane_star"] == pytest.approx(0.99)
    parts = payload["particles"]
    assert parts["n"] == 60
    res = np.frombuffer(base64.b64decode(parts["residence_b64"]), dtype="<f4")
    cross = np.frombuffer(base64.b64decode(parts["cross_b64"]), dtype="<f4")
    assert res.size == 60 and cross.size == 120
    assert payload["scale"]["geometry_L"] == pytest.approx(0.1)
    # size= re-dimensionalises the same shape for the card
    big = json.loads(
        render_flow(None, tree, {**args, "format": "json", "size": 0.5, "u0": 0.05})
    )
    assert big["scale"]["L"] == pytest.approx(0.5)
    assert big["scale"]["h0"] == pytest.approx(0.005)
    assert not big["card"]["ok"]


def test_flow_pockets_lists_chamber_specs() -> None:
    tree = _chamber_tree()
    found = flow_pockets(tree)
    assert [(fp.block, fp.pocket) for fp in found] == [("c", "flow")]
    assert found[0].spec.profile == "plug"
    assert flow_pockets(_box_tree()) == []


def test_payload_subsamples_over_cap() -> None:
    """Over ~20 MB the payload subsamples particles first, then frames,
    and says by how much."""
    chamber = _hex_domain(nx=60)
    paths = trace(
        solve(chamber.domain),
        Seed.across_inlet(chamber.domain),
        400,
        D_V_STAR,
        0,
        record_frames=8,
        profile="plug",
    )
    big = np.zeros((4200, 400, 3), dtype=np.float32)
    big[:, :, 0] = np.linspace(0, 1, 400)
    big[:, :, 2] = 0.5
    from dataclasses import replace

    paths = replace(paths, frames=big, frame_t=np.linspace(0, 1, 4200))
    payload = flow_payload(chamber, paths, None, u0=None, profile="plug", d_v_star=6.0)
    sub = payload["subsampled"]
    blob = base64.b64decode(payload["frames"]["data_b64"])
    assert len(blob) <= PAYLOAD_CAP_BYTES
    # 4200×400×3×4 B = 20.16 MB > cap → particles halved first.
    assert sub["particles_stride"] == 2 and sub["frames_stride"] == 1
    assert "subsampled" in sub["note"]
    assert payload["frames"]["shape"] == [4200, 200, 3]


def test_parse_flow_shape() -> None:
    spec = parse_flow_shape(
        "flow:in=patch:c.side3@0,0+0.006x0.001;out=patch:c.side1@0,0+0.006x0.001;dv=6"
    )
    assert isinstance(spec, FlowSpec)
    assert spec.d_v_star == 6.0 and spec.profile == "parabolic" and spec.zplane == 0.99
    spec2 = parse_flow_shape("flow:in=p;out=q;dv=0.5;profile=plug;zplane=0.98")
    assert isinstance(spec2, FlowSpec)
    assert spec2.profile == "plug" and spec2.zplane == 0.98
    for bad in (
        "flow:out=q;dv=1",
        "flow:in=p;dv=1",
        "flow:in=p;out=q",
        "flow:in=p;out=q;dv=x",
        "box:w1d1h1",
    ):
        with pytest.raises(ValueError):
            parse_flow_shape(bad)


def _chamber_tree() -> SeTree:
    tree = _box_tree()
    out_patch = "patch:c.side1@0,0+0.006x0.001"
    floor_patch = "patch:c.bottom@0,0+0.06x0.02"
    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "c",
                "name": "flow",
                "shape": (
                    "flow:in=patch:c.side3@0,0+0.006x0.001;"
                    "out=patch:c.side1@0,0+0.006x0.001;dv=6;profile=plug"
                ),
                "regions": [{"selector": out_patch}, {"selector": floor_patch}],
            }
        ],
    )
    return tree


def test_flow_shape_accepted_by_pocket_op() -> None:
    tree = _chamber_tree()
    assert (tree.blocks["c"].pockets["flow"].shape or "").startswith("flow:")
    tree2 = _box_tree()
    with pytest.raises(OpError):
        apply_ops(
            tree2,
            [
                {
                    "op": "add_pocket",
                    "block": "c",
                    "name": "bad",
                    "shape": "flow:in=patch:c.side3@0,0+0.006x0.001",
                    "regions": [],
                }
            ],
        )


def test_flow_computers() -> None:
    tree = _chamber_tree()
    out_patch = "patch:c.side1@0,0+0.006x0.001"
    floor_patch = "patch:c.bottom@0,0+0.06x0.02"
    bt = MeasureSpec(
        block="c",
        name="bt",
        datum=out_patch,
        measurand="breakthrough-fraction",
        unit="ratio",
    )
    v = FLOW_COMPUTERS["breakthrough-fraction"](tree, bt)
    assert v is not None and 0.0 <= v <= 1.0
    rr = MeasureSpec(
        block="c",
        name="rr",
        datum=out_patch,
        measurand="resistance-ratio",
        unit="ratio",
    )
    v2 = FLOW_COMPUTERS["resistance-ratio"](tree, rr)
    assert v2 == pytest.approx(1.0, rel=0.02)  # flat chamber ≈ its own reference
    rt = MeasureSpec(
        block="c",
        name="rt",
        datum=out_patch,
        measurand="residence-time-t0",
        unit="ratio",
    )
    v3 = FLOW_COMPUTERS["residence-time-t0"](tree, rt)
    assert v3 is not None and v3 > 0.0
    ev = MeasureSpec(
        block="c", name="ev", datum=floor_patch, measurand="evenness", unit="ratio"
    )
    # a flat chamber has no floor topography, so no particle reaches the
    # tracking plane: evenness is a reason, never a 0.0 value
    assert FLOW_COMPUTERS["evenness"](tree, ev) is None
    assert "no particle crossed" in (FLOW_COMPUTERS["evenness"].reason or "")


def test_flow_computer_reports_reason() -> None:
    """A measure on a region no flow pocket claims is uncomputable —
    with a reason, never silent."""
    tree = _box_tree()
    m = MeasureSpec(
        block="c",
        name="x",
        datum="patch:c.side1@0,0+0.006x0.001",
        measurand="breakthrough-fraction",
        unit="ratio",
    )
    comp = FLOW_COMPUTERS["breakthrough-fraction"]
    assert comp(tree, m) is None
    assert "pocket" in (comp.reason or "")


def test_flow_computer_field_chamber_loads_grid_via_tree(store: Any) -> None:
    """gr503066: a flow measure whose chamber envelope is a ``field:``
    grid blob computes from the tree-wired loader alone. The computers'
    signature is store-free (``drc()`` stays library-pure), so the loader
    reaches ``_flow_run`` through ``persist.load_tree`` — before the fix
    the chamber blob could not load and the measure came back
    uncomputable."""
    from precis.dispatch import Hub
    from precis_se import persist
    from precis_se.handler import SeHandler
    from tests._flow_hex import hex_chamber_field, hex_chamber_ops

    se = SeHandler(hub=Hub(store=store))
    ops = hex_chamber_ops("0" * 64)
    se.put(id="flow-field-chamber", text=json.dumps({"ops": ops[1:3]}))
    ref = store.get_ref(kind="se", id="flow-field-chamber")
    assert ref is not None
    sha = store.put_field(ref.id, hex_chamber_field(pitch=5e-4))
    ops = hex_chamber_ops(sha)
    ops[-1]["regions"] = [{"selector": f"patch:pout.side3@0,0+{PORT_W}x{H}"}]
    se.put(id="flow-field-chamber", text=json.dumps({"ops": ops}))

    ref = store.get_ref(kind="se", id="flow-field-chamber")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert tree.field_loader is not None
    m = MeasureSpec(
        block="chamber",
        name="bt",
        datum=f"patch:pout.side3@0,0+{PORT_W}x{H}",
        measurand="breakthrough-fraction",
        unit="ratio",
    )
    computer = FLOW_COMPUTERS["breakthrough-fraction"]
    value = computer(tree, m)
    assert value is not None, f"still uncomputable: {computer.reason}"
    assert 0.0 <= value <= 1.0


def test_registered_in_computers() -> None:
    from precis_se.properties import COMPUTERS, is_checked

    for slug in (
        "breakthrough-fraction",
        "residence-time-t0",
        "resistance-ratio",
        "evenness",
    ):
        assert slug in COMPUTERS and is_checked(slug)
    tree = _chamber_tree()
    m = MeasureSpec(
        block="c",
        name="bt",
        datum="patch:c.side1@0,0+0.006x0.001",
        measurand="breakthrough-fraction",
        unit="ratio",
    )
    v = COMPUTERS["breakthrough-fraction"](tree, m)
    assert v is not None and 0.0 <= v <= 1.0


# ── se-flow-kernel slice 4a: out.physics → flow: pocket + the import
# re-check (gr563788, td563794) ───────────────────────────────────────


def _flow_import(
    store: Any, out_dir: Any, *, design: str = "wsflow", target: str = "design"
) -> Any:
    """``import_workspace_run`` over a written ``out/`` tree as a fresh job."""
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    return import_workspace_run(
        store,
        workspace="wsflow",
        design=design,
        out_dir=out_dir,
        run_job=int(job.id),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        target=target,
    )


def _flow_workspace_run(store: Any, tmp_path: Any, **kw: Any) -> tuple[Any, Any, Any]:
    """The one stand-in for the not-yet-landed workspace ``run`` (Reto's
    ruling, gr563788): a host ``out.run`` written by the fixture script
    plus ``import_workspace_run`` — test code only, no product path.
    ``test_flow_workspace_run_to_player`` (xfail strict) forces deleting
    this once ``put(kind='workspace', mode='run')`` exists."""
    out_dir = Path(tmp_path) / "out"
    tamper = kw.pop("tamper", None)
    oc = flow_chamber.write_out(out_dir, **kw)
    if tamper is not None:
        _tamper(out_dir, tamper)
    se_ref = store.insert_ref(kind="se", slug="wsflow", title="wsflow", meta={})
    res = _flow_import(store, out_dir)
    return oc, res, persist.load_tree(store, se_ref.id)


def _tamper(out_dir: Path, tamper: Any) -> None:
    doc = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    tamper(doc)
    (out_dir / "manifest.json").write_text(
        json.dumps(doc, indent=1) + "\n", encoding="utf-8"
    )


def _reload(store: Any, slug: str = "wsflow") -> Any:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id)


def _se(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis_se.handler import SeHandler

    return SeHandler(hub=Hub(store=store))


_FLOW_MEASURES = flow_chamber.MEASURES
_FLAT_POCKET = "flow:in=patch:c.side3@0,0+0.006x0.001;out=patch:c.side1@0,0+0.006x0.001"


def test_flow_workspace_import_pocket_and_recheck(store: Any, tmp_path: Any) -> None:
    oc, res, tree = _flow_workspace_run(store, tmp_path)
    found = flow_pockets(tree)
    assert [(f.block, f.pocket) for f in found] == [("c", "flow")]
    spec = found[0].spec
    assert spec.cells == 200 and spec.seed == 0 and spec.n == 200
    assert not any("script_mismatch" in f for f in res.findings)
    assert not any("resolution_differs" in f for f in res.findings)
    assert {m.measurand for m in tree.measures} == {
        "breakthrough-fraction",
        "residence-time-t0",
        "resistance-ratio",
    }
    for m in tree.measures:
        comp = FLOW_COMPUTERS[m.measurand]
        value = comp(tree, m)
        assert value == pytest.approx(oc.measures[m.measurand], rel=1e-9)
        assert m.value == pytest.approx(value, rel=0)
        assert m.origin == "user"
    payload = json.loads(
        render_flow(
            store,
            tree,
            {
                "block": "c",
                "inlet": spec.inlet,
                "outlet": spec.outlet,
                "dv": spec.d_v_star,
                "profile": spec.profile,
                "n": 60,
                "frames": 4,
                "format": "json",
            },
        )
    )
    assert payload["mesh"]["outline"] and payload["frames"]["shape"] == [4, 60, 3]
    assert payload["mesh"]["ports"]["inlet_b64"]


def test_flow_fixture_numbers_depend_on_every_solve_input() -> None:
    """The 1e-9 comparisons only mean something if a wrong input moves a
    number: on the drilled chamber each of seed, n, dv, cells and seed_z
    shifts a stated measure far beyond that bound. The flat floor shows
    why the fixture digs holes — no particle reaches the plane there."""
    solid = flow_chamber.chamber_solid()

    def run(*, seed: int = 0, cells: int = 100, **spec: Any) -> dict[str, float]:
        full = flow_chamber.flow_spec(**spec)
        return evaluate(solid, ["flow"], full, seed=seed, cells=cells).measures

    base = run()
    assert 0.0 < base["breakthrough-fraction"] < 1.0
    moved = {
        "seed": run(seed=1),
        "n": run(n=150),
        "dv": run(dv=1.5),
        "cells": run(cells=60),
        "seed_z": run(seed_z=[0.5, 1.0]),
    }
    for name, other in moved.items():
        gap = max(
            abs(other[k] - base[k])
            for k in ("breakthrough-fraction", "residence-time-t0")
        )
        assert gap > 1e-6, name
    flat = evaluate(
        flow_chamber.chamber_solid(flow_chamber.FLAT_DSL),
        ["flow"],
        flow_chamber.flow_spec(),
        cells=100,
    )
    assert flat.measures["breakthrough-fraction"] == 0.0


def test_flow_workspace_import_script_mismatch(store: Any, tmp_path: Any) -> None:
    def tamper(doc: dict) -> None:
        for row in doc["physics"]:
            row["outcome"]["measures"]["breakthrough-fraction"] += 0.25

    oc, res, tree = _flow_workspace_run(store, tmp_path, tamper=tamper)
    bad = [f for f in res.findings if "measure.script_mismatch" in f]
    assert len(bad) == 1 and "c.bt" in bad[0]
    bt = next(m for m in tree.measures if m.name == "bt")
    assert bt.value == pytest.approx(oc.measures["breakthrough-fraction"], rel=1e-9)


def test_flow_workspace_import_small_tamper_is_caught(
    store: Any, tmp_path: Any
) -> None:
    """A stated residence nudged by 1e-6 relative — well past the 1e-9
    bound, far below anything a scan of the numbers would catch."""

    def tamper(doc: dict) -> None:
        doc["physics"][0]["outcome"]["measures"]["residence-time-t0"] *= 1.0 + 1e-6

    _oc, res, _tree = _flow_workspace_run(store, tmp_path, tamper=tamper)
    bad = [f for f in res.findings if "measure.script_mismatch" in f]
    assert len(bad) == 1 and "c.rt" in bad[0]


def test_flow_workspace_import_resolution_differs(store: Any, tmp_path: Any) -> None:
    """Declared cells differ from the stated solve's: both findings land,
    because the value check runs regardless, and the stored number is
    the server's at the declared cells."""

    def tamper(doc: dict) -> None:
        for row in doc["physics"]:
            row["cells"] = 60  # declared, but the outcome was solved at 200

    oc, res, tree = _flow_workspace_run(store, tmp_path, tamper=tamper)
    differs = [f for f in res.findings if "measure.resolution_differs" in f]
    assert any("c.rr" in f for f in differs)
    mismatch = [f for f in res.findings if "measure.script_mismatch" in f]
    assert any("c.rr" in f for f in mismatch)
    server = evaluate(
        flow_chamber.chamber_solid(),
        ["flow"],
        flow_chamber.flow_spec(),
        cells=60,
    )
    rr = next(m for m in tree.measures if m.name == "rr")
    assert rr.value == pytest.approx(server.measures["resistance-ratio"], rel=1e-9)
    assert rr.value != pytest.approx(oc.measures["resistance-ratio"], rel=1e-6)


def test_flow_import_null_server_value_without_outcome_is_a_finding(
    store: Any, tmp_path: Any
) -> None:
    """The flat floor gives evenness no crossings; with no stated outcome
    there is no claim to compare, and the measure still may not go quiet."""

    def tamper(doc: dict) -> None:
        del doc["physics"][0]["outcome"]

    _oc, res, tree = _flow_workspace_run(
        store,
        tmp_path,
        tamper=tamper,
        dsl=flow_chamber.FLAT_DSL,
        measures=[{"name": "ev", "measurand": "evenness"}],
        evenness_region=flow_chamber.floor_patch(),
    )
    (hit,) = [f for f in res.findings if "measure.uncomputable" in f]
    assert "c.ev" in hit and "no particle crossed" in hit
    assert next(m for m in tree.measures if m.name == "ev").value is None


def test_flow_workspace_import_cells_carried(store: Any, tmp_path: Any) -> None:
    oc, res, tree = _flow_workspace_run(store, tmp_path, cells=60, seed=7)
    spec = flow_pockets(tree)[0].spec
    assert spec.cells == 60 and spec.seed == 7
    assert not res.findings
    for m in tree.measures:
        value = FLOW_COMPUTERS[m.measurand](tree, m)
        assert value == pytest.approx(oc.measures[m.measurand], rel=1e-9)


def test_flow_workspace_import_hard_violation(store: Any, tmp_path: Any) -> None:
    # A hard bound must fail on the server's number whatever the manifest
    # claims: the stated outcome here says the drilled chamber's ratio
    # meets it, and the server's does not.
    def tamper(doc: dict) -> None:
        doc["physics"][0]["outcome"]["measures"]["resistance-ratio"] = 0.05

    _oc, res, tree = _flow_workspace_run(
        store,
        tmp_path,
        tamper=tamper,
        measures=[
            {
                "name": "rr-max",
                "measurand": "resistance-ratio",
                "strength": "hard",
                "max_value": 0.1,
            }
        ],
    )
    assert any("script_mismatch" in f for f in res.findings)
    m = next(m for m in tree.measures if m.name == "rr-max")
    assert m.strength == "hard" and m.max_value == 0.1
    value = FLOW_COMPUTERS["resistance-ratio"](tree, m)
    assert value is not None and value > 0.1
    from precis_se.drc import drc

    assert any(
        f.rule == "measure_unmet" and f.subject == "c.rr-max"
        for f in drc(tree).findings
    )


# A: measurands.


def test_flow_measurands_seeded(store: Any) -> None:
    """Every flow computer's slug resolves to a seeded ``ratio`` node."""
    from precis_se.properties import COMPUTERS
    from precis_se.properties.measurand import measurand_resolver

    resolve = measurand_resolver(store)
    flow_slugs = sorted(FLOW_COMPUTERS)
    assert len(flow_slugs) == 8 and set(flow_slugs) <= set(COMPUTERS)
    for slug in flow_slugs:
        msr = resolve(slug)
        assert msr.slug == slug and msr.unit == "ratio" and msr.ref_id
    # 0231 seeded four of them first: 0274 skips those, adding no twin.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'slug', count(*) FROM refs WHERE kind = 'taxon' "
            "AND meta->>'slug' = ANY(%s) GROUP BY 1",
            (flow_slugs,),
        ).fetchall()
    assert dict(rows) == dict.fromkeys(flow_slugs, 1)


def test_flow_import_unknown_measurand_refuses_with_next(
    store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurand the taxonomy cannot resolve is a refusal naming a next
    step, never a row written with a guessed unit."""

    def no_measurands(_store: Any) -> Any:
        def resolve(ref: Any) -> Any:
            raise MeasureError(f"no measurand {ref!r}")

        return resolve

    monkeypatch.setattr(persist, "measurand_resolver", no_measurands)
    with pytest.raises(BadInput, match="no measurand") as exc:
        _flow_workspace_run(store, tmp_path)
    assert exc.value.next
    assert flow_pockets(_reload(store)) == []


def test_apply_maps_a_raw_measure_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_tree: Any, _ops: Any) -> None:
        raise MeasureError("bad selector")

    monkeypatch.setattr(workspace_import, "apply_ops", boom)
    with pytest.raises(BadInput, match="bad selector") as exc:
        workspace_import._apply(SeTree(), [], what="block 'c'")
    assert exc.value.next


# B: the rect: selector.


def _rect_text(point: tuple[float, float, float] = (0.05, 0.0, 5e-4)) -> str:
    doc = {
        "point": list(point),
        "normal": [1.0, 0.0, 0.0],
        "u": [0.0, 1.0, 0.0],
        "v": [0.0, 0.0, 1.0],
        "half_u": 0.003,
        "half_v": 5e-4,
    }
    return "rect:" + json.dumps(doc)


@pytest.mark.parametrize(
    "bad",
    [
        "rect:garbage",
        'rect:{"point":1}',
        "rect:[1,2,3]",
        _rect_text().replace('"half_v"', '"half_w"'),
        _rect_text().replace("[1.0, 0.0, 0.0]", "[0.0, 0.0, 0.0]"),
        _rect_text().replace("0.003", "-0.003"),
        _rect_text().replace("0.003", "true"),
        _rect_text().replace("0.003", "NaN"),
        _rect_text().replace("[0.05, 0.0, 0.0005]", "[0.05, 0.0]"),
    ],
)
def test_rect_selector_malformed_refuses_at_every_door(bad: str) -> None:
    with pytest.raises(MeasureError, match="rect"):
        se_datums.parse_selector(bad)
    tree = _box_tree()
    with pytest.raises(OpError, match="rect"):
        apply_ops(
            tree,
            [{"op": "add_measure", "block": "c", "name": "m", "datum": bad}],
        )
    with pytest.raises(OpError, match="rect"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_pocket",
                    "block": "c",
                    "name": "p",
                    "regions": [{"selector": bad}],
                }
            ],
        )


def test_rect_selector_text_round_trips() -> None:
    text = _rect_text((0.1 + 0.2, -1e-17, 1 / 3))
    sel = se_datums.parse_selector(text)
    assert sel.kind == "rect"
    canon = se_datums._selector_text(sel)
    assert se_datums.parse_selector(canon) == sel
    assert se_datums.same_region(text, canon)
    # the diagnostic selector key is not the region's identity
    doc = json.loads(text[len("rect:") :])
    doc["selector"] = "patch:c.side1@0,0+1x1;with=noise"
    assert se_datums.same_region("rect:" + json.dumps(doc), text)
    assert not se_datums.same_region(_rect_text((0.0, 0.0, 0.0)), text)


def test_rect_selector_resolves_with_the_block_pose() -> None:
    tree = _box_tree()
    apply_ops(
        tree,
        [{"op": "set_pose", "block": "c", "pose": [1.0, 2.0, 3.0], "pose_unit": "m"}],
    )
    got = se_datums.resolve(tree, "c", _rect_text())
    assert got.error is None and got.kind == "rect"
    assert np.allclose(got.point, [1.05, 2.0, 3.0005])
    assert np.allclose(got.normal, [1.0, 0.0, 0.0])


def test_rect_port_builds_the_patch() -> None:
    port = patch_port(_box_tree(), "c", _rect_text())
    assert np.allclose(port.point, [0.05, 0.0, 5e-4])
    assert port.half_u == 0.003 and port.half_v == 5e-4
    with pytest.raises(BadInput) as exc:
        patch_port(_box_tree(), "c", "rect:garbage")
    assert exc.value.next


def test_flow_import_rect_is_clean_for_drc_and_hashes(
    store: Any, tmp_path: Any
) -> None:
    from precis_se.drc import drc
    from precis_se.pockets import canonical_class

    _oc, _res, tree = _flow_workspace_run(store, tmp_path)
    pocket = tree.blocks["c"].pockets["flow"]
    assert all(r.startswith("rect:") for r in pocket.regions)
    rules = {f.rule for f in drc(tree).findings}
    assert "datum_unresolved" not in rules
    canon = canonical_class(tree, "c", pocket)
    assert len(canon["regions"]) == 2
    # the outlet carries the three measures, the server's numbers hashed
    outlet = next(r for r in canon["regions"] if r["measures"])
    assert len(outlet["measures"]) == 3
    assert all(row[1] is not None for row in outlet["measures"])
    before = pocket.class_hash
    assert before
    apply_ops(tree, [{"op": "set_desc", "block": "c", "desc": "re-described"}])
    assert tree.blocks["c"].pockets["flow"].class_hash == before


def test_flow_reimport_of_the_same_run(store: Any, tmp_path: Any) -> None:
    oc, _res, tree = _flow_workspace_run(store, tmp_path)
    shape = tree.blocks["c"].pockets["flow"].shape
    res = _flow_import(store, Path(tmp_path) / "out")
    assert not any("script_mismatch" in f for f in res.findings)
    again = _reload(store)
    assert again.blocks["c"].pockets["flow"].shape == shape
    assert sorted(m.name for m in again.measures) == ["bt", "rr", "rt"]
    bt = next(m for m in again.measures if m.name == "bt")
    assert bt.value == pytest.approx(oc.measures["breakthrough-fraction"], rel=1e-9)


def test_flow_set_desc_through_apply_ops_after_import(
    store: Any, tmp_path: Any
) -> None:
    _flow_workspace_run(store, tmp_path)
    _se(store).edit(
        id="wsflow",
        ops=[{"op": "set_desc", "block": "c", "desc": "the chamber, re-described"}],
    )
    tree = _reload(store)
    assert tree.blocks["c"].descr == "the chamber, re-described"
    assert flow_pockets(tree)


def test_flow_first_import_rows_on_two_blocks(store: Any, tmp_path: Any) -> None:
    def second_chamber(o: Any) -> None:
        o.block("d", flow_chamber.CHAMBER_DSL)
        spec = flow_chamber.flow_spec(n=120)
        stated = evaluate(flow_chamber.chamber_solid(), ["flow"], spec, seed=3)
        o.physics("d", ["flow"], spec, outcome=stated, seed=3, cells=200)

    _oc, res, tree = _flow_workspace_run(store, tmp_path, extra=second_chamber)
    assert [fp.block for fp in flow_pockets(tree)] == ["c", "d"]
    assert not any("script_mismatch" in f for f in res.findings)
    assert len(tree.measures) == 6
    values = {(m.block, m.name): m.value for m in tree.measures}
    assert values[("c", "bt")] != values[("d", "bt")]


def test_flow_later_instances_and_connects_in_the_same_import(
    store: Any, tmp_path: Any
) -> None:
    def assembly(o: Any) -> None:
        o.block(
            "t_lid",
            "lid add box:w10mmd10mmh2mm\n",
            template=True,
            ports=[{"name": "a", "roles": ["mount"]}],
        )
        o.instance("lid1", template="t_lid", pose=[0.0, 0.0, 0.01], pose_unit="m")
        o.instance("lid2", template="t_lid", pose=[0.0, 0.0, 0.02], pose_unit="m")
        o.connect("lid1.a", "lid2.a")

    _oc, res, tree = _flow_workspace_run(store, tmp_path, extra=assembly)
    assert "lid1" in tree.blocks and len(tree.connects) == 1
    assert flow_pockets(tree) and not any("mismatch" in f for f in res.findings)


def test_flow_ops_export_replays(store: Any, tmp_path: Any) -> None:
    """The exported ops rebuild the pocket and the measures, including a
    hard measure whose server value breaks its own band: add_measure
    refuses that point, and the copy's computer re-derives it."""
    from precis_se.ops_export import design_ops
    from precis_se.properties.measurand import measurand_resolver

    _oc, _res, tree = _flow_workspace_run(
        store,
        tmp_path,
        measures=[
            *_FLOW_MEASURES,
            {
                "name": "rr-max",
                "measurand": "resistance-ratio",
                "strength": "hard",
                "max_value": 0.1,
            },
        ],
    )
    copy = SeTree()
    copy.measurands = measurand_resolver(store)
    apply_ops(copy, design_ops(tree))
    pocket = copy.blocks["c"].pockets["flow"]
    assert pocket.shape == tree.blocks["c"].pockets["flow"].shape
    assert pocket.regions == tree.blocks["c"].pockets["flow"].regions
    by_name = {m.name: m for m in copy.measures}
    assert sorted(by_name) == ["bt", "rr", "rr-max", "rt"]
    assert by_name["rr-max"].value is None and by_name["rr-max"].max_value == 0.1
    live = next(m for m in tree.measures if m.name == "bt")
    assert by_name["bt"].value == live.value and by_name["bt"].datum == live.datum


def test_flow_out_of_band_server_value_does_not_block_measure_edits(
    store: Any, tmp_path: Any
) -> None:
    """The stored 0.97 breaks ``max=0.1``, and nobody chose it: an edit
    that leaves the value alone goes through, and one that states a
    value out of band still refuses."""
    _oc, _res, tree = _flow_workspace_run(
        store,
        tmp_path,
        measures=[
            {
                "name": "rr-max",
                "measurand": "resistance-ratio",
                "strength": "hard",
                "max_value": 0.1,
            }
        ],
    )
    stored = tree.measures[0].value
    assert stored is not None and stored > 0.6
    se = _se(store)
    edit = {"op": "set_measure", "block": "c", "name": "rr-max"}
    se.edit(id="wsflow", ops=[{**edit, "reason": "flat-floor reference"}])
    se.edit(id="wsflow", ops=[{**edit, "max": 0.6}])
    m = _reload(store).measures[0]
    assert m.reason == "flat-floor reference" and m.max_value == 0.6
    assert m.value == stored
    with pytest.raises(BadInput, match="must sit inside"):
        se.edit(id="wsflow", ops=[{**edit, "value": 5.0}])


def test_set_measure_keeps_the_band_check_on_a_plain_measure() -> None:
    tree = _box_tree()
    apply_ops(
        tree,
        [{"op": "add_measure", "block": "c", "name": "m", "value": 1.0, "max": 2.0}],
    )
    with pytest.raises(OpError, match="must sit inside"):
        apply_ops(
            tree,
            [{"op": "set_measure", "block": "c", "name": "m", "max": 0.5}],
        )


# C: a measure row refuses as add_measure does, and says what to do.


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"datum": "banana"}, "banana"),
        ({"name": "a.b/c"}, "must not contain"),
        ({"min_value": "x"}, "finite number"),
        ({"min_value": 2.0, "max_value": 1.0}, "exceeds"),
        ({"strength": "hard"}, "hard measure needs"),
        ({"strength": "firm"}, "strength"),
        ({"measurand": "hole-count"}, "not an"),
    ],
)
def test_flow_import_bad_measure_refuses_with_next(
    store: Any, tmp_path: Any, change: dict[str, Any], match: str
) -> None:
    def tamper(doc: dict) -> None:
        doc["physics"][0]["spec"]["measures"][0].update(change)

    with pytest.raises(BadInput, match=match) as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper)
    assert exc.value.next or exc.value.options
    tree = _reload(store)
    assert not tree.blocks and not tree.measures


# D: the manifest is the whole state, and ownership bounds the drop.


def test_flow_import_without_physics_drops_what_the_last_made(
    store: Any, tmp_path: Any
) -> None:
    _flow_workspace_run(store, tmp_path)
    assert flow_pockets(_reload(store))
    out2 = Path(tmp_path) / "out2"
    flow_chamber.write_out(out2, physics=False)
    _flow_import(store, out2)
    tree = _reload(store)
    assert flow_pockets(tree) == [] and tree.measures == []


def test_flow_physics_measure_redeclared_on_the_block_survives(
    store: Any, tmp_path: Any
) -> None:
    """Run 1 makes ``rr`` as a physics measure; run 2 has no physics row
    and declares ``rr`` on the block with the same datum. The drop must
    leave this run's declaration, hard bound included."""
    _oc, _res, tree = _flow_workspace_run(store, tmp_path)
    datum = next(m for m in tree.measures if m.name == "rr").datum
    out2 = Path(tmp_path) / "out2"
    flow_chamber.write_out(
        out2,
        physics=False,
        block_measures=[
            {
                "name": "rr",
                "measurand": "resistance-ratio",
                "datum": datum,
                "strength": "hard",
                "max": 0.5,
            }
        ],
    )
    res = _flow_import(store, out2)
    assert not any("measure." in f for f in res.findings)
    tree = _reload(store)
    assert flow_pockets(tree) == []
    (rr,) = tree.measures
    assert (rr.name, rr.datum, rr.strength) == ("rr", datum, "hard")
    assert rr.max_value == 0.5


def test_flow_hand_authored_pockets_survive_the_drop(store: Any, tmp_path: Any) -> None:
    _flow_workspace_run(store, tmp_path)
    shape = _FLAT_POCKET + ";dv=2"
    se = _se(store)
    se.edit(
        id="wsflow",
        ops=[
            {"op": "add_pocket", "block": "c", "name": "hand", "shape": shape},
            {
                "op": "add_measure",
                "block": "c",
                "name": "mine",
                "measurand": "evenness",
                "datum": "frame",
            },
        ],
    )
    out2 = Path(tmp_path) / "out2"
    flow_chamber.write_out(out2, physics=False)
    _flow_import(store, out2)
    tree = _reload(store)
    assert list(tree.blocks["c"].pockets) == ["hand"]
    assert [m.name for m in tree.measures] == ["mine"]


def test_flow_hand_edited_flow_pocket_survives_and_blocks_the_row(
    store: Any, tmp_path: Any
) -> None:
    """A pocket named ``flow`` whose shape is not what the import wrote
    is someone's edit: it is neither dropped nor overwritten."""
    _flow_workspace_run(store, tmp_path)
    hand = _FLAT_POCKET + ";dv=2"
    _se(store).edit(
        id="wsflow",
        ops=[{"op": "set_pocket", "block": "c", "name": "flow", "shape": hand}],
    )
    with pytest.raises(BadInput, match="did not make") as exc:
        _flow_import(store, Path(tmp_path) / "out")
    assert exc.value.next
    assert _reload(store).blocks["c"].pockets["flow"].shape == hand
    out2 = Path(tmp_path) / "out2"
    flow_chamber.write_out(out2, physics=False)
    _flow_import(store, out2)
    assert _reload(store).blocks["c"].pockets["flow"].shape == hand


def test_flow_hand_measure_with_the_rows_name_refuses(
    store: Any, tmp_path: Any
) -> None:
    out2 = Path(tmp_path) / "out"
    flow_chamber.write_out(out2, physics=False)
    store.insert_ref(kind="se", slug="wsflow", title="wsflow", meta={})
    _flow_import(store, out2)
    _se(store).edit(
        id="wsflow",
        ops=[{"op": "add_measure", "block": "c", "name": "bt", "value": 0.5}],
    )
    flow_chamber.write_out(out2)
    with pytest.raises(BadInput, match="did not make") as exc:
        _flow_import(store, out2)
    assert exc.value.next
    assert next(m for m in _reload(store).measures if m.name == "bt").value == 0.5


def test_flow_fork_keeps_hand_chambers_and_owns_the_copied_pocket(
    store: Any, tmp_path: Any
) -> None:
    """On a fork the tree is a copy, so the source's record says what the
    import made: the copied ``c.flow`` is replaced, a hand-authored
    ``flow`` chamber on another block is not."""
    _flow_workspace_run(store, tmp_path)
    hand = (
        "flow:in=patch:h.side3@0,0+0.006x0.001;out=patch:h.side1@0,0+0.006x0.001;dv=2"
    )
    _se(store).edit(
        id="wsflow",
        ops=[
            {"op": "add_block", "name": "h", "envelope": "box:w0.1d0.03h0.001"},
            {"op": "add_pocket", "block": "h", "name": "flow", "shape": hand},
        ],
    )
    out2 = Path(tmp_path) / "out2"
    flow_chamber.write_out(out2, physics=False)
    res = _flow_import(store, out2, target="fork")
    fork = _reload(store, res.design_slug)
    assert fork.blocks["h"].pockets["flow"].shape == hand
    assert "flow" not in fork.blocks["c"].pockets
    assert fork.measures == []


def test_flow_duplicate_rows_on_one_block_refuse(store: Any, tmp_path: Any) -> None:
    def tamper(doc: dict) -> None:
        doc["physics"].append(json.loads(json.dumps(doc["physics"][0])))

    with pytest.raises(BadInput, match="already has a physics row") as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper)
    assert exc.value.next


# E: the solve budget.


def test_flow_import_solves_once_per_row_outside_the_lock(
    store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six measures over two rows cost two solves, and neither runs under
    ``tree_mutation``'s advisory lock."""
    from contextlib import contextmanager

    from precis_geom import physics as geom_physics

    def second_chamber(o: Any) -> None:
        o.block("d", flow_chamber.CHAMBER_DSL)
        spec = flow_chamber.flow_spec()
        o.physics("d", ["flow"], spec, seed=1, cells=60)

    out_dir = Path(tmp_path) / "out"
    flow_chamber.write_out(out_dir, cells=60, extra=second_chamber)
    store.insert_ref(kind="se", slug="wsflow", title="wsflow", meta={})

    locked = {"now": False}
    calls: list[bool] = []
    real_evaluate = geom_physics.evaluate
    real_mutation = persist.tree_mutation

    def counting(*args: Any, **kw: Any) -> Any:
        calls.append(locked["now"])
        return real_evaluate(*args, **kw)

    @contextmanager
    def tracked(*args: Any, **kw: Any) -> Any:
        with real_mutation(*args, **kw) as conn:
            locked["now"] = True
            try:
                yield conn
            finally:
                locked["now"] = False

    monkeypatch.setattr(geom_physics, "evaluate", counting)
    monkeypatch.setattr(persist, "tree_mutation", tracked)
    _flow_import(store, out_dir)
    assert calls == [False, False]
    assert len(_reload(store).measures) == 6


def test_flow_physics_row_cap_refuses(store: Any, tmp_path: Any) -> None:
    def tamper(doc: dict) -> None:
        row = doc["physics"][0]
        doc["physics"] = [
            json.loads(json.dumps(row))
            for _ in range(workspace_import.MAX_WS_PHYSICS_ROWS + 1)
        ]

    with pytest.raises(BadInput, match="rows-per-run cap") as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper)
    assert exc.value.next


def test_flow_physics_measure_cap_refuses(store: Any, tmp_path: Any) -> None:
    def tamper(doc: dict) -> None:
        doc["physics"][0]["spec"]["measures"] = [
            {"name": f"m{i}", "measurand": "resistance-ratio"}
            for i in range(workspace_import.MAX_WS_PHYSICS_MEASURES + 1)
        ]

    with pytest.raises(BadInput, match="measures-per-row cap") as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper)
    assert exc.value.next


def _open_transactions() -> int:
    """Client backends on this database inside a transaction, seen from
    a connection of their own."""
    import psycopg

    from tests.conftest import _active_dsn

    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND backend_type = 'client backend' AND xact_start IS NOT NULL"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_flow_op_door_solves_with_no_transaction_open(
    store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``files`` passed the door writes rows inside ``store.atomic()``,
    so a solve in there sits idle in transaction with row locks held: the
    solve has to come first. The control shows the spy sees a transaction
    when one is open."""
    from precis_geom import physics as geom_physics

    out_dir = Path(tmp_path) / "out"
    oc = flow_chamber.write_out(out_dir, cells=60)
    run = {
        p.relative_to(out_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in out_dir.rglob("*")
        if p.is_file()
    }
    se = _se(store)
    se.put(id="wsflow", text="{}")

    seen: list[int] = []
    real_evaluate = geom_physics.evaluate

    def spying(*args: Any, **kw: Any) -> Any:
        seen.append(_open_transactions())
        return real_evaluate(*args, **kw)

    monkeypatch.setattr(geom_physics, "evaluate", spying)
    body = se.edit(
        id="wsflow",
        ops=[
            {
                "op": "import_workspace",
                "workspace": "wsflow",
                "run": run,
                "files": {"main.py": "print('chamber')\n"},
            }
        ],
    ).body
    assert seen == [0]
    assert "script_mismatch" not in body
    bt = next(m for m in _reload(store).measures if m.name == "bt")
    assert bt.value == pytest.approx(oc.measures["breakthrough-fraction"], rel=1e-9)

    with store.atomic() as conn:
        conn.execute("SELECT 1")
        assert _open_transactions() >= 1


def _work_manifest(*rows: tuple[int, int]) -> dict[str, Any]:
    """A manifest of ``(cells, n)`` physics rows, one block each."""
    names = [f"c{i}" for i in range(len(rows))]
    return {
        "schema": 1,
        "blocks": [
            {"name": name, "file": f"blocks/{name}.cad", "scale": "macro"}
            for name in names
        ],
        "physics": [
            {
                "block": name,
                "physics": ["flow"],
                "cells": cells,
                "spec": {
                    "inlet": _rect_text(),
                    "outlet": _rect_text((-0.05, 0.0, 5e-4)),
                    "dv": 1.0,
                    "n": n,
                },
            }
            for name, (cells, n) in zip(names, rows, strict=True)
        ],
    }


@pytest.mark.parametrize(
    "rows",
    [
        [(860, 2000)] * 3,  # the first measured solve, 18.6 s each
        [(400, 40000)],  # the second, 32 s
        [(200, 400)] * 8,  # the row cap at the default resolution
    ],
)
def test_flow_physics_work_cap_passes(rows: list[tuple[int, int]]) -> None:
    assert len(workspace_import._vet_manifest(_work_manifest(*rows))["physics"]) == len(
        rows
    )


@pytest.mark.parametrize(
    "rows",
    [
        [(860, 2000)] * 4,
        [(400, 40000)] * 2,
        [(860, 2000)] * 8,
    ],
)
def test_flow_physics_work_cap_refuses(rows: list[tuple[int, int]]) -> None:
    with pytest.raises(BadInput, match="per-import cap") as exc:
        workspace_import._vet_manifest(_work_manifest(*rows))
    assert exc.value.next


# F: the token carries the script's exact numbers.


def test_flow_token_keeps_full_precision(store: Any, tmp_path: Any) -> None:
    """``:g`` keeps six digits, and on a drilled chamber the seventh moves
    the answer — the pre-fix token read back dv=6.12346."""
    dv, zplane, seed_z = 6.123456789, 0.987654321, [0.123456789, 0.987654321]
    oc, res, tree = _flow_workspace_run(
        store, tmp_path, dv=dv, zplane=zplane, seed_z=seed_z, cells=100
    )
    spec = flow_pockets(tree)[0].spec
    assert spec.d_v_star == dv and spec.zplane == zplane
    assert spec.seed_z == tuple(seed_z)
    assert not any("script_mismatch" in f for f in res.findings)
    for m in tree.measures:
        value = FLOW_COMPUTERS[m.measurand](tree, m)
        assert value == pytest.approx(oc.measures[m.measurand], rel=1e-9)
    assert (
        oc.measures
        != evaluate(
            flow_chamber.chamber_solid(),
            ["flow"],
            flow_chamber.flow_spec(
                dv=6.12346, zplane=0.987654, seed_z=[0.123457, 0.987654]
            ),
            cells=100,
        ).measures
    )


# H: the physics payload is typed at the door.


def _row(doc: dict) -> dict:
    return doc["physics"][0]


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        (lambda d: _row(d)["outcome"].update(measures="good"), "outcome.measures"),
        (lambda d: _row(d)["outcome"]["cost"].update(nx="200"), "cost.nx"),
        (lambda d: _row(d)["outcome"].update(cost=3), "outcome.cost"),
        (lambda d: _row(d)["outcome"].update(seed="0"), "outcome.seed"),
        (
            lambda d: _row(d)["outcome"]["measures"].update(evenness="high"),
            "evenness",
        ),
        (lambda d: _row(d)["spec"].update(dv="6"), r"spec\.dv"),
        (lambda d: _row(d)["spec"].update(dv=True), r"spec\.dv"),
        (lambda d: _row(d)["spec"].update(dv=-1.0), "dv must be"),
        (lambda d: _row(d)["spec"].update(seed_z=[0.0, 0.5, 1.0]), "seed_z"),
        (lambda d: _row(d)["spec"].update(seed_z=[0.5, 0.1]), "seed_z"),
        (lambda d: _row(d)["spec"].update(zplane="top"), "zplane"),
        (lambda d: _row(d)["spec"].update(n="many"), r"spec\.n"),
        (lambda d: _row(d)["spec"].update(n=0), r"spec\.n"),
        (lambda d: _row(d)["spec"].update(profile=4), "profile"),
        (lambda d: _row(d)["spec"].update(profile="turbulent"), "profile"),
        (lambda d: _row(d)["spec"].update(inlet="rect:garbage"), "rect"),
        (lambda d: _row(d)["spec"].update(outlet='rect:{"point":1}'), "rect"),
        (lambda d: _row(d)["spec"].update(inlet=3), "inlet"),
        (lambda d: _row(d)["spec"].update(inlet="frame"), "inlet"),
        (lambda d: _row(d)["spec"].update(measures="all"), "measures"),
        (lambda d: _row(d)["spec"].update(colour="red"), "colour"),
        (lambda d: _row(d).update(seed="0"), "seed"),
        (lambda d: _row(d).update(seed=-1), "seed"),
        (lambda d: _row(d).update(cells="200"), "cells"),
        (lambda d: _row(d).update(cells=4), "cells"),
        (lambda d: _row(d).update(frame="x"), "frame"),
        (lambda d: _row(d).update(physics=["heat"]), "heat"),
        (lambda d: d.update(physics="flow"), "physics"),
    ],
)
def test_flow_vet_refuses_bad_payload_before_any_transaction(
    store: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Any,
    match: str,
) -> None:
    def no_transaction(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a bad payload reached tree_mutation")

    monkeypatch.setattr(persist, "tree_mutation", no_transaction)
    with pytest.raises(BadInput, match=match) as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper, cells=60)
    assert exc.value.next or exc.value.options


def test_flow_physics_on_an_atomic_block_refuses() -> None:
    doc = {
        "schema": 1,
        "blocks": [{"name": "a", "file": "blocks/a.extxyz", "scale": "atomic"}],
        "physics": [
            {
                "block": "a",
                "physics": ["flow"],
                "spec": {"inlet": _rect_text(), "outlet": _rect_text(), "dv": 1.0},
            }
        ],
    }
    with pytest.raises(BadInput, match="cad solid") as exc:
        workspace_import._vet_manifest(doc)
    assert exc.value.next


# I: evenness scores the region evaluate scored.


def test_flow_evenness_uses_the_region_not_the_outlet(
    store: Any, tmp_path: Any
) -> None:
    region = flow_chamber.floor_patch()
    oc, res, tree = _flow_workspace_run(
        store,
        tmp_path,
        cells=100,
        measures=[{"name": "ev", "measurand": "evenness"}],
        evenness_region=region,
    )
    assert 0.0 < oc.measures["evenness"] <= 1.0
    assert not res.findings
    m = tree.measures[0]
    assert m.value == pytest.approx(oc.measures["evenness"], rel=1e-9)
    floor = se_datums.parse_selector(m.datum)
    assert floor.kind == "rect" and floor.rect is not None
    assert np.allclose(floor.rect[1], [0.0, 0.0, -1.0])
    pocket = tree.blocks["c"].pockets["flow"]
    assert any(se_datums.same_region(r, m.datum) for r in pocket.regions)
    value = FLOW_COMPUTERS["evenness"](tree, m)
    assert value == pytest.approx(oc.measures["evenness"], rel=1e-9)
    narrower = evaluate(
        flow_chamber.chamber_solid(),
        ["flow"],
        flow_chamber.flow_spec(evenness_region=flow_chamber.floor_patch(0.01, 0.005)),
        cells=100,
    )
    assert narrower.measures["evenness"] != oc.measures["evenness"]


def test_flow_evenness_without_a_region_refuses(store: Any, tmp_path: Any) -> None:
    with pytest.raises(BadInput, match="evenness_region") as exc:
        _flow_workspace_run(
            store,
            tmp_path,
            cells=60,
            measures=[{"name": "ev", "measurand": "evenness"}],
        )
    assert exc.value.next


def test_flow_evenness_with_another_datum_refuses(store: Any, tmp_path: Any) -> None:
    with pytest.raises(BadInput, match="not the spec's evenness_region") as exc:
        _flow_workspace_run(
            store,
            tmp_path,
            cells=60,
            measures=[
                {"name": "ev", "measurand": "evenness", "datum": _rect_text()},
            ],
            evenness_region=flow_chamber.floor_patch(),
        )
    assert exc.value.next


# L: ports and the solve read one geometry.


class _BoundChamberTree:
    """A cad-bound chamber whose authored envelope is another solid: a
    flat 40×10 mm box, while the bound design is the 100 mm drilled hex."""

    @staticmethod
    def build() -> SeTree:
        tree = SeTree()
        apply_ops(
            tree, [{"op": "add_block", "name": "c", "envelope": "box:w0.04d0.01h0.001"}]
        )
        node = tree.blocks["c"]
        node.bound_kind, node.bound = "cad", "hex-design"
        design, _expr = flow_chamber.chamber_solid()
        tree.cad_designs = lambda slug: (design, {}) if slug == "hex-design" else None
        return tree


def test_cad_bound_chamber_patch_ports_solve_on_the_envelope() -> None:
    """A ``patch:`` port exists only on the envelope, so the domain is the
    envelope too. Solved on the bound design instead, the 40 mm box's
    side faces land mid-chamber, and the ports claim interior cells."""
    tree = _BoundChamberTree.build()
    ports = {
        "inlet": "patch:c.side3@0,0+0.006x0.001",
        "outlet": "patch:c.side1@0,0+0.006x0.001",
    }
    chamber = flow_chamber_of(tree, ports)
    length = chamber.L
    assert length == pytest.approx(0.04)
    assert chamber.domain.ports.inlet[:, 5:].sum() == 0
    assert chamber.domain.ports.outlet[:, :-5].sum() == 0
    assert float(chamber.depth_star.max()) == pytest.approx(0.0)


def test_cad_bound_chamber_rect_ports_solve_on_the_design() -> None:
    """A ``rect:`` port is block-local coordinates, so the bound design —
    the solid with the floor topography — is the domain."""
    tree = _BoundChamberTree.build()
    from precis_geom.out import _physics_port

    ports = {
        "inlet": _physics_port(flow_chamber.tip_patch(-0.05, -1.0), "inlet"),
        "outlet": _physics_port(flow_chamber.tip_patch(0.05, 1.0), "outlet"),
    }
    chamber = flow_chamber_of(tree, ports)
    length = chamber.L
    assert length == pytest.approx(0.1)
    assert float(chamber.depth_star.max()) > 1.0


def _drc_flow_ports(tree: SeTree) -> list[Any]:
    from precis_se.drc import drc

    return [f for f in drc(tree).findings if f.rule == "flow_port_on_envelope"]


def test_drc_warns_a_cad_bound_chamber_with_envelope_ports() -> None:
    tree = _BoundChamberTree.build()
    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "c",
                "name": "flow",
                "shape": _FLAT_POCKET + ";dv=2",
            }
        ],
    )
    (hit,) = _drc_flow_ports(tree)
    assert hit.severity == "warn" and hit.subject == "c.flow"
    assert "rect:" in hit.detail and "hex-design" in hit.detail


def test_drc_quiet_for_rect_ports_and_unbound_chambers() -> None:
    rect = f"flow:in={_rect_text()};out={_rect_text((-0.05, 0.0, 5e-4))};dv=2"
    bound = _BoundChamberTree.build()
    apply_ops(
        bound,
        [{"op": "add_pocket", "block": "c", "name": "flow", "shape": rect}],
    )
    assert _drc_flow_ports(bound) == []
    assert _drc_flow_ports(_chamber_tree()) == []


def flow_chamber_of(tree: SeTree, ports: dict[str, str]) -> Chamber:
    from precis_se.flow import flow_chamber as build

    return build(None, tree, {"block": "c", "nx": 60, **ports})


def test_parse_flow_shape_cells_refusal_names_the_rule() -> None:
    with pytest.raises(ValueError, match="0 .the default. or ≥ 8"):
        parse_flow_shape("flow:in=p;out=q;dv=1;cells=4")
    with pytest.raises(ValueError, match="seed must be ≥ 0"):
        parse_flow_shape("flow:in=p;out=q;dv=1;seed=-1")


def test_out_physics_refuses_a_fluid_it_cannot_carry(tmp_path: Any) -> None:
    with geom_out.run(Path(tmp_path) / "out", check=False) as o:
        o.block("c", flow_chamber.CHAMBER_DSL)
        with pytest.raises(ValueError, match="spec.fluid"):
            o.physics(
                "c",
                ["flow"],
                flow_chamber.flow_spec(fluid={"mu": 1e-3, "rho": 1e3, "u0": 0.01}),
            )


def test_flow_import_refuses_a_manifest_fluid(store: Any, tmp_path: Any) -> None:
    """A manifest written by hand gets the same answer: the fluid is
    refused, never dropped on the way into the pocket."""

    def tamper(doc: dict) -> None:
        doc["physics"][0]["spec"]["fluid"] = {"mu": 1e-3, "rho": 1e3, "u0": 0.01}

    with pytest.raises(BadInput, match="spec.fluid") as exc:
        _flow_workspace_run(store, tmp_path, tamper=tamper, cells=60)
    assert exc.value.next


# K: the real run door.


class _NoWorkspaceRun(Exception):
    """``put(kind='workspace', mode='run')`` is not registered yet."""


def _workspace_run_door(hub: Any) -> Any:
    """The real run door — a capability check, not a guess at the module
    it will live in: the hub answers for the ``workspace`` kind and its
    ``put`` takes ``mode='run'`` (structure-workspaces §The loop)."""
    if "workspace" not in hub or "run" not in hub.modes_for("workspace", "put"):
        raise _NoWorkspaceRun("no put(kind='workspace', mode='run') on the hub")
    return hub.handler_for("workspace")


@pytest.mark.xfail(
    raises=_NoWorkspaceRun,
    strict=True,
    reason="structure-workspaces slice 1's run loop does not exist yet — "
    "when put(kind='workspace', mode='run') lands this goes red and "
    "_flow_workspace_run's scratch/tar stand-in is deleted with it",
)
def test_flow_workspace_run_to_player(runtime_with_store: Any, store: Any) -> None:
    """The real run end to end: the workspace door runs the fixture's
    ``main.py``, the design then carries its ``flow:`` pocket and
    ``view='flow'`` serves the player payload. Only the door's absence
    may fail this: any other exception, or a pass, is red."""
    door = _workspace_run_door(runtime_with_store.hub)
    _se(store).put(id="wsflow", text="{}")
    door.put(id="wsflow", args={"design": "se:wsflow"})
    door.edit(
        id="wsflow/main.py",
        mode="replace",
        text=Path(flow_chamber.__file__).read_text(encoding="utf-8"),
    )
    door.put(id="wsflow", mode="run", args={})
    tree = _reload(store)
    (found,) = flow_pockets(tree)
    assert (found.block, found.pocket) == ("c", "flow")
    payload = json.loads(
        render_flow(
            store,
            tree,
            {
                "block": found.block,
                "inlet": found.spec.inlet,
                "outlet": found.spec.outlet,
                "dv": found.spec.d_v_star,
                "n": 60,
                "frames": 4,
                "format": "json",
            },
        )
    )
    assert payload["mesh"]["outline"] and payload["frames"]["shape"] == [4, 60, 3]
