---
status: draft
title: eye link editor — add, retype and remove an edge from the focus page, through the link verb
pillar: memory-graph
prio: normal
model: opus
blocked-by: web-graph-navigation
---

# eye link editor — edit the edge you are looking at

## Motivation / why

Reto, 2026-10-06: a knowledge-graph viewer "(/editor)" — dive into the
graph, back out again, and fix what is wrong while there. The viewer is
the shipped `/eye/<handle>` focus page (`precis_web/routes/eye.py`),
which renders, it does not act. This item is the acting half for a
human: when the focus page shows a wrong or missing edge, the repair
happens on that page, not in SQL and not by composing a `link(...)` call
by hand.

What exists: every web write already goes through the verb runtime
(`precis_web/deps.py::await_dispatch`; `redirect_or_error` wraps it for
full-page forms), so the `link` verb's guards — `validate_relation`,
`check_relation_constraints` (domain/range kinds from migration 0180,
whose `BadInput` text is the one-line reason), `guard_and_route_contradicts_disputes`,
`guard_taxon_hierarchy` (`handlers/_link_tag_ops.py`) — apply to a web
POST for free. `apply_link_ops(store, src, link=, unlink=, rel=, meta=)`
already accepts `meta`, but the handler `link()` methods call it without
`meta` and swallow `**_kw`, so no provenance reaches `links.meta` from
the verb today. The web `link` dispatches are all side effects of
another action (todo reparent `todo.py`, Drive folder move `drive.py`,
Ask conv `derived-from` in `refs.py`); none lets a person choose a
relation. Edges are one row src→dst with inverses mirrored at read time
(`store/_links_ops.py::links_for`); `add_link` and `remove_link` each
open their own connection, so there is no multi-op transaction.

## In scope

0. **`meta=` through the link verb.** The shared handler `link()` (base
   class and the overrides: `paper.py`, `finding.py`, `taxon.py`,
   `folder.py`, …) passes `meta=` to `apply_link_ops`, which already
   forwards it to `store.add_link`. This is a verb-surface change that
   agents get too (`link(kind=, id=, target=, rel=, meta={...})`); the
   `precis-link-help` skill documents it. It is the one prerequisite the
   other slices share, and ships first (own commit).
1. **Per-ring-entry edge actions on `/eye/<handle>`.** Each neighbour
   row in a ring carries *retype* and *remove*; the ring header carries
   *add edge*. Actions POST with htmx (`static/htmx.min.js` is vendored)
   to routes that call `await_dispatch` directly and return a row partial
   on `HX-Request` (precedent: `routes/flags.py`, `routes/gripes.py`);
   the non-htmx fallback is `redirect_or_error` back to the same URL, so
   the trail is kept either way.
2. **Add edge.** A handle field (resolves like the `/eye/` search box)
   plus a relation picker. The picker lists only relations whose
   `domain_kinds`/`range_kinds` admit (focus kind, target kind) in the
   chosen direction — from `store.relation_constraints()`, applying the
   same inverse-slug swap `check_relation_constraints` applies — with
   `related-to` first and deprecated slugs hidden (`relation_constraints`
   gains `deprecated_at`). Direction is explicit: "focus → target" or
   "target → focus"; a one-way relation with no inverse shows one
   direction only.
3. **Retype = two ordered dispatches.** Add the new relation on the same
   (src, dst) pair, then unlink the old one by its `rel`. Order
   guarantees the pair never has zero edges; the only partial-failure
   state is both edges present, which the row shows and one more click
   repairs. No new atomic store op — `add_link`/`remove_link` keep their
   own connections. Direction is preserved (a retype on an inbound row
   keeps src = the neighbour).
4. **Remove with undo.** Before the unlink the route reads the row via
   `store.links_for(..., relation=)` and returns, in the partial, a greyed
   row carrying (src, dst, rel, meta) as hidden fields; *undo* POSTs them
   back through the add route. Undo is page-session only: reload and the
   greyed row is gone (decided below).
5. **Guard feedback in place.** A refused op renders the guard's
   `BadInput` text on the row partial; the rest of the page is untouched.
6. **Provenance.** `set_by` is the web user (already so through
   dispatch); `meta` gains `via: 'eye'` and the trail at the time of the
   edit (`trail: [...]`, same cap as the URL trail) via in-scope 0, so a
   gardener or a reviewer can see the path that led to a hand edit.
7. **Runtime doc.** One paragraph in `precis-fisheye-help` ("the focus
   page edits edges; the MCP side is `link(...)`"), the `meta=` line in
   `precis-link-help`, and one line in `precis-relations` pointing at the
   picker as the human view of the constraint columns.

## Explicitly NOT in scope

- Editing node bodies, tags or `meta` — the page edits *edges* only.
  Tags stay with Drive's tag action and the `tag` verb.
- A new write path or auth model. Web writes go through `await_dispatch`;
  every `web_users` row stays fully authorised (`identity-and-access.md`
  owns roles). In-scope 0 widens an existing verb, it adds no path.
- Mirroring an inverse row on write. The store's read-time inverse
  mapping stands.
- An atomic multi-op link transaction in the store.
- Bulk edits, merges, splits or relinks of many edges — those are
  gardener proposals (`graph-gardener.md`, "proposals, not edits"). This
  page could *review* a gardener proposal later; that is a separate item.
- The agent-side `focus` verb (`fisheye-level2.md`).
- Chunk-scoped edges (`~N` endpoints) in the add form. Rows that already
  have a chunk endpoint show it and can be removed/retyped, but the form
  adds ref-level edges only.
- A visual node-and-edge canvas. If the viewer grows one
  (`topology-cloud.js` is the candidate engine), edits stay on the ring
  rows; the canvas is not an edit surface.
- Undo surviving a reload, and any audit row beyond `links.meta`.

## Acceptance criteria

1. Fixture: a paper `corroborates` a finding hub (legal per 0180:
   domain paper, range finding). On the hub's `/eye/` page the row is
   retyped to `disputes`; afterwards `store.links_for(hub, 'in')` holds
   exactly one row for that pair, `relation='disputes'`, `src=paper`,
   and `get(kind='finding', id=hub, view='links')` shows it. The route
   test performs it as the two POSTs the UI issues.
2. Picker/guard parity: for every `relations` row with non-null
   constraints and every (src kind, dst kind) pair drawn from the kind
   registry, the picker offers the relation iff `check_relation_constraints`
   accepts it (inverse swap included). Deprecated slugs never appear.
3. Remove then undo on a fixture row: the re-added row carries the
   original `meta` keys plus `via: 'eye'`; a remove followed by a reload
   leaves no undo control.
4. A refused add (fixture: a relation whose `range_kinds` excludes the
   target) returns a 200 partial containing the `BadInput` text from
   `check_relation_constraints`, and a full `GET` of the page before and
   after is byte-identical.
5. An edit made on `/eye/<h>?trail=a>b>c` writes `meta.via == 'eye'` and
   `meta.trail == ['a','b','c']` on the link row; the same `meta` arrives
   when the MCP `link` verb is called with `meta=` (in-scope 0).
6. Every write route calls `await_dispatch` with verb `link`; a test
   patches the dispatcher and asserts each action produced the expected
   `link`/`unlink`/`rel`/`meta` arguments and no other store write.
7. `precis-fisheye-help` contains an "Editing edges" section and
   `precis-link-help` documents `meta=`; the skills' doc test covers both.

## Target + blast radius

- `src/precis/handlers/*::link` (base + overrides) — `meta=` passthrough
  (in-scope 0); `tools/core.py::link` signature if it drops `meta` today.
- `src/precis/store/_links_ops.py::relation_constraints` — select
  `deprecated_at`.
- `src/precis_web/routes/eye.py` — POST
  add / retype / remove / undo; partials under `templates/eye/`.
- Skills: `precis-fisheye-help`, `precis-link-help`, `precis-relations`.
- Tests: `tests/precis_web/test_eye_edit_routes.py` (TestClient, per
  `test_budget_route.py`), the parity test (AC 2), the dispatch-argument
  test (AC 6), the link-verb `meta` test (AC 5).

## Open questions / decisions log

- **[decided 2026-10-06]** Split from the viewer item rather than folded into it: the
  viewer shipped read-only;
  writing from the page adds guard-feedback UI and provenance that would
  reopen a specced item. `blocked-by` carries the ordering.
- **[decided 2026-10-06]** Retype is remove + add, not an UPDATE on
  `links.relation`: the UNIQUE index and the guards are written for
  add/remove, and a hand UPDATE would skip `guard_and_route_*`.
- **[decided 2026-10-06, ready-check]** Retype is two ordered dispatches
  (add new, then unlink old), not one `apply_link_ops` call: that
  function takes one `rel=` for both `link=` and `unlink=`, so a single
  call would unlink the relation it just added, and there is no
  transaction across the two store calls. Add-then-remove keeps the pair
  non-empty; "both present" is the accepted partial-failure state.
- **[decided 2026-10-06, ready-check]** Provenance rides the link verb's
  own `meta=` (in-scope 0), not a web-only path: `apply_link_ops` already
  has the parameter, the handlers drop it. Widening the verb is the
  smaller change and gives agents provenance on their links too.
- **[decided 2026-10-06, ready-check]** AC 1 uses `corroborates` →
  `disputes` on a paper→finding row; `contradicts` is memory→memory only
  and adjudication-derived, so it is not a fileable fixture.
- **[decided 2026-10-06, ready-check]** Undo is page-session only; the
  removed row's fields travel in the partial as hidden inputs, read via
  `links_for` before the unlink (`remove_link` returns a count only).
- **[decided 2026-10-06, ready-check]** Model tier `opus`: a new htmx
  surface plus a verb-signature change across handler overrides.
- **[decided 2026-10-06, ready-check]** Not split into three items: the
  `meta=` passthrough is a one-commit slice inside this item (in-scope
  0), and the picker and the retype/undo share the same route file and
  partials.
- **[decided 2026-10-12, fold-in]** Landed from `worktree-logical-percolating-firefly`
  onto a main where the trail and navigation moved to
  `web-graph-navigation.md` slices 6–9 (cookie-held trail, Reto
  2026-10-06). `blocked-by` follows them. AC 5's `?trail=` URL and the
  "same cap as the URL trail" in in-scope 6 read against that cookie
  trail; the `ready` pass that unblocks this item rewords them.
