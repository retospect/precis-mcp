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

Format facts below are spike-verified (2026-08-27, C42163081) against a
real component. Do not "fix" them without re-spiking:

* The footprint lives at ``result.packageDetail.dataStr.shape``
  (``docType: 4``). Plain ``result.dataStr`` is the *schematic symbol*
  (``docType: 2``) — a different primitive alphabet. This is the single
  most costly mix-up here; :func:`parse_component` asserts on docType and
  names the trap in the error.
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

from typing import TYPE_CHECKING, Any

from precis.pcb._http import with_backoff

if TYPE_CHECKING:
    import httpx

#: 1 EasyEDA unit = 10 mil = 0.254 mm.
_MM_PER_UNIT = 0.254

_API_BASE = "https://easyeda.com/api/products"

# CloudFront fronts easyeda.com and 403s a bare/bot-shaped request. This
# header set (easyeda2kicad's) clears it; Referer is the *load-bearing*
# one (spike-verified 2026-08-27, C42163081) — a request missing it 403s
# even with a normal User-Agent, so don't "clean it up" as dead weight.
_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://easyeda.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
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
    # (bulk warm, or a test double) would otherwise arrive without the
    # load-bearing Referer and eat a CloudFront 403 that looks like a bug in
    # the parser rather than a missing header.
    if client is not None:
        resp = with_backoff(
            lambda: safe_get(client, url, headers=_HEADERS), service="easyeda"
        )
    else:
        from precis.utils.http import http_client

        with http_client(timeout=20.0) as opened:
            resp = with_backoff(
                lambda: safe_get(opened, url, headers=_HEADERS), service="easyeda"
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
    dict ``store.part_footprint_put`` expects: ``{pads, pin_map, courtyard,
    centroid, source, raw}``. Returns None when the doc has no footprint
    (a symbol-only part, or an unrecognized shape).

    Raises ``ValueError`` if ``result.packageDetail.dataStr`` is not
    ``docType: 4`` — that means the caller handed in the *schematic*
    document (``result.dataStr``, ``docType: 2``) by mistake; see the
    module docstring for why that mix-up is easy and costly.
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

    doc_type = data.get("docType")
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
    origin_x = _to_mm(_num(head.get("x")))
    origin_y = _to_mm(_num(head.get("y")))

    pads: list[dict[str, Any]] = []
    outline: list[tuple[float, float]] = []
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
        # courtyard, skipped rather than treated as an error.

    if not pads:
        return None

    pin_map = {pad["number"]: {"name": pad["number"], "tags": []} for pad in pads}
    # No schematic cross-reference happens here (see module docstring on
    # why we deliberately never touch result.dataStr) — the footprint doc
    # only knows pad *numbers*, not functional pin names, so name defaults
    # to the pad's own number until a schematic-aware pass improves it.

    uuid = package.get("uuid") or data.get("uuid")
    source = "easyeda:packageDetail" + (f":{uuid}" if uuid else "")

    return {
        "pads": pads,
        "pin_map": pin_map,
        "courtyard": _courtyard(pads, outline),
        "centroid": _centroid(pads),
        "source": source,
        "raw": doc,
    }


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
        x = _to_mm(_num(fields[2])) - origin_x
        y = -(_to_mm(_num(fields[3])) - origin_y)  # Y grows down in EasyEDA
        w = _to_mm(_num(fields[4]))
        h = _to_mm(_num(fields[5]))
        layer_id = int(fields[6]) if fields[6] else 1
        number = fields[8]
    except (ValueError, IndexError):
        return None

    hole_radius = _num(fields[9]) if len(fields) > 9 and fields[9] else 0.0
    rotation = _num(fields[11]) if len(fields) > 11 and fields[11] else 0.0
    drill = round(_to_mm(hole_radius) * 2, 4) if hole_radius > 0 else None

    return {
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


def _parse_track(
    fields: list[str], origin_x: float, origin_y: float
) -> list[tuple[float, float]]:
    """Decode one ``TRACK~...`` primitive's polyline into mm points, for the
    cheap courtyard-outline hint. ``points`` (field 4) is a space-separated
    flat ``x1 y1 x2 y2 ...`` list."""
    if len(fields) < 5 or not fields[4]:
        return []
    raw = fields[4].split()
    pts: list[tuple[float, float]] = []
    for i in range(0, len(raw) - 1, 2):
        try:
            x = _to_mm(_num(raw[i])) - origin_x
            y = -(_to_mm(_num(raw[i + 1])) - origin_y)
        except ValueError:
            continue
        pts.append((x, y))
    return pts


def _courtyard(
    pads: list[dict[str, Any]], outline: list[tuple[float, float]]
) -> dict[str, Any]:
    """Bbox from pad extents, widened by any silk/assembly outline points
    cheaply recovered from TRACK primitives (a hint, not authoritative)."""
    xs: list[float] = []
    ys: list[float] = []
    for pad in pads:
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


def _num(raw: Any) -> float:
    """Best-effort float — EasyEDA fields are sometimes empty strings."""
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["fetch_component", "parse_component"]
