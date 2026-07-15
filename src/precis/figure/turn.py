"""The ``figure`` (SVG) binding of the generic draw-with-me turn loop.

The loop itself — prompt assembly, bounded auto-heal, the three-doc model, the
element→chunk binding reconcile — lives in :mod:`precis.diagram.turn`, generic
over :class:`~precis.diagram.lang.DiagramLang`. This module is the thin SVG
shim: it binds ``precis.figure.svg.SVG_LANG`` and the SVG model dispatch, and
re-exports the public names (``run_turn`` / ``build_prompt`` / ``TurnResult``)
with the SVG-flavoured keywords (``svg=`` / ``viewbox=``) the callers and tests
already use. Nothing here re-implements the loop.
"""

from __future__ import annotations

import os
from typing import Any

from precis.diagram import turn as _core
from precis.diagram.turn import ClaudeFn, TurnResult
from precis.figure.svg import SVG_LANG

__all__ = ["ClaudeFn", "TurnResult", "build_prompt", "run_turn"]


def _default_claude(prompt: str) -> dict[str, Any]:
    # Routed through the LLM seam (ADR 0046 unit 4b): CLOUD_SUPER (drawing
    # wants a capable model), so PRECIS_LLM_BACKEND can switch it. A
    # PRECIS_FIGURE_MODEL pin still wins (None ⇒ the tier default, opus-4.8).
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    res = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            source="figure",
            prompt=prompt,
            model=os.environ.get("PRECIS_FIGURE_MODEL"),
            max_usd=float(os.environ.get("PRECIS_FIGURE_MAX_USD", "1.0")),
            timeout_s=float(os.environ.get("PRECIS_FIGURE_TIMEOUT_S", "300")),
        )
    )
    if res.error:
        raise RuntimeError(res.error)
    return res.data or {}


def build_prompt(
    *,
    message: str,
    svg: str,
    vocab: str,
    notes: str = "",
    findings: list[Any],
    viewbox: tuple[float, float, float, float],
    skills: str = "",
    context: str = "",
) -> str:
    """Assemble an SVG figure turn prompt (SVG-keyword facade over the core)."""
    return _core.build_prompt(
        SVG_LANG,
        message=message,
        source=svg,
        vocab=vocab,
        notes=notes,
        findings=findings,
        bounds=viewbox,
        skills=skills,
        context=context,
    )


def run_turn(
    store: Any,
    ref: Any,
    message: str,
    *,
    claude_fn: ClaudeFn | None = None,
    skills: str | None = None,
) -> TurnResult:
    """Run one SVG draw-with-me turn (binds SVG_LANG + the SVG model call)."""
    return _core.run_turn(
        SVG_LANG,
        store,
        ref,
        message,
        claude_fn=claude_fn or _default_claude,
        skills=skills,
    )
