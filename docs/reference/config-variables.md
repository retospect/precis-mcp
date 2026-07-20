# Config variables — catalog + cluster deployment state

Every `PRECIS_*` env var precis reads, what it does, its **code
default**, its **deployed value per cluster service**, and an
**assessment** of whether that deployed state looks right.

- **Companion policy doc:** [`docs/conventions/env-vars.md`](../conventions/env-vars.md)
  is the *how-to-add* rule (the three-tier scheme). This file is the
  *what-is-set-where* map. Threshold conventions:
  [`docs/conventions/thresholds.md`](../conventions/thresholds.md); kind
  gating: [`docs/conventions/kind-enablement.md`](../conventions/kind-enablement.md).
- **Authoritative sources.** Code defaults live in
  `src/precis/config.py` (Tier-1) and each subsystem's `from_env()` /
  `cli/worker.py` reader (Tier-2). Deployed values live in the cluster
  ansible repo (`~/work/cluster`): the shared-env dict
  `inventory/group_vars/all/precis_env.yml`, the capability→host map
  `inventory/group_vars/all/topology.yml`, the per-daemon templates
  under `roles/*/templates/`, and `inventory/host_vars/*.yml`.
- **Truthiness.** Boolean toggles use `env_flag`/`env_truthy`
  (`src/precis/utils/env.py`): truthy = `1/true/yes/on`
  (case-insensitive). "unset" ⇒ the code default applies.
- **Drift warning.** The *code default* columns are read from source
  and stay true as long as this file is updated in the same commit that
  changes them (repo convention). The *deployed* columns are a **scan
  snapshot** of the cluster repo — re-verify against the live daemons
  (`launchctl print` / `systemctl show`) before trusting them for an
  incident.

---

## Cluster hosts & services

| Host | OS / runtime | Role | Notable capability |
|------|--------------|------|--------------------|
| **melchior** | macOS (Metal), launchd | `gateway` | The only agent/LLM host: agent-worker, dream, web, asa-bot, cron-tick, anki-sync, OA/GP fetch. Runs `hermes` OAuth. |
| **caspar** | macOS, launchd | `data` | Postgres / pgbouncer / redis / NFS server. Runs the reconcile daemon. |
| **balthazar** | macOS (Metal), launchd | `scheduler` | System worker + inbox watcher; an `agent_sandbox_host`. |
| **spark** | Linux (CUDA GB10), systemd | `inference` | System worker **plus** the GPU/container compute lanes: DFT, chem route, AlphaFold, TTS. |

**Long-lived daemons and where they run** (each Mac daemon is a
LaunchDaemon; spark uses systemd units):

| Service | Hosts | Includes shared-env? |
|---------|-------|----------------------|
| `precis worker --profile=system` | all 4 | yes |
| `precis worker --profile=agent` | melchior | yes |
| `dream_agent` (15-min) | melchior | yes |
| `cron-tick` (60-s) | melchior | yes |
| `precis web` / MCP serve | melchior | yes |
| `precis-anki-sync` (30-min) | melchior | yes |
| `precis-watch` (inbox ingest) | all 4 | yes |
| `precis-watch-poll` (citation-forward) | melchior | yes |
| `precis-reconcile` | caspar | no |
| `precis-heartbeat` | all 4 | no |
| `serve-embeddings` | all 4 | no (only `HF_HOME`) |
| `asa-bot` (Discord) | melchior | no |
| `code-sandbox` (podman) | balthazar, spark | no (no PRECIS_* at all) |

**The shared-env dict** (`precis_shared_env`) is looped into every
"yes" daemon above. It carries, identically on every host unless noted:
`PRECIS_SUMMARIZE_MODEL=qwen`, `PRECIS_FREEROUTING_JAR`,
`PRECIS_PATENT_RAW_ROOT`, `PRECIS_UNPAYWALL_EMAIL` (vault),
`PRECIS_OPENALEX_MIN_CREDITS=2000`, `PRECIS_WATCH_INBOX`,
`PRECIS_CORPUS_DIR`, `PRECIS_OPS_ALERT_TARGET` (a Discord channel), and
two **host-pinned to melchior**: `PRECIS_GP_FETCH` / `PRECIS_OA_FETCH`
(`1` on melchior, `0` elsewhere). API-key secrets were removed from env
(ADR 0055) and now resolve from the DB `vault.secrets` table.

---

## 1. Feature toggles (ship-dark gates)

The high-value switches — each gates a whole pass/kind. "Deployed"
values are from the cluster scan.

| Var | Gates | Code default | Deployed | Assessment |
|-----|-------|--------------|----------|------------|
| `PRECIS_ANKI_ENABLED` | Headless AnkiWeb sync pass | `False` | `1` on **melchior** only | ✅ Correct. Single AnkiWeb writer (owns the OAuth + `.anki2` mirror); must not run on two hosts. |
| `PRECIS_ANKI_FIX_ENABLED` | LLM precis-fix pass per sync | `False` | `1` on melchior | ✅ Correct — needs melchior's claude OAuth. |
| `PRECIS_ANKI_PROJECT_ENABLED` | Project all Anki cards → read-only PG refs | `False` | `1` on melchior | ✅ Correct — read-only, single host is fine. |
| `PRECIS_SUMMARIZE_LLM` | LLM two-part gloss (vs. extractive) | `False` | `1` on **melchior** only | ✅ Correct — deliberate trickle on the 80B-model host (CLAUDE.md). |
| `PRECIS_STRUCTURAL_REVIEW` | Structural reviewer (opus, 6h) | off | `1` on melchior **agent** worker | ✅ Correct — LLM tier needs the agent profile. |
| `PRECIS_DEEP_REVIEW` | Deep reviewer (opus, weekly) | off | `1` on melchior agent worker | ✅ Correct. |
| `PRECIS_CHEM_ENABLED` | `route`/chem kind **surface** | off | `1` on melchior (web) | ✅ Correct — kind surface on gateway, compute routes to spark via `PRECIS_CHEM_ROUTE_NODE=spark`. |
| `PRECIS_BIO_ENABLED` | `protein`/fold kind surface | off | `1` on melchior (web) | ✅ Correct — surface on gateway, `PRECIS_FOLD_NODE=spark`. |
| `PRECIS_CATPATH_ENABLED` | `pathway`/catpath kind | off | `1` on melchior (web) **and** spark (worker) | ✅ Correct — surface on gateway, compute env on spark. |
| `PRECIS_BRIEFING_AUDIO_ENABLED` | Daily briefing TTS pass | off | `1` on **spark** (TTS drop-in) | ✅ Correct — spark holds the `tts_render` capability + `precis-tts` image. |
| `PRECIS_CAST_AUDIO_ENABLED` | Podcast cast TTS pass | off | `1` on spark | ✅ Correct. |
| `PRECIS_OA_FETCH` | Unpaywall/OA fetch leg | `0` | `1` on **melchior** only | ✅ Correct — single fetcher avoids the shared-inbox race (CLAUDE.md, gripe history). |
| `PRECIS_GP_FETCH` | Google-Patents fetch leg | `0` | `1` on melchior only | ✅ Correct — same single-fetcher rationale. |
| `PRECIS_OPENALEX_MIN_CREDITS` | Low-balance alert floor for the paid OpenAlex leg (raw daily credits; content fetch = 100) | `2000` | `2000` (shared env) | ✅ Replaces the dropped `PRECIS_OPENALEX_CONTENT_AUTO` gate (2026-07-16). The `PRECIS_OPENALEX_CONTENT_KEY` (vault) is now the sole spend opt-in; this floor drives the `fetch_oa:openalex_balance` alert. |
| `PRECIS_CLASSIFY_ENABLED` | Chunk-tag classify pass | off | **not set** anywhere | ⚠️ Intentionally dark (CLAUDE.md: default-OFF). Fine — but if you want it live, enable as a trickle on one node like `PRECIS_SUMMARIZE_LLM`. |
| `PRECIS_PAPER_GLOSSARY_ENABLED` | Per-paper glossary pass | off | **not set** | ⚠️ Dark by design (slice built, not activated). OK. |
| `PRECIS_SANDBOX_ENABLED` | Register the `sandbox_run` executor pass | off | **not set** anywhere | 🔶 Note: the `code-sandbox` **container** is deployed on balthazar+spark (inventory group), but the *precis pass* that dispatches to it is gated by this env var, which is unset everywhere ⇒ `sandbox_run` never registers. If sandbox execution is meant to be live, set `PRECIS_SANDBOX_ENABLED=1` on the sandbox hosts. Currently dark end-to-end. |
| `PRECIS_QUEST_LOOP_ENABLED` | Autonomous quest research loop | off | **not set** | 🔶 Intentionally dark — awaiting Reto's go (autonomous GPU/token spend). To go live also set `PRECIS_QUEST_WEEKLY_CHARS` and enable the struct-relax GPU lane. See the quest memory. |
| `PRECIS_BACKLOG_GROOM_ENABLED` | Backlog groomer (auto repo-bug fixing) | off | **not set** in the cluster repo | ℹ️ Expected. The fixer/backlog loop runs **locally on `hephaestus` (Reto's laptop)**, outside the cluster ansible — so its env lives in the laptop's local launch/env config, not `~/work/cluster`. Manage it there. |
| `PRECIS_CHASE_LLM` | LLM finding-chase pass | `0` | not set | ✅ Off — the SQL chase covers prod; LLM chase is opt-in. |
| `PRECIS_DREAM_AGENT` | Dream agent enable | off | set `1` by `dream-pass.sh` wrapper (melchior) | ✅ Correct — the daemon wrapper sets it at runtime. |
| `PRECIS_FRICTION_REFLECT` | Friction-reflection pass | off | not set | ⚠️ Dark. Fine if unused; revisit if you want the tool-confusion auto-file loop. |
| `PRECIS_ORACLE_AUTO_REINGEST` | Reingest on oracle sync | `1` (on) | not set ⇒ on | ✅ On by default, matches intent. |
| `PRECIS_BACKFILL_CITATION_LENS` | Citation-lens backfill | `1` (on) | not set ⇒ on | ✅ On by default. |
| `PRECIS_FETCH_MARKUP` | Markup-first fetch leg | `0` | not set | ⚠️ Devin markup-first ingest ships dark (memory). Off is expected until activated. |
| `PRECIS_PATCH_PDFS` | Patch PDFs on ingest | `1` (on) | not set ⇒ on | ✅ On by default. |
| `PRECIS_LAYER2_FIXER` | Layer-2 plaintext fixer | off | not set | ⚠️ Dark; fine. |
| `PRECIS_DIAGRAM_AGENTIC` | Agentic diagram-propose path | off | not set | ⚠️ Dark; the diagram-propose loop is unshipped (memory). Expected. |
| `PRECIS_PYTHON_ALLOW_EXEC` | Allow `python` kind to exec code | off (refuses) | not set | ✅ Correct — keep off in prod unless a sandbox is guaranteed. |

**Kind gating** (a kind is hidden unless its gate is satisfied). Since the
capability-universalization (state-map slice 5), the *incidental* env gates
were dropped: `edgar` is now available on **every** host (its raw-root +
User-Agent default via `precis.config`), and `patent` gates on the genuinely
scarce **EPO credentials** via `KindSpec.requires_secret` (vault) — **not**
`PRECIS_PATENT_RAW_ROOT`, which is now just a config-defaulted path. Still
gated by `KindSpec.requires_env`: `PRECIS_PYTHON_ROOTS`→`python` (a deliberate
filesystem-scoping choice) and `PRECIS_ROOT`→markdown/plaintext/tex. So in
prod `patent` is live cluster-wide (EPO creds in the vault) and `edgar` is live
everywhere; only `python` stays hidden (no `PRECIS_PYTHON_ROOTS`).

---

## 2. Autonomy / mode selectors

| Var | Controls | Code default | Deployed | Assessment |
|-----|----------|--------------|----------|------------|
| `PRECIS_CARD_FORGE_AUTONOMY` | Card-forge: `report` (observe) vs `act` | `report` | not set ⇒ `report` | ✅ Observe-first is the intended safe default (CLAUDE.md). Flip to `act` once the forge is trusted. |
| `PRECIS_FIXER_AUTONOMY` | Fixer autonomy level | none | not set in this repo | ℹ️ Fixer runs **locally on `hephaestus` (Reto's laptop)**, not the cluster — its autonomy is set in the laptop's local env, outside `~/work/cluster`. |
| `PRECIS_DREAM_LENS` | Dream lens list | `sci` | not set ⇒ `sci` | ✅ Matches the oracle-lens design. |
| `PRECIS_AGENT_TABLE_FORMAT` | Agent table render | `toon` | not set | ✅ Default fine. |

---

## 3. Budgets & guardrails

| Var | Controls | Code default | Deployed | Assessment |
|-----|----------|--------------|----------|------------|
| `PRECIS_MAX_TICKS` | Planner max ticks | `10` | `10000` (all worker plists) | ✅ Correct — `10` is a test-scale default; prod deliberately raises it. |
| `PRECIS_MAX_TODO_USD` | Planner per-todo USD cap | `2.0` | `5.0` (worker plists) | ✅ Prod override, reasonable. |
| `PRECIS_DAILY_COST_CEILING` | Planner daily cost ceiling (dispatch-time, planner todos only) | `20.0` | `50.0` (worker plists) | 🔶 **Effectively dead** — the global breaker (`PRECIS_BUDGET_DAILY_USD` $20) trips first, so this $50 is never reached. Drop it, or raise the breaker above it if you want this as the real ceiling. |
| `PRECIS_BUDGET_DAILY_USD` | Global 24h spend cap — call-site circuit breaker (`budget.breaker.gate_tier`, router.py) | `20.0` | **not set** ⇒ `20.0` | 🔶 **This is now the binding global ceiling** — the breaker gates *all* paid router LLM + fetches at the call site, and its $20 default trips *before* the planner's $50 daily ceiling is ever reached (so `PRECIS_DAILY_COST_CEILING` below is effectively dead). Set this explicitly to the real intended number and treat it as authoritative; keep the planner caps only for tick-cap + per-todo cap. |
| `PRECIS_BUDGET_HOURLY_USD` | Global hourly cap (same breaker) | `5.0` | not set ⇒ `5.0` | 🔶 Set explicitly alongside the daily cap. |
| `PRECIS_BUDGET_CHEAP_MAX_USD` | Cheap/expensive band boundary | `0.02` | not set ⇒ `0.02` | ✅ Default fine. |
| `PRECIS_LOAD_CEILING` | Load-average gate for heavy passes | none (`cpu*1.5`) | unset ⇒ default | ✅ Leave at default. It gates on *load-average* (CPU); melchior's jetsam is *RAM*-driven, so this is the wrong lever — the real fix (ProcessType=Interactive + sweeper lease-honoring) already shipped. |
| `PRECIS_STUCK_JOB_HOURS` | Sweeper stuck-job threshold | `1.0` | not set ⇒ `1.0` | ✅ Default matches CLAUDE.md. |
| `PRECIS_SUMMARIZE_CONCURRENCY` | LLM-summarize concurrency | `3` | `1` on melchior | ✅ Deliberately throttled on the loaded host. |
| `PRECIS_SUMMARIZE_TIMEOUT` | LLM-summarize per-call timeout | `120.0` | `120` on melchior | ✅ Explicit = default. |

---

## 4. Models & LLM backend

None of the model IDs or the backend switch are set on the cluster —
they ride code defaults, so a model bump is a code change (or an env
override if you want per-host divergence).

| Var | Controls | Code default | Deployed | Assessment |
|-----|----------|--------------|----------|------------|
| `PRECIS_LLM_BACKEND` | `anthropic` vs OpenAI-compat OSS | `anthropic` | not set ⇒ `anthropic` | ✅ OSS backend ships dark (ADR 0046); byte-identical to `claude -p`. Flip per-host to test OSS. |
| `PRECIS_LLM_BASE_URL` / `PRECIS_LLM_API_KEY` | OSS endpoint + key (vault) | none | not set | ✅ Only needed when backend flips. |
| `PRECIS_LLM_FAILOVER` | Failover backend ladder | `""` | not set | ✅ Deferred (FailoverProvider not built). |
| `PRECIS_MODEL_OPUS` | CLOUD_SUPER model id | `claude-opus-4-8` | not set ⇒ default | ✅ Current. |
| `PRECIS_MODEL_SONNET` | CLOUD_MID model id | `claude-sonnet-5` | not set ⇒ default | ✅ Current — the `_TIER_MODEL[CLOUD_MID]` default now tracks Sonnet 5 (bumped from `claude-sonnet-4-6`). Minor tier (`tex_llm_fix` + `job` retry only). |
| `PRECIS_MODEL_HAIKU` | CLOUD_SMALL model id | `claude-haiku-4-5-20251001` | not set ⇒ default | ✅ Current. |
| `PRECIS_LOCAL_BIG_MODEL` | LOCAL_BIG tier alias | `qwen-heavy` | not set ⇒ default | ✅ Resolves via the litellm proxy tier table. |
| `PRECIS_SUMMARIZE_MODEL` | Summarize LLM alias | `summarizer` | `qwen` (shared-env) | ✅ Explicit prod alias. |
| Per-pass model overrides | `PRECIS_FIXER_CLAUDE_MODEL` (`claude-opus-4-8`), `PRECIS_FIX_CLAUDE_MODEL`, `PRECIS_{CLASSIFY,PAPER_GLOSSARY,STRUCTURAL,DEEP_REVIEW,STRUCTURE_PROPOSE,CAD_PROPOSE,CAD_DISCUSS,DREAM_AGENT,FIGURE,MERMAID,BRIEFING,MEDITATION,READING_BRIEF,CARD_FORGE,FOLLOWUP}_MODEL` | mostly none ⇒ tier resolver | not set | ✅ Unset ⇒ each falls to its tier default. Set only to pin a specific pass. |
| `PRECIS_EMBEDDER` | `mock`/`bge-m3`/`remote` | `mock` (config) / `bge-m3` (worker) | `remote` (asa-bot); workers pass `bge-m3`/remote via CLI args | ✅ Correct — daemons point at the loopback `serve-embeddings` at `127.0.0.1:8181`. |
| `PRECIS_EMBEDDER_BACKEND` | serve-embeddings backend | `bge-m3` | not set ⇒ `bge-m3` | ✅ Default. |

---

## 5. Compute routing, nodes & container images

Set on **spark** via systemd drop-ins (`/etc/precis/*.env`) because
spark owns every heavy-compute capability; the Mac workers get none of
these.

| Var | Controls | Deployed (spark) | Assessment |
|-----|----------|------------------|------------|
| `PRECIS_NODE` | Worker node identity (SSH pinning) | `spark` | ✅ Required for the derived-job SSH lane. |
| `PRECIS_DFT_NODE` / `PRECIS_DFT_IMAGE` / `PRECIS_DFT_CONTAINER_CMD` / `PRECIS_DFT_NFS_ROOT` | DFT relax lane | `spark` / image / cmd / root | ✅ Correct — GPAW relax proven on spark (structure memory). |
| `PRECIS_CHEM_ROUTE_NODE` / `PRECIS_CHEM_CONTAINER_CMD` / `PRECIS_CHEM_MODELS_DIR` / `PRECIS_CHEM_NFS_ROOT` | Chem route lane | spark / cmd / dirs | ✅ Correct — surface on gateway, compute here. |
| `PRECIS_FOLD_NODE` / `PRECIS_FOLD_IMAGE` / `PRECIS_FOLD_MODELS_DIR` / `PRECIS_FOLD_CONTAINER_CMD` / `PRECIS_FOLD_NFS_ROOT` / `PRECIS_FOLD_XLA_CACHE` / `PRECIS_FOLD_MEM_LIMIT` | AlphaFold3 lane | spark / … | ✅ Correct — AF3 dispatch proven on spark (bio memory). |
| `PRECIS_TTS_IMAGE` / `PRECIS_TTS_CONTAINER_CMD` / `PRECIS_TTS_SCRATCH` / `PRECIS_PODCAST_DIR` / `PRECIS_BRIEFING_AUDIO_VOICE` / `PRECIS_BRIEFING_AUDIO_LANG` | TTS render lane | `precis-tts:latest` / docker / … | ✅ Correct — Kokoro baked into the image; separate deploy from `scripts/deploy`. |
| `PRECIS_SANDBOX_HOSTS` / `PRECIS_SANDBOX_IMAGE` / `PRECIS_SANDBOX_*` limits | Sandbox-run resource caps | not set | 🔶 Unset — consistent with `PRECIS_SANDBOX_ENABLED` being off. Set together when activating the sandbox lane. |

---

## 6. Paths, roots & binaries

Mostly delivered via shared-env or per-role templates; NAS paths differ
by OS (Macs `/opt/nas/...`, spark `/nas/...`).

| Var | Controls | Deployed | Assessment |
|-----|----------|----------|------------|
| `PRECIS_DATABASE_URL` | Postgres DSN | `agent_rw@caspar:6432/precis_prod` via pgbouncer (all daemons); asa-bot also has `PRECIS_NOTIFY_DATABASE_URL` (direct `:5433` for LISTEN/NOTIFY) | ✅ Correct — pgbouncer for pooled, direct tunnel for NOTIFY. |
| `PRECIS_CORPUS_DIR` | Ingested-PDF corpus root | shared-env NAS path (OS-specific) | ✅ Per-host corpus, resolved by `corpus_reconcile`. |
| `PRECIS_WATCH_INBOX` | Inbox ingest dir | shared-env NAS path | ✅ All 4 watch the shared inbox (race handled). |
| `PRECIS_PATENT_RAW_ROOT` | Patent raw store (gates `patent`) | `/opt/nas/botshome/patents-raw` | ✅ Enables `patent` cluster-wide. |
| `PRECIS_ROOT` | md/plaintext/tex root (gates trio) | **not set** on cluster | ⚠️ The file-trio kinds are hidden in prod. Intentional if the corpus is DB-native; set it if you want file-backed notes. |
| `PRECIS_PYTHON_ROOTS` | python-kind repos | set (default empty) via asa MCP config | ⚠️ Empty ⇒ `python` kind hidden. Fine unless you want repo navigation. |
| `PRECIS_FREEROUTING_JAR` | Freerouting jar (PCB) | `/opt/precis/eda/freerouting.jar` (shared-env) | ⚠️ Jar path set, but the EDA binaries deploy is unpushed (pcb memory) — route step no-ops if absent (graceful). |
| `PRECIS_PODCAST_DIR` / `PRECIS_PODCAST_BASE_URL` | Podcast output + feed URL | set on web + TTS | ✅ Podcast feed is live. |
| `PRECIS_CLAUDE_BIN` / `PRECIS_MCP_CONFIG` | claude CLI + MCP config for spawned agents | set on agent/dream/web (hermes) | ✅ Correct — agentic passes need them. |

---

## 7. Secrets / credentials

Per ADR 0055, API keys are **not** env vars on any daemon — they
resolve from the DB `vault.secrets` table (`get_secret()`), populated by
`playbooks/populate-secrets-vault.yml`. The two still set as env are the
Anki login (`PRECIS_ANKI_USER` / `PRECIS_ANKI_PASSWORD`, from vault vars,
on the anki-sync daemon) and `PRECIS_UNPAYWALL_EMAIL` (shared-env).

| Var | Resolves via | Assessment |
|-----|--------------|------------|
| `PRECIS_LLM_API_KEY`, `PRECIS_CORE_API_KEY`, `PRECIS_ELSEVIER_API_KEY`, `PRECIS_WILEY_TDM_TOKEN`, `PRECIS_OPENALEX_CONTENT_KEY`, `PRECIS_EPO_KEY`, `PRECIS_SUMMARIZE_LLM_KEY` | DB vault | ✅ Correct — off-env is the ADR-0055 posture. |
| `PRECIS_SECRETS_FILE_DIR` | file fallback (`~/.secrets/pw`) | ✅ Local-dev fallback; unused in prod (vault wins). |
| `PRECIS_CROSSREF_MAILTO` / `PRECIS_UNPAYWALL_EMAIL` / `PRECIS_WIKIPEDIA_UA` | polite-pool identity | ✅ Set / defaulted; low-risk. |

---

## 8. Endpoints, targets & ops

| Var | Controls | Deployed | Assessment |
|-----|----------|----------|------------|
| `PRECIS_OPS_ALERT_TARGET` | Critical-alert Discord push target | `discord/<guild>/<channel>` (shared-env) | ✅ Set — critical nursery/quota alerts page instead of merging dark (CLAUDE.md warns the default-unset case is silent). Good that it's explicitly set. |
| `PRECIS_OPS_ALERT_WEBHOOK` | Deprecated alias | not set | ✅ Superseded by target. |
| `PRECIS_FIXER_DISCORD_WEBHOOK` / `PRECIS_FIXER_READYZ_URL` | Fixer push + readiness | not set here | ℹ️ Fixer is a **local `hephaestus` (laptop)** deploy — set in the laptop's env, not the cluster. |
| `PRECIS_EMBEDDER_URL` | Remote embedder endpoint | `http://127.0.0.1:8181` (asa; workers via CLI) | ✅ Loopback to local `serve-embeddings`. |
| `PRECIS_ASKCOS_URL` | ASKCOS chem endpoint | not set | ⚠️ Chem route uses the container lane, not ASKCOS HTTP. Expected unset. |

---

## 9. Tuning knobs (code-default in prod)

These are **not overridden anywhere in the cluster** — every host runs
the in-code default. Listed for completeness; override only with a
measured reason (see [`thresholds.md`](../conventions/thresholds.md)).

`PRECIS_DB_CONNECT_RETRY_SECONDS` (30), `PRECIS_EMBEDDER_TIMEOUT` (30),
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
`PRECIS_CORPUS_RECONCILE_REFRESH_HOURS`), and the log-handler batching
(`PRECIS_LOG_MAX_BUFFER` 50, `PRECIS_LOG_MAX_INTERVAL_SECONDS` 5).

**Assessment:** all at code defaults ⇒ no per-host drift to reason
about; the defaults are the CLAUDE.md-documented values. The only
knob worth a second look is `PRECIS_QUEST_WEEKLY_CHARS` (unset), which
**must** be set before flipping `PRECIS_QUEST_LOOP_ENABLED` — the meter
is character-count, not dollars (gr162594: the quest lane never reports
a $ cost).

---

## 10. Tier-3 IPC & build stamps (not configuration)

- **Per-invocation IPC** (a parent sets them per child; never
  "configure" these): `PRECIS_CURRENT_TODO`, `PRECIS_WORKSPACE`,
  `PRECIS_CURRENT_MODEL`, `PRECIS_CURRENT_AGENTLOG`, `PRECIS_SOURCE`
  (`precis-worker`/`web:reto`/…), `PRECIS_PROCESS`, `PRECIS_HOST_NAME`.
  Set correctly per daemon (each plist stamps `PRECIS_PROCESS` +
  `PRECIS_SOURCE`).
- **Build/provenance stamps** (baked into the image, surfaced in
  `/status`): `PRECIS_GIT_SHA`, `PRECIS_GIT_SHA_SHORT`,
  `PRECIS_GIT_BRANCH`, `PRECIS_GIT_DIRTY`, `PRECIS_GIT_LAST_TAG`,
  `PRECIS_GIT_DESCRIBE`, `PRECIS_BUILD_TIME`, `PRECIS_BUILD_HOST`,
  `PRECIS_BUILD_USER`. Not tunable.

---

## Shipped dark in the last ~48h — built but not turned on

Features merged to `main` in the last two days (per `git log`) whose
enable switch is still off, or whose activation needs a step beyond the
merge. **Deploy first** — most of these landed *after* the last cluster
`scripts/deploy`, so the code isn't live on the cluster yet regardless
of the flag (verify the deployed sha via `direct_url.json`, per the
deploy-sha memory).

| Feature (commits) | Switch to turn it on | Notes / gap |
|-------------------|----------------------|-------------|
| **Daily audio casts** — reading brief + nidra (`463d0cb8`, `edc99a1d`, `ae37657a`) | `PRECIS_CAST_AUDIO_ENABLED=1` is already set on **spark**, but you must **deploy** then run `precis cast schedule` to install the `level:recurring` watches | TTS render pass; compose is `claude_inproc` on melchior. Not deployed. |
| **`card_forge` morning card pass** (`ec4b3b4f`, `14890149`) | deploy + `precis cast schedule` + flip `PRECIS_CARD_FORGE_AUTONOMY=act` (default `report` = observe-only) | Mastery-from-Anki + mint 5/day + retire/rework. Shipped `main`, **not deployed**. |
| **Quest autonomous loop** (`2ce51f5f`…`45f19ef4`, slices 1–4e) | `PRECIS_QUEST_LOOP_ENABLED=1` **+** `PRECIS_QUEST_WEEKLY_CHARS=<n>` on the **melchior agent worker** | Deployed dark; also needs the struct-relax GPU lane on spark. Autonomous GPU/token spend — Reto's call. |
| **Patent FTO authoring loop** (`b9d775db`, `147a984f`, `5c0e9329`, `6a5d829d`) | No env flag — rides `plan_tick` once a patent project drives it; needs a first live-run validation (see `OPEN-ITEMS.md`) | `patent` kind is already live (`PRECIS_PATENT_RAW_ROOT` set). |
| **Diagram-propose autonomous drawer** (`6585223d`, `f22eccb4`) | `PRECIS_DIAGRAM_AGENTIC=1` (else auto: agentic when an MCP config is present) **+** a todo dispatching the `diagram_propose` job_type | Nothing dispatches to it yet. |
| **Chem deeper engines** — AiZynth (`9bc2f3c3`), LinChemIn (`fc41d983`), ASKCOS (`866d60b0`) | `route` kind surface is live (`PRECIS_CHEM_ENABLED=1`); the compute engines need their container/service env + deploy on spark | Slices 1b/2/3 shipped dark. |
| **Markup-first ingest** (devin, `e29b18a9`) | `PRECIS_FETCH_MARKUP=1` | Ships dark; off by default. |
| **Classify cascade** (older, still off) | `PRECIS_CLASSIFY_ENABLED=1` (+ optional `PRECIS_CLASSIFY_ESCALATE_MODEL`) | Enable as a trickle on one node like `PRECIS_SUMMARIZE_LLM`. |
| **Sandbox-run (slice 1)** (`sandbox_run` job_type) | `PRECIS_SANDBOX_ENABLED=1` on balthazar/spark | Build-only, no DB access, **no result harvest** (slice 2). Limited value until then. |

Already **un-darked / live** in the same window (for contrast): mermaid
kind (`c7ac23db`), protein/AlphaFold3 (`PRECIS_BIO_ENABLED=1`),
news-briefing audio (`PRECIS_BRIEFING_AUDIO_ENABLED=1` on spark), the
global budget breaker (default caps, active whenever a store is bound),
and mp3 podcast enclosures.

## Assessment summary — what's worth acting on

Most of the deployed state is **correct and deliberate** (single-writer
placement for Anki/OA/GP fetch, LLM passes pinned to the agent host,
compute lanes on spark, observe-first autonomy defaults, ship-dark
gates off). Items worth a decision, ranked:

1. **Consolidate the budget ceiling.** The global breaker
   (`PRECIS_BUDGET_DAILY_USD`, $20 default) is the binding constraint and
   trips before the planner's $50 `PRECIS_DAILY_COST_CEILING` ever
   matters. Set the breaker caps explicitly to your real numbers and drop
   (or deliberately out-scale) the planner daily ceiling. Keep the
   planner tick-cap + per-todo cap.
2. **Turn on the last-48h features you want live** (table above) — each
   needs a **deploy** first, then its flag/schedule step. The audio casts
   + `card_forge` are the closest to ready.
3. **Sandbox** is dark end-to-end (container deployed, pass gate off);
   activating slice 1 gives build-only runs with no harvest — park it
   until slice 2 unless you need build containers now.
4. **Paid OpenAlex leg** — the `PRECIS_OPENALEX_CONTENT_KEY` (vault) is
   the sole spend opt-in as of 2026-07-16; the old
   `PRECIS_OPENALEX_CONTENT_AUTO` second gate was dropped. Runway is
   guarded by the `fetch_oa:openalex_balance` low-balance alert
   (`PRECIS_OPENALEX_MIN_CREDITS` floor). Only bites on melchior (the
   fetcher).

**Not issues** (resolved during review): the fixer/backlog-groom runs
**locally on `hephaestus` (Reto's laptop)**, outside the cluster ansible
— its env lives there, not in `~/work/cluster`. `PRECIS_LOAD_CEILING`
is correctly left at default (wrong lever for RAM-driven jetsam).

> Deployment values are a scan snapshot of `~/work/cluster` at the time
> of writing. Re-verify against live daemons before an incident.
