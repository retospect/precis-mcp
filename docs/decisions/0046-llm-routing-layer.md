# 0046 — The LLM routing layer: one seam for model, transport, and result

- **Status**: proposed (2026-07-02) · **unit 4a landed** (the ADR + the
  seam + the resolver + the normalized result + tests;
  `src/precis/utils/llm/router.py`). The call-site migration is a
  **follow-up (unit 4b)** — this unit is additive and behavior-preserving,
  it does **not** rewire any existing caller.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0038 — Prompt assembly & principles](./0038-prompt-assembly-and-principles.md)
    — the **direct sibling**: 0038 consolidated *prompt construction* into
    one assembler + a `Profile` (`AGENT`/`HELPER`) that "maps onto the
    existing `claude_agent` vs `claude_p` choice." This ADR consolidates
    the *other half* — *model selection + transport dispatch + result
    shape* — and aligns its `Tier`/`Transport` vocabulary with that
    `Profile` so the two seams compose (assemble a prompt, then route it).
  - [ADR 0024 — Dream loop runs in-process against litellm, not the
    `claude` binary](./0024-dream-loop-litellm-inprocess.md) (**reversed**)
    — the prototyped-then-abandoned "local model + MCP tools over the
    OpenAI `tools=` wire." That path was reversed onto the `claude`
    binary; this ADR re-scopes wiring it back as the `local-big` tier's
    transport — the explicit **next step**, not this unit.

## Context

Model selection, transport choice, and result parsing are scattered.

**Three transports, three result shapes.**

- `utils/claude_p.py` — one-shot `claude -p`, no tools, parses the *last
  JSON block* from stdout → `ClaudePResult(data, raw_stdout, cost_usd)`.
- `utils/claude_agent.py` — multi-turn agentic `claude -p` with MCP tools,
  parses the trailing `stream-json` **result event** → `AgentResult(
  final_text, cost_usd, duration_s, turns_used)`.
- `workers/llm_summarize.py` `LlmClient` — OpenAI `/v1/chat/completions`
  POST to the loopback litellm proxy (the `summarizer` alias) →
  `LlmResult(text, total_tokens)`.

A caller that wants "run this prompt and give me the text + cost" must
know which of the three it's talking to and unpack a different shape.

**~a-dozen independent model reads.** Model ids are chosen by
`os.environ.get(...)` at each site, with no shared table. Catalogued (the
migration targets for unit 4b):

| # | Site | Env var | Default |
|---|---|---|---|
| 1 | `utils/claude_p.py` `_DEFAULT_MODEL` | `PRECIS_CLAUDE_MODEL` | `claude-haiku-4-5` |
| 2 | `utils/claude_agent.py` `_DEFAULT_MODEL` | `PRECIS_CLAUDE_AGENT_MODEL` | `claude-sonnet-4-6` |
| 3 | `workers/job_types/plan_tick.py` `_model_alias` (opus) | `PRECIS_MODEL_OPUS` | `claude-opus-4-7` |
| 4 | `workers/job_types/plan_tick.py` `_model_alias` (sonnet) | `PRECIS_MODEL_SONNET` | `claude-sonnet-4-6` |
| 5 | `workers/job_types/plan_tick.py` `_model_alias` (haiku) | `PRECIS_MODEL_HAIKU` | `claude-haiku-4-5-20251001` |
| 6 | `workers/llm_summarize.py` `LlmConfig` | `PRECIS_SUMMARIZE_MODEL` | `summarizer` |
| 7 | `workers/dream_agent.py` `_DEFAULT_MODEL` | `PRECIS_DREAM_AGENT_MODEL` | `claude-sonnet-4-6` |
| 8 | `workers/structural.py` `Reviewer.model` | `PRECIS_STRUCTURAL_MODEL` | `claude-opus-4-7` |
| 9 | `workers/deep_review.py` `Reviewer.model` | `PRECIS_DEEP_REVIEW_MODEL` | `claude-opus-4-7` |
| 10 | `workers/review.py` (per-reviewer override read) | `PRECIS_<NAME>_MODEL` | `Reviewer.model` |
| 11 | `workers/job_types/fix_gripe.py` `FixGripeConfig` | `PRECIS_FIX_CLAUDE_MODEL` | `claude-opus-4-7` |
| 12 | `workers/job_types/structure_propose.py` | `PRECIS_STRUCTURE_PROPOSE_MODEL` | *(unset → `claude_agent` sonnet default)* |
| 13 | `utils/tex_llm_fix.py` (Layer-2 fixer) | `PRECIS_MODEL_SONNET` | `claude-sonnet-4-6` |
| 14 | `precis_web/ask.py` (follow-up) | `PRECIS_FOLLOWUP_MODEL` → `PRECIS_DREAM_AGENT_MODEL` | *(chain → sonnet)* |
| — | `precis_web/routes/env.py` | *(mirrors 7/8/9/fix for the env UI)* | — |

The pattern is obvious once tabled: three model **families** — opus /
sonnet / haiku — plus the local `summarizer` alias, each pinned in several
places. The `PRECIS_MODEL_{OPUS,SONNET,HAIKU}` triad in `plan_tick` is the
most deliberate (it pins a model *id* so a `LLM:opus` tag binds to one
generation as the CLI default drifts); the rest largely re-derive the
same values.

**Three rogue subprocess sites.** `utils/tex_llm_fix.py` (#13),
`workers/job_types/fix_gripe.py` (#11), and the `structure_propose` /
`ask` paths build `claude -p` argv or pick models *outside* the two
wrappers, so a flag or auth fix in `_claude_subprocess.py` doesn't reach
them.

**No cost governance on `plan_tick`.** The planner mints ticks with a
model alias but no shared budget/telemetry surface; cost lands per-site.

## Decision

Add **one routing seam** — `src/precis/utils/llm/` — that owns model
selection, transport dispatch, and result normalization. It **wraps** the
three existing wrappers; it does not reimplement them. Callers move to it
incrementally (unit 4b); this unit only builds the seam.

### The tier model

A **`Tier`** names *what a task needs* (capability + tools), decoupled
from *which model* runs it. Five tiers, mapped onto capability + tool
need, aligned with the 0038 `Profile`:

| Tier | Capability / tools | Profile | Transport |
|---|---|---|---|
| `local-small` | tool-less local completion | HELPER | litellm `LlmClient` |
| `local-big` | local model **+ MCP tools** | AGENT | *(next step — see below)* |
| `cloud-small` | cloud haiku, one-shot JSON judge | HELPER | `claude_p` |
| `cloud-mid` | cloud sonnet, agentic default | AGENT | `claude_agent` |
| `cloud-super` | cloud opus, heavy reasoning + tools | AGENT | `claude_agent` |

The prompt's proposed capability vocabulary — **`local-small` /
`local-big` / `cloud-super`** — is the spine: small = tool-less
completion; big & super = MCP tools. The two intermediate cloud rungs
(`cloud-small`, `cloud-mid`) are kept because the *current* corpus of
call sites uses all three cloud families (haiku / sonnet / opus), and the
resolver must reproduce each byte-for-byte for a behavior-preserving 4b.

**Alignment with `Profile` (ADR 0038 §4).** A `HELPER` profile (tool-less,
one-shot, structured output) rides the tool-less transports (`claude_p`
for cloud, litellm for local); an `AGENT` profile (tools, multi-turn)
rides `claude_agent` (and, later, the local-big tools transport). The
seam exposes `transport_for_profile(profile, tier)` so this mapping is
explicit rather than re-derived at each site.

### The tier → model resolver (the ONE consolidation point)

`resolve_model(tier) -> str` is the single place model selection lives.
Each tier reads the *existing* env var with the *existing* default, so a
migrated caller resolves to the same id it uses today:

| Tier | Env var | Default |
|---|---|---|
| `cloud-super` | `PRECIS_MODEL_OPUS` | `claude-opus-4-7` |
| `cloud-mid` | `PRECIS_MODEL_SONNET` | `claude-sonnet-4-6` |
| `cloud-small` | `PRECIS_MODEL_HAIKU` | `claude-haiku-4-5-20251001` |
| `local-small` | `PRECIS_SUMMARIZE_MODEL` | `summarizer` |
| `local-big` | `PRECIS_LOCAL_BIG_MODEL` | `qwen-heavy` |

The cloud triad is `plan_tick`'s pinned set — the most intentional of the
scattered reads. The sonnet (`claude-sonnet-4-6`) and opus
(`claude-opus-4-7`) defaults are **shared verbatim** by every other cloud
site (dream #7, tex-fix #13, reviewers #8/#9/#10, fix-gripe #11), so those
migrate byte-for-byte. `local-small` reads `PRECIS_SUMMARIZE_MODEL`
default `summarizer` exactly as `LlmConfig.from_env` (#6). `local-big`
names ADR 0024's dream alias so the seam is complete even though that
transport isn't wired yet.

**One deliberate reconciliation.** `claude_p`'s legacy default (#1,
`claude-haiku-4-5`, no date suffix) differs from `plan_tick`'s dated haiku
pin (#5, `claude-haiku-4-5-20251001`). Both are the same haiku family;
`cloud-small` folds onto the **dated pin** (the more correct binding).
This is the *only* site whose default changes under 4b, and it is called
out here so the migration is a conscious choice, not a silent drift.

An **import-time totality assert** (`set(_TIER_MODEL) == set(Tier)`)
guards the table, mirroring the `TodoView` totality assert in
`handlers/todo.py`: adding a tier without a model is a load-time failure,
not a `KeyError` at dispatch.

### The single dispatch seam

`dispatch(LlmRequest) -> LlmResult`. An `LlmRequest` carries the prompt /
messages + `tier` + `tools_needed` + budget (`max_usd`) + `timeout_s` +
the `claude_agent` pass-through knobs. `select_transport(tier,
tools_needed)` — a pure function — picks the transport; `dispatch` wraps
the corresponding helper and normalizes the output. Local tiers route to
their local transport regardless of `tools_needed` (`local-small` is
tool-less by construction); cloud tiers split on `tools_needed`
(the `AGENT`/`HELPER` split). A caught `ClaudeProcessError` (or local
`RuntimeError`) is folded into `LlmResult.error` rather than raised, so
every path returns one shape; partial stdout (a recoverable-exhaustion
answer) is preserved as `text`.

### The normalized result

`LlmResult(text, cost_usd, turns_used | None, model, tier, error | None)`
unifies the three output shapes, with an adapter from each wrapper:

- `result_from_agent(AgentResult)` — `final_text` → `text`, `turns_used`
  carried, `cost_usd` from the stream-json result event.
- `result_from_claude_p(ClaudePResult)` — `raw_stdout` → `text` (the JSON
  block lives inside; `res.data` stays reachable for a caller that wants
  the dict), `turns_used = None`.
- `result_from_openai(...)` — the litellm `LlmClient.complete` result's
  `text` → `text`; `cost_usd = None` (the loopback proxy reports tokens,
  not dollars).

This gives `plan_tick` (and every other site) one place to read cost +
model for the governance/telemetry the current per-site reads lack.

### MCP-tools-per-tier plan (the next step)

`Transport.LOCAL_BIG_TOOLS` is a **documented, deliberately unimplemented**
branch — `dispatch` raises `NotImplementedError` there and the code
comment cites this ADR + 0024. The plan: a local OpenAI client
(`qwen-heavy` on the loopback proxy) with `tools=` populated from the MCP
config plus a tool-call loop, normalized into `LlmResult` like the rest —
i.e. **re-landing ADR 0024's reversed in-process-litellm-with-tools path**,
this time behind the shared seam instead of a bespoke dream loop. Cloud
tiers already carry MCP tools via `claude_agent`'s `--mcp-config`; the
seam leaves `local-big` as the one open rung. **Out of scope for this
unit** — the seam only reserves the place.

## Consequences

- **One model table.** Bumping a generation is a one-line edit in
  `_TIER_MODEL`; today it is ~a-dozen edits.
- **One result shape.** Callers stop unpacking three different dataclasses.
- **4b is mechanical + behavior-preserving.** Each call site swaps its
  `os.environ.get` + wrapper call for a `dispatch(LlmRequest(tier=…))`,
  and the resolver tests pin the exact defaults each site must keep
  (except the one flagged haiku reconciliation).
- **Governance hook exists.** `dispatch` is the natural chokepoint for a
  shared budget/telemetry pass over `plan_tick` and friends — a follow-up,
  but now there is a single seam to add it to.
- **The rogue subprocess sites get a home.** tex-fix / fix-gripe /
  structure-propose can route through `dispatch` in 4b, inheriting the
  `_claude_subprocess.py` flag/auth fixes automatically.
- **Additive.** No existing call site changes in this unit; nothing's
  model / transport / cost behavior moves.
