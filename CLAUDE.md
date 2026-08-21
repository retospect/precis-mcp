# Claude Code — project brief

> **Two surfaces — don't confuse them.** This repo **is** the precis MCP
> server. The `precis` MCP tools and `get(kind='skill', id=…)` skills in
> your session are the **product's** runtime surface, for cluster agents
> operating precis — **not** dev aids for this repo. Don't reach for
> `get(kind='skill')` to understand the code.

> **Lean router**, loaded every session: ship workflow, conventions that
> bite, pointers. **The doc system — where truth lives, how to keep it
> true — is defined once, in `docs/README.md`; defer to it.** Reading order:
> `docs/codebase.md` → the owning package's `__init__.py` docstring →
> `docs/glossary.md` → `docs/backlog/INDEX.md` (generated).
> `AGENTS.md` = conventions/workflow/DoD. This file changes only when the
> workflow, a convention, or the subsystem *set* changes.
> Prose house-style: `docs/conventions/llm-facing-prose.md`.

## Session workflow (worktree → ship)

1. **Start in a worktree** — `claude -w <name>` creates
   `.claude/worktrees/<name>/` on branch `worktree-<name>`, isolated from
   `main` and siblings.
2. **Do the work** — implement, test, iterate.
3. **`/land`** (ship) **or `/go`** (ship + deploy). Both run
   `scripts/ship`: commit WIP → sync (`fetch` + `merge` main) → container
   gate (auto-fix ruff, then `ruff` + `mypy` + `pytest`) →
   squash-merge to `main` if green → reset branch to shipped `main` →
   local-main fast-forward. **`/land` runs `scripts/ship --impacted`** — pytest
   narrowed to testmon's affected-tests set (ruff/mypy still full); **`/go`
   runs the full suite** (authoritative before a deploy) and also
   `scripts/deploy` (`ansible-playbook redeploy-precis.yml`). Both abort+report
   on gate failure; scripts are idempotent, so fix and re-run.

`scripts/ship` squashes onto `main` via `commit-tree` + `--force-with-lease`
CAS push, then resets the branch to shipped `main`. Merge target is `main` —
repo has no `master`.

**In-flight worktrees.** Many sibling sessions run at once. Before starting,
**scan for overlap**: `scripts/inflight` prints a live per-worktree table
(session, dirty, ahead/behind, PURPOSE, last commit); a `SessionStart` hook
injects it. **Once your task is clear, write one line to `.claude/purpose`**
(gitignored, self-cleaning) — git derives everything except intent. A
worktree that's merged + clean + has no live session (`inflight`'s
`safe_remove` bucket) is auto-reaped — on this session's `SessionEnd` if it's
the one closing, and as a `SessionStart` backstop otherwise
(`scripts/reap-worktrees`, `scripts/hooks/session-end-reap.sh`;
`PRECIS_NO_AUTOREAP=1` opts out). The primary checkout likewise auto-pins
back to `main` when drifted onto a merged+clean branch
(`scripts/hooks/heal-primary-branch.sh`, `PRECIS_NO_HEAL_PRIMARY=1` opts
out); a `PreToolUse` guard blocks checking out a branch there first
(`ALLOW_CHECKOUT_IN_PRIMARY=1` opts out).

## Subsystem map (detail on demand)

Start at `docs/codebase.md`'s package map, then the owning package's
`__init__.py` docstring.

- **Todo tree** — `kind='todo'` task graph. →
  `precis-tasks-help`, `precis-dispatch-help`.
- **Review tiers** — `nursery` (SQL/min) / `structural` / `deep_review`
  (opus). → `precis-nursery-help`.
- **Workers** — derived-queue passes, `system`/`agent` profiles. → the
  `precis.workers` package docstring.
- **Nanopub publication** — reviewed claims minted as signed,
  OTS-anchored artifacts (`view='nanopub'`, `precis nanopub` CLI). → the
  `precis.nanopub` package docstring; `precis-nanopub-help`.
- **Discovery layer (F20)** — per-chunk KeyBERT (`chunks.keywords`),
  `view='toc'`. → `docs/conventions/discovery-layer-policy.md`.
- **Chunk-tag classifier** — cascade regex → `role3`, default-OFF. →
  `src/precis/workers/classify.py`.
- **Live affordances** (folder, plan, figure, mermaid, gripe, concept,
  quest, structure, citation, email, term registry, cad/pcb, search, SSRF
  guard, ingest hygiene, …) → matching `precis-*-help`; full table:
  `precis-overview`.
- **Skill index** → `precis-toolpath-help` (call sequences).

## Where to find context

| Task | Read |
|---|---|
| **Orientation — read first** | **`docs/codebase.md`** (shape, lifecycle, seams) |
| Subsystem detail (present-state + why) | the owning package's `__init__.py` docstring (map: `docs/codebase.md`) |
| Coined / overloaded terms, project & quest aliases → files | `docs/glossary.md` |
| To-do list / what's planned next | `docs/backlog/` (one file/item, gitignored generated `INDEX.md`) — open work only, delete-on-ship (`docs/README.md`); incident forensics → `docs/runbooks/`, never a done-log |
| Conventions / workflow / DoD | `AGENTS.md` |
| Mission / pitch narrative | `docs/mission.md` (positioning, not architecture) |
| Master kinds table + recipes | skills `precis-overview`, `precis-toolpath-help` |
| Dated history | `git log` (no CHANGELOG) |
| Replicate this repo's setup elsewhere | `docs/how-to-setup-like-this.md` (portable scaffolding brief) |
| Full schema (prose / visual) | `docs/reference/schema.md` (generated); `docs/reference/schema-v2.svg` |
| Worker queue pattern | the `precis.workers` package docstring |
| Ingest pipeline | `src/precis/ingest/{marker,pipeline,text_chunker,db_writer}.py` |
| Worker code | `src/precis/workers/` |
| Web UI | `src/precis_web/` |
| Discord bridge (asa) | `src/asa_bot/` — `[asa]` extra; stdio to `precis serve` |
| SSRF guard | `src/precis/utils/safe_fetch.py` |

## Conventions that bite

- **Forward-only migrations.** Never edit a sealed `*.sql` in
  `src/precis/migrations/` — ship a new one. Fresh DBs load
  `migrations/baseline/schema.sql` + tail; regen via `scripts/bump`/
  `precis db dump-schema` (release-time, checked against files) — never
  hand-edit.
- **`uv` for everything.** Bare `pip`/`pytest`/`mypy` aren't reproducible.
- **Run tests via `scripts/test`, never bare pytest** — mounts your
  worktree + test DB; `--impacted` narrows to testmon's affected set, other
  args passthrough. → `docs/conventions/testing.md`.
- **Container-first; the shell is already IN your worktree.** `scripts/dev`
  → dev shell; `scripts/db` → psql (LOCAL `precis`/`precis_test` only). cwd
  is this worktree and the harness re-anchors it there after **every** Bash
  call — so a `cd <worktree>; …` prefix is pure token waste on every
  command; run bare. To read another tree (the primary checkout or a sibling
  worktree) use `git -C <path> …`, never `cd <path>` — a `cd` into the
  primary tree (siblings live under it) is hard-blocked by a guard and
  wastes the whole turn. →
  `docs/conventions/container-ops.md`.
- **Peeking at prod.** `scripts/prod-psql "SELECT …"` hops via
  caspar/melchior to `precis_prod` behind pgbouncer; `agent_rw` is
  WRITE-capable (prefer read-only) — `scripts/db` is local-only.
  `PRECIS_PROD_SSH_HOST`/`PRECIS_PROD_PSQL_OPTS` override host/flags.
- **Session `precis` MCP writes to PROD — READ-ONLY dogfood.** Local 5th
  worker; DB-backed kinds (todo, gripe, quest, memory, paper, …) target
  `precis_prod` as **write-capable** `agent_rw` (verify:
  `get(kind='skill', id='precis-status')`); "Sandbox PRECIS_ROOT" only
  scopes file-kinds. Read verbs only (`search`/`get`/`more`) —
  `put`/`edit`/`delete`/`tag` mutate production. Write-path testing →
  **dev-DB** precis (`scripts/dev`), never this MCP.
- **`rtk` compresses noisy Bash output** via a global PreToolUse hook — what
  you see is a filtered digest, not raw. → `docs/conventions/rtk.md`.
- **Dedicated tools over shell-util soup in Bash.** Read file ranges with the
  Read tool (`offset`/`limit`), never `sed -n 'X,Yp'` / `awk '/a/,/b/'` /
  `cat`; match text with the Grep tool, not `grep -rn` (its output is capped,
  bash grep dumps the whole hit set into context). Never narrate compound Bash
  with `echo "=== … ==="` section headers — the tool result is already
  delimited, so each echo is pure token bloat. These are the top context sinks
  in dev sessions; `head`/`tail`/`ls`/`find` on a real result belong behind
  `rtk`.
- **Semantic code search first** (repo-dev, not product): where-is/how-
  does/what-calls → `search_code`/`navigator` before grep/Read — ranked
  `file:line` across code+docs+tests (`claude-context`/Milvus,
  SessionStart-indexed). **MAIN** repo path only; index is **lazy** — Grep
  is truth for new code.
- **`coderef`: exact who-calls/what-depends-on**, grep's structural
  complement. `scripts/coderef callers|deps <file.py::Sym>` before grep —
  exact, no same-name false positives (`imports|importers` via grimp, `-h`
  verbs). PreToolUse hook nudges bare-symbol greps; Grep still right for
  text/non-Python/unnamed symbols — blind to dynamic dispatch.
- **Read once; target large files.** A file you already Read this session
  is still in context — don't re-Read it whole to "refresh" (re-injects the
  whole thing; edit against what you have). For a large file you need a slice
  of, pass `offset`/`limit` rather than pulling all of it. Never Read
  `scripts/ship` (or other gate scripts) to debug a red gate — the failure is
  printed above the `✖`; read *that*, or `scripts/test <path/-k>`, not the
  harness.
- **Skills are runtime docs** — `src/precis/data/skills/` is the
  agent-facing channel, served via `get(kind='skill')`.
- **Bug intake → triage via the `bug` skill** before fixing — masked root
  cause (obvious fix hides a deeper defect)? Dispatch `root-cause` first.
- **Embeddings come from the worker, not ingest**: ingest stores chunks
  `embedding IS NULL`; `embed:bge-m3` fills them — don't call
  `fill_embeddings` from ingest.
- **Don't mutate body chunks.** `chunks` append-only for body rows
  (`ord >= 0`); only `ord < 0` card variants DELETE/re-INSERT via a
  registered synthesis pass. "Update" = DELETE + INSERT so the
  embedding/summary cascade re-runs — in-place UPDATE strands
  `chunk_embeddings`/`chunk_summaries`.
- **Text IO always names its encoding.** `open()`/`Path.open()`/
  `read_text()`/`write_text()` default to the *locale* encoding, which is
  cp1252 on the Windows CI leg — a bare read of a UTF-8 file dies there with
  `UnicodeDecodeError` and never locally. Two guards, because neither is
  enough: ruff `PLW1514` (preview rule, opted in by exact code — see the
  `[tool.ruff.lint]` comment) only tracks a name as a `Path` while it's bound
  directly to `Path(...)`, and the `/` binop erases that — `Path(d) / "x"`
  then `.read_text()` is invisible to it (69 of the 388 real sites were all it
  saw). `tests/test_text_io_encoding.py` AST-walks the rest. Neither sees
  `subprocess(..., text=True)`, which decodes the child's stdout by the same
  locale rule; pass `encoding="utf-8"` there by hand.
- **Outbound HTTP → `safe_fetch`.** Agent-supplied-URL fetches (direct or
  post-redirect) must use `safe_get`/`safe_stream`
  (`src/precis/utils/safe_fetch.py`); raw
  `httpx…get(url, follow_redirects=True)` is an SSRF.
- **Cite code by durable anchor, not line, in docs/memory** — `file.py:308`
  rots on the next edit. → `docs/conventions/code-anchors.md`.
- If another branch left trivial drift (needs `ruff`), just fix it.
- **Commit messages: one-line subject only, no body.** `git log` is the
  record, docstrings carry the "why" — a multi-paragraph body is pure
  overhead. Subject + the required Co-Authored-By/session footer only,
  even outside `/land`/`/go` (which already ask for this).

## Agent sizing

Main loop is Opus, so every self-done task bills Opus — delegating down is
the primary cost lever, start cheap. → tier map: `AGENTS.md` §Agent sizing;
each agent def in `.claude/agents/` carries its own remit.
