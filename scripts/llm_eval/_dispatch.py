"""Shared eval-harness helpers — the two fixes the slab probe surfaced.

Both address *harness* confounds that libel good models (see memory
``llm_golden_eval``), not tool or model faults:

* :func:`robust_dispatch` — a content-guaranteeing dispatch. Reasoning models
  on OpenRouter draw reasoning tokens from the *same* ``max_tokens`` completion
  budget; on an open-ended prompt that can consume the whole budget, leaving
  ``content`` empty — which the OpenAI client stringifies to the literal
  ``"None"`` (``llm_summarize.LlmClient.complete``). A naive harness scored that
  0. The fix is a *separate* reasoning budget, not merely a bigger cap: retry an
  empty result with reasoning effort pinned ``low`` (bounds reasoning, frees the
  cap for content) and a doubled budget.
* :func:`last_json_object` — a fence- and nesting-tolerant last-JSON parser. The
  shipped ``_parse_last_json_block`` regex matches only ONE level of nesting, so
  it silently drops a nested slab payload (``{ops:[{...},{...}]}`` with a nested
  value is 3 levels); a naive ``{.*}`` greedy parser chokes on ```` ```json ````
  fences + trailing prose. This does a string-aware balanced-brace scan.

Kept in ``scripts/`` (not the product router) — this is eval scaffolding; the
product path already threads ``LlmRequest.effort`` → ``reasoning:{effort}``.
"""

from __future__ import annotations

import json
import time

from precis.utils.llm.router import LlmRequest, LlmResult, Tier, dispatch

#: A model that spent its whole completion budget on reasoning returns
#: ``content=None``, stringified to ``"None"`` (llm_summarize.py). Treat these —
#: and blank / ``null`` — as an empty (not a genuine) answer.
_EMPTY = {"", "none", "null"}


def is_empty(text: str | None) -> bool:
    """True when a reply carries no usable content (blank or the ``"None"``
    the client emits when reasoning ate the whole budget)."""
    return (text or "").strip().casefold() in _EMPTY


def robust_dispatch(
    *,
    tier: Tier,
    prompt: str,
    model: str,
    tools_needed: bool = False,
    max_tokens: int = 4096,
    source: str = "llm_eval",
    tries: int = 3,
) -> tuple[LlmResult, str]:
    """Dispatch ``prompt`` to ``model``, guaranteeing room for *content*.

    First attempt: the model's default reasoning with a generous cap. On a
    transient transport error, or an empty reply (reasoning starved the
    content), retry with reasoning effort pinned ``low`` and a doubled cap — the
    separate-budget lever, via the shipped ``LlmRequest.effort`` →
    ``reasoning:{effort}`` seam. Returns ``(result, note)`` where ``note`` is
    ``"ok"`` / ``"recovered@<n>"`` / ``"empty"`` / ``"error:<msg>"`` so the caller
    can tell a real model failure from a harness one.
    """
    res: LlmResult | None = None
    note = "empty"
    for attempt in range(tries):
        res = dispatch(
            LlmRequest(
                tier=tier,
                prompt=prompt,
                model=model,
                tools_needed=tools_needed,
                max_tokens=max_tokens * (2 if attempt else 1),
                effort="low" if attempt else None,
                source=source,
            )
        )
        if res.error:
            note = f"error:{res.error[:60]}"
            time.sleep(2)
            continue
        if is_empty(res.text):
            note = "empty"
            time.sleep(1)
            continue
        return res, ("ok" if attempt == 0 else f"recovered@{attempt}")
    assert res is not None  # tries >= 1
    return res, note


def last_json_object(text: str) -> dict | None:
    """Parse the last complete JSON object in ``text`` — fence- and
    nesting-tolerant.

    Scans for brace-balanced spans honoring string literals (so a ``}`` inside a
    JSON string can't miscount) and returns the last span that parses to a
    ``dict``. Handles arbitrary nesting and ```` ```json ```` fences / trailing
    prose, unlike the shipped one-level regex or a greedy ``{.*}`` match.
    """
    if not text:
        return None
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append((start, i + 1))
    for s, e in reversed(spans):
        try:
            obj = json.loads(text[s:e])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
