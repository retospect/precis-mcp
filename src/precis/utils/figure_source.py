"""Figure source resolver — the medium axis (ADR 0057).

A draft figure chunk (``chunk_kind='figure'``) is described by two orthogonal
axes:

* **origin** (rights) — ``original`` / ``own_graph`` / ``third_party`` — drives
  *clearance* (see :mod:`precis.utils.figure_clearance`, ADR 0034 §4).
* **medium** (production) — ``blob`` / ``canvas`` / ``graph`` / ``none`` —
  drives *how the pixels are produced*, i.e. what the reader / export / editor
  do with it.

This module is the single seam between the two axes and every consumer. Given a
figure chunk it resolves the medium and returns a :class:`FigureSource` — a
uniform ``(render, clearance, edit)`` contract the reader and the clearance
gate both call, so adding a medium is one branch here and nothing downstream.

Slice 1 (ADR 0057 §7) wires the ``canvas`` medium: a figure that is *ours* and
editable is backed by a live ``kind='figure'`` SVG canvas, referenced **by
link** (``has-figure``, chunk→ref) rather than a static blob. The reader
renders that canvas inline via the figure kind's script-safe
``/figure/{slug}/source.svg`` endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from precis.utils.figure_clearance import figure_status

Medium = Literal["blob", "canvas", "graph", "none"]
RenderMode = Literal["image", "canvas", "placeholder"]

#: Drawable SVG element localnames — a canvas with none of these is still the
#: birth ``default_svg`` (an empty comment-only canvas), so it isn't "drawn yet".
_DRAWABLE_RE = re.compile(
    r"<(?:\w+:)?(path|rect|circle|ellipse|line|polyline|polygon|text|image|use)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """How the reader should render a figure. A small tagged union so the
    template stops assuming a blob ``<img>``:

    * ``image``       — ``url`` (+ ``mime``) → ``<img src=url>`` (blob / graph).
    * ``canvas``      — ``url`` is the script-safe ``source.svg`` for inline
      ``<img>``; ``canvas_slug`` / ``open_url`` drive "open in /figure".
    * ``placeholder`` — no asset yet; ``cta`` labels the call-to-action.
    """

    mode: RenderMode
    url: str | None = None
    mime: str | None = None
    canvas_slug: str | None = None
    open_url: str | None = None
    cta: str = ""


@dataclass(frozen=True, slots=True)
class FigureSource:
    """A figure's resolved medium + the three consumer contracts."""

    medium: Medium
    render: RenderSpec
    cleared: bool
    reason: str  # empty when cleared, else a short human explanation
    canvas_slug: str | None = None  # set for the canvas medium (edit target)


def _svg_has_content(svg: str | None) -> bool:
    """True if an SVG source carries at least one drawable element (i.e. it is
    past the empty birth canvas)."""
    return bool(svg and _DRAWABLE_RE.search(svg))


def _canvas_source(store: Any, canvas_ref_id: int) -> str | None:
    """The ``figure_node`` SVG source for a canvas ref, or ``None``."""
    for c in store.reading_order(canvas_ref_id, kind="figure"):
        if c.chunk_kind == "figure_node":
            return c.text
    return None


#: mime → file extension for a raster figure blob embedded verbatim in export.
_EXT_BY_MIME: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
}

#: Rasterise SVG at this zoom so a print embed isn't blurry (viewBox px × N).
_SVG_EXPORT_ZOOM = 3.0


def _svg_to_png(svg: str) -> bytes | None:
    """Rasterise (sanitized) SVG source to PNG bytes via ``resvg`` — a
    self-contained Rust wheel, no system cairo. ``None`` if ``resvg`` is absent
    (export degrades: the figure is skipped with a warning) or the render
    fails. A white background so a transparent canvas prints as white."""
    try:
        import resvg_py
    except ImportError:  # pragma: no cover — resvg is a declared dep
        return None
    try:
        return bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg, zoom=_SVG_EXPORT_ZOOM, background="#ffffff"
            )
        )
    except Exception:
        return None


def figure_export_asset(store: Any, chunk: Any) -> tuple[bytes, str] | None:
    """``(bytes, ext)`` for embedding a figure in an export (ADR 0057 §7,
    slice 4), or ``None`` when the figure has no usable asset.

    Raster blobs pass through as-is; an SVG — whether a ``blob``-SVG (already
    sanitized at ingest, slice 3) or a linked **canvas** (sanitized at write) —
    rasterises to PNG so it embeds in both LaTeX (``\\includegraphics``) and
    docx (``add_picture``), neither of which consumes raw SVG."""
    canvas_ref_id = store.figure_canvas_ref(chunk.chunk_id)
    if canvas_ref_id is not None:
        svg = _canvas_source(store, canvas_ref_id)
        if not _svg_has_content(svg):
            return None
        assert svg is not None
        png = _svg_to_png(svg)
        return (png, "png") if png is not None else None

    blob = store.get_chunk_blob(chunk.handle)
    if blob is not None:
        data, mime = blob
        mime = (mime or "").split(";", 1)[0].strip()
        if mime == "image/svg+xml":
            png = _svg_to_png(data.decode("utf-8", "replace"))
            return (png, "png") if png is not None else None
        return data, _EXT_BY_MIME.get(mime, "png")

    return None


def resolve_figure_source(store: Any, chunk: Any) -> FigureSource:
    """Resolve a draft figure chunk to its :class:`FigureSource`.

    Resolution order (ADR 0057 §5): a ``has-figure`` link wins (``canvas``);
    else a render recipe / ``own_graph`` (``graph``, which still owns a blob);
    else a blob (``blob``); else nothing (``none`` — the placeholder that kills
    the broken-image glyph). ``origin`` never selects the medium — it only
    feeds clearance."""
    fig = (getattr(chunk, "meta", None) or {}).get("figure") or {}
    origin = fig.get("origin")

    # 1 — canvas: an owned, editable SVG canvas linked from this chunk.
    canvas_ref_id = store.figure_canvas_ref(chunk.chunk_id)
    if canvas_ref_id is not None:
        ref = store.get_ref(kind="figure", id=canvas_ref_id)
        slug = getattr(ref, "slug", None) if ref is not None else None
        if slug:
            drawn = _svg_has_content(_canvas_source(store, canvas_ref_id))
            return FigureSource(
                medium="canvas",
                render=RenderSpec(
                    mode="canvas",
                    url=f"/figure/{slug}/source.svg",
                    mime="image/svg+xml",
                    canvas_slug=slug,
                    open_url=f"/figure/{slug}",
                ),
                cleared=drawn,  # ours — cleared once there's an actual drawing
                reason="" if drawn else "canvas started but nothing drawn yet",
                canvas_slug=slug,
            )

    # 2/3 — blob-backed (a raster/static-SVG image, or a rendered graph). A
    # graph figure (0035) carries a render recipe and still lands its pixels in
    # chunk_blobs, so it resolves here; the medium label distinguishes it.
    if store.has_chunk_blob(chunk.chunk_id):
        is_graph = origin == "own_graph" or bool(fig.get("render_pending"))
        ok, reason = figure_status(fig)
        return FigureSource(
            medium="graph" if is_graph else "blob",
            render=RenderSpec(mode="image", url=f"/drafts/blob/{chunk.handle}"),
            cleared=ok,
            reason=reason,
        )

    # 4 — no asset at all: a caption-only placeholder. Not cleared (fixes the
    # "cleared to ship" over an empty figure), and offers "create drawing".
    return FigureSource(
        medium="none",
        render=RenderSpec(mode="placeholder", cta="create drawing"),
        cleared=False,
        reason="no image yet",
    )
