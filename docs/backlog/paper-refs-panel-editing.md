---
status: draft
title: Editable Sources/Cited panel on the paper page (DOI paste + search-to-attach)
---

# Editable Sources/Cited panel on the paper page (DOI paste + search-to-attach)

## Motivation / why
Reto (2026-08-28, reviewing /papers/solid90): the paper page's Sources and
Cited tabs are read-only — held `cites` links unioned with S2 neighbors
(`routes/papers.py::refs_panel`, `_sources_rows`/`_cited_rows`). When the
bibliography is wrong or incomplete there is no web door to fix it: no way
to attach a known source by DOI, find one by search, or detach a bad row.

## In scope
- Add-source affordance on the Sources tab: paste a DOI (resolve →
  held paper if we hold it, else create/ingest a stub) or search held
  papers by title/author (reuse the existing search surface) → creates a
  held `cites` link from this paper.
- Remove affordance per held-link row (delete the `cites` link; S2-derived
  rows are not deletable — they re-derive).
- Provenance: mark web-added links (link meta `by`/`at`, mirroring the
  meta-review "Reviewed ✓ … by web:reto" convention).

## Explicitly NOT in scope
- Editing the *Cited* (incoming) direction — an in-cite is another paper's
  out-link; editing it belongs on that paper.
- Full ingest-on-paste of the referenced paper's PDF (a stub ref +
  needs-acquisition flow is enough; acquisition stays the fetch queue's
  job).
- Suppressing/curating S2 neighbor rows.

## Acceptance criteria
- On /papers/<slug> Sources tab: pasting a valid DOI adds a row (held or
  stub) linked via a held `cites` edge; the panel re-renders showing it.
- Search-to-attach finds a held paper by title fragment and attaches it.
- A held-link row shows a remove control; removing it deletes the link and
  the row disappears; S2-only rows show no remove control.
- Any outbound DOI/metadata resolution goes through `safe_get`
  (`utils/safe_fetch.py`) — the DOI is agent/user-supplied input.
- Duplicate adds (DOI already linked) are a no-op with a visible notice.

## Target + blast radius
`src/precis_web/routes/papers.py` (refs_panel + new POST routes),
`templates/papers/_refs_panel.html.j2` / `_refs_row.html.j2`,
store link ops (`links_for`, add/delete link), DOI resolution helper
(crossref/habanero — same stack paper ingest uses).

## Open questions / decisions log
- Stub creation path: reuse the existing paper-stub mint (fetch-queue
  `put`/acquire pins, commit 6da71e5d) or a minimal refs row?
- Where does search-to-attach query — `search(kind='paper', title=…)`
  equivalent route, or a new lightweight endpoint?
