"""The ``figure`` kind — an interactive SVG canvas you draw *with* the model.

A figure is a chunk-tree ref on the ``draft`` substrate (migration 0057),
holding two model-owned documents: the SVG **source** (a ``figure_node``
chunk) and the shared **vocabulary** (a ``figure_vocab`` chunk — the
negotiated ground truth, "green circles are foos"). Chat turns persist as
``figure_turn`` chunks so a session is resumable.

The heavy lifting is split so each piece is testable in isolation:

- :mod:`precis.figure.svg` — pure functions: sanitize (XSS/SSRF strip),
  compile-check (parse), out-of-bounds lint (shape bbox vs the viewBox).
  No DB, no network, no model.
- :mod:`precis.figure.turn` — one interactive turn: build the prompt
  (pinned skills + vocab + current source + lint + user message), call the
  model, sanitize + lint the reply, bounded auto-heal, persist.

The handler (:mod:`precis.handlers.figure`) is the MCP surface (get/put/
edit/delete); the web editor (:mod:`precis_web.routes.figure`) is the
3-pane canvas + chat that drives :func:`precis.figure.turn.run_turn`.
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
