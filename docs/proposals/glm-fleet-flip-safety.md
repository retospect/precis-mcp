# Making the GLM-5.2 / OpenRouter fleet flip safe

- **Status**: proposed (2026-07-25) · **spec, not yet built**
- **Owner**: gripe 171782 (flip blockers)
- **Refs**: ADR 0046 (routing layer) · `docs/proposals/llm-openrouter-bypass.md`
  (items 1–3, shipped) · memory `factory_llm_switch`
- **Precondition already shipped**: the `/factory` GLM/OpenRouter toggle +
  preset (`live_config.GLM_OPENROUTER_PRESET`, `POST /factory/llm`, main
  `6940ed99`); `PRECIS_LLM_BASE_URL` in worker env; `OPENROUTER_API_KEY`
  vaulted. The switch works; the **downstream call-site behavior** does not.

## TL;DR — the gripe's premise was wrong; the real fix is narrower

Gripe 171782 framed blocker (a) as "route the un-forked `claude_agent`
group-B sites (dream/news/briefing) through the backend fork." **A full
call-site census disproves this.** 50+ cloud call-sites already route through
`dispatch()` / `DispatchClient`, which fork on `resolve_backend()` internally.
`dream` *is* forked (`dispatch(tier=CLOUD_SUPER, tools_needed=True)`,
`dream_agent.py:179`); `news_poll` has no LLM at all; `briefing` uses
`DispatchClient`. The **only** genuinely un-forked cloud site is `fix_gripe`
(a raw `claude -p` subprocess) — and it is dark (default-off) and sandboxed.

So the live-test breakage was **not** a transport-fork gap. Flip-window
forensics (`llm_call_log`, 2026-07-25 07:29–08:14, backend flipped to
`openai` + GLM preset) show exactly three failure classes:

| transport | errored | count | call class | root cause |
|---|---|---|---|---|
| `openai_compat` | yes | **395** | classify / summarizer (`LOCAL_SMALL`) | `select_transport` routes `LOCAL_SMALL`→`OPENAI_COMPAT` under `openai`, POSTing the local-only `summarizer` alias to OpenRouter → HTTP 400 |
| `claude_agent` | yes | **4** | `dream` (`CLOUD_SUPER`) | `model=z-ai/glm-5.2` sent to `claude_agent`, `terminal_reason=api_error` — a **backend/model override desync** (see Part 3) |
| `openai_tools` | no | 14 | GLM review + qwen `local-big` | worked, but every row logged `cost_usd=∅` — breaker blind to OpenRouter spend |

The three parts below map onto those three classes. Parts 1 and 2 are
mechanical and high-confidence; Part 3 is the subtle one and is what actually
needs care.

## Part 1 — transparent hosted small model under the flip *(the 395-error fleet-breaker)*

**Symptom.** Under `backend=openai`, `select_transport(LOCAL_SMALL,
tools_needed=False, backend=OPENAI)` returns `OPENAI_COMPAT`
(`router.py:288–291`). The classify / summarizer / paper-glossary /
inject-scan passes all pin `model="summarizer"` (a litellm loopback alias,
resolving to `rake-lemma` — no meaning on OpenRouter) → HTTP 400 on **every**
call. This was ~all of the fleet's error volume during the flip.

**Decision (Reto, 2026-07-25): the flip SHOULD move `LOCAL_SMALL` to a hosted
OSS small model too — transparently.** So the item-2
`LOCAL_SMALL`→`OPENAI_COMPAT` branch **stays**; the bug is only that the model
id sent is the dead local alias. The flip must resolve `LOCAL_SMALL` to a
**real** hosted small model automatically.

**The catch: the alias is pinned at the call-site, so a preset override alone
won't catch it.** `classify`/`summarize` pass `model="summarizer"` *explicitly*
(`worker.py:785`, `740`) to avoid a thinking-model returning empty, and
`dispatch` honors `req.model` over `resolve_model(tier)` (`router.py:933`). So
setting `llm.model.local-small` in the preset is silently ignored. The remap
must fire **where the local alias meets a hosted transport.**

**Fix — a transparent hosted-alias remap.** In `dispatch`, once
`transport` and `model` are resolved: if the transport is a **hosted OSS**
transport (`OPENAI_COMPAT`/`OPENAI_TOOLS` with a hosted `PRECIS_LLM_BASE_URL`,
i.e. not a `local_url` slot) **and** `model` is a known local-only alias
(`summarizer`/`rake-lemma`/`qwen-heavy`), remap it to the configured hosted
small model — resolved `llm.model.local-small` override → env
`PRECIS_LOCAL_SMALL_HOSTED_MODEL` → a compiled default (a real OpenRouter
small model, TBD — e.g. `z-ai/glm-4.7-flash`). Contained: one helper +
one call in `dispatch`. Under default `anthropic`, `LOCAL_SMALL`→`LITELLM`
(no hosted transport, no remap) → byte-identical to today.

**Visibility is what makes "transparent" safe.** Routing the high-volume
per-chunk gloss to a paid endpoint is only acceptable because **Part 2**
captures the cost — so the local-small spend shows up in `llm_call_log` and
the budget breaker, rather than silently accruing. Part 1 and Part 2 ship
together for this reason.

**Orthogonal (not needed for the flip): the direct-local path.** For the
*un-flipped* (default) case, "call the small model directly" (off the litellm
proxy) is the already-built **Phase-2 `served_by` flip** — seed a
`served_by.endpoint` on the small model's `llm` card and
`local_serving.acquire()` hands `dispatch` a `local_url` so `_dispatch_local`
POSTs straight to the host llama-swap endpoint (`local_serving.py:63–76`).
Dark only because no card seeds it yet; **no code change**, an ops/card step.
Independent of this proposal.

**Tests.** A `LOCAL_SMALL` dispatch with `model="summarizer"` under
`backend=openai` + hosted base_url routes `OPENAI_COMPAT` with the **hosted**
model id (not `summarizer`); under default `anthropic` it stays `LITELLM` with
`summarizer` unchanged; a `local_url` slot (served_by) is never remapped.

## Part 2 — meter OpenRouter cost through the `openai_tools` loop *(un-blind the budget breaker)*

**Symptom.** The 14 successful `openai_tools` rows (incl. the GLM review that
ran cleanly) all logged `cost_usd=∅`. The $85/$20 daily/hourly budget breaker
keys on `cost_usd`, so under a real fleet flip it would meter **zero**
OpenRouter spend and never trip.

**Root cause.** `_dispatch_openai_tools` hardcodes `cost_usd=None`
(`router.py:1679`) because the loop's `AgentLoopResult` accumulates
`total_tokens` but never `usage.cost`. (The tool-*less* `_dispatch_openai_compat`
path is already fine — `LlmClient.complete` captures `usage.cost` into
`LlmResult.cost_usd`, `llm_summarize.py:318–329`.)

**Fix** — mirror the existing `total_tokens` accumulation exactly:
1. `ChatTurn` (`openai_tools.py`): add `cost_usd: float | None`, populated
   from `usage.get("cost")` alongside `total_tokens` (line ~216–224).
2. `run_tool_loop`: a `total_cost` nonlocal summed in `_accumulate` (line ~302).
3. `AgentLoopResult`: add `cost_usd: float | None`.
4. `_dispatch_openai_tools` (`router.py:1679`): read `result.cost_usd`
   instead of `None`.

**Tests.** A scripted loop whose turns carry `usage.cost` sums into
`LlmResult.cost_usd`; a `record_call` assertion that the OpenRouter cost
lands in `llm_call_log.cost_usd`.

## Part 3 — backend/model override coherence *(the 4 `dream` errors — the real subtlety)*

**Symptom.** `dream` logged `model=z-ai/glm-5.2` on the `claude_agent`
transport → `api_error`. dream is already forked, so under `backend=openai`
it should have gone to `OPENAI_TOOLS`. It did not.

**Root cause — a half-applied flip desyncs backend and model.** `dispatch`
(`router.py:930–933`) demotes `OPENAI`→`ANTHROPIC` when
`PRECIS_LLM_BASE_URL` is absent from *that process's* env (the ships-dark
safety net) — but it resolves `model = req.model or resolve_model(tier)`
**after** the demotion, and `resolve_model` still returns the `app_settings`
OSS override (`z-ai/glm-5.2`). Result: an Anthropic transport gets an OSS
slug. At test time the base_url env had not reached the melchior agent-worker
process, so its dream ticks demoted to claude-transport while keeping the GLM
model. The two `/factory` overrides (`llm.backend`, `llm.model.<tier>`) are
independent rows, and the safety demotion reverts only one of them.

**Fix — make the demotion atomic.** When `dispatch` demotes the backend to
`ANTHROPIC` for a missing base_url, it must also **ignore the OSS model
override** and resolve the tier's Anthropic default (env → compiled
`_TIER_MODEL`). Cleanest shape: teach `resolve_model` (or a dispatch-local
resolve) to take the *effective* backend, and only honor an
`app_settings` model override whose value is coherent with that backend —
under `ANTHROPIC`, a non-claude override slug is dropped in favor of the
default. Net invariant: **no base_url ⇒ full Anthropic (default model +
claude transport), never a mixed OSS-slug-on-claude call.**

This also hardens the everyday case: if the base_url is ever cleared while a
model override lingers (or a worker restarts without the env), the fleet
degrades cleanly to Claude instead of 400-looping.

**Plus the one truly un-forked site: `fix_gripe`.** `fix_gripe.py:432–448`
spawns a raw `claude -p` subprocess with `--model resolve_model(CLOUD_SUPER)`.
Under `backend=openai` it would hand an OSS slug to `claude -p` → break. It is
**dark** (default-off, fed by `backlog_groom`) and intentionally sandboxed
(isolated clone, restricted env), so it is not a *live* blocker — but it must
be gated before any real fleet flip. Minimal fix: `fix_gripe` reads
`resolve_backend()` and, under `OPENAI`, either skips cleanly (logs + no-op,
like a breaker pause) or pins the Anthropic default. Recommend skip-clean;
re-home at the factory revamp. Low priority.

**Tests.** `dispatch` with `backend=openai` + model override set + no
`PRECIS_LLM_BASE_URL` resolves a **claude** model on `claude_agent` (not the
OSS slug); with base_url set it routes `OPENAI_TOOLS` with the OSS slug.
`fix_gripe` under `backend=openai` skips rather than spawning claude with an
OSS slug.

## Explicitly NOT in scope

- **Not** a broad "route group B through dispatch" migration — the census
  shows it is already done for every live site.
- **Not** per-tier or per-call-site backend override (still the global
  `llm.backend` flag; that's the load-bearing constraint — see below).
- **Not** the direct-local `served_by` seed (litellm-retire Phase-2) — that's
  an orthogonal ops step for the *un-flipped* path, not this proposal.
- **Not** re-flipping the fleet to validate. Live validation happens only
  **after** Parts 1–3 land, and even then via a scoped re-flip with a watch,
  not a synthetic paid smoke (avoids a direct paid OpenRouter call; the DB
  revert is instant via the 15 s `live_config` TTL).

## The load-bearing constraint (why there's no "narrow activation" today)

`llm.backend` is a **single global flag**. There is no per-tier or
per-call-site backend selector, so a flip hijacks *every* cloud-tier call at
once — including classify's `LOCAL_SMALL` path. That is why the flip cannot be
"turned on narrowly to test one path": it is a full group-wide fix (Parts
1–3) or nothing. Parts 1 and 3 together make the *global* flip coherent;
per-tier backend routing remains a separate, larger design (reframed in
`OPEN-ITEMS.md`, out of scope here).

## Acceptance criteria

1. A `LOCAL_SMALL` dispatch pinning `model="summarizer"` under
   `backend=openai` + hosted base_url routes `OPENAI_COMPAT` with the
   **hosted** small-model id (not `summarizer`); under default `anthropic` it
   stays `LITELLM` with `summarizer` unchanged; a `served_by`/`local_url` slot
   is never remapped (Part 1).
2. An `openai_tools` dispatch whose turns report `usage.cost` yields
   `LlmResult.cost_usd != None` and writes it to `llm_call_log.cost_usd`
   (Part 2).
3. `dispatch(backend=openai, model-override set, no PRECIS_LLM_BASE_URL)`
   resolves a **claude** model on `claude_agent` — never an OSS slug on an
   Anthropic transport (Part 3 desync fix).
4. `fix_gripe` under `backend=openai` does not spawn `claude -p` with an OSS
   slug (skips clean or pins Anthropic) (Part 3, dark site).
5. `backend` unset (default `anthropic`) ⇒ every path byte-identical to today
   (all four above are no-ops under the default).
6. Docs updated in the same commit: `state-map.md` LLM-independence section,
   and this proposal's item-2 reversal noted in
   `docs/proposals/llm-openrouter-bypass.md`.

## Recommended build order

1. **Part 1** (revert `LOCAL_SMALL` branch) + **Part 2** (cost capture) —
   independent, mechanical, high-confidence; together they kill the 395-error
   class and un-blind the breaker. Ship first (candidate for the `coder`
   tier).
2. **Part 3** (override-desync coherence) — the correctness-sensitive one,
   touches core `dispatch`/`resolve_model`; keep on the main loop or a
   carefully-specced `coder` round with the invariant test written first.
3. **Part 3 fix_gripe gate** — small, low-priority, can trail.
4. Only then: a **scoped** re-flip + watch to validate live (never before).

## Before re-flipping live — ops checklist

Code landing is necessary but not sufficient; the flip also needs these (the
desync bug was itself an ops-drift symptom):

1. **`PRECIS_LLM_BASE_URL` in *every* worker process env — verified, not
   assumed.** The 4 dream errors happened because it was absent in the
   melchior agent-worker's env at flip time. Part 3 makes that *safe* (clean
   Claude fallback), but for the flip to actually reach OpenRouter it must be
   present everywhere. Verify post-deploy on each host (system + agent
   workers) before flipping.
2. **Pick + validate the hosted small model** (Open Q1) — confirm the chosen
   id resolves on OpenRouter and returns non-empty on a classify/summarize
   prompt.
3. **Budget breaker headroom.** Part 2 will surface real OpenRouter spend
   (incl. the new per-chunk local-small traffic); confirm the $85/$20
   daily/hourly caps are set where you want them before the volume lands.
4. **`OPENROUTER_API_KEY` present in the vault** (already true, 2026-07-14) —
   re-confirm `_provider_api_key` resolves it on each host.
5. **Scoped re-flip plan, not fleet-blind.** Flip via `/factory`, watch
   `llm_call_log` for `errored` + `cost_usd` for a few minutes, revert on any
   anomaly (DELETE the app_settings rows — instant via the 15 s `live_config`
   TTL). Don't walk away from the first flip.
6. **Known-accepted:** the `/factory` POST has no auth (gripe 171512,
   tailnet-trust consciously accepted) — not a blocker, just noted.

## Capstone — retiring litellm (verdict: not yet; this is the path to it)

Assessed 2026-07-25: **litellm cannot be fully retired now.** It is still the
live serving path for the `LOCAL_SMALL` LLM tier — `classify` /
`paper_glossary` / `classify_topics` / `inject_scan` pin `model="summarizer"`,
which routes `LOCAL_SMALL`→`Transport.LITELLM`→ the gateway proxy, and
`llm_call_log` shows those calls succeeding on `transport=litellm` today.
Neither replacement is live: the direct-local `served_by`→llama-swap path is
dark (no card seeds `served_by.endpoint`, and a *small* LLM served on
llama-swap is unconfirmed), and the hosted remap is Part 1 (unbuilt). The
proxy's model config lives in the gitignored deploy overlay (no
`deploy/roles/litellm/` in-repo). The code/docs/memory footprint is ~50 files
+ `deploy/playbooks/06-litellm.yml`.

Retirement is therefore the **capstone of this track**, sequenced:

1. Land **Part 1** (hosted small-model remap) — gives `LOCAL_SMALL` a
   non-proxy path *when flipped*.
2. Seed `served_by.endpoint` for a small model on llama-swap + confirm it
   serves — gives `LOCAL_SMALL` a non-proxy path *when not flipped* (default).
   (Ops/card step, already-built code, `local_serving.py:63–76`.)
3. Verify `classify`/`paper_glossary`/`classify_topics`/`inject_scan` run
   green *off* the proxy on both paths (watch `llm_call_log.transport` ≠
   `litellm`).
4. **Then** retire: delete `06-litellm.yml` + the overlay config, rename
   `Transport.LITELLM`→`LOCAL_COMPAT` (the mechanism is generic OpenAI-compat,
   not litellm-specific), strip litellm from docs, and only *now* clean the
   memory refs (they correctly record litellm as live until this lands —
   purging earlier falsifies them).

Do **not** find-replace litellm out before step 3 verifies a green off-proxy
path: it is load-bearing for the local tier today.

## Open questions for Reto

1. **Part 1 hosted small-model id** — the transparent remap needs a concrete
   default hosted small model. Candidate `z-ai/glm-4.7-flash` (already the
   preset's `cloud-small`); or a cheaper generalist. Needs one pick + a
   confirm it accepts the classify/summarize prompt shape on OpenRouter.
   (Direction itself is decided: transparent remap, item-2 branch stays.)
2. **Part 3 shape** — make `resolve_model` backend-aware (drop an
   incoherent OSS override under `ANTHROPIC`), vs. a narrower dispatch-local
   guard. Recommendation: backend-aware resolve — it fixes the everyday
   base_url-cleared case too, not just the flip.
3. **fix_gripe under openai** — skip-clean vs. pin-Anthropic. Recommendation:
   skip-clean (it's a code-fixer that assumes Claude semantics).
