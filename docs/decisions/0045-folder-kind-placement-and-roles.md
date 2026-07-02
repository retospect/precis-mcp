# ADR 0045 — the `folder` kind: placement, kind roles, and spanning search

- **Status**: accepted (2026-07-02)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0027 — reparent via the reserved `parent` link relation
  - migration `0013_todo_tree.sql` — `refs.parent_id`
  - ADR 0041/0042/0043 — cad / pcb / structure (the artifact kinds
    that motivated a shared organizational layer)
  - `KindSpec.corpus_role` (cfp work) — the precedent for declarative
    per-kind policy

## Context

The corpus now holds several *authored* artifact families — drafts,
atomistic `structure` designs, `cad` designs, project todo trees, and
(soon) `pcb` — each with its own intrinsic hierarchy: structures form
`derived-from` ancestry trees, todos form a scheduling tree on
`parent_id`, drafts belong to workspaces. There is no extrinsic
"where do I keep this?" axis: nothing like a Drive/folder surface, no
way to gather a draft, two structures, and a project under one
heading, and no spanning "everything about X" scope for search.

Three observations settled the design discussion:

1. **Derivation and placement are orthogonal.** Per-kind derivation
   links carry semantics (run-cube cache reuse for structures;
   rotation / failure-bubbles for todos) and must not be unified.
   Placement is dumb and uniform — a ref sits in at most one folder.
2. **Folders organize things you *make*; search organizes things you
   *collect*.** The ~2,700 papers never go in folders — they already
   have clusters / TOC / tags / semantic search. Stream kinds
   (memory, alert, agentlog, job, news) never go in folders either —
   they arrive at machine rate and have their own reviewers
   (nursery / structural / deep_review). Foldering them would
   recreate the feed problem.
3. **Tags find; folders account.** Tag stamping is best-effort and
   skippable; a column is structural. Only containment can answer
   "have I seen everything under X?" — the completeness invariant.
   Containers can also carry cascading scope (the
   `meta.workspace.brief` precedent; the proprietary-content →
   local-models routing backlog item wants a *scope*, not per-row
   tags). And LLM agents orienteer: a folder listing is a bounded,
   complete context unit, where a tag query has uncertain recall.

## Decision

### 1. `kind='folder'` — a numeric ref, no new tables

A folder is a plain numeric ref (`title` = name, `meta` free). Its
children are the live refs whose `parent_id` points at it — the same
column the todo tree uses (migration 0013 put `parent_id` on **all**
refs; only todo used it until now). Subtree reads are a recursive CTE
over the indexed column; a move is one column write.

Containment is **single-parent** (Google Drive retreated from
multi-parent placement in 2020 for the same reasons: sync ambiguity,
"where is it really?"). Cross-cutting membership is what tags are for.

### 2. Placement generalizes the ADR 0027 `parent` façade

The move surface is the reserved virtual relation from ADR 0027,
now shared:

```
link(kind='draft',     id=N,      target='folder:7', rel='parent')
link(kind='structure', id='slug', target='folder:7', rel='parent')
link(kind='folder',    id=8,      target='folder:7', rel='parent')  # nest
link(kind=K,           id=…,      rel='parent', mode='remove')      # → Unfiled
```

`parent` stays out of the `Relation` vocabulary and the `relations`
table (ADR 0027's rationale holds). Each placeable handler intercepts
it and routes to a shared `handlers/_placement.py` helper that runs
the kind-agnostic guards (`check_no_cycle`, parent-exists) and calls
`Store.set_parent`. The todo-specific guards (owner-only, depth
gradient, todo-parent) stay in `TodoHandler._reparent`, which now
*also* accepts a folder target for placement.

Non-folder parents remain invalid for non-todo kinds: a draft's
parent must be a folder; a todo's parent must be a todo **or** a
folder (see §4).

### 3. Kind roles — declarative on `KindSpec`

A new field alongside `corpus_role`:

```python
role: Literal["artifact", "corpus", "stream", "system"] = "stream"
```

- **artifact** — authored things (draft, structure, cad, folder,
  todo, conv-as-authored-thread). Placeable in folders; first-class
  in the one-list / spanning search.
- **corpus** — collected/ingested sources (paper, cfp, patent, pres,
  wikipedia, web caches). Not placeable; searchable by default.
- **stream** — machine-emitted telemetry and notes-at-rate (memory,
  alert, agentlog, job, news, finding, gripe). Not placeable;
  excluded from default spanning scope, included on explicit opt-in.
- **system** — infrastructure kinds (skill, tag, cron, oracle,
  random, math, calc). Neither placeable nor listed.

The default is `stream` — the safe failure mode: a new kind stays
out of folders and default listings until deliberately promoted.
Role drives three behaviours: (a) whether `rel='parent'` placement
is legal, (b) inclusion in the default spanning-search / one-list
scope, (c) web Drive visibility.

Stream content reaches folders only by **promotion**: distill the
memory/dream into an authored note (draft / markdown) and place
that. Folders may *surface* stream kinds as facets (e.g. a
"memories mentioning things in here" panel), never contain them.

### 4. Todo roots may sit in folders

Root detection for the todo tree becomes kind-aware: a todo is a
strategic root iff `parent_id IS NULL` **or** its parent is a
`folder`. Rotation, doable, projects view, and the dispatch walk all
use one shared SQL fragment ("a root todo's parent is not a todo")
so the predicate cannot drift. Depth accounting stays todo-scoped:
the upward depth walk stops at the first non-todo ancestor, so
folder levels never consume the `MAX_DEPTH` budget. This keeps
folder = *where*, project = *what/why/when*: placing a project
root in a folder changes nothing about scheduling.

### 5. Unfiled is virtual; delete refuses non-empty

- **No seeded root.** Folders with `parent_id IS NULL` are top-level.
  Artifacts with no parent are **Unfiled** — a virtual bucket the
  Drive view and one-list render, preserving everything-is-somewhere
  without creation-time friction. (Todos are exempt: an unfoldered
  todo root is normal, not "unfiled".)
- **Delete** soft-deletes the folder ref and is **refused while the
  folder has live children** — children first move out (or the
  caller detaches them). No recursive delete: folders are cheap;
  their contents are not. (The `ON DELETE SET NULL` FK still covers
  hard-delete hygiene at the SQL layer.)

### 6. Spanning search gains a `folder=` scope

The existing cross-kind fan-out (`supports_search_hits`,
`kind='*'`) is the spanning surface; this ADR adds a `folder=`
parameter: resolve the folder's live subtree via the recursive CTE,
then filter any search mode (lexical, tag-list, semantic) to those
ref ids. The one-list is the same surface with an empty query —
browse and search converge.

### 7. Discipline (policy, not code)

Folders stay **cheap and shallow**: 1–2 levels, artifact kinds only,
no deep taxonomy tooling, no auto-foldering pass. The reviewers stay
on memory; no reviewer files things into folders. The skill
(`precis-folder-help`) teaches: tags find, folders account; if a
folder needs a third level, it probably wants to be a project.

## Consequences

- CAD/PCB integration lands into an organized world: declaring
  `role="artifact"` on the KindSpec buys placement, Drive
  visibility, and spanning search with no further wiring.
- `check_parent_exists` gets an `allowed_kinds` parameter; the
  cycle/depth walks were already kind-agnostic.
- The 0013 `parent_id` partial index already covers all kinds (it
  is not kind-predicated) — no index change.
- One tiny migration (`0048_folder_kind.sql`) seeds the `kinds` row
  for fresh DBs / the test template — boot's registry auto-upsert
  covers running prod, but `insert_ref` validates against the table
  before any boot has run. No new tables, no new columns.
- `role` does **not** change the default `kind='*'` search scope in
  this ADR — the existing fan-out set (`supports_search_hits`)
  stays as-is so `memory` et al. keep appearing in default cross-kind
  search. Scoping the *one-list web view* by role is where the
  artifact+corpus default applies; revisiting the MCP search default
  is an explicit follow-up, not a silent side effect.
- The web gains a Drive tab (folder tree + listing + breadcrumbs +
  move + Unfiled). Per-kind readers stay canonical; Drive deep-links.
- PCB's derivation-model question (board-level simulation lineage vs
  snapshots) stays open and *independent* — that is the point of
  keeping derivation and placement orthogonal.

## Alternatives considered

1. **Tags only, no folders.** Rejected: tags cannot provide the
   completeness invariant, carry cascading scope, or give an agent a
   bounded orientation listing; machine-rate tag stamping makes tag
   hygiene itself fragile.
2. **A `collection` links-based membership (multi-parent).**
   Rejected: duplicates the ADR 0027 lesson — hierarchy state split
   between a column and link rows needs sync; multi-parent placement
   reintroduces "where is it really?" (Drive's 2020 retreat).
3. **Materialized-path tags (`path:/a/b/c`).** Rejected: a rename or
   move becomes a subtree re-stamp; the recursive CTE over an
   indexed column is microseconds at this corpus size (~14k refs).
4. **Foldering the corpus and stream kinds too.** Rejected: papers
   already have a working discovery layer; stream kinds have their
   own reviewers and arrival rate — foldering them recreates the
   nursery-digest feed problem (>2,000 near-dup memories/day, once).
5. **A separate `folders` table.** Rejected: a folder needs nothing
   a ref doesn't already have (title, meta, soft-delete, tags,
   links, parent_id).
