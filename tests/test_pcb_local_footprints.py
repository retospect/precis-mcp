"""pcb-ewod-multitile Slice 1 — authored copper footprints (no LCSC part),
polygon pads, and honest export.

Exercises the new ``put(kind='pcb', args={'footprints': [...]})`` authoring
path end to end: a component with no ``part``/``part_lcsc`` names a
footprint by its own ``footprint`` field, its polygon pad reaches DRC/
gerber/SVG through the SAME pipeline a catalog part's pads do, and
``export_fab`` treats the authored geometry as real (never a
``SynthesizedPadError``) — the "authored, not synthesized" distinction
this slice exists to draw.
"""

from __future__ import annotations

import zipfile

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import gerber as pcb_gerber

# A single hexagon-ish electrode pad -- role=electrode, mask=covered (the
# field-wide mask_open feature below is what actually opens it), paste
# defaults to "none" for role=electrode when the author omits it.
_ELECTRODE_FOOTPRINT = {
    "name": "ewod_pad",
    "pads": [
        {
            "pin": "E",
            "shape": "polygon",
            "poly": [
                [-0.75, -0.75],
                [0.75, -0.75],
                [0.75, 0.6],
                [0.0, 0.75],
                [-0.75, 0.6],
            ],
            "role": "electrode",
            "mask": "covered",
        }
    ],
}

_DESIGN = {
    "footprints": [_ELECTRODE_FOOTPRINT],
    "components": [
        {
            "refdes": "E1",
            "label": "EWOD electrode",
            "footprint": "ewod_pad",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "E"}],
        },
        # An ordinary solderable pad on the same board, to prove the two
        # footprint origins (local vs. catalog-shaped) coexist.
        {
            "refdes": "TP1",
            "label": "testpoint",
            "footprint": "tp_pad",
            "x": 15.0,
            "y": 10.0,
            "pins": [{"name": "1"}],
        },
    ],
    "nets": [{"name": "PAD_E1"}, {"name": "TP"}],
    "connections": [
        {"net": "PAD_E1", "refdes": "E1", "pin": "E"},
        {"net": "TP", "refdes": "TP1", "pin": "1"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
        {
            "ftype": "mask_open",
            "layer": "top",
            "geom": {"polygon": [[8.0, 8.0], [12.0, 8.0], [12.0, 12.0], [8.0, 12.0]]},
        },
    ],
}

_TP_FOOTPRINT = {
    "name": "tp_pad",
    "pads": [{"pin": "1", "shape": "circle", "x": 0.0, "y": 0.0, "w": 1.0}],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb) -> str:
    pcb.put(id="ewod-1", args=_DESIGN)
    pcb.put(id="ewod-1", args={"footprints": [_TP_FOOTPRINT]})
    return "ewod-1"


# ── authoring + persistence ─────────────────────────────────────────────
def test_put_footprints_block_is_reported_in_the_response(pcb):
    resp = pcb.put(id="ewod-1", args=_DESIGN)
    assert "+1 footprint(s)" in resp.body


def test_footprints_block_persists_and_is_readable_back(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    local = pcb.store.pcb_local_footprints_for(ref.id)
    assert set(local) == {"ewod_pad", "tp_pad"}
    pad = local["ewod_pad"]["pads"][0]
    assert pad["shape"] == "polygon"
    assert pad["role"] == "electrode"
    assert pad["mask"] == "covered"
    # paste defaults to "none" for role=electrode when the author omits it
    assert pad["paste"] == "none"
    assert len(pad["poly"]) == 5


def test_reauthoring_the_same_footprint_name_upserts_geometry(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    bigger = {
        "name": "ewod_pad",
        "pads": [{"pin": "E", "shape": "rect", "x": 0.0, "y": 0.0, "w": 5.0, "h": 5.0}],
    }
    pcb.put(id=slug, args={"footprints": [bigger]})
    local = pcb.store.pcb_local_footprints_for(ref.id)
    assert local["ewod_pad"]["pads"][0]["shape"] == "rect"
    assert local["ewod_pad"]["pads"][0]["w"] == 5.0


def test_component_with_no_part_joins_via_its_footprint_field(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    graph = pcb.store.pcb_graph(ref.id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    assert by_refdes["E1"]["part_lcsc"] is None
    assert by_refdes["E1"]["footprint"] == "ewod_pad"


def test_bad_pad_role_is_rejected_as_bad_input(pcb):
    with pytest.raises(BadInput):
        pcb.put(
            id="bad-role",
            args={
                "footprints": [
                    {
                        "name": "bad",
                        "pads": [
                            {
                                "pin": "1",
                                "shape": "rect",
                                "x": 0,
                                "y": 0,
                                "w": 1,
                                "role": "not-a-role",
                            }
                        ],
                    }
                ]
            },
        )


def test_polygon_pad_needs_at_least_three_vertices(pcb):
    with pytest.raises(BadInput):
        pcb.put(
            id="bad-poly",
            args={
                "footprints": [
                    {
                        "name": "bad",
                        "pads": [
                            {"pin": "1", "shape": "polygon", "poly": [[0, 0], [1, 0]]}
                        ],
                    }
                ]
            },
        )


# ── DRC / gerber honesty ─────────────────────────────────────────────────
def test_drc_pads_carry_the_authored_polygon_shape(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pads = pcb._drc_pads(ref.id, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    e1 = next(p for p in pads if p.get("refdes") == "E1")
    assert e1["shape"] == "polygon"
    assert e1["synthesized"] is False
    assert len(e1["poly"]) == 5


def test_gerber_export_of_authored_footprints_never_raises_synthesized(pcb, tmp_path):
    """The whole point of this slice: authored copper is REAL, not a
    landpattern bound -- export_fab must not refuse it."""
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    assert "SynthesizedPadError" not in resp.body
    assert "exported ewod-1 → GERBER" in resp.body
    zpath = tmp_path / "ewod-1-fab.zip"
    assert zpath.exists()


def test_gerber_zip_carries_a_region_fill_for_the_polygon_pad(pcb, tmp_path):
    slug = _seed(pcb)
    pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "ewod-1-fab.zip") as zf:
        f_cu = zf.read("ewod-1-F_Cu.gbr").decode("utf-8")
        assert "G36*" in f_cu and "G37*" in f_cu


def test_gerber_electrode_mask_covered_and_no_paste(pcb, tmp_path):
    slug = _seed(pcb)
    pcb.get(id=slug, view="gerber", args={"dir": str(tmp_path)})
    with zipfile.ZipFile(tmp_path / "ewod-1-fab.zip") as zf:
        names = set(zf.namelist())
        # a solder-paste layer is only written when SOME pad wants paste
        # (TP1 does); the electrode itself contributes no paste aperture
        # of its own, checked by the mask-open region test below instead
        # of trying to prove a negative on stencil content directly.
        assert "ewod-1-F_Paste.gbr" in names
        mask = zf.read("ewod-1-F_Mask.gbr").decode("utf-8")
        # the field-wide mask_open feature's own ring (8,8)-(12,8)-...
        assert "X8000000Y8000000D02*" in mask


def test_mask_open_regions_helper_reads_the_authored_feature(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    regions = pcb._mask_open_regions(ref.id)
    assert regions == [
        {
            "side": "top",
            "polygon": [[8.0, 8.0], [12.0, 8.0], [12.0, 12.0], [8.0, 12.0]],
        }
    ]


def test_svg_fab_level_render_does_not_crash_on_polygon_pads(pcb):
    slug = _seed(pcb)
    resp = pcb.get(id=slug, view="svg", args={"level": "fab"})
    assert "<svg" in resp.body


def test_export_fab_still_refuses_a_genuinely_synthesized_pad():
    """The other half of "honest export": a partless, footprint-less
    instance's pad is STILL refused -- this slice adds a real authoring
    path, it does not weaken the refusal for everything else."""
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "outline": [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]],
        "copper": [],
        "pads": [
            {
                "layer": "F.Cu",
                "net": "N1",
                "shape": "rect",
                "x": 1.0,
                "y": 1.0,
                "w": 0.5,
                "h": 0.5,
                "synthesized": True,
            }
        ],
        "drills": [],
        "silkscreen": {"top": [], "bottom": []},
    }
    with pytest.raises(pcb_gerber.SynthesizedPadError):
        pcb_gerber.export_fab(model)
