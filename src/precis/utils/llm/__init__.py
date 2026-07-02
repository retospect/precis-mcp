"""The LLM routing layer (ADR 0046).

One seam for model selection + transport choice + result normalization,
consolidating the scattered ``os.environ.get(...)`` model reads and the
three transports (``claude_agent`` / ``claude_p`` / litellm ``LlmClient``)
behind a single :func:`dispatch`. This unit builds the seam; the call
sites fold through it in a follow-up.

Public surface:

* :class:`Tier` — capability tiers (local-small / local-big / cloud-*).
* :class:`Transport` — the three transports + the local-big extension point.
* :func:`resolve_model` — the ONE tier→model table.
* :func:`select_transport` / :func:`transport_for_profile` — routing.
* :class:`LlmRequest` / :func:`dispatch` — the seam.
* :class:`LlmResult` + ``result_from_*`` — the normalized result.
"""

from __future__ import annotations

from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Tier,
    Transport,
    dispatch,
    resolve_model,
    result_from_agent,
    result_from_claude_p,
    result_from_openai,
    select_transport,
    transport_for_profile,
)

__all__ = [
    "LlmRequest",
    "LlmResult",
    "Tier",
    "Transport",
    "dispatch",
    "resolve_model",
    "result_from_agent",
    "result_from_claude_p",
    "result_from_openai",
    "select_transport",
    "transport_for_profile",
]
