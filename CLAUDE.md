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
| To-do list / what's planned next | `OPEN-ITEMS.md` — **open work only**; when a ship completes an item, *delete* its entry in that same commit (never leave a "done ✅" note — `git log` is the record, like memory's landed-work rule). **No "completed log" file/directory** — a resolved item is proven by `git log` + its regression test; reusable forensics from an incident go to an **ADR** or `docs/runbooks/`, never a done-log |
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
- **`rtk` compresses noisy command output** (token-killer CLI proxy;
  `brew install rtk`, a prereq like `uv`/`docker`). A **global PreToolUse hook**
  (`rtk init --global`, installed on Reto's dev Mac → covers *every* local
  worktree session) auto-rewrites Bash commands to `rtk <cmd>` transparently —
  no manual prefix needed. **Consequence: Bash output is a filtered digest, not
  raw.** If a detail is missing, re-run via `rtk proxy <cmd>` (raw passthrough)
  or read the teed full log. Only rtk's *known* commands are rewritten
  (git/psql/grep/find/docker/cargo/pytest…); `scripts/*` wrappers and the
  already-terse `scripts/test` pass through untouched, and the repo's Bash
  guards (commit-on-main / git-stash / prod-psql) are prefix-robust so the
  rewrite can't blind them. **No hook in CI or cluster `claude -p` → prefix
  manually there:** `rtk git …`, `rtk err -- <cmd>`, `rtk summary -- <cmd>`.
  Filters: committed `.rtk/filters.toml` overrides the user-global template.
  Uninstall the hook: `rtk init --global --uninstall` (then restart).
- **Semantic code search — first move to orient** (repo-dev, not the product).
  For *where-is / how-does / what-calls* questions, `search_code` (or the
  `navigator` subagent) **before** grep/Read — one query → ranked `file:line`
  across code+docs+tests. `claude-context` MCP over Milvus, up'd by a
  SessionStart hook. Pass the **MAIN** repo path, not your worktree's (else no
  hits). Index is **lazy** — code you just wrote can lag; Grep is truth there.
- **`coderef` for exact who-calls / what-depends-on** (structural complement to
  the fuzzy index above). Over Python, `scripts/coderef callers|deps
  <file.py::Sym>` before grep — exact, no same-named false positives, returns the
  *connected* code (also `imports|importers` via grimp; `-h` for verbs). A
  PreToolUse hook nudges on bare-symbol greps. Grep stays right for text /
  non-Python / a symbol you can't yet name; blind to dynamic dispatch.
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

The main loop is Opus, so every task it does *itself* bills Opus — **delegating
down is the primary cost lever, not an afterthought.** Pick the cheapest tier
that fits; each agent def in `.claude/agents/` carries its own remit and
guardrails (the Agent tool surfaces those descriptions), so this is just the map.

- **Script (no model)** — a *deterministic* chore: hygiene scans (`memory-lint`,
  `migration-check`, `docs-orphans`, `backlog-lint`) and the cadence nudges that
  only decide *when* a judgment pass is due (`token-review`, the 7-day
  session-tightness clock).
- **Haiku** — mechanical, needs a model: `navigator`, `extract`, `test-runner`,
  `tidy`, `cluster-ops`.
- **Sonnet** — a *decided* change or bounded op (the *how*, not the *what*):
  `coder`, `test-author`, `reviewer`, `documenter`, `dep-bumper`,
  `cluster-admin`, `forensics`.
- **Opus (main loop)** — the *what/why*: architecture; core API/schema/
  abstraction; CFD/DFT/ML and NOx/catalyst reasoning; mission/voice prose;
  novel prod diagnosis; memory *reconsolidation*.

Default: start cheap, hand a decided change to the sonnet tier, keep genuine
design/domain judgment on Opus. An agent def with no `model:` line inherits the
session model (= Opus), so a mechanical task runs expensive by accident; use the
Agent tool's `model:` for one-off downgrades.
