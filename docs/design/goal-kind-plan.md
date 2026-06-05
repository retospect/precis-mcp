# Goal Kind Plan — human-written project goals, linkable from anywhere

Status: **queued** — plan captured for a future implementation slice.

A new `goal` kind: **file-backed, slug-addressed, human-written**
project-goal documents parsed into many small embedded chunks, with
formal links into and out of them. Sibling to `markdown` but a
separate kind so it earns its own mental model, list view, skill,
and filesystem root.

This doc also pulls in a cluster of related improvements that
bubbled up during the design discussion — link validation, read-only
awareness, optional embedding — because they're shaped by the goal
use case but apply equally to `markdown`.

## Why a separate kind from `markdown`

The usage pattern is different enough to be worth discriminating:

- **Intent** — durable project objectives vs scratch notes.
- **Longevity** — a goal is written once, revised occasionally,
  read often; a markdown note is write-once, reference-maybe.
- **Linking** — goals are heavily referenced *from* other refs;
  markdown is more often referenced *at* (in prose).
- **List view** — users want `get(kind='goal')` to show status /
  priority / owner / due, not a directory listing.
- **Skill** — goals get `precis-goal-help` explaining authoring
  convention, linking discipline, review cadence. Markdown gets a
  thinner skill focused on file mechanics.
- **Filesystem** — a dedicated `PRECIS_GOALS_ROOT` keeps charter
  docs physically separated from scratch.

## Prerequisites — already landed

The goal kind relies on two prerequisites that are already done as
of the phase-7/link-CRUD stash:

- ✅ `Store.add_link / remove_link / links_for` — `0005_link_relations.sql`
  seeded the full relation vocabulary (`cites`, `cited-by`,
  `derived-from/into`, `supports`/`supported-by`,
  `generalises`/`specialises`, `see-also`).
- ✅ `link=` / `unlink=` / `rel=` kwargs wired into numeric-ref
  handlers via `_link_target.py`, with pre-mutation validation of
  the target.

Remaining prereq work this plan needs to pick up before `goal`
itself lands:

- 🟡 **Cross-kind `link=` / `unlink=` on file-backed + paper**
  handlers. The link-CRUD pass explicitly deferred this. The goal
  kind needs it on its own put surface; fixing it generically
  upgrades markdown and paper too.

## Storage

No new tables. The existing hub-and-spokes carries goals:

- `refs` — one row per goal file, `kind='goal'`, `slug=<file path slug>`,
  `meta` carries front-matter (`status`, `priority`, `owner`, `due`)
  plus file fingerprint (`mtime_ns`, `sha256`, `size`, `path`).
- `blocks` — one row per logical chunk (heading / paragraph / code
  fence / list / table), slug content-derived, embedded.
- `links` — outbound links from goal blocks to other refs, inbound
  links from anywhere else.

New `0006_goal_kind.sql` migration registers `goal` in the `kinds`
table. Optional `goals` corpus seed if we decide on corpus
separation (see open decisions).

## Address shape (parallels `markdown`)

| Address                 | Renders                                          |
|-------------------------|--------------------------------------------------|
| `goal:`                 | Index with status / priority / owner columns     |
| `goal:ship-v2`          | Overview — front-matter + heading TOC + block count |
| `goal:ship-v2~slug`     | One block (content-derived stable slug)          |
| `goal:ship-v2/toc`      | Full hierarchical table of contents              |
| `goal:ship-v2/raw`      | Source markdown                                  |
| `goal:ship-v2/links`    | Outbound + inbound links, rendered as graph      |
| `goal:ship-v2/check`    | Link-health generator, paginated (see below)     |

Numeric-id form is disallowed — goal is slug-only.

## Chunking

Reuses `precis.utils.md_parse.parse_markdown`. One block per
heading / paragraph / code fence / list / table. Block slugs are
content-derived hashes, stable across re-ingest — same convention
as `markdown`. **Small blocks → links can target a specific chunk
of a goal** (the user's original ask).

## Writes — `put` modes

Same set as `markdown`: `create` / `append` / `replace` / `delete`.
Plus `tags=`, `link=`, `unlink=`, `rel=`, `untags=` accepted on every
call so an agent can append a paragraph and immediately link it to
the paper that motivated it:

```python
put(kind='goal', id='ship-v2~implementation-notes',
    text='Adopt async psycopg per benchmark in §3 of @paper:thompson2025.',
    mode='replace',
    link='paper:thompson2025~s3',
    rel='supported-by')
```

The inverse direction — pointing **at** a specific chunk of a goal
from a memory or todo — works through the same plumbing and becomes
useful once blocks are small:

```python
put(kind='memory',
    text='Decided: async psycopg for goal ship-v2.',
    link='goal:ship-v2~implementation-notes',
    rel='derived-from')
```

## Shared improvement #1 — `view='check'` link-health generator

Applies to any ref-bearing kind; we'll land it as a shared helper
and surface it first on `goal` and `markdown`.

**Categories:**

- **Hard breakage** — a formal `links` row where the target ref
  is soft-deleted, the target block pos no longer exists, or
  (for file-backed targets) the underlying file is missing on disk.
- **Soft warnings** — regex-detected path-like strings in block
  text (`src/foo.py`, `notes/x.md`) that fail to stat. Prose is
  informal by design; surface as advisory, not failure.

**Shape:**

```
get(kind='goal', id='ship-v2/check')

# ship-v2 — link health (3 of 12 unresolved)

## 1. ~impl-notes → paper:thompson2025
   target ref soft-deleted 2026-04-12
   fix: put(kind='goal', id='ship-v2~impl-notes', link='paper:<new>', rel='...')
        put(kind='goal', id='ship-v2~impl-notes', unlink='paper:thompson2025')

## 2. ~deploy-plan → markdown:notes--ops-runbook~setup-step-3
   block slug no longer exists (file rewritten)
   fix: inspect current blocks via get(kind='markdown', id='notes--ops-runbook/toc')

## 3. ~external-context → src/old/foo.py
   informal path mention (no formal link); file missing on disk
   fix: update prose, or convert to a python: kind link if the symbol still exists

Next: get(kind='goal', id='ship-v2/check?cursor=3') — all remaining
```

**Pagination:** `cursor=N` continuation, default page size 5.
Generator-shape so an agent loop can walk the full list without
context-window pressure. When clean, returns `all links resolve (N total)`.

**Shared helper** lives as `handlers/_link_check.py` or a method on
a base class; every handler exposing outbound links opts in by
declaring `check` in its views tuple.

## Shared improvement #2 — front-matter flags

Two new optional front-matter keys on file-backed kinds (markdown +
goal). Default behaviour unchanged.

```yaml
---
title: Project ship-v2
status: doing
embed: false            # default: true
readonly: false         # default: auto-detect filesystem; can be forced true
---
```

### `embed: false` semantics

- File parsed, ref created, blocks stored **with text but no embeddings**.
- Search via `search_refs_lexical` still finds it (title-level
  match). Block-level semantic search yields nothing.
- Linkable at **ref level** (`src_pos = -1`). Block-level links
  require the block to exist — so `embed: false` still lets you
  link block-by-block, you just can't find blocks by semantic query.
- Opt-in variant `embed: ref-only` → a single synthetic block at
  `pos=0` holding the title; no chunking. Useful for "just give me
  a stable handle I can link other things to".
- Use cases: big files, binary-ish drafts, embedding cost /
  privacy sensitivity, not-ready-for-chunk-search.

### `readonly: true` semantics (three layers)

| Layer | Source of truth | How detected | Effect |
|-------|-----------------|--------------|--------|
| Filesystem | `os.access(path, os.W_OK)` / chmod | Stat at `_resolve_path` | `put` → `BadInput("read-only on disk; chmod or edit externally")` |
| Front-matter `readonly: true` | The file itself | Parsed during ingest, cached in `meta.readonly` | `put` → `BadInput("ref marked readonly in front-matter; edit in your editor")` |
| Convention | Skill doc | n/a | "Some refs are reference; prefer linking-to over editing-of" |

Both detected layers are surfaced in the overview render:

```
# ship-v2
_Project: deploy v2 services_

path:     goals/ship-v2.md
status:   doing
readonly: yes (front-matter)
embed:    no (ref-only)
blocks:   1
…
```

So the agent sees state on every read and doesn't have to guess.

## `_FileHandlerBase` extraction

The existing `MarkdownHandler` is the only file-backed kind; the
shape it wants to factor out was flagged in the phase-6a memory
("extract `_FileHandlerBase` once a second file kind lands"). Adding
`goal` is that trigger.

**Base owns:**

- Path resolution (`_resolve_path`, slug↔path conversion,
  traversal safety via `Path.relative_to(root)`).
- Atomic writes (`_atomic_write`, tmpfile + rename).
- Lazy re-ingest (stat → mtime_ns compare → sha256 → re-parse on
  content change).
- Block upsert via `BlockInsert` replace semantics.
- Front-matter parsing + `readonly` / `embed` flag handling.
- Shared `view='check'` link-health generator.

**Subclass provides:**

- `kind` constant + `KindSpec` (title, description, env).
- `_parse_file(text) -> list[MdBlock]` (parse function).
- `_derive_title(blocks, fallback)` (title derivation).
- `_render_index()` (list view — goal differs from markdown here).
- `_render_overview(ref)` (summary render — goal adds status/priority).
- Optional: skill pointer for error trailers.

This matches the pattern `_numeric_ref.NumericRefHandler` uses for
numeric kinds.

## Skill: `precis-goal-help.md`

Covers:

- What a goal is — vs `quest`, `todo`, `oracle`, plain `markdown`.
- Front-matter shape (`status`, `priority`, `owner`, `due`, plus
  `readonly` and `embed`).
- Heading structure that gives agents good chunk handles.
- "Link, don't allude" — when to use `link=` vs prose. External
  paths in prose rot; formal links are FK-protected.
- Discovery: `get(kind='goal')` for the index,
  `search(kind='goal', q='...')` for content.
- Block-level link recipes (both directions).
- Stale-pointer disclaimer + pointer to `view='check'`.
- Agent cadence: run `view='check'` occasionally as a maintenance
  task.

Also add a `goal` row to `precis-overview` in the "Kinds — refs"
table so newcomers see it alongside `paper` / `quest` / `oracle`.

## Phasing

Four orthogonal slices, each independently shippable and
CHANGELOG-worthy.

### Slice 1 — Cross-kind `link=` / `unlink=` on file + paper

Prereq for goals *having* links. The link-CRUD pass did numeric
kinds; this extends:

- `MarkdownHandler.put(... link=, unlink=, rel=)` on all four modes
  (create, append, replace, delete). Block-level source via
  `id='slug~block'`.
- `PaperHandler.put(... link=, unlink=, rel=)` — gated by
  whichever put modes paper already supports.
- Generic helper in `_link_target.py` or a new `_put_linkmixin.py`
  so file + numeric share validation / rejection wording.
- Tests mirroring `test_link_crud.py` per handler.

### Slice 2 — Front-matter flags on markdown

Pure improvement to an existing kind. Lands before goal so goal
inherits a working behaviour.

- `embed: false` / `embed: ref-only` parsing + ingest branching.
- `readonly: true` parsing + filesystem check; both surfaced in
  overview render.
- Put paths consult readonly flag and raise `BadInput` with the
  right hint.
- Migration not needed (ref.meta is JSONB).
- Tests for every flag combination.
- `precis-markdown-help` updated.

### Slice 3 — `view='check'` link-health generator

Shared helper, surfaced on markdown first (existing kind benefits
immediately), inherited by goal.

- Shared module `handlers/_link_check.py` with
  `check_links(ref, *, cursor, page, store) -> Response`.
- Hard-breakage queries (target.deleted_at, block pos lookup,
  file-backed path stat for target kind).
- Optional: regex-based soft-warning sweep over block text (path
  and symbol patterns).
- Pagination via `cursor=` query-string on the view path.
- Handler declares `check` in its views tuple to opt in.
- Tests for every category + pagination + empty state.

### Slice 4 — `goal` kind

- `_FileHandlerBase` extracted from `MarkdownHandler`;
  `MarkdownHandler` refactored onto it (behaviour-neutral).
- `GoalHandler` on the base; `_render_index` + `_render_overview`
  override with status/priority columns.
- `migrations/0006_goal_kind.sql` registers `goal` in `kinds`.
- `PRECIS_GOALS_ROOT` env var + `registry.builtins` wiring.
- `precis-goal-help.md` skill + `precis-overview` row.
- MCP config snippet update (README or skill).
- Tests — happy path, readonly guard, embed flag, link CRUD,
  link-check view, index render.

Each slice ships as one commit on `v2` with a CHANGELOG entry.

## File layout — new + changed

New:
- `src/precis/handlers/_file_base.py`
- `src/precis/handlers/_link_check.py`
- `src/precis/handlers/goal.py`
- `src/precis/migrations/0006_goal_kind.sql`
- `src/precis/data/skills/precis-goal-help.md`
- `tests/test_file_base.py`
- `tests/test_link_check.py`
- `tests/test_goal_handler.py`
- `tests/test_file_frontmatter_flags.py`
- `tests/test_link_crud_file_kinds.py`

Changed:
- `src/precis/handlers/markdown.py` — refactor onto base; accept
  `link=` / `unlink=` / `rel=`; honour `readonly` + `embed`.
- `src/precis/handlers/paper.py` — accept `link=` / `unlink=` /
  `rel=` on supported modes.
- `src/precis/handlers/_link_target.py` — any shared validation
  split out so file kinds reuse it.
- `src/precis/utils/md_parse.py` — front-matter extraction (if
  not already).
- `src/precis/registry.py` — `goals_root` parameter + handler
  instantiation.
- `src/precis/cli.py` / config — `PRECIS_GOALS_ROOT` env var.
- `src/precis/data/skills/precis-overview.md` — goal row +
  front-matter flag documentation.
- `src/precis/data/skills/precis-markdown-help.md` — flag
  documentation + `view='check'` recipe.
- `src/precis/data/skills/precis-relations.md` — cross-kind link
  recipes once file kinds accept `link=`.

## Open decisions to settle before slice 4

These deferred from the earlier discussion:

1. **Slicing order** — slices 1 → 2 → 3 → 4 (recommended) vs one
   big PR. Keep them separate; each is reviewable and lands a
   distinct user-visible improvement.
2. **Soft-warning sweep in `view='check'`** — ship it (regex over
   block text for path-like strings) or defer to a follow-up and
   keep the first cut hard-breakage-only. Default: defer the
   sweep; hard breakage has higher signal and no tuning cost.
3. **Goal corpus separation** — own `goals` corpus vs `default`.
   Default: keep in `default`; adding a corpus is cheap later if
   cross-corpus hygiene demands it.
4. **Goal `embed:` default** — `true` (goals are short, embedding
   is cheap, semantic search across goals is valuable) vs
   `ref-only` (read end-to-end, no chunk-search). Recommendation:
   `true` by default; a future-self can set `embed: ref-only` on a
   per-file basis.
5. **Block-level link source positions on numeric refs** —
   already deferred in the link-CRUD pass. Revisit when a real
   consumer needs it; not gating goal delivery.

## Not in scope

- **Auto-mirroring inverse relations.** `cites` doesn't auto-insert
  `cited-by`. Explicit only, as shipped in phase-7.
- **Rich due-date semantics.** `due` in front-matter is a string the
  agent writes and reads; no server-side date filtering until a
  real consumer asks.
- **Multi-file goal projects.** A goal is one file. Multi-file
  projects belong to the queued `book` kind.
- **GUI or web view.** Goals live in the editor + the MCP surface;
  no bespoke viewer.

## Testing strategy

Each slice has its own test file; no slice regresses any previous.
Full run before merge: `pytest -q && ruff check && ruff format
--check && mypy src`.

Integration smoke: after slice 4, check against the live Windsurf
`precis2` MCP with `PRECIS_GOALS_ROOT=/tmp/precis-goals-smoke`, seed
a goal file, verify `get(kind='goal')`, `/toc`, `~block`, `/links`,
`/check` all render cleanly.

## Estimated work

- Slice 1: ~1 session (moderate, mostly test parity with numeric).
- Slice 2: ~0.5 session (small, front-matter work already partly
  done in phase 6a).
- Slice 3: ~1 session (shared helper plus two handlers to plug it
  into).
- Slice 4: ~1 session (`_FileHandlerBase` refactor + goal + skill).

Total: ~3–4 sessions, comparable to phase 6a.
