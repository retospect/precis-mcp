---
status: draft
title: Sources tab shows [N] marker badges and merges parsed-bib rows S2 doesn't have
model: sonnet
blocked-by: citation-bib-parse
---

# Sources tab — `[N]` badges + the merged (S2 ∪ parsed-bib) view

Consumer slice of the citation-resolution work; requires
`citation-bib-parse.md`'s `paper_bib_entries` table.

## Motivation / why

The Sources tab today renders only what `s2_neighbors` holds: no marker
numbers (a row can't be traced back to its `[34]` in the text), and holes
wherever a publisher gave S2 a narrow reference list. With
`paper_bib_entries` populated, both fix in the read path. Decided with Reto:
the tab becomes "our merged view", not "S2's view".

## In scope

- **New store accessor** (owned by this slice, not the base):
  `store.list_bib_entries(ref_id)` alongside `list_s2_neighbors` in
  `src/precis/store/_links_ops.py` — `papers.py` has no raw SQL and stays
  that way.
- `_sources_rows()` (`src/precis_web/routes/papers.py`) joins
  `paper_bib_entries` on `held_ref_id / doi / s2_id` (first match wins, in
  that order). **The real marker replaces the positional index**: today
  every row gets a positional `n` (`enumerate(neighbors, start=1)`,
  rendered `{{ row.n }}.`); a matched row's `n` becomes its bibliography
  marker, rendered bracket-styled (**`[34]`**) to distinguish it from the
  positional `34.` style, which remains for unmatched rows.
- Parsed entries with **no S2 row are unioned in** as first-class rows —
  bracket marker badge, **`raw_text` rendered verbatim as the display
  line** (it *is* the citation line; no composed format — a new template
  branch, since `_refs_row.html.j2` today only displays `row.title`), DOI
  link when matched, and the existing fetch button when a DOI is present.
- Row ordering, all three existing categories: rows with real markers sort
  by marker number first; unmarked S2 rows keep S2 order after them;
  unmarked held-but-not-in-S2 rows keep today's placement (appended last).
  A held row matched via `held_ref_id` joins the marker-sorted bucket.

## Explicitly NOT in scope

- No writes — read-path only; `s2_neighbors` schema and TTL refresh
  untouched.
- No change to the Cited tab (`_cited_rows`) — inbound citations have no
  marker semantics in this paper's bibliography.
- No in-prose marker hyperlinks/popovers in the reader — that's UI work for
  later, and depends on `citation-taproot-resolve.md`'s `chunk_citations`.

## Acceptance criteria

1. `/papers/carbon20` Sources rows show bracket `[N]` badges for entries
   matched to S2/held rows (replacing their positional index); at least one
   parsed entry absent from `s2_neighbors` renders as a new row with its
   marker, verbatim `raw_text` line, DOI link, and fetch button.
2. A paper with **no** `paper_bib_entries` rows renders the tab exactly as
   today — positional `n.` badges and all (byte-identical fragment in the
   test fixture); no regression. Covered by existing + new SQL-level tests
   in `tests/precis_web/`.
3. Rows are ordered as specified (marker bucket → S2-order → remaining
   held); dedup: an entry matched to a held/S2 row never *also* appears as
   a union row.
4. Gate green; state-map's web-UI section updated in the same ship.

## Target + blast radius

- **Web**: `src/precis_web/routes/papers.py` (`_sources_rows`, refs
  panel route), `templates/papers/_refs_row.html.j2` /
  `_refs_panel.html.j2`.
- **Store layer**: one new read accessor `list_bib_entries` in
  `src/precis/store/_links_ops.py` (this slice owns it — the base slice's
  worker writes the table but exposes no read API).
- **Untouched**: migrations, workers, taproot, `s2_neighbors`.

## Open questions / decisions log

Decided 2026-08-06: split out of `citation-resolution.md` per `ready`'s
split recommendation; merged-view semantics approved by Reto. Sonnet-tier
per `ready`'s sizing note (route + template consuming an existing table).

Round-1 findings below resolved 2026-08-06:

- Positional-badge baseline (blocker): real markers **replace** the
  positional `n` on matched rows, bracket-styled to distinguish; unmatched
  rows and no-bib-entries papers keep today's positional `n.` rendering
  unchanged. AC 2 now pins byte-identical output for the no-rows case. ✓
- Store accessor ownership (blocker): this slice owns
  `store.list_bib_entries` in `_links_ops.py`; declared in scope + blast
  radius. ✓
- Ordering of the third row category (advisory): specified — unmatched
  remaining-held rows keep today's appended-last placement. ✓
- Union-row display line (advisory): `raw_text` verbatim, no composed
  format; acknowledged as a new template branch. ✓
- Badge style (advisory): brackets `[34]` for real markers is intentional —
  it visually separates true bibliography markers from positional
  indices. ✓

## ready agent findings (2026-08-06)

- blocker: AC#2 ("A paper with no `paper_bib_entries` rows renders the tab
  exactly as today ... no badges") misdescribes the current baseline. In
  `_sources_rows()` (`src/precis_web/routes/papers.py:792`,
  `for i, nb in enumerate(neighbors, start=1)`), every S2-neighbor row
  already gets a non-null `n` today, and `_refs_row.html.j2:11` already
  renders it (`{{ row.n }}.`) — so "today" already shows a numeric badge on
  nearly every Sources row, just a positional-order one, not a real
  bibliography marker. As written, AC#2 is unverifiable/false against the
  current template, and satisfying it literally would require suppressing
  today's positional-index badge for the no-`paper_bib_entries` case — an
  undeclared change not named in "In scope" or "Target + blast radius." The
  spec needs to state explicitly whether the existing positional `n` is kept,
  replaced, or hidden once `paper_bib_entries` is authoritative.
- blocker: "Target + blast radius" claims web-only scope (routes + two
  templates) and lists `s2_neighbors` as untouched, but is silent on the
  store layer. Every existing DB read in `_sources_rows`/`_cited_rows` goes
  through a `store.*` accessor (`store.links_for`, `store.list_s2_neighbors`
  — defined in `src/precis/store/_links_ops.py` — `store.fetch_refs_by_ids`);
  `papers.py` contains no direct SQL. Joining on `paper_bib_entries` will
  need an equivalent new store accessor, which isn't declared here nor in
  `citation-bib-parse.md`'s own "Target + blast radius" (which lists "all web
  routes" as untouched and names no store-layer method either). Neither
  sibling proposal owns adding this accessor — a real gap between the two,
  not just an omission in this one.
- advisory: the "Row ordering" bullet ("rows with markers sort by marker
  number; unmarked S2-only rows keep S2 order after them") only names two of
  the three row categories `_sources_rows` currently produces. The third —
  held-but-not-in-`s2_neighbors` rows (the `remaining` loop,
  `papers.py:810-822`, today rendered `n=None`, sorted by ref id, appended
  last) — isn't placed by the ordering rule. If one of these matches
  `paper_bib_entries` via `held_ref_id` it presumably joins the marker-sorted
  bucket, but if it doesn't, where it sorts relative to "unmarked S2-only"
  rows is unstated.
- advisory: "parsed author/journal/year text as the display line" for
  union-only rows requires a new render branch in `_refs_row.html.j2` — the
  template only ever displays `row.title` (falling back to "(untitled)"),
  and `paper_bib_entries` (per `citation-bib-parse.md`) has no `title`
  column, only `authors`/`journal`/`year`/`volume`/`first_page`/`raw_text`.
  The spec names the three fields to show but not the composed format (built
  from the discrete fields, vs. rendering `raw_text` verbatim) — worth
  pinning down since it's a new template branch, not reuse of an existing
  one as the surrounding prose ("the existing fetch button ... already
  renders fetch for DOI-bearing rows") implies for the rest of the row.
- advisory: prose says "[34] marker badge" (bracket notation, matching the
  in-text citation style); the existing badge markup
  (`_refs_row.html.j2:11`) renders `{{ row.n }}.` — a trailing period, no
  brackets. Cosmetic only, but worth confirming intent before build (new
  bracket styling vs. reuse of the existing marker span).

## ready agent findings (2026-08-06, round 2)

round 2: no blockers, gate-clean. Verified against current code:
`_sources_rows()` (`src/precis_web/routes/papers.py:776-823`) still assigns
positional `n` via `enumerate(neighbors, start=1)` for every S2 row and
`n=None` for the `remaining` (held-not-in-S2) loop, matching AC2's "renders
exactly as today when no `paper_bib_entries` rows exist" baseline;
`_refs_row.html.j2:11`'s `{{ row.n }}.` (trailing-period, no brackets) is
still the only existing badge markup, so "bracket-styled to distinguish from
positional" is a real, well-scoped new branch, not a claim about existing
markup. `store.list_s2_neighbors` (`src/precis/store/_links_ops.py:554`)
confirms the accessor pattern (`store.*` in `_links_ops.py`, no raw SQL in
`papers.py`) the new `list_bib_entries` is specified to follow. Round-1's two
blockers read as genuinely resolved: the positional-vs-marker rendering rule
is now explicit and AC2 pins a byte-identical no-op case; the store accessor
now has a named, single owner (this slice) declared in both In scope and
Target + blast radius, with no competing claim from either sibling
(`citation-bib-parse.md` and `citation-taproot-resolve.md` neither reads nor
writes a `list_bib_entries`-shaped accessor). `docs/proposals/
citation-bib-parse.md`'s added `id` serial PK (noted in the task) doesn't
affect this slice — the sources-tab join keys off `held_ref_id`/`doi`/`s2_id`
only, and the PK is consumed exclusively by the taproot sibling's
`chunk_citations.bib_entry_id` FK. `blocked-by: citation-bib-parse` still
resolves to an existing file. No contradictions found between this file's
sections, against `_sources_rows`/`_cited_rows`/`_refs_row.html.j2`/
`_refs_panel.html.j2` as they exist today, or against the two sibling
proposals in `docs/proposals/`. `tests/precis_web/test_papers_refs.py`
exists, supporting AC2's "existing + new SQL-level tests" claim as
buildable. `model: sonnet` still fits (route + template consuming an
existing table, no new abstraction). No split signal — this remains one
coherent, independently shippable deliverable.
