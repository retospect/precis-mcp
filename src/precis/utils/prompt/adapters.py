"""Adapters — package assembled blocks for one runner (ADR 0038 §3).

The assembler is model-agnostic; the **adapter owns packaging and
caching**. Each runner gets one:

* :class:`ClaudeAgentAdapter` — ``claude_agent`` / ``claude_p``: a
  system/user split. Every ``CACHED`` block goes to the system prompt
  (the long cache prefix), every ``VARIABLE`` block to the user message.
* (future) a litellm/summarizer adapter — a single prefix-stable prompt
  for llama.cpp KV-cache; folds in with migration step 2.

Adapters preserve block order *within* a layer (authored intent) and
join with a blank line, matching the hand-rolled prompts they replace.
"""

from __future__ import annotations

from collections.abc import Sequence

from precis.utils.prompt.model import Block, Layer


def _join(blocks: Sequence[Block]) -> str:
    return "\n\n".join(b.text for b in blocks)


class ClaudeAgentAdapter:
    """Render blocks into a ``(system, user)`` pair for ``claude_agent``.

    ``CACHED`` blocks → ``system`` (the stable cache prefix shared across
    ticks); ``VARIABLE`` blocks → ``user`` (per-tick). Order within each
    layer is the assembled order. This is exactly the two-layer shape the
    planner has always emitted, now derived from the module list rather
    than hand-concatenated."""

    @staticmethod
    def render(blocks: Sequence[Block]) -> tuple[str, str]:
        cached = [b for b in blocks if b.layer is Layer.CACHED]
        variable = [b for b in blocks if b.layer is Layer.VARIABLE]
        return _join(cached), _join(variable)


__all__ = ["ClaudeAgentAdapter"]
