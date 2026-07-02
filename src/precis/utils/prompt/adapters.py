"""Adapters — package assembled blocks for one runner (ADR 0038 §3).

The assembler is model-agnostic; the **adapter owns packaging and
caching**. Each runner gets one:

* :class:`ClaudeAgentAdapter` — ``claude_agent`` / ``claude_p``: a
  system/user split. Every ``CACHED`` block goes to the system prompt
  (the long cache prefix), every ``VARIABLE`` block to the user message.
* :class:`LiteLLMAdapter` — the litellm ``summarizer``/helper alias
  (Qwen3-Next-80B on llama.cpp): the same split, packaged as an OpenAI
  chat ``messages`` list. ``CACHED`` blocks form the leading ``system``
  message (the stable KV-cache **prefix** llama.cpp reuses across a
  document's chunks); ``VARIABLE`` blocks form the trailing ``user``
  message (the per-tick tail). Migration step 2 (ADR 0038 §Migration)
  folds ``llm_summarize`` + ``briefing`` onto it.

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


class LiteLLMAdapter:
    """Render blocks into an OpenAI chat ``messages`` list (ADR 0038 §3/§4).

    The helper-profile adapter for the litellm ``summarizer`` alias
    (``llm_summarize``, ``briefing`` — Shot 2). It mirrors
    :class:`ClaudeAgentAdapter`'s layer split but emits the OpenAI
    ``[{"role","content"}, …]`` shape the ``LlmClient`` posts:

    * ``CACHED`` blocks → a single leading ``system`` message — the stable
      prefix llama.cpp keeps KV-cache-hot across every chunk of a document
      (instruction + few-shot examples + the per-doc header, in that
      order; Shot 2's "STABLE PREFIX"). The per-doc header rides here, not
      in the ``user`` turn, because it is *stable across a document's
      chunk-ticks* and belongs to the reusable prefix.
    * ``VARIABLE`` blocks → a single trailing ``user`` message — the
      per-chunk tail (section path + keywords + quantities + passage).

    Order within each layer is the assembled order; blocks join with a
    blank line, matching the hand-rolled prompt this replaces. A layer
    with no blocks emits no message (never a blank turn)."""

    @staticmethod
    def render(blocks: Sequence[Block]) -> list[dict[str, str]]:
        cached = [b for b in blocks if b.layer is Layer.CACHED]
        variable = [b for b in blocks if b.layer is Layer.VARIABLE]
        messages: list[dict[str, str]] = []
        if cached:
            messages.append({"role": "system", "content": _join(cached)})
        if variable:
            messages.append({"role": "user", "content": _join(variable)})
        return messages


__all__ = ["ClaudeAgentAdapter", "LiteLLMAdapter"]
