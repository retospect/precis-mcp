"""view='gerber' — the fab-output round-trip
(docs/backlog/pcb-fab-output-unwired.md).

This is the test the backlog doc says would have caught the whole export
tail being unwired: a real design (footprints cached, some copper
realized) -> a zipped gerber+Excellon bundle that actually contains pad
flashes, copper, drills and an outline -- not just files that exist.
"""

from __future__ import annotations

import zipfile

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP

# QFN-32-shaped footprint, trimmed to 3 pads -- enough to exercise SMD
# placement + a real pin->net lookup. Shape/keys match
# precis.pcb.easyeda.parse_component's real output exactly (number, shape,
# rot, layer, drill), not a hand-wavy stand-in.
_QFN_FOOTPRINT = {
    "pads": [
        {
            "number": "1",
            "shape": "RECT",
            "x": -1.0,
            "y": 0.0,
            "w": 0.3,
            "h": 0.6,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "2",
            "shape": "RECT",
            "x": 0.0,
            "y": -1.0,
            "w": 0.3,
            "h": 0.6,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
        {
            "number": "8",
            "shape": "RECT",
            "x": 1.0,
            "y": 0.0,
            "w": 0.3,
            "h": 0.6,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": None,
        },
    ],
    "pin_map": {
        "1": {"name": "VDD", "tags": ["power"]},
        "2": {"name": "GND", "tags": ["gnd"]},
        "8": {"name": "SCL", "tags": ["bidir", "i2c"]},
    },
}

# 0402-shaped footprint, 2 pads -- placed on the BOTTOM side in the design
# below, so this is the fixture the mirror-flip assertion reads.
_CAP_FOOTPRINT = {
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
        "1": {"name": "1", "tags": []},
        "2": {"name": "2", "tags": []},
    },
}

# A single through-hole pad -- the fixture the PTH-drill assertion reads.
_THT_FOOTPRINT = {
    "pads": [
        {
            "number": "1",
            "shape": "ELLIPSE",
            "x": 0.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.0,
            "rot": 0.0,
            "layer": "F.Cu",
            "drill": 0.4,
        },
    ],
    "pin_map": {"1": {"name": "1", "tags": []}},
}

_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "part": "C2838500",
            "footprint": "QFN-32",
            "x": 10.0,
            "y": 10.0,
            "rot": 90.0,
            "pins": [
                {"name": "VDD"},
                {"name": "GND"},
                {"name": "SCL"},
            ],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 11.5,
            "y": 10.0,
            "layer": "bottom",
            "pins": [{"name": "1"}, {"name": "2"}],
        },
        {
            "refdes": "J1",
            "label": "THT header",
            "part": "CTHT1",
            "footprint": "THT-1",
            "x": 5.0,
            "y": 5.0,
            "pins": [{"name": "1"}],
        },
    ],
    "nets": [
        {"name": "VCC3V3", "class": "power"},
        {"name": "GND", "class": "gnd"},
    ],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC3V3", "refdes": "C1", "pin": "1"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb, *, skip: frozenset[str] = frozenset()) -> str:
    """Seed the fixture design + cache its 3 footprints, except any
    C-number named in ``skip`` — the knob the mixed-design tests below use
    to leave exactly one part uncached rather than duplicating the whole
    fixture."""
    pcb.put(id="fabtest", args=_DESIGN)
    for lcsc, fp in (
        ("C2838500", _QFN_FOOTPRINT),
        ("C1525", _CAP_FOOTPRINT),
        ("CTHT1", _THT_FOOTPRINT),
    ):
        if lcsc not in skip:
            pcb.store.part_footprint_put(lcsc, fp)
    ref = pcb.store.get_ref(kind="pcb", id="fabtest")
    board_id = pcb.store.pcb_ensure_board(ref.id)
    net_ids = pcb.store.pcb_net_ids(ref.id)
    pcb.store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["VCC3V3"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [10.0, 10.0], "end": [11.5, 10.0]}
                    ],
                    "width_mm": 0.25,
                },
            },
            {
                # pcb_copper.layer is NOT NULL but a via's real layer
                # membership is its geom["span"] pair -- see
                # workers/job_types/pcb_route.py's own via-row comment.
                "ctype": "via",
                "layer": "F.Cu",
                "net_id": net_ids["GND"],
                "route_id": None,
                "geom": {
                    "x": 10.0,
                    "y": 12.0,
                    "dia_mm": 0.6,
                    "drill_mm": 0.3,
                    "span": ["F.Cu", "B.Cu"],
                },
            },
        ],
    )
    return "fabtest"


def test_gerber_view_writes_a_zip_and_reports_counts(pcb, tmp_path):
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    assert "exported fabtest → GERBER" in resp.body
    # 3 QFN SMD pads + 2 cap SMD pads + 1 THT pad flashed on all 4 stackup
    # layers (annular ring) = 3 + 2 + 4 = 9, PLUS the 3 board fiducials,
    # which are ordinary `model["pads"]` entries so that their copper and
    # their swelled mask opening come out of the existing pad pipeline
    # rather than a second one.
    assert "pads: 12" in resp.body
    # the THT pad's own drill -- the via's drill is a separate copper item,
    # not part of model["drills"] (precis.pcb.gerber.excellon_files reads
    # vias straight off model["copper"], see its own docstring).
    assert "drills: 1" in resp.body
    assert "copper item(s): 2" in resp.body
    zpath = tmp_path / "fabtest-fab.zip"
    assert zpath.exists()


def test_gerber_zip_contains_pad_copper_drill_and_outline(pcb, tmp_path):
    slug = _seed(pcb)
    pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "fabtest-fab.zip") as zf:
        names = set(zf.namelist())
        assert {
            "fabtest-F_Cu.gbr",
            "fabtest-B_Cu.gbr",
            "fabtest-F_Mask.gbr",
            "fabtest-B_Mask.gbr",
            "fabtest-Edge_Cuts.gbr",
            "fabtest-PTH.drl",
        } <= names

        f_cu = zf.read("fabtest-F_Cu.gbr").decode("utf-8")
        b_cu = zf.read("fabtest-B_Cu.gbr").decode("utf-8")
        # copper (the seeded track) + pad flashes (D03) both land in F.Cu.
        assert "D01*" in f_cu  # the track stroke
        assert "D03*" in f_cu  # at least one pad/via flash
        assert "D03*" in b_cu  # the mirrored 0402 pads + THT annular ring

        mask = zf.read("fabtest-F_Mask.gbr").decode("utf-8")
        assert "D03*" in mask  # soldermask openings derived from the pads

        edge = zf.read("fabtest-Edge_Cuts.gbr").decode("utf-8")
        assert "D01*" in edge  # the outline polygon traced

        drl = zf.read("fabtest-PTH.drl").decode("utf-8")
        assert "METRIC" in drl
        # the via (0.3mm) and the THT pad (0.4mm) both got a tool + a hit.
        assert "C0.3000" in drl
        assert "C0.4000" in drl


def test_gerber_bottom_instance_pads_land_on_b_cu_not_f_cu(pcb, tmp_path):
    # C1's footprint pads are authored on F.Cu; the design places C1 on
    # the bottom side -- this is the mirror-flip assertion the task brief
    # calls out explicitly ("assert that in a test rather than assuming").
    slug = _seed(pcb)
    pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "fabtest-fab.zip") as zf:
        b_cu = zf.read("fabtest-B_Cu.gbr").decode("utf-8")
        # C1 pads at local (-0.5,0)/(0.5,0) mirrored (x negated) then
        # placed at instance (11.5, 10.0), rot=0 -> board (12.0,10.0) and
        # (11.0,10.0) -- both must appear as flashes on B.Cu.
        assert "X12000000Y10000000D03*" in b_cu
        assert "X11000000Y10000000D03*" in b_cu


def test_solder_paste_opens_smd_pads_and_skips_the_through_hole_one(pcb, tmp_path):
    # Paste is the STENCIL, and paste printed over a plated hole falls
    # through it -- a THT pad must get a soldermask opening and no paste
    # aperture. J1 is the design's only THT part, at (5,5) on F.Cu with a
    # 0.4mm drill, so the same coordinate appearing in the mask and absent
    # from the paste is the whole assertion.
    slug = _seed(pcb)
    pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "fabtest-fab.zip") as zf:
        assert {"fabtest-F_Paste.gbr", "fabtest-B_Paste.gbr"} <= set(zf.namelist())
        f_paste = zf.read("fabtest-F_Paste.gbr").decode("utf-8")
        f_mask = zf.read("fabtest-F_Mask.gbr").decode("utf-8")
        b_paste = zf.read("fabtest-B_Paste.gbr").decode("utf-8")

        assert "%TF.FileFunction,SolderPaste,Top*%" in f_paste
        assert "X5000000Y5000000D03*" in f_mask, "THT pad needs a mask opening"
        assert "X5000000Y5000000D03*" not in f_paste, "THT pad must get no paste"

        # ...and the SMD pads DO get one, on both sides, so the exclusion
        # above is not simply an empty layer passing by accident.
        assert f_paste.count("D03*") == f_mask.count("D03*") - 1
        assert "D03*" in b_paste  # C1, the bottom-side 0402


# ── real vs. synthesized pads: the "pads must be precisely what the
#    footprint says" join (part_lcsc -> Store.pcb_footprints_for ->
#    session.footprints_by_refdes -> realize.pad_geometry) ──────────────
def test_export_fab_accepts_a_design_whose_every_part_is_cached(pcb, tmp_path):
    """A fully-cached design (every placed part has a real footprint) must
    export without ``export_fab`` raising ``SynthesizedPadError`` — the
    acceptance side of the refusal below, so a passing suite can't hide a
    weakened refusal behind "well, it never actually fires either way"."""
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    assert "exported fabtest → GERBER" in resp.body
    # no "N part(s) have no cached footprint" warning line at all.
    assert "no cached footprint" not in resp.body
    assert "synthesized" not in resp.body.lower()


def test_export_fab_refuses_a_mixed_design_with_one_uncached_part(pcb, tmp_path):
    """The opposite side: J1 (THT-1 / CTHT1) has no cached footprint in
    this seed, so its pad must come out ``synthesized: True`` and
    ``export_fab`` must still refuse the whole board — a real, cached
    part elsewhere on the SAME board must not launder a synthesized one
    through. Before the ``board_pads``/``pads_for_ir`` merge in
    ``_render_gerber``, an uncached part's pads were silently DROPPED
    from the model instead of marked synthesized, so this exact case
    exported successfully with J1's copper simply missing -- the
    naive all-or-nothing failure mode the task brief calls out."""
    slug = _seed(pcb, skip=frozenset({"CTHT1"}))
    with pytest.raises(BadInput, match="synthesized"):
        pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})


def test_drc_pads_real_for_cached_parts_synthesized_for_the_rest(pcb):
    """The mixed-design case at the geometry layer, directly:
    :meth:`PcbHandler._drc_pads` (DRC's own pad source, shared with the
    ``view='gerber'`` fallback and ``level='fab'`` preview) must mark
    U1's and C1's pads (cached footprints) ``synthesized=False`` and J1's
    (no cached footprint) ``synthesized=True`` — per pin, not per board.
    Note U1's own SCL pin is UNCONNECTED in this fixture (only VDD/GND
    are wired) — real-vs-synthesized is a property of the footprint
    cache, not of net membership, so that pin must still read
    ``synthesized=False``, which is why this identifies J1's one pad by
    its known placed coordinate rather than by net name."""
    slug = _seed(pcb, skip=frozenset({"CTHT1"}))
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    layer_names = [str(layer["name"]) for layer in DEFAULT_STACKUP]
    pads = pcb._drc_pads(ref.id, layer_names)
    # J1 sits at (5.0, 5.0) with a single pin and no rotation -- its pad
    # center is exactly the instance position, and no other instance in
    # this fixture is placed there.
    j1_pads = [p for p in pads if (p["x"], p["y"]) == (5.0, 5.0)]
    assert len(j1_pads) == 1
    assert j1_pads[0]["synthesized"] is True

    other_pads = [p for p in pads if p is not j1_pads[0]]
    assert len(other_pads) == len(pads) - 1
    assert other_pads, "U1 + C1 must still contribute real pads"
    assert all(p["synthesized"] is False for p in other_pads)
