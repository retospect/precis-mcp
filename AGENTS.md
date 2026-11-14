# precis-mcp — Codex shim

Codex-facing entry. Claude Code reads `CLAUDE.md`; both share one doc system.
This file holds Codex guidance and the shared repo-dev graph-memory policy.
Kimi Code: read `.kimi-code/AGENTS.md` before your first tool call; it
loads late on its own.

## Read order

`docs/README.md` (the doc-system contract) → `docs/codebase.md` (shape,
lifecycle, seams) → the owning package's `__init__.py` docstring →
the repo-dev vocabulary taxonomy for an unfamiliar or overloaded term
(`search(kind='taxon', q='<term>', args={'under': 'tn559460'})`) →
`docs/backlog/INDEX.md` (generated, gitignored —
run `scripts/docs-index` if absent). Runtime kinds and verbs:
`get(kind='skill', id='precis-overview')`. Conduct, the same for every
agent: `get(kind='skill', id='soul')` — open only the entries the task
touches.

## Rules by pointer

- **Ship workflow** (worktree per task, `/land`, `/go`, `/qland`, gate,
  refs): `CLAUDE.md` §Ship (the short form) and
  `docs/conventions/shipping.md` §Long form. Do not hand-roll a ship; use
  `scripts/ship`.
- **Invariants** (forward-only migrations, append-only body chunks,
  `safe_fetch`, `uv`): `docs/conventions/invariants.md`.
- **Conventions**: `docs/conventions/` (start with `thresholds.md`; prose
  style `llm-facing-prose.md`; code anchors `code-anchors.md`;
  poster/display TeX `poster-typography.md`; MCP handler error contract
  `mcp-authoring.md`; citing sources in specs and docs
  `citable-sources.md`).
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

**Memory shape card**

- One claim per node; no second topic.
- The title IS the claim: at most 12 words, the sūtra, with its scope.
- The body is the gloss: why, when it bites, what to do, one live anchor, a UTC date.
- Edges carry roles: `part-of` the hub, `cites`/`derived-from` for evidence, `qualifies` for scope.
- Name entities by handle; their current state belongs on their refs, never restated here.
- Expiry follows shape (gr560763): threads/handoffs/resumes/snapshots 14 days; incidents/campaigns 90 days.
- Rulings use `review_by`; reference/gotchas/runbooks have no age expiry; DREAM never expires by age.
- Expiry means review and compact before retirement, preserving still-true claims.
- No landed history or children lists; use the graph's links.
- No plan label (slice/round/R-number/leg/wave) without its handle on the same line.
- No bare commit sha older than 30 days; cite a live `path/file.py::Symbol` instead.

A recalled memory or set of memories is wonky, inconsistent or unclear →
file a `project:memory` gripe first. Include Goal, Route and the claims,
naming every involved memory and entity handle. The server attaches the
session's last ten memory get/search calls as `meta.route`; the written
story supplies the goal and the disagreement. The gardener fixes these,
never a coder lane. With evidence edit, on suspicion gripe: after learning
the truth with evidence, follow `precis-memory-help` "A recalled memory
misled me" and `precis-fix-memory-gripe-help`. Hold and ask before changing
a human-origin node. Replays must preserve both routes and recall coverage.

After the recorded cutover, graph memory is the durable repo-dev knowledge
source. At task start, recall relevant knowledge with
`search(kind='memory', tags=['SPACE:repo-dev'], q='<task>', view='index')`.
For exploratory memory/hub/index reads, prefer
`get(kind='memory', id='<handle>', extent='fisheye+2hop')`; explicit narrow
reads stay narrow. Canonical procedure: `get(kind='skill',
id='soul-structure')`, for selective group expansion and truncation
reporting. Mechanics: `precis-fisheye-help`, "The second hop".
This browsing preference does not change runtime defaults.
Use the bootstrap's verified root when orientation is needed; do not load
the entire graph or use a filename as a native ID.

The shared bootstrap is `~/.claude/projects/<escaped-main-checkout>/memory/MEMORY.md`: resolve the main checkout from Git's common directory and replace `/` in its absolute path with `-`. Worktrees share that pointer; do not create a second index.

Vocabulary lives in the same graph, as taxa, not in a file (Reto
2026-11-03): every coined or overloaded repo-dev term is one `taxon` under
the root `tn559460` ("repo-dev vocabulary"), hung off the hub node that
matches its memory hub (Ship/CI, Agent conduct, Session MCP, Product core,
Drafts & nanopubs, CAD/se/pcb, Chemistry) or the Projects & quests node for
a nickname. Look one up with `search(kind='taxon', q='<term>',
args={'under': 'tn559460'})`. The session that coins a term puts it:
`put(kind='taxon', text='<term> — <gloss> → <start-here anchor> · skill
<name>', meta={'pointer': '<anchor>', 'aliases': [...]},
link='taxon:<hub tn>', rel='specialises')`; a retired synonym becomes an
alias, a pending rename is noted in the gloss and flips with the code.

Search before keeping a learned fact. Use `SPACE:repo-dev`, relevant
project/topic tags, a concise `meta.hook`, and links to supporting evidence.
For an evidence-backed change to an existing fact, subject to the
human-origin hold above, use a unique anchored `edit` and full readback;
preserve unrelated body and history. Edit a changed fact on the same subject
in place; correct a wrong fact with `reason=`. Put new facts only when no
matching node exists. Behavioral and workflow rules belong here, not in fact nodes.

A `section:threads` node is one campaign's resume state. The
programme lives in its `docs/backlog/threads/<slug>.md` (link it); the node
keeps only where this campaign stopped. Its first line is `Left: …` (or
`Left: none`) — the index shows it — and is updated in the same session an
item finishes or appears. No untracked resume files. A follow-up that waits
on time is a todo or gripe tagged `due:YYYY-MM-DD`, linked from the node.

Retire (soft delete, recoverable at the SQL layer) after review and
preservation of still-true claims:

- A thread node at `Left: none` with no open linked due-item.
- A node replaced by a different node: link the new one `supersedes` the
  old, then retire the old.
- A mirrored node whose file was deleted: retire it (the importer's
  `--missing retire`, or by hand).
Any session may retire a completed or superseded node after this preservation,
subject to the human-origin hold above.
`precis memory sweep` dry-runs sweep candidates.
`scripts/memory-lint` lists candidates; it never retires. It also lints
each node as `fisheye+1hop` shows it.

The graph runs from root me474312 through subject hubs (`section:index`)
to nodes. Each node has exactly one parent: its hub or a summary with a
parent chain to that hub; use one `part-of` edge and exactly one type tag
`section:threads|gotchas|runbooks|workflow|reference`. A gotcha links the
threads it applies to with `qualifies`. Keep glosses under the 4000-character
fisheye cut; longer detail becomes children of a short summary. Each child
has a unique atomic claim title, the parent's `section:<type>` tag
(`section:reference` for results), and no `qualifies` links: it inherits
the parent's scope. Writes: the `remember` skill (the `put`/`edit`
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
- Commit message: `type(scope): what changed`, aim ≤72 chars, hard limit 120, optional short why
  as body (rules in `docs/conventions/shipping.md` §Long form; `scripts/ship`
  enforces them). No CHANGELOG; `git log` is the record.

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

The resident brief (`CLAUDE.md`) keeps only the hard rules and pointers
(Reto 2026-11-12); the rationale, orientation and traps that used to sit
there live here.

### Orientation

Specialist-domain work too: `search(kind='skill', q='<the task>')` before
doing any — precis carries curated skill families well beyond repo-dev, so
assume one exists until search says otherwise. Missing or hard to find →
gripe. `docs/codebase.md` (shape, lifecycle, seams) → the owning package's
`__init__.py` docstring. Runtime kinds/affordances: skills
`precis-overview`, `precis-toolpath-help`. Coined terms: taxa under
`tn559460` (`search(kind='taxon', q='<term>', args={'under': 'tn559460'})`).
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
