# Proposal: universal inspection views (`links` / `log` / `raw`)

**Status:** proposal (not yet implemented)
**Motivation:** skill-audit finding, 2026-07-04 — `precis-overview` and
`precis-toolpath-help` claimed `view='raw'/'links'/'log'` work on *every*
id-addressable ref kind. They don't: the three are implemented in
`NumericRefHandler` (`_numeric_ref.py:70`, `_BASE_VIEWS`) and inherited
only by the 12 numeric-ref kinds. Slug/file/compute kinds (`paper`,
`draft`, `cad`, `structure`, `pcb`, `tex`, `markdown`, `plaintext`,
`pres`, `oracle`, `news`, `part`, `datasheet`, `cfp`, …) reject them with
`unknown view`. The docs were narrowed to tell the truth; this proposal
is the alternative — make the claim true instead.

## Why lift it

`links` (the typed-edge graph), `log` (the `ref_events` trail), and
`raw` (verbatim columns + full `meta` JSON) are **ref-level** facts, not
kind-specific renders. Every row in `refs` has an id, can carry links,
and emits events. An LLM debugging a paper or draft naturally reaches for
`get(kind='paper', id=X, view='links')` — and today gets a confusing
`[error:Unsupported] unknown view 'links'`, even though the paper *has*
links and events. The inspection triad is exactly the surface you want
uniform: "show me this ref's edges / history / hidden state" should not
depend on which kind you happen to be holding.

## Scope: ref-backed kinds only

The triad is meaningful only where a stable `refs` row exists. Target:

- **In scope (ref-backed):** `paper`, `draft`, `cad`, `structure`,
  `pcb`, `tex`, `markdown`, `plaintext`, `pres`, `oracle`, `news`,
  `part`, `datasheet`, `cfp` — plus the 12 numeric-ref kinds that
  already have it.
- **Out of scope (no stable ref / pure compute or passthrough):**
  `calc`, `math`, `random`, `provenance`, `websearch`,
  `perplexity-*`, `wikipedia`, `youtube`, `web`. These are
  cache/compute kinds; `links`/`log`/`raw` have no referent. They should
  keep returning `unknown view` (with their own option list). `web`/
  `news`/`youtube` *do* mint cache refs and could be phased in later, but
  are deferred to keep v1 tight.

## Design options

1. **Shared mixin `RefInspectionViews`.** Extract `_render_links_view`,
   the `log` renderer, and `_render_raw` (today in `_numeric_ref.py`,
   lines ~912/1033) into a mixin keyed on `ref_id`. Mix it into the
   slug/file handler base(s). Each handler's view dispatch gains a
   fallthrough: `if view in _INSPECTION_VIEWS: return
   self._render_inspection(view, ref)`. Numeric-ref keeps its current
   behaviour by inheriting the same mixin. **Recommended** — smallest
   blast radius, preserves per-handler overrides.

2. **Dispatch-layer interception.** In the central `get` path
   (`dispatch.py`), intercept `view ∈ {links,log,raw}` before delegating
   to the handler and render generically for any ref-backed kind. Most
   uniform (zero per-handler edits) but bypasses handlers that customise
   one of the three (`paper` already ships its own richer `log`). Would
   need an opt-out registry.

3. **Promote to base `Handler` (`protocol.py`).** Cleanest conceptually
   but touches the common ancestor of compute kinds too, so it needs a
   `ref_backed` capability flag to avoid offering the views where they're
   meaningless.

Option 1 with a per-handler override hook (so `paper.log` stays its
richer variant) is the least-risk path.

## The override case: `paper.log`

`paper` already implements its own `view='log'` (richer than the generic
`ref_events` dump). The design must let a handler override any leg of the
triad while inheriting the other two. Concretely: the mixin provides
defaults; a handler that defines its own `log` wins. Encode this as
"handler-declared views take precedence over inherited inspection
views" in the dispatch/`accepted_views` merge.

## Work items

1. Extract the three renderers into `RefInspectionViews` (from
   `_numeric_ref.py`), parameterised by `ref_id` not `Ref`-subtype.
2. Mix into the slug/file handler base; add the dispatch fallthrough +
   precedence rule for handler-declared overrides.
3. Add `links`/`log`/`raw` to each in-scope kind's `accepted_views()` so
   the bogus-view option lists advertise them.
4. Tests: per in-scope kind, assert each view returns a real body (not
   `unknown view`); assert `paper.log` keeps its custom render; assert
   out-of-scope kinds (`calc`, `math`, …) still reject.
5. Re-widen `precis-overview` + `precis-toolpath-help` to state the new
   universal-over-ref-backed-kinds behaviour (revert the 2026-07-04
   narrowing), and note the compute-kind exclusion.

## Effort / rollout

Medium. One shared implementation + wiring + option-list updates + a
per-kind test matrix. Safely incremental: ship the mixin against the
highest-value kinds first (`paper`, `draft`, `cad`, `structure`, `pcb`,
`tex`, `markdown`, `plaintext` — where LLMs most reach for `links`/`log`)
and extend the in-scope set kind-by-kind. Each addition is docs-visible
via the option list, so partial rollout never lies.

## Risks

- `raw` exposes `meta` JSON — audit that no kind stashes secrets there
  (low risk; today's numeric-ref `raw` already dumps `meta`).
- Link/event volume on hot kinds (a paper with thousands of `cited-by`
  edges) — reuse the existing pagination the numeric-ref `links` view
  already has.
