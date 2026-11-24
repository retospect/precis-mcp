"""Bounded resvg rendering for the editable figure's persisted PNG."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from functools import cache
from importlib.util import find_spec
from pathlib import Path

from precis.figure.svg import read_viewbox, sanitize_svg
from precis.utils.figure_source import _svg_has_content

_MAX_SIDE = 4096
_LENGTH = re.compile(r"([0-9.eE+-]+)(px|in|cm|mm|pt|pc|%)?")
_UNITS = {"in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "pt": 96 / 72, "pc": 16.0}


def _pixels(raw: str | None, fallback: float) -> float:
    if raw is None:
        return fallback
    match = _LENGTH.fullmatch(raw.strip())
    if match is None:
        raise ValueError("unsupported SVG viewport length")
    value = float(match[1])
    if match[2] == "%":
        return value * fallback / 100
    return value * _UNITS.get(match[2] or "", 1.0)


@cache
def _font_files() -> list[str]:
    spec = find_spec("matplotlib")
    if spec is None or spec.origin is None:
        return []
    ttf = Path(spec.origin).parent / "mpl-data" / "fonts" / "ttf"
    return [str(path) for path in sorted(ttf.glob("DejaVu*.ttf"))]


def render_png(svg: str) -> bytes | None:
    """Render the static drawing at up to 3× using packaged fonts."""
    if not _svg_has_content(svg):
        return None
    import resvg_py

    try:
        root = ET.fromstring(sanitize_svg(svg))
        box = read_viewbox(svg)
        if box is not None and (
            not all(math.isfinite(v) for v in box) or min(box[2:]) <= 0
        ):
            return None
        fallback_w, fallback_h = box[2:] if box else (300.0, 150.0)
        width = _pixels(root.get("width"), fallback_w)
        height = _pixels(root.get("height"), fallback_h)
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            return None
        # Give resvg a finite viewport and bound the pixmap before allocation.
        zoom = min(3.0, _MAX_SIDE / max(width, height))
        root.attrib.pop("viewbox", None)
        root.set("viewBox", " ".join(map(str, box or (0, 0, width, height))))
        root.set("width", str(max(1, round(width * zoom))))
        root.set("height", str(max(1, round(height * zoom))))
        return bytes(
            resvg_py.svg_to_bytes(
                svg_string=ET.tostring(root, encoding="unicode"),
                background="#ffffff",
                skip_system_fonts=True,
                font_files=_font_files(),
                font_family="DejaVu Sans",
                sans_serif_family="DejaVu Sans",
                serif_family="DejaVu Serif",
                monospace_family="DejaVu Sans Mono",
            )
        )
    except (ValueError, OverflowError):
        return None
