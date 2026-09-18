"""The maze router is layer-blind for bottom-mounted pads (defect found
2026-09-18, instrumented offline reproduction against the EWOD dogfood
board): every pad in :func:`~precis.pcb.realize._realize_maze`'s ``pads``
list used to be claimed and searched on :data:`~precis.pcb.rules.PAD_LAYER`
(F.Cu) regardless of which physical side its instance actually mounts on
(:attr:`~precis.pcb.ir.PcbIR.inst_bottom`) — the same predicate
:func:`~precis.pcb.realize.pads_for_ir` already reads correctly (gr341516).
A bottom-mounted pin's real pad sits on B.Cu; claiming/searching it on F.Cu
instead means the cell it actually occupies is left unclaimed (any OTHER
net may route straight through it) while the search treats F.Cu — often
already owned by a neighbouring part's own pad — as the only legal
landing spot, so a connection that is perfectly routable on the real
board fails with ``no_path``.

This module is the router-side fix's own coverage, one layer below the
dogfood fixture's end-to-end assertion
(``tests/test_pcb_ewod_dogfood.py::test_dogfood_route_op_routes_real_
geometry_and_reports_the_escape_gap``, now layer-aware for the same
reason): a small, hand-built two-instance IR where the defect is
mechanically unambiguous rather than one signal among dozens.

**A second, related distinction pinned here too: SMD vs. THT/drilled
pad claims.** A trace MAY legally run on a different layer UNDER an SMD
pad (its copper is a flash on one side; the board underneath is real
routable space) but must NEVER cross THROUGH a drilled hole, which is
plated copper on every layer it passes through, physically the same
obstacle a via already is (see :func:`~precis.pcb.realize._stamp_pads`'s
own docstring, and ``drc.py::clearance_pairs_indexed``'s identical "a
drilled pad has no single ``item['layer']`` either" note). So a pad's
claim now carries a LAYER SET, not a single layer: one entry
(:func:`~precis.pcb.realize._side_layer`'s bottom/top resolution) for an
SMD pad, every board layer for a drilled one (keyed off
:attr:`~precis.pcb.realize.PadGeom.drill_mm`).
"""

from __future__ import annotations

from typing import Any

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.realize import RealizeConfig, realize

_TOP_LAYER = 0  # F.Cu -- DEFAULT_STACKUP index 0, also `rules.PAD_LAYER`
_BOTTOM_LAYER = 3  # B.Cu -- DEFAULT_STACKUP index 3, the last signal layer


def _graph(*, bottom: bool, blocker: bool) -> dict[str, object]:
    """U1 (top) at (0, 0) and U2 (``bottom`` ? bottom : top) at (5, 0),
    one net between them.

    ``blocker`` adds a THIRD instance, top-mounted, at U2's EXACT (x, y)
    on a foreign net -- real copper on F.Cu at the coordinate U2's pad
    sits at, which the pre-fix router would have claimed U2's own pad
    onto too (both nets stamped on ``PAD_LAYER`` regardless of U2's real
    ``inst_bottom`` side) and which the fixed router never visits at all
    when ``bottom`` is set, since U2's own pad is then claimed and
    searched on B.Cu, the layer it is actually on."""
    u2: dict[str, object] = {"refdes": "U2", "x": 5.0, "y": 0.0}
    if bottom:
        u2["layer"] = "bottom"
    instances: list[dict[str, object]] = [{"refdes": "U1", "x": 0.0, "y": 0.0}, u2]
    if blocker:
        instances.append({"refdes": "BLOCKER", "x": 5.0, "y": 0.0})
    nets: list[dict[str, object]] = [
        {
            "name": "N1",
            "members": [
                {"refdes": "U1", "pin": "1"},
                {"refdes": "U2", "pin": "1"},
            ],
        }
    ]
    if blocker:
        # Single member -- no segment, just a real claimed F.Cu pad at
        # U2's coordinates for a net U1/U2's own connection never touches.
        nets.append({"name": "N2", "members": [{"refdes": "BLOCKER", "pin": "1"}]})
    return {"instances": instances, "nets": nets}


def test_bottom_mounted_pad_routes_through_a_top_side_blocker_onto_b_cu():
    """(a) U2's real pad is on B.Cu, free the whole time; BLOCKER only
    ever claims F.Cu at that (x, y). Before the fix both U2's and
    BLOCKER's pads were claimed/searched on F.Cu (`PAD_LAYER`), so the
    two nets contest the identical F.Cu cell and U2's own goal cell ends
    up foreign-owned -- ``no_path``, on a board with nothing physically
    wrong. After the fix U2's pad lives where it really is (B.Cu), the
    connection reaches it there, and BLOCKER's F.Cu copper is simply
    never in the way.
    """
    ir = from_graph(_graph(bottom=True, blocker=True), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=RealizeConfig(router="maze"))

    assert result.unrouted == (), (
        f"N1 failed to route with a real B.Cu landing spot free the whole "
        f"time: {result.unrouted!r} / reasons={result.unrouted_reasons!r}"
    )
    assert result.tracks, "N1 must have drawn real copper"

    # The track/via geometry must actually REACH B.Cu at U2's pad (5, 0):
    # either a track segment drawn on B.Cu ending there, or a via whose
    # layer span includes B.Cu landing there.
    def _hits_bottom_pad(x: float, y: float) -> bool:
        return abs(x - 5.0) < 1e-6 and abs(y - 0.0) < 1e-6

    track_reaches = any(
        t.layer == _BOTTOM_LAYER
        and any(
            _hits_bottom_pad(float(seg["start"][0]), float(seg["start"][1]))
            or _hits_bottom_pad(float(seg["end"][0]), float(seg["end"][1]))
            for seg in t.segments
        )
        for t in result.tracks
    )
    via_reaches = any(
        _hits_bottom_pad(v.x, v.y) and v.layer_lo <= _BOTTOM_LAYER <= v.layer_hi
        for v in result.vias
    )
    assert track_reaches or via_reaches, (
        f"N1 routed but never actually reached B.Cu at U2's real pad "
        f"(5, 0): tracks={result.tracks!r} vias={result.vias!r}"
    )


def test_top_only_board_still_routes_on_the_top_layer_unchanged():
    """(b) Baseline: both pins top-mounted (no ``inst_bottom`` instance at
    all) must realize exactly as before -- a single-layer F.Cu track, no
    via, nothing about a same-side board's routing changed by this fix."""
    ir = from_graph(_graph(bottom=False, blocker=False), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=RealizeConfig(router="maze"))

    assert result.unrouted == ()
    assert result.tracks, "N1 must have drawn real copper"
    assert result.vias == (), "a same-side connection must need no layer change"
    assert all(t.layer == _TOP_LAYER for t in result.tracks), (
        f"a top-only board's track(s) must stay on F.Cu, unchanged from "
        f"before this fix: {result.tracks!r}"
    )


# ── SMD vs. drilled (THT) pad claims ─────────────────────────────────────

# A narrow strip (same construction as
# tests/test_pcb_fixed_copper_route.py's own fixed-copper "WALL" test):
# with no vertical room to detour, a claim spanning the strip's full
# height is a genuine total block on whichever layer(s) it covers, not
# merely a tight squeeze.
_WALL_OUTLINE = [(-2.0, -1.0), (12.0, -1.0), (12.0, 1.0), (-2.0, 1.0)]


def _wall_graph() -> dict[str, object]:
    """U1 (top, x=0) -- BLOCKER (x=5, dead centre of the strip) -- U2
    (bottom, x=10), one net between U1/U2. BLOCKER sits on its own
    single-member net (N2, no segment): nothing to route, just a real
    claimed pad in the way."""
    return {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "BLOCKER", "x": 5.0, "y": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "N1",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
            {"name": "N2", "members": [{"refdes": "BLOCKER", "pin": "1"}]},
        ],
    }


def _blocker_footprint(*, drilled: bool) -> dict[str, Any]:
    """BLOCKER's one real pad: a 3mm circle (enclosing radius ~2.1mm,
    comfortably wider than the strip's own 2mm height) at its own local
    origin -- large enough that its claim spans the strip's full height
    with no vertical gap on either side, on whichever layer(s) it
    claims."""
    pad: dict[str, Any] = {
        "number": "1",
        "x": 0.0,
        "y": 0.0,
        "w": 3.0,
        "h": 3.0,
        "shape": "circle",
    }
    if drilled:
        pad["drill"] = 1.0
    return {"pads": [pad], "pin_map": {"1": {"name": "1"}}}


def test_a_drilled_blocker_pad_walls_off_the_route_on_every_layer():
    """A drilled/THT pad's claim spans EVERY board layer -- with no
    vertical room in the strip to detour around it and no layer offering
    a free crossing, N1 must fail outright (never draw a track through
    the hole), and the diagnosis must say so honestly (``no_path``, not
    a width/congestion excuse — see :func:`_diagnose_unrouted`)."""
    ir = from_graph(_wall_graph(), stackup=DEFAULT_STACKUP, outline=_WALL_OUTLINE)
    result = realize(
        ir,
        config=RealizeConfig(router="maze"),
        footprints={"BLOCKER": _blocker_footprint(drilled=True)},
    )
    assert result.unrouted != (), (
        f"a drilled pad must block every layer -- N1 had no legal corridor "
        f"and must not have routed through the hole: tracks={result.tracks!r}"
    )
    reason = next(r for r in result.unrouted_reasons if r.seg_id == 0)
    assert reason.kind == "no_path", reason.message
    assert not any(
        abs(pt[0] - 5.0) < 2.5
        for t in result.tracks
        for seg in t.segments
        for pt in (seg["start"], seg["end"])
    ), "no track may pass anywhere near the drilled hole's own claimed radius"


def test_an_smd_blocker_pad_only_claims_its_own_layer_so_b_cu_passes_underneath():
    """The SAME geometry with an SMD pad instead: BLOCKER claims F.Cu
    ONLY (top-mounted, no ``drill``), so N1 must still realize — forced
    onto B.Cu (free the whole width of the strip) before it can cross
    BLOCKER's own (x, y), then continuing on B.Cu to U2's real pad. This
    is the "may run under an SMD pad" half of the same rule the drilled
    case above enforces the opposite of."""
    ir = from_graph(_wall_graph(), stackup=DEFAULT_STACKUP, outline=_WALL_OUTLINE)
    result = realize(
        ir,
        config=RealizeConfig(router="maze"),
        footprints={"BLOCKER": _blocker_footprint(drilled=False)},
    )
    assert result.unrouted == (), (
        f"an SMD blocker pad claims F.Cu only -- B.Cu stays free underneath "
        f"it and N1 must still realize: {result.unrouted_reasons!r}"
    )

    def _crosses_x5(seg: dict[str, Any]) -> bool:
        x0, x1 = float(seg["start"][0]), float(seg["end"][0])
        return min(x0, x1) <= 5.0 <= max(x0, x1)

    assert any(
        t.layer == _BOTTOM_LAYER and any(_crosses_x5(seg) for seg in t.segments)
        for t in result.tracks
    ), (
        f"N1 must cross the blocker's own (x, y) on B.Cu, UNDER its F.Cu "
        f"pad, not detour around it (there is no room to, in this strip): "
        f"tracks={result.tracks!r}"
    )
