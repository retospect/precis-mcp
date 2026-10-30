"""gr524392 — the wire guard's pad set IS the DRC's pad set.

``TopoDesign.pin_xy``/``pin_wh`` describe one pad per pin — the
primary. :func:`precis.pcb.realize.pads_for_ir` hands the DRC the
whole :func:`~precis.pcb.ir.footprint_pad_set`: a pin's second
same-named land, a split slug half, an unpinned land. The guard
measured pad clearance against the pin pads only, so it passed a
track 0.36mm clear of the nearest pad it knew while ``view=check``
errored on a pad it could not see. ``design_from_ir`` now carries
``pad_extra_*`` and :func:`~precis.pcb.topoplace.wires.guard_pad_rects`
places them; this test asserts the two enumerations agree pad-for-pad,
per part and layer — the gripe's ask verbatim.

``test_realise_pad_polys_*`` holds the same contract on
:func:`~precis.pcb.topoplace.realise._pad_polys` — the pad set the
emit guard (``_guarded_nets``) and the corner/join checks actually
measure, placed through the route pose rather than read off ``ir`` —
and sweeps every footprint in the cached sets the fixtures carry.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from shapely.geometry import Point

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.realize import pads_for_ir
from precis.pcb.session import apply_real_pin_offsets
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import _pad_polys
from precis.pcb.topoplace.wires import guard_pad_rects

_LAYERS = [str(layer["name"]) for layer in DEFAULT_STACKUP]
_ALL_LAYERS = (1 << len(_LAYERS)) - 1
_LAYER_IDX = {name: i for i, name in enumerate(_LAYERS)}


def _pad(number: str, x: float, y: float, w: float, h: float, **extra: Any) -> dict:
    return {
        "number": number,
        "shape": "RECT",
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        **extra,
    }


#: A VQFN-style part shaped like the gripe's BQ25504RGTR: one pin name
#: on TWO lands (the VSS lead and the exposed thermal slug — the
#: "guard sees (0.75, 1.5), check measures (-1.43, 0.61)" discrepancy),
#: an unpinned NC land, and a drilled shield name on two lands.
_QFN_FP: dict[str, Any] = {
    "pads": [
        _pad("1", -0.75, 1.50, 0.40, 0.25),  # VSS lead (primary)
        _pad("2", 0.75, 1.50, 0.40, 0.25),  # VOC_SAMP lead
        _pad("3", -0.75, -1.50, 0.40, 0.25),  # VIN_DC lead
        _pad("4", -1.43, 0.61, 0.90, 1.60),  # VSS slug — extra, pinned
        _pad("5", 0.00, -1.80, 1.00, 0.40),  # NC land — extra, unpinned
        _pad("6", 0.00, 1.80, 0.80, 0.80, drill=0.3),  # SH land (primary, TH)
        _pad("7", 1.80, -1.80, 0.80, 0.80, drill=0.3),  # SH land 2 — extra, TH
    ],
    "pin_map": {
        "1": {"name": "VSS"},
        "2": {"name": "VOC_SAMP"},
        "3": {"name": "VIN_DC"},
        "4": {"name": "VSS"},
        "6": {"name": "SH"},
        "7": {"name": "SH"},
        # "5" has no pin_map name and no declared pin — net-less copper.
    },
}


def _graph() -> dict[str, Any]:
    return {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1", "x": 10.0, "y": 10.0, "rot": 90.0},
            {
                "refdes": "U2",
                "part_lcsc": "C1",
                "x": 20.0,
                "y": 10.0,
                "layer": "bottom",
            },
            {"refdes": "J1", "x": 30.0, "y": 10.0},
        ],
        "nets": [
            {
                "name": "GND",
                "members": [
                    {"refdes": "U1", "pin": "VSS"},
                    {"refdes": "U2", "pin": "VSS"},
                    {"refdes": "J1", "pin": "1"},
                ],
            },
            {
                "name": "SIG",
                "members": [
                    {"refdes": "U1", "pin": "VOC_SAMP"},
                    {"refdes": "U2", "pin": "VOC_SAMP"},
                ],
            },
            {
                "name": "PWR",
                "members": [
                    {"refdes": "U1", "pin": "VIN_DC"},
                    {"refdes": "J1", "pin": "2"},
                ],
            },
        ],
        "unconnected": [
            {"refdes": "U1", "pin": "SH"},
            {"refdes": "U2", "pin": "VIN_DC"},
            {"refdes": "U2", "pin": "SH"},
        ],
    }


def _guard_pads(td, mesh) -> dict[str, list[tuple[float, ...]]]:
    """The guard's pad set keyed by refdes: ``(cx, cy, w, h, copper)``."""
    pads = guard_pad_rects(mesh, td, td.n_layers, td.pose0)
    out: dict[str, list[tuple[float, ...]]] = {}
    for i in range(len(pads.pin)):
        c = (pads.lo[i] + pads.hi[i]) / 2.0
        wh = pads.hi[i] - pads.lo[i]
        out.setdefault(str(td.parts[int(pads.part[i])]), []).append(
            (
                float(c[0]),
                float(c[1]),
                float(wh[0]),
                float(wh[1]),
                float(pads.copper[i]),
            )
        )
    return out


def test_guard_pad_rects_equal_pads_for_ir() -> None:
    ir = from_graph(
        _graph(),
        stackup=DEFAULT_STACKUP,
        outline=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
    )
    footprints = {"U1": _QFN_FP, "U2": _QFN_FP}
    apply_real_pin_offsets(ir, footprints)
    td = design_from_ir(ir, footprints)
    mesh = build_mesh(td, td.pose0)
    guard = _guard_pads(td, mesh)

    # J1 carries no cached footprint: two synthesized bounds a side.
    assert len(td.pad_extra_xy) == 6, (
        "U1+U2 slug, NC land and second SH land — three extras each"
    )

    unmatched: list[str] = []
    seen: set[tuple[str, int]] = set()
    n_check = 0
    for pad in pads_for_ir(ir, _LAYERS, footprints):
        refdes = str(pad["refdes"])
        if refdes.startswith("MH"):
            continue  # plated-hole annuli are zones in TopoDesign, not pads
        n_check += 1
        cands = guard.get(refdes) or []
        hit = -1
        for i, (cx, cy, w, h, _copper) in enumerate(cands):
            if (refdes, i) in seen:
                continue
            if abs(cx - float(pad["x"])) <= 1e-4 and abs(cy - float(pad["y"])) <= 1e-4:
                if (
                    abs(w - float(pad["w"])) <= 1e-4
                    and abs(h - float(pad.get("h", pad["w"]))) <= 1e-4
                ):
                    hit = i
                    break
        if hit < 0:
            unmatched.append(f"{refdes}.{pad['pin']} @ ({pad['x']}, {pad['y']})")
            continue
        seen.add((refdes, hit))
        copper = int(cands[hit][4])
        layer_idx = _LAYERS.index(str(pad["layer"]))
        if pad.get("drill") is not None:
            assert copper == _ALL_LAYERS, (
                f"{refdes}.{pad['pin']}: drilled land must block every layer"
            )
        else:
            assert copper == 1 << layer_idx, (
                f"{refdes}.{pad['pin']}: guard copper {copper:#b} misses "
                f"check layer {pad['layer']}"
            )

    assert not unmatched, "check pads no guard pad measures: " + ", ".join(unmatched)
    n_guard = sum(len(v) for v in guard.values())
    assert n_guard == n_check == len(seen), (
        f"guard {n_guard} vs check {n_check} pads — one source, one count"
    )


def test_extra_pad_pin_attribution() -> None:
    """A pinned extra shares its pin (its net's own copper — the own-pad
    exemption opens it); an unpinned land is ``-1``, foreign to every
    net, the ``net=""`` the DRC pad set gives it."""
    ir = from_graph(
        _graph(),
        stackup=DEFAULT_STACKUP,
        outline=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
    )
    footprints = {"U1": _QFN_FP}
    apply_real_pin_offsets(ir, footprints)
    td = design_from_ir(ir, footprints)

    u1 = td.parts.index("U1")
    keep = np.flatnonzero(td.pad_extra_part == u1)
    pins = [int(td.pad_extra_pin[k]) for k in keep]
    labels = {
        str(td.pin_label[int(td.pad_extra_pin[k])])
        for k in keep
        if int(td.pad_extra_pin[k]) >= 0
    }
    assert labels == {"VSS", "SH"}
    assert pins.count(-1) == 1, "exactly the NC land is foreign to all nets"
    sides = {int(td.pad_extra_side[k]) for k in keep}
    assert sides == {0, 2}, "SMD extras mount-side; the drilled land TH"


#: The real ``part_footprints`` row for the gripe's BQ25504RGTR (LCSC
#: C2876746, board taz-leaf-local-8): VSS is TWO lands — pads "1" and
#: "13", the second naming the pin in ``pin_map`` and arriving first in
#: array order, so it wins ``primary`` — and pad "17" is the 1.7x1.7
#: exposed slug named "EP". The gripe's numbers, verbatim: the guard's
#: pin roster put U2/VSS at (0.75, 1.5) — pad "13" — while the check
#: measured the track against pad "1" near (-1.5, 0.75), a same-named
#: land the roster never enumerated.
_BQ_PADS = [
    {"number": "17", "shape": "rect", "x": 0.0001, "y": -0.0001, "w": 1.7, "h": 1.7},
    {"number": "16", "shape": "obround", "x": -0.75, "y": 1.5, "w": 0.28, "h": 0.8},
    {"number": "15", "shape": "obround", "x": -0.25, "y": 1.5, "w": 0.28, "h": 0.8},
    {"number": "14", "shape": "obround", "x": 0.25, "y": 1.5, "w": 0.28, "h": 0.8},
    {"number": "13", "shape": "obround", "x": 0.75, "y": 1.5, "w": 0.28, "h": 0.8},
    {"number": "12", "shape": "obround", "x": 1.5, "y": 0.75, "w": 0.8, "h": 0.28},
    {"number": "11", "shape": "obround", "x": 1.5, "y": 0.25, "w": 0.8, "h": 0.28},
    {"number": "10", "shape": "obround", "x": 1.5, "y": -0.25, "w": 0.8, "h": 0.28},
    {"number": "9", "shape": "obround", "x": 1.5, "y": -0.75, "w": 0.8, "h": 0.28},
    {"number": "8", "shape": "obround", "x": 0.75, "y": -1.5, "w": 0.28, "h": 0.8},
    {"number": "7", "shape": "obround", "x": 0.25, "y": -1.5, "w": 0.28, "h": 0.8},
    {"number": "6", "shape": "obround", "x": -0.25, "y": -1.5, "w": 0.28, "h": 0.8},
    {"number": "5", "shape": "obround", "x": -0.75, "y": -1.5, "w": 0.28, "h": 0.8},
    {"number": "4", "shape": "obround", "x": -1.5, "y": -0.75, "w": 0.8, "h": 0.28},
    {"number": "3", "shape": "obround", "x": -1.5, "y": -0.25, "w": 0.8, "h": 0.28},
    {"number": "2", "shape": "obround", "x": -1.5, "y": 0.25, "w": 0.8, "h": 0.28},
    {"number": "1", "shape": "obround", "x": -1.5, "y": 0.75, "w": 0.8, "h": 0.28},
]
_BQ_NAMES = [
    "VSS",
    "VIN_DC",
    "VOC_SAMP",
    "VREF_SAMP",
    "OT_PROG",
    "VBAT_OV",
    "VRDIV",
    "VBAT_UV",
    "OK_HYST",
    "OK_PROG",
    "VBAT_OK",
    "AVSS",
    "VSS",
    "VBAT",
    "VSTOR",
    "LBST",
    "EP",
]
_BQ_FP: dict[str, Any] = {
    "pads": _BQ_PADS,
    "pin_map": {str(i + 1): {"name": n} for i, n in enumerate(_BQ_NAMES)},
}


def _bq_graph(**pose: Any) -> dict[str, Any]:
    """U2 the BQ25504 plus a two-pin header: VSS and the EP slug on GND,
    VIN_DC on VIN — every other footprint pin declared unconnected so
    the pin/pad join succeeds and the extras enumerate."""
    inst = {"refdes": "U2", "part_lcsc": "C2876746", "x": 20.0, "y": 10.0}
    inst.update(pose)
    return {
        "instances": [inst, {"refdes": "J1", "x": 30.0, "y": 10.0}],
        "nets": [
            {
                "name": "GND",
                "members": [
                    {"refdes": "U2", "pin": "VSS"},
                    {"refdes": "U2", "pin": "EP"},
                    {"refdes": "J1", "pin": "1"},
                ],
            },
            {
                "name": "VIN",
                "members": [
                    {"refdes": "U2", "pin": "VIN_DC"},
                    {"refdes": "J1", "pin": "2"},
                ],
            },
        ],
        "unconnected": [
            {"refdes": "U2", "pin": n}
            for n in _BQ_NAMES
            if n not in ("VSS", "EP", "VIN_DC")
        ],
    }


def _fold_pose0(ir: Any, td: Any) -> None:
    """Write ``td.pose0`` back onto ``ir`` — the state the route stage
    and the check gate actually share (the job mutates ``ir`` to the
    settled pose, then both measure it)."""
    for i in range(len(td.parts)):
        x, y, rot_q, side = (float(v) for v in td.pose0[i])
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        ir.inst_x[i] = x
        ir.inst_y[i] = y
        ir.inst_rot[i] = rot_q * 90.0
        ir.inst_bottom[i] = side >= 0.5


def _assert_pad_polys_equal_pads_for_ir(
    ir: Any, footprints: dict[str, dict[str, Any]], td: Any
) -> None:
    """``_pad_polys`` (the emit guard's pad set) ≡ ``pads_for_ir`` (the
    check gate's): every check pad's centre sits inside a guard poly on
    the same layer set, that poly's bbox reads the same ``w``x``h`` and
    its tag the same net — and the reverse, so the enumeration agrees
    pad-for-pad, per pin name, not merely in count."""
    n_layers = ir.n_layers
    pose = td.pose0
    guard = _pad_polys(td, ir, n_layers, pose)
    check = pads_for_ir(ir, _LAYERS, footprints)
    labels_of: dict[int, set[str]] = {}
    for pid in range(ir.n_pins):
        labels_of.setdefault(int(ir.pin_instance[pid]), set()).add(
            str(ir.pin_label[pid])
        )
    part_of_pin = [int(td.pin_part[p]) for p in range(len(td.pin_xy))]
    part_name = [str(r) for r in td.parts]

    matched: set[tuple[int, int]] = set()
    n_check = 0
    for pad in check:
        refdes = str(pad["refdes"])
        if refdes.startswith("MH"):
            continue  # plated-hole annuli are zones in TopoDesign, not pads
        n_check += 1
        x, y = float(pad["x"]), float(pad["y"])
        w, h = float(pad["w"]), float(pad.get("h", pad["w"]))
        lays = (
            range(n_layers)
            if pad.get("drill") is not None
            else [_LAYER_IDX[str(pad["layer"])]]
        )
        # A drilled land is ONE check row but blocks every layer — the
        # guard holds one poly per layer, so a hit is owed on each.
        for lay in lays:
            hit = -1
            for gi, (pname, poly, pin) in enumerate(guard[lay]):
                if (lay, gi) in matched:
                    continue
                if poly.distance(Point(x, y)) > 1e-4:
                    continue
                gx0, gy0, gx1, gy1 = poly.bounds
                gw, gh = gx1 - gx0, gy1 - gy0
                if not (
                    (abs(gw - w) <= 1e-3 and abs(gh - h) <= 1e-3)
                    or (abs(gw - h) <= 1e-3 and abs(gh - w) <= 1e-3)
                ):
                    continue
                assert str(pname) == str(pad["net"] or ""), (
                    f"{refdes}.{pad['pin']}: guard net {pname!r} "
                    f"!= check net {pad['net']!r}"
                )
                if pin >= 0:
                    assert str(td.pin_label[pin]) == str(pad["pin"]), (
                        f"{refdes}.{pad['pin']}: guard names it "
                        f"{part_name[part_of_pin[pin]]}.{td.pin_label[pin]}"
                    )
                else:
                    assert str(pad["pin"]) not in labels_of.get(
                        part_name.index(refdes), ()
                    ), f"{refdes}.{pad['pin']}: declared pin read as net-less land"
                hit = gi
                break
            assert hit >= 0, (
                f"{refdes}.{pad['pin']} net={pad['net']} @({x}, {y}) "
                f"{w}x{h} layer={lay}: no guard pad measures it"
            )
            matched.add((lay, hit))
    n_guard = sum(len(v) for v in guard.values())
    assert n_guard == len(matched), (
        f"guard {n_guard} pads vs {len(matched)} matched check pads "
        f"({n_check} rows) — one source, one count"
    )


def test_realise_pad_polys_equal_pads_for_ir() -> None:
    """The emit guard's pad set is the check's, on both fixture shapes:
    the synthetic VQFN (two VSS lands, an NC land, two SH lands) and
    the gripe's own BQ25504RGTR row (VSS on pads 1+13, EP slug on 17) —
    the second same-named land being exactly the copper the pre-``pad_extra``
    guard never measured."""
    for graph, footprints in (
        (_graph(), {"U1": _QFN_FP, "U2": _QFN_FP}),
        (_bq_graph(rot=90.0, layer="bottom"), {"U2": _BQ_FP}),
    ):
        ir = from_graph(
            graph,
            stackup=DEFAULT_STACKUP,
            outline=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
        )
        apply_real_pin_offsets(ir, footprints)
        td = design_from_ir(ir, footprints)
        _fold_pose0(ir, td)
        _assert_pad_polys_equal_pads_for_ir(ir, footprints, td)


def test_realise_pad_polys_cover_cached_footprints() -> None:
    """The same equality swept over every footprint row the test
    fixtures cache — ``nano_usb_pd_testboard``'s catalog stand-ins and
    its design-local rows — at a rotated and a mirrored pose each, so a
    mirror/rot regression in either enumeration has nowhere to hide."""
    from precis.pcb import rows as pcb_rows
    from precis.pcb.padplace import pad_label
    from tests.fixtures.pcb import nano_usb_pd_testboard as nano_fx

    frows: list[dict[str, Any]] = list(nano_fx.CATALOG_FOOTPRINTS.values())
    for f_ in nano_fx.FOOTPRINTS:
        _name, data = pcb_rows.normalize_footprint(f_)
        frows.append(data)

    for fp in frows:
        pin_map = fp.get("pin_map") or {}
        labels = sorted({pad_label(p, pin_map) for p in fp.get("pads") or []} - {""})
        assert labels, "fixture footprint with no pads"
        for rot, layer in ((0.0, "top"), (90.0, "top"), (270.0, "bottom")):
            graph = {
                "instances": [
                    {
                        "refdes": "U1",
                        "x": 20.0,
                        "y": 10.0,
                        "rot": rot,
                        "layer": layer,
                    }
                ],
                "nets": [],
                "unconnected": [{"refdes": "U1", "pin": name} for name in labels],
            }
            ir = from_graph(
                graph,
                stackup=DEFAULT_STACKUP,
                outline=[[0.0, 0.0], [40.0, 0.0], [40.0, 20.0], [0.0, 20.0]],
            )
            footprints = {"U1": fp}
            apply_real_pin_offsets(ir, footprints)
            td = design_from_ir(ir, footprints)
            _fold_pose0(ir, td)
            _assert_pad_polys_equal_pads_for_ir(ir, footprints, td)
