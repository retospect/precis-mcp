"""EasyEDA Pro ``.epro2`` writer — pure half (pcb-epro-export slice 2b).

Model dict in (the one :meth:`precis.handlers.pcb.PcbHandler._fab_model`
assembles), ``{filename: text}`` / ``bytes`` out: no DB, no clock, no
network, no path handling. The format record is the reader,
:mod:`precis.pcb.epro`; this module emits exactly the shapes that module's
docstring and the hand-written ``tests/fixtures/pcb_epro_tiny`` fixture
record, and the test suite reads every file back through that reader.

**What it emits** — ``project2.json`` plus ONE ``.epru`` stream of
documents:

* a ``PCB`` document: ``DOCHEAD``, ``LAYER`` records (copper, outline,
  silk/mask/paste, multi, document), ``NET`` (declared before use), the
  outline ``POLY`` on the ``OUTLINE`` layer, then per placed part
  ``COMPONENT`` + ``ATTR`` ``Designator`` + ``ATTR`` ``Footprint`` +
  ``PAD_NET`` per netted pad;
* one ``FOOTPRINT`` document per DISTINCT footprint (slice 2f): the
  placed instances that un-place to the same authored pad set share one
  document, as Pro's own libraries do — ``COMPONENT.angle`` carries the
  real rotation so the footprint stores authored (unrotated) pads;
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
_USER = "precis"

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
    return json.dumps(obj, separators=(",", ":"))


class _Stream:
    """The ``.epru`` text under construction: ``<header>||<body>|`` lines
    and one monotonic ticket counter shared by every document."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._ticket = 0

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

    def dochead(self, doc_type: str, doc_uuid: str) -> None:
        self.record(
            "DOCHEAD",
            {
                "docType": doc_type,
                "uuid": doc_uuid,
                "editVersion": _EDITOR_VERSION,
                "updateTime": _UPDATE_TIME_MS,
                "user": _USER,
                "version": "1",
            },
        )

    def text(self) -> str:
        return "\n".join(self._lines) + "\n"


# ── layers ──────────────────────────────────────────────────────────────
def _layer_records(n_inner: int) -> list[dict[str, Any]]:
    """The LAYER set. Copper, outline and the silk ids are spike-observed;
    the ``layerType`` words for silk/mask/paste/multi/document are best
    guesses (the reader reads only TOP/BOTTOM/SIGNAL/OUTLINE)."""
    rows: list[tuple[int, str, str]] = [
        (_L_TOP, "TOP", "Top Layer"),
        (_L_BOTTOM, "BOTTOM", "Bottom Layer"),
        (_L_SILK_TOP, "TOP_SILK", "Top Silkscreen Layer"),
        (_L_SILK_BOTTOM, "BOT_SILK", "Bottom Silkscreen Layer"),
        (5, "TOP_SOLDER_MASK", "Top Solder Mask Layer"),
        (6, "BOT_SOLDER_MASK", "Bottom Solder Mask Layer"),
        (7, "TOP_PASTE_MASK", "Top Paste Mask Layer"),
        (8, "BOT_PASTE_MASK", "Bottom Paste Mask Layer"),
        (_L_OUTLINE, "OUTLINE", "Board Outline Layer"),
        (_L_MULTI, "MULTI", "Multi-Layer"),
        (_L_DOCUMENT, "DOCUMENT", "Document Layer"),
    ]
    rows += [(_L_INNER0 + i, "SIGNAL", f"Inner{i + 1}") for i in range(max(0, n_inner))]
    return [
        {"layerId": lid, "layerType": lt, "layerName": ln, "use": True, "show": True}
        for lid, lt, ln in sorted(rows)
    ]


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


def _pad_body(local: _LocalPad) -> dict[str, Any]:
    """One footprint ``PAD`` body in the authored frame — ``(x, -y)`` in
    mils, the exact inverse of the reader's ``_pad_to_precis``."""
    default: dict[str, Any] = {"padType": _PAD_TYPES.get(local.shape, "RECT")}
    if local.shape == "polygon" and local.poly:
        default["path"] = _poly_path(
            [(to_mils(vx), to_mils(-vy)) for vx, vy in local.poly]
        )
    else:
        default["width"] = to_mils(local.w)
        default["height"] = to_mils(local.w if local.shape == "circle" else local.h)
    body: dict[str, Any] = {
        "netName": "",
        "layerId": _L_MULTI if local.drill else _L_TOP,
        "num": local.num,
        "centerX": to_mils(local.x),
        "centerY": to_mils(-local.y),
        "padAngle": _clean(round(local.rot, 4)),
        "defaultPad": default,
        "plated": True,
        "padType": "NORMAL",
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


def _fp_hole_fill(hole: dict[str, Any]) -> dict[str, Any]:
    """A footprint-owned non-plated hole as the ``FILL`` ``CIRCLE`` on
    ``MULTI`` the reader's ``_footprint_holes`` parses back
    (:func:`precis.pcb.epro._footprint_holes` — the shape Pro itself
    writes for a footprint's NPTH). ``hole`` is authored-local
    ``{"x","y","dia_mm"}`` in mm."""
    return {
        "layerId": _L_MULTI,
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


def _free_pad_body(drill: dict[str, Any], num: str, frame: EproFrame) -> dict[str, Any]:
    """A board-level drill as a free ``PAD`` — the shape the reader's
    ``extract_mounting_holes`` parses (``plated`` + ``hole.width``). A
    non-plated hole gets a hole-sized pad: there is no copper ring to
    keep. The plated case (a drilled test point that escaped its part)
    gets the same; no net is carried because the drill row has none."""
    dia = to_mils(float(drill["dia_mm"]))
    x, y = to_epro_xy(float(drill["x"]), float(drill["y"]), frame)
    plated = bool(drill.get("plated"))
    return {
        "netName": "",
        "layerId": _L_TOP if plated else _L_MULTI,
        "num": num,
        "centerX": x,
        "centerY": y,
        "padAngle": 0,
        "defaultPad": {"padType": "ELLIPSE", "width": dia, "height": dia},
        "hole": {"holeType": "ROUND", "width": dia, "height": dia},
        "plated": plated,
        "padType": "NORMAL",
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
            s.record(
                "POUR",
                {
                    "netName": net,
                    "layerId": lid,
                    "width": _POUR_WIDTH,
                    "name": f"POUR{out.pours}",
                    "order": 0,
                    "path": path,
                    "pourType": {"pourType": "SOLID", "fineness": _POUR_FINENESS},
                    "keepIsland": False,
                },
                _rid(slug, f"pour:{n}"),
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
                    "netName": net,
                    "centerX": cx,
                    "centerY": cy,
                    "holeDiameter": to_mils(float(item.get("drill_mm") or 0.0)),
                    "viaDiameter": to_mils(float(item.get("dia_mm") or 0.0)),
                    "viaType": "NORMAL",
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
                "netName": net,
                "layerId": lid,
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
            f"{cu.pours} pour(s)/plane(s) written as POUR zones — Pro "
            "re-pours them on open with ITS clearance rules, so the fill "
            "differs from precis' (antipads, islands, interior holes, "
            "thermals)"
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
                    "netName": "",
                    "layerId": lid,
                    "width": to_mils(float(draw.get("width_mm") or 0.0)),
                    "path": _poly_path(pts),
                    "polyType": "NORMAL",
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
    s.dochead("PCB", pcb_uuid)
    layers = [str(n) for n in model.get("layers") or []]
    n_inner = max(0, len(layers) - 2)
    for lay in _layer_records(n_inner):
        s.record("LAYER", lay, _dumps(["LAYER", lay["layerId"]]))

    # Verbatim passthrough: the design-rule table and the board-state
    # records the reader preserved (meta.epro_rules / meta.epro_board).
    # Written before first use, in the order they came — these are the
    # records precis has no constraint model for, kept lossless so the
    # file opens with the board's own rules and physical stackup.
    for key in ("epro_board", "epro_rules"):
        for rec in model.get(key) or []:
            s.record(
                str(rec.get("type")),
                rec.get("body"),
                str(rec["id"]) if rec.get("id") is not None else None,
            )

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
    # with no board members still needs its NET record, else the net is
    # gone); the pure-model path falls back to used nets.
    nets = sorted(
        {str(n) for n in model.get("nets") or [] if n}
        | {str(p["net"]) for part in parts.values() for p in part.pads if p.get("net")}
        | {str(i["net"]) for i in copper_items if i.get("net")}
    )
    for net in nets:
        s.record("NET", {"netType": None}, _dumps(["NET", net]))

    outline = [(float(x), float(y)) for x, y in model.get("outline") or []]
    if len(outline) >= 3:
        ring = outline if outline[0] == outline[-1] else [*outline, outline[0]]
        path = _poly_path([to_epro_xy(x, y, frame) for x, y in ring])
        s.record(
            "POLY",
            {
                "netName": "",
                "layerId": _L_OUTLINE,
                "width": 10,
                "path": path,
                "polyType": "NORMAL",
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
        label = labels.get(refdes)
        if label is not None:
            # The silk pass's own anchor (bottom-left of the text box,
            # board frame): Pro's ``origin`` LEFT_BOTTOM is the same corner
            # once Y is flipped, so the editable text lands where the
            # stroked label would have printed (slice 2e).
            lx, ly = to_epro_xy(float(label["x"]), float(label["y"]), frame)
            text_attrs: dict[str, Any] = {
                "x": lx,
                "y": ly,
                "fontSize": to_mils(float(label.get("height_mm") or 0.0)),
                "strokeWidth": to_mils(float(label.get("stroke_width_mm") or 0.0)),
                "origin": "LEFT_BOTTOM",
                "angle": _clean(float(label.get("angle") or 0.0)),
                "mirror": False,
            }
        else:
            text_attrs = {"x": x, "y": y, "angle": 0, "origin": "LEFT_BOTTOM"}
        s.record(
            "COMPONENT",
            {
                "layerId": side,
                "x": x,
                "y": y,
                "angle": _clean(epro_angle(part.inst)),
                "attrs": {"Unique ID": "gge" + _uuid_hex(slug, f"uid:{refdes}")[:12]},
                "locked": bool(part.inst.get("fixed")),
            },
            cid,
        )
        s.record(
            "ATTR",
            {
                "parentId": cid,
                "layerId": silk,
                **text_attrs,
                "key": "Designator",
                "value": refdes,
                "valueVisible": True,
            },
            cid + "e0",
        )
        s.record(
            "ATTR",
            {
                "parentId": cid,
                "layerId": silk,
                "x": None,
                "y": None,
                "key": "Footprint",
                "value": member_of[refdes].uuid,
                "valueVisible": False,
            },
            cid + "e1",
        )
        group = member_of[refdes]
        for local in part.local_pads:
            num = local.num or str(group.index[local.canon()] + 1)
            pad_id = pad_ids[group.name][group.index[local.canon()]]
            if local.net:
                s.record(
                    "PAD_NET",
                    {"padNet": local.net, "padLen": None, "attrsMap": {}},
                    _dumps(["PAD_NET", cid, num, pad_id]),
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
                "x": nx,
                "y": ny,
                "text": (
                    "PRECIS WARNING: SYNTHESIZED pads (land-pattern bounds, "
                    "NOT real footprints) on "
                    + ", ".join(synthesized)
                    + " - do not fabricate"
                ),
                "angle": 0,
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
            _free_pad_body(d, str(npth + plated_free), frame),
            _rid(slug, f"freepad:{npth + plated_free}"),
        )

    for group in groups:
        s.dochead("FOOTPRINT", group.uuid)
        s.record("META", {"title": group.name, "tags": ["Footprints"]}, "META")
        s.record("CANVAS", {"originX": 0, "originY": 0, "unit": "mm"}, "CANVAS")
        for k, local in enumerate(group.pads):
            num = local.num or str(k + 1)
            body = _pad_body(local)
            body["num"] = num
            s.record("PAD", body, pad_ids[group.name][k])
        for k, hole in enumerate(group.holes):
            s.record("FILL", _fp_hole_fill(hole), _rid(slug, f"fph:{group.name}:{k}"))

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
