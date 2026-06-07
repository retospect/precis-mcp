# Dream agent loop — in-process, litellm/OpenAI-compatible

Plan artefact for #8 of the dreaming capability. Extends
`docs/design/dreaming.md` (§The dreaming agent) and supersedes its
step-2 ("launch the `claude` binary") — see ADR 0024.

## What changes vs the original design

The original §agent-loop said: *"Launch the `claude` binary connected to
the precis MCP server."* The cluster decision is to drive a **local
Qwen** (`qwen-heavy` alias → DeepSeek-R1-Distill-70B) behind the
**LiteLLM** proxy on melchior (`http://127.0.0.1:4000/v1`,
OpenAI-compatible, tool-calling confirmed working through llama.cpp).

So the loop runs **in-process** inside `precis worker`, talks the
OpenAI `/v1/chat/completions` wire with `tools=`, and dispatches each
`tool_call` back through the in-process `PrecisRuntime` / handlers
(no MCP socket, no subprocess). This keeps the dream loop in the same
trust/resource boundary as the rest of the worker and reuses the
`as_dream_actor` suppression context directly.

## Module: `src/precis/workers/dream.py`

### Config (`DreamConfig`, env-driven, default-off)

| env | default | meaning |
|-----|---------|---------|
| `PRECIS_DREAM_LLM` | `0` (off) | master gate — loop is a no-op unless truthy |
| `PRECIS_DREAM_LLM_URL` | `http://127.0.0.1:4000/v1` | OpenAI-compatible base |
| `PRECIS_DREAM_MODEL` | `qwen-heavy` | litellm model alias |
| `PRECIS_DREAM_LLM_KEY` | `dummy` | bearer token (loopback proxy ignores it) |
| `PRECIS_DREAM_MAX_TURNS` | `12` | coarse safety net against loops |
| `PRECIS_DREAM_TIMEOUT` | `120` | per-call HTTP deadline (s) |
| `PRECIS_DREAM_REGION_N` | `12` | focus-region size |
| `PRECIS_DREAM_SPARKS_N` | `4` | inspiration sparks (angle spray) |
| `PRECIS_DREAM_ACQUIRE` | `0` (off) | gates the `acquire` tool specifically |

No `--max-budget-usd`: local inference has no per-call USD cost. `turns`
is the backstop; `cost_usd` is logged as `0.0` for local models.

### LLM client (stdlib transport, no new dep)

Mirror `RemoteEmbedder`: a `Transport = Callable[[method, url, body,
timeout], (status, json)]` seam defaulting to a `urllib` round-trip, so
tests inject scripted responses and **no live server / `httpx` dep** is
needed (httpx is only in the `external` extra). One method:
`chat(messages, tools) -> assistant_message_dict`.

### Tools exposed to the agent

OpenAI function schemas for the dream-allowed verbs only. Each maps to
an in-process call:

| tool | target | notes |
|------|--------|-------|
| `search` | `runtime.dispatch("search", …)` | supports `view='dreamable'`, `angle`/`like`/`n`, `q` |
| `get` | `runtime.dispatch("get", …)` | read a ref/block |
| `put` | `runtime.dispatch("put", kind='memory', …)` | additive note (auto-tagged `DREAM:`) |
| `link` | `runtime.dispatch("link", …)` | connect refs |
| `tag` | `runtime.dispatch("tag", …)` | label (e.g. `DREAM:speculative`) |
| `supersede` | `hub.handler_for('memory').supersede(…)` | guarded merge (handler enforces caps) |
| `acquire` | `hub.handler_for('paper').acquire(…)` | gated by `PRECIS_DREAM_ACQUIRE` |

`supersede` / `acquire` are **not** global MCP verbs — the dream loop is
their gated surface (the deferred "MCP/agent-surface exposure" from the
earlier sessions). The tool result handed back to the model is the
rendered `Response` text (or the typed-error text on `BadInput`, so the
model can read and retry).

### The run (`run_dream_pass(store, *, hub, config)`)

1. **Gate.** `config.enabled` false → return `{claimed:0}` (noop; the
   worker loop treats it as "no work").
2. **Build context.** `store.dreamable_region(n=region_n)` → focus
   region (+ seed). `store.angle_neighbours(seed_vec, angle≈0.4,
   n=sparks_n)` → sparks. (Both already shipped.) Empty corpus → log a
   `noop` dream and return.
3. **Wrap the whole run in `as_dream_actor()`** so the agent's own
   `search`/`get` reads don't bump salience (echo-chamber guard).
4. **Turn loop.** Seed messages = system+focus+sparks prompt
   (`docs/design/dreaming.md` §The prompt). Call `client.chat(messages,
   tools)`. If the assistant returns `tool_calls`, execute each, append
   `role:'tool'` results, and loop. If it returns content with no
   tool_calls (or `max_turns` hit), stop.
5. **Rotation.** Stamp `last_dreamt = now()` on every chunk the run
   touched — the focus region + sparks + any chunk whose ref the agent
   pulled via `search`/`get` (collected from tool results' ref ids).
   `store.touch_last_dreamt(chunk_ids)`.
6. **Record.** One `dream_log` row (`outcome` ∈ wrote|noop|error,
   `behaviors`, `result_ref_ids`, `turns`, `tool_calls`, `model`,
   `cost_usd=0`, `summary`) + the full trace into `dream_transcripts`
   (same `attempt_id`, one tx).

`outcome='wrote'` iff a write verb (put/link/tag/supersede/acquire)
succeeded at least once; else `noop`. Any unhandled exception → log an
`error` row (best-effort) and return `{failed:1}` — never crash the
worker loop (matches `chase`).

### Worker wiring

Add `dream` to `precis worker --only` choices. It is **not** in the
default (no `--only`) pass set — dreaming is expensive and explicitly
scheduled (#10 runs `precis worker --only dream --once` on melchior,
where litellm is loopback-local). So `args.only == "dream"` is the only
trigger.

## Testing

`tests/test_dream.py`, fully offline via a fake transport:

- gate off → `{claimed:0}`, no LLM call, no log row.
- one tool_call (`put`) then a stop → `outcome='wrote'`, ref created,
  `dream_log` + `dream_transcripts` rows written, `last_dreamt` stamped
  on the focus region.
- read-only run (only `search`/`get`, no writes) → `outcome='noop'`.
- `max_turns` reached → loop terminates, `outcome` reflects writes so
  far, no infinite loop.
- `acquire` blocked when `PRECIS_DREAM_ACQUIRE` off → tool returns a
  readable "disabled" message, model can continue.
- empty corpus → `noop` log, no crash.

## Out of scope (this change)

- The ansible/launchd kickoff (#10) — separate, depends on this landing.
- HDBSCAN sub-theming (cut, ADR 0023).
- Per-USD budgeting (local inference, not applicable).
