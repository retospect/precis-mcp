---
status: ready
title: web graph navigation — neighbourhood panel, graph focus page, paths, trail
pillar: memory-graph
prio: normal
---

# Web graph navigation — how a human walks the graph

Orchestrator proposal, from Reto 2026-10-06: "can we make a web plan for graph navigation", said when graph memory was unparked so file memory can migrate into the graph. Rulings of 2026-10-06 are logged under the decisions; all four open questions are decided.

## Motivation / why

Agents move through the graph with `get(view='links')` and the fisheye ladder. A human has no equivalent: `routes/refs.py::detail` (`/refs/{kind}/{ref_id}`) is a per-kind reader, and the generic link reads are bespoke (`links_for` calls for `serves`/`refines`/`derived-from` in `routes/refs.py`; `routes/papers.py` Sources/Cited tabs read S2 `s2_neighbors`, not typed links). Under `/graph` now: the slice-1 neighbourhood JSON, the slice-2 panel fragment on the ref page, the slice-3 radial-SVG focus page, the slice-4 shared filters the slice-5 path page and the slice-6 cookie trail. Once memory nodes live in the graph, a person who cannot walk it cannot audit it.

Reuse, do not rebuild: the agent link read is `store/_links_ops.py::Store.links_for` (`direction`, `relation`); the rel vocabulary is skill `precis-relations` (closed list, auto-mirrored inverses); ring grouping is `utils/refeye.py::RING_GROUPS`/`ring_group`.

## In scope

Slice order; each slice ships alone. Slices 1–7 shipped (kept below as the seam the rest build on).

1. **Neighbourhood JSON.** Shipped: `GET /graph/<kind>/<id>.json` (`precis_web/routes/graph.py`) over `store/_links_ops.py::Store.neighbourhood` (`depth`, `rels`, `kinds`, `since`/`until`, `cap` ≤200, default 60), plus `groups` (hop-1 rels under `ring_group` headings, "Other" for the rest). Still open from the liaison contract td470556 (2026-10-06, drive-ux; spec text lived only on the dropped `work/graph-memory/r17-gate-repair` branch), to take as later slices need: `link_id` + chunk endpoint ids per edge, `dir=cross` for depth-2 edges between non-focus nodes, retired endpoints as tombstones, `meta.native_targets` (typed id → handle/selector for a follow-up `get`), per-group `{total,returned,next}` continuation windows under the 25/group cap. The todo is the durable copy.
2. **Neighbourhood panel.** Shipped: `GET /graph/<kind>/<id>/panel` (HTMX fragment in `precis_web/routes/graph.py`), mounted on `templates/refs/detail.html.j2` and `papers/detail.html.j2` via `templates/graph/_panel_mount.html.j2`. Heading → rel → kind with counts over `ring_group` headings, 25 rows per group before "show all" (`?group=<heading>`), "Show memories" radio posting `?memories=0|1` (absent = the spec default: centre is memory or memory is the strict majority kind), each row linking its reader plus a ◦ re-focus link.
3. **Focus page.** Shipped: `GET /graph/<kind>/<id>` beside the `.json` route, 2-hop neighbourhood as server-rendered radial SVG (`precis_web/graph_svg.py::focus_graph_svg`), kinds as node shapes, rels as edge labels, node click = plain link to that node's `/graph/...`. Labels escaped, no `sanitize_svg` (it strips the `<a href>`s the page exists for). Over-cap neighbours collapse to count chips on the ring they would have filled. Cross-links to the reader, `.json` and `/eye/<handle>` both ways.
4. **Filters.** Shipped: the axes `rels`, `kinds` (registry-validated via `graph_filters.parse_kind_filter`, as `/eye`), `since`/`until` (Drive's `_parse_date`), `trust` for findings (`verified|signed|disputed|any`, as in search) and `depth` (page only — the panel is hop-1 by contract) are one `_Filters` parse + one `Store.neighbourhood` call behind `.json`, `/panel` and `/graph/<k>/<id>` (`precis_web/routes/graph.py`). The tier predicate is single-sourced as `precis.nanopub.overview.passes_trust` beside `HubOverviewRow` (the store may not import handlers); the tier list is `store.types.FINDING_TRUST_TIERS`. The URL is the state: the page's plain GET form + removable chips, the panel's HTMX form + chips, filtered empty state on all three.
5. **Path finding.** Shipped: `GET /graph/path?from=&to=&depth=&rels=` over `store/_links_ops.py::Store.link_path` (bidirectional BFS, links walked either way, depth ≤ 4, visited-node budget reported as `budget_hit`), a chain of ref cards with the stored direction on each arrow; the focus page carries a "path to…" form.
6. **Focus trail.** Shipped: every focus page advances a cookie-held trail (`graph_trail`, `kind:id|…`, last 8, `precis_web/routes/graph.py::_advance_trail`), rendered as crumbs plus a "← retreat" link by `templates/graph/_trail.html.j2` (the path page shows it read-only); revisiting a crumb truncates the trail back to it. Persisting the trail as a memory ref `derived-from` the visited refs is a later slice (see decisions).

Added 2026-10-06 from Reto's "conveniently dive into the graph and back out again" (a sibling session's spec, vetted by `ready` the same day and folded in here; its rulings are in the decisions log). Slice 6 gives one way out — *retreat* along the trail; slice 7 the second, zoom-out. These add the pieces that make a dive cheap:

7. **Zoom out.** Shipped: `utils/refeye.py::UP_RELATIONS` (every registered kind listed, disabled ones with their reason) + `resolve_up` (first step with a live far end wins, degree tie-break via `store.count_links_for_refs`, up to 5 alternatives as the chooser), rendered on the focus page as "↑ zoom out" or disabled with the reason. Two departures from the proposed table, from prod data (2026-10-15): evidence documents step `out` (every `establishes`/`corroborates` row is paper/patent → finding), and `memory` → `part-of` out (the 2026-10-08 mesh cutover made the hubs refs). Still open: `/eye/<handle>` reading the same table for its one "up" link (the text-ladder page, `precis_web/routes/eye.py`).
8. **Came-from slot and peek.** The previous focus (handle, label, the rel followed) renders in a fixed slot at the top so a refocus does not reshuffle the picture; ring order is stable across refocus. Hover/keyboard focus on a node shows its label and per-rel counts from one batched `store.link_rel_summary_for_refs(node_ids)` per render, embedded in the page (no per-hover request; counts in stored direction, accepted). Absorbs gr240051's hover-intent concern by construction.
9. **Keyboard map.** `j`/`k` across nodes in ring order, `Enter` refocus, `Backspace`/`u` retreat (slice 6), `U` zoom out (slice 7), `/` search, `1`/`2` depth. Every key targets an anchor or form already on the page, so route tests cover the server side; the key handler is a few lines of alpine (`static/alpine.min.js`, vendored), checked by hand on first deploy.

## Cross-links

- Figma mockup: https://www.figma.com/design/2Gd9t9hUkvY7ndMElQJ01u, page "Web graph navigation", frames A/B/C.

## Explicitly NOT in scope

- Editing links from the graph page (read-only).
- 3D, WebGL, force-directed layouts.
- The `focus` verb, render→act loop, fisheye distortion semantics: `fisheye-level2.md`. `/eye/<handle>` is shipped (`precis_web/routes/eye.py`), not an item. Ranking lives in `threads/graph-memory-consumers.md` and `threads/knowledge-mesh.md`, not here.
- Materialised closure, graph DB, cached neighbourhood (computed on read, as fisheye).
- New relations.

## Acceptance criteria

1. Slice 1: shipped — `tests/precis_web/test_graph_json.py` pins links_for parity (inverse rule included), `groups`, filters and the round-trip count.
2. Slice 2: shipped — `tests/precis_web/test_graph_panel.py` pins the mount (shared detail template + papers reader), the ring-group/rel/kind grouping with counts, the per-group expand past the 25-row cap, the memories toggle default and the 404.
3. Slice 3: shipped — `tests/precis_web/test_graph_focus.py` pins valid SVG with exactly N node links (hop ≤2), one labelled edge per row, count chips past the cap, label escaping, cross-links and the 404/400s.
4. Slice 4: shipped — `tests/precis_web/test_graph_filters.py` pins per-axis narrowing on all three surfaces, JSON ↔ panel ↔ page node/edge parity (depth-2 JSON ↔ page included), the `since`-after-everything empty state, controls + chips, group-expand carrying filters, and the 400s.
5. Slice 5: shipped — `tests/precis_web/test_graph_path.py` pins found at depth 4 / not at depth 2 for refs 3 links apart, cycle termination, stored-direction arrows, `rels` narrowing and the 400/404s.
6. Slice 6: shipped — `tests/precis_web/test_graph_trail.py` pins three visits listed in order with crumbs re-focusing, retreat, crumb truncation, the cap and untrusted-cookie junk dropped.
7. Slice 7: shipped — `tests/test_refeye_up_relations.py` pins totality over the kind registry, registered step relations, live-or-disabled rendering, paper → `corroborates` → fi → `serves` → qu (retreat to the paper, zoom-out to the quest), degree tie-break and the todo `parent` step.
8. Slice 8: a seeded quest fixture with 450 serving papers renders focus, came-from and peek within the frame budget of AC 3; peek counts come from one store call (call-count assertion); two successive refocuses keep ring order for the nodes common to both.
9. Slice 9: every target the keyboard map reaches is a link or form on the rendered page (route test walks the map's anchors); the alpine handler is verified by hand after the first deploy and noted in the ship report.

## Target + blast radius

`src/precis_web/routes/graph.py` (exists; slices 3–6 add routes there), a panel partial in the shared ref template, route tests. Read-only, no migration, same auth gate as `/refs`.

## Open questions / decisions log

- **[decided 2026-10-06, Reto]** Focus page sits beside the ref reader; the reader stays the content view and the panel links in.
- **[decided 2026-10-06, Reto]** Caps "as proposed": focus page depth 2, path depth 4, 60 nodes per page, 25 per group before "expand"; hubs with >200 edges collapse to count chips.
- **[decided 2026-10-06, Reto]** Memory refs: "Show memories" toggle stays. Reto: "if mesh is intended to be mainly made from memory memory on, otherwise off." Orchestrator's interpretation (not Reto's wording): default ON when the focused neighbourhood is mainly memory refs (centre node is a memory, or memory is the majority kind among its direct links), otherwise OFF. Hubs with >200 edges still collapse to count chips.
- **[decided 2026-10-06, Reto]** Trail: "cookie is adequate for now". Trail as a persisted memory ref (cross-session; depends on the `memory-native-authoring.md` shape) is a later slice.
- **[decided 2026-10-06, fold-in]** Slices 7–9 came from a sibling spec written against the eye-ladder item's `/eye/<handle>` slice (now shipped) the same day without sight of this item; `ready` vetted them (`UP_RELATIONS` per-kind table with degree tie-break; peek in stored direction, one batched call; seeded fixture, never live qu202467; pins deferred). That spec proposed the trail as URL state (`?trail=h1>h2>h3`, shareable, browser back for free) — Reto's cookie ruling stands; noted as a non-blocking option if a trail ever needs to be pasted to an agent.
- **[decided 2026-10-06]** Edge editing from the panel and the focus page is `eye-link-editor.md` (draft, `blocked-by: web-graph-navigation`); this item stays read-only.
