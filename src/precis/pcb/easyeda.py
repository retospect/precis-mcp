"""EasyEDA footprint fetch + parse (pcb-guided-place-route Slice 2).

Two halves, kept apart on purpose: :func:`parse_component` is pure (no
network, every branch unit-testable against a fixture doc) and
:func:`fetch_component` is a thin wrapper that gets bytes off the wire and
hands them to the parser. Neither converts to `.kicad_mod` — slice 4's
exporter emits KiCad footprints straight from the canonical
pads/pin_map/courtyard this module produces, so there is no intermediary
format to maintain.

``easyeda2kicad`` is the reference implementation this was cribbed from —
**not a dependency**. We need only the pad subset of its primitive decoder.

Format facts below are spike-verified (2026-08-27, C42163081; re-spiked
2026-09-02, C2765186) against real components. Do not "fix" them without
re-spiking:

* The footprint lives at ``result.packageDetail.dataStr.shape``
  (``docType: 4``). Plain ``result.dataStr`` is the *schematic symbol*
  (``docType: 2``) — a different primitive alphabet. This is the single
  most costly mix-up here; :func:`parse_component` asserts on docType and
  names the trap in the error.
* ``docType`` itself moved between the two spikes: C42163081 (2026-08-27)
  carried it as a top-level int on ``dataStr``; C2765186 (2026-09-02,
  editorVersion 6.5.57) carries no top-level ``docType`` at all — it's
  nested as the STRING ``"4"`` at ``dataStr.head.docType`` instead.
  :func:`parse_component` checks both locations and coerces a numeric
  string before comparing.
* Primitives are flat, ``~``-delimited strings: ``PAD~RECT~x~y~w~h~
  layer~net~number~...``, ``TRACK~width~layer~net~points~...``. Unknown
  primitive types are skipped, not fatal — EasyEDA's alphabet is larger
  than the pad subset we parse.
* Units are 10 mil (multiply by 0.254 for mm). Coordinates are relative to
  the package document's ``head.x``/``head.y`` origin.
* EasyEDA's Y axis grows downward; we flip it so output mm coordinates are
  a conventional right-handed PCB frame (+Y up).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from precis.pcb import MAX_FOOTPRINT_GRAPHICS, MAX_GRAPHIC_POINTS
from precis.pcb import rows as pcb_rows
from precis.pcb._http import EASYEDA_POLICY, with_backoff
from precis.utils.coerce import float_or_zero

if TYPE_CHECKING:
    import httpx

#: 1 EasyEDA unit = 10 mil = 0.254 mm.
_MM_PER_UNIT = 0.254

_API_BASE = "https://easyeda.com/api/products"

# CloudFront fronts easyeda.com and 403s a bare/bot-shaped request. The
# browser-shaped header set (easyeda2kicad's) cleared it (Referer was the
# load-bearing one, spike 2026-08-27, C42163081), but it spoofs a browser,
# so it is a workaround gated behind ``pcb.easyeda_browser_headers``
# (default off). The default is the honest precis User-Agent.
_BROWSER_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://easyeda.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _request_headers() -> dict[str, str]:
    """Headers for one EasyEDA request: honest by default, browser-shaped
    only when ``pcb.easyeda_browser_headers`` is on."""
    from precis import settings
    from precis.utils.http import DEFAULT_USER_AGENT

    if settings.get_bool("pcb.easyeda_browser_headers", default=False):
        return dict(_BROWSER_HEADERS)
    return {
        "Accept": _BROWSER_HEADERS["Accept"],
        "User-Agent": DEFAULT_USER_AGENT,
    }


# ── fetch ───────────────────────────────────────────────────────────────
def fetch_component(
    lcsc: str, *, client: httpx.Client | None = None
) -> dict[str, Any] | None:
    """GET the raw EasyEDA component JSON for a C-number, or None.

    None covers the two "this part has no EasyEDA component" shapes — an
    HTTP 404, and a 200 carrying ``{"success": false}`` — both normal
    answers, not errors. Real failures (403, retries exhausted, breaker
    open) raise via :func:`precis.pcb._http.with_backoff`.

    ``client`` lets a caller reuse one connection across many fetches (a
    bulk warm) and lets tests inject a fake — by default one is opened
    and closed per call via :func:`precis.utils.http.http_client`.
    """
    lcsc = lcsc.strip().upper()
    url = f"{_API_BASE}/{lcsc}/components"

    # Local imports: lets tests monkeypatch `precis.utils.safe_fetch.safe_get`
    # (the repo's standard seam, see tests/test_news.py) without needing a
    # real pinned httpx.Client.
    from precis.utils.safe_fetch import safe_get

    # Headers go on the REQUEST, not just the client: an injected ``client``
    # (bulk warm, or a test double) would otherwise arrive without them.
    headers = _request_headers()
    if client is not None:
        resp = with_backoff(
            lambda: safe_get(client, url, headers=headers),
            service="easyeda",
            policy=EASYEDA_POLICY,
        )
    else:
        from precis.utils.http import http_client

        with http_client(timeout=20.0) as opened:
            resp = with_backoff(
                lambda: safe_get(opened, url, headers=headers),
                service="easyeda",
                policy=EASYEDA_POLICY,
            )

    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    doc = resp.json()
    if not isinstance(doc, dict) or not doc.get("success"):
        return None
    return doc


# ── parse (pure) ────────────────────────────────────────────────────────
def parse_component(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a fetched EasyEDA component JSON into the canonical footprint
    dict ``store.part_footprint_put`` expects — ``{pads, pin_map,
    courtyard, centroid, graphics, source, raw}`` plus
    ``graphics_dropped``, the decode's per-class drop warnings the store
    ignores and the import surface reports. Returns None when the doc
    has no footprint (a symbol-only part, or an unrecognized shape).

    Raises ``ValueError`` if ``result.packageDetail.dataStr`` does not
    resolve to ``docType: 4`` — either because it resolves to something
    else (usually the caller handed in the *schematic* document,
    ``result.dataStr``, ``docType: 2``, by mistake) or because no
    ``docType`` can be found at all (checked at both the old top-level
    location and the current ``head.docType`` one — see the module
    docstring). The two cases get distinct, honest error text.
    """
    result = doc.get("result")
    if not isinstance(result, dict):
        return None
    package = result.get("packageDetail")
    if not isinstance(package, dict):
        return None  # part has a symbol but no linked footprint
    data = package.get("dataStr")
    if not isinstance(data, dict):
        return None

    doc_type_present, doc_type = _resolve_doc_type(data)
    if not doc_type_present:
        raise ValueError(
            "parse_component found no docType on packageDetail.dataStr — "
            "checked the top level (old API shape) and head.docType "
            "(current API shape, see precis.pcb.easyeda's module "
            "docstring). Cannot tell whether this is the FOOTPRINT "
            "document."
        )
    if doc_type != 4:
        raise ValueError(
            f"parse_component expected packageDetail.dataStr docType=4 "
            f"(the FOOTPRINT), got docType={doc_type!r}. This usually means "
            "result.dataStr (the SCHEMATIC SYMBOL, docType=2) was passed "
            "instead of result.packageDetail.dataStr — they are different "
            "primitive alphabets, see precis.pcb.easyeda's module docstring."
        )

    shape = data.get("shape")
    if not isinstance(shape, list) or not shape:
        return None

    head = data.get("head") or {}
    origin_x = _to_mm(float_or_zero(head.get("x")))
    origin_y = _to_mm(float_or_zero(head.get("y")))

    pads: list[dict[str, Any]] = []
    outline: list[tuple[float, float]] = []
    graphics, dropped = _footprint_graphics(shape, origin_x, origin_y)
    # The decode keeps only well-formed primitives, but a value can still
    # be formally off (a negative width, a non-finite point) — the row
    # validator drops those too rather than storing read-back garbage.
    graphics = pcb_rows.normalize_footprint_graphics(
        f"easyeda:{package.get('uuid') or '?'}", graphics, dropped=dropped
    )
    for prim in shape:
        if not isinstance(prim, str) or not prim:
            continue
        fields = prim.split("~")
        kind = fields[0]
        if kind == "PAD":
            pad = _parse_pad(fields, origin_x, origin_y)
            if pad is not None:
                pads.append(pad)
        elif kind == "TRACK":
            outline.extend(_parse_track(fields, origin_x, origin_y))
        # else: CIRCLE/ARC/VIA/SOLIDREGION/... — not needed for pads/
        # courtyard; the decodable ones land in ``graphics``.

    if not pads:
        return None

    # The footprint doc only knows pad NUMBERS. Functional pin names
    # (HVOUT41, DIN, VPP, ...) live in the schematic SYMBOL — the other
    # document the module docstring warns not to confuse with this one.
    # Read deliberately here, for names only (never geometry): a design
    # authored with datasheet pin names (the ``sink_grid`` generator's
    # ``channel_pins``, gr341532) can only land on real pads if
    # ``pin_map[number].name`` carries the symbol's name, because
    # ``session._real_pin_offsets`` keys the real offsets by that name.
    # A part whose symbol has no pin names (or no symbol at all) keeps
    # the number as its name, exactly as before.
    names = _symbol_pin_names(result)
    pin_map = {
        pad["number"]: {"name": names.get(pad["number"], pad["number"]), "tags": []}
        for pad in pads
    }

    uuid = package.get("uuid") or data.get("uuid")
    source = "easyeda:packageDetail" + (f":{uuid}" if uuid else "")

    return {
        "pads": pads,
        "pin_map": pin_map,
        "courtyard": _courtyard(pads, outline),
        "centroid": _centroid(pads),
        "graphics": graphics,
        "graphics_dropped": dropped,
        "source": source,
        "raw": doc,
    }


def footprint_graphics(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(graphics, dropped_classes)`` off a raw EasyEDA component JSON —
    the same ``dataStr.shape`` primitives :func:`parse_component` keeps,
    for a caller holding only the cached ``raw`` (a ``part_footprints``
    row written before this decode existed)."""
    result = doc.get("result") if isinstance(doc, dict) else None
    package = result.get("packageDetail") if isinstance(result, dict) else None
    data = package.get("dataStr") if isinstance(package, dict) else None
    if not isinstance(data, dict):
        return [], []
    shape = data.get("shape")
    if not isinstance(shape, list):
        return [], []
    head = data.get("head") or {}
    graphics, dropped = _footprint_graphics(
        shape,
        _to_mm(float_or_zero(head.get("x"))),
        _to_mm(float_or_zero(head.get("y"))),
    )
    return (
        pcb_rows.normalize_footprint_graphics(
            f"easyeda:{(package or {}).get('uuid') or '?'}",
            graphics,
            dropped=dropped,
        ),
        dropped,
    )


def _symbol_pin_names(result: dict[str, Any]) -> dict[str, str]:
    """``{pin_number: pin_name}`` from the schematic SYMBOL document
    (``result.dataStr``, docType 2), names only — geometry is never read
    from it. A symbol pin primitive is ``P~display~electric~NUMBER~x~y~
    rot~id~locked^^<dot>^^<path>^^1~x~y~rot~NAME~start~...^^1~x~y~rot~
    NUMBER~end~...^^...`` — but the ``start``/``end`` anchors follow the
    pin's ROTATION, not its role: a pin drawn on the symbol's other side
    (rot 0 vs 180) carries the NUMBER in the ``start`` segment and the
    NAME in ``end`` (C639448 pins 41..80 on prod, 2026-09-18: keying on
    ``start`` alone named pads 1..40 only). So the name is whichever text
    segment's label is NOT the pin number. Anything that does not parse
    is skipped, never fatal; a name equal to the number (or empty) is
    dropped so the caller's number fallback stays the single default.
    """
    data = result.get("dataStr")
    if not isinstance(data, dict):
        return {}
    shape = data.get("shape")
    if not isinstance(shape, list):
        return {}
    names: dict[str, str] = {}
    for prim in shape:
        if not isinstance(prim, str) or not prim.startswith("P~"):
            continue
        segments = prim.split("^^")
        head = segments[0].split("~")
        if len(head) < 4:
            continue
        number = head[3].strip()
        name = ""
        for seg in segments[1:]:
            fields = seg.split("~")
            if len(fields) < 6 or fields[5] not in ("start", "end"):
                continue
            label = fields[4].strip()
            if label and label != number:
                name = label
                break
        if number and name:
            names[number] = name
    return names


def _resolve_doc_type(data: dict[str, Any]) -> tuple[bool, Any]:
    """Find ``docType`` on a ``packageDetail.dataStr`` doc, old-shape or
    new-shape, and report whether it was found at all.

    C42163081 (spike-verified 2026-08-27) carried a top-level int
    ``docType``. C2765186 (spike-verified 2026-09-02, editorVersion
    6.5.57) carries none — it's nested as the STRING ``"4"`` under
    ``head.docType`` instead. Check both; coerce a numeric string so the
    caller's ``!= 4`` comparison doesn't silently fail on the new shape.
    Returns ``(False, None)`` only when neither location has anything.
    """
    if "docType" in data:
        raw: Any = data["docType"]
    else:
        head = data.get("head")
        raw = head.get("docType") if isinstance(head, dict) else None
        if raw is None:
            return False, None
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return True, int(raw)
    return True, raw


def _parse_pad(
    fields: list[str], origin_x: float, origin_y: float
) -> dict[str, Any] | None:
    """Decode one ``PAD~...`` primitive.

    Field order (easyeda2kicad's alphabet, spike-verified against
    C42163081): ``PAD~shape~x~y~w~h~layer~net~number~hole_radius~points~
    rotation~id~hole_length~...``. Fields past ``hole_length`` (plated/
    locked flags) are ignored — we don't need them. Malformed/short
    primitives are skipped, not fatal (defensive parsing per the caller).
    """
    if len(fields) < 9:
        return None
    try:
        shape = fields[1]
        x = _to_mm(float_or_zero(fields[2])) - origin_x
        y = -(_to_mm(float_or_zero(fields[3])) - origin_y)  # Y grows down in EasyEDA
        w = _to_mm(float_or_zero(fields[4]))
        h = _to_mm(float_or_zero(fields[5]))
        layer_id = int(fields[6]) if fields[6] else 1
        number = fields[8]
    except (ValueError, IndexError):
        return None

    hole_radius = float_or_zero(fields[9]) if len(fields) > 9 and fields[9] else 0.0
    rotation = float_or_zero(fields[11]) if len(fields) > 11 and fields[11] else 0.0
    drill = round(_to_mm(hole_radius) * 2, 4) if hole_radius > 0 else None

    pad: dict[str, Any] = {
        "number": number,
        "shape": shape,
        "x": round(x, 4),
        "y": round(y, 4),
        "w": round(w, 4),
        "h": round(h, 4),
        "rot": rotation,
        # layer id 1 = top, 2 = bottom; THT pads carry a multi-layer id
        # (e.g. 11) and default to F.Cu — the drill makes them span both.
        "layer": "B.Cu" if layer_id == 2 else "F.Cu",
        "drill": drill,
    }
    if shape == "POLYGON" and len(fields) > 10 and fields[10]:
        # field 10 ("points") is the same flat "x1 y1 x2 y2 ..." alphabet
        # TRACK's own points field uses (:func:`_parse_track`) — a
        # free-form pad's outline, in the SAME absolute-then-origin-
        # shifted mm frame as the pad's own x/y above (pcb-ewod-multitile
        # Slice 1: previously dropped entirely, so a real EasyEDA polygon
        # pad down-approximated to its w/h RECT bound with no way to tell
        # the difference between a bound and the fabricated shape).
        poly = _parse_points_field(fields[10], origin_x, origin_y)
        if len(poly) >= 3:
            pad["poly"] = [[round(px, 4), round(py, 4)] for px, py in poly]
    return pad


def _parse_points_field(
    raw: str, origin_x: float, origin_y: float
) -> list[tuple[float, float]]:
    """Decode a flat ``"x1 y1 x2 y2 ..."`` EasyEDA points field into
    origin-shifted mm pairs — the shared parser :func:`_parse_track` and
    :func:`_parse_pad` (a ``POLYGON`` pad's outline) both need."""
    parts = raw.split()
    pts: list[tuple[float, float]] = []
    for i in range(0, len(parts) - 1, 2):
        try:
            x = _to_mm(float_or_zero(parts[i])) - origin_x
            y = -(_to_mm(float_or_zero(parts[i + 1])) - origin_y)
        except ValueError:
            continue
        pts.append((x, y))
    return pts


def _parse_track(
    fields: list[str], origin_x: float, origin_y: float
) -> list[tuple[float, float]]:
    """Decode one ``TRACK~...`` primitive's polyline into mm points, for the
    cheap courtyard-outline hint. ``points`` (field 4) is a space-separated
    flat ``x1 y1 x2 y2 ...`` list."""
    if len(fields) < 5 or not fields[4]:
        return []
    return _parse_points_field(fields[4], origin_x, origin_y)


def _courtyard(
    pads: list[dict[str, Any]], outline: list[tuple[float, float]]
) -> dict[str, Any]:
    """Bbox from pad extents, widened by any silk/assembly outline points
    cheaply recovered from TRACK primitives (a hint, not authoritative)."""
    xs: list[float] = []
    ys: list[float] = []
    for pad in pads:
        if pad.get("poly"):
            # A POLYGON pad's own vertex ring is the real extent — its
            # w/h are whatever bbox EasyEDA happened to also carry, not
            # necessarily this pad's true bound (an oblong/zigzag outline
            # can run past a stale or zero w/h).
            xs += [p[0] for p in pad["poly"]]
            ys += [p[1] for p in pad["poly"]]
        else:
            xs += [pad["x"] - pad["w"] / 2, pad["x"] + pad["w"] / 2]
            ys += [pad["y"] - pad["h"] / 2, pad["y"] + pad["h"] / 2]
    for x, y in outline:
        xs.append(x)
        ys.append(y)
    return {
        "bbox": [
            round(min(xs), 4),
            round(min(ys), 4),
            round(max(xs), 4),
            round(max(ys), 4),
        ]
    }


def _centroid(pads: list[dict[str, Any]]) -> dict[str, float]:
    """Pick-place point — derived, NOT vendor-supplied: EasyEDA's footprint
    doc carries no authored centroid, so this is the pad bounding-box
    midpoint, an honest stand-in rather than a true assembly centroid."""
    xs = [pad["x"] for pad in pads]
    ys = [pad["y"] for pad in pads]
    return {
        "x": round((min(xs) + max(xs)) / 2, 4),
        "y": round((min(ys) + max(ys)) / 2, 4),
    }


def _to_mm(units_10mil: float) -> float:
    """EasyEDA's native unit is 10 mil (0.01 inch) -> mm."""
    return units_10mil * _MM_PER_UNIT


# ── footprint graphics (gr520150) ───────────────────────────────────────

#: Standard-format layer ids the spikes verified (``TRACK`` layer 3 = top
#: silk; 11 = the multi layer, mapped to Edge.Cuts like Pro's layer 12).
#: Anything else emits ``layer-<n>`` — a name is never invented for an
#: unobserved id.
_GRAPHIC_LAYER_NAMES = {
    1: "F.Cu",
    2: "B.Cu",
    3: "F.SilkS",
    4: "B.SilkS",
    11: "Edge.Cuts",
}

#: The primitive letters an SVG path this decoder reads: absolute M/L/Z
#: for a region, plus A for the arc form — anything else rejects the
#: path rather than misparsing it.
_SVG_TOKEN = re.compile(r"[MLAZ]|-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _graphic_layer(fields: list[str], index: int) -> str | None:
    try:
        return _GRAPHIC_LAYER_NAMES.get(int(fields[index]), f"layer-{fields[index]}")
    except (IndexError, ValueError):
        return None


def _svg_tokens(path: str) -> list[str] | None:
    """Tokenise an SVG path string, or ``None`` when it carries anything
    this decoder does not handle (a relative or curve command — better
    dropped loudly than misparsed)."""
    cleaned = path.replace(",", " ")
    tokens = _SVG_TOKEN.findall(cleaned)
    if not tokens or _SVG_TOKEN.sub("", cleaned).strip():
        return None
    return tokens


def _svg_polyline(
    path: str, origin_x: float, origin_y: float
) -> list[list[float]] | None:
    """An absolute ``M x y L x y ... [Z]`` path -> origin-shifted mm
    points (the Y flip applied, like the pads)."""
    tokens = _svg_tokens(path)
    if tokens is None or "A" in tokens:
        return None
    pts: list[list[float]] = []
    i = 0
    closed = False
    while i < len(tokens):
        t = tokens[i]
        if t == "Z":
            closed = True
            i += 1
            continue
        if t not in ("M", "L") or i + 2 >= len(tokens):
            return None
        try:
            x, y = float(tokens[i + 1]), float(tokens[i + 2])
        except ValueError:
            # A letter pair the number regex let through ("M L 1 2" —
            # float("L")) is a malformed path, not a fatal one.
            return None
        pts.append(
            [
                round(_to_mm(x) - origin_x, 4),
                round(-(_to_mm(y) - origin_y), 4),
            ]
        )
        i += 3
    if closed and pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts if len(pts) >= 2 else None


def _decode_graphic(
    fields: list[str], kind: str, origin_x: float, origin_y: float
) -> dict[str, Any] | None:
    """One ``shape`` primitive (already ``~``-split) -> a canonical
    ``graphics[]`` dict, or ``None`` when it is not decodable. Conversion
    slips propagate — the caller catches them and counts the primitive as
    dropped, so one malformed primitive never aborts the footprint."""
    item: dict[str, Any] | None = None
    if kind == "TRACK" and len(fields) >= 5:
        pts = _parse_points_field(fields[4], origin_x, origin_y)
        layer = _graphic_layer(fields, 2)
        if layer is not None and len(pts) >= 2:
            points = [[round(px, 4), round(py, 4)] for px, py in pts]
            closed = len(points) >= 4 and points[0] == points[-1]
            item = {
                "kind": "polygon" if closed else "polyline",
                "layer": layer,
                "width_mm": round(_to_mm(float_or_zero(fields[1])), 4),
                "points": points,
            }
    elif kind == "SOLIDREGION" and len(fields) >= 6:
        rpts = _svg_polyline(fields[3], origin_x, origin_y)
        layer = _graphic_layer(fields, 1)
        if layer is not None and rpts is not None and len(rpts) >= 3:
            item = {"kind": "region", "layer": layer, "points": rpts}
            if fields[4].strip().lower() in ("keepout", "prohibit", "prohibited"):
                item["keepout"] = True
    elif kind == "TEXT" and len(fields) >= 13 and fields[1] in ("P", "N"):
        # ``TEXT~P|N~x~y~width~rotation~mirror~layer~net~fontSize~
        # font~text~id~locked`` — positionally decoded, validated.
        try:
            x = _to_mm(float_or_zero(fields[2])) - origin_x
            y = -(_to_mm(float_or_zero(fields[3])) - origin_y)
            layer = _graphic_layer(fields, 7)
            font = _to_mm(float_or_zero(fields[9]))
            rot = float_or_zero(fields[5])
            text = fields[11].strip()
        except (IndexError, ValueError):
            x, layer, font, text = 0.0, None, 0.0, ""
        if layer is not None and text and font > 0:
            item = {
                "kind": "text",
                "layer": layer,
                "x": round(x, 4),
                "y": round(y, 4),
                "text": text,
                "height_mm": round(font, 4),
            }
            if rot:
                # CCW-positive in EasyEDA's Y-down frame reads as
                # CW-positive after the flip — precis' own convention.
                item["rot_deg"] = rot
            if fields[6].strip().lower() in ("1", "true", "yes"):
                item["mirror"] = True
    elif kind == "ARC" and len(fields) >= 5:
        # ``ARC~w~layer~net~path~id~locked``; path is SVG
        # ``M sx sy A rx ry rot laf sf ex ey``.
        tokens = _svg_tokens(fields[4])
        layer = _graphic_layer(fields, 2)
        if (
            layer is not None
            and tokens is not None
            and len(tokens) == 11
            and tokens[0] == "M"
            and tokens[3] == "A"
        ):
            try:
                sx, sy = float(tokens[1]), float(tokens[2])
                rx, laf, sf = float(tokens[4]), float(tokens[7]), float(tokens[8])
                ex, ey = float(tokens[9]), float(tokens[10])
            except ValueError:
                tokens = None
            if tokens is not None and rx > 0:
                item = {
                    "kind": "arc",
                    "layer": layer,
                    "start": [
                        round(_to_mm(sx) - origin_x, 4),
                        round(-(_to_mm(sy) - origin_y), 4),
                    ],
                    "end": [
                        round(_to_mm(ex) - origin_x, 4),
                        round(-(_to_mm(ey) - origin_y), 4),
                    ],
                    "radius_mm": round(_to_mm(rx), 4),
                    "large_arc": bool(laf),
                    # SVG sweep=1 is clockwise on a Y-down canvas; the
                    # flip inverts it in the Y-up frame.
                    "cw": sf == 0,
                    "width_mm": round(_to_mm(float_or_zero(fields[1])), 4),
                }
    elif kind == "CIRCLE" and len(fields) >= 6:
        # ``CIRCLE~cx~cy~r~w~layer~id``
        try:
            cx, cy = float_or_zero(fields[1]), float_or_zero(fields[2])
            r = _to_mm(float_or_zero(fields[3]))
            layer = _graphic_layer(fields, 5)
        except (IndexError, ValueError):
            r, layer = 0.0, None
        if layer is not None and r > 0:
            item = {
                "kind": "circle",
                "layer": layer,
                "center": [
                    round(_to_mm(cx) - origin_x, 4),
                    round(-(_to_mm(cy) - origin_y), 4),
                ],
                "radius_mm": round(r, 4),
                "width_mm": round(_to_mm(float_or_zero(fields[4])), 4),
            }
    return item


def _footprint_graphics(
    shape: list[Any], origin_x: float, origin_y: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode ``dataStr.shape`` primitives into canonical ``graphics[]``
    dicts (footprint-local mm, Y up, origin-shifted like the pads).

    Returns ``(graphics, warnings)``: every primitive that is not a PAD
    (decoded as pads) either lands as a graphic or is counted — one
    warning per undecoded primitive class, never a silent drop. A
    primitive that CRASHES its decoder (a malformed path, a field that
    is not a number) is counted the same way — vendor data degrades,
    it never aborts the footprint."""
    out: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    over_points = 0
    over_cap = 0
    for prim in shape:
        if not isinstance(prim, str) or not prim:
            continue
        fields = prim.split("~")
        kind = fields[0]
        if kind == "PAD":
            continue
        try:
            item = _decode_graphic(fields, kind, origin_x, origin_y)
        except (ValueError, TypeError, IndexError):
            item = None
        if item is None:
            dropped[kind] = dropped.get(kind, 0) + 1
            continue
        pts = item.get("points")
        if isinstance(pts, list) and len(pts) > MAX_GRAPHIC_POINTS:
            over_points += 1
        elif len(out) >= MAX_FOOTPRINT_GRAPHICS:
            over_cap += 1
        else:
            out.append(item)
    warnings = [
        f"{count} {kind} primitive(s) precis does not decode — NOT "
        "imported as footprint graphics"
        for kind, count in sorted(dropped.items())
    ]
    if over_points:
        warnings.append(
            f"{over_points} graphic(s) over the {MAX_GRAPHIC_POINTS}-point "
            "cap — NOT imported as footprint graphics"
        )
    if over_cap:
        warnings.append(
            f"{over_cap} graphic(s) over the {MAX_FOOTPRINT_GRAPHICS}-per-"
            "footprint cap — NOT imported"
        )
    return out, warnings


__all__ = ["fetch_component", "footprint_graphics", "parse_component"]
