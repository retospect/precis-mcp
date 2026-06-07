"""The dreaming agent loop (in-process, OpenAI-compatible / litellm).

Drives a local model (default the ``qwen-heavy`` litellm alias) over the
OpenAI ``/v1/chat/completions`` wire with ``tools=``, dispatching each
tool call back through the in-process :class:`~precis.runtime.PrecisRuntime`
and handlers. No MCP socket, no subprocess.

See ``docs/design/dream-agent-loop.md`` and ADR 0024. The whole feature
is gated off by default (``PRECIS_DREAM_LLM``); the ``dream`` pass runs
only via ``precis worker --only dream`` (scheduled), never in the
default worker pass set.

The HTTP client mirrors :class:`~precis.embedder.RemoteEmbedder`: a
stdlib ``urllib`` round-trip behind an injectable :data:`Transport`
seam, so tests script the model's tool-calls offline and no third-party
HTTP dep is required.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from precis.store import as_dream_actor

log = logging.getLogger(__name__)

# A callable taking ``(method, url, json_body, timeout)`` and returning
# ``(status_code, parsed_json)``. Default uses ``urllib``; tests inject
# a fake that returns scripted assistant turns.
Transport = Callable[
    [str, str, "dict[str, Any] | None", float], "tuple[int, dict[str, Any]]"
]

#: Verbs routed straight through ``runtime.dispatch``.
_DISPATCH_TOOLS = frozenset({"search", "get", "put", "link", "tag"})
#: Handler-method tools (not global MCP verbs — the dream loop is their
#: gated surface).
_HANDLER_TOOLS = frozenset({"supersede", "acquire"})
#: Write verbs whose success flips a run's outcome to ``wrote``.
_WRITE_TOOLS = frozenset({"put", "link", "tag", "supersede", "acquire"})


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


# ── config ──────────────────────────────────────────────────────────


@dataclass
class DreamConfig:
    """Env-driven knobs for one dream run. All default-off / local."""

    enabled: bool = False
    url: str = "http://127.0.0.1:4000/v1"
    model: str = "qwen-heavy"
    api_key: str = "dummy"
    max_turns: int = 12
    timeout: float = 120.0
    region_n: int = 12
    sparks_n: int = 4
    acquire_enabled: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DreamConfig:
        e = env if env is not None else dict(os.environ)
        return cls(
            enabled=_truthy(e.get("PRECIS_DREAM_LLM")),
            url=e.get("PRECIS_DREAM_LLM_URL") or cls.url,
            model=e.get("PRECIS_DREAM_MODEL") or cls.model,
            api_key=e.get("PRECIS_DREAM_LLM_KEY") or cls.api_key,
            max_turns=int(e.get("PRECIS_DREAM_MAX_TURNS") or cls.max_turns),
            timeout=float(e.get("PRECIS_DREAM_TIMEOUT") or cls.timeout),
            region_n=int(e.get("PRECIS_DREAM_REGION_N") or cls.region_n),
            sparks_n=int(e.get("PRECIS_DREAM_SPARKS_N") or cls.sparks_n),
            acquire_enabled=_truthy(e.get("PRECIS_DREAM_ACQUIRE")),
        )


# ── HTTP transport + client ─────────────────────────────────────────


def _urllib_transport(
    method: str, url: str, body: dict[str, Any] | None, timeout: float
) -> tuple[int, dict[str, Any]]:
    """Default :data:`Transport` — a stdlib ``urllib`` round-trip.

    No third-party HTTP dep so the torch-free worker image stays tiny
    (ADR 0021/0024). Returns ``(status, parsed_json)``.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = {}
    return status, parsed


class LiteLLMClient:
    """Minimal OpenAI ``/v1/chat/completions`` client for the dream loop."""

    def __init__(
        self, config: DreamConfig, *, transport: Transport | None = None
    ) -> None:
        self._config = config
        self._transport = transport or _urllib_transport

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One turn. Returns the assistant message dict (OpenAI shape).

        Raises ``RuntimeError`` on a non-200 or a malformed payload so
        the run loop logs an ``error`` dream rather than crashing the
        worker.
        """
        url = self._config.url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
        }
        # Loopback litellm has no master_key (auth unnecessary), so the
        # stdlib transport sends no bearer — same as RemoteEmbedder.
        status, parsed = self._transport("POST", url, body, self._config.timeout)
        if status != 200:
            raise RuntimeError(f"dream LLM returned HTTP {status}: {parsed!r:.200}")
        try:
            return dict(parsed["choices"][0]["message"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"dream LLM returned a malformed payload: {parsed!r:.200}"
            ) from exc


# ── tool schemas (OpenAI function-calling) ──────────────────────────


def _tool_schemas(acquire_enabled: bool) -> list[dict[str, Any]]:
    """The dream agent's tool surface as OpenAI function definitions."""

    def fn(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "additionalProperties": True,
                },
            },
        }

    tools = [
        fn(
            "search",
            "Search the knowledge base. Use view='dreamable' to re-pull "
            "the focus region, or like=<ref id> with angle (1=near, 0=unrelated, "
            "-1=opposite) for a diverse spray, or q='...' for ordinary search.",
            {
                "q": {"type": "string"},
                "kind": {
                    "type": "string",
                    "description": "e.g. '*', 'paper', 'memory'",
                },
                "view": {"type": "string"},
                "like": {"type": "string"},
                "angle": {"type": "number"},
                "n": {"type": "integer"},
            },
        ),
        fn(
            "get",
            "Fetch one ref or block by id, e.g. id='memory:42' or id='paper:slug'.",
            {"kind": {"type": "string"}, "id": {"type": "string"}},
        ),
        fn(
            "put",
            "Write a new memory note (a synthesis or inspiration). Tag "
            "speculative inspirations with tags=['DREAM:speculative'].",
            {
                "kind": {"type": "string", "description": "usually 'memory'"},
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        ),
        fn(
            "link",
            "Connect two refs, e.g. link='memory:42' relation='related-to'.",
            {
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "link": {"type": "string"},
                "relation": {"type": "string"},
            },
        ),
        fn(
            "tag",
            "Label a ref, e.g. tag a memory DREAM:speculative.",
            {
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        ),
        fn(
            "supersede",
            "Merge 2..10 near-duplicate MEMORIES into one survivor. "
            "Compress-only: new_text may not be longer than the originals "
            "combined. Papers are never merged.",
            {
                "merge_ids": {"type": "array", "items": {"type": "integer"}},
                "new_text": {"type": "string"},
                "new_tags": {"type": "array", "items": {"type": "string"}},
            },
        ),
    ]
    if acquire_enabled:
        tools.append(
            fn(
                "acquire",
                "Queue a missing paper the corpus keeps citing. Pass "
                "identifier='doi:...'|'arxiv:...'|'s2:...' or title=, plus "
                "context_ref_id where it came up.",
                {
                    "identifier": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "context_ref_id": {"type": "integer"},
                },
            )
        )
    return tools


# ── prompt ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT_HEAD = """You are dreaming over a personal knowledge base. Improve it a little.
Before you stop, leave at least ONE small change -- a note, a link, or a
conservative merge. Prefer small over sweeping.

You've been handed a FOCUS region (what's most due for a look, shown in
full) and a few SPARKS (distinct, far-flung items, shown as excerpts).
Sit with the focus; glance at the sparks for an unexpected connection.
Pass the id= values shown in the FOCUS / SPARKS list to get / link / tag
/ supersede those items -- never invent an id. Use get(kind=..., id=...)
to read any item (a spark, a linked neighbour) in full detail before you
act on it.

MAKE at least one change (small is good):
  - put(kind='memory', text=...)   a synthesis or inspiration note
  - link(...) / tag(...)           connect or label (e.g. DREAM:speculative)
  - supersede(merge_ids=[...], new_text=...)  merge near-dup memories (compress only)"""

_SYSTEM_PROMPT_ACQUIRE = (
    "  - acquire(identifier|title, ...) queue a missing paper to fetch"
)

_SYSTEM_PROMPT_TAIL = """When in doubt, make the smallest useful change -- then stop. Stop by
replying with a short closing note and no tool call."""


def _system_prompt(acquire_enabled: bool) -> str:
    """Assemble the system prompt; the ``acquire`` line is included only
    when the tool is actually enabled (otherwise it's dead text the
    model can't act on)."""
    parts = [_SYSTEM_PROMPT_HEAD]
    if acquire_enabled:
        parts.append(_SYSTEM_PROMPT_ACQUIRE)
    return "\n".join(parts) + "\n\n" + _SYSTEM_PROMPT_TAIL


def _excerpt(text: str | None, limit: int = 240) -> str:
    if not text:
        return ""
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


#: Per-item cap for the verbatim FOCUS body. Generous enough that real
#: memories / chunks land whole, but bounds the prompt when a focus item
#: is pathologically long (region_n items shown in full). `get(...)`
#: pulls the untruncated text if the agent needs it.
_FOCUS_BODY_CAP = 2000


def _cap_verbatim(text: str | None, cap: int) -> str:
    """Truncate ``text`` to ``cap`` chars, preserving newlines (verbatim)."""
    t = text or ""
    if len(t) <= cap:
        return t
    return t[: cap - 1].rstrip() + "…"


def _indent(text: str | None, prefix: str = "    ") -> str:
    """Indent each line of ``text`` by ``prefix`` (verbatim, newlines kept)."""
    return "\n".join(prefix + line for line in (text or "").splitlines())


def _format_hits(hits: list[tuple[Any, Any, float]], *, full: bool = False) -> str:
    """Render search hits for the prompt.

    ``full=True`` shows the body **verbatim** (the FOCUS region — the
    agent should sit with it in detail), capped at :data:`_FOCUS_BODY_CAP`
    chars so a pathological item can't blow the prompt. ``full=False``
    shows a single-line excerpt (the SPARKS — just enough to spot a
    connection, `get(...)` pulls the rest).
    """
    if not hits:
        return "(none)"
    lines = []
    for block, ref, score in hits:
        title = ref.title or ref.slug or f"#{ref.id}"
        header = f"- [{ref.kind}] id={ref.id} (cos={score:.2f}) {_excerpt(title, 80)}"
        if full:
            body = _indent(_cap_verbatim(block.text, _FOCUS_BODY_CAP))
        else:
            body = f"    {_excerpt(block.text)}"
        lines.append(f"{header}\n{body}")
    return "\n".join(lines)


# ── tool execution ──────────────────────────────────────────────────


@dataclass(slots=True)
class _RunState:
    turns: int = 0
    tool_calls: int = 0
    wrote: bool = False
    behaviors: set[str] = field(default_factory=set)
    result_ref_ids: list[int] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)


_BEHAVIOR_OF = {
    "put": "synthesize",
    "link": "synthesize",
    "tag": "inspire",
    "supersede": "consolidate",
    "acquire": "acquire",
}


def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    runtime: Any,
    config: DreamConfig,
    state: _RunState,
) -> str:
    """Run one tool call in-process; return rendered text for the model."""
    if name in _DISPATCH_TOOLS:
        body, is_error = runtime.dispatch_with_status(name, args)
        if name in _WRITE_TOOLS and not is_error:
            state.wrote = True
            state.behaviors.add(_BEHAVIOR_OF.get(name, name))
        return body
    if name == "supersede":
        return _run_handler(runtime, "memory", "supersede", args, state, name)
    if name == "acquire":
        if not config.acquire_enabled:
            return "acquire is disabled (set PRECIS_DREAM_ACQUIRE=1 to enable)."
        return _run_handler(runtime, "paper", "acquire", args, state, name)
    return f"unknown tool: {name}"


def _run_handler(
    runtime: Any,
    kind: str,
    method: str,
    args: dict[str, Any],
    state: _RunState,
    name: str,
) -> str:
    """Call a handler method, rendering Response / typed-error like dispatch."""
    from precis.errors import PrecisError

    handler = runtime.hub.handler_for(kind)
    if handler is None or not hasattr(handler, method):
        return f"{name} is unavailable in this deployment."
    try:
        response = getattr(handler, method)(**args)
        state.wrote = True
        state.behaviors.add(_BEHAVIOR_OF.get(name, name))
        return runtime._render(response)
    except PrecisError as exc:
        return runtime.render_error(exc)


# ── the run ─────────────────────────────────────────────────────────


def run_dream_pass(
    store: Any,
    *,
    embedder: Any = None,
    hub: Any = None,
    config: DreamConfig | None = None,
    transport: Transport | None = None,
) -> dict[str, int]:
    """One scheduled dream. Returns ``{claimed, ok, failed}`` for the loop.

    Gated off unless ``config.enabled``. Builds the focus region + sparks,
    runs the agentic turn loop (suppressing its own salience bumps via
    :func:`as_dream_actor`), stamps ``last_dreamt`` on the surfaced
    chunks (the rotation), and records one ``dream_log`` row + transcript.
    """
    config = config or DreamConfig.from_env()
    if not config.enabled:
        return {"claimed": 0, "ok": 0, "failed": 0}

    # Build the in-process runtime (full handler set).
    if hub is None:
        from precis.dispatch import boot

        hub = boot(store=store, embedder=embedder)
    from precis.config import PrecisConfig
    from precis.runtime import PrecisRuntime

    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    client = LiteLLMClient(config, transport=transport)
    state = _RunState()

    try:
        with as_dream_actor():
            seed_id, region = store.dreamable_region(
                kinds=("paper", "memory"), n=config.region_n
            )
            if seed_id is None or not region:
                _record(store, "noop", state, config, seed_id, note="empty corpus")
                return {"claimed": 1, "ok": 1, "failed": 0}

            sparks: list[tuple[Any, Any, float]] = []
            seed_vec = store.get_chunk_vector(seed_id)
            if seed_vec is not None and config.sparks_n > 0:
                member_ids = [b.id for b, _r, _s in region]
                sparks = store.angle_neighbours(
                    seed_vec,
                    angle=0.4,
                    n=config.sparks_n,
                    exclude_chunk_ids=member_ids,
                )

            user = (
                "--- FOCUS REGION (shown in full) ---\n"
                f"{_format_hits(region, full=True)}\n\n"
                "--- SPARKS (excerpts; get(...) for the rest) ---\n"
                f"{_format_hits(sparks, full=False)}"
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": _system_prompt(config.acquire_enabled)},
                {"role": "user", "content": user},
            ]
            tools = _tool_schemas(config.acquire_enabled)

            _agent_loop(client, runtime, messages, tools, config, state)

            # Rotation: stamp the region + sparks + seed so a different
            # region tops next run (docs/design/dreaming.md §Selection).
            touched = [b.id for b, _r, _s in region]
            touched += [b.id for b, _r, _s in sparks]
            if seed_id not in touched:
                touched.append(seed_id)
            store.touch_last_dreamt(touched)

            outcome = "wrote" if state.wrote else "noop"
            _record(store, outcome, state, config, seed_id)
            return {"claimed": 1, "ok": 1, "failed": 0}
    except Exception as exc:  # pragma: no cover — defensive, never crash the loop
        log.warning("dream: run failed: %s", exc, exc_info=True)
        try:
            _record(store, "error", state, config, None, note=str(exc)[:400])
        except Exception:
            log.warning("dream: failed to record error row", exc_info=True)
        return {"claimed": 1, "ok": 0, "failed": 1}


def _agent_loop(
    client: LiteLLMClient,
    runtime: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    config: DreamConfig,
    state: _RunState,
) -> None:
    """Drive turns until the model stops or ``max_turns`` is hit."""
    while state.turns < config.max_turns:
        state.turns += 1
        assistant = client.chat(messages, tools)
        messages.append(assistant)
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            return  # model stopped (closing note, no tool call)
        for call in tool_calls:
            state.tool_calls += 1
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except (ValueError, TypeError):
                args = {}
            result = _execute_tool(
                name, args, runtime=runtime, config=config, state=state
            )
            state.transcript.append({"tool": name, "args": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                }
            )


def _record(
    store: Any,
    outcome: str,
    state: _RunState,
    config: DreamConfig,
    seed_id: int | None,
    *,
    note: str | None = None,
) -> None:
    """Write one ``dream_log`` row + its 1:1 ``dream_transcripts`` sibling."""
    from psycopg.types.json import Jsonb

    summary: dict[str, Any] = {"tool_calls": state.tool_calls, "turns": state.turns}
    if note:
        summary["note"] = note
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO dream_log "
            "  (outcome, behaviors, seed_clusters, result_ref_ids, turns, "
            "   tool_calls, model, cost_usd, summary) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING attempt_id",
            (
                outcome,
                sorted(state.behaviors) or None,
                Jsonb({"seed_chunk_id": seed_id}),
                state.result_ref_ids or None,
                state.turns,
                state.tool_calls,
                config.model,
                0.0,
                Jsonb(summary),
            ),
        ).fetchone()
        attempt_id = int(row[0])
        conn.execute(
            "INSERT INTO dream_transcripts (attempt_id, transcript) VALUES (%s, %s)",
            (attempt_id, Jsonb(state.transcript)),
        )


__all__ = ["DreamConfig", "LiteLLMClient", "Transport", "run_dream_pass"]
