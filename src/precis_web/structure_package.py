"""A shareable "structure package": one image that carries its own caption.

gr504830 (shape agreed on the gripe, comment 1): a picture of one structure
— the stored atoms through the same :mod:`precis.viz3d` stick renderer the
figure views use — with an element legend built in and a caption band below
a dashed crop rule: what it is, where it lives (se design/block, structure
slug, handle, version, URL), what was recorded about its relaxation
(:mod:`precis_web.relax_provenance`, the same verdict the se page shows), the
block's stated intent, and the structure's free-text description labelled as
unchecked. Cropping the band off leaves a clean picture; the same text rides
in the file's metadata (PNG ``iTXt``, SVG ``<title>``/``<desc>``/
``<metadata>``), so a cropped copy still says what it is.

Text is rasterised with the DejaVu fonts matplotlib ships (a core
dependency), loaded explicitly with system fonts skipped: resvg drops text it
cannot resolve without an error, and a server's font set is not something
this code should depend on.
"""

from __future__ import annotations

import json
import re
import struct
import textwrap
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import numpy as np

from precis.structure.probe import detect_bonds
from precis.viz3d.camera import Camera
from precis.viz3d.render import render_svg
from precis.viz3d.stickfig import cpk_color, stick_scene
from precis_web.relax_provenance import RelaxState, formula

#: Hard bound on atoms drawn in-request (the figure view's own limit).
MAX_ATOMS = 20000
#: Above these atom counts the gradient shading is downgraded (r2 → r1 → r0).
_R2_MAX_ATOMS = 2000
_R1_MAX_ATOMS = 6000

#: Canvas geometry, SVG user units (the PNG is rasterised at ``_PNG_ZOOM``).
_W = 760
_PAD = 16
_IMG_H = 500
_LEGEND_H = 30
_PNG_ZOOM = 2.0
#: Characters per caption line at 11 px DejaVu Sans across ``_W - 2*_PAD``.
_WRAP = 118
#: Free text (intent, description) is cut here so the band stays a caption.
_TEXT_CAP = 400

#: Fixed three-quarter view: shows a tube's axis and its cross-section.
_CAMERA = Camera(azimuth_deg=35.0, elevation_deg=25.0)

_FONT = "DejaVu Sans"


class TooLarge(Exception):
    """The structure has more atoms than an in-request render may draw."""


@dataclass(frozen=True)
class PackageInfo:
    """Everything the caption says. ``url`` is absolute (the request's own
    base URL — never a literal host in code)."""

    title: str
    ref_line: str
    url: str
    relax: RelaxState
    description: str = ""
    intent: str = ""
    rendered_utc: str = ""
    record: dict[str, Any] = field(default_factory=dict)


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _TEXT_CAP else text[: _TEXT_CAP - 1].rstrip() + "…"


def _font_files() -> list[str]:
    import matplotlib

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    return [
        str(p)
        for p in (ttf / "DejaVuSans.ttf", ttf / "DejaVuSans-Bold.ttf")
        if p.exists()
    ]


def caption_lines(info: PackageInfo, facts: str) -> list[tuple[str, str]]:
    """The caption as ``(style, text)`` lines, wrapped — style is ``title``,
    ``key`` (a labelled fact) or ``note`` (grey detail). The same text, joined,
    is the image metadata's Description."""
    out: list[tuple[str, str]] = [("title", info.title)]

    def add(style: str, label: str, text: str) -> None:
        if not text:
            return
        body = f"{label}: {text}" if label else text
        for i, line in enumerate(textwrap.wrap(body, _WRAP) or [body]):
            out.append((style if i == 0 else "note", line))

    add("key", "ref", info.ref_line)
    add("key", "url", info.url)
    add("key", "atoms", facts)
    add("key", "relax", info.relax.headline)
    add("note", "", info.relax.detail)
    add("key", "intent", _cap(info.intent))
    add("key", "description (free text, not checked)", _cap(info.description))
    add("note", "", f"rendered {info.rendered_utc} · precis")
    return out


def _structure_svg(scene: Any) -> tuple[str, dict[str, int], int]:
    """``(stick-figure SVG, element counts, bond count)`` for ``scene``."""
    labels = list(scene.atoms)
    n = len(labels)
    if n > MAX_ATOMS:
        raise TooLarge(f"{n} atoms > {MAX_ATOMS}")
    index = {lab: i for i, lab in enumerate(labels)}
    coords = np.array(
        [scene.cell.frac_to_cart(scene.atoms[lab].frac) for lab in labels],
        dtype=np.float64,
    ).reshape(n, 3)
    all_bonds = [b for b in (scene.bonds or detect_bonds(scene)) if b.i != b.j]
    # Periodic-image bonds are not drawn (as in the figure view).
    drawn = [
        (index[b.i], index[b.j])
        for b in all_bonds
        if b.i in index and b.j in index and tuple(b.image) == (0, 0, 0)
    ]
    elements = [scene.atoms[lab].element for lab in labels]
    refine = 0 if n > _R1_MAX_ATOMS else 1 if n > _R2_MAX_ATOMS else 2
    svg = render_svg(stick_scene(elements, coords, drawn), _CAMERA, refine=refine)
    counts: dict[str, int] = {}
    for el in elements:
        counts[el] = counts.get(el, 0) + 1
    return svg, counts, len(all_bonds)


def _nest(svg: str, *, y: float, height: float) -> str:
    """Place a standalone ``<svg viewBox=…>`` at ``(0, y)`` sized ``_W×height``."""
    return re.sub(
        r"^<svg ",
        f'<svg x="0" y="{y:g}" width="{_W}" height="{height:g}" ',
        svg.strip(),
        count=1,
    )


def _legend(counts: dict[str, int], y: float) -> str:
    parts: list[str] = []
    x = float(_PAD)
    order = sorted(counts, key=lambda e: (-counts[e], e))
    for el in order:
        color = cpk_color(el)
        parts.append(
            f'<circle cx="{x + 7:g}" cy="{y + 15:g}" r="7" fill="{color}" '
            'stroke="#333333" stroke-width="1"/>'
        )
        label = f"{el} {counts[el]}"
        parts.append(
            f'<text x="{x + 19:g}" y="{y + 19:g}" font-size="12" '
            f'font-family="{_FONT}" fill="#222222">{escape(label)}</text>'
        )
        x += 30 + 8 * len(label)
    return "".join(parts)


def package_svg(scene: Any, info: PackageInfo) -> tuple[str, str]:
    """``(svg, caption text)``: the whole package as one SVG (structure,
    legend, crop rule, caption, metadata) and its caption as plain lines."""
    inner, counts, n_bonds = _structure_svg(scene)
    pbc = "".join("T" if p else "F" for p in scene.cell.pbc)
    facts = (
        f"{formula(counts)} · {sum(counts.values())} atoms · {n_bonds} bonds · "
        f"pbc {pbc}"
    )
    lines = caption_lines(info, facts)
    rule_y = _IMG_H + _LEGEND_H + 8
    y = rule_y + 22
    text: list[str] = []
    for style, line in lines:
        size, weight, fill, step = {
            "title": (15, "bold", "#111111", 22),
            "key": (11, "normal", "#222222", 16),
            "note": (11, "normal", "#666666", 16),
        }[style]
        text.append(
            f'<text x="{_PAD}" y="{y}" font-size="{size}" font-weight="{weight}" '
            f'font-family="{_FONT}" fill="{fill}">{escape(line)}</text>'
        )
        y += step
    height = y + _PAD - 10
    description = "\n".join(line for _s, line in lines)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{height}" '
        f'viewBox="0 0 {_W} {height}">'
        f"<title>{escape(info.title)}</title>"
        f"<desc>{escape(description)}</desc>"
        f"<metadata>{escape(json.dumps(info.record, sort_keys=True))}</metadata>"
        f'<rect x="0" y="0" width="{_W}" height="{height}" fill="#ffffff"/>'
        + _nest(inner, y=0, height=_IMG_H)
        + _legend(counts, _IMG_H)
        + f'<line x1="0" y1="{rule_y}" x2="{_W}" y2="{rule_y}" stroke="#999999" '
        'stroke-width="1" stroke-dasharray="6 4"/>' + "".join(text) + "</svg>"
    )
    return svg, description


def _itxt(keyword: str, text: str) -> bytes:
    # keyword NUL, compression flag 0, method 0, empty language NUL, empty
    # translated keyword NUL, then the UTF-8 text.
    data = keyword.encode("latin-1") + b"\x00" * 5 + text.encode("utf-8")
    body = b"iTXt" + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def with_text_chunks(png: bytes, fields: dict[str, str]) -> bytes:
    """``png`` with one uncompressed ``iTXt`` chunk per field, right after
    ``IHDR`` (UTF-8, so Å and θ survive)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    ihdr_end = 8 + 12 + struct.unpack(">I", png[8:12])[0]
    chunks = b"".join(_itxt(k, v) for k, v in fields.items() if v)
    return png[:ihdr_end] + chunks + png[ihdr_end:]


def png_text_fields(info: PackageInfo, description: str) -> dict[str, str]:
    """The metadata a cropped copy keeps (PNG keyword names from the spec)."""
    return {
        "Title": info.title,
        "Description": description,
        "Source": info.url,
        "Software": "precis",
        "Creation Time": info.rendered_utc,
        "Comment": json.dumps(info.record, sort_keys=True),
    }


def package_png(svg: str, fields: dict[str, str]) -> bytes:
    """Rasterise :func:`package_svg` output and stamp ``fields`` as ``iTXt``."""
    import resvg_py

    png = bytes(
        resvg_py.svg_to_bytes(
            svg_string=svg,
            zoom=_PNG_ZOOM,
            background="#ffffff",
            skip_system_fonts=True,
            font_files=_font_files(),
            font_family=_FONT,
            sans_serif_family=_FONT,
        )
    )
    return with_text_chunks(png, fields)
