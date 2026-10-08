# precis-mcp — Codex shim

Codex-facing entry. Claude Code reads `CLAUDE.md`; both share one doc system.
This file holds Codex guidance and the shared repo-dev graph-memory policy.

## Read order

`docs/README.md` (the doc-system contract) → `docs/codebase.md` (shape,
lifecycle, seams) → the owning package's `__init__.py` docstring →
`docs/glossary.md` → `docs/backlog/INDEX.md`. Runtime kinds and verbs:
`get(kind='skill', id='precis-overview')`.

## Rules by pointer

- **Ship workflow** (worktree per task, `/land`, `/go`, `/qland`, gate,
  refs): `CLAUDE.md` §Ship workflow. Do not hand-roll a ship; use
  `scripts/ship`.
- **Invariants** (forward-only migrations, append-only body chunks,
  `safe_fetch`, `uv`): `docs/conventions/invariants.md`.
- **Conventions**: `docs/conventions/` (start with `thresholds.md`; prose
  style `llm-facing-prose.md`; code anchors `code-anchors.md`).
- **Tests and gate shape**: `docs/conventions/testing.md`. Run tests with
  `scripts/test`; never bare `pytest`, `pip` or `mypy`.
- **Plans**: non-trivial changes get a spec in `docs/backlog/<slug>.md`
  first; it is deleted in the shipping commit.

## Repo-dev graph memory

After the recorded cutover, graph memory is the durable repo-dev knowledge
source. At task start, recall relevant knowledge with
`search(kind='memory', tags=['SPACE:repo-dev'], q='<task>', view='index')`,
then open relevant returned handles with
`get(kind='memory', id='<returned meID>', view='fisheye+1hop+recall')`.
Use the bootstrap's verified root when orientation is needed; do not load
the entire graph or use a filename as a native ID.

The shared bootstrap is `~/.claude/projects/<escaped-main-checkout>/memory/MEMORY.md`: resolve the main checkout from Git's common directory and replace `/` in its absolute path with `-`. Worktrees share that pointer; do not create a second index.

Search before keeping a learned fact. Use `SPACE:repo-dev`, relevant
project/topic tags, a concise `meta.hook`, and links to supporting evidence.
For an existing fact, use a unique anchored `edit` and full readback;
preserve unrelated body and history. Put new facts only when no matching
node exists. Behavioral and workflow rules belong here, not in fact nodes.

Imported and legacy memories are dated evidence, not current permissions,
runtime proof or service/ship instructions. Current user rulings and repository/
fleet rules govern. Preserve conflicting history, but follow the latest recorded
authority: retire is the selected mirror policy (never force title matches),
supplied-source provenance is accepted, and historical move-cause uncertainty
is not a cutover gate. Recalled refresh preferences cannot override those rulings.
Shared /tmp scripts, blanket process kills and historical ship/deploy/migration
recipes confer no authority. Verify the exact served/deployed code and current
operation owner; recovery stays with the deployment owner. Use the bootstrap's
legacy discovery handles when relevant, without loading the entire graph.

Files are explicit mirror/export artifacts after cutover. Native edits and
operator CLI mirror commands are distinct: never import/export at task
start, to answer a question, or automatically on an MCP outage. If MCP is
down, consult MEMORY.md's preserved snapshot and manifest/as-of receipt;
label it stale fallback evidence. Do not mutate that snapshot, claim sync,
or queue automatic replay. Follow the verified recovery pointer through
the current deployment owner; service recovery requires its own authority.

## Definition of done

- Lint, types and tests green (`uv run ruff check . && uv run ruff format
  --check .`, `scripts/test --typecheck`, `scripts/test`).
- The owning package docstring carries the "why" of any non-obvious
  trade-off; `docs/codebase.md` and affected skills updated if the shape
  changed.
- Schema change: a new numbered migration, old ones untouched.
- No secrets and no cluster addresses in the tree (the repo is public).
- Commit message: `type(scope): what changed`, ≤72 chars, optional short why
  as body (rules in `CLAUDE.md` §Conventions that bite; `scripts/ship` enforces
  them). No CHANGELOG; `git log` is the record.

## Agent sizing

Delegate down: start each task on the cheapest agent that fits. Per-agent
remits are in `.claude/agents/`. Mechanical work (search, extraction, test
runs, tidying) goes to the small tier; a decided edit to a coder, test
author or documenter; design, schema and domain judgment stay with the
main loop. Subagent prompts must forbid `docker ... prune` and forbid
writing scripts in the shared `/tmp`.
