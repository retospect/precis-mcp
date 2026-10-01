---
status: draft
title: memory native authoring — harness memory is written and loaded as SPACE:repo-dev graph nodes, with a gate-runnable retirement test per file class
pillar: memory-graph
prio: high
model: opus
---

# memory native authoring — the write half of "memory is the graph"

## Motivation / why

`docs/roadmap.md` pillar 1 retires text memory "when the graph serves it",
and `file-mirror.md` §"Pillar-review deltas" item 3 states the condition
per file class — then says none of them can fire while the mirror is
read-only. The item that makes them fire was deferred to "after the recall
AC, as a separate item" (td458720) and never filed. Reto moved it ahead
(2026-10-01, "fix now, top priority"): the mirror stays the staging format
and the recall number stays the measurement, but the write path, the
session-start load and the retirement tests are built now, not after.

Today the pieces exist apart. `handlers/memory.py::MemoryHandler` already
has `put`/`edit`/`search` over a `memory_body` chunk at `ord 0` (embedded,
keyworded). The closed-axis table `store/types.py::_CLOSED_VOCAB` and the
per-kind gate `_KIND_ALLOWED_AXES` know nothing of `SPACE:`, so the axis
file-mirror designs is not enforceable. Session start loads `MEMORY.md`
through the harness (`scripts/memory-lint` header documents that contract),
and the repo's `SessionStart` hooks (`.claude/settings.json`) run shell
scripts only — nothing reads the graph at that moment.

## In scope

1. **`SPACE:` registered as a closed axis.** `_CLOSED_VOCAB['SPACE'] =
   {research, repo-dev, personal}`; `_KIND_ALLOWED_AXES` admits it on
   `memory`, `markdown`, `skill`, `finding`, `todo`, `gripe`. Default on
   write: a `memory` put with no `SPACE:` gets `SPACE:research`; the
   mirror roots `memory`, `skills`, `repo`, `backlog` stamp `SPACE:repo-dev`.
   `precis-tags` (the axis matrix skill) gains the row.
2. **Native memory nodes.** A harness memory bullet or topic file becomes
   a `memory` ref tagged `SPACE:repo-dev` + `section:<threads|runbooks|
   gotchas|workflow|reference>` (the five `MEMORY.md` groups), body = the
   topic file, title = the hook line. Links: `related-to` for `[Title]
   (slug.md)` and `[[slug]]` references (same grammar as file-mirror's
   `precis mirror-links` pass), `part-of` to the section node. One-off
   import `precis memory import <dir>` (idempotent on title) seeds the
   corpus; after that the graph is written directly with `put`/`edit`
   and the file is not regenerated.
3. **Session-start load from the graph.** `scripts/hooks/session-start-
   memory.sh` (added to the `SessionStart` list) prints the index — one
   line per `SPACE:repo-dev` memory node, grouped by `section:`, in the
   same bullet shape `MEMORY.md` uses — from `search(kind='memory',
   tags=['SPACE:repo-dev'], sort='section')` via the session MCP
   (`scripts/prod-precis tools` as the fallback when the MCP is down).
   Output size is bounded by the same preamble budget `scripts/memory-lint`
   reports (6000 tok at 2026-09-14): over budget prints the hooks only.
4. **Retirement tests, one per file class, all gate-runnable** (they
   assert the graph-side condition over the test DB; deleting the file is
   the ship step that follows a green test):
   - memory index — `tests/test_memory_index_retired.py`: seed the
     `tests/fixtures/file_mirror/` tree through `precis memory import`,
     run the session-start script against the test DB, output equals the
     fixture's `MEMORY.md` bullets modulo ordering within a section.
   - skill listing — `tests/test_skill_listing_retired.py`: the skill
     index section of the synthesised `precis-overview`
     (`handlers/skill.py`, the synthesised-slug table) rendered from
     `markdown:skills/*` refs and their `part-of` links equals the one
     rendered from the file walk; empty diff is the retirement condition.
   - conventions — `tests/test_conventions_retired.py`: every
     `docs/conventions/*.md` has a `finding` with a `tests` edge, else it
     is in a shrinking allowlist with a justification line (the
     `tests/test_skill_size.py::_ALLOWLIST` pattern). The minting that
     drains the allowlist is `conventions-as-findings.md`.
5. **Runtime doc.** `precis-memory-help` drops the sentence that declares
   precis memory a different system from the harness files; one paragraph
   on `SPACE:` and the import.

## Explicitly NOT in scope

- Writing into the harness memory dir from precis — never
  (`file-mirror.md` §NOT in scope). Retirement deletes the file by hand
  after its test is green.
- The read-only mirror roots, link-minting pass and recall fixture
  (`file-mirror.md` builds them; this item consumes 2 and 5).
- Skill authoring in the graph (skills stay files under
  `src/precis/data/skills/`; only the listing retires here).
- Session transcripts (`session-history-into-precis.md`), dreaming
  (`dreaming.md`), the resident/discovered split
  (`context-memory-hierarchy.md` — its P1 measurement is what reads the
  recall number this item changes).
- `SPACE:personal` semantics (pillar 4, held); the value is registered so
  the axis is closed, nothing writes it.

## Acceptance criteria

1. `tag(kind='memory', id=M, add=['SPACE:repo-dev'])` succeeds;
   `add=['SPACE:lab']` raises `BadInput` listing the three values;
   `SPACE:` on `paper` raises "axis not allowed on kind".
2. `put(kind='memory', text=…)` with no `SPACE:` tag lands `SPACE:research`;
   `precis memory import tests/fixtures/file_mirror/` on an empty test DB
   creates one `memory` ref per topic file plus five section nodes, every
   ref `SPACE:repo-dev`, `[Title](slug.md)` references present as
   `related-to` links; a second run creates nothing.
3. `scripts/hooks/session-start-memory.sh` over that DB prints the
   fixture's `MEMORY.md` bullets (test 4a passes); with the MCP
   unreachable it falls back to `scripts/prod-precis` and prints the
   same; with both down it prints one line naming the failure and exits 0.
4. Tests 4b and 4c exist and run in the gate; 4b passes (the two
   renders agree) and 4c passes against a complete allowlist that
   `conventions-as-findings.md` drains.
5. The recall fixture (`file-mirror.md` in-scope 5) scores the same or
   better over native nodes as over the mirror — the number logged in
   this item's decisions log.
6. `tests/test_deploy_tree_no_secrets.py` stays green; no memory path is
   a tracked literal.

## Target + blast radius

- `src/precis/store/types.py::_CLOSED_VOCAB`, `::_KIND_ALLOWED_AXES`;
  `src/precis/handlers/_link_tag_ops.py::apply_tag_ops` (default stamping)
- `src/precis/handlers/memory.py::MemoryHandler` (`section:` tag,
  `part-of` to section nodes, `sort='section'`)
- `src/precis/cli/` (`memory import`), `scripts/hooks/session-start-
  memory.sh`, `.claude/settings.json` SessionStart list, `scripts/memory-
  lint` (reads the graph count beside the file count)
- `src/precis/data/skills/precis-memory-help.md`, `precis-tags.md`,
  `precis-overview.md`
- tests: the three retirement tests, axis registration, import
  idempotence, hook fallback

## Open questions / decisions log

- **[decided 2026-10-01, Reto]** Build now, ahead of td458720's "after the
  recall AC" sequencing. The recall number (AC 5) becomes a comparison
  native-vs-mirror rather than a go/no-go for filing this item.
- **Dependencies, stated honestly.** On `file-mirror.md`: in-scope 1
  (multi-root `markdown`) is required by test 4b only; in-scope 5 (the
  synthetic fixture) is reused by test 4a and AC 5 — if file-mirror has
  not landed, this item checks in the fixture tree itself and file-mirror
  adopts it. On `session-mcp-shared-server`: the hook assumes a session
  MCP reachable at session start; the `prod-precis` fallback is the
  bridge until it is. Nothing here waits on term-taxonomy or measures.
- **First slice, no dependency:** in-scope 1 (axis) + 2 (native nodes and
  import) + 3 (hook) + test 4a. That alone retires `MEMORY.md`'s index
  role on Reto's machine; the topic files stay until recall AC 5 is read.
- **[open, non-blocking]** Whether section nodes are `memory` refs or a
  `folder` placement; start as `memory` refs tagged `section:index` so
  no new kind is needed.
- **[open, non-blocking]** Whether `scripts/memory-lint`'s hysteresis
  (20 KB/15 KB) maps to a node count or is dropped once the index is
  graph-side; decide after the first month of native writes.
