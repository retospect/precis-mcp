# Routing local models through the precis router (retiring the litellm proxy)

**Status:** plan (2026-07-19). **Goal:** make precis's own LLM router call the
cluster's self-hosted models **directly** at their OpenAI-compatible `/v1`
endpoints, instead of going through the litellm proxy on `melchior:4000`. This is
the "local-serving flip" the factory design has been building toward, and it is
the load-bearing step that lets litellm retire.

## ⚠️ Reframe: "ollama" → "local models", and ollama is on the way out

The ask was "add the local **ollama** models to our router." Two facts reshape it:

1. **Ollama is being decommissioned.** `deploy/playbooks/99-ollama-purge.yml`
   removes it (Phase 6 of `docs/llamacpp-migration-plan.md`); melchior's host_vars
   tag the balthazar ollama tunnel "(retiring)". The strategic local backend is
   **llama.cpp via llama-swap** (role `deploy/roles/llamacpp`), which litellm
   already fronts through the `qwen`/`qwen-heavy`/`reasoner`/… aliases.
2. **The mechanism is endpoint-agnostic.** Registering a served endpoint is the
   *same* code path whether it points at llama-swap (`:11445/v1`) or ollama
   (`:11434/v1`) — both are authless OpenAI `/v1`. So the plan targets **local
   endpoints** generically; you choose which backend/models to register.

**Recommendation:** make **llama-swap** the primary target (it's the durable
backend and this doubles as the litellm-retire flip). Register **ollama**
endpoints only for models llama-swap doesn't serve yet that you still want
routable — notably spark's `llama3.3:70b` / `devstral` (spark's
`llamacpp_models` is currently `[]`), as an interim until spark migrates.

## How local routing already works (the machinery is built — dark)

Cited to `src/precis/`:

- **No dedicated LOCAL transport.** A self-hosted model rides the **`LITELLM`**
  transport → `LitellmProvider._dispatch_local` (`utils/llm/router.py:543-548,
  970-1006`). Tier→transport: `LOCAL_SMALL→LITELLM`, `LOCAL_BIG→OPENAI_TOOLS`
  (`router.py:240-243`).
- **The endpoint override.** `dispatch()` (`router.py:790-820`) acquires a
  `LocalSlot`; if the slot is `reserved` and has an `endpoint`, it does
  `req = replace(req, local_url=slot.endpoint)` + `model = slot.served_model`.
  `_dispatch_local` then `replace(cfg, url=req.local_url)` (`router.py:987-988`)
  and POSTs `<url>/chat/completions` with `model=<served_model>`
  (`workers/llm_summarize.py:293-299`) — bypassing the `:4000` proxy default.
- **The slot gate.** `local_serving.acquire(model)` returns `None` (dark no-op)
  unless a `resource_slots` row `(host, "llm:<model_id>")` exists
  (`local_serving.py:156`). When it exists, it reserves a slot and enriches it
  with `endpoint`/`served_model` read from the matching `llm` card's `served_by`
  (`local_serving.py:167-219`), matching `served_by.host == this node`
  (`PRECIS_HOST_NAME` or hostname).
- **The card.** `kind='llm'` cards (`handlers/llm.py`, `llm_catalog.py`). Routing
  reads a `served_by` entry: `{host, endpoint, max_parallel, model}` — card-level
  `meta.served_by` or offering-nested. Today the tiers resolve to cards named
  `summarizer` (local-small) and `qwen-heavy` (local-big), both `served_by`-NULL
  → they fall through to litellm.
- **Auth:** `_dispatch_local` never overrides `api_key`; the default `"dummy"`
  bearer is fine (llama-swap and ollama both ignore it).

**So the flip = give a card a `served_by` with an `endpoint`, seed its
`resource_slots` row, and point a tier's model at it.** Nothing else.

## Endpoint inventory (from `deploy/inventory`, 2026-07-19)

Precis workers run on all 4 hosts; `local_serving` matches `served_by.host ==`
the worker's own node, so serve each model from **its own host's loopback**.

| backend | host | endpoint (`served_by.endpoint`) | models (`served_by.model`) |
|---|---|---|---|
| llama-swap ✅ | melchior | `http://127.0.0.1:11445/v1` | `qwen3.6-27b-q8_0`, `qwen3-next-80b-a3b-q4_k_m`, `qwen3.6-27b-q5_k_m` |
| llama-swap ✅ | balthazar | `http://127.0.0.1:11445/v1` | `qwen3.6-35b-a3b-ud-q3_k_m` |
| llama-swap | spark | `http://127.0.0.1:11444/v1` | *(none yet — `llamacpp_models: []`)* |
| ollama ⏳(retiring) | spark | `http://127.0.0.1:11434/v1` | `llama3.3:70b`, `mistral:7b`, `qwen2.5-coder:7b`, `devstral`, `qwen3-coder-next` |
| ollama ⏳ | melchior/balthazar | `http://127.0.0.1:11435/v1` | `qwen3.5:9b` |

(macOS ollama binds loopback only; spark ollama binds `0.0.0.0:11434` but the
spark worker reaches it on `127.0.0.1` — keep it loopback in the card.)

## What needs to change

1. **Schema fix (code) — the one real gap.** `SERVED_BY_KEYS` (`llm_catalog.py:71`)
   is `{host, endpoint, max_parallel}` — it lacks the **`model`** sub-key that
   `local_serving.py:212` reads for the server-side id, and `build_meta`
   (`llm_catalog.py:205-237`) has no card-level `served_by` param. Today's wiring
   only works via raw `store.update_ref(meta_patch=…)` (as tests + reconcile do).
   → Add `"model"` to `SERVED_BY_KEYS`, extend `_validate_served_by`, and give
   `build_meta` a card-level `served_by` param, so cards can be minted through the
   validated `put`/`upsert_card` path. (Small, additive, unit-testable.)
2. **Seed `resource_slots` (deploy flag).** The slot rows are seeded by the
   `llm_reconcile` pass (`llm_reconcile.py:230-303` → `reconcile_llm_served_slots`),
   default OFF. Enable `PRECIS_LLM_RECONCILE_ENABLED` (or run `--only llm_reconcile`)
   so `served_by` cards produce their `(host, "llm:<model_id>")` slot rows.
3. **The cards (data, prod).** For each model to route: an `llm` card whose
   `model_id` is the precis-side handle a tier resolves to, carrying a `served_by`
   with `{host, endpoint, model, max_parallel}` per the table.
4. **Bind the tiers (env).** A tier reroutes only if its resolved model id equals
   a served card's `model_id`. `LOCAL_SMALL → PRECIS_SUMMARIZE_MODEL` (default
   `summarizer`); `LOCAL_BIG → qwen-heavy` (`router.py:166`, `resolve_model`
   `178-194`). Either name the served cards `summarizer`/`qwen-heavy` (reuse
   162070/162071), or set `PRECIS_SUMMARIZE_MODEL` to the new card id.
5. **Verify `PRECIS_HOST_NAME`** on each worker equals the `served_by.host` string
   (`local_serving.py:112-116`) — endpoints are host-scoped.

## Migration slices

- **S0 — inventory + choose targets.** Confirm live model ids per host:
  `ssh <host> 'curl -s 127.0.0.1:<port>/v1/models'` (llama-swap) and
  `ssh <host> ollama list`. Decide llama-swap-only vs also-ollama (see reframe).
- **S1 — schema fix (code, shippable, dark).** `SERVED_BY_KEYS += "model"`,
  `_validate_served_by`, `build_meta(served_by=…)` + tests (extend
  `tests/test_local_serving.py` / `test_llm_catalog`). No behavior change until
  cards + slots exist. Ship via `scripts/ship`.
- **S2 — mint the cards (prod data).** For each target model, create/patch the
  `llm` card with `served_by`. **Prod write** — via `precis put(kind='llm', …)`
  from the CLI/agent worker on the cluster (NOT the read-only session MCP), or a
  seed migration. Start with the two live tiers: repoint `summarizer` (162070) →
  a small local model and `qwen-heavy` (162071) → `qwen3-next-80b-a3b-q4_k_m` on
  melchior.
- **S3 — enable reconcile (deploy).** Flip `PRECIS_LLM_RECONCILE_ENABLED` in the
  overlay `precis_env`, deploy (`scripts/deploy`). Verify `resource_slots` has the
  `(host, llm:<model>)` rows: `scripts/prod-psql "SELECT host, resource FROM
  resource_slots WHERE resource LIKE 'llm:%'"`.
- **S4 — the flip + verify.** With cards+slots live, a `LOCAL_SMALL`/`LOCAL_BIG`
  dispatch now reroutes. **Verify it hits the local endpoint, not :4000:** watch
  the worker log during a summarize pass, or add a one-off probe; confirm the POST
  target URL. Roll back instantly by clearing `served_by.endpoint` (slot stays,
  endpoint null → back to proxy) — no deploy needed.
- **S5 — retire litellm (the pay-off, = the deferred "slice 7").** Once local
  *and* cloud both route natively (cloud already goes direct via
  `claude_agent`/`claude_p` + per-offering endpoints), repoint the remaining
  non-precis litellm consumers — `sortie` (`sortie-env.j2` LITELLM_URL),
  `daily_briefing` (`generate_briefing.py.j2`, likely legacy), monitoring/nginx
  scrapes — then stop + remove litellm (and its redis cache). Separate follow-on;
  not required to route local models.

## Verification

- Unit: extend `tests/test_local_serving.py` (the `_serve_card` template already
  exercises `served_by.endpoint` → `local_url` override) for the new validated
  `put` path + the `model` sub-key.
- Live (S4): a real summarize/dispatch POSTs to `http://127.0.0.1:<port>/v1/chat/
  completions` (worker log), returns a completion, and `:4000` sees no traffic for
  that tier.

## Risks / gotchas

- **Ollama is being purged** — don't wire durable routing to it; prefer llama-swap.
  If you register ollama for spark's big models, treat it as interim and revisit at
  llama-swap Phase 6.
- **Slot gate is silent** — no `resource_slots` row ⇒ `acquire` returns `None` and
  the call quietly goes to litellm. If a flip "does nothing," check the slot row
  (S3) and that `PRECIS_HOST_NAME == served_by.host`.
- **Model-id vs served-model** — the card's `model_id` is the precis-side handle a
  tier resolves to; `served_by.model` is the backend's own tag (e.g. `llama3.3:70b`
  for ollama, `qwen3-next-80b-a3b-q4_k_m` for llama-swap). Keep them distinct.
- **Schema-gap footgun** — until S1 lands, a `put` with `served_by.model` is
  rejected by validation; only raw `update_ref` works. Do S1 first.
- **Prod writes** — S2/S3 mutate prod; the session MCP is read-only. Drive them
  from a cluster worker / CLI or a seed migration, with Reto's go.

## Open decision (for the post-compact session)

**Which backends to register?** (a) llama-swap only — recommended, retires litellm,
durable; (b) llama-swap + ollama-for-spark-big-models (interim, gets `llama3.3:70b`
et al. routable now); (c) ollama only — not recommended (on the purge path).
