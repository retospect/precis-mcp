"""gr341578 — :func:`precis.pcb.realize.pads_for_ir` used to emit only
``layer/net/shape/x/y/w[/h][/poly]/synthesized/refdes/pin``, silently
dropping a pad's ``paste``/``mask``/``role``/``drill`` even when the
source footprint (real or authored-local) carried them. Consequence
measured on prod: the fab preview (``view='svg', args={'level':'fab'}`` —
:meth:`~precis.handlers.pcb.PcbHandler._render_fab_svg`, which reaches
pads through :meth:`~precis.handlers.pcb.PcbHandler._drc_pads` ->
:func:`~precis.pcb.realize.pads_for_ir`, NOT the ``view='gerber'`` export
path's :func:`~precis.pcb.padplace.board_pads`) flashed a solder-paste
aperture over every bare-ENIG electrode pad regardless of its authored
``paste: "none"``, because that fact never reached the model at all — so
its F_Paste layer came out byte-identical to F_Cu. A drilled pad fared
the same: ``drill`` never rode along, so nothing through this path could
ever mark a pad plated-through.

These tests pin the fix at the ``pads_for_ir`` boundary directly (no
routing, no job dispatch — fast) using the SAME authored-local-footprint
shape :mod:`tests.test_pcb_local_footprints` already exercises for the
``board_pads`` half of this same story.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import gerber as pcb_gerber

#: One footprint carrying both failure modes gr341578 names, plus an
#: ordinary reference pad: a mask-covered ELECTRODE pad (``paste``
#: omitted -> defaults to "none",
#: :func:`precis.store._pcb_ops._normalize_local_footprint_pad`'s own
#: rule), a drilled THT pad (``paste``/``mask``/``role`` all omitted ->
#: default "solderable"/"open"/"full", same function -- and
#: ``solderpaste_gerber`` skips ANY drilled pad regardless of ``paste``,
#: its own module docstring), and a plain SMD pad with nothing authored
#: at all (the "still gets a normal paste aperture" control).
_FOOTPRINT = {
    "name": "gr341578_fp",
    "pads": [
        {
            "pin": "E",
            "shape": "rect",
            "x": 0.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.0,
            "role": "electrode",
            "mask": "covered",
        },
        {
            "pin": "V",
            "shape": "circle",
            "x": 3.0,
            "y": 0.0,
            "w": 0.8,
            "drill": 0.3,
        },
        {
            "pin": "P",
            "shape": "rect",
            "x": 6.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.0,
        },
    ],
}

_DESIGN = {
    "footprints": [_FOOTPRINT],
    "components": [
        {
            "refdes": "U1",
            "label": "gr341578 fixture",
            "footprint": "gr341578_fp",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "E"}, {"name": "V"}, {"name": "P"}],
        },
    ],
    "nets": [{"name": "N_E"}, {"name": "N_V"}, {"name": "N_P"}],
    "connections": [
        {"net": "N_E", "refdes": "U1", "pin": "E"},
        {"net": "N_V", "refdes": "U1", "pin": "V"},
        {"net": "N_P", "refdes": "U1", "pin": "P"},
    ],
    "features": [
        {"ftype": "outline", "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]}},
    ],
}

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb) -> str:
    pcb.put(id="gr341578-1", args=_DESIGN)
    return "gr341578-1"


def _pads_by_pin(pcb, ref_id: int) -> dict[str, dict]:
    pads = pcb._drc_pads(ref_id, _LAYERS)
    return {
        str(p["pin"]): p
        for p in pads
        if str(p.get("refdes")) == "U1" and p.get("layer") == "F.Cu"
    }


def test_pads_for_ir_carries_paste_and_mask_for_an_electrode_pad(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    by_pin = _pads_by_pin(pcb, ref.id)
    e = by_pin["E"]
    assert e["role"] == "electrode"
    assert e["mask"] == "covered"
    # paste defaults to "none" for role=electrode when the author omits it
    # (`_normalize_local_footprint_pad`) -- the exact fact gr341578 says
    # never reached this pad source.
    assert e["paste"] == "none"


def test_pads_for_ir_carries_drill_for_a_through_hole_pad(pcb):
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    by_pin = _pads_by_pin(pcb, ref.id)
    v = by_pin["V"]
    assert v.get("drill") == pytest.approx(0.3)
    # the ordinary THT pad's role/mask/paste were never authored -- the
    # normalizer's own defaults (solderable/open/full) still ride through.
    assert v["role"] == "solderable"
    assert v["mask"] == "open"
    assert v["paste"] == "full"


def test_pads_for_ir_omits_paste_mask_role_drill_for_a_synthesized_pad(pcb):
    """A pin with NO real/authored footprint at all (synthesized bound,
    ``pads_for_ir``'s own docstring) carries none of these keys -- the
    same "no key" convention :func:`~precis.pcb.padplace.
    place_footprint_pads` uses for a bare pad, never an invented default
    on a fact nobody authored."""
    pcb.put(
        id="gr341578-synth",
        args={
            "components": [
                {
                    "refdes": "U9",
                    "label": "no cached footprint",
                    "part": "C_UNCACHED",
                    "footprint": "UNCACHED-1",
                    "pins": [{"name": "1"}],
                    "x": 5.0,
                    "y": 5.0,
                },
            ],
            "nets": [{"name": "N9"}],
            "connections": [{"net": "N9", "refdes": "U9", "pin": "1"}],
            "features": [
                {
                    "ftype": "outline",
                    "geom": {"path": [[0, 0], [10, 0], [10, 10], [0, 10]]},
                },
            ],
        },
    )
    # deliberately never `pcb.store.part_footprint_put("C_UNCACHED", ...)`
    # -- U9's pad has no real footprint at all, so it must synthesize.
    ref = pcb.store.get_ref(kind="pcb", id="gr341578-synth")
    assert ref is not None
    pads = pcb._drc_pads(ref.id, _LAYERS)
    u9 = next(p for p in pads if p.get("refdes") == "U9")
    assert u9["synthesized"] is True
    for key in ("paste", "mask", "role", "drill"):
        assert key not in u9


def test_fab_preview_paste_layer_is_not_byte_identical_to_copper(pcb):
    """The measured prod consequence, reproduced directly off the SAME
    pad source :meth:`~precis.handlers.pcb.PcbHandler._render_fab_svg`
    builds its ``export_fab`` model from
    (:meth:`~precis.handlers.pcb.PcbHandler._drc_pads` ->
    :func:`~precis.pcb.realize.pads_for_ir`) -- never the ``view='gerber'``
    export path, which already carried ``paste`` correctly via
    :func:`~precis.pcb.padplace.board_pads` and so never showed this bug.

    Before the fix: no pad in this model carried a ``paste`` key at all,
    so :func:`precis.pcb.gerber.solderpaste_gerber`'s ``pad.get("paste")
    == "none"`` skip never fired for the electrode pad either -- the
    F_Paste layer flashed the SAME apertures as F_Cu."""
    slug = _seed(pcb)
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pads = pcb._drc_pads(ref.id, _LAYERS)
    model = {
        "layers": _LAYERS,
        "outline": [],
        "copper": [],
        "pads": pads,
        "drills": [],
        "silkscreen": {"top": [], "bottom": []},
    }
    files = pcb_gerber.export_fab(model, name=slug, allow_synthesized=True)
    f_paste = files[f"{slug}-F_Paste.gbr"]
    f_cu = files[f"{slug}-F_Cu.gbr"]
    assert f_paste != f_cu
    # Stronger than a bare inequality: all three pads flash real copper,
    # but only "P" (nothing authored -> paste defaults to "full", no
    # drill) gets a paste aperture -- "E" is suppressed by its authored
    # `paste: "none"`, "V" by carrying a `drill` at all
    # (`solderpaste_gerber`'s own module docstring: THT pads never get a
    # stencil opening, independent of `paste`).
    assert f_cu.count("D03*") == 3
    assert f_paste.count("D03*") == 1


def _slot_footprint(pad_rot: float) -> dict:
    return {
        "pads": [
            {
                "number": "1",
                "shape": "OVAL",
                "x": 0.7,
                "y": 0.3,
                "w": 6.0,
                "h": 4.0,
                "rot": pad_rot,
                "layer": "F.Cu",
                "drill": 1.0,
                "slot": {"w": 3.0, "h": 1.0},
            }
        ],
        "pin_map": {"1": {"name": "1", "tags": []}},
    }


@pytest.mark.parametrize(
    ("layer", "inst_rot", "pad_rot"),
    [
        ("top", 0.0, 45.0),
        ("top", 90.0, 30.0),
        ("bottom", 0.0, 45.0),
        ("bottom", 90.0, 30.0),
    ],
)
def test_pads_for_ir_slot_matches_padplace_on_both_sides(pcb, layer, inst_rot, pad_rot):
    """The IR path (DRC/preview) and the fab path must agree on an oblique
    slot's end centres, including on a mirrored bottom-side part."""
    from precis.pcb import padplace

    fp = _slot_footprint(pad_rot)
    pcb.store.part_footprint_put("CSLOT1", fp)
    pcb.put(
        id="slot-parity",
        args={
            "components": [
                {
                    "refdes": "J1",
                    "label": "slot part",
                    "part": "CSLOT1",
                    "footprint": "SLOT-1",
                    "x": 10.0,
                    "y": 8.0,
                    "rot": inst_rot,
                    "layer": layer,
                    "pins": [{"name": "1"}],
                },
            ],
            "nets": [{"name": "N1"}],
            "connections": [{"net": "N1", "refdes": "J1", "pin": "1"}],
            "features": [
                {
                    "ftype": "outline",
                    "geom": {"path": [[0, 0], [20, 0], [20, 20], [0, 20]]},
                },
            ],
        },
    )
    ref = pcb.store.get_ref(kind="pcb", id="slot-parity")
    assert ref is not None
    ir_pad = next(p for p in pcb._drc_pads(ref.id, _LAYERS) if p["refdes"] == "J1")
    inst = {"x": 10.0, "y": 8.0, "rot": inst_rot, "layer": layer}
    _, drills = padplace.place_footprint_pads(fp["pads"], inst, layers=_LAYERS)
    want = drills[0]["slot"]
    assert ir_pad["drill"] == pytest.approx(1.0)
    for got_pt, want_pt in zip(ir_pad["slot"], want, strict=True):
        assert got_pt == pytest.approx(want_pt, abs=1e-4)
