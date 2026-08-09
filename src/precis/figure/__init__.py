"""The ``figure`` kind — an interactive SVG canvas you draw *with* the model.

A figure is a chunk-tree ref on the ``draft`` substrate (migration 0057),
never exported (``corpus_role='none'``), holding three model-owned
documents: the SVG **source** (a ``figure_node`` chunk, ``meta.no_index``
so raw markup never embeds), the shared **vocabulary** (``figure_vocab``,
embedded — the negotiated ground truth, "green circles are foos"), and the
model's private **implementation notes** (``figure_notes``, ``no_index``;
migration 0058). Vocab/notes are born empty — the "what this doc is for"
seed lives in the prompt / ``precis-figure-svg`` skill (prepended to every
turn prompt), never stored as content. Chat turns persist as
``figure_turn`` chunks so a session is resumable.

Since diagram-chunk binding ``figure`` is the **SVG instance** of the shared diagram
core (:mod:`precis.diagram` — the ``DiagramLang`` port + the generic turn
loop / context assembler); elements bind to the chunks they depict via
chunk-level ``depicts`` links.

- :mod:`precis.figure.svg` — pure functions: sanitize (XSS/SSRF strip),
  compile-check (parse), out-of-bounds lint (shape bbox vs the viewBox);
  defines ``SVG_LANG``. No DB, no network, no model.
- :mod:`precis.figure.turn` / :mod:`precis.figure.context` — thin shims
  binding ``SVG_LANG`` to the generic core in :mod:`precis.diagram`.

The handler (:mod:`precis.handlers.figure`) is the MCP surface (get/put/
edit/delete/link); the web editor (:mod:`precis_web.routes.figure`) is the
3-pane canvas + chat that drives the turn loop (SVG rendered as a
script-safe ``<img>``). Slice 1 is SVG 2D, browser-rendered; raster/3D
export and per-node chunk split are deferred.
"""

from __future__ import annotations

from precis.figure.svg import (
    DEFAULT_VIEWBOX,
    LintFinding,
    SvgError,
    default_svg,
    lint_svg,
    parse_error,
    read_viewbox,
    sanitize_svg,
)
from precis.figure.turn import TurnResult, build_prompt, run_turn

__all__ = [
    "DEFAULT_VIEWBOX",
    "LintFinding",
    "SvgError",
    "TurnResult",
    "build_prompt",
    "default_svg",
    "lint_svg",
    "parse_error",
    "read_viewbox",
    "run_turn",
    "sanitize_svg",
]
