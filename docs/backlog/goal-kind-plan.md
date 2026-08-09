# Goal Kind Plan — human-written project goals, linkable from anywhere

Status: **queued** — plan captured for a future implementation slice.

A new `goal` kind: **file-backed, slug-addressed, human-written**
project-goal documents parsed into many small embedded chunks, with
formal links into and out of them. Sibling to `markdown` but a
separate kind so it earns its own mental model, list view, skill, and
filesystem root. Pulls in a cluster of related improvements shaped by
the goal use case but applying equally to `markdown`: link
validation, read-only awareness, optional embedding.

## Why a separate kind from `markdown`

Durable project objectives vs scratch notes; written once, revised
occasionally, read often; heavily referenced *from* other refs; the
index should show status/priority/owner/due, not a directory listing;
its own `precis-goal-help` skill; a dedicated `PRECIS_GOALS_ROOT`.

## Prerequisites

Already landed (link-CRUD pass; verified in
`store/_links_ops.py::add_link/links_for` + `handlers/_link_target.py`):
the seeded relation vocabulary and `link=`/`unlink=`/`rel=` kwargs on
numeric-ref handlers with pre-mutation target validation.

Remaining prereq this plan picks up first: **cross-kind
`link=`/`unlink=` on file-backed + paper handlers** (deferred by the
link-CRUD pass); fixing it generically upgrades markdown and paper
too.

## Storage

No new tables — the hub-and-spokes carries goals: `refs` (one row per
goal file, `meta` = front-matter `status`/`priority`/`owner`/`due` +
file fingerprint), `blocks` (one per logical chunk, content-derived
stable slugs via `md_parse.parse_markdown`, embedded), `links`. One
migration registers `goal` in `kinds`. Slug-only addressing
(numeric-id form disallowed): `goal:` index · `goal:<slug>` overview ·
`~<block>` · `/toc` · `/raw` · `/links` · `/check`.

`put` modes mirror `markdown` (`create`/`append`/`replace`/`delete`)
plus `tags=`/`link=`/`unlink=`/`rel=`/`untags=` on every call, so an
agent can append a paragraph and immediately link it to the paper
that motivated it; the inverse direction (memory/todo → a specific
goal chunk) rides the same plumbing.

## Shared improvement #1 — `view='check'` link-health generator

Shared helper (`handlers/_link_check.py`), surfaced first on `goal` +
`markdown`; any handler opts in by declaring `check` in its views
tuple. Categories: **hard breakage** (target ref soft-deleted, target
block gone, file-backed target missing on disk) and **soft warnings**
(regex-detected path-like strings in prose that fail to stat —
advisory only). Paginated generator (`cursor=N`, page 5) so an agent
loop can walk it without context pressure; each entry renders a
concrete `fix:` command; clean state returns
`all links resolve (N total)`.

## Shared improvement #2 — front-matter flags (markdown + goal)

- **`embed: false`** — parsed, ref + blocks stored, no embeddings;
  lexical title search still works; ref-level linking works;
  `embed: ref-only` stores a single synthetic title block (a stable
  handle, no chunking). Use: big files, privacy/cost, not-ready.
- **`readonly`** — three layers: filesystem (`os.access` at
  `_resolve_path` → `BadInput("read-only on disk…")`), front-matter
  (`readonly: true` cached in `meta.readonly` →
  `BadInput("…edit in your editor")`), and convention (skill: prefer
  linking-to over editing-of). Both detected layers render in the
  overview so the agent sees state on every read.

## `_FileHandlerBase` extraction

Adding `goal` is the flagged trigger to factor the base out of
`MarkdownHandler`: base owns path resolution + traversal safety,
atomic writes, lazy re-ingest (mtime → sha256 → re-parse), block
upsert, front-matter flags, the shared `view='check'`. Subclass
provides `kind` + `KindSpec`, parse fn, title derivation, index +
overview renders. Same pattern as `NumericRefHandler`.

## Skill: `precis-goal-help.md`

What a goal is vs `quest`/`todo`/`oracle`/`markdown`; front-matter
shape; heading structure for good chunk handles; "link, don't
allude" (prose paths rot; formal links are FK-protected); discovery
recipes; block-level link recipes both directions; `view='check'`
cadence. Plus a `goal` row in `precis-overview`.

## Phasing (each slice independently shippable)

1. **Cross-kind `link=`/`unlink=` on file + paper** — all four put
   modes, block-level source via `id='slug~block'`; shared validation
   helper; tests mirroring `test_link_crud.py`.
2. **Front-matter flags on markdown** — lands before goal so goal
   inherits working behaviour; no migration (meta is JSONB).
3. **`view='check'`** — shared helper on markdown first.
4. **`goal` kind** — `_FileHandlerBase` refactor (behaviour-neutral),
   `GoalHandler`, the kind migration, `PRECIS_GOALS_ROOT`, skill,
   tests (happy path, readonly guard, embed flag, link CRUD, check
   view, index render). Integration smoke: seed a goal file, verify
   `get(kind='goal')`, `/toc`, `~block`, `/links`, `/check` render.

Estimated ~3–4 sessions total.

## Open decisions to settle before slice 4

1. Slicing order — separate slices (recommended) vs one big PR.
2. Soft-warning sweep in `view='check'` — default: defer; hard
   breakage has higher signal and no tuning cost.
3. Goal corpus separation — default: keep in `default`; a corpus is
   cheap to add later.
4. Goal `embed:` default — recommendation `true` (goals are short,
   semantic search across them is valuable); per-file `ref-only`
   opt-out.
5. Block-level link source positions on numeric refs — already
   deferred; revisit on a real consumer, not gating goal.

## Not in scope

- Auto-mirroring inverse relations (`cites` never auto-inserts
  `cited-by`) — explicit only, as shipped.
- Rich due-date semantics — `due` is a string; no server-side
  filtering until a real consumer asks.
- Multi-file goal projects — a goal is one file; multi-file belongs
  to the queued `book` kind.
- GUI / web view.

## Relationship to `todo-tree-plan.md`

Distinct and compatible: that plan is a dynamic in-DB execution graph
on `todo`; this is a file-backed charter-doc kind. Strategic todos may
`link` to a `goal:<slug>` for narrative context; neither depends on
the other.
