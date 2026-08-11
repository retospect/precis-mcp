# Config variables — the `PRECIS_*` catalog

Every `PRECIS_*` env var precis reads, what it controls, its **code
default**, and durable notes (design intent, gotchas, cross-refs).

- **Companion policy doc:** [`docs/conventions/env-vars.md`](../conventions/env-vars.md)
  is the *how-to-add* rule (the three-tier scheme). Threshold conventions:
  [`docs/conventions/thresholds.md`](../conventions/thresholds.md); kind
  gating: [`docs/conventions/kind-enablement.md`](../conventions/kind-enablement.md).
- **Authoritative sources.** Code defaults live in
  `src/precis/config.py` (Tier-1) and each subsystem's `from_env()` /
  `cli/worker.py` reader (Tier-2).
- **Truthiness.** Boolean toggles use `env_flag`/`env_truthy`
  (`src/precis/utils/env.py`): truthy = `1/true/yes/on`
  (case-insensitive). "unset" ⇒ the code default applies.

Cluster topology — which hosts exist, which daemons run where, what each
daemon's env carries: [`deploy/README.md`](../../deploy/README.md).

---

## 1. Feature toggles (ship-dark gates)

The high-value switches — each gates a whole pass/kind.

| Var | Gates | Code default | Notes |
|-----|-------|--------------|-------|
| `PRECIS_ANKI_ENABLED` | Headless AnkiWeb sync pass | `False` | Single AnkiWeb writer (owns the OAuth + `.anki2` mirror) — must never run on two hosts. |
| `PRECIS_ANKI_FIX_ENABLED` | LLM precis-fix pass per sync | `False` | Needs the host's claude OAuth. |
| `PRECIS_ANKI_PROJECT_ENABLED` | Project all Anki cards → read-only PG refs | `False` | Read-only projection; a single host suffices. |
| `PRECIS_SUMMARIZE_LLM` | LLM two-part gloss (vs. extractive) | `False` | Designed as a single-host trickle, not a fleet-wide pass. |
| `PRECIS_STRUCTURAL_REVIEW` | Structural reviewer (opus, 6h) | off | LLM tier — needs the agent worker profile. |
| `PRECIS_DEEP_REVIEW` | Deep reviewer (opus, weekly) | off | LLM tier — agent profile. |
| `PRECIS_CHEM_ENABLED` | `route`/chem kind **surface** | off | Surface gate only; compute routes to the node named by `PRECIS_CHEM_ROUTE_NODE` (§5). |
| `PRECIS_BIO_ENABLED` | `protein`/fold kind surface | off | Surface gate only; compute routes via `PRECIS_FOLD_NODE` (§5). |
| `PRECIS_AUTOCATPATH_ENABLED` | `pathway`/autocatpath kind | off | Surface on the gateway; compute env on the GPU node. |
| `PRECIS_BRIEFING_AUDIO_ENABLED` | Standalone daily briefing TTS pass (`news-<date>` episode) | off | Retired: the news wire is folded into the combined `morning_brief_<date>` reading-cast episode at narration time (`cast_audio._news_lead_in`); leave off to avoid double-publishing. |
| `PRECIS_CAST_AUDIO_ENABLED` | Podcast cast TTS pass | off | |
| `PRECIS_OA_FETCH` | Unpaywall/OA fetch leg | `0` | Single-fetcher design — two hosts fetching races the shared inbox (gripe history). |
| `PRECIS_GP_FETCH` | Google-Patents fetch leg | `0` | Same single-fetcher rationale. |
| `PRECIS_OPENALEX_MIN_CREDITS` | Low-balance alert floor for the paid OpenAlex leg (raw daily credits; content fetch = 100) | `2000` | Replaces the dropped `PRECIS_OPENALEX_CONTENT_AUTO` gate. `PRECIS_OPENALEX_CONTENT_KEY` (vault) is the sole spend opt-in; this floor drives the `fetch_oa:openalex_balance` alert. |
| `PRECIS_CLASSIFY_ENABLED` | Chunk-tag classify pass | off | Default-OFF by design; enable as a single-node trickle like `PRECIS_SUMMARIZE_LLM`. Activation: [`docs/backlog/classifier-corpus-enablement.md`](../backlog/classifier-corpus-enablement.md). |
| `PRECIS_PAPER_GLOSSARY_ENABLED` | Per-paper glossary pass | off | Slice built, dark by design. |
| `PRECIS_SANDBOX_ENABLED` | Register the `sandbox_run` executor pass | off | Deploying the `code-sandbox` container alone is not enough — the pass that dispatches to it never registers without this flag. Activation: [`docs/backlog/dark-features-activation.md`](../backlog/dark-features-activation.md). |
| `PRECIS_QUEST_LOOP_ENABLED` | Autonomous quest research loop | off | Autonomous GPU/token spend — operator's call. `PRECIS_QUEST_WEEKLY_CHARS` must be set too (§9). Activation: [`docs/backlog/quest-loop-activation.md`](../backlog/quest-loop-activation.md). |
| `PRECIS_BACKLOG_GROOM_ENABLED` | Backlog groomer (auto repo-bug fixing) | off | Activation: [`docs/backlog/backlog-groomer-items-half.md`](../backlog/backlog-groomer-items-half.md). |
| `PRECIS_CHASE_LLM` | LLM finding-chase pass | `0` | The SQL chase covers the default path; LLM chase is opt-in. |
| `PRECIS_DREAM_AGENT` | Dream agent enable | off | Set on the agent-profile worker process (with `PRECIS_DREAM_SOUL_PATH`) so that process is the `dream_agent` scheduler cadence's eligible claimant. |
| `PRECIS_FRICTION_REFLECT` | Friction-reflection pass | off | Staged — prerequisite recorded in [`docs/backlog/friction-reflection-enable.md`](../backlog/friction-reflection-enable.md). |
| `PRECIS_ORACLE_AUTO_REINGEST` | Reingest on oracle sync | `1` (on) | |
| `PRECIS_BACKFILL_CITATION_LENS` | Citation-lens backfill | `1` (on) | |
| `PRECIS_FETCH_MARKUP` | Markup-first fetch leg | `0` | Ships dark; flipping is gated on the PDF-race decision — [`docs/backlog/markup-first-ingest.md`](../backlog/markup-first-ingest.md). |
| `PRECIS_PATCH_PDFS` | Patch PDFs on ingest | `1` (on) | |
| `PRECIS_LAYER2_FIXER` | Layer-2 plaintext fixer | off | Kept dark deliberately — fate: [`docs/backlog/tex-layer2-fixer-fate.md`](../backlog/tex-layer2-fixer-fate.md). |
| `PRECIS_DIAGRAM_AGENTIC` | Agentic diagram-propose path | off | Explicit override; unset ⇒ auto (agentic when an MCP config is present). Activation: [`docs/backlog/dark-features-activation.md`](../backlog/dark-features-activation.md). |
| `PRECIS_PYTHON_ALLOW_EXEC` | Allow `python` kind to exec code | off (refuses) | Keep off unless a sandbox is guaranteed. |

**Kind gating** (a kind is hidden unless its gate is satisfied). Since the
capability-universalization (§L run-control cutover; see the ``precis.workers`` docstring), the *incidental* env gates
were dropped: `edgar` is available on **every** host (its raw-root +
User-Agent default via `precis.config`), and `patent` gates on the genuinely
scarce **EPO credentials** via `KindSpec.requires_secret` (vault) — **not**
`PRECIS_PATENT_RAW_ROOT`, which is now just a config-defaulted path. Still
gated by `KindSpec.requires_env`: `PRECIS_PYTHON_ROOTS`→`python` (a deliberate
filesystem-scoping choice) and `PRECIS_ROOT`→markdown/plaintext/tex.

---

## 2. Autonomy / mode selectors

| Var | Controls | Code default | Notes |
|-----|----------|--------------|-------|
| `PRECIS_CARD_FORGE_AUTONOMY` | Card-forge: `report` (observe) vs `act` | `report` | Observe-first is the intended safe default; flip to `act` once the forge is trusted ([`docs/backlog/reading-prep-loop.md`](../backlog/reading-prep-loop.md)). |
| `PRECIS_FIXER_AUTONOMY` | Fixer autonomy level | none | Fixer-side var — set where the fixer runs, not on cluster daemons. |
| `PRECIS_DREAM_LENS` | Dream lens list | `sci` | Matches the oracle-lens design. |
| `PRECIS_AGENT_TABLE_FORMAT` | Agent table render | `toon` | |

---

## 3. Budgets & guardrails

| Var | Controls | Code default | Notes |
|-----|----------|--------------|-------|
| `PRECIS_MAX_TICKS` | Planner max ticks | `10` | Test-scale default — raise deliberately for production use. |
| `PRECIS_MAX_TODO_USD` | Planner per-todo USD cap | `2.0` | |
| `PRECIS_DAILY_COST_CEILING` | Daily cost ceiling over *all* recorded LLM spend — gates the dispatcher at mint AND the scheduler's `spends=True` cadences at fire | `20.0` | The binding ceiling in practice: unlike the `PRECIS_BUDGET_DAILY_USD` breaker (real money only, `expensive` band only), this one counts notional `claude_agent`/`claude_p` OAuth dollars — the bulk of recorded spend. Set it with real headroom over a busy day. Tuning: [`docs/backlog/daily-cost-ceiling-tuning.md`](../backlog/daily-cost-ceiling-tuning.md). |
| `PRECIS_BUDGET_DAILY_USD` | Global 24h **real-money** spend cap — call-site circuit breaker (`budget.breaker.gate_tier`, router.py) | `20.0` | Complementary, not competing: `budget.meter` deliberately excludes the OAuth transports (subscription quota, not a metered balance), so this cap sees only metered spend. It is the *money* cap; `PRECIS_DAILY_COST_CEILING` above is the *discretionary-burn* cap that includes subscription quota. Set it to the real intended dollar number. |
| `PRECIS_BUDGET_HOURLY_USD` | Global hourly cap (same breaker) | `5.0` | Set explicitly alongside the daily cap. |
| `PRECIS_BUDGET_CHEAP_MAX_USD` | Cheap/expensive band boundary | `0.02` | |
| `PRECIS_LOAD_CEILING` | Load-average gate for heavy passes | none (`cpu*1.5`) | Gates on *load-average* (CPU) — the wrong lever for RAM-driven (jetsam) pressure. |
| `PRECIS_STUCK_JOB_HOURS` | Sweeper stuck-job threshold — the lease-epoch reclaim work retired this for `ssh_node`/`claude_inproc`/`claude_docker` rows (they self-heal via lease/epoch/attempt-cap); it still backstops lease-less legacy rows and the `coordinator` executor (no reclaim path of its own). | `1.0` | |
| `PRECIS_MAX_JOB_ATTEMPTS` | Generalized crash-loop attempt cap (§H piece 3, `executors/_common.py`'s `poison_guard`) — shared by every `reclaim_stale_running` executor (`ssh_node`/`claude_inproc`/`claude_docker`). Only *expiry*-reason reclaims bump the counter; an *epoch* (redeploy-provably-replaced) reclaim never does. | `3` | |
| `PRECIS_WAKE_DEADLINE_HOURS` | Fallback child-deadlock deadline (§H piece 5, `executors/coordinator.py`) for a `children_done` park whose children declare NO `params.resources.wall_seconds` budget at all — `wake_runner` re-queues a past-deadline `waiting_children` parent "woken-degraded" instead of waiting on a wake condition an unschedulable child might never satisfy. | `6.0` | |
| `PRECIS_SUMMARIZE_CONCURRENCY` | LLM-summarize concurrency | `3` | Throttle down on a loaded host. |
| `PRECIS_SUMMARIZE_TIMEOUT` | LLM-summarize per-call timeout | `120.0` | |
| `PRECIS_MATERIALIZE_EMBED` | Cutover gate (`workers/materialize.py`) — the `materialize` cadence mints bounded `embed_batch` jobs above the backlog high-water mark. §F cycle b flipped this **default-ON**; `0` (or any non-truthy token) is the documented opt-out/rollback (pair with a manual `precis worker --only embed` on any node — the standing pass lost its rotation slot in `registry.py`). | **on** (unset ⇒ active) | The standing `embed` pass is manual-only — this IS the drain path. |
| `PRECIS_EMBED_BACKLOG_HIGH` | Unembedded-chunk backlog high-water mark above which `materialize` mints bounded `embed_batch` jobs; also the base of the 4× backlog-WARNING liveness line (`materialize.py`). | `500` | |
| `PRECIS_EMBED_BATCH_MAX_JOBS` | Max `embed_batch` jobs `materialize` mints in one tick (`min(ceil(backlog/limit), this)`). | `4` | |
| `PRECIS_EMBEDDER_SLOTS` | `embedder` `resource_slots` capacity override (`capability_probe.py`) — bypasses the `/readyz` probe entirely, same as `PRECIS_GPU_COUNT`/`PRECIS_PODMAN_SLOTS`. | `1` when `/readyz` answers, else `0` | Additive — advertises regardless of `PRECIS_MATERIALIZE_EMBED`; only consumed once a `requires={'embedder'}` job_type (`embed_batch`) actually claims. `/readyz` answers 200 both `loaded` and `idle`, so idle-unload never retracts the slot. |
| `PRECIS_EMBEDDER_IDLE_S` | Idle-unload window (seconds) for `precis serve-embeddings` (`embedder_service.py`) — release the model's weights after this long with no `POST /embed`, reloading lazily on the next one. `0` disables (never unload). Ansible: `precis_embedder_idle_s` (`deploy/roles/precis_embedder/defaults/main.yml`), passed as `--idle-s`. | `1800` | The daemon (+ watchdog) stays standing; only model residency is elastic. An interactive-search host may want a longer value or `0` — the first query after idle pays a cold load. |
| `PRECIS_EMBEDDER_LOAD_DEADLINE_S` | Hard wall-clock deadline (seconds) on `BgeM3Embedder`'s model load (`embedder.py::_load_with_deadline`) — a hung load (e.g. `SentenceTransformer` dialing a slow/rate-limited HuggingFace Hub for revision metadata even with weights cached) exits the whole process (`os._exit(1)`) instead of leaving `/readyz` unready forever. | `600` | embedder-wedge-hardening.md §2. A restart-based watchdog can't fix a load hung on a remote service; the process has to notice and exit so launchd `KeepAlive` / systemd `Restart=always` own the retry. |
| `PRECIS_JOB_INPROC_LEASE_S` | Claim lease window (seconds) for the `job_inproc` executor (`workers/executors/job_inproc.py`) — must outlive the bounded work order. | `1800` | |

---

## 4. Models & LLM backend

Model IDs and the backend switch ride code defaults — a model bump is a
code change; env overrides exist for per-host divergence.

| Var | Controls | Code default | Notes |
|-----|----------|--------------|-------|
| `PRECIS_LLM_BACKEND` | `anthropic` vs OpenAI-compat OSS | `anthropic` | OSS backend ships dark (ADR 0046); byte-identical to `claude -p`. Also gates `SMALL`'s transport (`OPENAI_COMPAT` vs the loopback `LOCAL` wire) — see `PRECIS_LLM_FAILOVER` below. |
| `PRECIS_LLM_BASE_URL` / `PRECIS_LLM_API_KEY` | OSS endpoint + key (vault) | none | Only needed when the backend flips. OpenRouter recipe: `PRECIS_LLM_BASE_URL=https://openrouter.ai/api/v1`; `PRECIS_LLM_API_KEY` is already vaulted — no seeding step needed. |
| `PRECIS_LLM_FAILOVER` | Wraps an OSS primary in the `FailoverProvider` claude-fallback ladder | `""` | Built (`router._failover_ladder`/`FailoverProvider`). Also covers a *saturated local slot*: a paused call retries the ladder's hosted rung instead of failing outright, but only when rung 0's transport actually has a hosted mode (`OPENAI_TOOLS`/`OPENAI_COMPAT` — reachable for `BIG`/`MEDIUM`/`FRONTIER` under `PRECIS_LLM_BACKEND=openai`, or for any tier via an operator `llm.chain.<tier>` rung naming one explicitly); a tier with no such rung has no hosted escape and still backs off immediately. Off by default — real $ once a backend/base-url is configured. |
| `PRECIS_MODEL_OPUS` | FRONTIER model id | `claude-opus-4-8` | |
| `PRECIS_MODEL_SONNET` | BIG model id | `claude-sonnet-5` | Minor tier (`tex_llm_fix` + `job` retry only). |
| `PRECIS_MODEL_HAIKU` | MEDIUM model id | `claude-haiku-4-5-20251001` | |
| `PRECIS_LOCAL_BIG_MODEL` | ~~LOCAL_BIG tier alias~~ **RETIRED** | `qwen-heavy` | ADR 0066 Phase C deleted the `LOCAL_BIG` tier and its `_TIER_MODEL` row — nothing in `src/` reads this var anymore. Safe to drop from any host env. |
| `PRECIS_SUMMARIZE_MODEL` | Summarize LLM alias | `summarizer` | |
| `PRECIS_SLULLAMA_ENDPOINT` / `_MODEL` / `_MODEL_ID` / `_HOST` / `_MAX_PARALLEL` | Static `llm` card for a Slurm-HPC cluster's Ollama endpoint, tunnelled to loopback (`llm_catalog.seed_slullama_card`; CLI `precis llm seed --slullama`) | endpoint `http://127.0.0.1:11435/v1`, model_id `qwen-hpc`, host `melchior`, max_parallel `3` | `served_by` entry stamped `source="static"` — shielded from `workers/llm_serving.py::advertise_local_llm`'s per-heartbeat auto-discovery prune (never poll the tunnel; it'd keep the GPU node awake). Which chain rung (if any) routes here is a separate concern — `docs/backlog/slullama-hpc-placement.md`. |
| Per-pass model overrides | `PRECIS_FIXER_CLAUDE_MODEL` (`claude-sonnet-5`), `PRECIS_FIX_CLAUDE_MODEL`, `PRECIS_{CLASSIFY,PAPER_GLOSSARY,STRUCTURAL,DEEP_REVIEW,STRUCTURE_PROPOSE,CAD_PROPOSE,CAD_DISCUSS,DREAM_AGENT,FIGURE,MERMAID,BRIEFING,MEDITATION,READING_BRIEF,CARD_FORGE,FOLLOWUP,STUB_RANK_LLM}_MODEL` | mostly none ⇒ tier resolver | Unset ⇒ each falls to its tier default. Set only to pin a specific pass. `PRECIS_STUB_RANK_LLM_MODEL` pins the `stub_rank` pass's Tier-2 band client (SMALL tier); see §9. |
| `PRECIS_EMBEDDER` | `mock`/`bge-m3`/`remote` | `mock` (config) / `bge-m3` (worker) | `remote` points at a `serve-embeddings` endpoint (`PRECIS_EMBEDDER_URL`, §8); workers can also pass this via CLI args. |
| `PRECIS_EMBEDDER_BACKEND` | serve-embeddings backend | `bge-m3` | |

---

## 5. Compute routing, nodes & container images

The compute-lane env (node identity, images, container commands, NFS
roots) lives with the node that owns the capability — topology:
[`deploy/README.md`](../../deploy/README.md).

| Var | Controls | Notes |
|-----|----------|-------|
| `PRECIS_NODE` | Worker node identity (SSH pinning) | Required for the derived-job SSH lane. |
| `PRECIS_DFT_NODE` / `PRECIS_DFT_IMAGE` / `PRECIS_DFT_CONTAINER_CMD` / `PRECIS_DFT_NFS_ROOT` | DFT relax lane | |
| `PRECIS_CHEM_ROUTE_NODE` / `PRECIS_CHEM_CONTAINER_CMD` / `PRECIS_CHEM_MODELS_DIR` / `PRECIS_CHEM_NFS_ROOT` | Chem route lane | Kind surface (`PRECIS_CHEM_ENABLED`, §1) and compute node are deliberately decoupled. |
| `PRECIS_FOLD_NODE` / `PRECIS_FOLD_IMAGE` / `PRECIS_FOLD_MODELS_DIR` / `PRECIS_FOLD_CONTAINER_CMD` / `PRECIS_FOLD_NFS_ROOT` / `PRECIS_FOLD_XLA_CACHE` / `PRECIS_FOLD_MEM_LIMIT` | AlphaFold3 lane | |
| `PRECIS_TTS_IMAGE` / `PRECIS_TTS_CONTAINER_CMD` / `PRECIS_TTS_SCRATCH` / `PRECIS_PODCAST_DIR` / `PRECIS_BRIEFING_AUDIO_VOICE` / `PRECIS_BRIEFING_AUDIO_LANG` | TTS render lane | Kokoro is baked into the TTS image; the image deploys separately from `scripts/deploy`. |
| `PRECIS_SANDBOX_HOSTS` / `PRECIS_SANDBOX_IMAGE` / `PRECIS_SANDBOX_*` limits | Sandbox-run resource caps | Set together with `PRECIS_SANDBOX_ENABLED` (§1) when activating the sandbox lane. |
| `PRECIS_SANDBOX_ARTIFACT_ROOT` | Sandbox-run harvest's content-addressed tarball store root (see `workers/executors/_sandbox_harvest.py`; default `~/work`, tarball lands under `<root>/sandbox-artifacts/<sha256>.tar.gz`) | Point at the shared NAS mount (mirrors `PRECIS_CORPUS_DIR`, ADR 0029) when activating the sandbox lane. |
| `PRECIS_SANDBOX_READ_MCP` | Ops capability flag for `precis_access:read` — a `mode:build` job's per-run, token'd, read-only MCP callback. `sandbox_run.semantic_rejection` fails closed without it. | |
| `PRECIS_MCP_TOKEN` | Bearer token `precis serve --transport sse\|streamable-http` requires (or `--token`) — the network-transport gate `_install_token_auth` checks. Only consumed by the per-run `precis_access:read` callback child today; `--transport stdio` (every other caller) never reads it. | The per-run callback child generates its own token per launch — it never reads this env var. |

---

## 6. Paths, roots & binaries

| Var | Controls | Notes |
|-----|----------|-------|
| `PRECIS_DATABASE_URL` | Postgres DSN | Pooled access goes through pgbouncer; LISTEN/NOTIFY needs a direct connection (`PRECIS_NOTIFY_DATABASE_URL`, used by asa-bot). |
| `PRECIS_CORPUS_DIR` | Ingested-PDF corpus root | Per-host corpus, resolved by `corpus_reconcile`. NAS paths differ by OS. |
| `PRECIS_WATCH_INBOX` | Inbox ingest dir | Multiple hosts watching one shared inbox is supported (race handled). |
| `PRECIS_PATENT_RAW_ROOT` | Patent raw store | Config-defaulted path — no longer the `patent` kind gate (see §1 kind-gating note). |
| `PRECIS_ROOT` | md/plaintext/tex root (gates trio) | Unset ⇒ the file-trio kinds are hidden. Set it for file-backed notes. |
| `PRECIS_PYTHON_ROOTS` | python-kind repos | Empty/unset ⇒ `python` kind hidden. |
| `PRECIS_FREEROUTING_JAR` | Freerouting jar (PCB) | Route step no-ops gracefully if the jar is absent. |
| `PRECIS_PODCAST_DIR` / `PRECIS_PODCAST_BASE_URL` | Podcast output + feed URL | |
| `PRECIS_CLAUDE_BIN` / `PRECIS_MCP_CONFIG` | claude CLI + MCP config for spawned agents | Agentic passes need them. |

---

## 7. Secrets / credentials

Per ADR 0055, API keys are **not** env vars on any daemon — they
resolve from the DB `vault.secrets` table (`get_secret()`). The two
still set as env are the Anki login (`PRECIS_ANKI_USER` /
`PRECIS_ANKI_PASSWORD`) and `PRECIS_UNPAYWALL_EMAIL`.

| Var | Resolves via | Notes |
|-----|--------------|-------|
| `PRECIS_LLM_API_KEY`, `PRECIS_CORE_API_KEY`, `PRECIS_ELSEVIER_API_KEY`, `PRECIS_WILEY_TDM_TOKEN`, `PRECIS_OPENALEX_CONTENT_KEY`, `PRECIS_EPO_KEY`, `PRECIS_SUMMARIZE_LLM_KEY` | DB vault | Off-env is the ADR-0055 posture. |
| `PRECIS_SECRETS_FILE_DIR` | file fallback (`~/.secrets/pw`) | Local-dev fallback; the vault wins when both resolve. |
| `PRECIS_CROSSREF_MAILTO` / `PRECIS_UNPAYWALL_EMAIL` / `PRECIS_WIKIPEDIA_UA` | polite-pool identity | Low-risk. |

---

## 8. Endpoints, targets & ops

| Var | Controls | Notes |
|-----|----------|-------|
| `PRECIS_OPS_ALERT_TARGET` | Critical-alert Discord push target | Default-unset is **silent** — critical nursery/quota alerts merge dark without it (CLAUDE.md warns). Set it. |
| `PRECIS_DEADMAN_PING_URL` | §D external dead-man's-switch (`workers/health_digest.py`) — a healthchecks.io-style GET target, pinged hourly via `safe_fetch.safe_get` after every successful digest eval | The one signal that survives a *total* fleet/DB outage; everything else in the liveness net is DB-mediated and can't self-report that case. Provisioning: `docs/runbooks/dead-mans-switch.md`. |
| `PRECIS_DEADMAN_ALLOW_PRIVATE` | Opt-in to allow a private/loopback/LAN `PRECIS_DEADMAN_PING_URL` target (a self-hosted check) — the default SSRF guard blocks that range, since it's built for agent-supplied URLs, not this operator-set constant | Set to `"1"` only alongside a LAN `PRECIS_DEADMAN_PING_URL` — see `docs/runbooks/dead-mans-switch.md` § "LAN / self-hosted targets". |
| `PRECIS_FIXER_DISCORD_WEBHOOK` / `PRECIS_FIXER_READYZ_URL` | Fixer push + readiness | Fixer-side vars — set where the fixer runs, not on cluster daemons. |
| `PRECIS_EMBEDDER_URL` | Remote embedder endpoint | Pairs with `PRECIS_EMBEDDER=remote` (§4); typically a loopback `serve-embeddings`. |
| `PRECIS_ASKCOS_URL` | ASKCOS chem endpoint | Chem route uses the container lane by default; only needed for an ASKCOS HTTP instance. |

---

## 9. Tuning knobs

Override only with a measured reason (see
[`thresholds.md`](../conventions/thresholds.md)).

`PRECIS_DB_CONNECT_RETRY_SECONDS` (30), `PRECIS_EMBEDDER_TIMEOUT` (300 —
raised from 30 2026-08-10, embedder-wedge-hardening.md §5: a CPU-host
embed batch can legitimately run past 30s, and a client timeout shorter
than that hangs up on a still-computing batch, amplifying retries),
`PRECIS_EMBEDDER_MAX_RETRIES` (5/3), `PRECIS_EMBEDDER_MAX_INFLIGHT` (4),
`PRECIS_STARTUP_SKILLS_CAP_KB` (50), `PRECIS_INPROC_CONCURRENCY` (1),
`PRECIS_CLUSTER_INTERVAL_HOURS` (20), good-search knobs
(`PRECIS_GOOD_SEARCH_*` — heartbeat 180, deadline 1200, slices 30, pool
100, per-paper 3, max-children 4), quest allocator
(`PRECIS_QUEST_EWMA_ALPHA` 0.3, `PRECIS_QUEST_EXPLORE` 0.15,
`PRECIS_QUEST_COOL_AFTER_TICKS` 12, `PRECIS_QUEST_FRONTIER_REVIEW_EVERY`
5, `PRECIS_QUEST_STALL_TICKS` 4), reading/mastery
(`PRECIS_MASTERY_THRESHOLD`, `PRECIS_READING_CARDS_PER_DAY` 5,
`PRECIS_CARD_REWORK_MIN_DAYS`, `PRECIS_CARD_REWORK_STREAK_CAP` 3), figure
/ mermaid / cad limits (`PRECIS_FIGURE_MAX_TURNS` 20, `PRECIS_*_MAX_USD`,
`PRECIS_*_TIMEOUT_S`), sweeper retention
(`PRECIS_TRANSCRIPT_RETENTION_DAYS`, `PRECIS_AGENTLOG_RETENTION_DAYS`,
`PRECIS_LLM_LOG_RETENTION_DAYS`), reconcile refresh windows
(`PRECIS_PAPER_RECONCILE_REFRESH_HOURS`,
`PRECIS_CORPUS_RECONCILE_REFRESH_HOURS`), the log-handler batching
(`PRECIS_LOG_MAX_BUFFER` 50, `PRECIS_LOG_MAX_INTERVAL_SECONDS` 5), and
`stub_rank`'s Tier-2 LLM band (`PRECIS_STUB_RANK_BAND_LO` 0.30 /
`PRECIS_STUB_RANK_BAND_HI` 0.70 — the "uncertain middle" percentile
window the LLM judges; `PRECIS_STUB_RANK_LLM_BATCH` 25 — max LLM calls
per pass, the cost guard; `0` disables step (d) entirely; model override
`PRECIS_STUB_RANK_LLM_MODEL`, §4).

Gotcha: `PRECIS_QUEST_WEEKLY_CHARS` (no default) **must** be set before
flipping `PRECIS_QUEST_LOOP_ENABLED` — the meter is character-count, not
dollars (gr162594: the quest lane never reports a $ cost).

---

## 10. Tier-3 IPC & build stamps (not configuration)

- **Per-invocation IPC** (a parent sets them per child; never
  "configure" these): `PRECIS_CURRENT_TODO`, `PRECIS_WORKSPACE`,
  `PRECIS_CURRENT_MODEL`, `PRECIS_CURRENT_AGENTLOG`, `PRECIS_SOURCE`
  (`precis-worker`/`web:reto`/…), `PRECIS_PROCESS`, `PRECIS_HOST_NAME`.
- **Build/provenance stamps** (baked into the image, surfaced in
  `/status`): `PRECIS_GIT_SHA`, `PRECIS_GIT_SHA_SHORT`,
  `PRECIS_GIT_BRANCH`, `PRECIS_GIT_DIRTY`, `PRECIS_GIT_LAST_TAG`,
  `PRECIS_GIT_DESCRIBE`, `PRECIS_BUILD_TIME`, `PRECIS_BUILD_HOST`,
  `PRECIS_BUILD_USER`. Not tunable.
