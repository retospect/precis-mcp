# Claude Code — project brief

> **First**: read `AGENTS.md`. It is the canonical project guide
> (humans + agents). Conventions, workflow, definition-of-done,
> ingest guarantees — all there. This file is a thin pointer with
> recent-landing notes Claude Code sessions need before touching the
> discovery / search / chase paths. Update it the same commit you
> change the things it describes.

## What just landed (2026-06-19 — projects = workspace, first-class)

A **project** is a strategic-root todo that owns a `meta.workspace`
(no new kind). Three additions promote the existing workspace concept:

* **Owner-path `project:<slug>` tagging** — `TodoHandler.put` now
  derives `project:<slug>` from `meta.workspace.path` and stamps it
  even when the `PRECIS_WORKSPACE` env is unset (i.e. operator/CLI
  writes, not just planner ticks). Slug logic lives in
  `utils/workspace.project_tag_for_path` / `Workspace.project_tag`.
  Forward-only — stamps the ref being created, not its subtree.
* **Project brief** — new first-class `Workspace.brief`
  (`meta.workspace.brief`); cascades down the subtree and is injected
  as a `## Project context` block in the planner prompt's *variable*
  layer (`workers/planner_prompt._render_project_brief`). NOT in the
  cached system layer (per-project ⇒ no shared cache prefix).
* **`view='projects'`** — `_todo_views.render_projects`: dashboard of
  workspace-owning roots. View dispatch in `handlers/todo.py` is now a
  `TodoView` StrEnum + `_TREE_SEARCH_VIEWS` table with an import-time
  totality assert (no more frozenset/if-chain drift).

Skill: `precis-tasks-help` gained a "Projects" section + the
`view='projects'` line. No migration (meta/tag/view changes only).

## What just landed (2026-06-14 — Slices 3/4/5 + worker consolidation)

The todo tree is now a five-slice system unifying intent, scheduling,
execution, and review:

* **Slice 1** (shipped earlier) — `parent_id` column on refs, the
  todo hierarchy with strategic/tactical/subtask gradient, walk-on-read
  ancestry, the 1/N rotation across strategics by 7d picks.
* **Slice 1b** — `meta.auto_check` evaluator pattern. Evaluators:
  `paper_ingested` / `discord_reply_received` / `time_past` /
  `tag_present` / `child_job_succeeded` (new — see Slice 5).
* **Slice 4** — `level:recurring` umbrella ("Watches"),
  `meta.schedule` (cron or `every:` shorthand), per-minute spawner.
  PRIO is an int column on refs, 1..10. `PRIO:*` tag stays as
  back-compat alias.
* **Slice 3** — three review tiers writing memory digests:
  * `nursery` (SQL-only, every minute via system worker, idempotent
    on fingerprint) — orphans, stale claims, long waits, stuck
    doable, stalled recurrings.
  * `structural` (opus, 6h dedup, agent profile) — drift, sibling
    contradictions, depth/fanout warnings.
  * `deep_review` (opus, weekly dedup, agent profile) — Allen-style
    archive / prune / rebalance / long-wait review.
  Reviewers are factored into `workers/review.py` (`Reviewer`
  dataclass + `run_review_pass` driver); structural / deep are thin
  shims. Adding a new reviewer is a `Reviewer(...)` instance.
* **Slice 5** — jobs are now **children of todos**. `JobHandler.put`
  requires `parent_id` pointing at a `kind='todo'`. The `dispatch`
  worker (`workers/dispatch.py`) walks open todos with
  `meta.executor`, mints `kind='job'` under each with
  `FOR UPDATE SKIP LOCKED`, auto-injects
  `meta.auto_check={'type':'child_job_succeeded'}`. On job failure,
  the parent gets a `child-failed:<job_id>` open tag (the
  failure-bubble — `handlers/_job_bubble.py`); the doable view
  excludes parents with the tag so they stop re-entering the
  rotation until the owner decides retry / switch / give up.
  `view='tree'` walks `kind IN ('todo', 'job')` so child jobs
  render with a `⚙` marker. New `view='attention'` unions
  `asking-reto` leaves + `child-failed` parents for asa-bot's
  preamble.

**Worker consolidation** — the seven per-pass LaunchDaemons are
down to four (system worker, agent worker, dream, cron-tick).
`precis worker --profile=system` runs every chunk-level + SQL
ref-level pass (embed, summarize, chunk_keywords, chase, fetch,
tag_embeddings, auto_check, schedule, nursery, dispatch,
job_claude_inproc) on every cluster node. `precis worker
--profile=agent` runs structural + deep_review on melchior as
hermes (OAuth for claude `-p`). Each heavy pass internally
dedups on its tier-tagged memory + load-gates on
`PRECIS_LOAD_CEILING` (default `os.cpu_count() * 1.5`).
`dream_agent` keeps its own 15-min cron via the unchanged
`dream-pass.sh`.

**Unified `claude -p` agentic dispatch — `utils/claude_agent.py`.**
Peer to `utils/claude_p.py` (one-shot JSON judge). Carries the
agentic flag set (`--mcp-config` / `--strict-mcp-config`,
`--append-system-prompt`, `--max-turns`, `--permission-mode`,
optional `--bare`, `--disallowed-tools`) + cost cap + wall-clock
timeout + structured `log_event` to `ref_events`. All three
reviewers (structural / deep_review / dream_agent) share this
dispatch surface. Stub-binary tests via `PRECIS_CLAUDE_BIN`.

**LLM-facing skill index** lives under
`src/precis/data/skills/precis-*-help.md`. Start at
`precis-toolpath-help` (canonical call sequences per scenario).
Cross-refs: `precis-tasks-help`, `precis-decomposition-help`,
`precis-auto-tasks-help`, `precis-recurring-help`,
`precis-dispatch-help`, `precis-job-help`,
`precis-fix-gripe-help`, `precis-nursery-help`. `precis-overview`
has the master kinds table + skill index.

## What just landed (2026-06-05, follow-up)

**Gripe → first-class bug tracker + `job` substrate for offline
LLM work.** The minimal write-only gripe box is gone; gripe is
now a normal MCP kind with `get` / `search` / `tag` / `link` /
`delete`. The body and the append-only comment timeline live as
chunks (`gripe_body`, new `gripe_comment`) so they're searchable
through the standard chunk surface — embed + chunk_keywords
workers pick them up automatically.

New `kind='job'` is the substrate for offline runs: each job
carries `meta.job_type` and `meta.executor`, status lives as a
`STATUS:` tag, and forensics / final summary live as
`chunk_kind='job_event'` (hidden from default search) and
`chunk_kind='job_summary'` (searchable). v1 ships one job_type
(`fix_gripe`) and one executor (`claude_inproc`).

`fix_gripe` is the first job_type and the proof of the substrate:
`put(kind='job', job_type='fix_gripe', link='gripe:42',
rel='fixes')` clones the repo, runs `claude -p
--dangerously-skip-permissions` inside the precis container, and
pushes a `gripe_42` branch to origin for review. Deployment-side
this adds three bind-mounts to the precis service (`~/.claude`,
`$PRECIS_FIX_REPO_DIR`, `$PRECIS_FIX_WORK_DIR`) and bakes the
`claude` binary into the precis image.

Skills: `precis-gripe-help` (rewritten — the project's bug
tracker), `precis-job-help` (new — the substrate), and
`precis-fix-gripe-help` (new — the end-to-end recipe). The old
`precis gripes` CLI is deprecated.

Forward migration: `0005_gripe_first_class_and_jobs.sql`.

## What just landed (2026-06-05)

**F20: per-chunk KeyBERT supersedes the persistent discovery layer.**
The `ref_segments` / `ref_segment_sentences` tables described in ADR
`0018-persistent-discovery-layer.md` were dropped. The discovery
surface is now:

- `chunks.keywords TEXT[]` (canonical lower-case forms, GIN-indexed)
  + `chunks.keywords_meta JSONB` (versioned envelope with short/long
  pairs and KeyBERT scores).
- Worker: `precis worker --only chunk_keywords` (or run as part of
  the default round-robin). Source:
  `src/precis/workers/chunk_keywords.py`. Claim shape is
  `keywords IS NULL OR keywords_meta->>'version' != current`, so
  bumping `KEYWORDS_VERSION` re-claims every existing chunk.
- `view='toc'` (papers): dynamic DP clustering over the keyword
  arrays at request time — `src/precis/utils/toc_db.py`
  `render_from_store`. No precomputed segment rows; reads
  `chunks.keywords` directly.
- `view='toc'` (skills): still uses the per-request DP+KeyBERT
  renderer in `src/precis/utils/toc.py`; output is memoised per
  `(slug, scope)` on the handler instance since skill files are
  static for the life of the process.
- Search reranking against `ref_segment_sentences` was removed with
  F20. Result rows no longer carry indented `excerpt @ ~N` sub-lines.

**Other live affordances (still current as of 2026-06-05):**

- `citation` kind — verifier-workflow ref kind.
  `put(kind='citation', text=<claim>, source_handle, source_quote,
  verifier_confidence, link='paper:<slug>', rel='cites')`. Skill:
  `precis-citation-help`.
- `chunks.numerics TEXT[]` GIN-indexed lexical filter —
  `WHERE numerics @> ARRAY['1.523 eV']` for exact quantitative
  lookups. Currently unwired into the search verbs; available via
  direct SQL only.
- pysbd-backed sentence splitter in the chunker fallback chain
  (`et al.`, `Fig.`, `i.e.`, `e.g.`, `vs.`-aware).
- Dehyphenation in `marker._clean_text` (joins `-\n` when both
  sides are lowercase ASCII).
- HNSW index on `chunk_embeddings.vector` — semantic search no
  longer seq-scans.
- SSRF guard on outbound HTTP (`src/precis/utils/safe_fetch.py`)
  used by `handlers/web.py` and `workers/fetch_oa.py`. DNS-resolves
  the host before fetch and revalidates every redirect against the
  private/loopback/link-local/cloud-metadata blocklist.

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| Workflow + lint/test commands    | `AGENTS.md` |
| Full schema (prose)              | `docs/design/storage-v2.md` |
| Full schema (visual)             | `docs/design/schema-v2.svg` (PUML in same dir) |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| F20 (per-chunk keybert)          | `src/precis/workers/chunk_keywords.py` header + `src/precis/utils/toc_db.py` header |
| ADR 0018                         | Superseded by F20. Keep for history, do not implement against. |
| Agent-runtime surface (skills)   | `src/precis/data/skills/precis-*.md` |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/{embed,summarize,chunk_keywords,chase,fetch_oa,runner}.py` |
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
- **Outbound HTTP goes through `safe_fetch`.** Any new code that
  fetches an agent-supplied URL — directly or after a redirect —
  must use `safe_get` / `safe_stream` from
  `src/precis/utils/safe_fetch.py`. Raw `httpx.Client(...).get(url)`
  with `follow_redirects=True` is an SSRF.

## Recent unreleased changes

See the top of `CHANGELOG.md` under `## Unreleased` for the full
list. F20 (per-chunk keybert) is the headline since 2026-06-05;
everything else folds into it.
