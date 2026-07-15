"""The ``figure`` (SVG) binding of the generic diagram prepared-context
assembler (ADR 0057).

The assembler lives in :mod:`precis.diagram.context`, generic over
:class:`~precis.diagram.lang.DiagramLang`; this is the thin SVG shim so the
long-standing ``from precis.figure.context import render_diagram_context``
keeps working with the SVG source keyword.
"""

from __future__ import annotations

from typing import Any

from precis.diagram.context import render_diagram_context as _render
from precis.figure.svg import SVG_LANG


def render_diagram_context(store: Any, node_chunk_id: int, svg: str) -> str:
    """The element→chunk prepared context for an SVG figure source."""
    return _render(SVG_LANG, store, node_chunk_id, svg)
