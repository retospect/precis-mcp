"""precis_se.flow — the se adapter of docs/backlog/se-flow-kernel.md
slice 2: cad envelope → Domain (thin axis, depth map, patch: ports),
view='flow' payload, the ``flow:`` chamber-spec pocket and the four
flow COMPUTERS.
"""

from __future__ import annotations

import base64
import json
import math

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
from precis_se import datums as se_datums
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
from precis_se.measures import MeasureSpec
from precis_se.ops import OpError, SeTree, apply_ops

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
    v4 = FLOW_COMPUTERS["evenness"](tree, ev)
    assert v4 is not None and 0.0 <= v4 <= 1.0


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
