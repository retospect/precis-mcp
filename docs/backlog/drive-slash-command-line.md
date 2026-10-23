---
status: draft
pillar: platform
---
# Drive slash command line

Reto, 2026-10-12 (from gr480070): typing `/` in the /drive search box (and the
global loupe in `base.html.j2`, which submits to /drive) opens a popdown that
narrows as you type, `/d` → drive, drafts, datasheets…; then grows into
expressions like `/draft:nanobuds` → its papers, "papers this draft cites that
are stubs and need downloading", `/pathway:no->nh3/pareto`. Discoverability is
the popdown itself: every stage completes from what exists.

## Design rule: the address grammar, not a new language

An expression is the precis address agents already use (`kind:id~selector`,
`view=`, path ids like `/recent`), so one language serves both:

- `/draft:nanobuds` = `get(kind='draft', id='nanobuds')`
- `/draft:nanobuds/fisheye` = the same with `view='fisheye'`
- `/pathway:no->nh3/pareto` — `no->nh3` is a query, not a slug: try id first,
  fall back to search, and show which happened (`kind:?<query>` forces search).

Completions come from the live registry (kinds, each kind's views, ids via
search), so the palette cannot drift from the surface. The expression is the
URL, so every result is bookmarkable and shareable.

## Stages (pipe-separated)

`/draft:nanobuds | cites paper | is:stub`

| Stage | Examples | Source of truth |
|---|---|---|
| address | `draft:nanobuds`, `paper:wang2020state~40` | address grammar (`precis-addressing-help`) |
| traverse | `cites paper`, `part-of`, `refines` | link relations (`precis-relations`) |
| filter | `is:stub`, `has:body`, `tag:PRIO:high`, `since:7d` | GitHub-style qualifiers |
| view | `/recent`, `/pareto`, `/toc` | the kind's view roster |

`|` rather than space: ids and queries contain spaces. The popdown inserts the
pipe, so it is never typed blind.

`is:stub` / `has:body` replace the asked-for `/papers:withchunks` /
`:withoutchunks` and work on any chunked kind. `is:stub` counts
`body:partial` and `body:needs_ocr` papers as stubs (ingest already treats
them as no usable body).

## Actions stay out of the grammar

The language is read-only. Spending/writing operations ("fetch these 12
stubs") are buttons on the result list with a cost line and confirmation, so a
shared or bookmarked URL can never trigger a download or a paid fetcher.

## Shape

One server-side parser → stage list → executed by existing handlers (no new
query engine). The popdown calls a completion endpoint with the text and
cursor position. Reuse the Drive kind/filter picker, chips and URL facets
(`src/precis_web/__init__.py`, `drive-filter-hierarchy.md`) rather than a
second picker. A later MCP form (`search(q='| …')`) can share the parser.

## Slices

1. Palette: `/` + kinds, ids, views; navigation only. Mostly shipped
   (`GET /drive/complete` gr486407, the leading-`/kind` peel
   `routes/drive.py::_split_kind_tokens` with `/paper+chunks` gr498598, the
   Tab-complete in `base.html.j2` gr500642).
2. Qualifiers: `is:stub`, `has:body`, `tag:`, `since:`, plus the `//`
   literal-search escape. **Landed 2026-10-21** (`routes/drive.py::
   _peel_qualifiers`/`_qualifier_completions`; surviving rationale is in
   `precis_web/__init__.py`'s module docstring and the function
   docstrings — nothing left to fold).
3. Pipes: traverse stages over link relations.
4. Result-list actions, first: fetch stubs, with cost line.

Remaining open work is slices 3 and 4 (both still need their own
ready-check before a fixer branch can pick them up — see §Decided for the
one call already made for slice 3).

## Decided

- Traversal (slice 3) uses relation slugs verbatim from the live
  `relations` table, inverses included (`cites`/`cited-by`,
  `contains`/`part-of`); direction is the slug's. No alias table: none
  exists in code and none is needed. Friendlier names, if wanted, are a
  separate item.
