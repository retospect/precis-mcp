"""EasyEDA Pro ``.epro2`` writer — pure half (pcb-epro-export slice 2b).

Model dict in (the one :meth:`precis.handlers.pcb.PcbHandler._fab_model`
assembles), ``{filename: text}`` / ``bytes`` out: no DB, no clock, no
network, no path handling. The format record is the reader,
:mod:`precis.pcb.epro`; this module emits exactly the shapes that module's
docstring and the hand-written ``tests/fixtures/pcb_epro_tiny`` fixture
record, and the test suite reads every file back through that reader.

**What it emits** — ``project2.json`` plus ONE ``.epru`` stream of
documents:

* a ``PCB`` document: ``DOCHEAD``, the full ``LAYER`` palette Pro
  itself writes (93 records — every layer's colors and transparency,
  not just the copper ones; a missing ``activeColor`` is a
  ``startsWith`` crash in Pro's ``parseLayer``, TAZ V1.15's open
  attempt 2026-10-14), the board-state block
  (``CANVAS``/``META``/``PREFERENCE``/``ACTIVE_LAYER``/``SILK_OPTS``/
  ``PRIMITIVE``/``LAYER_PHYS`` — verbatim passthrough on a re-export,
  Pro's own defaults on a fresh one), the outline ``POLY`` on the
  ``OUTLINE`` layer, then per placed part ``COMPONENT`` + ``ATTR``
  ``Designator`` + ``ATTR`` ``Footprint``/catalog keys with NUMERIC
  x/y/size (a hidden attr's null position is ``addText``'s
  ``NaN is not permitted``) + ``PAD_NET`` per netted pad — and one
  STALE ``PAD_NET`` per declared net that has no members, the same
  mechanism the source file itself uses for a schematic-only net
  (there is no ``NET`` record in Pro's own files);
* one ``FOOTPRINT`` document per DISTINCT footprint (slice 2f): the
  placed instances that un-place to the same authored pad set share one
  document, as Pro's own libraries do — ``COMPONENT.angle`` carries the
  real rotation so the footprint stores authored (unrotated) pads.
  Each doc carries the same LAYER/LAYER_PHYS/CANVAS/PREFERENCE block,
  ``META``, and the ``Name``/``Designator`` attrs Pro's library docs
  carry;
* copper (slice 2c): every ``ctype='track'`` segment as a ``LINE`` or
  ``ARC`` on its copper layer, every ``ctype='via'`` as a through-hole
  ``VIA``. The records are the ones the reader decodes
  (:func:`precis.pcb.epro.extract_tracks` / :func:`extract_vias`), so a
  written board reads back through it; ``tests/test_pcb_epro_export.py``
  holds that round trip to 0.5 um. An ``ARC`` stores a SIGNED sweep in
  the Y-down frame; the Y flip reverses handedness, so a precis ``cw``
  arc is written with a positive sweep (:func:`arc_sweep_deg`, the exact
  inverse of the reader's ``cw = sweep > 0``). A blind/buried via is
  written through-hole and named in a warning: ``VIA`` is documented
  through-hole only, and flattening one silently would short layers;
* pours (slices 2d/2f): a ``ctype='pour'`` becomes a ``POUR`` zone — the
  ``"R"`` rectangle op for an axis-aligned rectangle (Pro 3.2.149's
  shape), else the vertex-list ``"L"`` path Pro 3.2.206 writes on the
  TAZ V1.15 board (slice 2f's missing non-rectangular shape, observed
  there). Pro RE-POURS on open with its own clearances — precis'
  realized fill (antipads, islands, interior holes) is not carried, and
  the response says so;
* silk (slice 2e): courtyard outlines, pin-1 marks and board-frame text
  strokes as ``POLY`` on the silk layers; the refdes label as the
  editable ``Designator`` ``ATTR`` anchored at the silk pass's own text
  box (``model["silk_labels"]``, ``silk.SilkPlacement.x_mm/y_mm``,
  ``origin`` LEFT_BOTTOM) with its stroked twin suppressed, so no label
  prints twice. Pro's font replaces precis' stroke glyphs; the ``mirror``
  flag is written ``False`` on both sides, which assumes Pro mirrors
  bottom-layer text by layer — unverified until a human opens one;
* holes (slice 2f): a footprint-owned non-plated hole rides its
  FOOTPRINT document as a ``FILL`` ``CIRCLE`` on ``MULTI`` (the shape the
  reader's ``_footprint_holes`` parses back); a board-level
  mounting-hole drill becomes a free ``PAD`` with ``plated: false`` —
  the shape the reader's ``extract_mounting_holes`` parses;
* the imported rule table, verbatim (slice 2f):
  ``model["epro_rules"]``/``model["epro_board"]`` carry the records the
  reader preserved (``meta.epro_rules`` on the ref — RULE, RULE_SELECTOR,
  LAYER_PHYS, CANVAS, PREFERENCE, SILK_OPTS, PRIMITIVE, ACTIVE_LAYER,
  META) and are written back unchanged, so the file keeps the board's
  own design rules and physical stackup even where precis has no
  constraint class for them.

**What it deliberately does not emit** — mask-open regions and per-pad
paste/mask intent (no Pro record for a board-level mask region has been
observed; the fill records on solder/paste layers inside footprint
documents are library art, not regions), teardrops (precis places none;
Pro's ``TEARDROP`` records are generated geometry and re-derive on
repour), and NO ``SCH``/``SCH_PAGE``/``DEVICE``/``SYMBOL`` document, on
purpose: a schematic invites "update PCB from schematic", which rewrites
the netlist and destroys the board. Each omission that drops something
present in the model is reported in :attr:`EproExport.warnings`, never
silent.

**Orientation is REAL, not baked.** ``COMPONENT.angle`` is the precis
``rot`` for a top part and ``(rot + 180) % 360`` for a bottom one — the
exact inverse of the reader's convention — and every pad is written in
the footprint's AUTHORED local frame (:func:`to_footprint_xy`), which is
what lets instances share one document. The bottom-side Y-mirror is part
of that convention: a bottom part's pads land where Pro puts them only if
Pro mirrors the footprint in Y on READ. What the convention was verified
against: Pro 3.2.149's own WRITE of ``heaterBaseTest.epro2`` (Reto's
board, bottom-side parts included), by net agreement of 573 track
endpoints with zero cross-net hits (:mod:`precis.pcb.epro`). Pro reading
our file the same way is assumed, not observed; the tests here check the
writer against the same reader, so they cannot catch it. A human opening
an asymmetric bottom-side part in Pro is the check.

**A pad's authored rotation survives only when the board pad says so.**
``padplace.board_pads`` marks a pad that landed non-axis-aligned with
``oblique_rot`` (the total rotation); the authored ``padAngle`` is
recovered as ``oblique_rot - inst_rot``. An axis-aligned board pad
decomposes as ``padAngle 0`` plus ``w``/``h`` un-swapped by the instance
rotation — the rendered shape is identical; only the "rotated inside the
footprint" label is chosen canonically.

**Determinism.** A re-export must re-open as the same project: document
uuids are ``uuid5`` of the slug, record ids ``uuid5`` of slug + refdes +
role, tickets a single monotonic counter, ``updateTime`` a constant, and
the zip's member timestamps are fixed. Same model in, identical bytes out.
"""

from __future__ import annotations

import io
import json
import math
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import padplace, pose
from precis.pcb.gerber import SynthesizedPadError

#: 1 mil = 0.0254 mm — the ``.epru`` body unit. Pinned equal to the
#: reader's own constant by the test suite rather than imported (private).
MM_PER_MIL = 0.0254

#: Decimal places kept on a mil value: 0.0001 mil = 2.54 nm, the format's
#: own storage precision (:func:`precis.pcb.epro._nm`) and 200x finer than
#: the 0.5 um the round-trip test asserts.
_MIL_DECIMALS = 4

#: Clearance, in mm, between the board's extreme geometry and the frame's
#: edges, so every coordinate is strictly positive.
_FRAME_MARGIN_MM = 5.0

_EDITOR_VERSION = "3.2.149"
#: ``DOCHEAD.updateTime``. Constant on purpose: a wall-clock stamp would
#: make two exports of the same model differ.
_UPDATE_TIME_MS = 1_790_000_000_000
_USER_OBJ = {"uuid": "0" * 32, "username": "precis", "nickname": "precis"}

_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "precis-mcp.epro2")

#: ``COMPONENT.layerId`` — see :data:`precis.pcb.epro._COMP_TOP`.
_COMP_TOP, _COMP_BOTTOM = 1, 2
#: Layer ids. ``1``/``2`` copper, ``11`` outline, ``15``+ inner and ``3``/
#: ``4`` silk are spike-observed; the rest follow Pro's own id order.
_L_TOP, _L_BOTTOM, _L_OUTLINE, _L_MULTI, _L_DOCUMENT = 1, 2, 11, 12, 13
_L_SILK_TOP, _L_SILK_BOTTOM = 3, 4
_L_INNER0 = 15

#: Where the "this board carries synthesized pads" STRING sits, mm above
#: the outline's top edge.
_NOTE_RISE_MM = 2.0

#: ``ARC.arcType`` as Pro 3.2.149 writes it on every routed arc of the
#: 2026-10-05 asymbendtest fixture (17 of 17); the reader never reads it.
_ARC_TYPE = "DOT"


# ── frame conversions ───────────────────────────────────────────────────
def _clean(v: float) -> float:
    """``-0.0`` -> ``0.0`` (it compares equal but serialises differently)."""
    return v + 0.0


def to_mils(mm: float) -> float:
    """mm -> mil, rounded to the format's own 0.0001-mil precision."""
    return _clean(round(mm / MM_PER_MIL, _MIL_DECIMALS))


@dataclass(frozen=True)
class EproFrame:
    """Where precis' mm / +Y-up frame sits in the file's mil / +Y-down one.

    ``x0_mm`` is the board-frame X that lands on mil X = 0, ``y1_mm`` the
    board-frame Y that lands on mil Y = 0 (Y flips, so it is the TOP of
    the drawn extent). Chosen so every written coordinate is positive;
    :class:`precis.pcb.epro.Frame` re-derives its own offsets from the
    outline, so the exact values never matter on the way back in.
    """

    x0_mm: float
    y1_mm: float


def to_epro_xy(x_mm: float, y_mm: float, frame: EproFrame) -> tuple[float, float]:
    """Board mm (+Y up) -> file mil (+Y down), the exact inverse of
    :meth:`precis.pcb.epro.Frame.xy` up to the origin translation."""
    return to_mils(x_mm - frame.x0_mm), to_mils(frame.y1_mm - y_mm)


def to_footprint_xy(
    pad_xy_mm: tuple[float, float], inst: dict[str, Any]
) -> tuple[float, float]:
    """A board-frame position as AUTHORED footprint-local mm (+Y up): the
    exact inverse of :func:`precis.pcb.pose.point_to_world` — un-rotate by
    the instance's (clockwise) ``rot``, then un-mirror X for a bottom
    part. The footprint document stores ``(x, -y)`` of this in mils.
    """
    dx = float(pad_xy_mm[0]) - float(inst["x"])
    dy = float(pad_xy_mm[1]) - float(inst["y"])
    ux, uy = pose.rotate_cw(dx, dy, -float(inst.get("rot") or 0.0))
    return (-ux, uy) if padplace.is_bottom_instance(inst) else (ux, uy)


def epro_angle(inst: dict[str, Any]) -> float:
    """The ``COMPONENT.angle`` for an instance: precis ``rot`` on top,
    ``(rot + 180) % 360`` on the bottom — the exact inverse of the
    reader's ``rot = (angle + 180 if bottom) % 360``
    (:func:`precis.pcb.epro.extract_components`)."""
    rot = float(inst.get("rot") or 0.0) % 360.0
    return (rot + 180.0) % 360.0 if padplace.is_bottom_instance(inst) else rot


def arc_sweep_deg(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    *,
    cw: bool,
) -> float:
    """The signed ``ARC.angle`` for a precis arc segment (board mm, +Y up).

    The reader (:func:`precis.pcb.epro._segment`) decides ``cw`` from the
    stored sign alone — ``cw = sweep > 0`` — because the Y flip is a
    reflection and reverses handedness. So the magnitude is the angle
    swept around ``center`` from ``start`` to ``end`` in the arc's own
    direction, and the sign is ``+`` for ``cw``. ``0`` means the arc has
    no finite centre (start == end, or a degenerate centre) and is the
    caller's cue to drop it: the reader drops a 0/360 sweep too.
    """
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    mag = (a0 - a1) % (2 * math.pi) if cw else (a1 - a0) % (2 * math.pi)
    deg = math.degrees(mag)
    if deg <= 0.0 or deg >= 360.0:
        return 0.0
    return deg if cw else -deg


def copper_layer_ids(layers: list[str]) -> dict[str, int]:
    """precis layer name -> ``layerId``, the inverse of
    :func:`precis.pcb.epro.copper_layers` over the ids
    :func:`_layer_records` declares: top 1, bottom 2, inner ``i`` at
    ``15 + i`` (spike-observed on the real 4-layer board: 1, 15, 16, 2)."""
    if len(layers) < 2:
        return {}
    ids = {str(layers[0]): _L_TOP, str(layers[-1]): _L_BOTTOM}
    for i, name in enumerate(layers[1:-1]):
        ids[str(name)] = _L_INNER0 + i
    return ids


def frame_for(model: dict[str, Any]) -> EproFrame:
    """The frame enclosing the outline, every placed instance, every pad
    and every copper endpoint (plus a margin), so a part hanging off the
    board still lands at a positive coordinate."""
    xs: list[float] = []
    ys: list[float] = []
    for x, y in model.get("outline") or []:
        xs.append(float(x))
        ys.append(float(y))
    for item in model.get("copper") or []:
        if item.get("ctype") == "via" and item.get("x") is not None:
            xs.append(float(item["x"]))
            ys.append(float(item["y"]))
        for seg in item.get("segments") or []:
            for px, py in (seg.get("start"), seg.get("end")):
                xs.append(float(px))
                ys.append(float(py))
    for inst in model.get("instances") or []:
        if inst.get("x") is not None and inst.get("y") is not None:
            xs.append(float(inst["x"]))
            ys.append(float(inst["y"]))
    for pad in model.get("pads") or []:
        w = float(pad.get("w") or 0.0)
        h = float(pad.get("h", w) or 0.0)
        xs += [float(pad["x"]) - w / 2.0, float(pad["x"]) + w / 2.0]
        ys += [float(pad["y"]) - h / 2.0, float(pad["y"]) + h / 2.0]
        for vx, vy in pad.get("poly") or []:
            xs.append(float(vx))
            ys.append(float(vy))
    if not xs:
        return EproFrame(0.0, 0.0)
    return EproFrame(x0_mm=min(xs) - _FRAME_MARGIN_MM, y1_mm=max(ys) + _FRAME_MARGIN_MM)


# ── record plumbing ─────────────────────────────────────────────────────
def _uuid_hex(slug: str, role: str) -> str:
    return uuid.uuid5(_NS, f"{slug}:{role}").hex


def _rid(slug: str, role: str) -> str:
    """A stable record id: pure function of slug + role, short enough to
    read in a diff."""
    return "e" + _uuid_hex(slug, role)[:15]


def _dumps(obj: Any) -> str:
    # allow_nan=False: a non-finite float would serialize as the bare token
    # NaN/Infinity, which is invalid JSON and crashes Pro's parser the way a
    # null coordinate did ("NaN is not permitted" in addText, 2026-10-14).
    return json.dumps(obj, separators=(",", ":"), allow_nan=False)


class _Stream:
    """The ``.epru`` text under construction: ``<header>||<body>|`` lines
    and one monotonic ticket counter shared by every document."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._ticket = 0

    def z(self) -> int:
        """The ticket the NEXT record gets — for a body's own ``zIndex``
        slot, which Pro orders records by."""
        return self._ticket + 1

    def record(
        self, type_: str, body: dict[str, Any] | None, rid: str | None = None
    ) -> None:
        self._ticket += 1
        header: dict[str, Any] = {"type": type_, "ticket": self._ticket}
        if rid is not None:
            header["id"] = rid
        head = _dumps(header)
        if "||" in head:
            # The reader splits on the FIRST "||"; a header containing one
            # (a net named ``A||B``) would be cut mid-id.
            raise ValueError(
                f"{type_} record id {rid!r} contains '||', which the .epru "
                f"record separator cannot carry"
            )
        payload = '""' if body is None else _dumps(body)
        self._lines.append(f"{head}||{payload}|")

    def dochead(self, doc_type: str, doc_uuid: str, client: str) -> None:
        # The shape Pro writes: ``client``/``version`` are the exporter's
        # own hex tag and the updateTime AS A STRING, ``user`` an object —
        # a bare-string user is one of the field shapes its parser chokes
        # on (the 2026-10-14 load loop).
        self.record(
            "DOCHEAD",
            {
                "docType": doc_type,
                "client": client,
                "uuid": doc_uuid,
                "updateTime": _UPDATE_TIME_MS,
                "version": str(_UPDATE_TIME_MS),
                "editVersion": _EDITOR_VERSION,
                "user": _USER_OBJ,
            },
        )

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


# ── layers ──────────────────────────────────────────────────────────────
#: The complete LAYER palette Pro writes on EVERY document of an exported
#: project (93 records on TAZ V1.15's PCB doc, identically on each of its
#: 145 FOOTPRINT docs), verbatim from Pro 3.2.206's own file.
#: ``(layerId, layerType, layerName, use, show, activeColor,
#: inactiveColor, activateTransparency, inactiveTransparency)`` — the
#: color/transparency fields are what Pro's ``parseLayer`` reads with
#: ``startsWith``; an absent ``activeColor`` was the 13x TypeError in the
#: 2026-10-14 load loop.
_LAYER_TABLE: tuple[tuple[int, str, str, bool, bool, str, str, float, float], ...] = (
    (1, "TOP", "Top Layer", True, True, "#ff0000", "#ff0000", 1, 1),
    (2, "BOTTOM", "Bottom Layer", True, True, "#0000ff", "#0000ff", 1, 1),
    (3, "TOP_SILK", "Top Silkscreen Layer", True, True, "#ffcc00", "#ffcc00", 1, 1),
    (4, "BOT_SILK", "Bottom Silkscreen Layer", True, True, "#66cc33", "#66cc33", 1, 1),
    (
        7,
        "TOP_PASTE_MASK",
        "Top Paste Mask Layer",
        True,
        True,
        "#808080",
        "#808080",
        1,
        1,
    ),
    (
        8,
        "BOT_PASTE_MASK",
        "Bottom Paste Mask Layer",
        True,
        True,
        "#800000",
        "#800000",
        1,
        1,
    ),
    (
        5,
        "TOP_SOLDER_MASK",
        "Top Solder Mask Layer",
        True,
        True,
        "#800080",
        "#800080",
        0.7,
        0.7,
    ),
    (
        6,
        "BOT_SOLDER_MASK",
        "Bottom Solder Mask Layer",
        True,
        True,
        "#aa00ff",
        "#aa00ff",
        0.7,
        0.7,
    ),
    (57, "OTHER", "Ratline Layer", True, True, "#6464ff", "#6464ff", 1, 1),
    (11, "OUTLINE", "Board Outline Layer", True, True, "#ff00ff", "#ff00ff", 1, 1),
    (12, "MULTI", "Multi-Layer", True, True, "#c0c0c0", "#c0c0c0", 1, 1),
    (13, "DOCUMENT", "Document Layer", True, True, "#ffffff", "#ffffff", 1, 1),
    (9, "TOP_ASSEMBLY", "Top Assembly Layer", False, False, "#33cc99", "#33cc99", 1, 1),
    (
        10,
        "BOT_ASSEMBLY",
        "Bottom Assembly Layer",
        False,
        False,
        "#5555ff",
        "#5555ff",
        1,
        1,
    ),
    (14, "MECHANICAL", "Mechanical Layer", False, False, "#f022f0", "#f022f0", 1, 1),
    (
        52,
        "COMPONENT_MODEL",
        "Component Model Layer",
        False,
        False,
        "#66ccff",
        "#66ccff",
        1,
        1,
    ),
    (15, "SIGNAL", "Inner1", True, True, "#ff3399", "#ff3399", 1, 1),
    (16, "SIGNAL", "Inner2", True, True, "#008000", "#008000", 1, 1),
    (17, "SIGNAL", "Inner3", False, False, "#00ff00", "#00ff00", 1, 1),
    (18, "SIGNAL", "Inner4", False, False, "#bc8e00", "#bc8e00", 1, 1),
    (19, "SIGNAL", "Inner5", False, False, "#70dbfa", "#70dbfa", 1, 1),
    (20, "SIGNAL", "Inner6", False, False, "#00cc66", "#00cc66", 1, 1),
    (21, "SIGNAL", "Inner7", False, False, "#9966ff", "#9966ff", 1, 1),
    (22, "SIGNAL", "Inner8", False, False, "#800080", "#800080", 1, 1),
    (23, "SIGNAL", "Inner9", False, False, "#008080", "#008080", 1, 1),
    (24, "SIGNAL", "Inner10", False, False, "#15935f", "#15935f", 1, 1),
    (25, "SIGNAL", "Inner11", False, False, "#000080", "#000080", 1, 1),
    (26, "SIGNAL", "Inner12", False, False, "#00b400", "#00b400", 1, 1),
    (27, "SIGNAL", "Inner13", False, False, "#2e4756", "#2e4756", 1, 1),
    (28, "SIGNAL", "Inner14", False, False, "#99842f", "#99842f", 1, 1),
    (29, "SIGNAL", "Inner15", False, False, "#ffffaa", "#ffffaa", 1, 1),
    (30, "SIGNAL", "Inner16", False, False, "#99842f", "#99842f", 1, 1),
    (31, "SIGNAL", "Inner17", False, False, "#2e4756", "#2e4756", 1, 1),
    (32, "SIGNAL", "Inner18", False, False, "#3535ff", "#3535ff", 1, 1),
    (33, "SIGNAL", "Inner19", False, False, "#8000bc", "#8000bc", 1, 1),
    (34, "SIGNAL", "Inner20", False, False, "#43ae5f", "#43ae5f", 1, 1),
    (35, "SIGNAL", "Inner21", False, False, "#c3ecce", "#c3ecce", 1, 1),
    (36, "SIGNAL", "Inner22", False, False, "#728978", "#728978", 1, 1),
    (37, "SIGNAL", "Inner23", False, False, "#39503f", "#39503f", 1, 1),
    (38, "SIGNAL", "Inner24", False, False, "#0c715d", "#0c715d", 1, 1),
    (39, "SIGNAL", "Inner25", False, False, "#5a8a80", "#5a8a80", 1, 1),
    (40, "SIGNAL", "Inner26", False, False, "#2b937e", "#2b937e", 1, 1),
    (41, "SIGNAL", "Inner27", False, False, "#23999d", "#23999d", 1, 1),
    (42, "SIGNAL", "Inner28", False, False, "#45b4e3", "#45b4e3", 1, 1),
    (43, "SIGNAL", "Inner29", False, False, "#215da1", "#215da1", 1, 1),
    (44, "SIGNAL", "Inner30", False, False, "#4564d7", "#4564d7", 1, 1),
    (45, "SIGNAL", "Inner31", False, False, "#6969e9", "#6969e9", 1, 1),
    (46, "SIGNAL", "Inner32", False, False, "#9069e9", "#9069e9", 1, 1),
    (
        48,
        "COMPONENT_SHAPE",
        "Component Shape Layer",
        False,
        False,
        "#00cccc",
        "#00cccc",
        1,
        1,
    ),
    (
        51,
        "PIN_FLOATING",
        "Pin Floating Layer",
        False,
        False,
        "#cc9999",
        "#cc9999",
        1,
        1,
    ),
    (
        49,
        "COMPONENT_MARKING",
        "Component Marking Layer",
        False,
        False,
        "#66ffcc",
        "#66ffcc",
        1,
        1,
    ),
    (47, "HOLE", "Hole Layer", True, True, "#222222", "#111111", 1, 0.5),
    (
        50,
        "PIN_SOLDERING",
        "Pin Soldering Layer",
        True,
        False,
        "#cc9999",
        "#664c4c",
        1,
        0.5,
    ),
    (
        53,
        "3D_SHELL_OUTLINE",
        "3D Shell Outline Layer",
        True,
        True,
        "#66ff99",
        "#337f4c",
        1,
        0.5,
    ),
    (
        54,
        "3D_SHELL_TOP",
        "3D Shell Top Layer",
        True,
        True,
        "#ffccff",
        "#7f667f",
        1,
        0.5,
    ),
    (
        55,
        "3D_SHELL_BOTTOM",
        "3D Shell Bottom Layer",
        True,
        True,
        "#0066cc",
        "#003366",
        1,
        0.5,
    ),
    (
        56,
        "DRILL_DRAWING",
        "Drill Drawing Layer",
        True,
        True,
        "#008080",
        "#004040",
        1,
        0.5,
    ),
    (
        58,
        "TOP_STIFFENER",
        "Top Stiffener Layer",
        False,
        False,
        "#eee666",
        "#777333",
        1,
        0.5,
    ),
    (
        59,
        "BOTTOM_STIFFENER",
        "Bottom Stiffener Layer",
        False,
        False,
        "#ccff00",
        "#667f00",
        1,
        0.5,
    ),
    (71, "CUSTOM", "Custom1", False, False, "#9069e9", "#483474", 1, 0.5),
    (72, "CUSTOM", "Custom2", False, False, "#6969e9", "#343474", 1, 0.5),
    (73, "CUSTOM", "Custom3", False, False, "#4564d7", "#22326b", 1, 0.5),
    (74, "CUSTOM", "Custom4", False, False, "#215da1", "#102e50", 1, 0.5),
    (75, "CUSTOM", "Custom5", False, False, "#45b4e3", "#225a71", 1, 0.5),
    (76, "CUSTOM", "Custom6", False, False, "#23999d", "#114c4e", 1, 0.5),
    (77, "CUSTOM", "Custom7", False, False, "#2b937e", "#15493f", 1, 0.5),
    (78, "CUSTOM", "Custom8", False, False, "#5a8a80", "#2d4540", 1, 0.5),
    (79, "CUSTOM", "Custom9", False, False, "#0c715d", "#06382e", 1, 0.5),
    (80, "CUSTOM", "Custom10", False, False, "#39503f", "#1c281f", 1, 0.5),
    (81, "CUSTOM", "Custom11", False, False, "#728978", "#39443c", 1, 0.5),
    (82, "CUSTOM", "Custom12", False, False, "#c3ecce", "#617667", 1, 0.5),
    (83, "CUSTOM", "Custom13", False, False, "#43ae5f", "#21572f", 1, 0.5),
    (84, "CUSTOM", "Custom14", False, False, "#8000bc", "#40005e", 1, 0.5),
    (85, "CUSTOM", "Custom15", False, False, "#3535ff", "#1a1a7f", 1, 0.5),
    (86, "CUSTOM", "Custom16", False, False, "#2e4756", "#17232b", 1, 0.5),
    (87, "CUSTOM", "Custom17", False, False, "#99842f", "#4c4217", 1, 0.5),
    (88, "CUSTOM", "Custom18", False, False, "#ffffaa", "#7f7f55", 1, 0.5),
    (89, "CUSTOM", "Custom19", False, False, "#99842f", "#4c4217", 1, 0.5),
    (90, "CUSTOM", "Custom20", False, False, "#2e4756", "#17232b", 1, 0.5),
    (91, "CUSTOM", "Custom21", False, False, "#00b400", "#005a00", 1, 0.5),
    (92, "CUSTOM", "Custom22", False, False, "#000080", "#000040", 1, 0.5),
    (93, "CUSTOM", "Custom23", False, False, "#15935f", "#00000a", 1, 0.5),
    (94, "CUSTOM", "Custom24", False, False, "#008080", "#004040", 1, 0.5),
    (95, "CUSTOM", "Custom25", False, False, "#800080", "#400040", 1, 0.5),
    (96, "CUSTOM", "Custom26", False, False, "#9966ff", "#4c337f", 1, 0.5),
    (97, "CUSTOM", "Custom27", False, False, "#00cc66", "#006633", 1, 0.5),
    (98, "CUSTOM", "Custom28", False, False, "#70dbfa", "#386d7d", 1, 0.5),
    (99, "CUSTOM", "Custom29", False, False, "#bc8e00", "#5e4700", 1, 0.5),
    (100, "CUSTOM", "Custom30", False, False, "#00ff00", "#007f00", 1, 0.5),
    (361, "SUBSTRATE", "Dielectric1", False, False, "#000000", "#000000", 1, 0.5),
    (362, "SUBSTRATE", "Dielectric2", False, False, "#000000", "#000000", 1, 0.5),
    (363, "SUBSTRATE", "Dielectric3", False, False, "#000000", "#000000", 1, 0.5),
    (364, "SUBSTRATE", "Dielectric4", False, False, "#000000", "#000000", 1, 0.5),
)

#: The 13 ``LAYER_PHYS`` stackup rows Pro writes on every document
#: ``(["LAYER_PHYS", n], body)``, verbatim.
_LAYER_PHYS_TABLE: tuple[tuple[int, dict[str, Any]], ...] = (
    (
        1,
        {
            "material": "",
            "thickness": 1.379,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 4,
        },
    ),
    (
        3,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 1,
        },
    ),
    (
        5,
        {
            "material": "",
            "thickness": 0.394,
            "permittivity": 3.3,
            "lossTangent": 0.02,
            "isKeepIsland": True,
            "zIndex": 3,
        },
    ),
    (
        7,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 2,
        },
    ),
    (
        2,
        {
            "material": "",
            "thickness": 1.378,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 10,
        },
    ),
    (
        4,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 13,
        },
    ),
    (
        6,
        {
            "material": "",
            "thickness": 0.394,
            "permittivity": 3.3,
            "lossTangent": 0.02,
            "isKeepIsland": True,
            "zIndex": 11,
        },
    ),
    (
        8,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": True,
            "zIndex": 12,
        },
    ),
    (
        15,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": False,
            "zIndex": 6,
        },
    ),
    (
        16,
        {
            "material": "",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": False,
            "zIndex": 8,
        },
    ),
    (
        362,
        {
            "material": "FR4",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": False,
            "zIndex": 5,
        },
    ),
    (
        363,
        {
            "material": "FR4",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": False,
            "zIndex": 7,
        },
    ),
    (
        364,
        {
            "material": "FR4",
            "thickness": 0,
            "permittivity": 0,
            "lossTangent": 0,
            "isKeepIsland": False,
            "zIndex": 9,
        },
    ),
)

#: Pro's default ``PREFERENCE`` body, verbatim.
_PREFERENCE_BODY: dict[str, Any] = {
    "startTrackWidthFollowLast": False,
    "lastTrackWidth": 10,
    "startViaSizeFollowLast": False,
    "lastViaInnerDiameter": 24,
    "lastViaDiameter": 12,
    "snap": True,
    "routingMode": "NONE",
    "routingCorner": "L45",
    "removeLoop": True,
    "rotatingObject": True,
    "trackFollow": None,
    "stretchTrackMinCorner": 1,
    "preferenceConfig": "",
    "realTimeUpdateUnusedLayers": False,
    "unusedPadRange": "ALL",
    "pushVia": "OPTIMIZA_NONE",
    "pathOptimization4BePushed": "NONE",
    "currentPathOptimization4BePushed": "OPTIMIZA_NONE",
    "removeCircuitsContainingVias": True,
    "removeAntenna": True,
}

#: The 45 ``PRIMITIVE`` display/pick toggles, verbatim (name -> display,
#: pick).
_PRIMITIVE_TABLE: tuple[tuple[str, bool, bool], ...] = (
    ("ALL", True, False),
    ("COMPONENT", True, True),
    ("COMPONENTSILK", True, False),
    ("PROPERTY", True, True),
    ("DESIGNATOR", True, True),
    ("TRACK", True, True),
    ("PAD", True, True),
    ("TESTPOINT", True, True),
    ("PADSPAIR", True, False),
    ("VIA", True, True),
    ("SUTUREHOLE", True, True),
    ("TEXT", True, True),
    ("IMAGE", True, True),
    ("PICTURE", True, True),
    ("DIMENSION", True, True),
    ("FPC_STIFFENER", True, True),
    ("BOARDOUTLINE", True, True),
    ("BOARDSHAPE", True, True),
    ("SLOTREGION", True, True),
    ("PADMETALLICDRILLING", True, True),
    ("PADNONMETALLICDRILLING", True, True),
    ("VIADRILLING", True, True),
    ("COPPEROUTLINE", True, True),
    ("COPPERFILLED", True, True),
    ("PLANE", True, True),
    ("FILLREGIEN", True, True),
    ("PROHIBITEDREGION", True, True),
    ("LINE", True, True),
    ("CONSTRAINT", True, True),
    ("SHELL", True, True),
    ("BOSS", True, True),
    ("CREASE", True, True),
    ("TOPSHELLCUT", True, True),
    ("SIDESHELLCUT", True, True),
    ("TOPBOTTOMENTITY", True, True),
    ("SIDEENTITY", True, True),
    ("D3BODY", True, True),
    ("NETWORK", True, False),
    ("DRC", True, True),
    ("TEARDROP", True, True),
    ("RATLINE", True, True),
    ("GROUP", True, False),
    ("PARTITION", True, True),
    ("LOCKED", True, True),
    ("UNLOCKED", True, True),
)

#: ``CANVAS`` on a FOOTPRINT doc is the mil/grid block; on the PCB doc the
#: same fields with the board's mm origin/grid.
_CANVAS_FP: dict[str, Any] = {
    "originX": 0,
    "originY": 0,
    "unit": "mil",
    "gridXSize": 5,
    "gridYSize": 5,
    "snapXSize": 5,
    "snapYSize": 5,
    "altSnapXSize": 5,
    "altSnapYSize": 5,
    "gridType": "NONE",
    "multiGridType": "NONE",
    "multiGridRatio": 5,
    "highlightValue": 0.5,
    "layerBrightness": "NORMAL",
}
_CANVAS_PCB: dict[str, Any] = {
    **_CANVAS_FP,
    "unit": "mm",
    "gridXSize": 78.74016,
    "gridYSize": 78.74016,
    "snapXSize": 0.03937,
    "snapYSize": 0.03937,
    "altSnapXSize": 0.03937,
    "altSnapYSize": 0.03937,
}
_SILK_OPTS_BODY: dict[str, Any] = {"defaultColor": "#000000", "baseColor": "#ffffff"}


def _layer_records(n_inner: int) -> list[tuple[dict[str, Any], str]]:
    """The full LAYER palette as ``(body, record-id)`` pairs — one per row
    of :data:`_LAYER_TABLE`, with the board's own inner signal layers
    marked ``use``/``show`` the way Pro marks a live layer. The body
    carries NO ``layerId`` (the number lives only in the record id) —
    that is the field shape Pro writes."""
    used_inner = {_L_INNER0 + i for i in range(max(0, n_inner))}
    out = []
    for lid, ltype, lname, use, show, ac, ic, at, it in _LAYER_TABLE:
        if lid in used_inner or lid in (1, 2):
            use = show = True
        body: dict[str, Any] = {
            "layerType": ltype,
            "layerName": lname,
            "use": use,
            "show": show,
            "locked": False,
            "activeColor": ac,
            "inactiveColor": ic,
            "activateTransparency": at,
            "inactiveTransparency": it,
        }
        out.append((body, _dumps(["LAYER", lid])))
    return out


def _board_state(
    s: _Stream, slug: str, *, present: set[str], pcb_meta_title: str
) -> None:
    """The geometry-free board records a fresh export needs and a
    round-trip gets from ``meta.epro_board`` instead — emitted only for
    the record types the passthrough did NOT already write, so a re-export
    never doubles a record Pro stores verbatim."""
    if "CANVAS" not in present:
        s.record("CANVAS", dict(_CANVAS_PCB), "CANVAS")
    if "META" not in present:
        s.record(
            "META",
            {
                "title": pcb_meta_title,
                "parent": "",
                "source": "",
                "board": _uuid_hex(slug, "board")[:16],
                "zIndex": None,
            },
            "META",
        )
    if "ACTIVE_LAYER" not in present:
        s.record("ACTIVE_LAYER", {"layerId": 1}, "ACTIVE_LAYER")
    if "PREFERENCE" not in present:
        s.record("PREFERENCE", dict(_PREFERENCE_BODY), "PREFERENCE")
    if "SILK_OPTS" not in present:
        for lid in (3, 4):
            s.record("SILK_OPTS", dict(_SILK_OPTS_BODY), _dumps(["SILK_OPTS", lid]))
    if "PRIMITIVE" not in present:
        for name, display, pick in _PRIMITIVE_TABLE:
            s.record(
                "PRIMITIVE",
                {"display": display, "pick": pick},
                _dumps(["PRIMITIVE", name]),
            )
    if "LAYER_PHYS" not in present:
        for lid, body in _LAYER_PHYS_TABLE:
            s.record("LAYER_PHYS", dict(body), _dumps(["LAYER_PHYS", lid]))


def _footprint_state(s: _Stream, name: str, n_inner: int) -> None:
    """A FOOTPRINT doc's own copy of the board-state block — every one of
    Pro's library docs carries the same 93-layer palette, stackup table,
    canvas and preferences."""
    s.record("CANVAS", dict(_CANVAS_FP), "CANVAS")
    s.record("META", {"title": name, "tags": [], "source": ""}, "META")
    for body, rid in _layer_records(n_inner):
        s.record("LAYER", body, rid)
    for lid, body in _LAYER_PHYS_TABLE:
        s.record("LAYER_PHYS", dict(body), _dumps(["LAYER_PHYS", lid]))
    s.record("ACTIVE_LAYER", {"layerId": 1}, "ACTIVE_LAYER")
    s.record("PREFERENCE", dict(_PREFERENCE_BODY), "PREFERENCE")


# ── pads ────────────────────────────────────────────────────────────────
_PAD_TYPES = {
    "circle": "ELLIPSE",
    "rect": "RECT",
    "obround": "OVAL",
    "polygon": "POLYGON",
}


@dataclass
class _Part:
    inst: dict[str, Any]
    refdes: str
    bottom: bool
    pads: list[dict[str, Any]] = field(default_factory=list)
    #: ``pads`` un-placed into the authored footprint frame — populated by
    #: :func:`_group_pads` once every board pad is filed.
    local_pads: list[_LocalPad] = field(default_factory=list)


def _group_pads(model: dict[str, Any], parts: dict[str, _Part]) -> int:
    """File each model pad under its part, collapsing the per-copper-layer
    repeats of a through-hole pad (same refdes, pin, position) to one.
    Returns how many pads belong to no placed part (fiducials, stray rows)."""
    seen: set[tuple[str, str, float, float]] = set()
    orphans = 0
    for pad in model.get("pads") or []:
        part = parts.get(str(pad.get("refdes") or ""))
        if part is None:
            orphans += 1
            continue
        key = (
            part.refdes,
            str(pad.get("pin") or ""),
            round(float(pad["x"]), 4),
            round(float(pad["y"]), 4),
        )
        if key in seen:
            continue
        seen.add(key)
        part.pads.append(pad)
    for part in parts.values():
        part.local_pads = [_local_pad(p, part.inst) for p in part.pads]
    return orphans


@dataclass
class _FpGroup:
    """The placed instances that share one ``FOOTPRINT`` document — the
    set that un-places to the same authored pads (and footprint holes).
    Content-keyed, never name-keyed: two cache rows may carry the same
    footprint name with different geometry, and the reader's own
    ``name~hash`` disambiguation proves equal names need not be equal
    footprints. ``index`` maps a pad's :meth:`_LocalPad.canon` to its
    position in ``pads``, so each member's ``PAD_NET`` can point at the
    shared pad element."""

    name: str
    pads: list[_LocalPad]
    holes: list[dict[str, Any]]
    members: list[str]
    uuid: str = ""
    index: dict[tuple[Any, ...], int] = field(default_factory=dict)


def _footprint_groups(
    parts: dict[str, _Part],
    holes_by_refdes: dict[str, list[dict[str, Any]]],
) -> list[_FpGroup]:
    """Dedup placed parts into one group per identical authored pad set +
    footprint-hole set. Names are picked deterministically (the lowest
    member footprint name, ``~N``-suffixed when distinct groups share
    one), so the file does not depend on model ordering."""
    keyed: dict[str, _FpGroup] = {}
    for refdes in sorted(parts):
        part = parts[refdes]
        holes = sorted(
            holes_by_refdes.get(refdes) or [],
            key=lambda h: (float(h["x"]), float(h["y"]), float(h["dia_mm"])),
        )
        key = json.dumps(
            [
                [p.canon() for p in part.local_pads],
                [
                    [
                        round(float(h["x"]), 4),
                        round(float(h["y"]), 4),
                        round(float(h["dia_mm"]), 4),
                    ]
                    for h in holes
                ],
            ]
        )
        group = keyed.get(key)
        if group is None:
            group = _FpGroup(
                name="",
                pads=part.local_pads,
                holes=[dict(h) for h in holes],
                members=[],
            )
            group.index = {p.canon(): i for i, p in enumerate(part.local_pads)}
            keyed[key] = group
        group.members.append(refdes)

    groups = [keyed[k] for k in sorted(keyed)]
    # Name after the content sort, so a "~2" never depends on which
    # instance the model listed first.
    seen_names: dict[str, int] = {}
    for group in groups:
        names = sorted(
            {
                str(parts[r].inst.get("footprint") or "")
                for r in group.members
                if str(parts[r].inst.get("footprint") or "")
            }
        )
        base = names[0] if names else "FP"
        seen_names[base] = seen_names.get(base, 0) + 1
        group.name = base if seen_names[base] == 1 else f"{base}~{seen_names[base]}"
    return groups


def _poly_path(points: list[tuple[float, float]]) -> list[Any]:
    """``POLY.path`` shape: ``[x0, y0, "L", x1, y1, x2, y2, ...]``."""
    path: list[Any] = []
    for i, (x, y) in enumerate(points):
        if i == 1:
            path.append("L")
        path += [x, y]
    return path


# ── footprint-local pads (slice 2f) ────────────────────────────────────
@dataclass
class _LocalPad:
    """One pad expressed in the footprint's AUTHORED frame (mm, +Y up) —
    what a shared ``FOOTPRINT`` document stores. ``num`` is the pin
    label the model carries (``padplace.board_pads`` resolves it through
    the footprint's pin_map); two instances share a document exactly when
    their local pad sets agree, fields rounded to the same 0.0001-mm
    quantum :func:`_group_pads` dedups board pads on."""

    num: str
    shape: str
    x: float
    y: float
    rot: float  # padAngle, degrees
    w: float
    h: float
    net: str = ""
    drill: float | None = None
    slot: tuple[float, float] | None = None  # (width, height) in the pad frame
    slot_rounded: bool = False  # an off-axis slot written as a round hole
    poly: tuple[tuple[float, float], ...] | None = None

    def canon(self) -> tuple[Any, ...]:
        """The identity a footprint group dedups on. Rounded to 1 um:
        un-placing through an odd instance angle and back accumulates
        ~0.1 um of float noise, and library footprints differ by tens of
        microns at the smallest — 1 um splits the noise without ever
        merging two real footprints."""
        return (
            self.num,
            self.shape,
            round(self.x, 3),
            round(self.y, 3),
            round(self.rot, 3),
            round(self.w, 3),
            round(self.h, 3),
            None if self.drill is None else round(self.drill, 3),
            self.slot,
            (
                tuple((round(vx, 3), round(vy, 3)) for vx, vy in self.poly)
                if self.poly
                else None
            ),
        )


def _local_pad(pad: dict[str, Any], inst: dict[str, Any]) -> _LocalPad:
    """Un-place one board-frame model pad into its instance's authored
    footprint frame.

    Position inverts exactly through :func:`to_footprint_xy`. The
    ``w``/``h``/``padAngle`` triple needs the decomposition the board pad
    only partly carries: ``board_pads`` emits AUTHORED ``w``/``h`` (swapped
    only when the total rotation lands on 90 mod 180) and marks a
    non-axis-aligned placed pad ``oblique_rot`` = instance + pad rotation.
    From that: an oblique pad's authored angle is ``oblique_rot - rot``
    exactly; an axis-aligned one is written ``padAngle 0`` with ``w``/``h``
    un-swapped by the instance rotation — rendered identically (module
    docstring, "A pad's authored rotation").
    """
    bottom = padplace.is_bottom_instance(inst)
    rot = float(inst.get("rot") or 0.0)
    lx, ly = to_footprint_xy((float(pad["x"]), float(pad["y"])), inst)
    shape = str(pad.get("shape") or "rect")
    w = float(pad.get("w") or 0.0)
    h = float(pad.get("h", w) or 0.0)
    oblique = pad.get("oblique_rot")
    pad_rot = 0.0
    if oblique is not None:
        pad_rot = _clean((float(oblique) - rot) % 360.0)
    elif shape in ("rect", "obround") and padplace.rect_swaps_wh(rot):
        w, h = h, w
    local = _LocalPad(
        num=str(pad.get("pin") or ""),
        shape=shape,
        x=lx,
        y=ly,
        rot=pad_rot,
        w=w,
        h=h,
        net=str(pad.get("net") or ""),
        drill=float(pad["drill"]) if pad.get("drill") else None,
    )
    if shape == "polygon" and pad.get("poly"):
        local.poly = tuple(
            to_footprint_xy((float(vx), float(vy)), inst) for vx, vy in pad["poly"]
        )
    if local.drill and pad.get("slot"):
        # model pads carry the slot as board-frame end centres; the hole's
        # w/h live in the PAD frame (padAngle turns both, same as the pad's
        # own w/h). Un-rotate by the total rotation, un-mirroring on the
        # bottom side — a mirror only flips an axis, so the axis check is
        # what survives.
        (sx1, sy1), (sx2, sy2) = pad["slot"]
        dx, dy = float(sx2) - float(sx1), float(sy2) - float(sy1)
        long_ = math.hypot(dx, dy) + local.drill
        ux, uy = pose.rotate_cw(dx, dy, -rot)
        if bottom:
            ux = -ux
        ux, uy = pose.rotate_cw(ux, uy, -pad_rot)
        if abs(uy) < 1e-6:
            local.slot = (round(long_, 4), round(local.drill, 4))
        elif abs(ux) < 1e-6:
            local.slot = (round(local.drill, 4), round(long_, 4))
        else:
            local.slot_rounded = True
    return local


def _attr_body(
    *,
    layer: int,
    parent: str,
    key: str,
    value: str,
    visible: bool,
    x: float,
    y: float,
    angle: float,
    font_size: float,
    z_index: int,
    stroke_width: float = 6,
) -> dict[str, Any]:
    """An ``ATTR`` record in exactly the field shape Pro writes for a
    component/footprint attribute (TAZ V1.15, Pro 3.2.206): every slot a
    text renderer can read — ``x``/``y``/``angle``/``fontSize``/
    ``strokeWidth`` — is a NUMBER even when the attr is invisible,
    because Pro's ``addText`` runs on hidden attrs too (a null ``x`` was
    the ``NaN is not permitted`` crash of 2026-10-14)."""
    return {
        "layerId": layer,
        "groupId": "",
        "locked": False,
        "zIndex": z_index,
        "partitionId": None,
        "parentId": parent,
        "key": key,
        "value": value,
        "keyVisible": False,
        "valueVisible": visible,
        "x": x,
        "y": y,
        "angle": angle,
        "origin": "LEFT_BOTTOM",
        "fontFamily": "default",
        "fontSize": font_size,
        "strokeWidth": stroke_width,
        "expansion": 0,
        "reverse": False,
        "mirror": False,
        "specialColor": None,
    }


def _pad_body(local: _LocalPad, z_index: int) -> dict[str, Any]:
    """One footprint ``PAD`` body in the authored frame — ``(x, -y)`` in
    mils, the exact inverse of the reader's ``_pad_to_precis``. The field
    set is Pro's own (TAZ V1.15): ``relativeAngle`` is descriptive, not a
    second rotation — 90 iff the stored rect is portrait (``h > w``);
    a pad with no drill still carries an empty ``hole``."""
    default: dict[str, Any] = {"padType": _PAD_TYPES.get(local.shape, "RECT")}
    if local.shape == "polygon" and local.poly:
        default["path"] = _poly_path(
            [(to_mils(vx), to_mils(-vy)) for vx, vy in local.poly]
        )
    else:
        default["width"] = to_mils(local.w)
        default["height"] = to_mils(local.w if local.shape == "circle" else local.h)
        if local.shape == "rect":
            default["radius"] = 0
    portrait = local.shape in ("rect", "obround") and local.h > local.w
    body: dict[str, Any] = {
        "layerId": _L_MULTI if local.drill else _L_TOP,
        "groupId": "",
        "locked": False,
        "zIndex": z_index,
        "partitionId": None,
        "netName": "",
        "num": local.num,
        "centerX": to_mils(local.x),
        "centerY": to_mils(-local.y),
        "padAngle": _clean(round(local.rot, 4)),
        "hole": {"holeType": "ROUND", "width": 0, "height": 0},
        "defaultPad": default,
        "specialPad": [],
        "padOffsetX": 0,
        "padOffsetY": 0,
        "relativeAngle": 90 if portrait else 0,
        "plated": True,
        "padType": "NORMAL",
        "topSolderExpansion": 2,
        "bottomSolderExpansion": 2,
        "topPasteExpansion": 0,
        "bottomPasteExpansion": 0,
        "connectMode": None,
        "spokeSpace": None,
        "spokeWidth": None,
        "spokeAngle": None,
        "unusedInnerLayers": [],
        "refs": [],
    }
    if local.drill:
        d = to_mils(local.drill)
        if local.slot:
            body["hole"] = {
                "holeType": "SLOT",
                "width": to_mils(local.slot[0]),
                "height": to_mils(local.slot[1]),
            }
        else:
            body["hole"] = {"holeType": "ROUND", "width": d, "height": d}
    return body


def _fp_hole_fill(hole: dict[str, Any], z_index: int) -> dict[str, Any]:
    """A footprint-owned non-plated hole as the ``FILL`` ``CIRCLE`` on
    ``MULTI`` the reader's ``_footprint_holes`` parses back
    (:func:`precis.pcb.epro._footprint_holes` — the shape Pro itself
    writes for a footprint's NPTH). ``hole`` is authored-local
    ``{"x","y","dia_mm"}`` in mm."""
    return {
        "layerId": _L_MULTI,
        "groupId": "",
        "locked": False,
        "zIndex": z_index,
        "partitionId": None,
        "netName": "",
        "width": 0.2,
        "fillStyle": "SOLID",
        "path": [
            [
                "CIRCLE",
                to_mils(float(hole["x"])),
                to_mils(-float(hole["y"])),
                to_mils(float(hole["dia_mm"]) / 2.0),
            ]
        ],
        "isBridgingCopper": False,
        "specialColor": None,
    }


def loose_drills(
    model: dict[str, Any],
    parts: dict[str, _Part],
    copper_items: list[dict[str, Any]],
    groups: list[_FpGroup],
) -> list[dict[str, Any]]:
    """``model["drills"]`` rows that no exported pad, via or footprint hole
    already covers — the candidates for free ``PAD`` records. A drill at
    a placed footprint hole's world position is covered by the ``FILL``
    the group writes; everything else is a board-level hole."""
    covered = {
        (round(float(p["x"]), 3), round(float(p["y"]), 3))
        for part in parts.values()
        for p in part.pads
        if p.get("drill")
    } | {
        (round(float(i["x"]), 3), round(float(i["y"]), 3))
        for i in copper_items
        if i.get("ctype") == "via" and i.get("x") is not None
    }
    for group in groups:
        for refdes in group.members:
            inst = parts[refdes].inst
            for h in group.holes:
                wx, wy = pose.point_to_world(
                    float(h["x"]),
                    float(h["y"]),
                    float(inst["x"]),
                    float(inst["y"]),
                    float(inst.get("rot") or 0.0),
                    mirrored=parts[refdes].bottom,
                )
                covered.add((round(wx, 3), round(wy, 3)))
    return [
        d
        for d in sorted(
            (d for d in model.get("drills") or []),
            key=lambda d: (float(d["x"]), float(d["y"])),
        )
        if (round(float(d["x"]), 3), round(float(d["y"]), 3)) not in covered
    ]


def _free_pad_body(
    drill: dict[str, Any], num: str, frame: EproFrame, z_index: int
) -> dict[str, Any]:
    """A board-level drill as a free ``PAD`` — the shape the reader's
    ``extract_mounting_holes`` parses (``plated`` + ``hole.width``). A
    non-plated hole gets a hole-sized pad: there is no copper ring to
    keep. The plated case (a drilled test point that escaped its part)
    gets the same; no net is carried because the drill row has none."""
    dia = to_mils(float(drill["dia_mm"]))
    x, y = to_epro_xy(float(drill["x"]), float(drill["y"]), frame)
    plated = bool(drill.get("plated"))
    return {
        "layerId": _L_TOP if plated else _L_MULTI,
        "groupId": "",
        "locked": False,
        "zIndex": z_index,
        "partitionId": None,
        "netName": "",
        "num": num,
        "centerX": x,
        "centerY": y,
        "padAngle": 0,
        "hole": {"holeType": "ROUND", "width": dia, "height": dia},
        "defaultPad": {"padType": "ELLIPSE", "width": dia, "height": dia},
        "specialPad": [],
        "padOffsetX": 0,
        "padOffsetY": 0,
        "relativeAngle": 0,
        "plated": plated,
        "padType": "NORMAL",
        "topSolderExpansion": 2,
        "bottomSolderExpansion": 2,
        "topPasteExpansion": 0,
        "bottomPasteExpansion": 0,
        "connectMode": None,
        "spokeSpace": None,
        "spokeWidth": None,
        "spokeAngle": None,
        "unusedInnerLayers": [],
        "refs": [],
    }


# ── copper (slices 2c/2d) ───────────────────────────────────────────────
#: ``POUR.width`` / ``pourType.fineness`` exactly as Pro 3.2.149 wrote them
#: on all eight pours of the real 4-layer board (every one a rectangle
#: ``["R", x, y, w, h, angle, 0]`` whose box spans ``x..x+w`` and
#: ``y-h..y`` in the Y-down frame at angle 0 — checked against each net's
#: own copper extent, 2026-10-07).
_POUR_WIDTH = 0.2
_POUR_FINENESS = 8


def _axis_aligned_rect(
    polygon: list[Any],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """``((x0, y0), (x1, y1))`` when ``polygon`` is an axis-aligned
    rectangle (4 corners, optionally closed), else ``None``. The only
    ``POUR`` zone shape observed from Pro is the ``"R"`` rectangle op, so
    that is the only one written."""
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    if len(pts) == 5 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) != 4:
        return None
    xs, ys = sorted({x for x, _ in pts}), sorted({y for _, y in pts})
    if len(xs) != 2 or len(ys) != 2:
        return None
    if {(x, y) for x, y in pts} != {(x, y) for x in xs for y in ys}:
        return None
    return (xs[0], ys[0]), (xs[1], ys[1])


def sorted_copper(model: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's copper items in an order that does not depend on the
    model's (a re-route lists the same rows differently; the file must
    not)."""
    items = [i for i in model.get("copper") or [] if isinstance(i, dict)]
    return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, default=str))


@dataclass
class _CopperOut:
    tracks: int = 0
    lines: int = 0
    arcs: int = 0
    vias: int = 0
    pours: int = 0
    pours_dropped: int = 0
    pours_with_holes: int = 0
    netless: int = 0
    widthless: int = 0
    unknown: int = 0
    dropped: list[str] = field(default_factory=list)
    blind: list[str] = field(default_factory=list)
    off_layer: dict[str, int] = field(default_factory=dict)


def _write_copper(
    s: _Stream,
    items: list[dict[str, Any]],
    *,
    slug: str,
    frame: EproFrame,
    layers: list[str],
) -> _CopperOut:
    """``LINE``/``ARC``/``VIA`` records for every track segment and via in
    ``items`` (already :func:`sorted_copper`). Pours are counted for the
    caller's warning, not written (slice 2d)."""
    ids = copper_layer_ids(layers)
    through = [str(layers[0]), str(layers[-1])] if layers else []
    out = _CopperOut()
    for n, item in enumerate(items):
        ctype = str(item.get("ctype") or "")
        net = str(item.get("net") or "")
        if ctype == "pour":
            lid = ids.get(str(item.get("layer") or ""))
            if lid is None:
                out.pours_dropped += 1
                continue
            polygon = item.get("polygon") or []
            rect = _axis_aligned_rect(polygon)
            if rect is None and len(polygon) < 3:
                out.pours_dropped += 1
                continue
            if item.get("holes"):
                out.pours_with_holes += 1
            out.pours += 1
            if rect is not None:
                (x0, y0), (x1, y1) = rect
                # "R" anchors at (x, y) = min-x/MAX-y in the file frame —
                # the box spans x..x+w, y-h..y (verified on Pro's own
                # write, pcb-epro-export.md slice 2d), which is the
                # board's (x0, y0) corner.
                fx, fy = to_epro_xy(x0, y0, frame)
                path: list[Any] = [
                    ["R", fx, fy, to_mils(x1 - x0), to_mils(y1 - y0), 0, 0]
                ]
            else:
                # The vertex-list "L" path Pro 3.2.206 writes for a
                # free-form zone (observed on TAZ V1.15's four pours).
                # Interior rings are not expressible: Pro re-derives the
                # clearance gaps when it re-pours anyway.
                ring = [(float(p[0]), float(p[1])) for p in polygon]
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                path = [_poly_path([to_epro_xy(px, py, frame) for px, py in ring])]
            pour_rid = _rid(slug, f"pour:{n}")
            s.record(
                "POUR",
                {
                    "layerId": lid,
                    "groupId": "",
                    "locked": False,
                    "zIndex": s.z(),
                    "partitionId": None,
                    "netName": net,
                    "width": _POUR_WIDTH,
                    "name": str(item.get("name") or f"POUR{out.pours}"),
                    "order": 0,
                    "path": path,
                    "pourType": {"pourType": "SOLID", "fineness": _POUR_FINENESS},
                    "keepIsland": bool(item.get("keep_island")),
                },
                pour_rid,
            )
            # Pro caches the computed pour fill in a POURed record id'd
            # ["POURED", <pour element id>]; a file without one opens with
            # the zone unfilled until a manual "rebuild copper". The
            # source's regions ride through meta.epro_pours as "fill" and
            # are re-emitted verbatim — they are display cache only (Pro
            # recomputes on rebuild, gerber regenerates), and pourFill is
            # in 10-MIL units, not mils (the reader's documented trap).
            regions: list[dict[str, Any]] = []
            for region in item.get("fill") or []:
                rings = [
                    _poly_path(
                        [
                            (fx / 10.0, fy / 10.0)
                            for fx, fy in (to_epro_xy(px, py, frame) for px, py in ring)
                        ]
                    )
                    for ring in region.get("paths") or []
                ]
                if rings:
                    regions.append(
                        {
                            "id": str(region.get("id") or f"copper{len(regions)}"),
                            "fill": bool(region.get("fill", True)),
                            "strokeWidth": float(region.get("stroke_width") or 0.0),
                            "path": rings,
                        }
                    )
            if regions:
                s.record(
                    "POURED",
                    {"pourFill": regions},
                    _dumps(["POURED", pour_rid]),
                )
            continue
        if ctype == "via":
            if item.get("x") is None or item.get("y") is None:
                out.dropped.append(f"via on {net or '?'} without a position")
                continue
            vx, vy = float(item["x"]), float(item["y"])
            span = item.get("layers") or item.get("span")
            if span and [str(span[0]), str(span[-1])] != through:
                out.blind.append(f"{net or '?'} at ({vx:.3f}, {vy:.3f})")
            if not net:
                out.netless += 1
            cx, cy = to_epro_xy(vx, vy, frame)
            s.record(
                "VIA",
                {
                    "layerId": _L_MULTI,
                    "groupId": "",
                    "locked": False,
                    "zIndex": s.z(),
                    "partitionId": None,
                    "netName": net,
                    "centerX": cx,
                    "centerY": cy,
                    "holeDiameter": to_mils(float(item.get("drill_mm") or 0.0)),
                    "viaDiameter": to_mils(float(item.get("dia_mm") or 0.0)),
                    "viaType": "NORMAL",
                    "topSolderExpansion": None,
                    "bottomSolderExpansion": None,
                    "unusedInnerLayers": [],
                },
                _rid(slug, f"via:{n}"),
            )
            out.vias += 1
            continue
        if ctype != "track":
            out.unknown += 1
            continue
        layer = str(item.get("layer") or "")
        lid = ids.get(layer)
        if lid is None:
            out.off_layer[layer] = out.off_layer.get(layer, 0) + 1
            continue
        width = float(item.get("width_mm") or 0.0)
        if width <= 0.0:
            out.widthless += 1
            continue
        if not net:
            out.netless += 1
        wrote = 0
        for k, seg in enumerate(item.get("segments") or []):
            a = (float(seg["start"][0]), float(seg["start"][1]))
            b = (float(seg["end"][0]), float(seg["end"][1]))
            if a == b:
                out.dropped.append(
                    f"zero-length {layer} segment on {net or '?'} at "
                    f"({a[0]:.3f}, {a[1]:.3f})"
                )
                continue
            ax, ay = to_epro_xy(a[0], a[1], frame)
            bx, by = to_epro_xy(b[0], b[1], frame)
            body: dict[str, Any] = {
                "layerId": lid,
                "groupId": "",
                "locked": False,
                "zIndex": s.z(),
                "partitionId": None,
                "netName": net,
                "startX": ax,
                "startY": ay,
                "endX": bx,
                "endY": by,
                "width": to_mils(width),
            }
            if seg.get("shape") == "arc":
                c = (float(seg["center"][0]), float(seg["center"][1]))
                sweep = arc_sweep_deg(a, b, c, cw=bool(seg.get("cw", True)))
                if sweep == 0.0:
                    out.dropped.append(
                        f"{layer} arc on {net or '?'} at ({a[0]:.3f}, {a[1]:.3f}) "
                        "has no finite centre (full circle or degenerate)"
                    )
                    continue
                body["angle"] = _clean(round(sweep, 9))
                body["arcType"] = _ARC_TYPE
                s.record("ARC", body, _rid(slug, f"arc:{n}:{k}"))
                out.arcs += 1
            else:
                s.record("LINE", body, _rid(slug, f"line:{n}:{k}"))
                out.lines += 1
            wrote += 1
        if wrote:
            out.tracks += 1
    return out


def _copper_warnings(cu: _CopperOut) -> list[str]:
    """Everything :func:`_write_copper` could not carry, one line each."""
    out: list[str] = []
    if cu.pours:
        out.append(
            f"{cu.pours} pour(s)/plane(s) written as POUR zones with the "
            "source's cached POURed fill where it existed — the cache is "
            "stale if copper moved since import; run rebuild copper in "
            "Pro after edits (antipads, islands, interior holes, "
            "thermals are Pro's own then)"
        )
    if cu.pours_dropped:
        out.append(
            f"{cu.pours_dropped} pour(s)/plane(s) are NOT exported: fewer "
            "than three outline vertices, or a layer the stackup does not "
            "declare"
        )
    if cu.blind:
        shown = ", ".join(cu.blind[:6]) + ("…" if len(cu.blind) > 6 else "")
        out.append(
            f"{len(cu.blind)} blind/buried via(s) written as THROUGH-HOLE "
            f"({shown}) — VIA carries no span in this format; fix their span "
            "in Pro before fabricating"
        )
    if cu.netless:
        out.append(
            f"{cu.netless} copper item(s) carry no net and are written as "
            "netless copper (Pro keeps it; a precis re-import skips it)"
        )
    if cu.widthless:
        out.append(f"{cu.widthless} track(s) have no width and are not exported")
    if cu.off_layer:
        named = ", ".join(f"{k or '?'} ({v})" for k, v in sorted(cu.off_layer.items()))
        out.append(
            f"{sum(cu.off_layer.values())} track(s) sit on a layer the stackup "
            f"does not declare and are not exported: {named}"
        )
    if cu.unknown:
        out.append(f"{cu.unknown} copper item(s) of an unknown kind are not exported")
    if cu.dropped:
        shown = "; ".join(cu.dropped[:4]) + ("…" if len(cu.dropped) > 4 else "")
        out.append(f"{len(cu.dropped)} copper segment(s) dropped: {shown}")
    return out


# ── silk (slice 2e) ─────────────────────────────────────────────────────
@dataclass
class _SilkOut:
    polys: int = 0
    refdes: int = 0
    regions: int = 0
    clear: int = 0


def _write_silk(
    s: _Stream, model: dict[str, Any], *, slug: str, frame: EproFrame
) -> _SilkOut:
    """Every silk stroke draw (:mod:`precis.pcb.silk`'s courtyard outlines,
    pin-1 marks, board-frame text) as a ``POLY`` on the side's silk layer.
    A ``role == "refdes"`` draw is SUPPRESSED: its text travels as the
    editable ``Designator`` ``ATTR`` instead, anchored where these strokes
    would have printed (``model["silk_labels"]``), so a label is never
    doubled. A region fill and a clear-polarity (knockout) draw have no
    observed Pro counterpart and are counted for the warning."""
    out = _SilkOut()
    silk = model.get("silkscreen") or {}
    for side, lid in (("top", _L_SILK_TOP), ("bottom", _L_SILK_BOTTOM)):
        draws = [d for d in silk.get(side) or [] if isinstance(d, dict)]
        draws.sort(key=lambda d: json.dumps(d, sort_keys=True, default=str))
        for k, draw in enumerate(draws):
            if draw.get("polarity") == "clear":
                out.clear += 1
                continue
            if draw.get("shape") == "region":
                out.regions += 1
                continue
            if draw.get("role") == "refdes":
                out.refdes += 1
                continue
            pts: list[tuple[float, float]] = []
            for seg in draw.get("segments") or []:
                a = to_epro_xy(float(seg["start"][0]), float(seg["start"][1]), frame)
                b = to_epro_xy(float(seg["end"][0]), float(seg["end"][1]), frame)
                if not pts or pts[-1] != a:
                    pts.append(a)
                pts.append(b)
            if len(pts) < 2:
                continue
            s.record(
                "POLY",
                {
                    "layerId": lid,
                    "groupId": "",
                    "locked": False,
                    "zIndex": s.z(),
                    "partitionId": None,
                    "netName": "",
                    "width": to_mils(float(draw.get("width_mm") or 0.0)),
                    "path": _poly_path(pts),
                    "polyType": "NORMAL",
                    "specialColor": None,
                },
                _rid(slug, f"silk:{side}:{k}"),
            )
            out.polys += 1
    return out


def _silk_warnings(so: _SilkOut, *, labelled: int) -> list[str]:
    out: list[str] = []
    if so.refdes:
        out.append(
            f"{so.refdes} refdes silk stroke(s) suppressed: designators travel "
            f"as editable ATTR text ({labelled} anchored where the silk pass "
            "put them, the rest at their part origin) — Pro renders them with "
            "its own font, not precis' stroke glyphs"
        )
    if so.regions or so.clear:
        out.append(
            f"{so.regions} silk region fill(s) and {so.clear} knockout "
            "(clear-polarity) stroke(s) are NOT exported — no observed Pro "
            "record carries them"
        )
    return out


# ── the export ──────────────────────────────────────────────────────────
@dataclass
class EproExport:
    """``files`` is ``{member name: text}`` ready for :func:`zip_epro`;
    ``warnings`` is everything the model held that the file does not."""

    files: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _safe_stem(slug: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in slug) or "board"


def epro_files(
    model: dict[str, Any], *, slug: str, allow_synthesized: bool = False
) -> EproExport:
    """The ``.epro2`` members for ``model`` (a :meth:`_fab_model` dict).

    Raises :class:`precis.pcb.gerber.SynthesizedPadError` when any pad is a
    synthesized land-pattern bound, unless ``allow_synthesized`` — the
    colleague's likely next action is to order the board, and a bound
    solders to nothing. Permitted, the refdes are named in a ``STRING`` on
    the document layer, because the response text does not travel with the
    file.
    """
    synthesized = sorted(
        {
            str(p.get("refdes") or "?")
            for p in model.get("pads") or []
            if p.get("synthesized")
        }
    )
    if synthesized and not allow_synthesized:
        raise SynthesizedPadError(
            f"{len(synthesized)} part(s) carry synthesized pad geometry "
            f"({', '.join(synthesized[:5])}{'...' if len(synthesized) > 5 else ''})"
            " — these are land-pattern BOUNDS, not the real footprint, and "
            "must not be fabricated. Cache real footprints, or pass "
            "allow_synthesized=True for a preview-only export."
        )

    warnings: list[str] = []
    frame = frame_for(model)

    parts: dict[str, _Part] = {}
    unplaced: list[str] = []
    for inst in sorted(
        model.get("instances") or [], key=lambda i: str(i.get("refdes") or "")
    ):
        refdes = str(inst.get("refdes") or "")
        if not refdes:
            continue
        if inst.get("x") is None or inst.get("y") is None:
            unplaced.append(refdes)
            continue
        parts[refdes] = _Part(
            inst=inst, refdes=refdes, bottom=padplace.is_bottom_instance(inst)
        )
    orphans = _group_pads(model, parts)
    padless = sorted(r for r, p in parts.items() if not p.pads)
    for r in padless:
        del parts[r]

    # Footprint-owned non-plated holes, authored-local mm, by refdes —
    # `model["footprint_holes"]` (the handler derives it off the cached
    # footprints). They travel inside the shared FOOTPRINT document.
    holes_by_refdes = {
        str(r): [dict(h) for h in holes]
        for r, holes in (model.get("footprint_holes") or {}).items()
        if isinstance(holes, list)
    }
    groups = _footprint_groups(parts, holes_by_refdes)
    member_of = {r: g for g in groups for r in g.members}
    for g in groups:
        # Content-keyed uuid: same pads, same doc, whichever instances land
        # it. Naming is already resolved (deterministic per content).
        g.uuid = _uuid_hex(slug, f"fp:{g.name}")

    pcb_uuid = _uuid_hex(slug, "pcb")
    comp_id = {r: _rid(slug, f"comp:{r}") for r in parts}
    pad_ids = {
        g.name: [_rid(slug, f"fpd:{g.name}:{k}") for k in range(len(g.pads))]
        for g in groups
    }

    s = _Stream()
    client = _uuid_hex(slug, "client")[:16]
    s.dochead("PCB", pcb_uuid, client)
    layers = [str(n) for n in model.get("layers") or []]
    n_inner = max(0, len(layers) - 2)
    for lay_body, lay_rid in _layer_records(n_inner):
        s.record("LAYER", lay_body, lay_rid)

    # Verbatim passthrough: the design-rule table and the board-state
    # records the reader preserved (meta.epro_rules / meta.epro_board).
    # Written before first use, in the order they came — these are the
    # records precis has no constraint model for, kept lossless so the
    # file opens with the board's own rules and physical stackup. A fresh
    # export has no passthrough; the missing board-state records are then
    # filled from Pro's own defaults so every document still carries them.
    present = {"LAYER"}
    for key in ("epro_board", "epro_rules"):
        for rec in model.get(key) or []:
            present.add(str(rec.get("type")))
            s.record(
                str(rec.get("type")),
                rec.get("body"),
                str(rec["id"]) if rec.get("id") is not None else None,
            )
    _board_state(s, slug, present=present, pcb_meta_title=slug)

    copper_items = sorted_copper(model)
    # A stackup plane whose authored zone did not survive (meta.epro_pours
    # covers the imported case) still gets a POUR over the whole outline —
    # else the plane net never reaches the file. ``model["planes"]`` comes
    # from ``_fab_model``'s stackup read.
    pour_layers = {
        str(i.get("layer")) for i in copper_items if i.get("ctype") == "pour"
    }
    for layer, net in sorted((model.get("planes") or {}).items()):
        if layer in pour_layers or not model.get("outline"):
            continue
        copper_items.append(
            {
                "ctype": "pour",
                "layer": layer,
                "net": net,
                "polygon": [list(p) for p in model["outline"]],
            }
        )
    # model["nets"] carries the board's full net list (a schematic net
    # with no board members still needs its declaration, else the net is
    # gone); the pure-model path falls back to used nets. Pro declares a
    # net with no members the way TAZ V1.15's own S$151 survives: a stale
    # PAD_NET row on a live component naming a pad that is not there.
    # There is no NET record in Pro's own files, so none is written.
    nets = sorted(
        {str(n) for n in model.get("nets") or [] if n}
        | {str(p["net"]) for part in parts.values() for p in part.pads if p.get("net")}
        | {str(i["net"]) for i in copper_items if i.get("net")}
    )
    declared_nets = {str(i["net"]) for i in copper_items if i.get("net")}
    bad_nets = [n for n in nets if "||" in n]
    if bad_nets:
        raise ValueError(
            f"net name(s) {bad_nets[:3]} contain '||', the .epru record "
            "separator — Pro cannot store that name"
        )

    outline = [(float(x), float(y)) for x, y in model.get("outline") or []]
    if len(outline) >= 3:
        ring = outline if outline[0] == outline[-1] else [*outline, outline[0]]
        path = _poly_path([to_epro_xy(x, y, frame) for x, y in ring])
        s.record(
            "POLY",
            {
                "layerId": _L_OUTLINE,
                "groupId": "",
                "locked": False,
                "zIndex": s.z(),
                "partitionId": None,
                "netName": "",
                "width": 10,
                "path": path,
                "polyType": "NORMAL",
                "specialColor": None,
            },
            _rid(slug, "outline"),
        )
    else:
        warnings.append(
            "the model has no outline polygon; the file has no board edge "
            "(and the reader refuses a file without one)"
        )

    labels = {
        str(lab.get("refdes") or ""): lab
        for lab in model.get("silk_labels") or []
        if lab.get("x") is not None and lab.get("y") is not None
    }
    for refdes, part in parts.items():
        cid = comp_id[refdes]
        x, y = to_epro_xy(float(part.inst["x"]), float(part.inst["y"]), frame)
        side = _COMP_BOTTOM if part.bottom else _COMP_TOP
        silk = _L_SILK_BOTTOM if part.bottom else _L_SILK_TOP
        comp_angle = _clean(epro_angle(part.inst))
        label = labels.get(refdes)
        if label is not None:
            # The silk pass's own anchor (bottom-left of the text box,
            # board frame): Pro's ``origin`` LEFT_BOTTOM is the same corner
            # once Y is flipped, so the editable text lands where the
            # stroked label would have printed (slice 2e).
            lx, ly = to_epro_xy(float(label["x"]), float(label["y"]), frame)
            des_x, des_y = lx, ly
            des_angle = _clean(float(label.get("angle") or 0.0))
            des_font = to_mils(float(label.get("height_mm") or 0.0)) or 45.0
            des_stroke = to_mils(float(label.get("stroke_width_mm") or 0.0)) or 6.0
        else:
            des_x, des_y = x, y
            des_angle = comp_angle
            des_font = 45.0
            des_stroke = 6.0
        s.record(
            "COMPONENT",
            {
                "layerId": side,
                "groupId": "",
                "locked": bool(part.inst.get("fixed")),
                "zIndex": s.z(),
                "partitionId": None,
                "x": x,
                "y": y,
                "angle": comp_angle,
                "attrs": {"Unique ID": "gge" + _uuid_hex(slug, f"uid:{refdes}")[:12]},
                "specialColor": None,
                "footprintPrimitives": False,
                "pinSwap": False,
            },
            cid,
        )
        s.record(
            "ATTR",
            _attr_body(
                layer=silk,
                parent=cid,
                key="Designator",
                value=refdes,
                visible=True,
                x=des_x,
                y=des_y,
                angle=des_angle,
                font_size=des_font,
                z_index=s.z(),
                stroke_width=des_stroke,
            ),
            cid + "e0",
        )
        s.record(
            "ATTR",
            _attr_body(
                layer=silk,
                parent=cid,
                key="Footprint",
                value=member_of[refdes].uuid,
                visible=False,
                x=x,
                y=y,
                angle=comp_angle,
                font_size=67.5,
                z_index=s.z(),
            ),
            cid + "e1",
        )
        # The catalog identity the import recovered — written back as the
        # same ATTR keys Pro itself emits, so a re-import (or a colleague's
        # EasyEDA open) still carries the C-number and the part it names.
        # Hidden attrs sit on the component's own position: Pro's addText
        # runs on them too, so a null x/y is a NaN crash (2026-10-14).
        part_meta = part.inst.get("part") or {}
        for k, (key, value) in enumerate(
            (
                (
                    "Supplier Part",
                    str(
                        part.inst.get("part_lcsc")
                        or part_meta.get("supplier_part")
                        or ""
                    ),
                ),
                ("Manufacturer Part", str(part_meta.get("mpn") or "")),
                ("Manufacturer", str(part_meta.get("mfr") or "")),
                ("Supplier", str(part_meta.get("supplier") or "")),
                ("Value", str(part_meta.get("value") or "")),
                ("Name", str(part_meta.get("name") or "")),
                ("link", str(part_meta.get("link") or "")),
                ("JLCPCB Part Class", str(part_meta.get("part_class") or "")),
                ("Add into BOM", "no" if part_meta.get("bom") == "no" else ""),
                # A file we write has no schematic/symbol docs, so the
                # pin's etype/nc markers ride a JSON blob the reader joins
                # back (by pin name) — a lossless private key; Pro ignores
                # it.
                (
                    "Pin Types",
                    json.dumps(pin_sem, sort_keys=True)
                    if (pin_sem := part.inst.get("pin_sem") or {})
                    else "",
                ),
            )
        ):
            if not value:
                continue
            s.record(
                "ATTR",
                _attr_body(
                    layer=silk,
                    parent=cid,
                    key=key,
                    value=value,
                    visible=False,
                    x=x,
                    y=y,
                    angle=comp_angle,
                    font_size=67.5,
                    z_index=s.z(),
                ),
                cid + f"e{k + 2}",
            )
        group = member_of[refdes]
        for local in part.local_pads:
            num = local.num or str(group.index[local.canon()] + 1)
            pad_id = pad_ids[group.name][group.index[local.canon()]]
            if local.net:
                declared_nets.add(str(local.net))
                s.record(
                    "PAD_NET",
                    {"padNet": local.net, "padLen": None, "propagationDelay": None},
                    _dumps(["PAD_NET", cid, num, pad_id]),
                )
    # A declared net with no exported pad and no copper still needs its
    # row or the net vanishes: the same stale-PAD_NET mechanism the source
    # file itself uses (a live component id + an empty pad slot) — our
    # reader and Pro both keep the net name off that row.
    if parts:
        anchor = next(iter(comp_id.values()))
        for net in nets:
            if net not in declared_nets:
                s.record(
                    "PAD_NET",
                    {"padNet": net, "padLen": None, "propagationDelay": None},
                    _dumps(["PAD_NET", anchor, "", _rid(slug, f"netdecl:{net}")]),
                )
    elif nets:
        warnings.append(
            f"{len(nets)} net(s) have no component to declare against and "
            "are not in the file: " + ", ".join(nets[:8])
        )

    cu = _write_copper(s, copper_items, slug=slug, frame=frame, layers=layers)
    silk_out = _write_silk(s, model, slug=slug, frame=frame)

    if synthesized:
        xs = [p[0] for p in outline] or [0.0]
        ys = [p[1] for p in outline] or [0.0]
        nx, ny = to_epro_xy(min(xs), max(ys) + _NOTE_RISE_MM, frame)
        s.record(
            "STRING",
            {
                "layerId": _L_DOCUMENT,
                "groupId": "",
                "locked": False,
                "zIndex": s.z(),
                "partitionId": None,
                "x": nx,
                "y": ny,
                "text": (
                    "PRECIS WARNING: SYNTHESIZED pads (land-pattern bounds, "
                    "NOT real footprints) on "
                    + ", ".join(synthesized)
                    + " - do not fabricate"
                ),
                "origin": "LEFT_BOTTOM",
                "fontFamily": "default",
                "fontSize": 60,
                "strokeWidth": 8,
                "expansion": 0,
                "angle": 0,
                "reverse": False,
                "mirror": False,
                "specialColor": None,
            },
            _rid(slug, "note:synthesized"),
        )
        warnings.append(
            f"allow_synthesized: {len(synthesized)} part(s) carry SYNTHESIZED "
            f"pad bounds, not real footprints ({', '.join(synthesized[:8])}"
            f"{'…' if len(synthesized) > 8 else ''}); named in a STRING on the "
            f"document layer — do not fabricate this board"
        )

    # Board-level drills that no exported pad, via or footprint hole
    # covers: non-plated mounting holes become free NPTH pads; a plated
    # drill that escaped its part becomes a plated free pad (netless —
    # the drill row carries no net). Both read back through the reader's
    # PAD branches. These MUST be written before the first FOOTPRINT
    # DOCHEAD — every record after that lands in another document.
    npth = 0
    plated_free = 0
    for d in loose_drills(model, parts, copper_items, groups):
        plated = bool(d.get("plated"))
        if plated:
            plated_free += 1
        else:
            npth += 1
        s.record(
            "PAD",
            _free_pad_body(d, str(npth + plated_free), frame, s.z()),
            _rid(slug, f"freepad:{npth + plated_free}"),
        )

    for group in groups:
        s.dochead("FOOTPRINT", group.uuid, client)
        _footprint_state(s, group.name, n_inner)
        # The library attrs Pro writes on its own docs: the footprint's
        # name and a visible Designator placeholder ("R?", "U?", …).
        prefix = "".join(c for c in group.members[0] if not c.isdigit()) or "U"
        des_y = min(to_mils(-p.y) for p in group.pads) - 45.0 if group.pads else 0.0
        s.record(
            "ATTR",
            _attr_body(
                layer=_L_SILK_TOP,
                parent="",
                key="Name",
                value=group.name,
                visible=False,
                x=0,
                y=0,
                angle=0,
                font_size=45,
                z_index=s.z(),
            ),
            _rid(slug, f"fpattr:{group.name}:name"),
        )
        s.record(
            "ATTR",
            _attr_body(
                layer=_L_SILK_TOP,
                parent="",
                key="Designator",
                value=f"{prefix}?",
                visible=True,
                x=0,
                y=des_y,
                angle=0,
                font_size=45,
                z_index=s.z(),
            ),
            _rid(slug, f"fpattr:{group.name}:des"),
        )
        for k, local in enumerate(group.pads):
            num = local.num or str(k + 1)
            body = _pad_body(local, s.z())
            body["num"] = num
            s.record("PAD", body, pad_ids[group.name][k])
        for k, hole in enumerate(group.holes):
            s.record(
                "FILL",
                _fp_hole_fill(hole, s.z()),
                _rid(slug, f"fph:{group.name}:{k}"),
            )

    # ── what the model held that this slice drops ────────────────────
    if unplaced:
        warnings.append(
            f"{len(unplaced)} unplaced part(s) omitted: {', '.join(unplaced[:8])}"
        )
    if padless:
        warnings.append(
            f"{len(padless)} placed part(s) have no pads in the model and are "
            f"omitted: {', '.join(padless[:8])}"
        )
    if orphans:
        warnings.append(
            f"{orphans} pad(s) belong to no placed part (fiducials, stray "
            f"rows) and are not exported"
        )
    warnings += _copper_warnings(cu)
    if plated_free:
        warnings.append(
            f"{plated_free} plated drill(s) belong to no exported pad or via "
            "(free test points) and are written as netless plated pads — "
            "re-attach their nets in Pro"
        )
    if model.get("mask_open_regions"):
        warnings.append(
            f"{len(model['mask_open_regions'])} soldermask-opening region(s) "
            "are NOT exported — Pro will mask-cover that area (no observed "
            "record carries a board-level mask region)"
        )
    warnings += _silk_warnings(silk_out, labelled=len(labels.keys() & parts.keys()))
    oblique_slots = sorted(
        r for r, part in parts.items() if any(lp.slot_rounded for lp in part.local_pads)
    )
    if oblique_slots:
        warnings.append(
            f"{len(oblique_slots)} part(s) have a slot hole off the pad "
            f"frame's axes ({', '.join(oblique_slots[:8])}"
            f"{'…' if len(oblique_slots) > 8 else ''}): written as a ROUND "
            "hole of the slot width — redraw the slot in Pro before "
            "fabricating"
        )
    dropped_nets = sorted(
        {str(p["net"]) for p in model.get("pads") or [] if p.get("net")} - set(nets)
    )
    if dropped_nets:
        warnings.append(
            f"{len(dropped_nets)} net(s) have no exported pad and are not in "
            f"the file: {', '.join(dropped_nets[:8])}"
            f"{'…' if len(dropped_nets) > 8 else ''}"
        )

    stem = _safe_stem(slug)
    manifest = {
        "title": slug,
        "cbb_project": False,
        "editorVersion": _EDITOR_VERSION,
        "introduction": "",
        "description": "",
        "tags": "[]",
    }
    files = {
        "project2.json": json.dumps(manifest, indent=2) + "\n",
        f"{stem}.epru": s.text(),
    }
    return EproExport(
        files=files,
        warnings=warnings,
        stats={
            "components": len(parts),
            "pads": sum(len(p.pads) for p in parts.values()),
            "footprint_docs": len(groups),
            "nets": len(nets),
            "tracks": cu.tracks,
            "lines": cu.lines,
            "arcs": cu.arcs,
            "vias": cu.vias,
            "pours": cu.pours,
            "pours_dropped": cu.pours_dropped,
            "npth": npth,
            "plated_free": plated_free,
            "rule_records": len(model.get("epro_rules") or []),
            "silk_polys": silk_out.polys,
            "silk_refdes_suppressed": silk_out.refdes,
            "silk_dropped": silk_out.regions + silk_out.clear,
            "labels_anchored": len(labels.keys() & parts.keys()),
        },
    )


#: Fixed member timestamp — ``ZipInfo`` otherwise stamps the wall clock and
#: two exports of one model would differ in bytes.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def zip_epro(files: dict[str, str]) -> bytes:
    """Zip ``{member: text}`` in memory — the one place this module touches
    bytes; no disk or network. Members are sorted and carry a fixed
    timestamp, so the bytes are a pure function of ``files``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content.encode("utf-8"))
    return buf.getvalue()


__all__ = [
    "EproExport",
    "EproFrame",
    "SynthesizedPadError",
    "arc_sweep_deg",
    "copper_layer_ids",
    "epro_angle",
    "epro_files",
    "frame_for",
    "loose_drills",
    "sorted_copper",
    "to_epro_xy",
    "to_footprint_xy",
    "to_mils",
    "zip_epro",
]
