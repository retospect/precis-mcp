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
   {research, repo-dev, personal}`, code-only (no migration, no
   `data/axes/*.yaml`, same as `ATTEMPT`). `_KIND_ALLOWED_AXES` gains
   `SPACE` on the listed kinds `memory`, `skill`, `todo`, `gripe`;
   `markdown` and `finding` are unlisted (unrestricted) and accept it
   with no edit — listing `finding` would strip its other axes. So does
   every other unlisted kind; that is accepted, not gated. Default on
   write: `MemoryHandler.default_tags_on_create = ("SPACE:research",)`
   (the `gripe.py` `STATUS:open` pattern; a caller's `SPACE:` replaces it
   because closed tags add with `replace_prefix`). Mirror-root stamping
   belongs to `file-mirror.md`. `precis-tags` gains the axis row and the
   matrix entries.
2. **Native memory nodes.** `precis memory import <dir>` reads
   `<dir>/MEMORY.md`. Each `## <Section>` header becomes a **section
   node**: a `memory` ref titled `<Section>`, tags `SPACE:repo-dev` +
   `section:index`, `meta.section=<slug>` (lowercased header),
   `meta.order` = its position. Each bullet `- [Title](file.md) — hook`
   under it becomes a **topic node**: title `Title`, body = `file.md`
   minus its frontmatter (the bullet text when the file is missing),
   tags `SPACE:repo-dev` + `section:<slug>`, `meta.slug` = file stem,
   `meta.hook` = the hook text, `meta.order` = its position in the
   section, `part-of` → its section node. Sections come from the headers
   present, not a fixed list of five. `related-to` links from every
   `[X](other.md)` and `[[other]]` in a topic body to the node with that
   `meta.slug` (written by the importer; the handler's `kind:ref`
   autolink is separate). Idempotent on `meta.slug` (topic) and
   `meta.section` (section) among live `SPACE:repo-dev` memory refs —
   `refs.title` has no uniqueness, so the importer does the lookup. An
   existing node is never overwritten (graph-side edits win); links are
   re-added idempotently. After the seed the graph is written directly
   with `put`/`edit`.
3. **Session-start load from the graph.** `precis memory index` renders
   the index: sections by `meta.order`, then each section's topic nodes
   by `meta.order` (unordered native writes after, oldest first), one
   bullet each — `- [Title](slug.md) — hook` for imported nodes,
   `- Title (<handle>)` for a native node with no `meta.slug`.
   `scripts/hooks/session-start-memory.sh` runs it: with
   `PRECIS_DATABASE_URL` set, against that DSN; otherwise through
   `scripts/prod-precis` (the session MCP is not reachable from a shell
   hook). Budget: memory-lint's preamble budget (8000 tok since
   2026-09-29, CLAUDE.md + index, ~4 B/token) minus CLAUDE.md's share is
   passed as `--budget-tok`; over budget, hooks are cut to 60 chars and
   one trailing line names the overage (a tripwire like memory-lint's,
   not a hard limit). Any failure prints one line naming it and exits 0.
   Wiring it into `.claude/settings.json` is the cutover step, not this
   slice: while `MEMORY.md` still loads, the hook would double the index.
4. **Retirement tests, one per file class, all gate-runnable** (they
   assert the graph-side condition over the test DB; deleting the file is
   the ship step that follows a green test):
   - memory index — `tests/test_memory_index_retired.py`: seed the
     `tests/fixtures/file_mirror/` tree through `precis memory import`,
     run the session-start script against the test DB, output equals the
     fixture's `MEMORY.md` bullet lines (`- [Title](slug.md) — hook`,
     exact strings) under the same `## Section` headers, in order.
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
   with `tags=['SPACE:repo-dev']` it lands only `SPACE:repo-dev`.
   `precis memory import tests/fixtures/file_mirror/` on an empty test DB
   creates one `memory` ref per index bullet plus one section node per
   `##` header, every ref `SPACE:repo-dev`, `[Title](slug.md)` and
   `[[slug]]` references present as `related-to` links; a second run
   creates nothing.
3. `scripts/hooks/session-start-memory.sh` over that DB prints the
   fixture's `MEMORY.md` bullets (test 4a passes); over a dead DSN it
   prints one line naming the failure and exits 0; with `--budget-tok`
   below the index size it prints the cut hooks plus the overage line.
4. Tests 4b and 4c exist and run in the gate; 4b passes (the two
   renders agree) and 4c passes against a complete allowlist that
   `conventions-as-findings.md` drains.
5. The recall fixture (`file-mirror.md` in-scope 5) scores the same or
   better over native nodes as over the mirror — the number logged in
   this item's decisions log.
6. `tests/test_deploy_tree_no_secrets.py` stays green; no memory path is
   a tracked literal.

## Target + blast radius

- `src/precis/store/types.py::_CLOSED_VOCAB`, `::_KIND_ALLOWED_AXES`
- `src/precis/handlers/memory.py::MemoryHandler.default_tags_on_create`
- `src/precis/cli/memory.py` (`memory import`, `memory index`; logic in
  store-taking functions), `scripts/hooks/session-start-memory.sh`;
  `.claude/settings.json` SessionStart list at cutover only
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
- **[readiness 2026-10-02, needs-work → folded in]** Three blockers from
  the vet, each now in the text above: a shell hook cannot call the
  session MCP (→ `precis memory index` over a DSN, `prod-precis`
  otherwise); `sort='section'` does not exist and memory search swallows
  it (→ ordering by `meta.order` inside `memory index`); the 6000-tok
  budget was stale (→ 8000 minus CLAUDE.md, passed as `--budget-tok`).
  Advisories folded: default stamping via `default_tags_on_create`, not
  `apply_tag_ops`; the bullet's slug and hook persisted in `meta`;
  section nodes derived from the headers present; idempotence key is
  `meta.slug`, not the title.
- **[decided 2026-10-02]** The hook is not wired into `.claude/settings.json`
  in the first slice. Cutover (Reto's call) is four steps: run the import
  against prod, wire the hook, reduce `MEMORY.md` to a pointer, and change
  the memory-writing instruction so new memories go to `put(kind='memory',
  tags=['SPACE:repo-dev','section:…'])` instead of a file.
- **[decided 2026-10-02]** Section nodes are `memory` refs tagged
  `section:index`, not a `folder` placement — no new kind.
- **[built 2026-10-02, first slice]** Choices made in the build:
  - Registering `SPACE` reserves the bare flags `research`, `repo-dev` and
    `personal`. Prod had 0 live refs carrying them.
  - A `supersede` survivor keeps the originals' `SPACE:`, and a merge
    across spaces is refused.
  - A node orphaned between `put` and its meta patch is adopted by title
    on the next import.
  - Topic nodes with no known section render under `## Unfiled`.
  - The render reads `section:` tags; it does not read `part-of` links.
- **[open, non-blocking]** Whether `scripts/memory-lint`'s hysteresis
  (20 KB/15 KB) maps to a node count or is dropped once the index is
  graph-side; decide after the first month of native writes.
