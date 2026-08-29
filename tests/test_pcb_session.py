"""Unit tests for ``precis.pcb.session`` — no DB. Focused on
:func:`~precis.pcb.session.footprints_by_refdes`, the join between
:meth:`~precis.store.Store.pcb_footprints_for`'s C-number-keyed cache and
:func:`~precis.pcb.realize.pad_geometry`'s refdes-keyed ``footprints`` arg
(the missing link the "pads must be precisely what the footprint says"
task closes: :attr:`~precis.pcb.ir.PcbIR.instance_part_lcsc` is the ONLY
thing on the IR side that knows an instance's C-number).
"""

from __future__ import annotations

from typing import Any

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.session import footprints_by_refdes, pin_swap_diff

_FP = {"pads": [{"number": "1", "x": 0.0, "y": 0.0, "w": 1.0, "shape": "RECT"}]}


def _graph():
    return {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C2838500"},
            {"refdes": "C1", "part_lcsc": "C1525"},
            {"refdes": "J1"},  # no catalog part at all
        ],
        "nets": [
            {"name": "N1", "members": [{"refdes": "U1", "pin": "1"}]},
            {"name": "N2", "members": [{"refdes": "C1", "pin": "1"}]},
            {"name": "N3", "members": [{"refdes": "J1", "pin": "1"}]},
        ],
    }


def test_footprints_by_refdes_remaps_cached_lcsc_rows_onto_refdes():
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    footprints_by_lcsc = {"C2838500": _FP}
    out = footprints_by_refdes(ir, footprints_by_lcsc)
    assert out == {"U1": _FP}


def test_footprints_by_refdes_omits_instances_with_no_cache_hit():
    """C1 has a real ``part_lcsc`` but no cached row yet, and J1 has no
    linked catalog part at all -- both are simply absent from the result
    (never a KeyError, never an invented entry); the caller
    (:func:`precis.pcb.realize.pad_geometry`) already treats "absent" as
    "fall back to synthesized" for exactly this reason."""
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    out = footprints_by_refdes(ir, {})
    assert out == {}


def test_footprints_by_refdes_is_a_pure_remap_not_a_size_computation():
    """The function only remaps the KEY (C-number -> refdes) -- the VALUE
    passes through byte-identical, since :func:`precis.pcb.realize.
    pad_geometry` (not this function) is what turns a cached row into
    real per-pin geometry."""
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    footprints_by_lcsc: dict[str, dict[str, Any]] = {
        "C2838500": _FP,
        "C1525": {"pads": []},
    }
    out = footprints_by_refdes(ir, footprints_by_lcsc)
    assert out["U1"] is _FP
    # C1's cached row has no pads -- still remapped verbatim; whether an
    # empty `pads` list counts as "no real data" is `pad_geometry`'s own
    # call (its docstring: `fp and fp.get("pads")`), not this function's.
    assert out["C1"] == {"pads": []}


# ── pin_swap_diff must not let NO_NET (-1) wrap-index into net_name ─────
# (found while wiring `part_lcsc` through this same module, and reported
# by a sibling agent as the identical sentinel-collision defect it hit in
# `realize.pads_for_ir` and `maze.FREE`: `NO_NET == -1` is also a valid
# Python/numpy index, so `ir.net_name[NO_NET]` doesn't raise -- it
# silently wraps to the LAST real net in the array.)
def _no_net_swap_graph():
    """U1 has three degree-0 pins (every net here has exactly one member,
    so `from_graph` creates the pin but no segment/dart for it) -- the
    equal-rotation-CSR-degree precondition `PcbIR.swap_pins` enforces is
    satisfied trivially, which is what lets this fixture swap a REAL net
    onto NO_NET without needing any placed geometry at all. `NET_LAST` is
    a SECOND, distinct net so a wrap-around bug reads back a wrong-but-
    real net name instead of accidentally matching the correct one -- with
    only one net in the graph the bug and the fix would look identical."""
    return {
        "instances": [{"refdes": "U1"}],
        "nets": [
            {"name": "NET_A", "members": [{"refdes": "U1", "pin": "A"}]},
            {"name": "NET_LAST", "members": [{"refdes": "U1", "pin": "C"}]},
        ],
        "unconnected": [{"refdes": "U1", "pin": "B"}],
    }


def test_pin_swap_diff_reports_empty_net_not_a_wrapped_last_net_name():
    ir = from_graph(_no_net_swap_graph(), stackup=DEFAULT_STACKUP)
    pin_a = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "A")
    pin_b = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "B")
    baseline = ir.pin_net.copy()
    ir.swap_pins(pin_a, pin_b)  # NET_A's pin now sits at NO_NET, and vice versa

    diff = pin_swap_diff(ir, baseline)
    by_pin = {d["pin"]: d for d in diff}
    assert by_pin["A"]["net"] == "", (
        "pin A moved OFF its net onto NO_NET -- must read back as an empty "
        "net, never a real (wrong) net name"
    )
    assert by_pin["B"]["net"] == "NET_A"
