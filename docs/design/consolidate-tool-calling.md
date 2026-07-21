# Consolidate tool-calling onto one router-governed substrate

> Design-of-record for collapsing the two ways an internal agentic pass
> drives the precis verbs — the `claude -p` + MCP-stdio subprocess (method
> A) and the in-process OpenAI-`tools=` loop (method B) — onto **one**
> backend-agnostic path, reached only through `router.dispatch`. Companion
> to ADR 0024 (the OSS tool loop) and ADR 0046 (the LLM-independence
> router). Present-tense where built; explicit about what is deferred. The
> decisions log at the bottom is authoritative.

## 0. Thesis

**Nothing internal talks to a model except through `router.dispatch`.** The
router already owns model selection (`resolve_model`), the backend switch
(`resolve_backend`), the spend breaker (`gate_tier`), the route-log
(`llm_call_log`), and `served_by`/local-slot placement. Every call site that
hand-builds a `claude -p` command or news up an `LlmClient` bypasses one or
more of those. The MCP server (`server.py`) stays — it is the *product*
surface for external clients (Cursor/Windsurf/Claude Desktop) — but internal
passes stop shelling to `claude`.

## 1. The two methods today

| | **A — claude + MCP subprocess** | **B — in-process OSS tools loop** |
|---|---|---|
| Entry | `utils/claude_agent.py::call_claude_agent` (router `CLAUDE_AGENT`) | `utils/llm/openai_tools.py::run_tool_loop` (router `OPENAI_TOOLS`) |
| Transport | `claude -p` binary | any OpenAI-compatible endpoint (local qwen / OpenRouter) |
| Tool schema | `TOOL_REGISTRY` → FastMCP (`server.py::_register_tools_from_registry`) | `TOOL_REGISTRY` → OpenAI `tools=` (`utils/llm/precis_tools.py::precis_tool_specs`) |
| Execution | claude → MCP stdio → **separate** MCP server proc → `runtime.dispatch` | model `tool_call` → **in-process** `runtime.dispatch` (no socket) |
| Permissions | claude flags (`--permission-mode`, `--disallowed-tools`) + envelope deny-list | DB role + kind-gate; no claude layer |
| Transcript/cost | scraped from claude `stream-json` result events | assembled in the loop; cost from `usage` |

**Schema is already one source** (`TOOL_REGISTRY`). The divergence is the
loop + permission model + transcript/cost accounting, **not** the tool set.

## 2. Inventory — every LLM call site (audit @ `HEAD`)

### A. Bypass the router entirely — hand-built `claude -p`
- `workers/job_types/plan_tick.py::run` — `--permission-mode acceptEdits`; **live outage** (MCP tool calls denied, tick halts with exit 0). Also calls `run_oss_tool_loop` directly (bypassing `dispatch`).
- `workers/job_types/fix_gripe.py` — `--dangerously-skip-permissions`; model via `resolve_model`, execution off-router.
- `fixer/tick.py` — `--dangerously-skip-permissions`; repo-dev fixer daemon (`com.precis.fixer`).
- `utils/tex_llm_fix.py` — tex-compile auto-fix (sonnet).

### B. Bypass `router.dispatch` — direct litellm `LlmClient` to the proxy
Miss the breaker, route-log, and `served_by` placement.
- `workers/llm_summarize.py` — per-chunk summarizer (the canonical one).
- `workers/classify.py` — chunk classify.
- `workers/briefing.py` — morning briefing (claude-opus alias).
- `reading/meditation.py` (+ `reading_brief`, `card_forge` via the reading family).

### C. Already router-compliant (`dispatch(LlmRequest(...))`) — no work
`quest/tick`, `workers/_chase_llm`, `workers/deep_review`, `workers/job_types/{cad_discuss,cad_propose,structure_propose,good_search}`, `mermaid/turn`, `anki/fix`. The transport helpers (`call_claude_agent`, `call_claude_p`, `run_oss_tool_loop`) are each reached from **only** their router provider.

### Excluded (not generative)
- `workers/executors/agent_container.py` — the container wrap *inside* `call_claude_agent` (router path), not a bypass.
- `utils/claude_quota.py` — a `claude -p quota` probe.

## 3. Target — one ring

Make **B the single internal tool-calling substrate**; the LLM is just a
transport the router picks. B already runs on the canonical
`runtime.dispatch`, is backend-agnostic, and reaches claude via OpenRouter
(or a thin anthropic-native `tools=` adapter) — so "run this pass on claude"
becomes a router transport choice, *still through loop B*.

Invariants the consolidation must hold:
- **One schema source**: `TOOL_REGISTRY` feeds both the MCP registration and
  the OpenAI `tools=` specs (already true — keep it, delete no second schema).
- **One permission model**: the DB-role + kind-gate becomes the sole
  enforcement for internal passes; claude deny-flags retire internally. The
  per-todo `Envelope` maps to (DB role, kind-disable set), not to claude
  flags.
- **One transcript + cost path**: the loop writes the agentlog/transcript
  and the `llm_call_log` row regardless of transport.
- **MCP server untouched** as the external product surface.

## 4. Migration sequence

1. **`plan_tick` → router, both transports (DONE).** The bespoke `claude -p`
   spawn is gone; `run()` picks the transport via `select_transport(tier,
   tools_needed=True, backend)` and delegates:
   - **`CLAUDE_AGENT`** (the default ANTHROPIC backend) → `_run_claude_tick`
     dispatches through `router.dispatch` as a real `claude -p` agent (MCP
     tools, OAuth Max subscription). This restores `claude -p` as a
     *router-selectable* transport. `call_claude_agent` defaults to
     `bypassPermissions` — the fix for the original incident (the bespoke
     spawn used `acceptEdits`, which auto-approves *edits* only, so every MCP
     *tool* call was denied and the tick halted). The subprocess can't read the
     in-proc ContextVar, so the tick's context is threaded via a new
     `LlmRequest.env_overlay` (`PRECIS_CURRENT_TODO`/`_MODEL`/`PRECIS_WORKSPACE`/
     agentlog id/`PRECIS_KINDS_DISABLED`) + a `LlmRequest.cwd` neutral cwd (ADR
     0051 §12). `AgentResult.raw_stdout`/`terminal_reason` → `LlmResult.raw_text`/
     `terminal_reason` carry the stream + resume signal back through the router.
   - **`OPENAI_TOOLS`** (`PRECIS_LLM_BACKEND=openai`) → `_run_oss_tick` drives
     the in-process OSS `tools=` loop, binding context in a `TickContext`
     ContextVar.
   No model pin in `plan_tick` — the tier resolves per backend. `REQUIRES` is
   `{claude_bin, mcp_config}` again (provided by `claude_inproc`; harmless on
   the OSS branch). **Both branches now route through `router.dispatch`** — the
   OSS branch too, so it gains the breaker gate + route-log the claude branch
   has: `LlmResult` carries the OSS loop's `stop_reason` (`_dispatch_openai_tools`
   sets it), and `_run_oss_tick` wraps `dispatch(...)` in the `tick_context`
   block so the loop still runs with the ContextVar bound. The **draft prose-file
   kind-gate is re-homed on both transports** from one source
   (`_draft_prose_kind_gate`): the claude path folds it into the `env_overlay`'s
   `PRECIS_KINDS_DISABLED` (the subprocess MCP server honors it at construction),
   and the OSS path folds it into `TickContext.disabled_kinds` — a **per-call**
   gate in `runtime._resolve_handler` (via `_tick_disabled_hint`), because the
   in-process Hub is built once at boot and its `PRECIS_KINDS_DISABLED` gate is
   construction-time, so a per-tick prohibition can't be an env var here. No-op
   outside a tick (the ContextVar is unset for the MCP server / CLI / tests).
2. **Family A → router**: `fix_gripe`, `fixer/tick`, `tex_llm_fix` stop
   hand-building `claude` commands; each dispatches through the router.
3. **Family B → router**: `llm_summarize`, `classify`, `briefing`,
   `reading/*` route through `router.dispatch`. The router's
   capability-aware `served_by` placement pins a single-served model to its
   host, so llama.cpp prefix-cache locality is preserved (it is the
   placement, not a threat to it).
4. **Retire the internal claude subprocess path**: once A+B are through the
   router, `call_claude_agent`'s internal callers are gone; keep it only if a
   deployment still selects the `CLAUDE_AGENT` transport, else fold claude in
   behind loop B via OpenRouter.

## 5. Open questions

- **Anthropic through loop B**: OpenRouter's anthropic models over the
  `tools=` wire, or a native anthropic `tools=` adapter behind
  `OpenAIToolsProvider`? Decides whether `CLAUDE_AGENT` can be retired
  outright.
- **Envelope translation**: the exact map from `workers/envelope.py` tier-1
  deny-list → (DB role, `PRECIS_KINDS_DISABLED`) for the in-process loop.
- **Transcript fidelity**: the executor parses claude `stream-json` for
  resume reasons (`max_turns`, budget); confirm the OSS loop's
  `AgentLoopResult.stop_reason` carries the same signals for every consumer,
  not just `plan_tick`.

## Decisions log

- **2026-07-20** — Consolidate onto method B (in-process OSS `tools=` loop);
  MCP server stays as the external product surface only. Everything internal
  goes through `router.dispatch`; no call site hand-builds a `claude` command
  or news up an `LlmClient`.
- **2026-07-20** — `plan_tick` is step 1 (live outage), not a throwaway fix.
- **2026-07-20** — Family B (summarizer included) moves too: router
  `served_by` placement *is* the prefix-cache locality guarantee.
- **2026-07-20** — Step 1 *first* shipped as a full fold (claude branch
  deleted), then **reversed** on the user's call: `claude -p` is restored as a
  **router-selectable transport**, not dropped. The deferred
  `env_overlay`/`cwd`/`raw_stdout` seam on `call_claude_agent` is now built, and
  `plan_tick` branches on `select_transport` (claude via `dispatch` under
  ANTHROPIC, OSS loop under OpenAI). Rationale: the OAuth Max subscription
  (no per-token billing) is worth keeping the subprocess for; routing it
  *through* the router (not a hand-built command) is what the consolidation
  actually wants. Reaching claude via loop B (OpenRouter) stays a *future*
  option (§5), not a replacement for the subscription-billed agent.
- **2026-07-20** — The claude tick's resumable-exhaustion semantics are
  preserved through the router: `call_claude_agent` swallows a recoverable
  non-zero exit but now surfaces `terminal_reason`, which `_claude_exit` maps
  to the executor's resume signals (`max_turns`/`budget`/`timeout`), plus a
  breaker-pause → resumable `paused`.
- **2026-07-20** — The OSS branch's dispatch bypass is closed too: it now routes
  through `router.dispatch` (breaker + route-log), gated by `LlmResult` carrying
  the loop `stop_reason`. The draft prose-file kind-gate — previously claude-only
  (an `env_overlay` entry) — is re-homed on the in-process runtime as a per-call
  `TickContext.disabled_kinds` check in `runtime._resolve_handler`, since the
  long-lived in-proc Hub can't be re-gated by an env var per tick. Both
  transports now derive the gate from one `_draft_prose_kind_gate` source.
