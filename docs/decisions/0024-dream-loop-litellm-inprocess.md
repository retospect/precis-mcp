# ADR 0024 — Dream loop runs in-process against litellm, not the `claude` binary

- **Status**: **accepted** (2026-06) — implemented in
  `src/precis/workers/dream.py`. Tracks
  `docs/design/dream-agent-loop.md`; supersedes step 2 of
  `docs/design/dreaming.md` §"The dreaming agent".
- **Deciders**: Reto + agent
- **Relates to**: [ADR 0020 — embedder as service](./0020-embedder-as-service.md)
  (same stdlib-transport client pattern),
  [ADR 0023 — dreamable no clustering dep](./0023-dreamable-no-clustering-dep.md).

## Context

`docs/design/dreaming.md` originally specified the dream agent as a
launch of the **`claude` binary** connected to the precis MCP server
over a socket, gated by `PRECIS_DREAM_LLM` with a `--max-budget-usd`
cost cap.

The cluster now runs a local inference stack: LiteLLM proxy on melchior
(`127.0.0.1:4000`, loopback), backend `llamacpp` (llama.cpp / llama-swap),
exposing OpenAI-compatible `/v1` with working tool-calling. The chosen
dream model is the `qwen-heavy` alias (DeepSeek-R1-Distill-Llama-70B).
`precis worker` already runs on every node. Driving a *cloud* `claude`
subprocess for a background janitorial task is both unnecessary cost and
an awkward second trust/resource boundary (cf. ADR 0022's concern about
`job_claude_inproc` co-scheduling).

## Decision

Run the dream loop **in-process inside `precis worker`**, speaking the
OpenAI `/v1/chat/completions` wire with `tools=` to the litellm proxy,
and dispatch each returned `tool_call` directly through the in-process
`PrecisRuntime` / handlers — no MCP socket, no subprocess.

- **HTTP client**: a stdlib `urllib` round-trip behind an injectable
  `Transport` seam, identical in shape to `RemoteEmbedder` (ADR 0020).
  No new top-level dependency (`httpx`/`openai` stay out of core; httpx
  remains an `external`-extra concern only).
- **Cost cap → turn cap**: local inference has no per-call USD cost, so
  `PRECIS_DREAM_MAX_TURNS` is the backstop and `cost_usd` is logged as
  `0.0`. `--max-budget-usd` is dropped for the local path.
- **`supersede` / `acquire`** stay handler-only methods; the dream loop
  is their gated agent surface (not promoted to global MCP verbs).
- **Default-off**: `PRECIS_DREAM_LLM` gates the whole loop;
  `PRECIS_DREAM_ACQUIRE` gates the `acquire` tool specifically. The
  `dream` pass is **not** in the default worker pass set — it runs only
  via `precis worker --only dream` (scheduled, #10).

## Alternatives considered

- **`claude` binary over MCP (original design).** Rejected for the
  default path: cloud cost for a background task, a second process/trust
  boundary, and a socket round-trip when the worker already holds an
  in-process runtime. Could still be wired later behind
  `PRECIS_DREAM_MODEL=claude-*` if a cloud dream is ever wanted — the
  loop is model-agnostic over the OpenAI wire (litellm exposes Claude
  tiers too), so this is not a one-way door.
- **`openai` python SDK.** Rejected: a new top-level dependency for one
  POST. The stdlib transport seam already proven by `RemoteEmbedder`
  covers it and keeps the torch-free worker image tiny.
- **Native MCP client loop.** Rejected: the worker already constructs
  the handlers in-process; an MCP socket would only add latency and a
  serialization boundary.

## Consequences

- **Positive**: zero new deps; same trust/resource boundary as the
  worker; no cloud cost; reuses `as_dream_actor`, `dreamable_region`,
  `angle_neighbours`, `supersede`, `acquire` directly; fully offline
  testable via the transport seam.
- **Negative**: tool-calling quality now depends on the local model
  (qwen-heavy) honouring the OpenAI tool schema; weaker than Claude at
  multi-step planning. Mitigated by the deliberately minimal prompt and
  the "one small change" success bar.
- **Neutral**: the `claude`-binary wording in `dreaming.md` is now
  historical; this ADR is the live reference.

## See also

- `docs/design/dream-agent-loop.md`
- `docs/design/dreaming.md` (§The dreaming agent — superseded step 2)
- Cluster: `~/work/cluster/roles/litellm` (`litellm_port: 4000`,
  `litellm_backend: llamacpp`, model aliases `qwen` / `qwen-heavy`).
