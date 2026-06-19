# Claude Code — project brief

> **First**: read `AGENTS.md`. It is the canonical project guide
> (humans + agents). Conventions, workflow, definition-of-done,
> ingest guarantees — all there. This file is a current-state map of
> the discovery / task / worker / review subsystems a Claude Code
> session needs before touching them. It is **present-tense** — for
> the dated story of how each piece landed, read `## Unreleased` (and
> the release sections) in `CHANGELOG.md`. Keep this file true:
> update it in the same commit that changes what it describes.

## Session workflow (worktree → ship)

Best practice for a unit of work:

1. **Start in a worktree.** Launch with `claude -w <name>` (alias of
   `--worktree`). Claude Code creates an isolated worktree at
   `.claude/worktrees/<name>/` on a new `worktree-<name>` branch, so
   the work is isolated from `main` and from sibling sessions.
2. **Do the work** in that worktree — implement, test, iterate.
3. **End with `/endsession`.** The `/endsession` command
   (`.claude/commands/endsession.md`) wraps up: commits any WIP,
   `git town sync` (rebase onto the latest `main`), runs the
   container integration gate (`ruff` + `mypy` + `pytest`), and — only
   if green — `git town ship` (squash-merges the branch back to `main`
   and removes it). It **aborts and reports** on any gate failure; fix
   and re-run. Landing on `main` is the end goal of a feature branch.

This relies on the repo's git-town config (`ship-strategy =
squash-merge`, feature branches parented on `main`). git-town must be
installed on the host (`brew install git-town`). NB the merge target is
`main`, not `master` — the repo has no `master`.

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
* **Jobs are children of todos.** `JobHandler.put` requires a
  `parent_id` pointing at a `kind='todo'`. The `dispatch` worker
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
  post-processing).
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

* `nursery` — SQL-only, every minute on the system worker,
  idempotent on fingerprint. Flags orphans, stale claims, long waits,
  stuck doable, stalled recurrings, and **spin loops** (any
  `(ref_id, source)` emitting > `SPIN_LOOP_EVENTS_24H` (200)
  `ref_events` in 24h).
* `structural` — opus, 6h dedup, agent profile. Drift, sibling
  contradictions, depth/fanout warnings.
* `deep_review` — opus, weekly dedup, agent profile. Allen-style
  archive / prune / rebalance / long-wait review.

## Workers

**Two `precis worker` profiles, four LaunchDaemons total.**

* `precis worker --profile=system` runs on every cluster node and
  drives every chunk-level + SQL ref-level pass: `embed`, `summarize`,
  `llm_summarize`, `chunk_keywords`, `chase`, `fetch`, `gp_fetch`,
  `tag_embeddings`, `auto_check`, `schedule`, `nursery`, `dispatch`,
  `sweeper`, `watch_poll`, `job_claude_inproc`, `job_coordinator`,
  `quota_check`, `wake_runner`, `clusterize`.
* `precis worker --profile=agent` runs the LLM-heavy reviewers
  (`structural`, `deep_review`) on melchior as hermes (OAuth for
  `claude -p`); it skips the embedder load it doesn't need.
* `dream_agent` keeps its own 15-min cadence via `dream-pass.sh`,
  and `cron-tick` is the fourth daemon. Each heavy pass dedups on its
  tier-tagged memory and load-gates on `PRECIS_LOAD_CEILING` (default
  `os.cpu_count() * 1.5`).

**Notable passes:**

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
* `fetch` / `chase` backoff — the OA fetcher's retry window arms on
  any `fetcher:%` event (not just `unpaywall`, which is disabled in
  prod); finding-chase skips a `waiting` finding newer than
  `WAITING_BACKOFF_MINUTES` (60). Both fixes killed `ref_events`
  spin-loop floods.

**Unified `claude -p` agentic dispatch — `utils/claude_agent.py`.**
Peer to `utils/claude_p.py` (one-shot JSON judge). Carries the
agentic flag set (`--mcp-config` / `--strict-mcp-config`,
`--append-system-prompt`, `--max-turns`, `--permission-mode`,
optional `--bare`, `--disallowed-tools`) + cost cap + wall-clock
timeout + structured `log_event` to `ref_events`. The reviewers,
`dream_agent`, and the web "ask a follow-up" path all share this
surface. Stub-binary tests via `PRECIS_CLAUDE_BIN`.

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

## Other live affordances

- **Cluster maps (`/clusters`)** — a spatial browse over the corpus.
  The `clusterize` worker (system profile, `utils/cluster_map.py`,
  numpy-only) trains a hierarchical Self-Organizing-Map over chunk
  embeddings — a *grid* where adjacent tiles are similar — and labels
  each tile with a sibling-scoped c-TF-IDF word cloud. Daily rebuilds
  **warm-start** from the prior run so a tile's address (`4.7.1`) stays
  put as the corpus drifts. Two scopes (`paper` deep tree / `memory`
  shallow grid). Storage: `0027_clusterize.sql` (`cluster_runs` /
  `cluster_cells` / `cluster_assignments`). Web:
  `precis_web/routes/clusters.py` + the Clusters nav tab.
- **`gripe`** — first-class bug tracker (`get`/`search`/`tag`/`link`/
  `delete`); body + append-only comment timeline live as chunks
  (`gripe_body`, `gripe_comment`), so embed + `chunk_keywords` index
  them automatically.
- **`job` substrate** — `meta.job_type` + `meta.executor`,
  `STATUS:` tag, forensics as `chunk_kind='job_event'` (hidden) /
  `job_summary` (searchable) / `job_result` (structured per-tick
  audit). Executors include `claude_inproc`; `fix_gripe` is the
  reference job_type (clones the repo, runs `claude -p`, pushes a
  review branch).
- **`citation`** — verifier-workflow kind:
  `put(kind='citation', text=<claim>, source_handle, source_quote,
  verifier_confidence, link='paper:<slug>', rel='cites')`. The tex
  workspace skeleton's `\citequote{key}{verbatim}` macro persists the
  same `source_quote`. Skill: `precis-citation-help`.
- **`chunks.numerics TEXT[]`** — GIN-indexed lexical filter
  (`WHERE numerics @> ARRAY['1.523 eV']`); available via direct SQL,
  not yet wired into the search verbs.
- **`precis web`** — browser UI (Tasks / Papers / Console /
  Conversations / Status). Papers carry DOI/arXiv verify links and
  presence filters (`has_pdf` / `has_chunks`); PDFs serve from a
  multi-root `PRECIS_CORPUS_DIR` (ADR 0029); per-todo compiled-PDF
  viewer; "ask a follow-up" on any thought spawns a `conv` thread
  linked `derived-from` the source. Status page has a Background
  Health panel (active spin loops + failed passes, 24h). `precis_web`
  is a sibling package over the handler layer (ADR 0026).
- **SSRF guard** — `src/precis/utils/safe_fetch.py`, used by
  `handlers/web.py` and `workers/fetch_oa.py`; DNS-resolves before
  fetch and revalidates every redirect against the private /
  loopback / link-local / cloud-metadata blocklist.
- **Ingest hygiene** — pysbd sentence splitter in the chunker
  fallback chain; dehyphenation in `marker._clean_text`; HNSW index
  on `chunk_embeddings.vector`.

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
| Workflow + lint/test commands    | `AGENTS.md` |
| Dated history of every change    | `CHANGELOG.md` (`## Unreleased`) |
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
| SSRF guard                       | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` file
  under `src/precis/migrations/`. If you find a bug in a sealed
  file, ship a new forward migration that corrects it. Rationale
  lives in `docs/decisions/0005-greenfield-migrations.md`.
- **`uv` for everything.** Bare `pip` / `pytest` / `mypy` are
  not reproducible. Use `scripts/dev pytest …` inside the
  container, or `uv run …` on the host.
- **Container-first ops.** `scripts/dev` → dev shell;
  `scripts/db` → psql. Compose file lives outside the repo at
  `~/work/infrastructure/compose.yaml`.
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
