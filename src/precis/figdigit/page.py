"""Input rasters for the digitiser: image files via PIL, PDF page-clips
via pymupdf.

Per spec Decisions 1 the caller hands the digitiser a path or an image —
whether ingest keeps source PDFs or per-figure crops is a separate
decision. Both loaders return a ``(H, W, 3)`` uint8 RGB array, the shape
every downstream function takes. PIL and ``fitz`` import lazily inside the
functions that need them: pillow rides with matplotlib in every install
but is deliberately not a core dep (the ``[paper]`` marker-pdf cap), and
pymupdf lives behind the ``[paper]`` extra.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

__all__ = ["load_image", "png_bytes", "render_page_png", "render_pdf"]

# Hard cap on a rendered page's longest side (gr520326 review): the
# ``/files/pdf_page`` door renders whatever ``pdf_url`` pulled in, and a
# hostile 14400 pt MediaBox at 200 dpi is a ~4.8 GB pixmap — one reusable
# token then OOMs the gateway on every hit. 4000 px at the default zoom
# still serves any real paper page at full resolution.
_MAX_PAGE_EDGE_PX = 4000


def load_image(source: str | Path | np.ndarray) -> NDArray[np.uint8]:
    """An image file → ``(H, W, 3)`` uint8 RGB; an array passes through.

    Array input must already be ``(H, W)``-compatible grey or ``(H, W, 3+)``
    — the geometry layer's ``as_gray`` accepts both, so this only
    normalises the file case.
    """
    if isinstance(source, np.ndarray):
        return np.asarray(source, dtype=np.uint8)
    from PIL import Image

    with Image.open(source) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


def render_pdf(
    path: str | Path,
    *,
    page: int = 0,
    clip: tuple[float, float, float, float] | None = None,
    dpi: float = 600.0,
) -> NDArray[np.uint8]:
    """One PDF page (optionally clipped) rendered to an RGB raster.

    ``clip`` is ``(x0, y0, x1, y1)`` in PDF points (1/72 in, origin
    top-left) — the bbox a paper's figure panel occupies on the page. 600
    dpi is the spec default: fine enough that tick marks stay several px
    wide. pymupdf is the ``[paper]`` extra; a missing install raises the
    typed optional-dep error with the install hint.
    """
    from precis.utils.optional_deps import require_optional

    fitz = require_optional("fitz", extra="paper")
    zoom = dpi / 72.0
    doc = fitz.open(path)
    try:
        pg = doc[page]
        rect = fitz.Rect(*clip) if clip is not None else None
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        return np.ascontiguousarray(arr[..., :3])
    finally:
        doc.close()


def png_bytes(rgb: np.ndarray) -> bytes:
    """RGB raster → PNG bytes, the wire format a vision proposer consumes."""
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.asarray(rgb, dtype=np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def render_page_png(
    path: str | Path,
    *,
    page: int,
    dpi: float = 200.0,
) -> tuple[bytes, int]:
    """One whole PDF page → ``(PNG bytes, page_count)``.

    The page-image door's renderer (gr520326): unlike :func:`render_pdf`
    this emits the wire format directly — ``pixmap.tobytes("png")`` — so
    the web route needs no PIL/numpy, only ``fitz`` (lazy; the install
    hint names ``[web]`` because the gateway venv is ``[web,estimate]``,
    not ``[paper]``). The zoom is clamped so the longest rendered side
    stays under ``_MAX_PAGE_EDGE_PX`` — the token is reusable until
    expiry, so an oversize MediaBox must not turn every hit into a
    multi-GB pixmap. ``page`` is the 0-based fitz index — the same
    numbering Marker ``page-N`` spans and ``chunks.page_first`` carry.
    ``doc[page]`` raises ``IndexError`` out of range; the caller maps
    that to its not-found semantics.
    """
    from precis.utils.optional_deps import require_optional

    fitz = require_optional("fitz", extra="web")
    zoom = dpi / 72.0
    doc = fitz.open(path)
    try:
        pg = doc[page]
        edge = max(pg.rect.width, pg.rect.height)
        if edge * zoom > _MAX_PAGE_EDGE_PX:
            zoom = _MAX_PAGE_EDGE_PX / edge
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png"), doc.page_count
    finally:
        doc.close()
