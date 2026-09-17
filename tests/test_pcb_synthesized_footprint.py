"""gripe gr341532 — a catalog part with no cached footprint must not
silently DRC as phantom pins at fabricated coordinates.

Three loud-failure seams this covers: :func:`precis.pcb.drc.
check_synthesized_footprint` (the ``view='drc'`` finding),
:meth:`precis.handlers.pcb.PcbHandler._render_fab_svg`'s ``<desc>`` legend
note (the ``level='fab'`` picture), and :func:`precis.pcb.svg._pad_el`'s
drilled-pad rendering (a separate, adjacent defect found while fixing the
first two — a drilled ``level='board'`` pad drew as a solid disc with no
hole, unlike a via).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc as pcb_drc
from precis.pcb.svg import _pad_el

# A real, cached SOT-23-shaped footprint -- 2 pads, matching U1's pins
# below exactly (number/shape/x/y/w/h/rot/layer/drill match precis.pcb.
# easyeda.parse_component's real output shape, same convention
# tests/test_pcb_fab_export.py's fixtures use).
_REAL_FOOTPRINT = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {
        "1": {"name": "A", "tags": []},
        "2": {"name": "B", "tags": []},
    },
}

# U2's footprint, cached ONLY by the "fully cached" test below -- 3 pads,
# matching U2's 3 pins (A/B/C), so the "footprint IS cached" case produces
# real, non-synthesized geometry rather than accidentally still falling
# back.
_U2_FOOTPRINT = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.0,
            "y": 0.5,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "3",
            "shape": "RECT",
            "x": 0.5,
            "y": 0.0,
            "w": 0.4,
            "h": 0.4,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {
        "1": {"name": "A", "tags": []},
        "2": {"name": "B", "tags": []},
        "3": {"name": "C", "tags": []},
    },
}

_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "cached SOT-23",
            "part": "CREAL1",
            "footprint": "SOT-23",
            "x": 5.0,
            "y": 5.0,
            "pins": [{"name": "A"}, {"name": "B"}],
        },
        {
            # No `part_footprint_put` call for CGHOST1 below -- the whole
            # point: a catalog part (has `part=`) whose cache never got
            # populated, mirroring tests/test_pcb_fab_export.py's
            # `_seed(..., skip=...)` knob but as the PERMANENT state of
            # this fixture's own U2, not a toggled one.
            "refdes": "U2",
            "label": "uncached SOT-23",
            "part": "CGHOST1",
            "footprint": "SOT-23",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        },
    ],
    "nets": [{"name": "N1", "class": "signal"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "A"},
        {"net": "N1", "refdes": "U2", "pin": "A"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb, *, slug: str, cache_u2: bool) -> str:
    """The fixture design above -- U1's footprint always cached, U2's only
    when ``cache_u2`` (the knob the "footprint IS cached produces no
    finding" test flips)."""
    pcb.put(id=slug, args=_DESIGN)
    pcb.store.part_footprint_put("CREAL1", _REAL_FOOTPRINT)
    if cache_u2:
        pcb.store.part_footprint_put("CGHOST1", _U2_FOOTPRINT)
    return slug


# ── check_synthesized_footprint (unit, no store) ─────────────────────────


def test_check_synthesized_footprint_groups_by_refdes_and_counts_pins():
    model = {
        "pads": [
            {"refdes": "U2", "pin": "A", "synthesized": True, "part_lcsc": "CGHOST1"},
            {"refdes": "U2", "pin": "B", "synthesized": True, "part_lcsc": "CGHOST1"},
            {"refdes": "U2", "pin": "C", "synthesized": True, "part_lcsc": "CGHOST1"},
            {"refdes": "U1", "pin": "A", "synthesized": False, "part_lcsc": "CREAL1"},
            {"refdes": "U1", "pin": "B", "synthesized": False, "part_lcsc": "CREAL1"},
        ]
    }
    findings = pcb_drc.check_synthesized_footprint(model)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "synthesized_footprint"
    assert f.severity == "error"
    assert f.where == "part U2"
    assert "3 pin(s) of U2 have no real footprint" in f.detail
    assert "synthesized bound" in f.detail
    assert f.objects == ({"refdes": "U2", "n_pins": 3},)


def test_check_synthesized_footprint_clean_when_every_pad_is_real():
    model = {
        "pads": [
            {"refdes": "U1", "pin": "A", "synthesized": False, "part_lcsc": "CREAL1"},
            {"refdes": "U1", "pin": "B", "synthesized": False, "part_lcsc": "CREAL1"},
        ]
    }
    assert pcb_drc.check_synthesized_footprint(model) == []


def test_check_synthesized_footprint_ignores_a_part_less_instance():
    """A design-local/hand-authored pad (no `part_lcsc`) is synthesized by
    construction and can never be cached -- a generator's own copper, a
    mounting hole, a wire. It must never be flagged, no matter how many
    synthesized pads it has."""
    model = {
        "pads": [
            {"refdes": "W11", "pin": "1", "synthesized": True},
            {"refdes": "W11", "pin": "2", "synthesized": True},
        ]
    }
    assert pcb_drc.check_synthesized_footprint(model) == []


# ── view='drc' (handler-level) ────────────────────────────────────────


def test_drc_view_reports_exactly_one_synthesized_footprint_finding(pcb):
    slug = _seed(pcb, slug="synthtest", cache_u2=False)
    resp = pcb.get(id=slug, view="drc")

    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    _run_id, findings = pcb.store.pcb_drc_findings_latest(ref.id)
    synth = [f for f in findings if f["rule"] == "synthesized_footprint"]
    assert len(synth) == 1
    assert synth[0]["severity"] == "error"
    assert "3 pin(s) of U2 have no real footprint" in synth[0]["detail"]
    assert synth[0]["objects"] == [{"refdes": "U2", "n_pins": 3}]

    # The human-readable header names the count too, not just the row.
    assert "1 part(s) have no cached footprint" in resp.body
    assert "U2" in resp.body


def test_drc_view_has_no_synthesized_footprint_finding_when_fully_cached(pcb):
    slug = _seed(pcb, slug="synthtest-cached", cache_u2=True)
    resp = pcb.get(id=slug, view="drc")

    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    _run_id, findings = pcb.store.pcb_drc_findings_latest(ref.id)
    assert [f for f in findings if f["rule"] == "synthesized_footprint"] == []
    assert "have no cached footprint" not in resp.body


# ── level='fab' SVG desc/legend note ────────────────────────────────────


def test_fab_svg_desc_names_the_synthesized_refdes_and_pin_count(pcb):
    slug = _seed(pcb, slug="synthtest-svg", cache_u2=False)
    resp = pcb.get(id=slug, view="svg", args={"level": "fab"})
    assert "<desc>synthesized footprints" in resp.body
    assert "synthesized footprint: U2 (3 pins)" in resp.body
    assert "geometry is a bound, not the part" in resp.body


def test_fab_svg_desc_omits_the_note_when_fully_cached(pcb):
    slug = _seed(pcb, slug="synthtest-svg-cached", cache_u2=True)
    resp = pcb.get(id=slug, view="svg", args={"level": "fab"})
    assert "synthesized footprint" not in resp.body


# ── precis.pcb.svg._pad_el drill rendering ──────────────────────────────


def test_pad_el_draws_the_drill_hole_like_a_via_when_drilled():
    pad = {
        "shape": "circle",
        "x": 1.0,
        "y": 2.0,
        "w": 1.0,
        "drill": 0.4,
    }
    svg = _pad_el(pad, fill="#c8781e")
    # The outer copper annulus...
    assert '<circle cx="1' in svg
    assert 'fill="#c8781e"' in svg
    # ...AND a white hole punched through it, same convention _via_el uses.
    assert 'fill="#ffffff"' in svg
    assert svg.count("<circle") == 2


def test_pad_el_undrilled_pad_has_no_hole():
    pad = {"shape": "circle", "x": 1.0, "y": 2.0, "w": 1.0, "drill": None}
    svg = _pad_el(pad, fill="#c8781e")
    assert svg.count("<circle") == 1
    assert "#ffffff" not in svg


def test_pad_el_drilled_rect_pad_still_gets_the_hole():
    pad = {
        "shape": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 1.2,
        "h": 1.2,
        "drill": 0.6,
    }
    svg = _pad_el(pad, fill="#c8781e")
    assert svg.startswith("<rect")
    assert 'fill="#ffffff"' in svg
    assert "<circle" in svg
