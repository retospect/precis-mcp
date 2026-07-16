"""The agentic drawer — L3 of the diagram-propose design.

Where the web draw-with-me loop injects a **single-shot** model call
(``figure.turn._default_claude``: prompt → reply JSON), the autonomous tick
injects **this**: a ``claude -p`` session that can ``search`` / ``get`` the
corpus (and reach external kinds — ``perplexity-research``, ``websearch``,
``wikipedia``) through the precis MCP tools *before* it draws, then returns the
same reply JSON the loop expects. So a diagram commissioned by a document can
find and bind its own sources instead of being handed them.

Transport (Reto, 2026-07-15): **``claude -p`` + MCP tools through the
``call_claude_agent`` wrapper**, reached via ``dispatch(tools_needed=True)`` so
the ADR-0046 LLM seam still owns model / backend selection. The in-process
``runtime.dispatch`` bridge (no MCP socket) is the convergence target, deferred
behind the same seam.

``run_turn`` is unchanged — its ``claude_fn`` seam is exactly the injection
point. The web loop keeps its single-shot fn; the tick passes
:func:`build_agentic_claude_fn`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from precis.diagram.turn import ClaudeFn

log = logging.getLogger(__name__)

#: Prepended to the assembled turn prompt for an agentic run. The prompt
#: already ends with the JSON reply contract; this tells the model to *use its
#: tools first*. The ground-truth-first stance lives in the document-context
#: block (``diagram.doc_context``); this is the tool-use nudge.
_AGENTIC_PREAMBLE = (
    "You are drawing WITH tools. You have the precis MCP verbs — use them "
    "before you draw:\n"
    "- `search` the corpus (mode='semantic'|'hybrid'|'lexical') and `get` any "
    "chunk (e.g. get(kind='draft', id='dc123', args={'extent':'verbatim'})) to "
    "read the document this figure illustrates and confirm what each part looks "
    "like;\n"
    "- go external only for drawing *craft* the corpus lacks: "
    "get(kind='perplexity-research', q='how X is drawn in a patent figure'), "
    "search(kind='websearch'|'wikipedia', q=...).\n"
    "Read enough to draw faithfully, then STOP calling tools and reply with the "
    "single JSON object specified at the end of this prompt (and nothing else). "
    "Bind every element you commit to the chunk it depicts via the `links` "
    "field. Do NOT put(kind='figure') / edit the figure yourself — returning the "
    "JSON is how the drawing is applied."
)


def _mcp_config_path() -> Path | None:
    """The precis MCP config Path from ``PRECIS_MCP_CONFIG``, or ``None`` (mirrors
    ``workers.review._mcp_config_path``). ``None`` ⇒ the agent runs tool-less —
    it degrades to a single-shot draw rather than failing."""
    raw = os.environ.get("PRECIS_MCP_CONFIG")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def _try_load(s: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _last_json_object(s: str) -> dict[str, Any] | None:
    """The last brace-balanced JSON *object* embedded in ``s`` (an agent's final
    message often wraps the reply in prose or a fence). ``None`` if none parse."""
    dec = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(s[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            found = obj
    return found


def extract_reply_json(text: str) -> dict[str, Any]:
    """Pull the reply object out of an agent's final text. Tolerant: whole-string
    JSON, a ```json fenced block, or the last embedded object. Falls back to
    ``{"reply": <text>}`` so a non-JSON finish surfaces as chat, not silence
    (the loop then changes nothing — a safe no-op)."""
    s = (text or "").strip()
    if not s:
        return {}
    obj = _try_load(s)
    if obj is not None:
        return obj
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m and (obj := _try_load(m.group(1))) is not None:
        return obj
    obj = _last_json_object(s)
    if obj is not None:
        return obj
    return {"reply": s[:1000]}


def build_agentic_claude_fn(
    *,
    source: str = "figure:agentic",
    mcp_config: Path | None | str = "auto",
    max_turns: int | None = None,
    max_usd: float | None = None,
    timeout_s: float | None = None,
    model: str | None = None,
) -> ClaudeFn:
    """A tool-using ``claude_fn`` for :func:`precis.diagram.turn.run_turn`.

    Routes through the ADR-0046 seam (``dispatch(LlmRequest(tools_needed=True,
    …))``) exactly as the structural / deep reviewers do, so backend + model
    selection stays central. ``mcp_config='auto'`` resolves ``PRECIS_MCP_CONFIG``
    (tool-less if unset). The returned fn prepends the tool-use preamble, runs
    the agent, and parses its final text into the loop's reply dict; a seam
    error is raised so the loop degrades to a chat-only turn (nothing lost)."""
    cfg = (
        _mcp_config_path()
        if mcp_config == "auto"
        else (Path(mcp_config) if isinstance(mcp_config, str) else mcp_config)
    )
    _turns = (
        max_turns
        if max_turns is not None
        else int(os.environ.get("PRECIS_FIGURE_MAX_TURNS", "20"))
    )
    _usd = (
        max_usd
        if max_usd is not None
        else float(os.environ.get("PRECIS_FIGURE_MAX_USD", "2.0"))
    )
    _timeout = (
        timeout_s
        if timeout_s is not None
        else float(os.environ.get("PRECIS_FIGURE_TIMEOUT_S", "600"))
    )
    _model = model if model is not None else os.environ.get("PRECIS_FIGURE_MODEL")

    def _agentic_claude(prompt: str) -> dict[str, Any]:
        from precis.utils.llm.router import LlmRequest, Tier, dispatch

        res = dispatch(
            LlmRequest(
                tier=Tier.CLOUD_SUPER,
                source=source,
                prompt=f"{_AGENTIC_PREAMBLE}\n\n{prompt}",
                tools_needed=True,
                mcp_config=cfg,
                model=_model,
                max_turns=_turns,
                max_usd=_usd,
                timeout_s=_timeout,
                output_format="stream-json",
            )
        )
        if res.error:
            raise RuntimeError(res.error)
        return extract_reply_json(res.text or "")

    return _agentic_claude
