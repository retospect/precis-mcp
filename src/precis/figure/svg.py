"""Pure SVG helpers for the ``figure`` kind — no DB, no network, no model.

Three jobs, each a mechanical fact about a string of SVG:

- :func:`sanitize_svg` — strip the XSS/SSRF surface so model-authored markup
  is safe to **inline into the page DOM** (the canvas renders the SVG inline,
  not sandboxed in an ``<img>``, so declarative SMIL/CSS animation plays). It
  removes ``<script>`` / ``<foreignObject>``, ``on*`` event handlers, external
  ``href`` / ``xlink:href``; neutralises ``@import`` and external ``url(...)``
  in ``<style>`` bodies and ``style=`` attributes; and drops SMIL animation
  elements that target an ``on*`` / ``href`` attribute (a runtime re-injection
  of the attributes stripped at rest). This is the seam — the trust boundary
  — that makes model markup safe to inline.
- :func:`parse_error` — the *compile* check: does it parse as XML at all?
  ``None`` when clean, else a one-line reason. This is one of the two
  auto-lints fed back into the turn loop.
- :func:`lint_svg` — the compile check plus the *out-of-bounds* lint:
  every measurable shape whose bbox spills past the ``viewBox`` is a
  finding. These are the only two mechanically-detectable lints (ADR:
  conventions are the model's job via the vocab, not a checker).

Everything is namespace-agnostic — SVG may be authored bare (no
``xmlns``, tags like ``rect``) or fully namespaced
(``{http://www.w3.org/2000/svg}rect``); we match on the local tag name.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

#: Default canvas box ``(min-x, min-y, width, height)`` — a fixed coordinate
#: frame is the *shared spatial language* between you and the model (the
#: ruler/scale overlay visualises exactly this).
DEFAULT_VIEWBOX: tuple[float, float, float, float] = (0.0, 0.0, 256.0, 256.0)

#: Elements removed wholesale by :func:`sanitize_svg` (active content).
_DANGEROUS_TAGS = frozenset({"script", "foreignObject", "foreignobject"})

#: SMIL animation elements. Allowed (declarative, browser-native — see the
#: ``precis-figure-animate`` skill), but dropped when their ``attributeName``
#: targets an ``on*`` handler or ``href`` — animating those would re-introduce
#: at *runtime* exactly the attributes we strip at rest (a live-DOM XSS vector
#: once the SVG is inlined rather than sandboxed in an ``<img>``).
_ANIMATION_TAGS = frozenset(
    {"animate", "animatetransform", "animatemotion", "animatecolor", "set"}
)

#: CSS ``@import`` at-rule (external fetch) — stripped from ``<style>`` bodies.
_CSS_AT_IMPORT_RE = re.compile(r"@import\b[^;]*;?", re.IGNORECASE)

#: A ``url(...)`` reference in CSS. Local ``url(#id)`` fragment refs are kept;
#: any external / ``data:`` target is neutralised to ``url(#)`` so an inlined
#: ``<style>`` or ``style=`` can't fetch off-origin.
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^)]*?)\1\s*\)""", re.IGNORECASE)

#: Shapes whose bounding box :func:`lint_svg` can compute cheaply. Paths,
#: text and groups are not bounds-checked in slice 1 (documented limitation).
_BBOX_SHAPES = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon"})


class SvgError(ValueError):
    """Raised when a string cannot be parsed as SVG/XML."""


@dataclass(frozen=True, slots=True)
class LintFinding:
    """One mechanically-detected problem with a figure's source.

    ``kind`` is ``'compile'`` (unparseable), ``'bounds'`` (a shape spills
    past the viewBox), or ``'binding'`` (an element→chunk binding names an
    ``id`` that no element in the source carries — ADR 0057 drift). ``node``
    is the offending element's ``id`` (or its tag when it has none); empty
    for a whole-document compile failure.
    """

    kind: str
    node: str
    message: str


@dataclass(frozen=True, slots=True)
class Element:
    """A named element of a figure — the anchor a chunk binding attaches to.

    ``id`` is the stable source ``id=`` (the join key for a ``depicts``
    binding, ADR 0057); ``tag`` the local tag name; ``coords`` a compact
    human/model-readable geometry string (``x40 y60 w80 h20`` for a rect,
    ``cx120 cy70 r8`` for a circle, a bbox for a polygon, or ``""`` for a
    shape whose geometry isn't cheaply measurable — path/text/group).
    """

    id: str
    tag: str
    coords: str


def _localname(tag: str) -> str:
    """The local tag name, stripping any ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _parse(svg: str) -> ET.Element:
    """Parse ``svg`` to a root element or raise :class:`SvgError`."""
    try:
        return ET.fromstring(svg)
    except ET.ParseError as exc:
        raise SvgError(f"not well-formed SVG/XML: {exc}") from exc


def parse_error(svg: str) -> str | None:
    """Return ``None`` when ``svg`` parses, else a one-line reason.

    The *compile* auto-lint. Cheap and total — the turn loop calls this
    before trusting a model reply, and treats a non-``None`` result as a
    failure to auto-heal. Also rejects a well-formed fragment whose root is
    not ``<svg>`` (a model returning bare ``<rect/>``).
    """
    try:
        root = _parse(svg)
    except SvgError as exc:
        return str(exc)
    if _localname(root.tag) != "svg":
        return f"root element must be <svg>, got <{_localname(root.tag)}>"
    return None


def read_viewbox(svg: str) -> tuple[float, float, float, float] | None:
    """Extract the root ``viewBox`` as ``(min-x, min-y, w, h)``, or ``None``.

    ``None`` when absent or malformed — the viewBox is *content* (it lives
    on the ``<svg>`` root, the model can edit it), mirrored onto the ref's
    ``meta.viewbox`` for the editor. Never raises on a parseable document.
    """
    try:
        root = _parse(svg)
    except SvgError:
        return None
    return _viewbox_of(root)


def _viewbox_of(root: ET.Element) -> tuple[float, float, float, float] | None:
    raw = root.get("viewBox") or root.get("viewbox")
    if not raw:
        return None
    parts = raw.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        return None
    return (x, y, w, h)


def sanitize_svg(svg: str) -> str:
    """Strip the active-content / external-fetch surface and re-serialize.

    Removes ``<script>`` / ``<foreignObject>`` subtrees, every ``on*`` event
    handler attribute, and any ``href`` / ``xlink:href`` that is not a local
    ``#fragment`` reference (external URLs *and* ``data:`` URIs — both are an
    SSRF/XSS vector). Because the result is **inlined into the page DOM**, it
    also neutralises CSS external fetches (``@import`` and non-fragment
    ``url(...)`` in ``<style>`` bodies and ``style=`` attributes) and drops any
    SMIL animation element whose ``attributeName`` targets an ``on*`` / ``href``
    attribute (declarative animation itself is kept — see
    ``precis-figure-animate``). Guarantees ``xmlns`` on the root so the result
    renders standalone. Raises :class:`SvgError` if ``svg`` does not parse (the
    caller compile-checks first).
    """
    root = _parse(svg)
    _strip_dangerous(root)
    # Guarantee the default namespace so `<img src=data:image/svg+xml…>` and
    # a standalone rasterizer both render it. register_namespace keeps the
    # serialization prefix-free (no `ns0:`).
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    if "}" not in root.tag:
        # Bare (un-namespaced) authoring — promote to the SVG namespace so the
        # rendered output is a valid standalone document (gets an `xmlns`).
        _promote_namespace(root)
    return ET.tostring(root, encoding="unicode")


def _strip_dangerous(root: ET.Element) -> None:
    """In-place: drop dangerous subtrees + attributes across the whole tree."""
    # ElementTree has no parent pointers — build one to delete children.
    parents = {child: parent for parent in root.iter() for child in parent}
    for el in list(root.iter()):
        tag = _localname(el.tag).lower()
        if tag in _DANGEROUS_TAGS or (
            tag in _ANIMATION_TAGS and _animates_dangerous_attr(el)
        ):
            parent = parents.get(el)
            if parent is not None:
                parent.remove(el)
            continue
        _strip_attrs(el)
        # A <style> body is inlined into the page DOM (no <img> sandbox), so its
        # CSS can fetch off-origin via @import / url(...) — neutralise both while
        # keeping @keyframes (the animation payload) intact.
        if tag == "style" and el.text:
            el.text = _sanitize_css(el.text)


def _animates_dangerous_attr(el: ET.Element) -> bool:
    """True if a SMIL element's ``attributeName`` targets ``on*`` or ``href``."""
    for name, value in el.attrib.items():
        if _localname(name).lower() == "attributename":
            target = (value or "").strip().lower().rsplit(":", 1)[-1]
            return target.startswith("on") or target == "href"
    return False


def _strip_attrs(el: ET.Element) -> None:
    """Drop ``on*`` handlers + external ``href``; neutralise ``style=`` urls."""
    for attr in list(el.attrib):
        local = _localname(attr).lower()
        if local.startswith("on") or (
            local == "href" and not (el.attrib[attr] or "").lstrip().startswith("#")
        ):
            del el.attrib[attr]
        elif local == "style":
            el.attrib[attr] = _sanitize_css(el.attrib[attr] or "")


def _sanitize_css(css: str) -> str:
    """Strip ``@import`` and rewrite any non-fragment ``url(...)`` to ``url(#)``.

    Keeps local ``url(#gradient)`` refs and ``@keyframes`` rules; removes the
    external-fetch surface that becomes live once ``<style>`` / ``style=`` is
    inlined into the page. Regex-level (not a full CSS parser) — adequate for
    model-authored, operator-viewed SVG, not a hostile-multi-tenant boundary.
    """
    if not css:
        return css
    css = _CSS_AT_IMPORT_RE.sub("", css)
    return _CSS_URL_RE.sub(
        lambda m: m.group(0) if m.group(2).strip().startswith("#") else "url(#)",
        css,
    )


def _promote_namespace(root: ET.Element) -> None:
    """Move a bare (prefix-less, namespace-less) tree into the SVG namespace."""
    for el in root.iter():
        if "}" not in el.tag:
            el.tag = f"{{{SVG_NS}}}{el.tag}"


def default_svg(
    viewbox: tuple[float, float, float, float] = DEFAULT_VIEWBOX,
) -> str:
    """A starter empty canvas at ``viewbox`` — the birth source of a figure."""
    x, y, w, h = viewbox
    vb = f"{_num(x)} {_num(y)} {_num(w)} {_num(h)}"
    return (
        f'<svg xmlns="{SVG_NS}" viewBox="{vb}">\n'
        f"  <!-- empty canvas — draw here -->\n"
        f"</svg>\n"
    )


def _num(v: float) -> str:
    """Format a float without a trailing ``.0`` (256.0 → ``256``)."""
    return str(int(v)) if v == int(v) else str(v)


def lint_svg(
    svg: str,
    viewbox: tuple[float, float, float, float] | None = None,
) -> list[LintFinding]:
    """Return every mechanically-detected finding: compile + out-of-bounds.

    A compile failure short-circuits to a single ``'compile'`` finding. Else
    every measurable shape (rect/circle/ellipse/line/polyline/polygon) whose
    bbox spills past ``viewbox`` (defaulting to the document's own viewBox,
    then :data:`DEFAULT_VIEWBOX`) yields a ``'bounds'`` finding. Paths, text
    and groups are not bounds-checked (slice 1 limitation).
    """
    try:
        root = _parse(svg)
    except SvgError as exc:
        return [LintFinding("compile", "", str(exc))]
    box = viewbox or _viewbox_of(root) or DEFAULT_VIEWBOX
    minx, miny, w, h = box
    maxx, maxy = minx + w, miny + h
    findings: list[LintFinding] = []
    for el in root.iter():
        if _localname(el.tag) not in _BBOX_SHAPES:
            continue
        bbox = _shape_bbox(el)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        if x0 < minx or y0 < miny or x1 > maxx or y1 > maxy:
            node = el.get("id") or _localname(el.tag)
            findings.append(
                LintFinding(
                    "bounds",
                    node,
                    f"{node} extends outside the {_num(w)}×{_num(h)} viewBox "
                    f"(bbox {_num(x0)},{_num(y0)}…{_num(x1)},{_num(y1)})",
                )
            )
    return findings


def elements(svg: str) -> list[Element]:
    """Every element carrying a stable ``id=`` — the bindable anchors of the
    figure (ADR 0057). Returns ``[]`` on an unparseable document (the caller
    already compile-checks). Order is document order."""
    try:
        root = _parse(svg)
    except SvgError:
        return []
    out: list[Element] = []
    for el in root.iter():
        eid = el.get("id")
        if eid:
            out.append(Element(id=eid, tag=_localname(el.tag), coords=_coords_str(el)))
    return out


def lint_bindings(svg: str, bound_ids: set[str]) -> list[LintFinding]:
    """A ``'binding'`` finding for each bound element id absent from the
    source — the dangling-binding drift check (ADR 0057). Empty when every
    binding still resolves to a live element (or nothing is bound)."""
    if not bound_ids:
        return []
    present = {e.id for e in elements(svg)}
    return [
        LintFinding(
            "binding",
            eid,
            f"binding references element id {eid!r}, but the source has no "
            f"element with that id (renamed or removed?)",
        )
        for eid in sorted(bound_ids)
        if eid not in present
    ]


def _coords_str(el: ET.Element) -> str:
    """A compact geometry string for :class:`Element` — authored attribute
    values per shape, a bbox for polys, ``""`` when not measurable."""
    name = _localname(el.tag)
    g = el.get
    if name == "rect":
        return f"x{g('x', '0')} y{g('y', '0')} w{g('width', '0')} h{g('height', '0')}"
    if name == "circle":
        return f"cx{g('cx', '0')} cy{g('cy', '0')} r{g('r', '0')}"
    if name == "ellipse":
        return f"cx{g('cx', '0')} cy{g('cy', '0')} rx{g('rx', '0')} ry{g('ry', '0')}"
    if name == "line":
        return f"{g('x1', '0')},{g('y1', '0')}→{g('x2', '0')},{g('y2', '0')}"
    bbox = _shape_bbox(el)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        return f"bbox {_num(x0)},{_num(y0)}…{_num(x1)},{_num(y1)}"
    return ""


def _fget(el: ET.Element, attr: str, default: float = 0.0) -> float:
    try:
        return float(el.get(attr, default))
    except (TypeError, ValueError):
        return default


def _shape_bbox(el: ET.Element) -> tuple[float, float, float, float] | None:
    """Axis-aligned bbox ``(x0, y0, x1, y1)`` for a measurable shape, or None.

    Ignores ``transform`` (slice 1) — a transformed shape simply isn't
    bounds-checked rather than mis-reported.
    """
    if el.get("transform"):
        return None
    name = _localname(el.tag)
    try:
        if name == "rect":
            x, y = _fget(el, "x"), _fget(el, "y")
            return (x, y, x + _fget(el, "width"), y + _fget(el, "height"))
        if name == "circle":
            cx, cy, r = _fget(el, "cx"), _fget(el, "cy"), _fget(el, "r")
            return (cx - r, cy - r, cx + r, cy + r)
        if name == "ellipse":
            cx, cy = _fget(el, "cx"), _fget(el, "cy")
            rx, ry = _fget(el, "rx"), _fget(el, "ry")
            return (cx - rx, cy - ry, cx + rx, cy + ry)
        if name == "line":
            x1, y1 = _fget(el, "x1"), _fget(el, "y1")
            x2, y2 = _fget(el, "x2"), _fget(el, "y2")
            return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if name in ("polyline", "polygon"):
            return _points_bbox(el.get("points", ""))
    except (TypeError, ValueError):
        return None
    return None


def _points_bbox(raw: str) -> tuple[float, float, float, float] | None:
    nums = [float(n) for n in raw.replace(",", " ").split()] if raw.strip() else []
    if len(nums) < 4 or len(nums) % 2 != 0:
        return None
    xs = nums[0::2]
    ys = nums[1::2]
    return (min(xs), min(ys), max(xs), max(ys))
