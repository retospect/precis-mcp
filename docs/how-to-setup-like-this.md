# Set up this repo like precis-mcp

Paste this into Claude Code on a fresh (or maturing) repo and say **"set it up
like that."** Establish the scaffolding below. Adapt names/tools to the stack,
but keep the **shapes**. Don't build all of it in one shot — propose the set,
then land it in small shipped increments. Prefer wiring an existing tool over
writing a new one.

---

## 0. Prime directive: everything terse, LLM-first

All docs, skills, hooks, and commit messages are **written to be acted on by an
agent, not read for narrative.** No filler, no executive summary, no
motivational preamble, no "note that". One sentence per concept; a table or an
inline `# comment` beats a paragraph; if a pointer carries the meaning, drop the
prose and just point. **Name internals** (files, functions, tables, flags) — the
internals are the payload. Codify this in a `docs/conventions/llm-facing-prose.md`
and point every doc-writer at it.

**Point, don't copy.** Every fact lives at exactly one altitude; elsewhere you
link. Duplication = N rot sites for one fact.

---

## 1. Doc architecture — the altitude ladder

| File | Altitude | Holds |
|---|---|---|
| `CLAUDE.md` | router (loaded every session) | what-before-first-tool-call + where everything is; changes only when the workflow/conventions/subsystem-set change |
| `docs/codebase.md` | orientation | invariants, lifecycle, subsystem map, seams — the shape that survives refactors. Carries a `_Verified @ <sha>_` stamp; bump on re-verify |
| `docs/architecture/state-map.md` | present-state | per-subsystem current status (read before touching one) |
| `docs/decisions/NNNN-*.md` (ADRs) | rationale | why a decision, what was rejected. Numbered; **never delete, only supersede**; `README.md` = index + supersession graph |
| `docs/architecture/glossary.md` | vocabulary | coined/overloaded term → best entry-point file |
| `AGENTS.md` | conventions / workflow / Definition-of-Done | the rules that bite |
| `docs/design/*.md` | plans | one per non-trivial change; **delete-on-ship** (see §7) |

**No CHANGELOG** — history is `git log`. **Freshness contract:** update the doc
in the *same commit* that changes what it describes; a subsystem change updates
state-map (and codebase.md if the *shape* changed) in that commit.

If the repo has two audiences (e.g. a product runtime + the repo-dev surface),
say so loudly at the top of `CLAUDE.md` ("two surfaces — don't confuse them")
and give each its own cut-list.

---

## 2. Worktree → ship workflow (the CLI wrapper)

- **Start every task in a worktree:** a `claude -w <name>` wrapper creates
  `.claude/worktrees/<name>/` on branch `worktree-<name>`, isolated from `main`
  and siblings. Many run at once.
- **Finish with one command, not hand-rolled git:** `/endsession` (ship) or
  `/go` (ship + deploy), both slash-commands that call **`scripts/ship`**:
  commit WIP → sync (`fetch` + `merge` main) → **gate** → **squash-merge to main
  via `git commit-tree` + `--force-with-lease` CAS push** → reset the feature
  branch to shipped main → fast-forward local main. Idempotent: fix and re-run.
- **Why plumbing, not `merge --squash`:** concurrent worktrees share one
  `.git/index`; a plain squash co-mingles staged files. `commit-tree` + CAS push
  sidesteps the race.
- **Merge target is `main`.** No `master`.
- **The gate (authoritative, in-container):** auto-fix `ruff --fix` + format and
  amend, then run `ruff` · `format` · `mypy` · `pytest` against the worktree.
  Docs/config-only changes take a **light gate** (lint + a link/doc-pointer
  check; skip mypy/pytest). Never ship red.
- **Commit messages:** conventional-commit style, terse subject; add a co-author
  and a session-permalink trailer if you want provenance.

---

## 3. Scripts — the canonical verbs (don't reinvent)

Establish thin, idempotent scripts and **always reach for them** instead of
hand-rolling. Admonish this in `CLAUDE.md`/`AGENTS.md`:

| Script | Does | Rule |
|---|---|---|
| `scripts/test` | runs the suite in the **dev container** against YOUR worktree (bind-mount), test DB wired, terse output | canonical inner loop; don't hand-roll `uv run pytest` on the host (missing extras → spurious failures) |
| `scripts/test --impacted` | only tests a change touches (testmon map) | tightest loop |
| `scripts/ship` / `scripts/deploy` | the full pre-merge gate / the deploy | never hand-roll the git dance or the deploy |
| `scripts/db` | psql to the LOCAL dev DB | container-first |
| `scripts/prod-psql "SELECT …"` | read prod through a bastion hop | prefer read-only; local `db` never reaches prod |
| `scripts/code-index` | seed/refresh the semantic code-search index | reproducible from shell, no MCP session needed |
| `scripts/docs-orphans` | flag `docs/design` plans with no inbound ref | advisory; wired into ship when the diff touches `docs/design/` |
| `scripts/migration-check` | flag duplicate migration **numbers** across main + all worktrees | advisory in ship when the diff touches migrations; fleet view in `/whatneedsdoing` |
| `scripts/memory-lint` | broken-link/unindexed + landed-thread scan (a `## Threads` bullet whose cited commits are all in main) + over-budget + reconsolidation-due signal | advisory; `/whatneedsdoing` |
| `scripts/backlog-lint` | flag done-marked items still sitting in the backlog (`OPEN-ITEMS.md`) | advisory in ship when the diff touches it; `/whatneedsdoing` |

Package manager: pick one (`uv`, etc.) and forbid bare `pip`/`pytest`/`mypy`
("not reproducible"). Container-first for ops.

---

## 4. Hooks (`.claude/settings.json`)

Small, single-purpose, never-block-unless-guarding:

- **`guard-commit-on-main`** (PreToolUse, Bash) — deny `git commit` on
  main/master. The drift-onto-main backstop.
- **`guard-worktree-path`** — catch edits whose absolute path points at MAIN
  instead of the current worktree.
- **`map-staleness-reminder`** (PostToolUse, Write) — on a Write to a
  handler/migration/other-usually-drifts path, print a one-line nudge to update
  the maps. Silent otherwise.
- **`code-search-up`** (SessionStart) — bring the code-search stack up + print
  the one path a session needs to hit the shared index. Never fails start.
- **In-flight worktree table** (SessionStart) — print a live per-worktree table
  (session, dirty, ahead/behind, PURPOSE, last commit) so you scan for overlap.
- **`.claude/purpose`** (gitignored, self-cleaning) — once the task is clear,
  write one line; git derives the rest.

---

## 4a. Custom commands, subagents & skills (`.claude/`)

`.claude/` is the repeatable-work surface — build it out so procedures aren't
re-driven from memory each session:
- **Commands** (`.claude/commands/*.md`) — slash-commands for multi-step
  routines: `/endsession` (ship), `/go` (ship+deploy), a `/whatneedsdoing`
  backlog survey. A routine done twice becomes a command.
- **Subagents** (`.claude/agents/*.md`; frontmatter = model + tool allowlist) —
  task-scoped, usually **read-only + cheap-model**, so heavy work doesn't burn
  main context: a `navigator` (codebase orientation), an explore/search agent.
- **Skills** (`.claude/skills/<name>/SKILL.md`) — a packaged procedure + a
  trigger description; loaded when the trigger matches. E.g. `docs-triage` (the
  dead-doc dead-check), an output-discipline skill.
- **`settings.json`** — wires the hooks (§4) and permissions.

---

## 5. Workspace aids

- **Semantic code search** — a local vector index (e.g. Milvus + a
  `claude-context` MCP) over the code. **One shared MAIN index** serves every
  worktree (hits are repo-relative). Seed once; freshness is lazy (Merkle
  re-sync). A SessionStart hook keeps the stack up.
- **A read-only `navigator` subagent** — orientation specialist ("where is X",
  "how does Y flow", "what calls Z") that reads the orientation docs + runs code
  search and returns `file:line` answers, so navigation doesn't burn main
  context. Cheap model.
- **`rtk`** (or any output-compressor) — prefix noisy commands
  (`rtk git …`, `rtk psql …`, `rtk err -- <cmd>`) to filter firehoses to signal
  and tee the full log to disk. Run it manually (the explicit `rtk` is the
  signal a filter is in play); never wrap already-terse or interactive commands.
- **A `docs-triage` skill + `docs-orphans` detector** — keep `docs/` current-
  state: the detector surfaces dead plans, the skill packages the per-doc
  dead-check (see §7).

---

## 5a. Named tools & token-efficiency (exemplars — "a tool such as X", swap freely)

Name the *need*; the tool is an example, not a mandate.

| Need | A tool such as |
|---|---|
| Compress noisy cmd output → signal (token saver) | **`rtk`** — CLI proxy: `rtk git/psql/rg …`, `rtk err -- <cmd>`; shows a filtered digest, tees the full log to disk |
| Compact tabular output for an LLM reader | **TOON** — token-lean table serialization, far terser than JSON rows (header once, values aligned); the format a verb *returns* |
| Semantic code search over the repo | a **`claude-context`**-style MCP over a local vector store (**Milvus** + **Ollama** embeddings) — one shared MAIN index, repo-relative hits |
| Reproducible env / package mgr | **`uv`** (forbid bare `pip`/`pytest`/`mypy` — not reproducible) |
| Containerized dev + ops | **Docker** + **Compose** |
| Lint + format (auto-fixed inside the gate) | **`ruff`** |
| Static type gate | **`mypy`** |
| Test runner: parallel + impact-select | **`pytest`** + **`pytest-xdist`** (`-n`) + **`pytest-testmon`** (`--impacted`) |
| Relational store (+ vectors) | **Postgres** (+ **`pgvector`**) |
| Deploy | **`ansible`** playbooks |
| Service supervision | **launchd** (macOS) / **systemd** (Linux) |
| Private host networking | **Tailscale** |
| Secrets | a **vault** (1Password / env-injected) — never in repo or transcript |
| GitHub ops | **`gh`** CLI |

**Token-efficiency is a first-class concern** — the biggest avoidable context
sinks are raw output and re-derived navigation. Patterns:
- **Compress noisy commands** (`rtk`) — never let a 1000-line firehose hit context.
- **Offload navigation** to a cheap read-only navigator subagent → it returns
  `file:line`, not file dumps.
- **One shared** code index, not a per-worktree re-index.
- **Run fewer tests** — `--impacted` (testmon) in the inner loop; full suite only at the gate.
- **Terse docs + terse script output** — the gate prints signal, not logs.
- **Fan out** heavy read/verify work to background subagents; keep the conclusion, not the transcript.

## 6. Memory (cross-session knowledge)

A file-based memory dir with:
- **Topic files** — one fact each; frontmatter is exactly `type` + `description`
  (identity = the filename; `[[slug]]` = filename stem). `type` ∈ `thread`
  (in-flight — delete on ship) · `runbook` · `gotcha` · `workflow` ·
  `reference`. Link related ones with `[[slug]]`; convert relative dates to
  absolute; for guidance/threads add **Why** + **How to apply**.
- **`MEMORY.md`** — always-loaded index, **hand-edited** (the harness manages
  this dir and expects it edited in place, not generated). One bullet per memory
  under a bare-noun section: **Threads** (in-flight — delete on ship) · Runbooks
  · Gotchas · Workflow · Reference. Keep it under a byte budget.
- **No `ARCHIVE.md`.** Landed work is deleted — the repo git log + ADRs are its
  record. Memory keeps only live threads + durable knowledge; `memory-lint`
  flags a `## Threads` bullet whose cited commits all landed in `main`.

Discipline: before saving, check for an existing file to update (no dupes);
delete memories proven wrong; don't save what the repo already records
(structure, past fixes, git history). Save the *non-obvious*.

**Reconsolidate ≤ once/day.** A full pass (re-verify claims vs code, compact the
topic files, **delete landed threads**, regenerate the index) is a **circadian**
chore, not per-session — re-auditing constantly churns without benefit, like
sleep-time consolidation. Keep a dated `memory-consolidation-log`; a `memory-lint`
reads its top date and only flags a full pass when the last was an earlier day (the cheap
broken-link/unindexed/size checks still run every time).

---

## 7. Git & repo conventions (the rules that bite)

- **Forward-only migrations.** Never edit a sealed `*.sql`; ship a new forward
  migration to fix bugs. A fresh DB loads a `baseline/schema.sql` snapshot then
  applies the tail; regenerate the snapshot at release time only, never
  hand-edit it. (A `docs/design/…md` path-link inside a sealed migration means
  you **can't de-ref it → can't delete that doc** — keep it.)
- **ADR log:** next number, never reuse; the older ADR names its successor and
  vice-versa; supersede (never retro-edit). Archive a fully-superseded ADR only
  when a live successor names it (move-not-delete: keep filename + one-line
  banner + update every referrer same-commit).
- **Design docs = delete-on-ship.** A plan is a point-in-time doc; when its
  feature ships the truth moves to code + the ADR, so delete the plan
  (**delete-default**, "rest in git for the archaeologists"). KEEP only a plan
  still referenced by `src/`, a current anchor, a sealed migration, or an active
  ADR/proposal that delegates real build-detail to it. Never delete a doc
  referenced by live code without fixing that reference in the same commit.
- **Never ship red; never commit on main; never bare `git stash`** in a shared
  worktree (use a WIP commit, or `git stash push -u -m <tag>` + `apply <sha>`).
- **Secrets never in the repo or the transcript** — inject from a vault/env.
- Outbound HTTP from agent-supplied URLs goes through one SSRF-guarded fetch
  helper, not raw redirect-following.

---

## 8. Agent sizing & observability

- **Sizing:** start cheap (search/format/lint, single-file edits); escalate to a
  frontier model only for multi-file/architecture/deep-reasoning work.
- **Observability:** know the log locations. **Mine the persisted run
  transcript** — the record each agent/job leaves (e.g. a `meta.transcript`
  field on the job row, or the CLI session log) — for recurring tool-call
  confusion (`[error:*]`) → fix the skill/tooling, not just the symptom. Verify
  DEPLOYED code by the installed artifact's commit id, not the checkout's
  `.git/HEAD`.

---

## 8a. Active backlog & review

- **One top-level `OPEN-ITEMS.md`** = the current to-do/backlog (distinct from
  memory, which is durable cross-session knowledge — backlog is *now*).
- **A `/whatneedsdoing`-style command** surveys open threads (backlog + unshipped
  branches + in-flight worktrees + open issues) and, as one step, mines run
  transcripts (§8) for latent tool-friction bugs.
- **Post-green-ship residual harvest:** bugs the session surfaced but didn't fix →
  persist to `OPEN-ITEMS.md`/tracker, fix the in-reach ones, file the rest; never
  spin on an unbounded chase.

---

## 9. Definition-of-done (put in AGENTS.md)

A change is done when: the gate is green via `scripts/test`; the touched-
subsystem docs (state-map, codebase.md stamp, affected skills) are updated in the
same commit; any dead plan doc is deleted + its refs fixed; residuals persisted
per §8a and either fixed-in-reach or filed — never spun on.

---

## 10. If the repo exposes an MCP / agent-facing API (optional)

Design the surface so an LLM drives it with minimal priming:
- **A tiny closed verb set over many kinds.** A handful of verbs
  (`get`/`search`/`put`/`edit`/`delete`/`tag`/`link`) × N resource `kind`s,
  discriminated by a `kind=` arg — one handler per kind, not a bespoke tool per
  resource. Fewer tools = less schema to load, less to confuse.
- **Universal short-code handles.** Every resource gets a terse, type-prefixed,
  copy-pasteable id (`pc324`, `dc149`, `td158` = 2-char type + decimal PK). The id
  verbs accept a handle with **no `kind=`** (prefix → table → row); search/get
  output *emit* handles. The LLM round-trips ids it sees; it never constructs them.
- **Discoverable skills via embeddings.** Ship runtime how-to docs as a searchable
  `skill` kind — `search(kind='skill', q='<goal>')` + a `toc`/overview entry
  point + per-kind `*-help` skills + one call-sequence "toolpath" skill. The agent
  finds the procedure by semantic search, not by reading a manual up front.
- **Skills ARE the docs.** Editing the skill files is the agent-facing channel —
  the server serves them live; no separate doc site to drift.
- **Terse tabular output** (a TOON-style compact format), not verbose JSON — the
  reader is an LLM; every token counts.
- **Typed links between resources** (a `link` verb + a relations vocabulary), not
  raw FK columns — relations are queryable and reversible.
- **Graceful degrade** — a read embeds the query but falls back to lexical if the
  embedder is down; never hard-fail a read.
- **Self-describing status** — `get(kind='skill', id='status')` returns build/sha/DB
  so the agent can orient itself.
