"""via_mask_web (Reto 2026-11-03, no tenting): a via keeps the soldermask
web — pad mask expansion + dam + via mask expansion, JLC-min 0.20mm — off
EVERY pad's copper edge, its own net's included (the own-net exemption in
``_ViaPlanter.static_survivors`` is gone). Where the copper clearance is
larger it wins. The planter refuses the site, the DRC flags the copper.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Point, box

from precis.pcb import capabilities as caps
from precis.pcb import drc
from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.mesh import build_mesh
from tests.test_pcb_topoplace_walls import _board

_R_SITE = 0.3
_WEB = 0.20  # 2 x 0.05 expansion + 0.10 dam at the JLC-min figures
_CAP4 = caps.capability_for("4layer")


def _probe_board(gap_mm: float, *, own: bool) -> tuple:
    """The corridor: two net-0 pins at (10, 20) and (30, 20); a 30mm-wide
    probe pad centred so its near edge sits ``_R_SITE + gap_mm`` above the
    corridor line — a via planted on the corridor shows it ``gap_mm`` of
    copper-edge clearance. ``own`` pins the probe pad to net 0."""
    pins = [
        {"x": 10.0, "y": 20.0, "net": 0, "side": 0},
        {"x": 30.0, "y": 20.0, "net": 0, "side": 0},
        {
            "x": 20.0,
            "y": 20.0 + 0.2 + _R_SITE + gap_mm,
            "net": 0 if own else 1,
            "side": 0,
            "w": 30.0,
            "h": 0.4,
        },
    ]
    d = _board(pins, n_nets=2)
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    planter = wires_mod._ViaPlanter(ctx, d, d.pose0)
    pad = box(5.0, 20.0 + _R_SITE + gap_mm, 35.0, 20.4 + _R_SITE + gap_mm)
    return d, mesh, ctx, planter, pad


def _survivor_gap(planter, mesh, ctx, pad, *, net: int, clearance: float) -> float:
    """The smallest via-edge to pad-edge gap any surviving candidate on a
    non-board edge shows — inf when nothing survives."""
    best = np.inf
    for e in range(len(mesh.edges)):
        if ctx.board[e]:
            continue
        for x, y in planter.static_survivors(e, net, _R_SITE, clearance):
            best = min(best, float(pad.distance(Point(float(x), float(y)))) - _R_SITE)
    return best


@pytest.mark.parametrize("own", [True, False])
def test_a_via_015_off_a_pad_is_refused(own: bool) -> None:
    """The pad sits 0.15mm of via-edge clearance off the corridor — inside
    the web, so no surviving site may show less than the web (0.20mm);
    without the rule the closest approach is the pad's own 0.15."""
    _d, mesh, ctx, planter, pad = _probe_board(0.15, own=own)
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.15)
    assert gap == np.inf or gap >= _WEB - 1e-3


@pytest.mark.parametrize("own", [True, False])
def test_a_via_021_off_a_pad_is_accepted(own: bool) -> None:
    _d, mesh, ctx, planter, pad = _probe_board(0.21, own=own)
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.15)
    assert 0.19 < gap < 0.3


def test_a_foreign_pad_still_holds_its_larger_copper_clearance() -> None:
    """The web is a floor, not the whole bar: at clearance 0.25 > web a
    foreign pad refuses the same 0.21mm geometry (``max(cv, cp, web)``).
    An own pad bars ``max(cv, web)`` — with cv 0.15 < web the web is the
    binding bar and the 0.21mm site survives."""
    _d, mesh, ctx, planter, pad = _probe_board(0.21, own=False)
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.25)
    assert gap == np.inf or gap >= 0.25 - 1e-3
    _d, mesh, ctx, planter, pad = _probe_board(0.21, own=True)
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.15)
    assert _WEB - 1e-3 <= gap < 0.3


def test_the_search_uses_the_emit_guards_pad_predicate() -> None:
    """A pad on a HIGH-clearance foreign net (cp=0.45): the old index
    priced it clearance=0, so the search planted at ``max(cv, web)`` —
    inside the emit guard's ``max(cv, cp, web)`` — and the branch was
    severed. With ``net_clearance`` on the index the same geometry is
    refused (no survivor under 0.45); without it the 0.30mm site would
    have been planted and severed. The own half: the guard bars an own
    pad at the web alone and the search at ``max(cv, web)`` — with
    cv 0.15 < web both reduce to the web, so the 0.18mm site that
    clears cv is refused by the shared predicate (the web binds)."""
    d, mesh, ctx, _planter, pad = _probe_board(0.30, own=False)
    planter = wires_mod._ViaPlanter(ctx, d, d.pose0, net_clearance=(0.15, 0.45))
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.15)
    assert gap == np.inf or gap >= 0.45 - 1e-3
    d, mesh, ctx, _planter, pad = _probe_board(0.18, own=True)
    planter = wires_mod._ViaPlanter(ctx, d, d.pose0, net_clearance=(0.15, 0.45))
    gap = _survivor_gap(planter, mesh, ctx, pad, net=0, clearance=0.15)
    assert gap == np.inf or gap >= _WEB - 1e-3


def test_via_mask_web_mm_reads_the_jlc_min_tier() -> None:
    assert caps.via_mask_web_mm(_CAP4) == pytest.approx(0.20)
    assert caps.via_mask_web_mm(None) == pytest.approx(0.20)


def _model(pad_net: str, gap_mm: float, *, via_net: str = "GND") -> dict:
    """One pad on F.Cu, one through-via ``gap_mm`` clear of its copper."""
    return {
        "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        "pads": [
            {
                "x": 0.0,
                "y": 0.0,
                "w": 0.4,
                "h": 0.4,
                "shape": "rect",
                "net": pad_net,
                "layer": "F.Cu",
                "refdes": "U1",
                "pin": "1",
            }
        ],
        "copper": [
            {
                "ctype": "via",
                "x": 0.0,
                "y": 0.2 + 0.3 + gap_mm,  # pad edge 0.2, via radius 0.3
                "dia_mm": 0.6,
                "net": via_net,
            }
        ],
    }


@pytest.mark.parametrize("pad_net", ["GND", "SIG"])
def test_drc_flags_a_via_inside_the_mask_web(pad_net: str) -> None:
    (f,) = drc.check_via_mask_web(_model(pad_net, 0.15), _CAP4)
    assert f.rule == "via_mask_web" and f.severity == "error"
    assert drc.check_via_mask_web(_model(pad_net, 0.21), _CAP4) == []


def test_drc_mask_web_runs_in_the_geometric_pass() -> None:
    findings = drc.run_geometric_drc(_model("SIG", 0.15), capability=_CAP4)
    assert any(f.rule == "via_mask_web" for f in findings)
