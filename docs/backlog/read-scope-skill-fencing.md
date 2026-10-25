---
status: ready
title: Read-only connections see only read recipes (skills, toc, search, Next:, next=)
pillar: platform
---

# Read-only connections see only read recipes

Origin: gr512233 (Reto 2026-10-24, chat-99). Ruling: option B, Reto
2026-10-25. Thread: `threads/agent-surface.md`.

## Problem and evidence

Every caller gets the same skill bodies, skill toc and search rows, `Next:`
trailers and `next=` refusal lines, write recipes included. Read-only
callers pay tokens for recipes they cannot run, and the recipes invite
writes that the envelope then refuses.

- **Skills.** `rg '\b(put|edit|delete|tag|link)\('` over
  `src/precis/data/skills/` gives 1058 lines in 148 of 216 files; the
  call-shaped subset `\((kind|id)=` gives 727 lines in 132 files. Only 15
  lines (6 files) are `put(kind='gripe', ...)` creates, the one write a
  read-only run may make. The five verb skills (`precis-put/edit/delete/
  tag/link-help`) are write-only end to end (59 call-shaped lines).
- **Hints.** 653 write-call literals sit in `handlers/`, `runtime/`,
  `todo/` and `utils/` (60 files), mostly `Next:` rows; 281 `next=` lines
  across 47 files name a write verb.
- **Read-only is not a tool-list fact.** `extract`, `navigator`,
  `cluster-ops`, `reviewer` and `root-cause` carry `put`/`tag` so they can
  file a gripe (`.claude/agents/extract.md`, "Filing a gripe").
- **Scope already exists per process, not per session.** A `write:none`
  run gets `PRECIS_MCP_DENY_VERBS=put,edit,delete,tag,link`
  (`workers/envelope.py::denied_verbs`), the `agent_ro` role when tier-2 is
  on (`envelope.py::db_role`, dark by default per
  `store/pool.py::_db_role_enforced`), and the sandbox sidecar runs
  `precis serve --read-only` (`server.py::_restrict_to_read_verbs`).
  71d7bcb7e keys the gripe carve-out on the env deny
  (`runtime/dispatch.py::_put_denied`, `_ro_gripe_create`).
- **The shared session server has one scope for everyone.** One
  streamable-http process, one bearer token, one DB role
  (`server.py` module docstring). Claude Code subagents share the parent's
  MCP session (`handlers/skill.py::SkillHandler.get`, the `have=` comment),
  so nothing on that connection tells a read-only subagent apart today.

## Decision

**B: derive read mode from the connection scope; no per-agent or
per-call flag** (Reto 2026-10-25). In the gripe's lettering this is
candidate A without its request-flag first slice (candidate C).

Why: a flag the agent must pass fails open the first time a definition
forgets it, and split `-write-help` files (the gripe's B) double the
files, drift, and leave `Next:` and `next=` unfiltered. The server already
knows a run is read-only from its envelope; the work is to read that
signal in one place, add it to the shared server as a token scope, and
apply it on every surface that prints a recipe. One skill source stays the
truth; the server filters.

This trims what is shown. Refusal stays with the existing guards: the
dispatch verb deny, the DB role and `--read-only`.

## Design: scope source

New `runtime/scope.py` function `current_scope() -> Literal['read',
'write']`, the one function every surface asks. Resolution order:

1. **Per request, shared HTTP server (slice 4).** The token gate learns a
   second token, `PRECIS_MCP_TOKEN_RO`, whose scope is `read`. Scope is read
   at call time from the MCP request context (`request_ctx`, the contextvar
   `server.py::_calling_session` already reads; its `.request` carries the
   Authorization header), not from a middleware contextvar: streamable-http
   runs tool calls in the session's task, not the HTTP request's.
2. **Per process.** `read` when any holds: `precis serve --read-only`;
   `PRECIS_MCP_DENY_VERBS` names `put` (a `write:none` envelope);
   `PRECIS_MCP_READ_ONLY` is on; the boot probe
   (`dispatch.py::_probe_read_only`, `agent_ro` or a read-only DSN) said so.
   Computed once at boot and cached.
3. Otherwise `write`. `write:scoped` (only `delete` denied) stays `write`.

`dispatch.py::_put_denied` and the gripe handler's read-only branch
(`handlers/gripe.py`, the `_probe_read_only` call) move onto
`current_scope() == 'read'`, so a read token gets the same verb deny and
capped gripe channel a `write:none` run gets today.

## Design: marking write material

Three markers, smallest first. Authoring rule: mark the section, not the
line, unless a read section holds one stray write line.

- **Whole skill:** front matter `scope: write` (new optional
  `SkillFrontmatter.scope`, values `read`/`write`, default `read`). For
  the five verb skills and skills that only teach mutation.
- **Section:** a trailing ` {write}` on an H2/H3 heading. It drops the
  heading and everything to the next heading of the same or higher level.
  On a `chunk_by_h2` alias group (stacked question headings) the marker on
  any heading marks the group.
- **Lines inside a read section:** an own-line pair `<!-- write -->` /
  `<!-- /write -->`. Unbalanced or nested pairs fail the lint.

Exempt, never marked: a create-only `put(kind='gripe', text=...)` with no
`id=`, `link=` or `tags=` (the write:none channel takes `text=` only,
`handlers/gripe.py`). A gripe comment `put(kind='gripe', id=N, ...)` is a
write and gets marked.

All markers are stripped on serve in both scopes. In `write` scope the
served body is byte-identical to the file before it was marked.

## Design: filtering and the read-only pointer

One pure function, `render(text, scope)` in `utils/skill_scope.py`
-> (body, dropped_headings)`, does the strip and the drop. Every surface
below calls it or the verb test `is_write_call(call: str) -> bool`
(leading `put|edit|delete|tag|link(`; the gripe create above is `False`).

| surface | read scope |
|---|---|
| `get(kind='skill', id=slug)` | filtered body; each dropped run becomes one pointer line |
| `slug~N`, `slug/toc` | chunks on the filtered body; a fully dropped chunk serves the pointer |
| `scope: write` skill | a stub: title, `summary:`, pointer; not `NotFound` |
| `id='toc'` / index rows | `scope: write` skills omitted; footer `N write-only skills hidden (read-only connection)` |
| `search(kind='skill')` | drop hits from `scope: write` skills and from dropped chunks; footer counts them |
| graph footer (`_graph_footer`) | no links to `scope: write` skills |
| `Next:` (`utils/next_block.py::render_next_section`) | drop rows where `is_write_call`; no header if none left |
| hint bus (`hints.py`) | drop hints whose `text` contains a write call |
| `next=` (`runtime/error.py::render_error`) | drop write entries; if all drop, one pointer line |

Pointer text, one line per dropped run:
`(write recipe omitted: this connection is read-only. "<heading>" needs a
write connection; to report a problem: put(kind='gripe', text='...'))`.
It names what was cut, so a reader can ask a write-capable caller instead
of guessing the recipe does not exist.

Hand-built `Next:` text that bypasses `render_next_section` (about 45
files contain a literal `Next:`) is not filtered until slice 6 moves it
onto the renderer; slice 2 pins the current list as a ratchet.

## Design: caching

- `_SKILLS_MAP_CACHE` and the skill graph keep raw text; filtering runs per
  serve. It is a line scan over at most ~50 KB; a `(slug, rev, scope)`
  memo is allowed if a profile shows a cost.
- `SkillHandler._toc_cache` key gains the scope:
  `(slug, chunk_scope, scope)`. A missed key would serve a write toc to a
  reader or the reverse.
- `skill rev` (`have=`, gr477798) is the sha of the scope-filtered body,
  so a rev read in one scope never stubs a serve in the other.
- The skill embedding index (`FileCorpusIndex`, `skill_embeddings`) stays
  one index over the write-scope body; read scope filters hits after
  ranking, using a per-chunk write flag computed at index build. No second
  embedding cache.
- `utils/hint_ledger.py::was_shown` keys on rendered text per session; the
  filtered block is different text, so no change.

## Design: tests and evals

- `tests/test_skill_scope.py`: `render()` on fixtures: heading marker
  drops to the next same-or-higher heading; alias groups; inline pairs;
  `scope: write`; the gripe create survives and a gripe comment does not;
  markers stripped in `write` scope.
- `tests/test_runtime_scope.py`: each process source yields `read`; the
  default is `write`; `write:scoped` is `write`; a read token beats a
  write process (slice 4).
- Byte-identity: every marked skill, served in `write` scope, equals its
  unmarked text (fixture or `git show` of the pre-marking blob).
- Hints: `tests/hintcheck.py` runs each trailer test in both scopes; in
  `read` no extracted call is a write except the gripe create.
- `tests/test_skill_write_fence.py`: ratchet over the skills tree. Per file,
  count call-shaped write lines (`\b(put|edit|delete|tag|link)\((kind|id)=`)
  outside a marker, minus gripe creates; baseline in
  `tests/data/skill_write_fence_baseline.json`, which may only go down
  (`--write` refuses to raise, as `tests/test_mcp_raise_next_ratchet.py`).
  New skills start at zero.
- Eval: bytes per skill serve in both scopes (`scripts/skill-scope-bytes`,
  stdout table) and, after slice 4, one `cluster-ops` task run before and
  after, comparing tool-result bytes and the share of write rows in
  `Next:` from the `tool_calls` ledger. Numbers go in the commit body.

## Slices

**1 (M). Filter mechanism, process scope, one skill.** `runtime/scope.py`
with the process sources; `utils/skill_scope.py`;
`SkillFrontmatter.scope`; `SkillHandler.get` for the whole-skill,
`slug~N` and `slug/toc` paths, the scoped `_toc_cache` key and the scoped
rev; mark `precis-gripe-help` (36 matched lines, all three cases plus the
gripe exemption).
Acceptance: with `PRECIS_MCP_DENY_VERBS=put,edit,delete,tag,link`,
`get(kind='skill', id='precis-gripe-help')` shows no comment, triage,
close, link, delete or split recipe, shows one pointer per dropped run,
and keeps `put(kind="gripe", text=...)`; with the variable unset the body
is byte-identical to the unmarked file; the two scopes give different
`skill rev` values and a `have=` from one scope does not stub the other;
`tests/test_skill_scope.py` and `tests/test_runtime_scope.py` pass; the
commit body records the skill's bytes in both scopes.

**2 (S). `Next:`, hint bus, `next=`.** `is_write_call`;
`render_next_section`, `hints.py` rendering and `render_error` filter in
`read` scope. Acceptance: hintcheck green in both scopes; a read-scope
refusal whose `next=` were all writes prints the pointer line; a ratchet
lists the files that build `Next:` by hand.

**3 (S). toc, index, search, graph footer; the five verb skills.**
`scope: write` on `precis-put/edit/delete/tag/link-help`. Acceptance: in
`read` scope `id='toc'` omits them with the hidden-count footer,
`get(kind='skill', id='precis-put-help')` returns the stub,
`search(kind='skill', q='add a tag')` returns no hit from a write skill or
dropped chunk and says how many it hid; `write` scope output unchanged.

**4 (M). Read token on the shared server.** See the scope source design;
`deploy/mcp-http/precis-mcp-http-ensure.sh` mints `PRECIS_MCP_TOKEN_RO`
and prints a second client entry, `precis-ro`. Waits on Q1 for the agent
definition change. Acceptance: on one server, a request with the read
token gets `Denied` for `put(kind='memory', ...)`, files through
`file_gripe_readonly` for `put(kind='gripe', text=...)` and gets filtered
skills, while the write token is unchanged; two concurrent sessions with
different tokens keep their own scope (extend
`tests/test_mcp_session_concurrency.py`); the `cluster-ops` eval numbers
are in the commit body.

**5 (M). Lint ratchet, waves A and B** (see Migration plan). Acceptance:
the ratchet test is in the suite with the post-wave baseline; byte-identity
holds for every touched skill.

**6 (S). Hand-built `Next:` onto the renderer.** Acceptance: the slice-2
ratchet list is empty.

**7 (S, repeat). Wave C.** Acceptance: each run lowers the baseline; the
item ships when it reads zero.

## Migration plan

Sized 2026-10-25 with `rg '\b(put|edit|delete|tag|link)\('` (1058 lines,
148 files; prose mentions included) and the lint's call-shaped pattern
(727 lines, 132 files). Gripe creates (15 lines, 6 files) stay unmarked.

| wave | slice | files | matched lines | marker |
|---|---|---|---|---|
| gripe skill | 1 | `precis-gripe-help` | 36 | section + inline |
| A: verb skills | 3 | `precis-put/edit/delete/tag/link-help` | 89 | front matter |
| B: heavy skills | 5 | memory 39, relations 32, todo 24, toolpath 24, component 20, draft 20, plan 20, quest 20, job 18, todo-tree 18, structure 18, draft-review 17, paper-ref 17, pcb-route 17 | 304 | section |
| C: long tail | 7 | ~128 files, 1-15 lines each | ~629 | inline or section |

Wave A also reviews `precis-taproot-hub-edit-help`,
`precis-taproot-mint-help`, `precis-fix-gripe-help` and
`precis-draft-section-insertion` for `scope: write`. Reference skills
(`precis-relations`, `precis-tags`) document vocabularies a reader also
needs for `search` filters: mark only their call examples, not the tables.
Wave C splits by directory prefix (`precis-pcb-*`, `precis-se-*`,
`precis-draft-*`, the rest), one S run each; each run lowers the
ratchet baseline. Rough cost: wave B is one builder session, wave C
three or four.

## Acceptance criteria

Per slice, above. For the item as a whole:

- A `read`-scope caller (process or token) sees no write recipe in a skill
  body, skill toc, skill search, `Next:` trailer or `next=` line, except
  the create-only gripe filing; each cut is announced by a pointer line.
- A `write`-scope caller's output is byte-identical to today's.
- The fence ratchet baseline is zero and new skills cannot add unmarked
  write calls.
- Before/after bytes and the `cluster-ops` eval are recorded in the
  shipping commits.

## Explicitly NOT in scope

- Enforcement. The verb deny, the DB role and `--read-only` stay the
  guards; this item only extends the existing deny to the read token.
- Tool descriptions and the server `instructions` preamble. FastMCP sends
  one tool list per process; `tool-description-budgets` owns their size.
- Splitting skills into `-help` / `-write-help` files (rejected by the
  ruling).
- A per-agent or per-call mode flag (rejected by the ruling).
- Per-verb filtering for `write:scoped` (hiding only `delete` recipes).
- Removing `put`/`tag` from read-only agent definitions.
- Filtering non-skill corpora (`md`, `markdown` kinds serve repo text as
  is).

## Open questions / decisions log

1. **How do Claude Code read-only subagents get a read connection?** They
   share the parent's MCP session, so the token scope only reaches them if
   their definitions name a second server entry (`mcp__precis-ro__get`,
   ...) instead of `mcp__precis__*`. Recommendation: do that for
   `extract`, `navigator`, `cluster-ops`, `reviewer`, `root-cause` and
   `forensics`; choosing a connection is the scope, not a per-call flag.
   Needs Reto's confirmation that this fits "no per-agent flag". Blocks
   only the agent half of slice 4; slices 1-3 and 5-7 serve spawned
   `write:none` runs and the sandbox sidecar without it.
2. **Marker spelling.** `{write}` on headings and `<!-- write -->` pairs
   are the proposal. Slice 1 confirms both survive the md kind's indexer
   and `chunk_by_h2` unchanged; change the spelling there if not.
3. **Hint bus shape.** `hints.py::Hint` is prose (`text`, `topic`), so
   slice 2 scans `text` for a write call. If that drops read hints that
   merely mention a verb, add an explicit `write: bool` field to `Hint`
   instead. Not a blocker.
4. **Decided 2026-10-25:** option B, scope from the connection (Reto).
   The 2026-10-24 draft's request-flag slice 1 is dropped.
5. Related token work: `tool-description-budgets`, gr509449.
