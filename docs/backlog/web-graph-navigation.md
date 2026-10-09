---
status: ready
title: web graph navigation — neighbourhood panel, graph focus page, paths, trail
pillar: memory-graph
prio: normal
---

# Web graph navigation — how a human walks the graph

Orchestrator proposal, from Reto 2026-10-06: "can we make a web plan for graph navigation", said when graph memory was unparked so file memory can migrate into the graph. Rulings of 2026-10-06 are logged under the decisions; all four open questions are decided.

## Motivation / why

Agents move through the graph with `get(view='links')` and the fisheye ladder. A human has no equivalent: `routes/refs.py::detail` (`/refs/{kind}/{ref_id}`) is a per-kind reader, and the generic link reads are bespoke (`links_for` calls for `serves`/`refines`/`derived-from` in `routes/refs.py`; `routes/papers.py` Sources/Cited tabs read S2 `s2_neighbors`, not typed links). Only the slice-1 neighbourhood JSON exists under `/graph`; no panel, focus page or path endpoint yet. Once memory nodes live in the graph, a person who cannot walk it cannot audit it.

Reuse, do not rebuild: the agent link read is `store/_links_ops.py::Store.links_for` (`direction`, `relation`); the rel vocabulary is skill `precis-relations` (closed list, auto-mirrored inverses); ring grouping is `utils/refeye.py::RING_GROUPS`/`ring_group`.

## In scope

Slice order; each slice ships alone. Slice 1 shipped (kept below as the seam the rest build on).

1. **Neighbourhood JSON.** Shipped: `GET /graph/<kind>/<id>.json` (`precis_web/routes/graph.py`) over `store/_links_ops.py::Store.neighbourhood` (`depth`, `rels`, `kinds`, `since`/`until`, `cap` ≤200, default 60), plus `groups` (hop-1 rels under `ring_group` headings, "Other" for the rest). Not exposed: `trust` — the store's tier predicate duplicates `handlers.finding._passes_trust` (store cannot import handlers); slice 4 single-sources it before the browser sees it. Still open from the liaison contract td470556 (2026-10-06, drive-ux; spec text lived only on the dropped `work/graph-memory/r17-gate-repair` branch), to take as later slices need: `link_id` + chunk endpoint ids per edge, `dir=cross` for depth-2 edges between non-focus nodes, retired endpoints as tombstones, `meta.native_targets` (typed id → handle/selector for a follow-up `get`), per-group `{total,returned,next}` continuation windows under the 25/group cap. The todo is the durable copy.
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

1. Slice 1: shipped — `tests/precis_web/test_graph_json.py` pins links_for parity (inverse rule included), `groups`, filters and the round-trip count.
2. Slice 2: the panel on `/refs/paper/<id>` lists every row `get(view='links')` shows, grouped by rel then kind, counts correct; a non-paper fixture renders it too.
3. Slice 3: `/graph/<kind>/<id>` for the fixture renders SVG with exactly N nodes (hop ≤2, under cap) and one labelled edge per row; a node past the cap shows a count chip and the response stays under the frame budget.
4. Slice 4: each filter narrows nodes and edges identically on JSON, panel and page; a `since` after every link's creation gives the empty state.
5. Slice 5: between two fixture refs 3 hops apart the path is found at depth 4 and not at depth 2; cycles terminate.
6. Slice 6: after visiting three focus pages the cookie-held trail lists them in order and each crumb re-focuses. Persisting the trail as a memory ref is out of this slice.

## Target + blast radius

`src/precis_web/routes/graph.py` (exists; slices 3–6 add routes there), a panel partial in the shared ref template, route tests. Read-only, no migration, same auth gate as `/refs`.

## Open questions / decisions log

- **[decided 2026-10-06, Reto]** Focus page sits beside the ref reader; the reader stays the content view and the panel links in.
- **[decided 2026-10-06, Reto]** Caps "as proposed": focus page depth 2, path depth 4, 60 nodes per page, 25 per group before "expand"; hubs with >200 edges collapse to count chips.
- **[decided 2026-10-06, Reto]** Memory refs: "Show memories" toggle stays. Reto: "if mesh is intended to be mainly made from memory memory on, otherwise off." Orchestrator's interpretation (not Reto's wording): default ON when the focused neighbourhood is mainly memory refs (centre node is a memory, or memory is the majority kind among its direct links), otherwise OFF. Hubs with >200 edges still collapse to count chips.
- **[decided 2026-10-06, Reto]** Trail: "cookie is adequate for now". Trail as a persisted memory ref (cross-session; depends on the `memory-native-authoring.md` shape) is a later slice.
