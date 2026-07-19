# Claude Code — project brief

> **Two surfaces — don't confuse them.** This repo **is** the precis MCP
> server. The `precis` MCP tools and `get(kind='skill', id=…)` skills in
> your session are the **product's** runtime surface, for cluster agents
> operating precis — **not** dev aids for this repo. To *develop the repo*,
> navigate: **`docs/codebase.md`** (orientation) → `state-map.md` (subsystem
> status) → `glossary.md` (terms) → `docs/decisions/` (ADRs). Don't reach
> for `get(kind='skill')` to understand the code.

> **Lean router**, loaded every session: ship workflow, conventions that
> bite, pointers to deeper detail. Read **`docs/codebase.md`** first for the
> shape of the system; subsystem present-state lives in
> **`docs/architecture/state-map.md`** (read before touching one).
> `AGENTS.md` = conventions/workflow/DoD. No CHANGELOG — history is `git log`.
> **Keep docs true:** a subsystem change updates `state-map.md` (and, if the
> *shape* changed, `docs/codebase.md`) in the same commit; this file changes
> only when the workflow, a convention, or the subsystem *set* changes.
> Prose house-style: `docs/conventions/llm-facing-prose.md`.

## Session workflow (worktree → ship)

1. **Start in a worktree** — `claude -w <name>` creates
   `.claude/worktrees/<name>/` on branch `worktree-<name>`, isolated from
   `main` and siblings.
2. **Do the work** — implement, test, iterate.
3. **`/land`** (ship) **or `/go`** (ship + deploy). Both run
   `scripts/ship`: commit WIP → sync (`fetch` + `merge` main) → container
   gate (auto-fix ruff, then authoritative `ruff` + `mypy` + `pytest`) →
   squash-merge to `main` if green → reset branch to shipped `main` →
   local-main fast-forward. `/go` also runs `scripts/deploy`
   (`ansible-playbook redeploy-precis.yml`). Both abort+report on gate
   failure; scripts are idempotent, so fix and re-run.

`scripts/ship` squashes onto `main` via `commit-tree` + `--force-with-lease`
CAS push, then resets the branch to shipped `main`. Merge target is `main` —
repo has no `master`.

**In-flight worktrees.** Many sibling sessions run at once. Before starting,
**scan for overlap**: `scripts/inflight` prints a live per-worktree table
(session, dirty, ahead/behind, PURPOSE, last commit); a `SessionStart` hook
injects it. **Once your task is clear, write one line to `.claude/purpose`**
(gitignored, self-cleaning) — git derives everything except intent. The
footer lists removable candidates (merged+clean+no session) but only
reports — never auto-remove.

## Subsystem map (detail on demand)

Read `docs/architecture/state-map.md` first; `precis-*-help` skills are the
per-kind reference.

- **Todo tree (five slices)** — `kind='todo'` hierarchy: strategic/tactical
  gradient, `auto_check` leaves, `level:recurring` watches, jobs (intent vs
  compute lane, ADR 0044), planner coroutines, views, projects.
  → skills `precis-tasks-help`, `precis-dispatch-help`.
- **Review tiers** — `nursery` (SQL, per-minute, only `critical` alerts:
  worker-restart / dead-worker / dispatch-stall), `structural` +
  `deep_review` (opus). → skill `precis-nursery-help`.
- **Workers** — two profiles (`system` every node, `agent` on melchior),
  four LaunchDaemons; passes (`cast_audio`, `card_forge`, `sweeper`,
  `corpus_reconcile`, `paper_reconcile`, `fetch`/`chase` backoff);
  `claude_agent` dispatch + switchable LLM router (ADR 0046).
- **Discovery layer (F20)** — per-chunk KeyBERT (`chunks.keywords`),
  `view='toc'`; ADR 0018 superseded.
  → `docs/conventions/discovery-layer-policy.md`.
- **Chunk-tag classifier (ADR 0047)** — cascade regex → `role3` local →
  optional escalate; `ROLE3:own` = citation-grounding filter, default-OFF.
  → `docs/design/chunk-classifier-cascade.md`.
- **Live affordances** — `folder`, `plan`, `figure`, `mermaid`, `gripe`,
  `anki`, `concept`, `quest`, `llm`, `alert`, `agentlog`, `job`/sandbox,
  `structure`, `citation`, `cfp`, `email` (live IMAP browse, read-only;
  `docs/design/email-kind.md`), term registry, `cad`/`pcb`, broad+deep
  search, `precis web`, SSRF guard, ingest hygiene. → matching `precis-*-help`.
- **Skill index** — start at `precis-toolpath-help` (call sequences) +
  `precis-overview` (master kinds table).

## Where to find context

| Task                             | Read |
|----------------------------------|------|
| **Orientation — read first**     | **`docs/codebase.md`** (shape, lifecycle, seams) |
| Subsystem detail (present-state) | `docs/architecture/state-map.md` |
| Coined / overloaded terms → files| `docs/architecture/glossary.md` |
| To-do list / what's planned next | `OPEN-ITEMS.md` — **open work only**; when a ship completes an item, *delete* its entry in that same commit (never leave a "done ✅" note — `git log` is the record, like memory's landed-work rule) |
| Conventions / workflow / DoD     | `AGENTS.md` |
| Mission / pitch narrative        | `docs/mission.md` (positioning, not architecture) |
| Master kinds table + recipes     | skills `precis-overview`, `precis-toolpath-help` |
| Dated history                    | `git log` (no CHANGELOG) |
| Replicate this repo's setup elsewhere | `docs/how-to-setup-like-this.md` (portable scaffolding brief) |
| Full schema (prose / visual)     | `docs/design/storage-v2.md` (F20-amended); `schema-v2.svg` |
| Worker queue pattern             | `docs/decisions/0007-derived-queue-no-block-jobs.md`, `0017` |
| ADR index + supersession graph   | `docs/decisions/README.md` |
| Ingest pipeline                  | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code                      | `src/precis/workers/` |
| Web UI                           | `src/precis_web/` |
| Discord bridge (asa)             | `src/asa_bot/` — `[asa]` extra; stdio to `precis serve` |
| SSRF guard                       | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` under
  `src/precis/migrations/` — ship a new forward migration to fix bugs
  (ADR 0005). A fresh DB loads the `migrations/baseline/schema.sql`
  snapshot then applies only the tail. Regenerate the snapshot with
  `scripts/bump` / `precis db dump-schema` — never hand-edit it
  (release-time only; it's checked against the files). Dual-track (ADR 0031).
- **`uv` for everything.** Bare `pip`/`pytest`/`mypy` aren't reproducible.
- **Run tests via `scripts/test`.** It runs pytest in the dev container
  against YOUR worktree (bind-mount) with the RAM test DB wired and terse
  output — the canonical iteration loop. Don't hand-roll `uv run pytest`
  (torch-free host → spurious `ModuleNotFoundError` for `marker`,
  `sentence_transformers`, … — not real bugs) or `scripts/dev pytest` (mounts
  MAIN, not your worktree). The dev image bakes all extras, so no
  `--with`/`--extra` needed.

      scripts/test                         # full suite (-n6)
      scripts/test tests/test_x.py -k …    # subset; args pass through to pytest
      scripts/test --impacted              # ONLY tests your change affects (testmon)

  `--impacted` is the tightest inner loop: `pytest-testmon` maps test↔code and
  runs just the tests a working-tree change touches (first run builds the map;
  later runs are sub-second when nothing relevant changed). `scripts/ship` (via
  `/land`, `/go`) runs the authoritative full pre-merge gate — everything
  else is the fast loop before it.
- **Never `cd` into your own worktree.** The Bash shell already runs in the
  worktree root and the harness re-anchors cwd there after *every* call, so a
  `cd <worktree> && …` prefix is pure redundancy — wasted tokens on each call
  and the exact "`cd` in a compound command can trigger a permission prompt"
  footgun. Run commands bare; use an **absolute path** to reach another dir
  (`--git-dir=…`, `ls /Users/reto/work/cluster`), not a `cd`. (Log audit: ~60%
  of Bash calls carried a redundant `cd` — the top single waste in the fleet.)
- **Container-first ops.** `scripts/dev` → dev shell; `scripts/db` → psql
  (LOCAL `precis` / `precis_test` only; dev pgvector at `127.0.0.1:5432`,
  `POSTGRES_USER=postgres`). Compose file: `~/work/infrastructure/compose.yaml`.
- **Peeking at prod.** `scripts/prod-psql "SELECT …"` — hops through a
  cluster node (caspar/melchior) to the live `precis_prod` behind pgbouncer.
  `agent_rw` is WRITE-capable (prefer read-only); local `scripts/db` doesn't
  reach prod. `PRECIS_PROD_SSH_HOST=melchior` / `PRECIS_PROD_PSQL_OPTS="-At"`
  override host / add psql flags.
- **The session `precis` MCP writes to PROD — dogfood READ-ONLY.** The `precis`
  MCP loaded in your dev session is the local 5th worker: its DB-backed kinds
  (todo, gripe, quest, memory, paper, …) target `precis_prod` on caspar as
  `agent_rw` (**write-capable** — verify with `get(kind='skill',
  id='precis-status')`). The "Sandbox PRECIS_ROOT" banner scopes only the
  file-kinds (markdown/plaintext/tex). So dogfood test-and-fix via the session
  MCP with **read verbs only** (`search`/`get`/`more`); `put`/`edit`/`delete`/
  `tag` mutate production. For write-path testing, drive a **dev-DB** precis
  (`scripts/dev`, local test DB) — never the session MCP.
- **Compress noisy output with `rtk`** (a token-killer CLI proxy;
  `brew install rtk` — a prereq, like `uv`/`docker`). Prefix a verbose command
  to filter it to signal: `rtk git …`, `rtk psql …`, `rtk grep/rg/find …`, or
  `rtk err -- <cmd>` / `rtk summary -- <cmd>` for anything else. Safe
  passthrough (an unfiltered command runs unchanged) and it tees the full log
  to disk — but **the shown output is a filtered digest, not raw**: treat it as
  such and re-run raw if a detail is missing. We run it **manually, no hook**
  (an explicit `rtk` in the command line is the signal that a filter is in
  play). Project filters live in `.rtk/filters.toml` (committed). Don't wrap
  `scripts/test` (already terse) or interactive commands (a `psql` shell).
- **Semantic code search** (repo-dev, not the product). The `claude-context`
  MCP (`.mcp.json`) over local Milvus (`docker/code-search/compose.yaml`, up'd
  by a SessionStart hook) indexes the code. One **shared MAIN index** serves
  every worktree: call `search_code` with the **main** repo path, not the
  worktree's — hits are repo-relative and map onto your tree. Seed once with
  `index_codebase(path=<main-root>)`; freshness is lazy (Merkle re-sync).
- **Skills are runtime docs.** Editing `src/precis/data/skills/` is the
  agent-facing channel — the MCP server serves them via `get(kind='skill')`.
- **Embeddings populated by the worker, not ingest** (ADR 0007): ingest
  stores chunks `embedding IS NULL`; `embed:bge-m3` fills them. Don't call
  `fill_embeddings` from the ingest path.
- **Don't mutate body chunks.** `chunks` is append-only for body rows
  (`ord >= 0`); only `ord < 0` card variants may be DELETE/re-INSERTed by a
  registered synthesis pass. To "update" text, DELETE + INSERT so the
  embedding/summary cascade re-runs — in-place UPDATE leaves stale
  `chunk_embeddings` / `chunk_summaries`.
- **Outbound HTTP goes through `safe_fetch`.** Any code fetching an
  agent-supplied URL (directly or post-redirect) must use `safe_get` /
  `safe_stream` from `src/precis/utils/safe_fetch.py`. Raw
  `httpx…get(url, follow_redirects=True)` is an SSRF.
- **Cite code by durable anchor, not line, in docs/memory.** A `file.py:308`
  written down rots on the next edit; reference the symbol —
  `path/file.py::Qual.name` (Python `__qualname__` shape). `scripts/coderef
  anchor file.py:LINE` authors it, `resolve <anchor>` → clickable `file:line`,
  `check docs` flags drift (advisory, in `/whatneedsdoing`). Convention:
  `docs/conventions/code-anchors.md`. (Bare `:line` is fine in throwaway
  chat/terminal — this governs what gets written down to read later.)
- If another branch left trivial drift (needs `ruff`), just fix it.

## Agent sizing

- **Cheap:** search, extract, format, lint; single-file edits, local refactors.
- **Opus:** multi-file/architecture; CFD/DFT/ML and NOx/catalyst reasoning;
  core API or abstraction decisions.
- **Default:** start cheap for mechanical/exploratory work; escalate only
  when deep cross-file reasoning is clearly needed.
- **Prefer a cheap-pinned agent over a bare Opus subagent for rote work, when
  practical** (a default, not an enforced gate — subagents otherwise inherit
  the session model = Opus, so mechanical delegations run expensive by
  accident). Haiku-pinned defs in `.claude/agents/`: `navigator` (locate /
  orient), `extract` (rote gather), `test-runner` (`scripts/test`), `tidy`
  (ruff/format), `cluster-ops` (read-only ssh/log-tail/prod-psql probe — keeps
  raw log & psql dumps off the main loop; never deploys or writes). Reach for
  these instead of spawning `general-purpose` on Opus for a mechanical task;
  use the Agent tool's `model:` for one-off downgrades.
- **Three tiers, cheapest that fits:** a **deterministic** chore → a *script*
  (zero model, reproducible) — the hygiene scans already are (`memory-lint`,
  `migration-check`, `docs-orphans`, `backlog-lint`), as are the **cadence
  nudges** that only decide *when* a judgment pass is due (`token-review`, the
  7-day session-tightness clock); **mechanical-but-needs-a-model** → a haiku
  agent (above); **judgment / stakes** → Opus. So memory *index* hygiene is a
  script; memory *reconsolidation* (re-verify claims vs code, merge/sharpen) is
  a judgment pass — not haiku. Likewise the token-review *cadence check* is a
  script; the *review* it triggers is a judgment session.
