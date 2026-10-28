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
"""

from __future__ import annotations

from typing import Any

import numpy as np

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.realize import pads_for_ir
from precis.pcb.session import apply_real_pin_offsets
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.wires import guard_pad_rects

_LAYERS = [str(layer["name"]) for layer in DEFAULT_STACKUP]
_ALL_LAYERS = (1 << len(_LAYERS)) - 1


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
