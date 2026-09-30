"""EasyEDA Pro ``.epro2`` reader — pure half (pcb-epro-import Slice 1a).

``bytes`` in, dataclasses out: no DB, no network, no path handling, so
every branch is testable from an in-memory fixture. The Store-facing half
lives in :mod:`precis.ingest.pcb_epro`; the writer half is
`docs/backlog/pcb-epro-export.md`.

**This is not the format KiCad's importer documents.** KiCad's dev-docs
describe ``.epro``: a ZIP of several JSON-Lines documents
(``project.json`` + ``*.esch``/``*.epcb``/``*.efoo``), each line a typed
JSON *array* with a dozen positional fields, several of them labelled
``unk``. EasyEDA Pro 3.2 exports ``.epro2``, which shares the record-type
vocabulary and almost nothing else. Everything below is spike-verified
(2026-09-29, EasyEDA Pro editorVersion 3.2.149, a real 140-component
4-layer board). Do not "fix" these without re-spiking:

* The container is a ZIP holding ``project2.json`` (a 6-key manifest:
  title, editorVersion, …), ``IMAGE/*.webp`` (schematic-sheet previews,
  not board data), and exactly ONE ``*.epru`` — every document in the
  project concatenated into a single stream.
* A record is one line, ``<header>||<body>|``: two JSON objects separated
  by a literal ``||``, with ONE trailing ``|``. The header carries
  ``type``, ``ticket``, and usually ``id``; the body carries the payload.
  A body of ``""`` is legal and means "no payload" — it is NOT an error
  and NOT a deletion.
* ``type: "DOCHEAD"`` opens a new document; its body's ``docType`` is one
  of PCB, BOARD, SCH, SCH_PAGE, SYMBOL, DEVICE, FOOTPRINT, CONFIG, PANEL,
  BLOB. Every record until the next DOCHEAD belongs to that document.
  There are no per-document files and no directory — the DOCHEAD
  boundary is the only structure.
* **The stream is a SNAPSHOT, not an event log.** ``ticket`` and
  ``firstTicket`` look like revision stamps and invite a replay reading;
  they are not needed. Verified: across the spike board's 5149 PCB
  records, no ``(type, id)`` pair appears twice. :func:`split_documents`
  refuses a stream that violates this rather than silently keeping the
  last write, because a replay reader and a snapshot reader disagree
  exactly where it would matter most.
* **Bodies are named-key JSON objects**, not positional arrays. The
  ``unk`` fields that are the entire risk surface of the ``.epro`` format
  do not exist here.
* Coordinates are **mils** (0.0254 mm), Y grows DOWN, and the origin sits
  at a corner of the board outline on this board — but that is the user's
  ``CANVAS.originX/originY``, not a guarantee, so :class:`Frame` derives
  the transform from the outline rather than assuming it.
  ONE exception, and it is a trap: ``POURED.pourFill`` paths are in
  **10-mil units** (the EasyEDA *Standard* unit, cf.
  :data:`precis.pcb.easyeda._MM_PER_UNIT`) — a silent factor of ten
  against every other coordinate in the same document. We discard
  ``POURED`` (it is derived fill, recomputed on the far side), so this
  costs nothing today; it would be a plausible-looking disaster in a
  reader that kept it.
* ``LINE`` and ``VIA`` carry ``netName`` **directly**. No net-index
  indirection, so copper extraction needs neither ``NET`` nor
  ``PAD_NET``.
* ``VIA`` has no ``layerId``; the span is ``viaType`` plus
  ``unusedInnerLayers``. Through-hole with an empty list is the only
  combination this reader has seen; :func:`extract_vias` warns on any
  other rather than guessing a span.
* A component's pad frame is ``component (x, y) + R(+angle) · (px, ±py)``
  in the Y-down frame — plain translate at angle 0, POSITIVE rotation
  sense, and a bottom-side part mirrors in **Y** (negate the pad's local
  ``centerY``). Verified by net agreement against routed copper: 573
  track endpoints land on a same-net pad and **zero** land on a pad of a
  different net; all five other rotation/mirror candidates produce
  disagreements. This is the convention that fails silently — a wrong
  handedness renders a plausible board that cannot be built — so the
  check is reproduced in the test suite, not just recorded here.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass, field
from typing import Any

#: 1 mil = 0.0254 mm. The ``.epru`` document body's unit, everywhere
#: except ``POURED.pourFill`` (see the module docstring).
_MM_PER_MIL = 0.0254

#: Separator between a record's header and body objects.
_SEP = "||"

#: ``layerType`` values that are copper, in physical stack order: the top
#: layer, then every inner signal layer by ascending ``layerId``, then the
#: bottom. Derived from the LAYER records rather than hardcoding ids,
#: because a 6-layer board numbers its inner layers differently and the
#: LAYER records are self-describing.
_TOP, _BOTTOM, _INNER = "TOP", "BOTTOM", "SIGNAL"

#: ``layerType`` of the board-outline layer.
_OUTLINE = "OUTLINE"


def _nm(v_mil: float) -> float:
    """mil → mm, rounded to the nanometre.

    The multiply leaves binary float residue in the twelfth decimal, which
    is far below any fab tolerance but makes two runs over the same file
    compare unequal — so determinism tests, and the round-trip comparison
    the export half will need, would be testing float noise. Nanometres
    are already three orders finer than the format's own 0.0001-mil
    storage precision, so nothing real is lost.
    """
    return round(v_mil * _MM_PER_MIL, 6)


class EproError(ValueError):
    """The archive or stream is not readable as EasyEDA Pro ``.epro2``."""


def _describe(doc: EproDocument) -> str:
    meta = doc.bodies("META")
    title = meta[0].get("title") if meta else None
    return f"{doc.uuid} ({title})" if title else str(doc.uuid)


@dataclass(frozen=True)
class EproRecord:
    """One ``<header>||<body>|`` line. ``body`` is the decoded payload, or
    ``None`` where the record carried the empty-string body the format
    uses for "no payload"."""

    type: str
    id: str | None
    body: dict[str, Any] | None
    ticket: int | None = None


@dataclass(frozen=True)
class EproDocument:
    """One DOCHEAD-delimited document and its records."""

    doc_type: str
    uuid: str | None
    records: list[EproRecord]
    edit_version: str | None = None

    def of_type(self, type_: str) -> list[EproRecord]:
        return [r for r in self.records if r.type == type_]

    def bodies(self, type_: str) -> list[dict[str, Any]]:
        """Just the non-empty bodies of ``type_`` — the common case, since
        a record whose payload is absent says nothing about geometry."""
        return [r.body for r in self.records if r.type == type_ and r.body is not None]


@dataclass(frozen=True)
class EproProject:
    """A parsed ``.epro2`` archive."""

    title: str
    editor_version: str
    documents: list[EproDocument]

    def by_type(self, doc_type: str) -> list[EproDocument]:
        return [d for d in self.documents if d.doc_type == doc_type]

    def pcb(self, uuid: str | None = None) -> EproDocument:
        """The one board to import.

        Refuses to guess when a project carries several. Spike-verified
        2026-09-30: the arc fixture is one project holding SEVEN ``PCB``
        documents (PCB1..PCB7), six of them near-empty variants, and the
        board with the arcs is the LAST. ``by_type("PCB")[0]`` silently
        picked the wrong one and reported zero arcs — a wrong answer with
        no error, which is exactly the shape of failure this reader is
        supposed to refuse.
        """
        boards = self.by_type("PCB")
        if not boards:
            raise EproError("no PCB document in this project")
        if uuid is not None:
            for d in boards:
                if d.uuid == uuid:
                    return d
            raise EproError(
                f"no PCB document with uuid {uuid!r}; this project has: "
                f"{', '.join(_describe(d) for d in boards)}"
            )
        if len(boards) > 1:
            raise EproError(
                f"{len(boards)} PCB documents in this project — name one "
                f"explicitly rather than importing whichever came first: "
                f"{', '.join(_describe(d) for d in boards)}"
            )
        return boards[0]


def _split_line(line: str, lineno: int) -> EproRecord:
    head, sep, body = line.partition(_SEP)
    if not sep:
        raise EproError(
            f"line {lineno}: no '{_SEP}' header/body separator — this does not "
            f"look like an .epro2 record stream"
        )
    if body.endswith("|"):
        body = body[:-1]
    try:
        header = json.loads(head)
    except json.JSONDecodeError as exc:
        raise EproError(f"line {lineno}: header is not JSON ({exc})") from exc
    if not isinstance(header, dict) or "type" not in header:
        raise EproError(f"line {lineno}: header is not an object with a 'type'")
    payload: dict[str, Any] | None = None
    if body.startswith(("{", "[")):
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EproError(
                f"line {lineno}: {header['type']} body is not JSON ({exc})"
            ) from exc
        payload = decoded if isinstance(decoded, dict) else {"_list": decoded}
    elif body not in ("", '""'):
        raise EproError(
            f"line {lineno}: {header['type']} body is neither JSON nor the "
            f"empty-payload marker, got {body[:40]!r}"
        )
    return EproRecord(
        type=str(header["type"]),
        id=header.get("id"),
        body=payload,
        ticket=header.get("ticket"),
    )


def split_documents(stream: str) -> list[EproDocument]:
    """Split a ``.epru`` stream into its DOCHEAD-delimited documents.

    Refuses a stream where any ``(type, id)`` repeats inside one document:
    that would mean the stream is an event log rather than the snapshot
    the spike observed, and the two readings disagree about which copy of
    a moved track is current. Better to refuse than to render the wrong
    board convincingly.
    """
    docs: list[EproDocument] = []
    current: list[EproRecord] | None = None
    seen: set[tuple[str, str]] = set()
    for lineno, raw in enumerate(stream.split("\n"), start=1):
        if not raw.strip():
            continue
        rec = _split_line(raw, lineno)
        if rec.type == "DOCHEAD":
            head = rec.body or {}
            current = []
            seen = set()
            docs.append(
                EproDocument(
                    doc_type=str(head.get("docType", "")),
                    uuid=head.get("uuid"),
                    records=current,
                    edit_version=head.get("editVersion"),
                )
            )
            continue
        if current is None:
            raise EproError(
                f"line {lineno}: {rec.type} record before any DOCHEAD — the "
                f"stream does not open a document"
            )
        if rec.id is not None:
            key = (rec.type, rec.id)
            if key in seen:
                raise EproError(
                    f"line {lineno}: {rec.type} id {rec.id!r} appears twice in "
                    f"one document. This reader assumes a snapshot; a stream "
                    f"with repeats is an event log and needs replay semantics "
                    f"(see the module docstring)."
                )
            seen.add(key)
        current.append(rec)
    return docs


def read_archive(data: bytes) -> EproProject:
    """Parse ``.epro2`` bytes. Raises :class:`EproError` with the reason."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise EproError(f"not a ZIP archive: {exc}") from exc
    names = zf.namelist()
    manifest_name = next((n for n in names if n.endswith("project2.json")), None)
    if manifest_name is None:
        raise EproError(
            "no project2.json in the archive. A ZIP with project.json and "
            "separate .epcb/.esch members is the older .epro format, which "
            "this reader does not read."
        )
    manifest = json.loads(zf.read(manifest_name).decode("utf-8"))
    streams = [n for n in names if n.endswith(".epru")]
    if len(streams) != 1:
        raise EproError(f"expected exactly one .epru member, found {len(streams)}")
    stream = zf.read(streams[0]).decode("utf-8")
    return EproProject(
        title=str(manifest.get("title", "")),
        editor_version=str(manifest.get("editorVersion", "")),
        documents=split_documents(stream),
    )


@dataclass(frozen=True)
class Frame:
    """The mil/Y-down → mm/Y-up transform, derived from the board outline.

    precis places the origin at a board-outline corner with +X right and
    +Y up (ADR 0042); EasyEDA Pro's Y grows down and its origin is
    wherever the user's ``CANVAS`` put it. Deriving both offsets from the
    outline bounding box means an imported board lands in precis' own
    frame no matter where the author's origin was.
    """

    min_x_mil: float
    max_y_mil: float

    def xy(self, x_mil: float, y_mil: float) -> tuple[float, float]:
        return (
            _nm(x_mil - self.min_x_mil),
            _nm(self.max_y_mil - y_mil),
        )

    @staticmethod
    def length(v_mil: float) -> float:
        return _nm(v_mil)


def _poly_points(path: list[Any]) -> list[tuple[float, float]]:
    """Decode a ``POLY.path``: a flat list of numbers with ``"L"`` / arc
    opcodes interleaved. We keep the vertices and drop the opcodes, which
    is exact for the ``"L"`` (line-to) runs an outline is made of; an arc
    opcode is reported by the caller rather than silently chorded."""
    pts: list[tuple[float, float]] = []
    nums: list[float] = []
    for item in path:
        if isinstance(item, str):
            continue
        nums.append(float(item))
    for i in range(0, len(nums) - 1, 2):
        pts.append((nums[i], nums[i + 1]))
    return pts


def _outline_opcodes(path: list[Any]) -> set[str]:
    return {item for item in path if isinstance(item, str)}


def board_outline(pcb: EproDocument) -> tuple[list[tuple[float, float]], Frame]:
    """The board outline in mm, plus the :class:`Frame` it defines.

    Raises when there is no outline: every downstream consumer silently
    falls back to a synthetic rectangle around the placed parts
    (:func:`precis.pcb.export.board_bbox`), which is a board shape that is
    a lie. Refusing at import is the only honest option.
    """
    layers = layer_map(pcb)
    outline_ids = {lid for lid, lt in layers.items() if lt[0] == _OUTLINE}
    polys = [
        b
        for b in pcb.bodies("POLY")
        if b.get("layerId") in outline_ids and b.get("path")
    ]
    if not polys:
        raise EproError(
            "no POLY on the board-outline layer. precis needs a real outline — "
            "without one every geometry consumer falls back to a bounding box "
            "around the placed parts, which is not the board."
        )
    if len(polys) > 1:
        raise EproError(
            f"{len(polys)} outline polygons on the board-outline layer; this "
            f"reader takes exactly one (a multi-board panel needs --board)"
        )
    path = polys[0]["path"]
    mils = _poly_points(path)
    if not mils:
        raise EproError("the outline POLY has no vertices")
    frame = Frame(min_x_mil=min(x for x, _ in mils), max_y_mil=max(y for _, y in mils))
    return [frame.xy(x, y) for x, y in mils], frame


def layer_map(pcb: EproDocument) -> dict[int, tuple[str, str]]:
    """``layerId -> (layerType, layerName)`` for every layer the document
    declares in use. Self-describing, so no id table to drift."""
    out: dict[int, tuple[str, str]] = {}
    for b in pcb.bodies("LAYER"):
        if b.get("use") and "layerId" in b:
            out[int(b["layerId"])] = (str(b["layerType"]), str(b["layerName"]))
    return out


def copper_layers(pcb: EproDocument) -> dict[int, str]:
    """``layerId -> precis layer name``, in physical stack order.

    Inner signal layers are ordered by ascending ``layerId``. The
    ``LAYER_PHYS`` records carry a real ``zIndex`` stack order but no link
    back to a ``layerId``, so they cannot confirm it; for the 4-layer case
    the spike board's ids (1, 15, 16, 2) match the editor's own top-to-
    bottom order. A board with more inner layers should be re-spiked
    before this ordering is trusted.
    """
    layers = layer_map(pcb)
    top = sorted(lid for lid, (lt, _) in layers.items() if lt == _TOP)
    inner = sorted(lid for lid, (lt, _) in layers.items() if lt == _INNER)
    bottom = sorted(lid for lid, (lt, _) in layers.items() if lt == _BOTTOM)
    if len(top) != 1 or len(bottom) != 1:
        raise EproError(
            f"expected exactly one TOP and one BOTTOM copper layer, found "
            f"{len(top)} and {len(bottom)} — precis has no single-sided board "
            f"model, and a second outer layer is not something this reader has "
            f"seen"
        )
    ordered = top + inner + bottom
    names = ["F.Cu"] + [f"In{i + 1}.Cu" for i in range(len(inner))] + ["B.Cu"]
    return dict(zip(ordered, names, strict=True))


@dataclass
class Extraction:
    """Copper rows in ``pcb_fixed_copper``'s own shape, plus every
    judgement call the reader made on the way."""

    tracks: list[dict[str, Any]] = field(default_factory=list)
    vias: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [*self.tracks, *self.vias]


@dataclass(frozen=True)
class _Seg:
    """One source-frame copper segment, straight or curved."""

    a: tuple[float, float]
    b: tuple[float, float]
    kind: str  # "line" | "arc"
    sweep_deg: float = 0.0  # signed, source frame; arcs only


def _arc_centre(
    a: tuple[float, float], b: tuple[float, float], sweep_deg: float
) -> tuple[float, float]:
    """Centre of the arc that sweeps ``sweep_deg`` from ``a`` to ``b``.

    ``ARC`` stores start, end and a signed swept angle; precis' segment
    shape wants a centre and a direction. The centre sits on the
    perpendicular bisector of the chord, offset by
    ``(chord/2) / tan(sweep/2)`` along the chord's left normal — the sign
    of ``tan`` carries the major/minor and direction cases, so there is no
    branch here to get wrong. Verified exact (|r_a − r_b| < 3e-13 mil, and
    the swept angle recovered from the centre reproduces the stored one)
    on all five arcs of the 2026-09-29 arc fixture, including both signs
    of a 180° semicircle.
    """
    th = math.radians(sweep_deg)
    dx, dy = b[0] - a[0], b[1] - a[1]
    chord = math.hypot(dx, dy)
    h = (chord / 2) / math.tan(th / 2)
    return (
        (a[0] + b[0]) / 2 - h * dy / chord,
        (a[1] + b[1]) / 2 + h * dx / chord,
    )


def _chain(segments: list[_Seg]) -> list[list[tuple[int, bool]]]:
    """Chain coincident segments into polylines.

    Returns each polyline as ``[(index, flipped), …]`` rather than a point
    list, because an arc carries a direction that a bare point sequence
    would drop: a segment traversed backwards needs its endpoints swapped
    AND its sweep negated.

    A node with more than two incident segments is a tee and ENDS the
    polyline on both sides — merging through it would invent a corner the
    author did not draw and would make one of the branches disappear.
    """
    adj: dict[tuple[float, float], list[int]] = {}
    for i, s in enumerate(segments):
        adj.setdefault(s.a, []).append(i)
        adj.setdefault(s.b, []).append(i)
    used: set[int] = set()
    out: list[list[tuple[int, bool]]] = []
    for i, s in enumerate(segments):
        if i in used:
            continue
        used.add(i)
        run = [(i, False)]
        head, tail = s.a, s.b
        for append in (True, False):
            while True:
                node = tail if append else head
                if len(adj[node]) != 2:
                    break
                nxt = [j for j in adj[node] if j not in used]
                if len(nxt) != 1:
                    break
                j = nxt[0]
                used.add(j)
                t = segments[j]
                # `flipped` is True when the run enters this segment at
                # its stored END -- appending forward, or prepending and
                # arriving at its stored START.
                if append:
                    flipped = t.b == node
                    tail = t.a if flipped else t.b
                    run.append((j, flipped))
                else:
                    flipped = t.a == node
                    head = t.b if flipped else t.a
                    run.insert(0, (j, flipped))
        out.append(run)
    return out


def extract_tracks(pcb: EproDocument, frame: Frame, *, quantum: int = 4) -> Extraction:
    """``LINE`` records on copper layers → ``ctype='track'`` rows.

    Segments are grouped by ``(net, layer, width)`` and chained into
    polylines, because that is what the author drew and what
    ``pcb_fixed_copper`` stores; a per-segment row would multiply the row
    count by ~3.5 and lose the fact that a corner is a corner.
    ``quantum`` is the mil-space rounding used to decide two endpoints are
    the same point.
    """
    out = Extraction()
    layers = copper_layers(pcb)
    groups: dict[tuple[str, int, float], list[_Seg]] = {}
    for kind, records in (("line", pcb.bodies("LINE")), ("arc", pcb.bodies("ARC"))):
        for b in records:
            lid = b.get("layerId")
            if lid not in layers:
                continue
            net = str(b.get("netName") or "")
            if not net:
                out.warnings.append(
                    f"a {layers[lid]} {kind} at "
                    f"({b['startX']:.1f}, {b['startY']:.1f}) mil carries no net "
                    f"name; skipped — fixed copper may not invent a net"
                )
                continue
            a = (round(b["startX"], quantum), round(b["startY"], quantum))
            z = (round(b["endX"], quantum), round(b["endY"], quantum))
            if a == z:
                out.warnings.append(
                    f"zero-length {layers[lid]} {kind} on {net} at "
                    f"({a[0]:.1f}, {a[1]:.1f}) mil; dropped"
                )
                continue
            sweep = float(b.get("angle") or 0.0) if kind == "arc" else 0.0
            if kind == "arc" and (sweep == 0.0 or abs(sweep) >= 360.0):
                out.warnings.append(
                    f"{layers[lid]} arc on {net} at ({a[0]:.1f}, {a[1]:.1f}) mil "
                    f"sweeps {sweep}°, which has no finite centre; dropped "
                    f"rather than chorded"
                )
                continue
            groups.setdefault((net, int(lid), float(b["width"])), []).append(
                _Seg(a=a, b=z, kind=kind, sweep_deg=sweep)
            )

    for (net, lid, width_mil), segs in sorted(groups.items()):
        for run in _chain(segs):
            out.tracks.append(
                {
                    "ctype": "track",
                    "layer": layers[lid],
                    "net": net,
                    "geom": {
                        "segments": [
                            _segment(segs[i], flipped, frame) for i, flipped in run
                        ],
                        "width_mm": Frame.length(width_mil),
                    },
                }
            )
    return out


def _segment(seg: _Seg, flipped: bool, frame: Frame) -> dict[str, Any]:
    """One source segment as a precis copper segment, in the precis frame.

    The Y flip is a REFLECTION, so it reverses handedness: a sweep stored
    positive in EasyEDA's Y-down frame is clockwise once Y points up.
    That is why ``cw`` reads the sign directly instead of recomputing an
    orientation that the transform has already decided.
    """
    a, b = (seg.b, seg.a) if flipped else (seg.a, seg.b)
    start, end = frame.xy(*a), frame.xy(*b)
    if seg.kind == "line":
        return {"shape": "line", "start": list(start), "end": list(end)}
    sweep = -seg.sweep_deg if flipped else seg.sweep_deg
    centre = frame.xy(*_arc_centre(a, b, sweep))
    return {
        "shape": "arc",
        "start": list(start),
        "end": list(end),
        "center": list(centre),
        "cw": sweep > 0,
    }


def extract_vias(pcb: EproDocument, frame: Frame) -> Extraction:
    """``VIA`` records → ``ctype='via'`` rows.

    A via carries no ``layerId``; the span is ``viaType`` plus
    ``unusedInnerLayers``. Only a ``NORMAL`` via with nothing unused has
    been spiked, so anything else is reported and skipped — emitting a
    blind via as through-hole would short it to layers it must not touch.
    """
    out = Extraction()
    layers = copper_layers(pcb)
    names = list(layers.values())
    span = [names[0], names[-1]]
    for b in pcb.bodies("VIA"):
        net = str(b.get("netName") or "")
        vtype = str(b.get("viaType", ""))
        unused = b.get("unusedInnerLayers") or []
        x_mil, y_mil = b["centerX"], b["centerY"]
        if not net:
            out.warnings.append(
                f"a via at ({x_mil:.1f}, {y_mil:.1f}) mil carries no net name; "
                f"skipped — fixed copper may not invent a net"
            )
            continue
        if vtype != "NORMAL" or unused:
            out.warnings.append(
                f"via on {net} at ({x_mil:.1f}, {y_mil:.1f}) mil is "
                f"viaType={vtype!r} unusedInnerLayers={unused} — not a plain "
                f"through-hole via; skipped rather than flattened to one"
            )
            continue
        x, y = frame.xy(x_mil, y_mil)
        out.vias.append(
            {
                "ctype": "via",
                "layer": names[0],
                "net": net,
                "geom": {
                    "x": x,
                    "y": y,
                    "dia_mm": Frame.length(b["viaDiameter"]),
                    "drill_mm": Frame.length(b["holeDiameter"]),
                    "span": span,
                },
            }
        )
    return out


def live_components(pcb: EproDocument) -> dict[str, dict[str, Any]]:
    """``component id -> COMPONENT body`` for the components that exist.

    Trivial by itself; it exists so the stale-row rule has ONE definition
    that :func:`live_pad_nets` shares.
    """
    return {r.id: r.body for r in pcb.of_type("COMPONENT") if r.id and r.body}


def live_pad_nets(pcb: EproDocument) -> tuple[dict[tuple[str, str], str], list[str]]:
    """``(component id, pad number) -> net name``, stale rows dropped.

    Reto's ruling, 2026-09-30: leftovers from earlier revisions are how
    EasyEDA files come, and the import drops them rather than carrying
    them in — "we make better ones on the round trip".

    Two logically independent staleness tests:

    * **no payload.** A superseded ``PAD_NET`` keeps its key and loses its
      body, so it names no net at all.
    * **no such component.** The key's second element is a component id,
      and 289 of the 429 distinct ids on the spike board refer to
      components the PCB no longer contains.

    Measured on the spike board (2026-09-30): 984 rows are bodiless — the
    914 that name a deleted component PLUS 70 that name a live one and
    simply have no net (an unconnected pad) — leaving 599 live. So the
    payload test subsumes the component test **on this file**, and the
    component check currently rejects nothing extra. It is kept anyway
    because the two are independent claims about the data, and the one it
    guards against is the expensive direction: a row with a payload
    naming a component that is gone would bind a real net to a part that
    is not on the board.
    """
    comps = live_components(pcb)
    out: dict[tuple[str, str], str] = {}
    bodiless = orphaned = 0
    for r in pcb.of_type("PAD_NET"):
        if not r.id:
            continue
        try:
            key = json.loads(r.id)
        except json.JSONDecodeError:
            continue
        if not isinstance(key, list) or len(key) != 4:
            continue
        _, comp_id, pad_num, _pad_el = key
        if r.body is None or not r.body.get("padNet"):
            bodiless += 1
            continue
        if comp_id not in comps:
            orphaned += 1
            continue
        out[(str(comp_id), str(pad_num))] = str(r.body["padNet"])
    notes = []
    if bodiless:
        notes.append(f"{bodiless} pad-net row(s) superseded (no payload)")
    if orphaned:
        notes.append(f"{orphaned} pad-net row(s) naming a deleted component")
    return out, notes


def live_nets(pcb: EproDocument) -> tuple[list[str], list[str]]:
    """Net names that something actually references, and what was dropped.

    A ``NET`` record whose name is empty, or that no pad and no copper
    mentions, is a leftover. Importing it would put a net with no members
    into ``pcb_nets``, where it survives every later re-put (an existing
    net is reused by name) and shows up forever in ``view='drc'``'s
    unrouted census as something that can never be routed.
    """
    declared = []
    for r in pcb.of_type("NET"):
        if not r.id:
            continue
        try:
            key = json.loads(r.id)
        except json.JSONDecodeError:
            continue
        if isinstance(key, list) and len(key) == 2 and key[1]:
            declared.append(str(key[1]))
    used = set(live_pad_nets(pcb)[0].values())
    for t in ("LINE", "ARC", "VIA", "POUR"):
        used |= {str(b["netName"]) for b in pcb.bodies(t) if b.get("netName")}
    kept = sorted({n for n in declared if n in used})
    dropped = sorted(set(declared) - used)
    notes = []
    if dropped:
        notes.append(
            f"{len(dropped)} declared net(s) reference no pad and no copper; "
            f"dropped (e.g. {', '.join(dropped[:5])})"
        )
    empty = sum(1 for r in pcb.of_type("NET") if r.id and r.id.endswith('""]'))
    if empty:
        notes.append(f"{empty} NET record(s) with an empty name; dropped")
    return kept, notes


def extract_copper(pcb: EproDocument, frame: Frame) -> Extraction:
    """Every copper row this reader can recover, with all warnings merged.

    Pours are deliberately absent: ``pcb_fixed_copper``'s CHECK allows
    only ``track|via``, and a single-net pour is better modelled as a
    precis plane assignment than as frozen geometry. ``POURED`` fill is
    discarded outright — it is derived, and it is the one record type in
    the format that uses a different unit (see the module docstring).
    """
    tracks = extract_tracks(pcb, frame)
    vias = extract_vias(pcb, frame)
    merged = Extraction(
        tracks=tracks.tracks,
        vias=vias.vias,
        warnings=[*tracks.warnings, *vias.warnings],
    )
    pours = pcb.bodies("POUR")
    if pours:
        nets = sorted({str(p.get("netName") or "?") for p in pours})
        merged.warnings.append(
            f"{len(pours)} POUR region(s) on {', '.join(nets)} are not imported "
            f"as fixed copper; assign the single-net ones as planes instead"
        )
    return merged
