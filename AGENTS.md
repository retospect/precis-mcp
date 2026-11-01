# precis-mcp — Codex shim

Codex-facing entry. Claude Code reads `CLAUDE.md`; both share one doc system.
This file holds Codex guidance and the shared repo-dev graph-memory policy.
Kimi Code: read `.kimi-code/AGENTS.md` before your first tool call; it
loads late on its own.

## Read order

`docs/README.md` (the doc-system contract) → `docs/codebase.md` (shape,
lifecycle, seams) → the owning package's `__init__.py` docstring →
`docs/glossary.md` → `docs/backlog/INDEX.md` (generated, gitignored —
run `scripts/docs-index` if absent). Runtime kinds and verbs:
`get(kind='skill', id='precis-overview')`.

## Rules by pointer

- **Ship workflow** (worktree per task, `/land`, `/go`, `/qland`, gate,
  refs): `CLAUDE.md` §Ship workflow (one line per path) and
  `docs/conventions/shipping.md` §Long form. Do not hand-roll a ship; use
  `scripts/ship`.
- **Invariants** (forward-only migrations, append-only body chunks,
  `safe_fetch`, `uv`): `docs/conventions/invariants.md`.
- **Conventions**: `docs/conventions/` (start with `thresholds.md`; prose
  style `llm-facing-prose.md`; code anchors `code-anchors.md`;
  poster/display TeX `poster-typography.md`; MCP handler error contract
  `mcp-authoring.md`).
- **Tests and gate shape**: `docs/conventions/testing.md`. Run tests with
  `scripts/test`; never bare `pytest`, `pip` or `mypy`.
- **Skill evals** (advisory, never a ship gate): `scripts/skill-eval`
  runs `src/precis/data/skill-evals/<skill>.yaml` through a fake or a
  budgeted host `claude -p` runner. → `docs/conventions/skill-evals.md`.
- **Code navigation**: structure first, grep second. The precis python kind
  (`search(kind='python', mode='pattern', q=...)`;
  `get(kind='python', id='main::<qualname>')` for signature, callers and
  callees) and `scripts/coderef callers|deps <file.py::Sym>`. `main::`
  reads main; a worktree session queries its own tree as
  `wt-<tree name>::<qualname>` (read-only; grep if the alias doesn't
  resolve). Grep is truth for code you changed. Skill
  `precis-python-help`.
  Never `rg -rn`/`rg -rln`: in ripgrep `-r` is `--replace <text>`, so
  `-rn` rewrites every match to `n` and silently mangles output
  (gr515812). Use `rg -n`.
- **Rounds**: when a round is open (`scripts/round status`), mark it from
  your tree with `scripts/round in <sha>|none|eta <text>`. The mark is the
  status report; message the coordinator only for a question, blocker or
  decision. Details: `docs/conventions/shipping.md`.
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

A `section:threads` node is one campaign's resume state, days not weeks. The
programme lives in its `docs/backlog/threads/<slug>.md` (link it); the node
keeps only where this campaign stopped. Its first line is `Left: …` (or
`Left: none`) — the index shows it — and is updated in the same session an
item finishes or appears. No untracked resume files. A follow-up that waits
on time is a todo or gripe tagged `due:YYYY-MM-DD`, linked from the node.

Retire (soft delete, recoverable at the SQL layer) so recall stays current:
- A thread node at `Left: none` with no open linked due-item; or one
  untouched for 14 days, after moving what still matters to its thread
  file, a gotcha, or a todo.
- A fact that changed but is the same subject: anchored `edit` in place, no
  second node. A wrong fact: `edit` with a `reason=`.
- A node replaced by a different node: link the new one `supersedes` the
  old, then retire the old.
- A mirrored node whose file was deleted: retire it (the importer's
  `--missing retire`, or by hand).
Any session may retire a node it has shown landed or superseded.
Since 2026-10-14 the daily `memory_thread_sweep` cadence retires the
thread cases automatically — `Left: none`, an inbound live `supersedes`
edge, or 14 days untouched (a `Left:` naming open work becomes a linked
todo first); `precis memory sweep` dry-runs it.
`scripts/memory-lint` lists candidates; it never retires. It also lints
each node as `fisheye+1hop` shows it.

Shape (since 2026-10-10): root me474312 → subject hubs (`section:index`) →
nodes, each with one `part-of` edge to its hub (or to a summary node that
has one) and one type tag `section:threads|gotchas|runbooks|workflow|reference`;
a gotcha links the threads it applies to with `qualifies`. Bodies stay under
the 4000-char fisheye cut; longer material becomes `part-of` children of a
short summary — a child takes a unique title (the summary's title + the
pass/sub-thread it holds, so the dup-thread-prefix check stays quiet), the
parent's `section:<type>` tag (`section:reference` when the child is a
result), one `part-of` edge to the summary only, and no `qualifies` links
(it inherits the parent's). Writes: the `remember` skill (the `put`/`edit`
response's `Next:` lines name the missing links). Periodic cleanup: the
`reconsolidate` skill. Search one subtree with `args={'under': 'me<hub>'}`.

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
start, to answer a question, or automatically on an MCP outage.

Outage (emergency measure, Reto 2026-10-22): MCP down → read the node copies
under `~/.cache/precis/memory-nodes*` as stale scratch (never edit them as
memory; they are overwritten). Anything that must be remembered is appended to
`<harness memory dir>/outage-memory.md` (`## <UTC timestamp> <session name>`,
then the fact); the SessionStart hook flags it once the graph is back and the
session integrates then empties it. No other memory writes, no replay tooling.
`docs/runbooks/memory-restore.md`.

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
remits are in `.claude/agents/`. Spec, verify and review work uses the
named Sonnet agents (`ready`, `reviewer`, `root-cause`, `test-author`);
pollers and runners use the Haiku agents (`cmd-runner`, `test-runner`) —
never `general-purpose` or a fork for these jobs. Other mechanical work
(search, extraction, tidying) goes to the small tier; a decided edit to a
coder, test author or documenter; design, schema and domain judgment stay
with the main loop. Subagent prompts must forbid `docker ... prune` and forbid
writing scripts in the shared `/tmp`.

An editing subagent either works in your tree (you stay off its files) or
runs with `isolation: "worktree"` and ends in `scripts/take-agent <tree>`
(fast-forward into your branch) or `scripts/take-agent --discard <tree>`.
Never copy its diff: ship reaps only agent branches take-agent recorded
(`docs/conventions/shipping.md` §Sync, and agent branches).

## Long form — orientation, conventions and traps (moved from CLAUDE.md, 2026-11-01)

The resident brief (`CLAUDE.md`) keeps one line per rule; the rationale and
pointers that used to sit there live here (context-memory-hierarchy S4).
Nothing below is new.

### Orientation

Specialist-domain work too: `search(kind='skill', q='<the task>')` before
doing any — precis carries curated skill families well beyond repo-dev, so
assume one exists until search says otherwise. Missing or hard to find →
gripe. `docs/codebase.md` (shape, lifecycle, seams) → the owning package's
`__init__.py` docstring. Runtime kinds/affordances: skills
`precis-overview`, `precis-toolpath-help`. Coined terms: `docs/glossary.md`.
Planned work: `docs/backlog/` (open items only, delete-on-ship); pillars +
the active thread set: `docs/roadmap.md`. Dated history: `git log` (no
CHANGELOG). Schema: `docs/reference/schema.md` (generated). Mission:
`docs/mission.md`. Replicate this setup: `docs/how-to-setup-like-this.md`.
Install/run the product: `docs/setup-single-machine.md` · cluster:
`deploy/README.md`. Code: workers `src/precis/workers/`, ingest
`src/precis/ingest/`, web UI `src/precis_web/`, Discord bridge
`src/asa_bot/`, SSRF guard `src/precis/utils/safe_fetch.py`. Design in se /
CAD / mechanical (geometry, joints, fasteners, bosses, enclosures, molds,
prints): the product's own skills are the spec — read them before
authoring. `precis-se-help` first (op grammar), then the task skill
(`precis-se-fasten-help`, `-print-`, `-fit-`, `-check-`, …). Repo entry
point: the `se-design` skill. Missing one → gripe.

### Conventions that bite — rationale

- **Forward-only migrations.** Never edit a sealed migration; ship a new
  one. → `docs/conventions/invariants.md`
- **Don't mutate body chunks.** Body `chunks` rows are append-only: DELETE
  + INSERT, never UPDATE. → `docs/conventions/invariants.md`
- **Session `precis` MCP targets PROD** (write-capable `agent_rw`). Writes
  land in production and need no ask unless destructive, outward-facing or
  over $25 (`docs/conventions/thresholds.md`, Reto 2026-10-01); do
  write-path *testing* on the dev DB (`scripts/dev`) — never this MCP.
  Ad-hoc SQL: `scripts/prod-psql "SELECT …"` (prefer read-only);
  `scripts/db` is local-only.
- **Agent-supplied-URL fetches → `safe_get`/`safe_stream`.** →
  `docs/conventions/invariants.md`
- **No cluster addresses anywhere — the repo is PUBLIC.** Tailnet/LAN IPs
  and vault blobs are gated across the *whole* tree
  (`tests/test_deploy_tree_no_secrets.py`); real node hostnames are gated
  under `deploy/` only (there they mean an un-parameterised role). Prod
  coordinates come from the gitignored overlay via
  `scripts/lib/pgb-host.sh` — use `scripts/prod-psql`, never a literal.
  Genuine ranges/samples take a `secret-gate: allow — <reason>` marker.
- **Embeddings come from the worker, not ingest** — ingest stores chunks
  `embedding IS NULL`; never call `fill_embeddings` from ingest.
- **`uv` for everything; tests via `scripts/test`** — never bare
  pytest/pip/mypy. → `docs/conventions/invariants.md`

### Hook/gate-enforced traps — detail

- Container-first; shell cwd is already this worktree — never `cd`. Other
  trees are NOT reachable by `git -C` (the harness refuses it) — read them
  with `scripts/inflight --json`. → `docs/conventions/container-ops.md`
- One plain command per Bash call: no loops, heredocs, `$(…)`,
  `&&`/`;`/`||` chains, or shell variables holding paths — the harness's
  worktree-isolation guard refuses them (gr456287, ~127/12 h); a chain
  another driver tolerates is still off-rule (gr514492). Write a script
  with the Write tool, then run it as one plain command.
- Text IO names `encoding="utf-8"` (ruff PLW1514 + AST-walk test; also
  `subprocess(..., text=True)`).
- MCP handler errors: a call does what its ack says or refuses with a next
  step; raises carry `next=`/`options=` (ratchet-pinned), library faults map
  to Upstream with a retry hint. → `docs/conventions/mcp-authoring.md`
- **Timestamps are UTC, labelled `Z`/`UTC`** — `date -u`,
  `datetime.now(UTC)`, never `date.today()`/`utcnow()` (`date +%s` is fine).
  → `docs/conventions/time.md`
- `rtk` digests noisy Bash output — you see a filtered digest. →
  `docs/conventions/rtk.md`
- Read/Grep tools over cat/sed/bash-grep; no `echo "==="` narration; don't
  re-Read files already in context.
- Structure-aware first: the session MCP's python kind
  (`search(kind='python', mode='pattern', q=...)`, `get(kind='python',
  id='main::<qualname>')` for signature + callers + callees) and
  `scripts/coderef callers|deps <file.py::Sym>` before grepping bare
  symbols. `main::` is the live main checkout; a worktree session queries
  its own tree as `wt-<tree name>::<qualname>` (git-registered worktrees,
  read-only; Grep if the alias doesn't resolve) — Grep is truth for code
  you changed here. Skill `precis-python-help`. Repo prose the same way:
  the md kind (`get(kind='md', id='main/docs/backlog/<item>.md')` for the
  heading outline, `~<heading>` for one section, `search(kind='md', q=...,
  scope='main/docs/backlog')`) before cat/grep on docs; roots are `main`
  (live main), `repo` (the image checkout) and `wt-<tree name>` (the same
  read-only worktree aliases as the python kind — Grep if the alias doesn't
  resolve). Skill `precis-md-help`.
- Product data (paper, quest, gripe, job, memory, finding) goes through the
  session MCP first (`get`/`search`, links, `extent='fisheye+1hop'`);
  `scripts/prod-psql` only when the MCP has no affordance for the question,
  and then name the missing affordance in a gripe (Reto 2026-10-20).
- Cite durable anchors, not line numbers, in docs/memory. →
  `docs/conventions/code-anchors.md`
- Bug intake → the `bug` skill before fixing; masked root cause → dispatch
  `root-cause` first.
- Skills are runtime docs: `src/precis/data/skills/`, served via
  `get(kind='skill')`.
- Sibling branches' trivial drift (needs `ruff`): just fix it, in its own
  commit.

### Response style — the examples

- No validation-as-move ("that's valid", "not your fault") and no reflexive
  agreement/praise ("you're absolutely right", "great question"). Agree
  when earned and say why; don't manufacture disagreement either.
- No performed insight: aphorisms, metaphors, named "tensions".
- Cut ceremony, not reasoning: no preamble/recap, no tool-call narration,
  no filler/hedges (just, really, basically, actually, it's worth noting),
  no pleasantries, no emoji, no decorative headers on short answers.
- Quote the shortest decisive line + `path:line`; never dump
  logs/files/diffs.
- Each fact once per response; don't re-derive what's established.
- No invented abbreviations (cfg, impl, req) — they save nothing.
- EXEMPT from compression: security warnings, confirmations for
  destructive/irreversible actions, and ordered multi-step instructions.
