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
  against every other coordinate in the same document.
  :func:`extract_pours` keeps the fill as a display cache for the
  export; it scales by ten into mils before :class:`Frame`, and it
  would be a plausible-looking disaster in a reader that fed it raw.
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
* **Converting that frame into precis' costs a HALF TURN on the bottom.**
  precis mirrors a bottom instance's pads in **X**
  (``padplace._transform_local_point``) where EasyEDA mirrors them in
  **Y**, and the two reflections differ by exactly 180°. So a pad's local
  ``y`` is negated (the same reflection :class:`Frame` applies) and a
  bottom-side instance's rotation becomes ``(angle + 180) % 360``.
  Verified against :func:`precis.pcb.padplace.place_pad_point` itself
  over every side × rotation × pad combination rather than by hand
  algebra — see :func:`extract_components`.
"""

from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import PIN_ETYPES

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
    if len(polys) == 1:
        mils = _poly_points(polys[0]["path"])
    else:
        # Pro also stores an outline drawn edge by edge: several OPEN
        # line-only POLYs on the outline layer whose ends meet (Reto's TAZ
        # control board, 2026-10-13: eleven pieces, chamfered corners).
        # Chain them into the one ring the rest of the reader expects;
        # anything that does not chain into exactly one closed ring is
        # refused with the reason, never guessed at.
        mils = _chain_outline_pieces(polys)
    if not mils:
        raise EproError("the outline POLY has no vertices")
    frame = Frame(min_x_mil=min(x for x, _ in mils), max_y_mil=max(y for _, y in mils))
    return [frame.xy(x, y) for x, y in mils], frame


#: Two outline-piece endpoints closer than this (mil) are the same corner.
#: Pro writes the shared corner of two edges to 0.001 mil; 0.1 mil is 2.5 um.
_OUTLINE_JOIN_TOL_MIL = 0.1


def _chain_outline_pieces(polys: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Join open outline POLYs end to end into one closed vertex ring.

    Greedy walk from the first piece: at each step take the unused piece
    whose start (or, reversed, end) lies within :data:`_OUTLINE_JOIN_TOL_MIL`
    of the ring's current end. Refuses an arc opcode (a chorded arc would
    be a silently wrong board edge), a piece that cannot be attached, a
    leftover piece (two rings = a panel), and a ring whose last vertex
    does not return to its first.
    """
    pieces: list[list[tuple[float, float]]] = []
    for b in polys:
        extra = _outline_opcodes(b["path"]) - {"L"}
        if extra:
            raise EproError(
                f"outline POLY {b.get('id', '?')} uses {sorted(extra)} opcodes; "
                f"this reader chains line-only outline pieces (an arc edge "
                f"would be chorded silently — draw the arc as a polyline)"
            )
        pts = _poly_points(b["path"])
        if len(pts) >= 2:
            pieces.append(pts)
    if not pieces:
        return []

    def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return (
            abs(a[0] - b[0]) <= _OUTLINE_JOIN_TOL_MIL
            and abs(a[1] - b[1]) <= _OUTLINE_JOIN_TOL_MIL
        )

    ring = list(pieces[0])
    unused = pieces[1:]
    while unused:
        tail = ring[-1]
        pick = next(
            (
                (i, p if _near(p[0], tail) else list(reversed(p)))
                for i, p in enumerate(unused)
                if _near(p[0], tail) or _near(p[-1], tail)
            ),
            None,
        )
        if pick is None:
            # Nothing continues from this end; the file may list the
            # pieces in any order, so grow from the other end instead and
            # refuse only when neither end has a continuation.
            head = ring[0]
            if any(_near(p[0], head) or _near(p[-1], head) for p in unused):
                ring.reverse()
                continue
            raise EproError(
                f"{len(polys)} outline POLYs on the board-outline layer do not "
                f"chain into one ring: no piece continues from "
                f"({tail[0]:.3f}, {tail[1]:.3f}) or ({head[0]:.3f}, "
                f"{head[1]:.3f}) mil and {len(unused)} piece(s) remain (a "
                f"multi-board panel needs --board; a gap needs closing in Pro)"
            )
        i, piece = pick
        ring.extend(piece[1:])
        del unused[i]
    if not _near(ring[0], ring[-1]):
        raise EproError(
            f"the chained outline does not close: it runs from "
            f"({ring[0][0]:.3f}, {ring[0][1]:.3f}) to "
            f"({ring[-1][0]:.3f}, {ring[-1][1]:.3f}) mil"
        )
    return ring[:-1]


def layer_map(pcb: EproDocument) -> dict[int, tuple[str, str]]:
    """``layerId -> (layerType, layerName)`` for every layer the document
    declares in use. Self-describing, so no id table to drift."""
    out: dict[int, tuple[str, str]] = {}
    for rec in pcb.of_type("LAYER"):
        b = rec.body or {}
        if not b.get("use"):
            continue
        lid = b.get("layerId")
        if lid is None:
            # Newer Pro exports (Reto's TAZ board, 2026-10-13) carry the
            # id only in the record header, as the JSON string
            # '["LAYER", <n>]'; the body has layerType/layerName/use alone.
            lid = _layer_id_from_header(rec.id)
        if lid is None:
            continue
        out[int(lid)] = (str(b["layerType"]), str(b["layerName"]))
    return out


def _layer_id_from_header(rec_id: str | None) -> int | None:
    """``'["LAYER", 15]'`` -> ``15``; anything else -> ``None``."""
    if not rec_id or not rec_id.startswith("["):
        return None
    try:
        parsed = json.loads(rec_id)
    except json.JSONDecodeError:
        return None
    if (
        isinstance(parsed, list)
        and len(parsed) == 2
        and parsed[0] == "LAYER"
        and isinstance(parsed[1], int | float)
    ):
        return int(parsed[1])
    return None


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
    """Copper rows (``{"ctype", "layer", "net", "geom"}``), plus every
    judgement call the reader made on the way. The measurement workflow
    (Reto, 2026-09-30) still uses :func:`measured_copper` and
    :mod:`precis.pcb.copper_report`. Fresh intake also preserves this as
    fixed geometry (gr470192, 2026-10-06), not an inferred routing sketch."""

    tracks: list[dict[str, Any]] = field(default_factory=list)
    vias: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def measured_copper(
    pcb: EproDocument, frame: Frame
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every track and via on ``pcb`` in the flat item shape
    :mod:`precis.pcb.drc` reads (``{"ctype", "layer", "net", ...geom}``),
    plus the reader's warnings — the input
    :func:`precis.pcb.copper_report.report` measures."""
    tracks, vias = extract_tracks(pcb, frame), extract_vias(pcb, frame)
    flat = [
        {"ctype": r["ctype"], "layer": r["layer"], "net": r["net"], **r["geom"]}
        for r in (*tracks.tracks, *vias.vias)
    ]
    return flat, [*tracks.warnings, *vias.warnings]


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
    polylines, because that is what the author drew; a per-segment row
    would multiply the row
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
                    f"name; skipped — a measurement cannot attribute copper to no net"
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
                f"skipped — a measurement cannot attribute copper to no net"
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


# ── the design-rule table (RULE / RULE_SELECTOR) ───────────────────────
#: ``safeSpacing`` matrix axis order — the lower-triangular ``content``
#: pairs these object types. Read off Reto's TAZ V1.15 export
#: (2026-10-14) and confirmed against KiCad's own ``.epro2`` importer
#: (``pcb_io_easyedapro_v3_parser.cpp``'s ``safeSpacingNames``): the
#: track/track cell is ``content[0][0]`` and the row values sit in a
#: plausible order only under this axis assignment.
_SAFE_SPACING_AXES = (
    "Track",
    "SMD Pad",
    "TH Pad",
    "SMD Test Point",
    "TH Test Point",
    "Via",
    "Fill Region / Teardrop",
    "Copper / Plane Zone",
    "Slot Region",
    "Line",
    "Text / Image",
    "Board Outline",
    "Hole",
)

#: The matrix cells precis' scalar ``clearance_mm`` stands in for: rows
#: and columns up to ``Via`` are the copper the router and DRC place
#: (tracks, pads, test points, vias). Zone/fill, line, text, outline and
#: hole cells are NOT net-copper clearances — a pour keeps its own
#: clearance semantics (Pro re-pours anyway) and holes/outline are
#: separate keep-outs precis handles per-feature. Those cells survive
#: verbatim in the passthrough, so no authored value is lost.
_SAFE_COPPER_CELLS = 6  # rows and columns 0..5


def _rule_name(rec: EproRecord) -> tuple[str, str] | None:
    """``(category, name)`` of a ``RULE`` record — the second and third
    elements of its ``["RULE", category, name]`` id — or ``None``."""
    if not rec.id:
        return None
    try:
        parts = json.loads(rec.id)
    except json.JSONDecodeError:
        return None
    if isinstance(parts, list) and len(parts) >= 3 and parts[0] == "RULE":
        return str(parts[1]), str(parts[2])
    return None


def _spacing_cell_mm(content: list[Any]) -> float | None:
    """The widest copper-copper spacing in one ``safeSpacing`` entry's
    lower-triangular matrix (``content``), in mm. ``None`` when the matrix
    is empty or every copper cell is zero/absent."""
    best: float | None = None
    for row_i, row in enumerate(content[:_SAFE_COPPER_CELLS]):
        if not isinstance(row, list):
            continue
        for col_i, cell in enumerate(row[:_SAFE_COPPER_CELLS]):
            if col_i > row_i or not isinstance(cell, int | float):
                continue
            mm = Frame.length(float(cell))
            if best is None or mm > best:
                best = mm
    return best


def _track_default_mil(ctx: dict[str, Any]) -> tuple[float | None, set[float]]:
    """``(max stroDef, distinct stroDefs)`` over the TRACK rule's
    per-layer ``content`` entries — max because the authored default is a
    floor for the copper the rule intends, never a cap."""
    content = (ctx.get("track") or {}).get("content") or []
    defs = {
        float(e["stroDef"])
        for e in content
        if isinstance(e, dict) and e.get("stroDef") is not None
    }
    return (max(defs) if defs else None), defs


@dataclass
class RuleTable:
    """The design-rule table of one PCB document, split three ways:

    * ``classes``: ``{rule name: rules}`` in the ``pcb_net_classes.rules``
      shape — the mapped slice (``track_width_mm`` ← ``TRACK.stroDef``,
      ``clearance_mm`` ← the widest pad/track/via cell of
      ``SAFE.safeSpacing``, ``via_dia_mm``/``via_drill_mm`` ←
      ``RADIUS.defRadius``/``defInner`` ×2). Rule category and name
      combine into one class: an EasyEDA net carries one *named* rule
      group, which is exactly a precis net class.
    * ``default``: the name of the ``ruleState == "DEFAULT"`` group —
      the class every net takes when nothing assigns one (and nothing
      does when the board has no ``RULE_SELECTOR`` records).
    * ``records``: every ``RULE``/``RULE_SELECTOR`` record verbatim
      (``{"type", "id", "body"}``), in document order — the
      ``meta.epro_rules`` passthrough the writer emits back byte-for-byte.
      The mapped slice stays in it too: the passthrough is the lossless
      copy, the classes are what precis actually reads.
    """

    classes: dict[str, dict[str, float]] = field(default_factory=dict)
    default: str | None = None
    selectors: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract_rules(board: EproDocument) -> RuleTable:
    """Read the ``RULE``/``RULE_SELECTOR`` records of ``board``.

    **Every number in ``ruleContext`` is mils, whatever its ``unit`` field
    says** — KiCad's importer documents the same mislabel ("EasyEDA Pro
    labels this array 'mm', but its values are always mils"), and the TAZ
    V1.15 values only make sense in mils (``stroDef`` 11.811 = the 0.3 mm
    default track; solder expansion 2 = 0.05 mm).

    Unmapped categories (OTHER, CREEPAGE, NET_LENGTH, DIFFER_ENTAIL,
    BLIND, PLANE, COPPER, PASTE, SOLDER, and any future one) have no
    precis constraint class today — they are preserved verbatim in
    :attr:`RuleTable.records` and reported once in ``warnings``.
    ``RULE_SELECTOR`` records (the per-net/per-area assignments) are
    counted and preserved but never applied: their body shape has not
    been observed (the 168 on the 2025 heater board went with the file;
    V1.15 carries none), so every net falls back to the default class.
    """
    out = RuleTable()
    unmapped: set[str] = set()
    defaults: set[str] = set()
    per_layer_width: list[str] = []
    undecoded = 0

    for rec in board.of_type("RULE"):
        out.records.append({"type": "RULE", "id": rec.id, "body": rec.body})
        named = _rule_name(rec)
        if named is None or rec.body is None:
            undecoded += 1
            continue
        category, name = named
        ctx = rec.body.get("ruleContext") or {}
        # Only the per-net categories form a named rule GROUP (= a precis
        # net class). The singleton categories (OTHER, CREEPAGE, SOLDER,
        # ...) are board-global rules whose ruleState DEFAULT means "the
        # global entry", not "the default group" — TAZ V1.15 marks 11 of
        # them DEFAULT. They map to nothing and vote for nothing.
        if category == "TRACK":
            cls = out.classes.setdefault(name, {})
            width_mil, defs = _track_default_mil(ctx)
            if width_mil is not None:
                cls["track_width_mm"] = Frame.length(width_mil)
            if len(defs) > 1:
                per_layer_width.append(name)
        elif category == "SAFE":
            cls = out.classes.setdefault(name, {})
            clearances = [
                mm
                for entry in ctx.get("safeSpacing") or []
                if isinstance(entry, dict)
                for mm in [_spacing_cell_mm(entry.get("content") or [])]
                if mm is not None
            ]
            if clearances:
                cls["clearance_mm"] = max(clearances)
        elif category == "RADIUS":
            cls = out.classes.setdefault(name, {})
            dia, inner = ctx.get("defRadius"), ctx.get("defInner")
            if isinstance(dia, int | float):
                cls["via_dia_mm"] = 2.0 * Frame.length(float(dia))
            if isinstance(inner, int | float):
                cls["via_drill_mm"] = 2.0 * Frame.length(float(inner))
        else:
            unmapped.add(category)
            continue
        if rec.body.get("ruleState") == "DEFAULT":
            defaults.add(name)

    out.selectors = len(board.of_type("RULE_SELECTOR"))
    for rec in board.of_type("RULE_SELECTOR"):
        out.records.append({"type": "RULE_SELECTOR", "id": rec.id, "body": rec.body})

    if len(defaults) == 1:
        out.default = next(iter(defaults))
    elif defaults:
        out.warnings.append(
            f"the rule table marks {len(defaults)} different groups DEFAULT "
            f"({', '.join(sorted(defaults))}); nets take {sorted(defaults)[0]!r}"
        )
        out.default = sorted(defaults)[0]
    if undecoded:
        out.warnings.append(
            f"{undecoded} RULE record(s) are preserved verbatim but their id "
            "is not the [RULE, category, name] shape precis decodes — any "
            "keepout/rule-area among them would bind nothing on the precis "
            "side (docs/backlog/pcb-keepout-does-not-bind.md)"
        )
    if unmapped:
        out.warnings.append(
            f"{len(unmapped)} design-rule categor(ies) have no precis "
            f"equivalent ({', '.join(sorted(unmapped))}) — preserved verbatim "
            f"in meta.epro_rules and written back on export"
        )
    if out.selectors:
        out.warnings.append(
            f"{out.selectors} RULE_SELECTOR record(s) (per-net/per-area rule "
            f"assignments) are preserved verbatim but NOT applied — their "
            f"body shape has never been observed; affected nets take the "
            f"default rule instead"
        )
    if per_layer_width:
        out.warnings.append(
            f"rule group(s) {', '.join(sorted(per_layer_width))} set different "
            f"default track widths per layer; precis carries one width per "
            f"class — the widest is used"
        )
    return out


#: Board-level records with no precis home that carry NO geometry —
#: preserved verbatim so a re-export keeps the board's physical stackup
#: (``LAYER_PHYS``, the dielectric table precis' stackup does not model),
#: its document title (``META``) and its canvas/preference/silk state.
#: Geometry-bearing records (``IMAGE``, ``DIMENSION``, board silk
#: ``POLY``/``STRING``) are deliberately absent: their coordinates live in
#: the source's own frame and would re-emit misplaced.
_BOARD_PASSTHROUGH = (
    "LAYER_PHYS",
    "META",
    "CANVAS",
    "PREFERENCE",
    "SILK_OPTS",
    "PRIMITIVE",
    "ACTIVE_LAYER",
)


def extract_board_state(board: EproDocument) -> list[dict[str, Any]]:
    """The PCB document's geometry-free verbatim records, in document
    order — the ``meta.epro_board`` passthrough the writer emits back
    unchanged, so a re-export carries the physical stackup table and the
    board's title rather than only what precis models."""
    return [
        {"type": rec.type, "id": rec.id, "body": rec.body}
        for rec in board.records
        if rec.type in _BOARD_PASSTHROUGH
    ]


def extract_pours(
    pcb: EproDocument, frame: Frame
) -> tuple[list[dict[str, Any]], list[str]]:
    """``POUR`` zones as board-frame ``ctype='pour'`` items (``{"layer",
    "net", "polygon"}`` in precis coords) — the AUTHORED zone outline.

    Import does not model a pour: an inner-layer single-net zone becomes
    the layer's plane, a board-covering outer one a pour-carried net
    (``derive_stackup``'s ``pour_nets``, gr486958), a small outer one a
    warning, and the polygon is otherwise dropped. These items exist so the design keeps the
    authored polygon in ``meta.epro_pours`` — the export writes it back
    verbatim and Pro re-pours inside it on open, which beats rebuilding
    the zone from the stackup. The source's computed fill
    (``POURED.pourFill``) rides along verbatim too, as a display cache
    so the reopened file shows poured copper immediately.

    A ``"R"`` rectangle op becomes its four corners; the vertex-list
    ``"L"`` path Pro 3.2.206 writes for a free-form zone (TAZ V1.15's
    four pours) carries straight through. A multi-path pour is warned
    and its first path wins — the writer emits one path the same way.
    """
    layers = copper_layers(pcb)
    poured_by_pour: dict[str, dict[str, Any]] = {}
    for rec in pcb.of_type("POURED"):
        try:
            ref = json.loads(rec.id or "")
        except ValueError:
            continue
        if isinstance(ref, list) and len(ref) == 2 and ref[0] == "POURED" and rec.body:
            poured_by_pour[str(ref[1])] = rec.body
    pours: list[dict[str, Any]] = []
    warnings: list[str] = []
    for rec in pcb.of_type("POUR"):
        body = rec.body
        if body is None:
            continue
        lid = body.get("layerId")
        layer = layers.get(int(lid)) if lid is not None else None
        if layer is None:
            warnings.append(
                f"a POUR names layerId {lid}, which the LAYER records do "
                "not declare as copper; skipped"
            )
            continue
        paths = body.get("path") or []
        if len(paths) != 1:
            warnings.append(
                f"a POUR on {layer} ({body.get('netName')}) carries "
                f"{len(paths)} path(s); precis keeps the first only"
            )
        first = paths[0] if paths else []
        if first and first[0] == "R":
            # ["R", x, y, w, h, angle, ?] — the anchor is (x, y) = the
            # min-x/MAX-y corner in file coords: the box spans x..x+w,
            # y-h..y (Pro 3.2.149's own write; pcb-epro-export.md 2d).
            if len(first) < 5:
                warnings.append("a POUR 'R' path is too short; skipped")
                continue
            rx, ry, rw, rh = (float(v) for v in first[1:5])
            poly = [
                frame.xy(rx, ry - rh),
                frame.xy(rx + rw, ry - rh),
                frame.xy(rx + rw, ry),
                frame.xy(rx, ry),
            ]
        else:
            pts = _poly_points(first)
            if len(pts) < 3:
                warnings.append(
                    f"a POUR on {layer} ({body.get('netName')}) carries a "
                    f"path of {len(pts)} vertex/vertices; skipped"
                )
                continue
            poly = [frame.xy(px, py) for px, py in pts]
        item: dict[str, Any] = {
            "layer": layer,
            "net": str(body.get("netName") or ""),
            "polygon": [[x, y] for x, y in poly],
            "keep_island": bool(body.get("keepIsland")),
            "name": str(body.get("name") or ""),
        }
        poured = poured_by_pour.get(rec.id or "")
        if poured:
            regions = []
            for region in poured.get("pourFill") or []:
                rings = []
                for p in region.get("path") or []:
                    pts = _poly_points(p)
                    if len(pts) >= 3:
                        # 10-mil Standard units -> mils -> mm (the trap
                        # from the module docstring; never raw frame.xy).
                        rings.append(
                            [
                                [x, y]
                                for x, y in (
                                    frame.xy(px * 10.0, py * 10.0) for px, py in pts
                                )
                            ]
                        )
                if rings:
                    regions.append(
                        {
                            "id": str(region.get("id") or ""),
                            "fill": bool(region.get("fill", True)),
                            "stroke_width": float(region.get("strokeWidth") or 0.0),
                            "paths": rings,
                        }
                    )
            if regions:
                item["fill"] = regions
        pours.append(item)
    return pours, warnings


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
    if not pcb.of_type("NET"):
        # Reto's TAZ V1.15 export (2026-10-13) carries no NET records at
        # all: the netlist exists only as the names its PAD_NETs and
        # copper mention. Those names ARE the live nets then — the
        # intersection below would drop every connection on the board.
        return sorted(used), [
            f"no NET records; {len(used)} net(s) taken from the pads and copper "
            f"that name them"
        ]
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


# ── placement + netlist (slice 1b) ──────────────────────────────────────
#: ``COMPONENT.layerId`` values. The board's copper layer ids are
#: self-describing (:func:`layer_map`) but a component's SIDE is not a
#: copper-layer reference — 1/2 are the editor's fixed top/bottom
#: component layers, and the spike board's 86/54 split across them agrees
#: with Reto's own ground truth ("SW2 is top, U24 is bottom").
_COMP_TOP, _COMP_BOTTOM = 1, 2

#: ``defaultPad.padType`` -> the precis pad shape
#: ``precis.store._pcb_ops._normalize_local_footprint_pad`` accepts.
#: ``ELLIPSE`` maps to ``circle`` only when it is actually circular; an
#: ellipse with width != height has no precis shape at all and degrades
#: to ``obround`` with a warning. ``RECT`` carries a corner ``radius``
#: precis cannot express either — zero on all 256 of the spike board's
#: rect pads, so a non-zero one is warned about rather than assumed
#: impossible.
_PAD_SHAPES = {"RECT": "rect", "ELLIPSE": "circle", "OVAL": "obround"}


#: Component ``ATTR`` keys that carry the part's value, in precedence
#: order. ``Value``/``Comment`` are the generic ones; EasyEDA's own
#: symbol library puts the value on typed attrs instead (TAZ V1.15 has
#: ``Resistance (Ohms)`` ×16, ``Resistance``, ``Capacitance``, no plain
#: ``Value`` at all).
_PART_VALUE_KEYS = (
    "Value",
    "Comment",
    "Resistance (Ohms)",
    "Resistance",
    "Capacitance",
    "Inductance",
)


def _component_identity(a: dict[str, str]) -> dict[str, str]:
    """The catalog identity a component's ATTRs carry:
    ``{"lcsc", "value", "mpn", "mfr", "supplier", "supplier_part"}``, each
    only when present.

    ``Supplier Part`` is the LCSC C-number on a JLC-sourced part (Supplier
    "LCSC"); it lands on the lcsc key only in ``C<digits>`` shape, so a
    free-text supplier code never ends up in the ``part_lcsc`` join
    column. ``lcsc`` is upper-cased to match ``parts.lcsc``.
    """
    out: dict[str, str] = {}
    for key in ("Supplier Part", "LCSC"):
        cand = (a.get(key) or "").strip().upper()
        if re.fullmatch(r"C\d+", cand):
            out["lcsc"] = cand
            break
    value = next(
        (
            (a.get(k) or "").strip()
            for k in _PART_VALUE_KEYS
            if (a.get(k) or "").strip()
        ),
        "",
    )
    if value:
        out["value"] = value
    for key, name in (
        ("Manufacturer Part", "mpn"),
        ("Manufacturer", "mfr"),
        ("Supplier", "supplier"),
        ("Supplier Part", "supplier_part"),
        # The device/symbol's display name — for a passive it IS the
        # value ("100n", "243Ω"), for an IC the MPN-ish symbol name;
        # carried either way as part_meta["name"].
        ("Name", "name"),
        # The schematic's `link` attr is the part's datasheet URL (an
        # lcsc.com/eda_search link or the real datasheet PDF).
        ("link", "link"),
        # JLC's own assembly tier ("Basic Part"/"Extended Part") —
        # availability reasoning, not identity, but cheap to keep.
        ("JLCPCB Part Class", "part_class"),
    ):
        v = (a.get(key) or "").strip()
        if v:
            out[name] = v
    # "Add into BOM" is the author's own keep-out — only the negative is
    # worth recording (the default is yes).
    bom = (a.get("Add into BOM") or "").strip().lower()
    if bom in ("no", "false", "0"):
        out["bom"] = "no"
    return out


def _footprint_ref(attrs: dict[str, str]) -> str | None:
    """The footprint uuid a component's attributes point at. The 2025
    heater-base export keyed it ``Footprint``; Reto's TAZ V1.15 export
    (2026-10-13) keys the same uuid ``FootprintName``. Either, first
    non-empty wins."""
    return attrs.get("Footprint") or attrs.get("FootprintName") or None


def component_attrs(pcb: EproDocument) -> dict[str, dict[str, str]]:
    """``component id -> {attr key: value}``.

    A ``COMPONENT`` body carries position, angle and side but neither its
    refdes nor which footprint it uses; both are ``ATTR`` records
    pointing back by ``parentId``. The spike board has exactly three keys
    per component — ``Designator``, ``Footprint``, ``Device`` — 140 of
    each, so this is the whole indirection and not a sample of it.
    """
    out: dict[str, dict[str, str]] = {}
    for r in pcb.of_type("ATTR"):
        b = r.body or {}
        parent, key = b.get("parentId"), b.get("key")
        if not parent or not key:
            continue
        out.setdefault(str(parent), {})[str(key)] = str(b.get("value") or "")
    return out


def symbol_pin_info(symbol: EproDocument) -> dict[str, dict[str, str]]:
    """``pin record id -> {"num", "name", "type"}`` from one ``SYMBOL``
    document — the fuller read of what ``symbol_pin_names`` discards.

    A ``PIN`` record carries geometry only — no name, no number. Both are
    ``ATTR`` records hanging off the pin by ``parentId``, keyed ``Pin
    Number`` and ``Pin Name``; ``Pin Type`` (``IN``/``OUT``/``IO``/…, the
    symbol's own electrical-type word) is the third, and is what feeds a
    pin's ``etype`` for ERC (pcb-datasheet-review.md slice 1).
    """
    out: dict[str, dict[str, str]] = {}
    for r in symbol.of_type("ATTR"):
        b = r.body or {}
        parent, key, val = b.get("parentId"), b.get("key"), b.get("value")
        if not parent or val in (None, ""):
            continue
        ent = out.setdefault(str(parent), {})
        if key == "Pin Number":
            ent["num"] = str(val)
        elif key == "Pin Name":
            ent["name"] = str(val)
        elif key == "Pin Type":
            ent["type"] = str(val)
    return out


def symbol_pin_names(symbol: EproDocument) -> dict[str, str]:
    """``pad number -> pin name`` — derived from :func:`symbol_pin_info`.

    A pin with a number and no name keeps the number as its name, which
    is the identity case most passives are: on the spike board 100 of 140
    components resolve to an identity map and 40 to real signal names.
    """
    return {
        info["num"]: info.get("name") or info["num"]
        for info in symbol_pin_info(symbol).values()
        if info.get("num")
    }


#: EasyEDA's symbol ``Pin Type`` vocabulary -> precis ``PIN_ETYPES``.
#: ``Undefined`` (the common case — 295 of 372 on taz15) and anything
#: unrecognized map to *unset*: a missing etype is honest, a guessed one
#: is wrong data ERC would trust.
_EASYEDA_ETYPE = {
    "IN": "input",
    "OUT": "output",
    "IO": "bidi",
    "OC": "open_drain",
    "HIZ": "tristate",
    "PASSIVE": "passive",
    "POWER": "power_in",
    "NC": "nc",
}


def sch_component_attrs(project: EproProject) -> dict[str, dict[str, str]]:
    """``{refdes: {attr key: value}}`` across every ``SCH_PAGE`` document.

    The schematic carries component attrs the board doc does not —
    ``link`` (the part's datasheet URL), ``NO_CONNECT`` markers and,
    secondhand, the same Supplier Part/Manufacturer/Name triple the PCB
    side already shows. On taz15 the two sides' shared keys agree
    exactly; the merge in ``extract_components`` lets the board's copy
    win any conflict anyway."""
    out: dict[str, dict[str, str]] = {}
    for doc in project.by_type("SCH_PAGE"):
        for a in component_attrs(doc).values():
            refdes = (a.get("Designator") or "").strip()
            if refdes:
                out[refdes] = a
    return out


def sch_no_connects(project: EproProject) -> dict[str, set[str]]:
    """``{refdes: {pad numbers}}`` — the schematic's ``NO_CONNECT``
    markers. Each is an ``ATTR`` whose ``parentId`` is
    ``<schematic-component-id>-<symbol-pin-record-id>``: the schematic
    component gives the refdes and (via its ``Symbol`` attr) the symbol
    document whose pin record resolves the pad number."""
    symbols = {d.uuid: d for d in project.by_type("SYMBOL") if d.uuid}
    out: dict[str, set[str]] = {}
    for doc in project.by_type("SCH_PAGE"):
        attrs = component_attrs(doc)
        for r in doc.of_type("ATTR"):
            b = r.body or {}
            if b.get("key") != "NO_CONNECT":
                continue
            if str(b.get("value") or "").lower() not in ("yes", "true", "1"):
                continue
            comp_id, _, pin_id = str(b.get("parentId") or "").partition("-")
            comp = attrs.get(comp_id)
            if comp is None or not pin_id:
                continue
            sym = symbols.get(str(comp.get("Symbol") or ""))
            refdes = (comp.get("Designator") or "").strip()
            num = (
                (symbol_pin_info(sym).get(pin_id) or {}).get("num")
                if sym is not None
                else None
            )
            if refdes and num:
                out.setdefault(refdes, set()).add(num)
    return out


def device_pin_names(project: EproProject) -> dict[str, dict[str, str]]:
    """``device uuid -> {pad number: pin name}``, walking the whole chain.

    A ``DEVICE`` document holds a single ``META`` record and no geometry;
    its ``attributes["Symbol"]`` names the ``SYMBOL`` document that has
    the pins. Verified end to end on the spike board: all 140 components
    resolve a Device, a Symbol and a non-empty pin map, and no symbol
    names a pad its footprint lacks.
    """
    symbols = {d.uuid: d for d in project.by_type("SYMBOL") if d.uuid}
    out: dict[str, dict[str, str]] = {}
    for dev in project.by_type("DEVICE"):
        if not dev.uuid:
            continue
        metas = dev.bodies("META")
        sym_uuid = (metas[0].get("attributes") or {}).get("Symbol") if metas else None
        sym = symbols.get(str(sym_uuid)) if sym_uuid else None
        if sym is not None:
            out[dev.uuid] = symbol_pin_names(sym)
    return out


def footprint_title(footprint: EproDocument) -> str:
    """The footprint's human name (``META.title``), or its uuid.

    The uuid is what a ``COMPONENT``'s ``Footprint`` ``ATTR`` actually
    names, but it is meaningless in ``view='bom'``; the title is the
    library name (``SW-SMD_4P-L5.1-W5.1-P3.70-LS6.5-TL_H1.5``). All 33 of
    the spike board's footprints carry a distinct non-empty title, so the
    fallback is defensive rather than routine.
    """
    metas = footprint.bodies("META")
    title = str((metas[0].get("title") if metas else "") or "").strip()
    return title or f"epro:{footprint.uuid}"


def _pad_to_precis(
    b: dict[str, Any], where: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """One EasyEDA ``PAD`` body -> one precis footprint pad, or ``None``
    when the shape has no precis equivalent at all.

    Footprint-LOCAL coordinates, and the Y negation here is the same
    reflection :class:`Frame` applies to the board: ``centerY`` grows
    down, precis' pad-local ``y`` grows up. The rotation half of the
    conversion is deliberately NOT here — it belongs to the instance
    (:func:`extract_components`), because precis' ``padplace`` applies
    the instance rotation to these local coordinates itself.
    """
    warnings: list[str] = []
    num = str(b.get("num") or "").strip()
    if not num:
        return None, [f"{where}: a PAD with no 'num' cannot be addressed; skipped"]
    dp = b.get("defaultPad") or {}
    ea_shape = str(dp.get("padType") or "").upper()
    w_mil, h_mil = dp.get("width"), dp.get("height")

    pad: dict[str, Any] = {
        "pin": num,
        "x": Frame.length(float(b.get("centerX") or 0.0)),
        # Y-down -> Y-up. The one-line reflection the whole frame rests on.
        # ``or 0.0`` collapses the negative zero the negation produces at
        # y=0: it compares equal to 0.0 but serialises as ``-0.0``, which
        # would make a round-trip byte comparison fail on a pad that is
        # exactly on the centre line.
        "y": -Frame.length(float(b.get("centerY") or 0.0)) or 0.0,
        "rot": float(b.get("padAngle") or 0.0),
    }
    if ea_shape == "POLYGON":
        pts = _poly_points(dp.get("path") or [])
        if len(pts) < 3:
            return None, [
                f"{where}: pad {num!r} is a POLYGON with {len(pts)} vertices; skipped"
            ]
        pad["shape"] = "polygon"
        pad["poly"] = [[Frame.length(px), -Frame.length(py) or 0.0] for px, py in pts]
    elif ea_shape in _PAD_SHAPES:
        shape = _PAD_SHAPES[ea_shape]
        if w_mil is None:
            return None, [f"{where}: pad {num!r} ({ea_shape}) has no width; skipped"]
        w = Frame.length(float(w_mil))
        h = Frame.length(float(h_mil)) if h_mil is not None else w
        if ea_shape == "ELLIPSE" and w_mil != h_mil:
            shape = "obround"
            warnings.append(
                f"{where}: pad {num!r} is an ellipse {w_mil}x{h_mil} mil; precis "
                f"has no ellipse shape, imported as an obround (same bbox, "
                f"squarer ends)"
            )
        if ea_shape == "RECT" and float(dp.get("radius") or 0.0) != 0.0:
            warnings.append(
                f"{where}: pad {num!r} is a rounded rect (radius "
                f"{dp['radius']} mil); precis has no corner radius, imported "
                f"as a square-cornered rect of the same bbox"
            )
        pad["shape"] = shape
        pad["w"] = w
        pad["h"] = h
    else:
        return None, [
            f"{where}: pad {num!r} has shape {ea_shape or '<none>'!r}, which has "
            f"no precis equivalent; skipped rather than approximated blindly"
        ]

    hole = b.get("hole")
    if isinstance(hole, dict) and hole.get("width"):
        # Through-hole. ``drill`` is the round tool diameter; a SLOT (or any
        # width != height hole) keeps the SHORT axis there (the cutter
        # width) and the full extent in ``slot`` {w, h}, in the same
        # pad-local frame as the pad's own ``w``/``h`` (EasyEDA authors the
        # hole in the unrotated pad frame; ``padAngle`` turns both).
        hw = float(hole["width"])
        hh = float(hole.get("height") or hole["width"])
        htype = str(hole.get("holeType") or "ROUND").upper()
        if htype not in ("ROUND", "SLOT"):
            warnings.append(
                f"{where}: pad {num!r} has an unknown holeType {htype!r} "
                f"({hw}x{hh} mil); imported by its hole extents"
            )
        pad["drill"] = Frame.length(min(hw, hh))
        if hw != hh:
            pad["slot"] = {"w": Frame.length(hw), "h": Frame.length(hh)}
    return pad, warnings


def pad_numbers(pads: list[dict[str, Any]]) -> list[str]:
    """The DISTINCT pad numbers of a footprint, in first-seen order.

    Several pads legitimately share one number — a split thermal pad, a
    connector shield broken into tabs — and they are one electrical pin,
    not several. The spike board has one such footprint
    (``SMD-1_BD8.7-D6.2``, three pads all numbered ``1``). Every caller
    that means "the pins of this footprint" wants this, not the pad list:
    precis' ``pin_map`` is keyed by pad number, so all three resolve to
    the same pin name through ``padplace.pad_label`` — which is the
    correct model, and why duplicates here are collapsed rather than
    renamed apart.
    """
    seen: list[str] = []
    for pad in pads:
        num = str(pad.get("pin") or pad.get("number") or "")
        if num and num not in seen:
            seen.append(num)
    return seen


def _dedup_pin_names(
    numbers: list[str], pin_names: dict[str, str], where: str
) -> tuple[dict[str, str], list[str]]:
    """``pad number -> pin name`` with name collisions broken apart.

    Resolves the ``(component_id, name)`` collision four ``GND`` pins
    would cause on ``pcb_pins`` by suffixing the pad number. Not
    hypothetical: one of the spike board's 47 symbols names six pins
    ``GND``. Copper still lands correctly either way, because ``PAD_NET``
    binds a net per PAD — but two pins sharing a name cannot both exist
    as rows, so the second would vanish and its connections with it.

    Takes DISTINCT pad numbers (:func:`pad_numbers`): two pads sharing a
    number are one pin and must keep one name, so renaming them apart
    would invent a pin the board does not have — and would collide
    anyway, since the suffix is that same shared number.
    """
    warnings: list[str] = []
    claimed: dict[str, str] = {}
    resolved: dict[str, str] = {}
    for num in numbers:
        name = pin_names.get(num, num)
        if name in claimed:
            renamed = f"{name}_{num}"
            warnings.append(
                f"{where}: pads {claimed[name]!r} and {num!r} are both named "
                f"{name!r}; pad {num} imported as pin {renamed!r} so both "
                f"survive (pcb_pins is keyed on the name)"
            )
            name = renamed
        claimed[name] = num
        resolved[num] = name
    return resolved, warnings


@dataclass
class Design:
    """Everything ``Store.pcb_apply`` needs, plus every judgement call.

    The pure reader's whole output for the netlist/placement half —
    :mod:`precis.ingest.pcb_epro` turns this into DB rows and adds
    nothing to it, so what a ``--dry-run`` prints and what an import
    writes cannot drift apart.
    """

    components: list[dict[str, Any]] = field(default_factory=list)
    nets: list[dict[str, Any]] = field(default_factory=list)
    connections: list[dict[str, Any]] = field(default_factory=list)
    footprints: list[dict[str, Any]] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``{name: rules}`` for ``pcb_net_classes`` — the mapped slice of the
    #: board's RULE table (:func:`extract_rules`). Empty when the file
    #: carries no rule records.
    net_classes: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Every RULE/RULE_SELECTOR record verbatim (``{"type","id","body"}``)
    #: — the ``meta.epro_rules`` passthrough the writer can emit back
    #: unchanged. The mapped net classes are derived from these, not from
    #: this list.
    epro_rules: list[dict[str, Any]] = field(default_factory=list)
    #: The board's geometry-free records verbatim (LAYER_PHYS, META,
    #: CANVAS, … — :func:`extract_board_state`) — the ``meta.epro_board``
    #: passthrough.
    epro_board: list[dict[str, Any]] = field(default_factory=list)
    #: Authored ``POUR`` zone outlines in board coords
    #: (:func:`extract_pours`) — the ``meta.epro_pours`` passthrough. Not
    #: precis copper: an inner pour is the layer's plane, an outer one
    #: was never imported at all; the polygon exists so export writes the
    #: authored zone back.
    epro_pours: list[dict[str, Any]] = field(default_factory=list)
    #: Counts the CLI prints, so a human can compare them to the source.
    stats: dict[str, int] = field(default_factory=dict)


#: A footprint's ``COMPONENT_SHAPE`` layer — EasyEDA Pro's courtyard, the
#: body outline its own component-spacing check reads. Spike-verified
#: 2026-10-02 on the real board: 31 of its 33 footprints draw one, as
#: ``POLY`` line runs in footprint-local mil.
_FP_COURTYARD_LAYER = 48

#: The MULTI layer. A ``FILL`` on it inside a footprint is a board cutout:
#: KiCad's EasyEDA Pro importer maps layer 12 to Edge.Cuts. Spike-verified
#: 2026-10-02 on the real board: 7 such FILLs, all circles: the Ø6.4 mm
#: hole of the ``SMD-1_BD8.7-D6.2`` standoff and the Ø1.1-1.6 mm peg holes
#: of three connectors. Imported as non-plated holes.
_FP_MULTI_LAYER = 12

#: Courtyard excess around the pads when a footprint draws no courtyard
#: of its own: IPC-7351's nominal (density level B) 0.25 mm.
_FALLBACK_COURTYARD_EXCESS_MM = 0.25


def _footprint_courtyard(
    doc: EproDocument, pads: list[dict[str, Any]]
) -> dict[str, Any]:
    """``{"bbox": [x0, y0, x1, y1]}`` in footprint-local mm (Y up, as the
    pads): the bounding box of the footprint's own courtyard polygon(s),
    grown to enclose every pad. Without one the store falls back to the
    bare pad extent, a courtyard lying ON the pad edges, which is the
    footprint's stored body size for its DRC bbox fallback and SVG
    viewBox. A footprint that draws none gets the pad extent plus
    :data:`_FALLBACK_COURTYARD_EXCESS_MM`. Non-line opcodes (arcs,
    circles) make a polygon's flat number list unreadable as vertices, so
    such a polygon is skipped rather than misread."""
    xs: list[float] = []
    ys: list[float] = []
    for pad in pads:
        if pad.get("poly"):
            xs += [v[0] for v in pad["poly"]]
            ys += [v[1] for v in pad["poly"]]
        else:
            xs += [pad["x"] - pad["w"] / 2.0, pad["x"] + pad["w"] / 2.0]
            ys += [pad["y"] - pad["h"] / 2.0, pad["y"] + pad["h"] / 2.0]
    margin = _FALLBACK_COURTYARD_EXCESS_MM
    for b in doc.bodies("POLY"):
        path = b.get("path") or []
        if b.get("layerId") != _FP_COURTYARD_LAYER or _outline_opcodes(path) - {"L"}:
            continue
        for x, y in _poly_points(path):
            xs.append(Frame.length(x))
            ys.append(-Frame.length(y) or 0.0)
            margin = 0.0
    return {
        "bbox": [
            round(min(xs) - margin, 4),
            round(min(ys) - margin, 4),
            round(max(xs) + margin, 4),
            round(max(ys) + margin, 4),
        ]
    }


def extract_footprints(
    project: EproProject, pcb: EproDocument
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """The ``FOOTPRINT`` documents the board actually places, as
    ``pcb_apply`` ``footprints[]`` entries, plus ``uuid -> name``.

    Only referenced footprints are emitted: the spike board's project
    carries 33 and places 20, and an unreferenced library leftover in
    ``pcb_local_footprints`` is noise a reader of the imported board
    would have to explain away.

    Each footprint gets ONE ``pin_map``, which is only well-defined
    because a footprint carries one set of pin names across every
    component that places it — verified: zero of the spike board's 20
    placed footprints see two different maps. A board that violated that
    would need per-variant footprints, so it is detected and warned
    about rather than resolved by last-write-wins.
    """
    warnings: list[str] = []
    by_uuid = {d.uuid: d for d in project.by_type("FOOTPRINT") if d.uuid}
    pin_maps = device_pin_names(project)
    attrs = component_attrs(pcb)

    # footprint uuid -> the device uuids placed with it
    placed: dict[str, set[str]] = {}
    for r in pcb.of_type("COMPONENT"):
        if not r.id or not r.body:
            continue
        a = attrs.get(r.id, {})
        fp_uuid = _footprint_ref(a)
        if fp_uuid:
            placed.setdefault(fp_uuid, set()).add(a.get("Device") or "")

    names: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for fp_uuid, device_uuids in sorted(placed.items()):
        doc = by_uuid.get(fp_uuid)
        if doc is None:
            warnings.append(
                f"footprint {fp_uuid!r} is placed but the project carries no "
                f"FOOTPRINT document for it; its components import with no pads"
            )
            continue
        title = footprint_title(doc)
        name = title
        if name in names.values():
            # Two footprints with one title: the join key must stay unique
            # or one silently wins. Never seen (33 distinct titles for 33
            # documents) but cheap to make impossible.
            name = f"{title}~{fp_uuid[:8]}"
            warnings.append(
                f"two footprints share the title {title!r}; the second is "
                f"imported as {name!r} so both keep their own pads"
            )
        names[fp_uuid] = name

        pads: list[dict[str, Any]] = []
        for b in doc.bodies("PAD"):
            pad, pad_warnings = _pad_to_precis(b, f"footprint {name!r}")
            warnings += pad_warnings
            if pad is not None:
                pads.append(pad)
        if not pads:
            warnings.append(
                f"footprint {name!r} yielded no usable pads; its components "
                f"import as placed outlines with nothing to solder"
            )
            continue

        maps = {tuple(sorted(pin_maps.get(d, {}).items())) for d in device_uuids if d}
        if len(maps) > 1:
            warnings.append(
                f"footprint {name!r} is placed by {len(maps)} devices that name "
                f"its pins DIFFERENTLY; precis stores one pin map per footprint, "
                f"so the first is used and the others fall back to pad numbers"
            )
        chosen = dict(sorted(maps)[0]) if maps else {}
        pin_map, dedup_warnings = _dedup_pin_names(
            pad_numbers(pads), chosen, f"footprint {name!r}"
        )
        warnings += dedup_warnings
        out.append(
            {
                "name": name,
                "pads": pads,
                "pin_map": pin_map,
                "courtyard": _footprint_courtyard(doc, pads),
                "holes": _footprint_holes(doc, name, warnings),
            }
        )
    return out, names, warnings


def _footprint_holes(
    doc: EproDocument, name: str, warnings: list[str]
) -> list[dict[str, float]]:
    """The footprint's non-plated holes, ``{x, y, dia_mm}`` in
    footprint-local mm (the pads' frame): every circular ``FILL`` on
    :data:`_FP_MULTI_LAYER`. A non-circular one is a cutout of another
    shape, which precis has no model for, so it is warned about and
    dropped rather than approximated."""
    out: list[dict[str, float]] = []
    for b in doc.bodies("FILL"):
        if b.get("layerId") != _FP_MULTI_LAYER:
            continue
        path = b.get("path") or []
        circle = path[0] if path and isinstance(path[0], list) else path
        if len(circle) == 4 and circle[0] == "CIRCLE":
            out.append(
                {
                    "x": Frame.length(float(circle[1])),
                    "y": -Frame.length(float(circle[2])) or 0.0,
                    "dia_mm": 2.0 * Frame.length(float(circle[3])),
                }
            )
        else:
            warnings.append(
                f"footprint {name!r} has a non-circular cutout on the multi "
                f"layer; precis has no cutout model, so it is NOT imported"
            )
    return out


def extract_components(
    project: EproProject,
    pcb: EproDocument,
    frame: Frame,
    footprint_names: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Placed ``COMPONENT`` records as ``pcb_apply`` ``components[]``,
    plus the ``component id -> refdes`` map of the records that WON.

    That map is returned rather than re-derived because a connection is
    keyed by component id and has to reach the same row: two components
    sharing a designator means one is skipped, and a caller that
    re-resolved id -> refdes by name would bind the skipped one's pads to
    the surviving row's pins — silently moving copper onto the wrong part.

    **The instance rotation is not the source angle.** A bottom-side
    component takes ``(angle + 180) % 360``, because precis mirrors a
    bottom instance's pads in **X** (``padplace._transform_local_point``)
    while EasyEDA mirrors them in **Y**, and those two reflections differ
    by exactly a half turn. Verified against ``padplace.place_pad_point``
    itself over every side x rotation combination: pad-local ``y``
    negated plus this half turn reproduces the spike-verified source-frame
    rule exactly, and dropping the half turn puts every bottom-side pad
    diametrically opposite its true position — a board that renders
    plausibly and cannot be built.

    The catalog identity attrs — ``Supplier Part`` (the LCSC C-number),
    ``Value``/``Comment``/typed value keys, ``Manufacturer Part``,
    ``Manufacturer``, ``Supplier``, plus the schematic-side ``Name``,
    ``link`` (datasheet URL) and ``JLCPCB Part Class`` — land on the
    component as ``part_lcsc``, ``label`` (value, then MPN, then Name,
    else the refdes the store already fills) and a ``part_meta`` blob
    (:func:`_component_identity`). ``part_lcsc`` used to be dropped on
    purpose: the footprint join consulted ``pcb_local_footprints`` only
    when ``part_lcsc IS NULL``, so importing the C-number orphaned every
    pad this reader had recovered. The join now resolves the design-local
    footprint FIRST whenever the instance names one (Reto 2026-10-14:
    "import the numbers, then keep the board's own footprint where it
    really differs"), so carrying the C-number is safe — and it is what
    queues datasheet pulls, mints board→part edges and fills the BOM.

    Pins carry the symbol's electrical type (``Pin Type`` -> ``etype``)
    and the schematic's ``NO_CONNECT`` markers (``nc``) — the ERC half
    of pcb-datasheet-review.md slice 1.
    """
    warnings: list[str] = []
    attrs = component_attrs(pcb)
    sch_attrs = sch_component_attrs(project)
    nc_by_refdes = sch_no_connects(project)
    symbols = {d.uuid: d for d in project.by_type("SYMBOL") if d.uuid}
    devices = {d.uuid: d for d in project.by_type("DEVICE") if d.uuid}
    pin_maps = device_pin_names(project)
    fp_docs = {d.uuid: d for d in project.by_type("FOOTPRINT") if d.uuid}

    out: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for r in pcb.of_type("COMPONENT"):
        if not r.id or not r.body:
            continue
        b = r.body
        a = attrs.get(r.id, {})
        refdes = (a.get("Designator") or "").strip()
        if not refdes:
            warnings.append(
                f"component {r.id!r} has no Designator attribute; skipped "
                f"(a refdes is how every other record addresses it)"
            )
            continue
        if refdes in seen:
            warnings.append(
                f"two components share the designator {refdes!r}; the second "
                f"is skipped (refdes is the identity precis applies on)"
            )
            continue
        seen[refdes] = r.id

        layer_id = int(b.get("layerId") or _COMP_TOP)
        if layer_id not in (_COMP_TOP, _COMP_BOTTOM):
            warnings.append(
                f"{refdes}: layerId {layer_id} is neither the top ({_COMP_TOP}) "
                f"nor the bottom ({_COMP_BOTTOM}) component layer; imported as top"
            )
        bottom = layer_id == _COMP_BOTTOM
        x, y = frame.xy(float(b.get("x") or 0.0), float(b.get("y") or 0.0))
        angle = float(b.get("angle") or 0.0)

        comp: dict[str, Any] = {
            "refdes": refdes,
            "x": x,
            "y": y,
            # See the docstring: the half turn is the bottom-side mirror
            # axis difference, not a fudge factor.
            "rot": (angle + (180.0 if bottom else 0.0)) % 360.0,
            "layer": "bottom" if bottom else "top",
        }
        fp_uuid = _footprint_ref(a) or ""
        fp_name = footprint_names.get(fp_uuid)
        if fp_name:
            comp["footprint"] = fp_name

        # The schematic's copy of the same component fills gaps the board
        # doc leaves (link, and any identity attr the layout pass never
        # copied); the board's own ATTRs win where both speak.
        sa = sch_attrs.get(refdes, {})
        ident = _component_identity({**sa, **a})
        if ident.get("lcsc"):
            comp["part_lcsc"] = ident["lcsc"]
        label = ident.get("value") or ident.get("mpn") or ident.get("name")
        if label:
            comp["label"] = label
        if ident:
            comp["part_meta"] = {k: v for k, v in ident.items() if k != "lcsc"}

        doc = fp_docs.get(fp_uuid)
        # Distinct pad numbers: several pads may share one (a split thermal
        # pad), and they are one pin. Emitting one row per PAD would give
        # the component three identical pins and three identical
        # connections for the same piece of copper.
        nums = (
            pad_numbers([{"pin": p.get("num")} for p in doc.bodies("PAD")])
            if doc
            else []
        )
        names = pin_maps.get(a.get("Device") or "", {})
        resolved, _ = _dedup_pin_names(nums, names, refdes)
        if resolved:
            comp["pins"] = [{"name": resolved[n], "pad": n} for n in nums]
        else:
            warnings.append(
                f"{refdes}: no pads resolved, so it carries no pins; its "
                f"connections cannot be imported"
            )

        # Pin semantics from the symbol (the chain: the schematic's
        # `Symbol` attr, else the component's `Device` -> the DEVICE
        # meta's Symbol). `Pin Type` -> `etype`, `NO_CONNECT` -> `nc`.
        sym_uuid = sa.get("Symbol")
        if not sym_uuid:
            dev = devices.get(str(a.get("Device") or sa.get("Device") or ""))
            metas = dev.bodies("META") if dev else []
            sym_uuid = (
                str((metas[0].get("attributes") or {}).get("Symbol") or "")
                if metas
                else ""
            )
        sym = symbols.get(str(sym_uuid))
        if sym is not None and comp.get("pins"):
            pinfo = symbol_pin_info(sym)
            by_pad = {v["num"]: v for v in pinfo.values() if v.get("num")}
            nc_pads = nc_by_refdes.get(refdes) or set()
            for pin in comp["pins"]:
                pad = str(pin.get("pad") or "")
                etype = _EASYEDA_ETYPE.get(
                    str((by_pad.get(pad) or {}).get("type") or "").upper()
                )
                if etype:
                    pin["etype"] = etype
                if pad in nc_pads:
                    pin["nc"] = True

        # Files we wrote carry the same semantics as a "Pin Types" JSON
        # ATTR keyed by PAD number (the symbol docs do not round-trip, so
        # a written file's pins are named by pad — a name key would miss
        # them). Pin name is the fallback for a hand-authored file.
        # Symbol-derived values win where both speak — the authored
        # symbol is the source, the JSON the transport.
        raw_pt = a.get("Pin Types") or sa.get("Pin Types")
        if raw_pt and comp.get("pins"):
            pin_types: dict[str, Any] | None = None
            try:
                decoded = json.loads(str(raw_pt))
                if isinstance(decoded, dict):
                    pin_types = decoded
            except json.JSONDecodeError:
                pass
            if pin_types is None:
                warnings.append(
                    f"{refdes}: a 'Pin Types' attribute is present but not "
                    f"JSON ({str(raw_pt)[:60]!r}); pin semantics unread"
                )
            else:
                for pin in comp["pins"]:
                    ent = (
                        pin_types.get(str(pin.get("pad")))
                        or pin_types.get(str(pin.get("name")))
                        or {}
                    )
                    etype = _EASYEDA_ETYPE.get(str(ent.get("etype") or "").upper())
                    etype = etype or (
                        str(ent["etype"]) if ent.get("etype") in PIN_ETYPES else None
                    )
                    if etype and "etype" not in pin:
                        pin["etype"] = etype
                    if ent.get("nc"):
                        pin["nc"] = True
        if b.get("locked"):
            comp["fixed"] = "both"
        out.append(comp)
    return out, {cid: rd for rd, cid in seen.items()}, warnings


def extract_mounting_holes(
    pcb: EproDocument, frame: Frame
) -> tuple[list[dict[str, Any]], list[str]]:
    """Non-plated ``PAD`` records on the PCB document itself ->
    ``mounting_hole`` features (the shape
    ``session.mounting_holes_from_features`` reads).

    These are free pads, belonging to no component: the spike board has
    30, of which 24 are non-plated 6.0 mm holes (the mounting pattern)
    and 6 are plated 10.0 mm holes on ``GND``. Only the non-plated ones
    become features — a plated free pad is real copper on a real net with
    no precis home at all, so it is reported rather than quietly turned
    into a mounting hole, which would drop its net.
    """
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    plated_free = 0
    for b in pcb.bodies("PAD"):
        hole = b.get("hole")
        dia_mil = (hole or {}).get("width") if isinstance(hole, dict) else None
        if b.get("plated"):
            plated_free += 1
            continue
        if not dia_mil:
            warnings.append(
                f"a non-plated free pad at "
                f"({b.get('centerX')}, {b.get('centerY')}) mil has no hole; skipped"
            )
            continue
        x, y = frame.xy(float(b.get("centerX") or 0.0), float(b.get("centerY") or 0.0))
        # No ``fixed`` flag: ``pcb_features.fixed`` is an unconstrained
        # text column that NOTHING reads (the same inertness
        # docs/backlog/pcb-keepout-does-not-bind.md reports for
        # ftype='keepout'), so setting it would store a freeze that binds
        # nothing and reads as if it did. Mounting holes are already
        # immovable in practice — no placer or router touches features.
        # Position on the feature row, drill as ``geom.diameter``: the
        # shape every reader takes (session.mounting_holes_from_features,
        # the gerber drill list, DRC). Until 2026-10-02 this wrote
        # ``geom: {x, y, dia_mm}``, which every reader skipped, so no
        # imported hole reached the drill file, DRC or the router.
        out.append(
            {
                "ftype": "mounting_hole",
                "x": x,
                "y": y,
                "geom": {"diameter": Frame.length(float(dia_mil))},
            }
        )
    if plated_free:
        nets = sorted(
            {str(b.get("netName") or "?") for b in pcb.bodies("PAD") if b.get("plated")}
        )
        warnings.append(
            f"{plated_free} plated free pad(s) on {', '.join(nets)} belong to no "
            f"component; precis has no free-pad model, so they are NOT imported "
            f"— their net loses those connections"
        )
    return out, warnings


def placed_footprint_hole_count(
    components: list[dict[str, Any]], footprints: list[dict[str, Any]]
) -> int:
    """How many footprint holes the placed components carry in total. The
    holes themselves stay on the footprint (footprint-local, mapped to the
    board per instance through :mod:`precis.pcb.pose`), never baked into a
    board-level feature at the part's import pose."""
    holes_by_fp = {f["name"]: len(f.get("holes") or []) for f in footprints}
    return sum(holes_by_fp.get(str(c.get("footprint") or ""), 0) for c in components)


def build_design(
    project: EproProject, pcb: EproDocument | None = None
) -> tuple[Design, Frame]:
    """The whole netlist/placement read, in one call.

    Order matters and is the reason this exists as one function rather
    than a recipe each caller repeats: the outline defines the
    :class:`Frame` every coordinate below is expressed in, footprints
    must be named before components can reference them, and the netlist
    is filtered to live rows before connections are built from it so a
    dropped net cannot leave a dangling connection.
    """
    board = pcb if pcb is not None else project.pcb()
    outline, frame = board_outline(board)
    design = Design()

    footprints, fp_names, fp_warnings = extract_footprints(project, board)
    design.footprints = footprints
    design.warnings += fp_warnings

    components, refdes_by_comp, comp_warnings = extract_components(
        project, board, frame, fp_names
    )
    design.components = components
    design.warnings += comp_warnings

    nets, net_warnings = live_nets(board)
    design.nets = [{"name": n} for n in nets]
    design.warnings += net_warnings

    # The design-rule table (was dropped pre-2026-10-14): mapped categories
    # become net classes so re-routing uses the board's OWN track width /
    # clearance / via size instead of the fab floor; the whole table is
    # preserved verbatim for export. With no RULE_SELECTOR records (TAZ
    # V1.15 carries none) EasyEDA's semantic is "every net uses the
    # DEFAULT-named rule" — mirrored by stamping that class on every net.
    rules = extract_rules(board)
    design.net_classes = rules.classes
    design.epro_rules = rules.records
    design.epro_board = extract_board_state(board)
    design.epro_pours, pour_warnings = extract_pours(board, frame)
    design.warnings += rules.warnings + pour_warnings
    if rules.default:
        for n in design.nets:
            n["net_class"] = rules.default

    pad_nets, pad_warnings = live_pad_nets(board)
    design.warnings += pad_warnings

    # (component id, pad number) -> (refdes, pin name). A connection names
    # a PIN, so it needs the same pad->pin resolution the components got;
    # re-deriving it here from the emitted pins keeps the two in step
    # instead of resolving the chain twice and hoping they agree.
    emitted = {c["refdes"]: c for c in components}
    pin_by_comp: dict[str, dict[str, str]] = {
        comp_id: {
            str(p["pad"]): str(p["name"]) for p in emitted[refdes].get("pins", [])
        }
        for comp_id, refdes in refdes_by_comp.items()
    }

    live_net_names = set(nets)
    dropped_conn = 0
    for (comp_id, pad_num), net_name in sorted(pad_nets.items()):
        refdes = refdes_by_comp.get(comp_id)
        pin = pin_by_comp.get(comp_id, {}).get(pad_num)
        if refdes is None or pin is None or net_name not in live_net_names:
            dropped_conn += 1
            continue
        design.connections.append({"net": net_name, "refdes": refdes, "pin": pin})
    if dropped_conn:
        design.warnings.append(
            f"{dropped_conn} live pad-net row(s) name a component, pad or net "
            f"that did not survive the import; those connections are dropped"
        )

    holes, hole_warnings = extract_mounting_holes(board, frame)
    design.warnings += hole_warnings
    design.features = [
        {"ftype": "outline", "geom": {"path": [[x, y] for x, y in outline]}},
        *holes,
    ]

    design.stats = {
        "components": len(design.components),
        "footprints": len(design.footprints),
        "nets": len(design.nets),
        "connections": len(design.connections),
        "mounting_holes": len(holes)
        + placed_footprint_hole_count(components, footprints),
        "outline_vertices": len(outline),
        "rule_records": len(rules.records),
        "rule_classes": len(rules.classes),
    }
    return design, frame
