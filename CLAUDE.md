# Claude Code — project brief

> **First**: read `AGENTS.md`. It is the canonical project guide
> (humans + agents). Conventions, workflow, definition-of-done,
> ingest guarantees — all there. This file is a current-state map of
> the discovery / task / worker / review subsystems a Claude Code
> session needs before touching them. It is **present-tense** — for
> the dated story of how each piece landed, read the **git history**
> (`git log`); there is no CHANGELOG file. Keep this file true:
> update it in the same commit that changes what it describes.

## Session workflow (worktree → ship)

Best practice for a unit of work:

1. **Start in a worktree.** Launch with `claude -w <name>` (alias of
   `--worktree`). Claude Code creates an isolated worktree at
   `.claude/worktrees/<name>/` on a new `worktree-<name>` branch, so
   the work is isolated from `main` and from sibling sessions.
2. **Do the work** in that worktree — implement, test, iterate.
3. **End with `/endsession`** (ship) **or `/go`** (ship **+ deploy**).
   Both run the deterministic `scripts/ship`: commit WIP → sync
   (`git fetch` + `git merge` main) → the container integration gate (auto-fix ruff, then
   authoritative `ruff` + `mypy` + `pytest`) → squash-merge to `main`
   (only if green) → reset the branch to the shipped `main` → local-main
   fast-forward. `/go` additionally runs
   `scripts/deploy` on a green ship to push `main` to the cluster
   (`ansible-playbook redeploy-precis.yml` — the dark-factory
   one-keystroke). Both **abort and report** on any gate failure; fix
   and re-run (the scripts are idempotent). Landing on `main` — and, via
   `/go`, on the cluster — is the end goal of a feature branch.

`scripts/ship` is **plain git — no git-town dependency** (this repo runs
flat feature branches on `main`, so git-town only ever did `fetch + merge
main` here). It integrates `origin/main` with `fetch` + `merge`, squashes
the branch onto `main` via `commit-tree` + a `--force-with-lease` CAS
push, then **resets the feature branch to the shipped `main`** so the next
ship starts at zero divergence — no phantom squash-artifact conflict on
already-shipped work. NB the merge target is `main`, not `master` — the
repo has no `master`.

## The todo tree (five slices)

`kind='todo'` is a hierarchical task graph unifying intent,
scheduling, execution, and review:

* **Hierarchy.** `parent_id` column on refs; a
  strategic / tactical / subtask gradient with walk-on-read ancestry
  and a 1/N rotation across strategics by 7-day picks. Reparenting
  goes through a reserved `parent` **link** relation (ADR 0027), not
  a raw column write.
* **`meta.auto_check` leaves.** Wait-for-condition evaluators under
  `auto_check_evaluators/`: `paper_ingested`, `discord_reply_received`,
  `time_past`, `tag_present`, `child_job_succeeded`.
* **`level:recurring` umbrella ("Watches").** `meta.schedule` (cron
  or `every:` shorthand) drives a per-minute spawner. `PRIO` is an
  int column on refs (1..10); `PRIO:*` tag stays as a back-compat
  alias.
* **Jobs hang off an owner ref (parent-kind polymorphic, ADR 0044).**
  `JobHandler.put` requires a `parent_id`, but that parent is one of two
  lanes, distinguished by its **kind** (`JOB_PARENT_KINDS`), not a
  declared flag. **Intent lane** — parent is a `kind='todo'`: the classic
  case (rotation + the `child-failed` bubble + `child_job_succeeded`).
  **Compute lane** — parent is a build subject (`structure`/`cad`/
  `draft`): a *derived* job (DFT relax / route / compile) — idempotent,
  content-addressed, cache-fillable — owned by the artifact, which has no
  rotation to enter. An intentful task that wants to *block* on a derived
  build links `requested`→job (migration 0046); `derived_job_succeeded`
  closes the requester on success and the failure-bubble follows the link
  on failure. This dropped ADR 0043's "relax needs a parent todo". The
  `dispatch` worker
  walks open todos carrying `meta.executor`, mints `kind='job'` under
  each with `FOR UPDATE SKIP LOCKED`, and auto-injects
  `meta.auto_check={'type':'child_job_succeeded'}`. On job failure
  the parent gets a `child-failed:<job_id>` open tag (the
  failure-bubble, `handlers/_job_bubble.py`); the doable view excludes
  bubbled parents so they stop re-entering the rotation until the
  owner decides retry / switch / give up.
* **Planner coroutines.** An `LLM:*`-tagged todo runs the `plan_tick`
  coroutine — each tick is a `kind='job'` that may mint children
  (`verdict: continue`) or yield (`ask-user:`) and still exit
  `STATUS:succeeded`. `child_job_succeeded` is guarded so it never
  auto-closes a parent that is `LLM:*`-tagged or still has a live
  child todo, and `dispatch` strips the spec when minting a
  self-resolving tick. Job lease is 90 min (covers a 60-min tick plus
  post-processing). A tick cut off by an **exhaustion** — the
  `--max-turns` ceiling *or* the wall-clock timeout (exit 124) — is
  **resumable, not a failure**: the executor (`_resume_reason`) marks it
  succeeded-but-non-blocking so `dispatch` re-mints a fresh tick, bounded
  by a per-parent streak cap (`meta.plan_tick_resume_streak`, default 3,
  env `PRECIS_PLAN_TICK_RESUME_CAP`) past which it bubbles as a real
  failure (the task needs splitting).
* **Views.** `view='tree'` walks `kind IN ('todo','job')` so child
  jobs render with a `⚙` marker; `view='attention'` unions
  `asking-reto` leaves + `child-failed` parents for asa-bot's preamble;
  `view='projects'` (`_todo_views.render_projects`) is a dashboard of
  workspace-owning roots. View dispatch in `handlers/todo.py` is a
  `TodoView` StrEnum + `_TREE_SEARCH_VIEWS` table with an import-time
  totality assert.
* **Projects.** A *project* is a strategic-root todo that owns a
  `meta.workspace` (no new kind). `TodoHandler.put` stamps a
  `project:<slug>` owner-path tag derived from `meta.workspace.path`
  (`utils/workspace.project_tag_for_path`), even on operator/CLI
  writes (not just planner ticks). A first-class `Workspace.brief`
  (`meta.workspace.brief`) cascades down the subtree and is injected
  as a `## Project context` block into the planner prompt's *variable*
  layer (`workers/planner_prompt._render_project_brief`) — kept out of
  the cached system layer since it is per-project. Skill:
  `precis-tasks-help` (Projects section).

## Review tiers

Three reviewers write memory digests, factored into
`workers/review.py` (`Reviewer` dataclass + `run_review_pass`
driver; adding one is a `Reviewer(...)` instance):

* `nursery` — SQL-only, every minute on the system worker. Flags
  orphans, stale claims, long waits, stuck doable, stalled recurrings,
  **spin loops** (any `(ref_id, source)` emitting >
  `SPIN_LOOP_EVENTS_24H` (200) `ref_events` in 24h), **plan-tick
  spins** (a planner parent minting > `PLAN_TICK_REMINT_24H` (16)
  `plan_tick` jobs in 24h — the coroutine "succeeds" each tick but never
  converges, which the resume-streak cap doesn't catch since it only
  guards exhaustion loops), and **worker health** (daemon liveness, not
  the todo graph): **worker-restart** (a `(host, process)` emitting >
  `WORKER_RESTART_STORM_1H` (8) `worker: started` boot rows in 1h — the
  jetsam-cull signature that was invisible for 1.5 days; the boot row is
  emitted at `cli/worker.run` startup, the only DB restart signal there
  is) and **dead-worker** (a continuous daemon in
  `WORKER_CONTINUOUS_PROCESSES` silent > `DEAD_WORKER_SILENCE_MIN` (10)
  min while its host is otherwise alive) and **dispatch-stall**
  (`claude_inproc` jobs sitting `STATUS:queued` > `DISPATCH_STALL_MINUTES`
  (15) with **zero** live-lease jobs running — the single agent-profile
  executor stopped claiming: culled / OAuth-401 / never-started. Minting is
  cluster-wide but execution is melchior-only, so this is the "45 min dark"
  SPOF, gripe 55748. The "nothing running" gate distinguishes a dead
  executor from a healthy-but-backlogged one; symptom-level, so it also
  catches an agent worker that never started — which has no log rows for
  dead-worker to age). These three are the only
  `critical` categories — a thrashing/dead/stalled worker stalls the planner
  cluster-wide, so on the *first* sighting `raise_alert` (now returning
  `(ref_id, is_new)`) fires a one-shot `notify_critical_alert` — a
  `kind='message'` to `PRECIS_OPS_ALERT_TARGET`
  (`discord/<guild>/<channel>`, the same asa_bot channel the daily news
  briefing uses; no webhooks exist in this deployment) via
  `pg_notify('precis.messages')`; default unset → the push merges dark;
  alerts still land in `/alerts` + agent triage. Each
  finding is raised as a `kind='alert'` (one per condition, `alert_source
  = nursery:<category>`, deduped on `meta.fingerprint`; a non-ref-scoped
  worker-health finding sets `ref_id=None` + an explicit
  `fingerprint_key`; cleared conditions auto-resolve) — **not** a
  `kind='memory'` digest any more. See `## Other live affordances` →
  `alert`, and `precis-nursery-help`. (Replacing the digest killed a self-spin: the
  spin-loop finding set churns every second, so the old
  `(category, ref_id)` digest fingerprint changed every pass and the
  per-node per-minute writer emitted >2000 near-dup memories/day.)
* `structural` — opus, 6h dedup, agent profile. Drift, sibling
  contradictions, depth/fanout warnings.
* `deep_review` — opus, weekly dedup, agent profile. Allen-style
  archive / prune / rebalance / long-wait review.

## Workers

**Two `precis worker` profiles, four LaunchDaemons total.**

* `precis worker --profile=system` runs on every cluster node and
  drives every chunk-level + SQL ref-level pass: `embed`, `summarize`,
  `chunk_keywords`, `chase`, `fetch`, `gp_fetch`, `tag_embeddings`,
  `auto_check`, `schedule`, `nursery`, `dispatch`, `sweeper`,
  `job_coordinator`, `job_ssh_node`, `wake_runner`, `clusterize`,
  `corpus_reconcile`, `paper_reconcile`.
  (`llm_summarize` is opt-in on top — env `PRECIS_SUMMARIZE_LLM=1` or
  `--only llm_summarize`; enabled on melchior as a deliberate trickle.
  `job_claude_docker` is opt-in on top too — env `PRECIS_SANDBOX_ENABLED=1`
  or `--only job_claude_docker`; default-OFF so the slice merges dark,
  meant only for the `agent_sandbox_host` nodes, **never melchior**.)
* `precis worker --profile=agent` runs the passes that need the
  hermes OAuth / `~/.claude` state on melchior: the LLM-heavy
  reviewers (`structural`, `deep_review`) plus `job_claude_inproc`
  (planner-coroutine slice — moved off system 2026-06-15 so data-host
  workers stop claiming plan_tick/fix_gripe jobs they can't run and
  false-bubbling `child-failed`) and `quota_check`. It skips the
  embedder load it doesn't need. `quota_check` also **watches claude
  auth**: `claude_quota.refresh_snapshot` returns a `RefreshOutcome`,
  and a genuine 401 (`AUTH_FAILED`, distinguished from free-tier
  `NO_LIMITS` / transient `UNAVAILABLE`) raises a **critical**
  `quota_check:auth` alert (+ one-shot `notify_critical_alert`) so a
  stale/revoked OAuth token pages instead of silently 401-ing every
  agentic call for a day; auth recovering auto-resolves it.
* `dream_agent` keeps its own 15-min cadence via `dream-pass.sh`,
  and `cron-tick` is the fourth daemon. Each heavy pass dedups on its
  tier-tagged memory and load-gates on `PRECIS_LOAD_CEILING` (default
  `os.cpu_count() * 1.5`).

**Notable passes:**

* `cast_audio` — the daily audio **casts** (docs/design/reading-prep-loop.md
  §Audio). Two standing casts ride one produce→narrate→publish spine, two voice
  profiles: **`reading`** (morning situational-awareness brief, `bm_george`,
  ~15 min — `reading/briefing_cast.py` unions news/activity/recall lanes, each
  degrade-to-empty) and **`nidra`** (evening concept-graph meditation,
  `af_nicole`, ~45 min segmented walk — `reading/meditation.py`). Producers
  persist a standalone dated `draft` marked `meta.cast`; `workers/cast_audio.py`
  (spark, default-OFF `PRECIS_CAST_AUDIO_ENABLED` + `PRECIS_TTS_IMAGE`) narrates
  any un-narrated cast draft via `render_narration` → `render_episode` →
  `publish_episode(source="reading")`, idempotent on `meta.audio_episode_id`
  (sibling to `briefing_audio`). Compose is the `reading_brief`/`meditation`
  **coordinator** job_types (any system node, dodging the melchior `claude_inproc`
  SPOF) on daily `level:recurring` watches. CLI: `precis cast run <reading|nidra>
  [--publish]` + `precis cast schedule [--now]`. Skill: `precis-audio-help`.
* `llm_summarize` — model-authored two-part summary (gist + a
  sentence of detail) into `chunk_summaries` under
  `summarizer='llm-v1'`, distinct from the lexical `rake-lemma` row
  and the per-chunk KeyBERT keywords. A ref-pass (own claim/writes),
  not a pure `WorkerHandler`. Registered by
  `0025_register_llm_summarizer.sql`.
* `sweeper` — fails `kind='job'` rows whose `STATUS:running` is older
  than `PRECIS_STUCK_JOB_HOURS` (1.0h), tagging `swept:claim-orphaned`
  so the parent's failure-bubble unblocks the cascade. Recovers
  deploy-time claim orphans.
* `corpus_reconcile` — maintains the per-host `pdf_locations` presence
  ledger (migration 0052). Each node stats the held-paper PDFs under its
  own `PRECIS_CORPUS_DIR` roots (preferring `pdfs.storage_path`, falling
  back to the `corpus_pdf_dest` cite_key convention) and records a verdict
  per `(pdf_sha256, host)` — the path found, or `''` for checked-and-absent.
  The draft reader's held-but-missing ▲ then reads that ledger
  (`Store.pdf_missing`: checked-yet-no-fresh-copy) instead of re-stat-ing at
  request time, so the marker is a corpus-wide fact independent of the web
  host's mounts (ADR 0029). Self-throttling via a refresh window
  (`PRECIS_CORPUS_RECONCILE_REFRESH_HOURS`, default 6, ≪ the ledger TTL
  `PRECIS_PDF_LOCATION_TTL_DAYS`, default 7); idle once every verdict is
  fresh. No-op on a node with no corpus roots.
* `paper_reconcile` — the standing dedup sweep behind `precis
  reconcile-duplicates`, now on a cadence (it was manual-only). Folds
  duplicate paper refs into the survivor across three classes: shared
  `pdf_sha256`, DOI-modulo-case, and **id-less title-only stubs that
  duplicate a held paper** (`dedup.reconcile_by_title_similarity`, the
  Phase-3 near-dup case — auto-merge only the high-confidence band, the
  rest surfaced for review). Prevention is upstream in
  `Store.upsert_stub_paper` (a title-only acquire fuzzy-matches held
  papers first). Cheap between runs: an `app_state`
  `paper_reconcile:last_run` marker gates the pass to once per
  `PRECIS_PAPER_RECONCILE_REFRESH_HOURS` (default 24), and a single-runner
  `pg_try_advisory_lock` keeps just one node sweeping corpus-wide. The same
  pass also runs the deterministic **hygiene heals** (`ingest/paper_hygiene.py`):
  rebuild drifted `card_combined` chunks (title repaired but the embedded
  search card never rewritten), collapse `superseded_by` chains onto the
  final live survivor, repoint non-`supersedes` links off soft-deleted
  papers, and **re-queue stranded OA fetches** (`requeue_stranded_fetches`
  — a stub that logged `fetch_ok` but never ingested, i.e. `pdf_sha256`
  still NULL, older than `PRECIS_OA_STRANDED_HOURS` (default 48): the
  pre-2026-06-19 inbox-misconfig signature. Deletes the stub's `fetcher:%`
  events to reset the exponential backoff so the fixed pipeline re-fetches,
  stamping a one-shot `meta.oa_requeued` guard so a re-failure can't spin).
  See `docs/design/duplicate-paper-handling.md` (Phase 3).
* `fetch` / `chase` backoff — **both exponential**. The OA fetcher's
  retry window arms on any `fetcher:%` event (not just `unpaywall`,
  which is disabled in prod) and doubles per prior attempt
  (`base * 2^(attempts-1)`, capped). Finding-chase skips a `waiting`
  finding inside an equally-exponential window — `WAITING_BACKOFF_MINUTES`
  (60) doubling per consecutive `waiting` up to `WAITING_BACKOFF_MAX_MINUTES`
  (1440), the run resetting on any non-`waiting` outcome. Both fixes
  kill `ref_events` spin-loop floods. NB the fix only helps once
  *deployed* — prod ran pre-fix code well after the merge, so a
  spin-loop digest spike usually means "redeploy", not "new bug"
  (check the deployed sha under `~deploy/.cache/uv/git-v0/checkouts/`).

**Unified `claude -p` agentic dispatch — `utils/claude_agent.py`.**
Peer to `utils/claude_p.py` (one-shot JSON judge). Carries the
agentic flag set (`--mcp-config` / `--strict-mcp-config`,
`--append-system-prompt`, `--max-turns`, `--permission-mode`,
optional `--bare`, `--disallowed-tools`) + cost cap + wall-clock
timeout + structured `log_event` to `ref_events`. The reviewers,
`dream_agent`, and the web "ask a follow-up" path all share this
surface. Stub-binary tests via `PRECIS_CLAUDE_BIN`. A non-zero exit
that is a **resumable exhaustion** — the `--max-turns` ceiling or the
`--max-budget-usd` cap, detected via the trailing `stream-json` result
event (`_recoverable_exhaustion`) — is **recovered, not raised**: the
wrapper returns the partial `AgentResult` (final text via the result
event, falling back to the last assistant message rather than dumping
the raw JSON stream), mirroring how `plan_tick` treats exhaustion as
resumable. This stopped the follow-up "ask & think" path surfacing a
bare `⚠️ thinking failed: …exited 1:` whenever the agent ran out of
turns. Genuine errors still raise — now with the `terminal_reason`
folded into the message, since stream-json errors leave stderr empty.

**LLM independence — the switchable router (`utils/llm/`, ADR 0046).**
Every routed call goes through `dispatch(LlmRequest)` → a narrow
`LlmProvider` port (`run(req, *, model) -> LlmResult`) picked from a
`Transport`-keyed registry. `claude -p` is now just two adapters
(`ClaudeAgentProvider`/`ClaudePProvider`) among peers — Anthropic is a
swappable leaf. A `Backend` switch (`PRECIS_LLM_BACKEND`, default
`anthropic`, **ships dark**) flips cloud work to an **OpenAI-compatible
OSS backend** (OpenRouter/DeepInfra/remote vLLM at `PRECIS_LLM_BASE_URL`,
API key from the secrets vault via `get_secret('PRECIS_LLM_API_KEY')`):
tool-less calls → `OpenAICompatProvider`, tool-using calls →
`OpenAIToolsProvider`. The latter is the OSS **`tools=` agent loop**
(`utils/llm/openai_tools.py` engine + `precis_tools.py` bridge): it
advertises the precis verbs from `TOOL_REGISTRY` as OpenAI function
schemas and executes each tool call **in-process** via `runtime.dispatch`
(no MCP socket round-trip), rebuilding ADR 0024's reversed loop behind the
port. Model ids resolve from the same `PRECIS_MODEL_*` table, so switching
model is env-only. With the backend unset, behavior is byte-identical to
`claude -p`. **Unit 4b (call sites folded through the seam) is done**:
dream, the structural/deep reviewers, cad_propose/cad_discuss/
structure_propose, the web follow-up (`precis_web/ask`), and the
`claude_p` judges (chase, good_search triage, figure) all call
`dispatch(LlmRequest)` now — so `PRECIS_LLM_BACKEND` switches the whole
agentic + judge surface. (Still direct `claude -p`: `plan_tick` /
`fix_gripe`, which build the subprocess themselves — a separate
migration.) Deferred: a `FailoverProvider` ladder (method + model
failover) over the same port.

## Discovery layer (F20)

Per-chunk KeyBERT supersedes the dropped `ref_segments` /
`ref_segment_sentences` tables (migration `0003_drop_legacy_segments`;
ADR 0018 status note):

- `chunks.keywords TEXT[]` (canonical lower-case forms, GIN-indexed)
  + `chunks.keywords_meta JSONB` (versioned envelope: short/long pairs
  + KeyBERT scores). Worker: `chunk_keywords` (claim shape
  `keywords IS NULL OR keywords_meta->>'version' != current`, so
  bumping `KEYWORDS_VERSION` lazily re-claims the whole corpus).
- `view='toc'` (papers): DP-clusters the keyword arrays at request
  time — `src/precis/utils/toc_db.py` `render_from_store`. No
  precomputed segment rows.
- `view='toc'` (skills): per-request DP+KeyBERT via
  `src/precis/utils/toc.py`, memoised per `(slug, scope)` since skill
  files are static for the process lifetime.
- Search no longer reranks against `ref_segment_sentences`; result
  rows carry no `excerpt @ ~N` sub-lines.

Policy: `docs/conventions/discovery-layer-policy.md` (F20-rewritten).

## Chunk-tag classifier (ADR 0047 cascade)

Controlled chunk/paper tags written by a measured **cascade**, not a
single model. Axis defs live in `src/precis/data/axes/*.yaml` (id +
values + prompt + few-shot + `applies_when`); gold sets + accuracy live in
`scripts/classify/` (`gold_set/`, `eval-classifier`, `EVAL_RESULTS.md`).

- **Why a cascade.** The free local model (`summarizer` alias) is ~72% on
  the 11-way `role` — it fails the *attribution test* (own-work vs
  others') — but 94% at junk (furniture vs substance) and **88% /
  91%-own-precision** at the 3-way collapse **`role3`** (own / background
  / furniture). Human agreement is ~89%, so ~85-90% is the ceiling; the
  residual is real ambiguity, absorbed by gold `accept:` sets + the
  query-time agent. So the cheap model does the coarse, high-value calls
  and a stronger model is reserved for the narrow residual.
- **Tiers.** 0: free regex drops furniture (~24% of prod). 1: `junk` gate
  → `role3`, local, cheap. 2 (optional, gated): re-judge `own` chunks with
  a stronger model (`--escalate-model` / `PRECIS_CLASSIFY_ESCALATE_MODEL`).
- **Writes** `Tag.closed("ROLE3", own|background|furniture)` → `chunk_tags`
  (`pos=ord`, single-valued). `ROLE3:own` is the citation-grounding filter
  (91% precision) — use as candidate-gen/soft-boost, verify with the agent,
  never a lone hard precision gate.
- **Pass.** `workers/classify.py` `run_classify_pass` (self-contained
  ref-pass like `llm_summarize`; `chunk_claims` artifact
  `classify:cascade-v<CLASSIFY_VERSION>`, idempotent, reversible),
  registered in `cli/worker.py` **default-OFF** (`PRECIS_CLASSIFY_ENABLED=1`
  / `--only classify`). Manual backfill + eval: `scripts/classify/classify
  --cascade` (dry-run default; `--commit` to write). Full design:
  `docs/design/chunk-classifier-cascade.md`.

## Other live affordances

One line per affordance — code path + skill for the detail. The
`precis-*-help` skills are the authoritative, on-demand reference (the MCP
serves them via `get(kind='skill', id=…)`); this list is just the index.
The master kinds table lives in the `precis-overview` skill.

- **Cluster maps (`/clusters`)** — spatial SOM browse over chunk embeddings;
  `clusterize` worker (`utils/cluster_map.py`, numpy-only, warm-started daily),
  `0027_clusterize.sql`, `precis_web/routes/clusters.py`.
- **`folder`** — single-parent placement container for authored artifacts on
  `refs.parent_id` (ADR 0045); `handlers/_placement.py`, `KindSpec.role`,
  `search(folder=)` scopes a subtree. Skill: `precis-folder-help`.
- **`plan`** — a thread's reasoning outline (ADR 0051 §2b, slice A1): a
  hierarchical todo-list + notes on the `draft` chunk-tree substrate
  (`handlers/plan.py`, reusing the kind-parameterized `DraftMixin`), but a
  **distinct kind that is never exported** (`export/guard_exportable`,
  `corpus_role='none'`). Rendered whole with `[open]`/`[wip]`/`done:` +
  `?`/`⚠` + a model-owned `▸` cursor (`meta.cursor` on the ref); nodes
  `pe<id>`, one per project via `plan-of`. Migration `0056_plan_kind.sql`.
  Ships dark — nothing dispatches to it yet.
- **`figure`** — an interactive **SVG canvas you draw *with* the model**
  (`handlers/figure.py` + `precis/figure/{svg,turn}.py`, reusing the
  kind-parameterized `DraftMixin`), a **distinct kind that is never exported**
  (`corpus_role='none'`). Three model-owned docs — the SVG source (`figure_node`
  chunk `fn<id>`, `meta.no_index` so raw markup never embeds), a **shared
  vocabulary** (`figure_vocab`, embedded — high-level, human-facing), and
  **implementation notes** (`figure_notes`, `no_index` — the model's private
  design log; migration 0058) — plus a `figure_turn` chat log. Vocab/notes are
  born empty (the "what this doc is for" seed is instruction, kept in the
  prompt/`precis-figure-svg` skill, never stored as content). The pinned
  `precis-figure-svg` skill body is prepended to the turn prompt (editing the
  skill edits the prompt). The
  draw-with-me turn loop (`figure/turn.py`: state + two lints (compile +
  out-of-bounds) + vocab + user msg → whole-source rewrite, sanitize, bounded
  auto-heal) is the **web** editor `/figure` (`precis_web/routes/figure.py`);
  the canvas renders SVG as a script-safe `<img>`. MCP surface is
  put/get/edit/delete/link. Migration `0057_figure_kind.sql`; skills
  `precis-figure-help` + `precis-figure-svg`. Slice 1 = SVG 2D, browser-
  rendered; **deferred**: PNG/animated raster export, three.js/`scene3d` mode,
  per-node chunk split, draft-embedding, `read(handle)` reference tool.
  Since ADR 0057 `figure` is the **SVG instance** of a shared **diagram core**
  (`src/precis/diagram/` — the `DiagramLang` port + the generic turn loop /
  context assembler; `figure/turn.py`+`context.py` are thin shims), and its
  elements **bind to the chunks they depict** (see `mermaid` below).
- **`mermaid`** — a **mermaid diagram you draw *with* the model** (flowchart /
  sequence / state / class …), the **second instance** of the diagram core
  beside `figure` (ADR 0057, slice 4): same draw-with-me turn loop, three docs
  (`mermaid_node`/`mermaid_vocab`/`mermaid_notes` + `mermaid_turn`), same
  handle scheme (`mm<ref>`/`mn<chunk>`), never exported (`corpus_role='none'`).
  Validation / SVG render / PNG-PDF export are **pure-Python via `mermaidx`**
  (`src/precis/mermaid/mermaid.py::MERMAID_LANG` — the real mermaid.js in an
  embedded QuickJS + resvg; **no Node, no Chromium, no container**), lazy-
  imported behind the `[mermaid]` extra. **Element→chunk bindings (ADR 0057):**
  a node (by its stable id) binds to the `dc…`/`pc…`/`me…` chunk it depicts via
  a chunk-level `depicts` link (element id in `links.meta.elements`); the turn
  prepared-context lists each node + topology + the linked chunk body, and a
  `[binding]` lint catches drift. MCP `handlers/mermaid.py` (put/get/edit/
  delete/link) + web `/mermaid` (`precis_web/routes/mermaid.py`, renders
  server-side through figure's `sanitize_svg`). Ships **dark** behind
  `PRECIS_MERMAID_ENABLED`; migration `0066_mermaid_kind.sql`; skills
  `precis-mermaid-help` + `precis-mermaid`. **Autonomous tick:** the
  `diagram_propose` job_type (`workers/job_types/diagram_propose.py`, ADR 0057
  slice 5) runs **one** figure/mermaid turn against the model from an
  instruction + seed chunk handles — mutating the diagram in place + reconciling
  bindings, owned by the diagram (compute lane; figure/mermaid set
  `KindSpec.can_own_jobs`). Deferred: a full mermaid source grammar (node
  extraction is a scan), rich cross-kind seed rendering.
- **`gripe`** — first-class bug tracker; body + comment timeline as chunks
  (`gripe_body`/`gripe_comment`), so they embed + keyword-index automatically.
- **`anki`** — spaced-repetition **cloze** cards (`{{c1::…}}`) that live in the
  corpus and sync to AnkiWeb. Numeric-ref `handlers/anki.py`; body is cloze
  markup, `meta` carries the generic Anki note shape (`notetype`/`deck`/`fields`,
  optional terse `Back Extra` after a lone `---`), emits a markup-stripped
  `card_combined` chunk so cards embed + search. **Anki owns scheduling — no
  SM-2.** Supersedes and retires `flashcard` (handle prefix `fc`→`ak`; migration
  0060). **Headless AnkiWeb sync** (`src/precis/anki/`, `precis anki-sync`, gated
  `PRECIS_ANKI_ENABLED`, `anki` wheel lazy-imported/ansible-installed): precis is
  the Anki client holding one `.anki2` mirror; add-only-own-notes by stable guid,
  guard allows FULL_DOWNLOAD but **refuses FULL_UPLOAD**, reads decay stats back
  into `meta.anki_stats`. **precis-fix** (`anki/fix.py`, `--fix`): tag a card
  `precis-fix` in Anki + a comment → LLM rewrites it → written back (per-card
  opt-in widening of own-notes-only). **Foreign-card read-only PG projection**
  (`anki/project.py`, `--project`): every Anki card (any notetype) mirrored into
  PG as a read-only `anki` ref (`meta.source=anki-foreign`), content-hash-gated so
  only changed cards re-embed (stats refreshed cheaply each sync), vanished ones
  soft-deleted — the whole collection searchable + feeding the knowledge-model,
  can't corrupt the account. **Per-card decks** (`deck-<topic>` tag →
  `Precis::<topic>` sub-deck). **Leech-finder** `get(kind='anki', id='/leeches')`
  surfaces bad-recall cards (high lapses / collapsed ease from `meta.anki_stats`)
  → fix-cloze-or-study. Design `docs/design/anki-integration.md`; skills
  `precis-anki-help` (ref) + `precis-cloze` (authoring craft).
- **`concept` + the reading-prep loop** — an adaptive, activity-driven study
  system that preps the human on what the corpus is working on (design-of-record
  `docs/design/reading-prep-loop.md`, **ships dark, in progress**). The spine is
  a bespoke **concept graph**: `kind='concept'` (numeric-ref `handlers/concept.py`,
  handle `cn`, migration 0063) is a node in the learner's knowledge graph — a term
  with a continuous **mastery** field + derived state + an embeddable
  `card_combined` definition (so a concept *is* a vector), and typed edges
  `has-prerequisite`/`prerequisite-of` (the learning DAG), `analogy-of`,
  `contrasts-with`, `represents`. Node model + promotion live in
  `src/precis/reading/` (`concepts.py`, `promote.py`). Slices built:
  **(1)** `paper_glossary` worker (`workers/paper_glossary.py`, default-OFF
  `PRECIS_PAPER_GLOSSARY_ENABLED`) — a per-paper inferred glossary as a
  `card_glossary` (ord=-1000) derived chunk; **(2a/b/c)** concept kind + graph
  relations + promotion (`reading/promote.py`: glossary terms → concept nodes,
  corpus-wide **name-anchored dedup** via `meta.norm_name`, cohort membership in
  `meta.cohorts`, `derived-from`→paper provenance). Remaining: graph-edge
  inference, mastery-from-Anki, embedding routing (reading-readiness /
  shortest-path / **daily review-path walk**), booklet, cards-as-representations,
  briefing+audio. **Anki is a renderer, not the brain** — the concept graph is the
  source of truth; leaf cards sync down.
- **`quest`** — the striving above the work (design-of-record
  `docs/proposals/quest-layer.md`; slice 1 **live, read-only, does not steer
  yet**). A quest is a **perpetual, unachievable striving** (the medieval Grail
  sense) — the **only** new aim-kind (numeric-ref `handlers/quest.py`, handle
  `qu`, migration 0065, `emits_card` so it *is* a vector, `corpus_role='none'`).
  Never `done`: lifecycle `active|dormant|abandoned`, enforced in the handler's
  `tag()` (STATUS is a shared union axis, so the value-subset is guarded per-kind).
  Achievable work stays ordinary todos/projects marked `serves` → the quest — a
  **DAG of strivings** above the todo tree, walked by `view='tree'` (servers by
  kind + sub-quest recursion + deed ledger). Two records: an **append-only
  `quest_log` logbook** (the gripe body+comment pattern — WORM, dated, typed
  entries `note·observation·hypothesis·result·decision·dead-end·milestone·
  reflection·cost` + `by`; a `milestone` is a deed, `cost` feeds the **tote** =
  a query over the dated log, no separate cost store), and a dossier `draft`
  (arrives with the loop, slice 4). **Slice 2 (reweighting) live**
  (`src/precis/quest/reweight.py`): priority flows down the `serves` DAG
  (max-agg, `STRIVING_DECAY` per quest→quest ladder hop; only **active** quests
  pull; canonical priority = `refs.prio`, set via a `PRIO:` tag synced in the
  handler) into three sinks — **rotation** (`_fetch_doable`/`render_roots`
  discount a strategic's picks by served weight), **acquisition** (`fetch_oa`
  claim tiers a quest-serving stub ahead), **reading**
  (`build_meditation(bias_active_quests=)`, dark until reading-prep slice 3). A
  **no-op until quests + servers exist**, so it's live without a flag. Coming:
  gap surfacing (slice 3), the autonomous research loop (local grind + frontier
  steering, materials as `structure` servers, slice 4). Skill:
  `precis-quest-help`.
- **`alert`** — machine-detected ops/health conditions (spin loops, orphans),
  raised via `precis.alerts.raise_alert` (fingerprint upsert + auto-resolve),
  read via `AlertHandler`/`/alerts`. **Not embedded.** Skill: `precis-alert-help`.
- **`agentlog`** — per-run attribution record (prompt + model + `touched` links
  to every chunk a run wrote), **not embedded**; `precis.agentlog` write side,
  sweeper GCs past `PRECIS_AGENTLOG_RETENTION_DAYS`. Skill: `precis-agentlog-help`.
- **`job` substrate** — `meta.job_type`+`meta.executor`, `STATUS:` tag,
  forensics as `job_event`/`job_summary`/`job_result` chunks; `claude_inproc`
  executor; `fix_gripe` is the reference job_type. The `claude_docker`
  executor (`job_claude_docker` pass, **default-OFF** under
  `PRECIS_SANDBOX_ENABLED`) runs the `sandbox_run` job_type as a detached,
  cgroup-capped, poll-reaped container on an `agent_sandbox_host` — slice 1
  is the stub-podman substrate (mint→claim→launch→poll→terminal, `mode:build`
  only; harvest is slice 2). See `docs/design/sandbox-run.md`. Skill:
  `precis-job-help`.
- **`structure`** — atomistic cell+bond IR (ADR 0043); typed ops + in-memory
  probes, relax on the GPU node (derived-lane job, ADR 0044), cursors/measures
  on `struct_measures`, web `/structure`. Skill: `precis-structure-help`.
- **`citation`** — verifier-workflow kind (`text`+`source_handle`+`source_quote`
  +`verifier_confidence`, `link='paper:<slug>'`); tex `\citequote` persists the
  same quote. Skill: `precis-citation-help`.
- **`cfp`** — spec-role sibling of `paper` (proposal requirements doc); same
  Marker→chunks ingest + reader, `KindSpec.corpus_role='spec'` (never cited as
  evidence), links to its project via `has-requirement`. Skill: `precis-proposal-help`.
- **Term registry (`draft`, ADR 0052)** — glossary / patent parts / manufacturing
  components are one abstraction over the `chunk_kind='term'` leaf, discriminated by
  `meta.registry ∈ {glossary,parts,components}` + a per-registry numbering policy
  (`src/precis/draft/registry.py`: `components→insert`/frozen `meta.callout`,
  `parts→render`/positional numerals). Store: `defined_terms` (rich hover map) +
  `ensure_registry_heading(role)` (lookup-by-tag → adopt-legacy → one-per-role
  reconcile) + `parts_callout_map`. Reader: rich `.pa-pop` card (MPN/mfr/datasheet)
  + a bare `[[dc…]]` part ref renders as its numeral (`linkify.callouts`). No new
  kind, no migration. Section-style skill: `components.md` (+ `patent-image-part`).
- **Keystone kinds (`cad`/`pcb`/`structure`)** — "own a legible IR, rent the
  heavy kernel only at export" (ADR 0041/0042/0043); the LLM traverses a graph,
  never pixels. `pcb` exporters in `src/precis/pcb/export.py` (JLCPCB BOM/CPL —
  **footgun:** CPL wants CCW, `jlc_rotation(r)=(360-r)%360`), route via
  `pcb/route.py` (headless Freerouting, skips if absent). Skills: `precis-pcb-help`.
- **`cad` web editor (`/cad`)** — three.js viewer + edit-by-prompt. Viewer
  tessellates **client-side** from a ~1 KB recipe (`GET /cad/<slug>/scene.json`)
  via `static/cad-tessellate.js` (a port of `cad/tessellate.py`, drift-guarded
  byte-for-byte by `tests/test_cad_parity.py`, node-gated); `model.gltf` kept for
  download + solid-mode. Server-side STEP/STL/3mf/scad export; `cad_propose` job →
  `CadHandler.derive`. Analysis is off the render path (`GET /cad/<slug>/analysis`,
  memoised); `cad/bulk.py` volume is an exact ray-interval quadrature, not the old
  200k-point Monte-Carlo. Drive (`/drive`) is the default landing. Skill: `precis-cad-help`.
- **Broad + deep paper search** — Tier 1 `search(kind='paper', queries=[…],
  answers=[…HyDE], per_paper=N)` RRF fusion; Tier 2 `good=True` mints an async
  `good_search` coordinator campaign. `docs/design/good-search-coordinator.md`;
  skill: `precis-search-help`.
- **`chunks.numerics TEXT[]`** — GIN-indexed lexical filter
  (`WHERE numerics @> ARRAY['1.523 eV']`); direct-SQL only, not yet in search verbs.
- **`precis web`** — browser UI (Tasks/Papers/Console/Conversations/Status).
  Two-pane paper reader (`routes/papers.py` + vendored pdf.js); the **draft
  reader** (`routes/drafts.py`) is a true virtual scroller for 10k-block drafts
  (skeleton + windowed DOM, no IntersectionObserver — see git log for the
  feedback-loop lesson). `precis_web` is a sibling package over the handlers (ADR 0026).
  **Export can bundle the cited sources** (`export/sources.py`,
  `collect_cited_sources`/`build_sources_zip`): the reader's `+ sources`
  checkbox appends every cited paper/datasheet PDF the host holds to the PDF as
  a `pdfpages` appendix (`export_draft(include_sources=True)`) — Word gets a zip
  (`report.docx` + `sources/`) since it can't embed PDF pages — and
  `GET /drafts/{id}/papers.zip` (also `precis draft papers`) zips just the cited
  PDFs + a `manifest.txt`. PDFs resolve via the same corpus resolver as
  `corpus_reconcile` (`corpus_layout.rebase_onto_local`); the corpus being
  per-host, unlocatable sources are listed in the manifest rather than failing.
- **SSRF guard** — `src/precis/utils/safe_fetch.py` (used by `handlers/web.py`
  + `workers/fetch_oa.py`); DNS-resolves + revalidates every redirect against the
  private/loopback/link-local/cloud-metadata blocklist.
- **Ingest hygiene** — pysbd sentence splitter in the chunker fallback chain;
  dehyphenation in `marker._clean_text`; HNSW index on `chunk_embeddings.vector`.

## LLM-facing skill index

Lives under `src/precis/data/skills/precis-*-help.md`. Start at
`precis-toolpath-help` (canonical call sequences per scenario);
`precis-overview` has the master kinds table + skill index (it, plus
the synthesised `precis-help`, is the authoritative kind catalogue —
the README lists only a sample). Cross-refs: `precis-tasks-help`,
`precis-decomposition-help`, `precis-auto-tasks-help`,
`precis-recurring-help`, `precis-dispatch-help`, `precis-job-help`,
`precis-fix-gripe-help`, `precis-nursery-help`.

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| To-do list / what's planned next | `OPEN-ITEMS.md` |
| Mission / pitch narrative + facts| `docs/mission.md` (positioning, not architecture — copy from here for decks/talks) |
| Master kinds table + call recipes| skills: `precis-overview`, `precis-toolpath-help` |
| Workflow + lint/test commands    | `AGENTS.md` |
| Dated history of every change    | `git log` (no CHANGELOG file) |
| Full schema (prose)              | `docs/design/storage-v2.md` (F20-amended) |
| Full schema (visual)             | `docs/design/schema-v2.svg` (PUML in same dir — carries a drift note; redraw pending) |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| ADR index + supersession graph   | `docs/decisions/README.md` |
| F20 (per-chunk keybert)          | `src/precis/workers/chunk_keywords.py` header + `src/precis/utils/toc_db.py` header |
| ADR 0018                         | Superseded by F20. Keep for history, do not implement against. |
| Agent-runtime surface (skills)   | `src/precis/data/skills/precis-*.md` |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/` (`embed`, `summarize`, `llm_summarize`, `chunk_keywords`, `chase`, `fetch_oa`, `dispatch`, `sweeper`, `nursery`, `review`, `runner`) |
| Web UI                           | `src/precis_web/` |
| Discord bridge (asa)             | `src/asa_bot/` — a sibling package like `precis_web`; the `asa-bot` entry point + `[asa]` extra (discord.py). Talks to `precis serve` over stdio; deployed as `precis-mcp[asa]` (folded in from the standalone asa-bot repo 2026-07-14). |
| SSRF guard                       | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` file
  under `src/precis/migrations/`. If you find a bug in a sealed
  file, ship a new forward migration that corrects it. Rationale
  lives in `docs/decisions/0005-greenfield-migrations.md`. A fresh
  DB does **not** replay the whole chain: it loads the generated
  `migrations/baseline/schema.sql` snapshot (the chain compiled to
  one file, self-stamping the ledger) and applies only the tail.
  Regenerate the snapshot with `scripts/bump` / `precis db
  dump-schema` — **never hand-edit it**; it is checked against the
  files. This is a dual-track scheme, not a greenfield (ADR 0031).
- **`uv` for everything.** Bare `pip` / `pytest` / `mypy` are
  not reproducible. Use `scripts/dev pytest …` inside the
  container, or `uv run …` on the host.
- **Run the FULL suite in the dev container, not the host.** The
  host venv is deliberately torch-free (no `[paper]` / `[embed]` /
  most extras), so a host `uv run pytest` reports dozens of spurious
  `ModuleNotFoundError` failures/errors (`sympy`, `marker`, `lxml`,
  `sentence_transformers`, …) that are **not real bugs**. The
  `precis-mcp:dev` image bakes **all extras** into `/opt/venv` and
  wires `PRECIS_TEST_PG_URL`, so the canonical green run is::

      scripts/dev pytest            # full suite, all extras, DB wired

  Under the hood that is `docker compose … run --rm precis-dev`,
  which bind-mounts THIS repo at `/app` (live edits, no rebuild). A
  long-lived `precis-mcp-dev-*` container works too:
  `docker exec -e PRECIS_TEST_PG_URL=<dsn> <ctr> bash -lc 'cd /app &&
  /opt/venv/bin/python -m pytest …'` (note `/app` is a **read-only**
  mount there — point `COVERAGE_FILE` / `-o cache_dir` at `/tmp`).
  Reserve host `uv run pytest` for targeted, extra-free subsets.
- **Container-first ops.** `scripts/dev` → dev shell;
  `scripts/db` → psql (LOCAL `precis` / `precis_test` only — the
  dev pgvector container is published at `127.0.0.1:5432`,
  `POSTGRES_USER=postgres`). Compose file lives outside the repo at
  `~/work/infrastructure/compose.yaml`.
- **Peeking at prod.** To inspect the live `precis_prod` DB (read the
  spin-loop / nursery state, count `ref_events`, etc.) hop through a
  cluster node and psql the pgbouncer:
  `ssh -o IdentityAgent=none melchior 'psql -h 100.126.127.107 -p 6432
  -U agent_rw -d precis_prod -c "…"'` (caspar works too). `agent_rw`
  has SELECT; the local `scripts/db` creds do **not** reach prod. The
  `-o IdentityAgent=none` works around the flaky ssh-agent forwarding.
- **Host pytest is subset-only (prefer the container).** See the
  full-suite rule above — the host is torch-free, so only run
  targeted, extra-free subsets there. `scripts/dev` mounts the MAIN
  repo, so to test *worktree* edits on the host set
  `PRECIS_TEST_PG_URL=postgresql://postgres:<pw>@localhost:5432/precis_test`
  (pw from the `postgres-postgres-1` container env; the secret DSN in
  `~/.secrets/pw/PRECIS_TEST_PG_URL` uses `host.docker.internal` —
  rewrite it to `127.0.0.1` on the host). Worker tests that import
  `precis.ingest.citations` need the S2 client —
  `uv run --with semanticscholar pytest …` avoids pulling the whole
  heavy `[paper]` extra (marker/torch).
- **Skills are runtime docs.** Updating a skill file under
  `src/precis/data/skills/` is the agent-facing channel — the
  MCP server reads them at boot and serves them via
  `get(kind='skill', id='…')`.
- **Embeddings populated by the worker, not at ingest.** Per ADR
  0007: ingest stores chunks with `embedding IS NULL`; the
  `embed:bge-m3` worker picks them up. Callers must not call
  `fill_embeddings` from the ingest path.
- **Don't mutate body chunks.** `chunks` is append-only for body
  rows (`ord >= 0`); only `ord < 0` card variants may be DELETEd and
  re-INSERTed by a registered synthesis pass. To "update" a chunk's
  text, DELETE + INSERT so the embedding/summary cascade re-runs —
  an in-place UPDATE leaves stale `chunk_embeddings` / `chunk_summaries`.
- **Outbound HTTP goes through `safe_fetch`.** Any new code that
  fetches an agent-supplied URL — directly or after a redirect —
  must use `safe_get` / `safe_stream` from
  `src/precis/utils/safe_fetch.py`. Raw `httpx.Client(...).get(url)`
  with `follow_redirects=True` is an SSRF.
- there is no CHANGELOG.md file because it is all in git history.
- if another branch left trivial stuff (like a file that needs to run ruff, just do it).
