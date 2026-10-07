---
status: ready
title: web graph navigation — neighbourhood panel, graph focus page, paths, trail
pillar: memory-graph
prio: normal
---

# Web graph navigation — how a human walks the graph

Orchestrator proposal, from Reto 2026-10-06: "can we make a web plan for graph navigation", said when graph memory was unparked so file memory can migrate into the graph. Rulings of 2026-10-06 are logged under the decisions; all four open questions are decided.

## Motivation / why

Agents move through the graph with `get(view='links')` and the fisheye ladder. A human has no equivalent: `routes/refs.py::detail` (`/refs/{kind}/{ref_id}`) is a per-kind reader, and the generic link reads are bespoke (`links_for` calls for `serves`/`refines`/`derived-from` in `routes/refs.py`; `routes/papers.py` Sources/Cited tabs read S2 `s2_neighbors`, not typed links). No `/graph`, neighbourhood JSON or path endpoint exists (grepped `src/precis_web/routes`). Once memory nodes live in the graph, a person who cannot walk it cannot audit it.

Reuse, do not rebuild: the agent link read is `store/_links_ops.py::Store.links_for` (`direction`, `relation`); the rel vocabulary is skill `precis-relations` (closed list, auto-mirrored inverses); ring grouping is `utils/refeye.py::RING_GROUPS`/`ring_group`.

## In scope

Slice order; each slice ships alone.

1. **Neighbourhood JSON.** One store-level function (one query over `links` joined to `refs`) returning `{focus, nodes:[{kind,id,label,state}], edges:[{src,dst,rel,dir}], counts:{rel:{kind:n}}}`, with `depth` (1|2), `rels`, `kinds`, `since`/`until`, `trust` and a node cap. Exposed as `GET /graph/<kind>/<id>.json`. Panel, focus page and agent tooling read this one shape. Cost: inverse rows are not all stored (`links_for` rewrites `cited-by`), so the query must apply the same inverse rule; reuse it, do not re-derive. **Seam (2026-10-07):** the store function landed as `store/_links_ops.py::Store.neighbourhood` (memory-recall-walk-keep slice 1a; shape as above plus `truncated`, and `counts2` for the second hop); this slice is now only the `GET /graph/<kind>/<id>.json` route over it. Its `trust` filter duplicates the finding handler's tier predicate (store cannot import handlers); slice 4 should single-source it before exposing `trust` in the browser.
2. **Neighbourhood panel** on the ref page: inbound and outbound links grouped by rel then kind, counts, per-group "expand" (HTMX fragment from slice 1 with a group filter). Browser form of fisheye level 1; headings follow `ring_group`, "Other" for rels in no group. Lives in the shared detail template, not per-kind readers. "Show memories" toggle; default per the memories decision.
3. **Focus page** `GET /graph/<kind>/<id>`, beside the ref reader (not a replacement): 2-hop neighbourhood as **server-rendered SVG**, kinds as node shapes, rels as edge labels, node click = plain link to that node's `/graph/...` (re-focus, no JS). Layout is radial rings (focus centre, hop 1, hop 2) computed in Python. Precedent is server-side SVG (`precis_web/blocktree_svg.py`; `routes/mermaid.py` inlines SVG sanitised by `precis.figure.svg.sanitize_svg`), so no JS graph library and no force layout. Cost: no drag/zoom; acceptable under the node cap; optional pan/zoom script deferred. Fisheye distortion: `fisheye-level2.md` defines none for the browser (it is the `focus` verb and render loop), so this page shows rings by hop and collapses far nodes to count chips. `fisheye-everywhere.md` in-scope 4 (`/eye/<handle>`, the text ladder) owns the handle page; this page is the picture and the two cross-link.
4. **Filters** as query params on panel and page: `kind`, `rel`, `since`/`until` (parsed as `/drive` does, `routes/drive.py::index`), `trust` for findings (`verified|signed|disputed|any`, as in search). The URL is the state; chips reflect it.
5. **Path finding** `GET /graph/path?from=<kind>/<id>&to=<kind>/<id>&depth=4&rels=`: bounded BFS over typed links, shortest first, rendered as a chain of ref cards with the rel on each arrow. "No path within depth N" is an answer, not an error.
6. **Focus trail.** Breadcrumb of recent focus pages as a cookie-held list (no storage). Later slice, not this one's acceptance: persist the trail as a memory ref `derived-from` the visited refs, where this meets the file-memory migration; deferred; the cookie is the slice-6 deliverable (see decisions).

## Cross-links

- Figma mockup: https://www.figma.com/design/2Gd9t9hUkvY7ndMElQJ01u, page "Web graph navigation", frames A/B/C.

## Explicitly NOT in scope

- Editing links from the graph page (read-only).
- 3D, WebGL, force-directed layouts.
- The `focus` verb, render→act loop, fisheye distortion semantics, `/eye/<handle>`: `fisheye-level2.md`, `fisheye-everywhere.md`. Ranking lives in `threads/graph-memory-consumers.md` and `threads/knowledge-mesh.md`, not here.
- Materialised closure, graph DB, cached neighbourhood (computed on read, as fisheye).
- New relations.

## Acceptance criteria

1. Slice 1: for a fixture ref with links in both directions across ≥3 rels, the JSON edge set equals `links_for(direction='both')` row for row (inverse rule included), fetched in one query (query-count test).
2. Slice 2: the panel on `/refs/paper/<id>` lists every row `get(view='links')` shows, grouped by rel then kind, counts correct; a non-paper fixture renders it too.
3. Slice 3: `/graph/<kind>/<id>` for the fixture renders SVG with exactly N nodes (hop ≤2, under cap) and one labelled edge per row; a node past the cap shows a count chip and the response stays under the frame budget.
4. Slice 4: each filter narrows nodes and edges identically on JSON, panel and page; a `since` after every link's creation gives the empty state.
5. Slice 5: between two fixture refs 3 hops apart the path is found at depth 4 and not at depth 2; cycles terminate.
6. Slice 6: after visiting three focus pages the cookie-held trail lists them in order and each crumb re-focuses. Persisting the trail as a memory ref is out of this slice.

## Target + blast radius

New `src/precis_web/routes/graph.py` (registered like the other routers), a neighbourhood function beside `Store.links_for`, a panel partial in the shared ref template, route tests. Read-only, no migration, same auth gate as `/refs`.

## Open questions / decisions log

- **[decided 2026-10-06, Reto]** Focus page sits beside the ref reader; the reader stays the content view and the panel links in.
- **[decided 2026-10-06, Reto]** Caps "as proposed": focus page depth 2, path depth 4, 60 nodes per page, 25 per group before "expand"; hubs with >200 edges collapse to count chips.
- **[decided 2026-10-06, Reto]** Memory refs: "Show memories" toggle stays. Reto: "if mesh is intended to be mainly made from memory memory on, otherwise off." Orchestrator's interpretation (not Reto's wording): default ON when the focused neighbourhood is mainly memory refs (centre node is a memory, or memory is the majority kind among its direct links), otherwise OFF. Hubs with >200 edges still collapse to count chips.
- **[decided 2026-10-06, Reto]** Trail: "cookie is adequate for now". Trail as a persisted memory ref (cross-session; depends on the `memory-native-authoring.md` shape) is a later slice.
