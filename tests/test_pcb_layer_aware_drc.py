"""gripe gr341516 — DRC clearance/courtyard checks ignored board SIDE.

``rules.py::PAD_LAYER`` used to force every pad's IR ``layer`` to index 0
(F.Cu) regardless of the owning instance's real ``pcb_instances.layer``
(top/bottom) — so a bottom-side part placed directly under a top-side one
(an EWOD sink grid's whole point, ``generators.py``'s own module
docstring) read to DRC as sitting on the SAME copper as whatever was
above it. Fixed by giving the IR a real per-instance side
(:attr:`precis.pcb.ir.PcbIR.inst_bottom`, populated by
:func:`precis.pcb.ir.from_graph` off the SAME ``pcb_instances.layer``
string :func:`precis.pcb.padplace.is_bottom_instance` already parses for
the real gerber-export path), and teaching :func:`precis.pcb.realize.
pads_for_ir` to emit each pad's own outer layer off that field instead of
the retired constant.

These tests pin the DRC-side consequences directly, at the smallest
model that exercises each one — :func:`precis.pcb.drc.clearance_pairs_
indexed` (via :func:`~precis.pcb.drc.check_clearance`), a drilled
(through-hole) pad's "spans every layer" exemption from that same
side-awareness, and :func:`precis.pcb.drc.check_courtyard_overlap`'s new
``bottom_by_refdes`` — plus one end-to-end check against the real
``ewod-dogfood-1`` fixture (:mod:`tests.test_pcb_ewod_dogfood`), the
board this gap was originally measured on.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP, drc
from precis.pcb.capabilities import capability_for
from tests.test_pcb_drc import _pad, _square
from tests.test_pcb_ewod_dogfood import _seed

_CAP4 = capability_for("4layer")
_LAYERS = [
    layer["name"] for layer in DEFAULT_STACKUP
]  # ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


@pytest.fixture
def pcb(store):
    # Same one-line fixture every other `tests/test_pcb_*.py` module
    # defines locally (never cross-imported — that pattern is how a
    # `pcb` fixture parameter and a same-named import collide under
    # ruff's F811). `_seed` (below) IS imported, since it is real
    # fixture-building logic worth not duplicating.
    return PcbHandler(hub=Hub(store=store))


# ── (a) same (x, y), opposite sides -- clearance is a same-layer-only rule ─


def test_clearance_ignores_a_foreign_net_pad_directly_below_on_the_other_side():
    """Two pads at the SAME (x, y), different nets, well closer than the
    fab's minimum trace spacing -- a hard clearance error if they were
    coplanar. On OPPOSITE board sides they are not: they never touch, no
    matter how close in plan view, because there is a whole board's worth
    of dielectric between them."""
    top = _pad("A", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    bottom = _pad("B", "B.Cu", 0.0, 0.0, w=1.0, h=1.0)
    model = {"layers": _LAYERS, "copper": [], "pads": [top, bottom]}
    assert drc.check_clearance(model, _CAP4) == []


def test_clearance_still_fires_when_the_same_pair_is_both_on_top():
    """The control for the test above: identical geometry, SAME side --
    must still fire. Proves the quiet result above is the side exemption
    doing its job, not some other change silencing the rule outright."""
    top_a = _pad("A", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    top_b = _pad("B", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    model = {"layers": _LAYERS, "copper": [], "pads": [top_a, top_b]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"


# ── (b) a drilled (through-hole) pad spans every layer regardless ────────


def test_clearance_a_through_hole_pad_still_contends_against_the_far_side():
    """A THT pad's plated land is real copper on every board layer, not
    only the single outer side :func:`precis.pcb.realize.pads_for_ir`
    reports for it (that field means "this pad's own OUTER flash", see
    its own docstring) -- so it must still contend for clearance against
    a foreign-net pad on the OTHER side, unlike the plain SMD case above."""
    tht = _pad("A", "F.Cu", 0.0, 0.0, w=1.0, h=1.0, drill=0.3)
    bottom = _pad("B", "B.Cu", 0.0, 0.0, w=1.0, h=1.0)
    model = {"layers": _LAYERS, "copper": [], "pads": [tht, bottom]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_via_pad_keepout_a_through_hole_pad_is_never_excluded_by_the_vias_span():
    """The keep-out twin of the clearance test above --
    :func:`drc.check_via_pad_keepout` used to skip a pad the via's own
    layer span never reached; a drilled pad's land is on every layer, so
    it must never be excluded on that basis."""
    tht = _pad("SIG", "B.Cu", 0.0, 0.0, w=1.0, h=1.0, drill=0.3)
    via = {
        "ctype": "via",
        "net": "SIG",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],  # never reaches B.Cu -- would have excluded a plain SMD pad
    }
    model = {"layers": _LAYERS, "copper": [via], "pads": [tht]}
    findings = drc.check_via_pad_keepout(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"


# ── (c) courtyard overlap is a same-side-only rule when told the sides ───


def test_courtyard_overlap_fires_without_side_information_by_default():
    """``bottom_by_refdes=None`` (the default) is the pre-gr341516
    behaviour, unchanged -- a caller that never resolved sides still gets
    the old, side-blind answer."""
    overlapping = [("U1", _square(0.0, 0.0)), ("U2", _square(0.5, 0.0))]
    findings = drc.check_courtyard_overlap(overlapping)
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_courtyard_overlap_opposite_sides_never_flagged():
    """A bottom-side part directly under a top-side one (an EWOD sink grid's
    whole point) is not two parts colliding."""
    overlapping = [("U1", _square(0.0, 0.0)), ("U2", _square(0.5, 0.0))]
    bottom_by_refdes = {"U1": False, "U2": True}
    assert (
        drc.check_courtyard_overlap(overlapping, bottom_by_refdes=bottom_by_refdes)
        == []
    )


def test_courtyard_overlap_same_side_pair_still_flagged():
    """The control: identical geometry, same resolved side -- must still
    fire even with ``bottom_by_refdes`` supplied."""
    overlapping = [("U1", _square(0.0, 0.0)), ("U2", _square(0.5, 0.0))]
    both_top = {"U1": False, "U2": False}
    findings = drc.check_courtyard_overlap(overlapping, bottom_by_refdes=both_top)
    assert len(findings) == 1
    assert findings[0].severity == "error"

    both_bottom = {"U1": True, "U2": True}
    findings = drc.check_courtyard_overlap(overlapping, bottom_by_refdes=both_bottom)
    assert len(findings) == 1


def test_courtyard_overlap_a_refdes_absent_from_the_map_defaults_to_top():
    """Same default :func:`precis.pcb.padplace.is_bottom_instance` itself
    uses for an unset ``layer`` -- a caller's map that only resolved ONE
    of the two refdes still gets a meaningful answer, not a KeyError."""
    overlapping = [("U1", _square(0.0, 0.0)), ("U2", _square(0.5, 0.0))]
    findings = drc.check_courtyard_overlap(overlapping, bottom_by_refdes={"U2": True})
    assert findings == []  # U1 defaults to top, U2 is bottom -- opposite sides

    findings = drc.check_courtyard_overlap(overlapping, bottom_by_refdes={})
    assert len(findings) == 1  # both default to top -- same side


# ── (d) the real ewod-dogfood-1 fixture: the board the gap was measured on ─


@pytest.mark.slow
def test_dogfood_sink_pads_land_on_bottom_copper_in_the_fab_svg(pcb):
    """``ARR1_SINK_0`` is authored ``layer='bottom'`` and sits directly
    under the ``ARR1`` array (round-7 ``sink_grid`` — see ``tests.
    test_pcb_ewod_dogfood``'s module docstring). Its pads must now flash
    on ``B_Cu`` in the fab SVG (:mod:`precis.pcb.gerber_view`'s own hover
    title, ``"<layer> · (x, y) mm · pad <pin> of <refdes>"``), never
    ``F_Cu`` — before gr341516 every pad, sink included, landed on F_Cu."""
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="svg", args={"level": "fab"})
    titles = re.findall(r"<title>([^<]*)</title>", resp.body)
    sink_titles = [t for t in titles if "of ARR1_SINK_0" in t]
    assert sink_titles, "expected at least one pad title naming ARR1_SINK_0"
    assert all(t.startswith("B_Cu") for t in sink_titles), sink_titles
    assert not any(t.startswith("F_Cu") for t in sink_titles), sink_titles


@pytest.mark.slow
def test_dogfood_drc_no_longer_reports_array_vs_sink_cross_layer_clearance(pcb):
    """The DRC-facing half of the same fact: ``ARR1`` (top) and
    ``ARR1_SINK_0`` (bottom) share several nets (the sink's channel
    pins ARE the electrodes' own escape nets) but even a FOREIGN-net pair
    between the two must now be silent -- they are on opposite sides of
    the board, never coplanar. Round-8's ``test_dogfood_drc_view_
    findings_are_all_the_documented_side_gap`` (``tests.test_pcb_ewod_
    dogfood``) is the strong, whole-fixture form of this same assertion;
    this is the narrow, two-refdes-only reproduction."""
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pads = pcb._drc_pads(ref.id, _LAYERS)
    two_refdes_pads = [
        p for p in pads if str(p.get("refdes")) in ("ARR1", "ARR1_SINK_0")
    ]
    assert any(p.get("refdes") == "ARR1_SINK_0" for p in two_refdes_pads)
    model = {"layers": _LAYERS, "copper": [], "pads": two_refdes_pads}
    errors = [f for f in drc.check_clearance(model, _CAP4) if f.severity == "error"]
    detail = "\n".join(
        f"  {f.rule}: {f.where} :: {str(f.detail)[:140]}" for f in errors[:12]
    )
    assert not errors, (
        f"{len(errors)} array-vs-sink cross-layer clearance error(s):\n{detail}"
    )
