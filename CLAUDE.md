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
  and **spin loops** (any `(ref_id, source)` emitting >
  `SPIN_LOOP_EVENTS_24H` (200) `ref_events` in 24h). Each finding is
  raised as a `kind='alert'` (one per condition, `alert_source =
  nursery:<category>`, deduped on `meta.fingerprint`; cleared
  conditions auto-resolve) — **not** a `kind='memory'` digest any
  more. See `## Other live affordances` → `alert`, and
  `precis-nursery-help`. (Replacing the digest killed a self-spin: the
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
  `job_coordinator`, `job_ssh_node`, `wake_runner`, `clusterize`.
  (`llm_summarize` is opt-in on top — env `PRECIS_SUMMARIZE_LLM=1` or
  `--only llm_summarize`; enabled on melchior as a deliberate trickle.)
* `precis worker --profile=agent` runs the passes that need the
  hermes OAuth / `~/.claude` state on melchior: the LLM-heavy
  reviewers (`structural`, `deep_review`) plus `job_claude_inproc`
  (planner-coroutine slice — moved off system 2026-06-15 so data-host
  workers stop claiming plan_tick/fix_gripe jobs they can't run and
  false-bubbling `child-failed`) and `quota_check`. It skips the
  embedder load it doesn't need.
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
- **`alert`** (migration `0029`) — machine-detected ops / health
  conditions (spin loops, orphaned todos, stalled recurrings). Raised
  by background passes via `precis.alerts.raise_alert` (upsert on
  `meta.fingerprint`; `resolve_stale_alerts` auto-closes cleared ones),
  read via `AlertHandler` (`get(id='/open')`) + the `/alerts` web tab.
  **Not embedded** — body in `title`/`meta`, no `card_combined` chunk,
  so it never reaches semantic search. Deliberately separate from
  `memory` (ops telemetry ≠ thought); the LLM reviewers stay on
  `memory`. Skill: `precis-alert-help`. Producer is generic — sweeper /
  quota / failed-pass detectors can adopt it next.
- **`agentlog`** (migration `0034`) — run-attribution record, the
  structural twin of `alert` (numeric, machine-produced, **not
  embedded**). One per agentic run that touches the corpus, carrying the
  full assembled prompt (`meta.prompt`) + model/source + a **`touched`
  link to every chunk the run wrote/moved** (a new symmetric, no-inverse
  link relation). Write side `precis.agentlog`
  (`open_log`/`attach_touch`/`touch_from_env`/`finalize_log`/
  `gc_stale_logs`); `plan_tick` opens the log and threads its id to the
  `claude -p` subprocess via `PRECIS_CURRENT_AGENTLOG`, the
  `DraftHandler` reads it and attributes each write. The **sweeper**
  GCs logs past `PRECIS_AGENTLOG_RETENTION_DAYS` (default 30), dropping
  the `touched` links but never the chunks. Read via `AgentLogHandler`
  + the `/agentlogs` web tab (prompt + touched chunks + transcript
  link); touched chunks also surface as a Connections chip on the draft
  reader. Skill: `precis-agentlog-help`.
- **`job` substrate** — `meta.job_type` + `meta.executor`,
  `STATUS:` tag, forensics as `chunk_kind='job_event'` (hidden) /
  `job_summary` (searchable) / `job_result` (structured per-tick
  audit). Executors include `claude_inproc`; `fix_gripe` is the
  reference job_type (clones the repo, runs `claude -p`, pushes a
  review branch).
- **`structure`** — atomistic cell + bond-graph IR (ADR 0043): a
  slug-addressed design (cell on `refs.meta`; atoms/bonds/measures in
  `struct_*`), edited by typed **ops** and read by in-memory **probes**,
  never pixels. Energy rungs relax via the run-cube cache (§23.16) +
  `struct_relax` on the GPU node — a **derived-lane** job parented on the
  structure, *not* a todo (ADR 0044): a relax dispatches with no todo
  required; pass `requested_by=<todo>` on the relax op to make an
  intentful task block on it. **Cursors + measures** (§6.8/§7) live on
  `struct_measures` — `cursor`/`measure`/`unmark`/`remove_measure` ops
  anchored by stable atom **label** (not row id, which an edit orphans),
  persisting across edits and re-evaluated (`view='markers'`). `link`
  relates designs (`derived-from`); `StructureHandler.derive(id,to,ops)`
  branches a new slug (the web Apply). **Web** (`precis_web/routes/
  structure.py`, `/structure`): a 3D viewer (element-coloured clickable
  atoms + authoritative clickable bonds + initial/relaxed overlay + a
  `data-atoms` text→cell cross-highlight), a run-cube panel with a **"Relax"
  button** (`POST /relax`, rung clean/ml/dft + default params → a derived
  `struct_relax` job on the GPU node, no todo — ADR 0044) whose **in-flight
  job is shown as a live pending row** (`_pending_jobs` — the job exists at
  redirect but has no `struct_runs` row until the GPU node finishes) and
  polled via `GET /runs_status` until a run lands (queued→running→reload; a
  failed job shows a red pill), a cursors &
  measures panel + 3D overlay, a lineage row, and a **"Further instructions"
  box** — `POST /instruct` mints a `structure_propose` job (tool-less
  `claude -p`, so it *cannot* mutate: it returns dry-run-validated ops
  JSON), the box polls `/proposal`, and `POST /apply` derives a new design.
  Skill: `precis-structure-help`.
- **`citation`** — verifier-workflow kind:
  `put(kind='citation', text=<claim>, source_handle, source_quote,
  verifier_confidence, link='paper:<slug>', rel='cites')`. The tex
  workspace skeleton's `\citequote{key}{verbatim}` macro persists the
  same `source_quote`. Skill: `precis-citation-help`.
- **`cfp`** — call-for-proposal / requirements document (proposal
  writing). A **spec-role** sibling of `paper`: ingested by the
  *identical* Marker→chunks pipeline (`precis add --as cfp`, or
  `inbox/cfp/` — one more inbox kind dir alongside papers/books/
  presentations), so it gets embed / keywords / TOC / search for free,
  and read in the *same* two-pane reader at `/cfp/<slug>`.
  `CfpHandler(PaperHandler)` (`handlers/cfp.py`) differs only by
  declaration — a new `KindSpec.corpus_role` field (`evidence` for
  paper/patent, `spec` for cfp): a `spec` doc is **never cited as
  evidence** (the citation handler already resolves `source_handle`
  only against `kind='paper'`, so it's safe by construction), stays out
  of `search(kind='paper')`, drops the bibtex/citation views, and has no
  `put` (acquired by ingest only). The paper handler's ~dozen structural
  `kind="paper"` literals were routed through `self.spec.kind` so the
  subclass reuses get/search verbatim; the `ref_id`-scoped reader
  endpoints (`/papers/<id>/search|toc|chunk|pdf`) accept the doc family
  (`_DOC_FAMILY`). A proposal **project** (`LLM:*` todo) links to its
  cfp via the `has-requirement`/`requirement-of` relation (migration
  `0039`); the planner prompt's `_m_requirements` module injects the
  call's required sections into the writing tick. Word limits live on
  draft section headings as `meta.word_target={min,max}`, checked via
  `get(kind='draft', view='wordcount')` (per-section over/under/ok +
  a web badge). Skill: `precis-proposal-help`.
- **Keystone kinds (`cad` / `pcb` / `structure`)** — the "own a legible
  IR, rent the heavy kernel only at export" family (ADR 0041 / 0042 /
  0043). Each hands the LLM a *graph* to traverse, never pixels: `cad`
  probes a parametric solid analytically (point/ray/section/clearance);
  `structure` is an atomistic cell+bond graph relaxed on a fidelity
  ladder; `pcb` is a netlist + placement graph — author components /
  nets / connections in one batch `put`, then read it as a graph
  (`slug#U1` → pins → nets → neighbours) via probe views. The heavy
  tools are gated to the **export** step only: `handlers/pcb.py`
  dispatches `_EXPORT_VIEWS` (`bom`/`cpl`/`netlist`/`dsn`/`mechanical`)
  to the **pure** exporters in `src/precis/pcb/export.py` (IR → text,
  no binaries — BOM/CPL are JLCPCB-native, mechanical is the 0041
  outline+holes bridge) and the `route` view to `src/precis/pcb/route.py`,
  a headless-Freerouting `.dsn`→`.ses` round-trip (`place_route_round_trip`,
  escalating passes) gated on `PRECIS_FREEROUTING_JAR` (+ java) — it
  **skips, never raises** when the backend is absent. Coordinate frame:
  mm, origin at the outline corner, +X right / +Y up, rotation CW —
  **footgun:** JLCPCB CPL wants CCW, so `jlc_rotation(r) = (360-r) % 360`.
  Parts come from `kind='part'` (JLCPCB-assemblable), datasheets from
  `kind='datasheet'`. Artifacts land under `PRECIS_CORPUS_DIR/pcb/<slug>/`.
  The `[pcb]` extra (`easyeda2kicad`) is staged for Flow-B footprint
  conversion — the fetch itself is still a `FeatureUnavailable` stub
  (Slice 2 deferred item), so exports fall back to placeholder pin spreads.
  Skills: `precis-pcb-help` (+ part-select / net-class / measures and the
  i2c/spi/decoupling/datasheet playbooks, skill-search-only). Cluster
  deploy is Tier-1 (JRE + jar on the gateway; kicad-cli gerbers deferred).
- **Broad + deep paper search.** Tier 1: `search(kind='paper', q=,
  queries=[…rephrasings], answers=[…HyDE passages], per_paper=N)` —
  app-level RRF fusion over N lexical/semantic legs
  (`store.search_blocks_multi`; caps ≤8/≤8 enforced at the handler, one
  batched embed call, degrade-to-lexical). Tier 2 (thin slice):
  `search(kind='paper', q=…, good=True)` mints a **`good_search`
  coordinator campaign** (async handle; poll `get(kind='job', id=…)`) —
  Tier-1 fuse → cheap `claude_inproc` triage children parented on the
  coordinator (ADR 0044 extension) → heartbeat gather → curated result
  in `meta.result`. Design + phase-2 roadmap:
  `docs/design/good-search-coordinator.md`; skill:
  `precis-search-help`.
- **`chunks.numerics TEXT[]`** — GIN-indexed lexical filter
  (`WHERE numerics @> ARRAY['1.523 eV']`); available via direct SQL,
  not yet wired into the search verbs.
- **`precis web`** — browser UI (Tasks / Papers / Console /
  Conversations / Status). Papers carry DOI/arXiv verify links and
  presence filters (`has_pdf` / `has_chunks`); PDFs serve from a
  multi-root `PRECIS_CORPUS_DIR` (ADR 0029). The **paper detail page**
  (`routes/papers.py`, slug- or id-addressed; numeric→slug 301) is a
  two-pane reader: a collapsible sidebar (Navigate = semantic/keyword/TOC
  over the paper's chunks via `/papers/<id>/search` + `/toc`; Jump by
  text/page/ord via `/chunk/<ord>`; Meta = all metadata + edit/triage)
  beside the vendored Mozilla **pdf.js** viewer (`static/pdfjs/`, driven
  same-origin to jump pages + highlight chunk text via its find API; no
  per-chunk bbox yet, so highlight is text-layer best-effort). Also a
  per-todo compiled-PDF viewer; "ask a follow-up" on any thought spawns a `conv` thread
  linked `derived-from` the source. The **draft reader**
  (`routes/drafts.py`, the per-block Tier-A grid) is a **true virtual
  scroller** — built for 10k-block drafts: only the on-screen window of
  rows lives in the DOM. The page embeds a compact **skeleton** (one tiny
  record per block — `_skeleton`) + the server-rendered first
  `INITIAL_WINDOW` (30) rows; sized `#dr-top`/`#dr-bot` spacers stand in
  for off-screen blocks. Client JS (`draftDoc`) reconciles `#dr-win` to the
  blocks intersecting the viewport on scroll — **idempotent, no
  IntersectionObserver, no feedback loop** (the earlier node-per-block +
  observer approach styled/Alpine-walked all ~9,700 → the minute-lag, and
  load↔unload thrash flickered ~5×/s) — fetching them in one
  `/drafts/<id>/rows?handles=…` batch and dropping rows that scroll away.
  Collapse recomputes the visible set + spacers; find / deep-links scroll
  the target into the window first. The **view slider** (body / summary /
  keywords) toggles each row's column via Alpine `x-show`, so a switch
  changes every block's height: the skeleton carries a body estimate
  (`est`, length-derived) *and* a one-line estimate (`estS`) for the
  summary/keywords views, and `setView` drops the per-view measured
  `heights` + re-measures the on-screen rows once Alpine has re-toggled
  (else the spacers stay sized for the old view — the bug where
  summary/keywords stopped scrolling, bottom blank). `_doc_state` memoises reading-order +
  version + abbrevs per `(ref,version)`; `_build_rows(want_idx)` scopes
  per-handle queries to wanted blocks + neighbours; the inline abbrev scan
  is skipped above 300k chars. `GET /drafts/<id>/skeleton` (JSON) feeds the
  live poll. **Delete**: a name-confirmed button → `POST /drafts/<id>/delete`
  → `store.soft_delete_draft` (ref `deleted_at` + chunks retired in one txn;
  recoverable). Status page has a Background
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
- **Host pytest needs the DB URL + `paper` deps.** `scripts/dev`
  mounts the MAIN repo, so to test *worktree* edits run host pytest
  with `PRECIS_TEST_PG_URL=postgresql://postgres:<pw>@localhost:5432/precis_test`
  (pw from the `postgres-postgres-1` container env). Worker tests that
  import `precis.ingest.citations` need the S2 client —
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
