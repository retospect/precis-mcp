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
* one ``FOOTPRINT`` document per placed INSTANCE (``R1_0402``,
  ``R2_0402``, … — the decided first cut, ``docs/backlog/pcb-epro-export.md``);
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
  through-hole only, and flattening one silently would short layers.

**What it deliberately does not emit** — pours (slice 2d); silk, courtyards, pin-1 ticks (2e); mask-open regions,
mounting-hole/fiducial free pads, per-pad paste/mask intent (2f); and NO
``SCH``/``SCH_PAGE``/``DEVICE``/``SYMBOL`` document, on purpose: a
schematic invites "update PCB from schematic", which rewrites the netlist
and destroys the board. Each omission that drops something present in the
model is reported in :attr:`EproExport.warnings`, never silent.

**Orientation is BAKED.** Every ``COMPONENT`` is written at angle 0 and
every footprint's pads are expressed in a frame where that is true: the
pad's board position minus the component position, inverted through the
reader's R1 convention (``component (x, y) + R(+angle) · (px, ±py)`` in the
Y-down frame, a bottom-side part mirrored in **Y**) for angle 0. So the
file does not depend on how Pro reads ``angle``; the cost is that each part
has its own footprint document. :func:`to_footprint_local` is that
inversion and is pinned against the reader by
``tests/test_pcb_epro_export.py``.

**Bottom-side parts still depend on the mirror convention.** Baking the
angle does not remove the Y-mirror: a bottom part's pads land where Pro
puts them only if Pro mirrors that footprint in Y on READ. What the
convention was verified against: Pro 3.2.149's own WRITE of
``heaterBaseTest.epro2`` (Reto's board, bottom-side parts included), by net
agreement of 573 track endpoints with zero cross-net hits, where the
X-mirror candidate disagrees (:mod:`precis.pcb.epro`). Pro reading our file
the same way is assumed, not observed; the tests here check the writer
against the same reader, so they cannot catch it. A human opening an
asymmetric bottom-side part in Pro is the check.

**Oblique pads are warned, not rotated.** A rect/obround pad at a
non-right-angle board rotation arrives with its authored ``w``/``h`` and an
``oblique_rot`` marker (:func:`precis.pcb.padplace.board_pads`); it is
written with ``padAngle`` 0 and named in a warning. Writing the real angle
would add a second unverified convention (Pro's ``padAngle`` sense, and
how it composes with the bottom mirror); that waits for a Pro-written
file that exercises it.

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

from precis.pcb import padplace
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


def to_footprint_local(
    pad_xy_mm: tuple[float, float],
    comp_xy_mm: tuple[float, float],
    *,
    bottom: bool,
) -> tuple[float, float]:
    """A pad's footprint-local ``(centerX, centerY)`` in mil for a
    ``COMPONENT`` at angle 0.

    The reader's R1 convention at angle 0 is a plain translate in the
    Y-down frame, with a bottom-side part mirrored in **Y** (the pad's
    local ``centerY`` negated). In the board's Y-up frame the pad offset is
    ``(dx, dy)``, so ``centerX = dx`` always and ``centerY`` is ``-dy`` on
    top, ``+dy`` on the bottom. Verified against
    :func:`precis.pcb.padplace.place_pad_point` through the reader, for
    every side x rotation, in ``tests/test_pcb_epro_export.py``.
    """
    dx = pad_xy_mm[0] - comp_xy_mm[0]
    dy = pad_xy_mm[1] - comp_xy_mm[1]
    return to_mils(dx), to_mils(dy if bottom else -dy)


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
    return orphans


def _poly_path(points: list[tuple[float, float]]) -> list[Any]:
    """``POLY.path`` shape: ``[x0, y0, "L", x1, y1, x2, y2, ...]``."""
    path: list[Any] = []
    for i, (x, y) in enumerate(points):
        if i == 1:
            path.append("L")
        path += [x, y]
    return path


def _pad_record(
    pad: dict[str, Any], num: str, part: _Part, comp_xy: tuple[float, float]
) -> dict[str, Any]:
    """One footprint ``PAD`` body, in the footprint-local frame."""
    shape = str(pad.get("shape") or "rect")
    w = float(pad.get("w") or 0.0)
    h = float(pad.get("h", w) or 0.0)
    cx, cy = to_footprint_local(
        (float(pad["x"]), float(pad["y"])), comp_xy, bottom=part.bottom
    )
    default: dict[str, Any] = {"padType": _PAD_TYPES.get(shape, "RECT")}
    if shape == "polygon" and pad.get("poly"):
        # The reader takes polygon vertices as footprint-local (not
        # pad-centre-relative), Y negated exactly like ``centerY``.
        default["path"] = _poly_path(
            [
                to_footprint_local((float(vx), float(vy)), comp_xy, bottom=part.bottom)
                for vx, vy in pad["poly"]
            ]
        )
    else:
        default["width"] = to_mils(w)
        default["height"] = to_mils(h if shape != "circle" else w)
    body: dict[str, Any] = {
        "netName": "",
        "layerId": _L_MULTI if pad.get("drill") else _L_TOP,
        "num": num,
        "centerX": cx,
        "centerY": cy,
        "padAngle": 0,
        "defaultPad": default,
    }
    if pad.get("drill"):
        d = to_mils(float(pad["drill"]))
        body["hole"] = {"holeType": "ROUND", "width": d, "height": d}
    body["plated"] = True
    body["padType"] = "NORMAL"
    return body


# ── copper (slice 2c) ───────────────────────────────────────────────────
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
            out.pours += 1
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
            f"{cu.pours} pour(s)/plane(s) are NOT exported yet (slice 2d) — "
            "Pro shows the board without its planes; re-pour there"
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

    pcb_uuid = _uuid_hex(slug, "pcb")
    fp_uuid = {r: _uuid_hex(slug, f"fp:{r}") for r in parts}
    comp_id = {r: _rid(slug, f"comp:{r}") for r in parts}

    s = _Stream()
    s.dochead("PCB", pcb_uuid)
    layers = [str(n) for n in model.get("layers") or []]
    n_inner = max(0, len(layers) - 2)
    for lay in _layer_records(n_inner):
        s.record("LAYER", lay, _dumps(["LAYER", lay["layerId"]]))

    copper_items = sorted_copper(model)
    nets = sorted(
        {str(p["net"]) for part in parts.values() for p in part.pads if p.get("net")}
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

    pad_ids: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for refdes, part in parts.items():
        cid = comp_id[refdes]
        x, y = to_epro_xy(float(part.inst["x"]), float(part.inst["y"]), frame)
        side = _COMP_BOTTOM if part.bottom else _COMP_TOP
        silk = _L_SILK_BOTTOM if part.bottom else _L_SILK_TOP
        s.record(
            "COMPONENT",
            {
                "layerId": side,
                "x": x,
                "y": y,
                "angle": 0,
                "attrs": {"Unique ID": "gge" + _uuid_hex(slug, f"uid:{refdes}")[:12]},
                "locked": False,
            },
            cid,
        )
        s.record(
            "ATTR",
            {
                "parentId": cid,
                "layerId": silk,
                "x": x,
                "y": y,
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
                "value": fp_uuid[refdes],
                "valueVisible": False,
            },
            cid + "e1",
        )
        entries: list[tuple[str, str, dict[str, Any]]] = []
        for k, pad in enumerate(part.pads):
            num = str(pad.get("pin") or "") or str(k + 1)
            pad_id = _rid(slug, f"pad:{refdes}:{k}")
            entries.append((pad_id, num, pad))
            if pad.get("net"):
                s.record(
                    "PAD_NET",
                    {"padNet": str(pad["net"]), "padLen": None, "attrsMap": {}},
                    _dumps(["PAD_NET", cid, num, pad_id]),
                )
        pad_ids[refdes] = entries

    cu = _write_copper(s, copper_items, slug=slug, frame=frame, layers=layers)

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

    for refdes, part in parts.items():
        comp_xy = (float(part.inst["x"]), float(part.inst["y"]))
        s.dochead("FOOTPRINT", fp_uuid[refdes])
        fp_name = str(part.inst.get("footprint") or "FP")
        s.record(
            "META", {"title": f"{refdes}_{fp_name}", "tags": ["Footprints"]}, "META"
        )
        s.record("CANVAS", {"originX": 0, "originY": 0, "unit": "mm"}, "CANVAS")
        for pad_id, num, pad in pad_ids[refdes]:
            s.record("PAD", _pad_record(pad, num, part, comp_xy), pad_id)

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
    exported_holes = {
        (round(float(p["x"]), 3), round(float(p["y"]), 3))
        for part in parts.values()
        for p in part.pads
        if p.get("drill")
    } | {
        (round(float(i["x"]), 3), round(float(i["y"]), 3))
        for i in copper_items
        if i.get("ctype") == "via" and i.get("x") is not None
    }
    loose = [
        d
        for d in model.get("drills") or []
        if (round(float(d["x"]), 3), round(float(d["y"]), 3)) not in exported_holes
    ]
    if loose:
        warnings.append(
            f"{len(loose)} drill(s) belong to no exported pad or via (mounting "
            f"holes) and are not exported"
        )
    if model.get("mask_open_regions"):
        warnings.append(
            f"{len(model['mask_open_regions'])} soldermask-opening region(s) "
            "are NOT exported — Pro will mask-cover that area"
        )
    if (model.get("silkscreen") or {}).get("top") or (
        model.get("silkscreen") or {}
    ).get("bottom"):
        warnings.append(
            "silkscreen is NOT exported (slice 2e); designators are written "
            "as editable attributes"
        )
    oblique = sorted(
        r for r, part in parts.items() if any("oblique_rot" in p for p in part.pads)
    )
    if oblique:
        warnings.append(
            f"{len(oblique)} part(s) sit at a non-right-angle rotation with "
            f"rect/obround pads ({', '.join(oblique[:8])}"
            f"{'…' if len(oblique) > 8 else ''}): pad CENTRES are right, pad "
            "SHAPES are written unrotated (padAngle 0) — fix their pad angle "
            "in Pro before fabricating"
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
            "nets": len(nets),
            "tracks": cu.tracks,
            "lines": cu.lines,
            "arcs": cu.arcs,
            "vias": cu.vias,
            "pours": cu.pours,
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
    "epro_files",
    "frame_for",
    "sorted_copper",
    "to_epro_xy",
    "to_footprint_local",
    "to_mils",
    "zip_epro",
]
