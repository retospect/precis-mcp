"""The ``mermaid`` binding of the generic draw-with-me turn loop.

Mirrors ``precis.figure.turn``: the loop lives in :mod:`precis.diagram.turn`,
this shim binds ``MERMAID_LANG`` and the mermaid model dispatch. The web route
calls :func:`run_turn`; tests drive :func:`precis.diagram.turn.run_turn` with
``MERMAID_LANG`` directly.
"""

from __future__ import annotations

import os
from typing import Any

from precis.diagram import turn as _core
from precis.diagram.turn import ClaudeFn, TurnResult
from precis.mermaid.mermaid import MERMAID_LANG

__all__ = ["TurnResult", "run_turn"]


def _default_claude(prompt: str) -> dict[str, Any]:
    from precis.utils.llm.router import LlmRequest, Tier, dispatch

    res = dispatch(
        LlmRequest(
            tier=Tier.CLOUD_SUPER,
            source="mermaid",
            prompt=prompt,
            model=os.environ.get("PRECIS_MERMAID_MODEL"),
            max_usd=float(os.environ.get("PRECIS_MERMAID_MAX_USD", "1.0")),
            timeout_s=float(os.environ.get("PRECIS_MERMAID_TIMEOUT_S", "300")),
        )
    )
    if res.error:
        raise RuntimeError(res.error)
    return res.data or {}


def run_turn(
    store: Any,
    ref: Any,
    message: str,
    *,
    claude_fn: ClaudeFn | None = None,
    skills: str | None = None,
) -> TurnResult:
    """Run one mermaid draw-with-me turn (binds MERMAID_LANG + the model call)."""
    return _core.run_turn(
        MERMAID_LANG,
        store,
        ref,
        message,
        claude_fn=claude_fn or _default_claude,
        skills=skills,
    )
