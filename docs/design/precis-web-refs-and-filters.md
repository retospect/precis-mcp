# precis-web: per-kind ref browsers, filters/sort, task tags, PDF diagnostics

- **Status**: implemented
- **Builds on**: ADR 0026 (precis-web surface),
  `docs/design/todo-reparent-via-link.md`

## Problem

Cut 1 of precis-web (ADR 0026) shipped Tasks, Papers, Console, Status.
The operator asked for four follow-ups:

1. Browse the other durable ref kinds (memory, conv, oracle, gripe,
   patent, pres), not just papers.
2. Filters (date window, tag) and sort on those lists.
3. Edit a task's text and add/remove its tags from the tree.
4. The paper PDF viewer reported "No PDF on disk" — looking in the
   wrong place.

## Decisions

### One route module, one tab per kind

The operator chose **a tab per kind** over a single unified view with a
kind selector. To honour that without N near-duplicate templates, there
is exactly one generic route module (`routes/refs.py`) serving
`/refs/{kind}` (list) and `/refs/{kind}/{ref_id}` (detail), and the
top-nav renders one tab per browsable kind. The nav *is* the kind
selector; the implementation stays DRY.

`REF_KINDS` (in `routes/refs.py`) is the browsable set:
`memory, conv, oracle, gripe, patent, pres`. The nav loop in
`base.html.j2` mirrors it. Adding a kind = one entry in each.

Browsable kinds are durable stored refs only. Tool/cache kinds (math,
websearch, web, youtube, …) are deliberately excluded — they don't
list usefully.

### Read-only browse; mutations stay on their tabs

`/refs/{kind}` reads off the DB (`search_refs_lexical` when a query is
present, else `list_refs` with filters + sort + offset pagination).
Detail renders the handler's own `get` output through the in-process
runtime (`dispatch`), so the rendering can't drift from the MCP
surface. No mutation routes on the refs tabs — editing stays on Tasks
or the Console.

URL addressing uses the numeric `ref_id` for both numeric and slug
kinds (avoids slug-escaping in the path). Detail resolves the canonical
address for the `get` call: the slug when the ref has one
(conv/oracle/patent/pres), else the numeric id (memory/gripe).

### Sort: whitelisted `order_by` in the store, not caller SQL

`Store.list_refs` gains an `order_by` parameter resolved against a
class-level whitelist (`_LIST_ORDER_BY`: updated/created/title/id ×
asc/desc). The caller string never reaches the SQL; an unknown key
falls back to `updated_desc` rather than erroring, so a stale bookmark
can't 500. This is the only store change.

### Date filter: presets → `updated_after`

The UI offers `any / 24h / 7d / 30d / 90d`, converted to an
`updated_after` datetime passed to `list_refs`. While a text query is
active, ranking is by relevance and date/sort are inert (shown with a
banner) — search and browse are different orderings.

### Task tags: yes. Task text: deferred (needs a verb).

Numeric refs (todo) are **create-only** — `put(id=N)` is explicitly
rejected and there is no `edit` verb on `TodoHandler`. So:

- **Tags** (add/remove) ride the existing `tag` verb. New route
  `POST /tasks/{id}/tags` dispatches `tag(add=[...]/remove=[...])`;
  the dashboard shows removable chips (excluding `STATUS:`/`level:`,
  which have dedicated controls) plus an add input. Validation stays
  single-sourced in the handler.
- **Text editing** is **not** implemented. It would require a new
  backend capability: a store title-update plus a card-chunk
  re-synthesis (per AGENTS.md, body/card chunks can't be mutated in
  place) and likely a new verb or an `edit` surface on numeric refs.
  That's a substantive change deserving its own design pass + ADR; it
  is flagged, not bolted on.

### PDF path: a config bug, made self-diagnosing

The resolver builds `<corpus_dir>/<letter>/<cite_key>.pdf`. The
operator's file (`/opt/nas/botshome/papers/corpus/a/azrak18.pdf`)
matches that layout exactly — the bug was `corpus_dir` defaulting to
`~/work/corpus` because `PRECIS_CORPUS_DIR` was unset for the web
process (the file lives on an NFS mount only present on the cluster
host). The fix is operational (set the env var), but the surface is now
self-diagnosing:

- The `/papers/{id}/pdf` 404 names the resolved path and `corpus_dir`
  and points at `PRECIS_CORPUS_DIR`.
- The detail page distinguishes a *stub* (no `pdf_sha256` — queued for
  fetch) from *held-but-missing* (file expected, not found) and shows
  where it looked.
- The Status tab shows the active `corpus_dir`.

## Tests

`tests/precis_web/test_routes.py` — refs list/search/unknown-kind,
detail dispatch by id and by slug, wrong-kind 404, nav tabs present,
task tag add/remove/no-op, PDF error names the path, detail shows the
lookup path. `tests/test_store.py` — `order_by` whitelist ordering and
the garbage-key fallback (no SQL injection, no 500).

## Out of scope / follow-ups

- Task text editing (own design + ADR).
- Faceted/multi-tag filters and free-text date ranges (presets only
  for now).
- Per-kind bespoke detail layouts (all kinds share the `get`-render
  detail today).
