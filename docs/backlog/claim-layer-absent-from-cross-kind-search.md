---
status: draft
title: The claim layer is invisible to the default search — supports_search_hits=False on finding
prio: high
---

# The claim layer is invisible to the default search

`FindingHandler.spec` sets `supports_search_hits=False`. Consequences,
from `runtime/search.py::_cross_kind_kinds` /
`::_resolve_cross_kind_request`:

- `search(q='…')` with no `kind=` — the wildcard fan-out, and the call
  an agent reaches for first — **never returns a claim hub.** It returns
  papers.
- `search(kind='paper,finding', q='…')` raises `BadInput`.
- Only an explicit `search(kind='finding', …)` reaches the claim layer.

This cuts against the premise the whole Taproot search build order was
justified by: a draft-writing agent should search the *settled claim
layer* before the underlying passage corpus. Today it has to already
know the layer exists and address it by name. `trust=`, the posture
columns, `uncited=` and the claim-graph eye are all reachable — but only
after the agent has decided to look.

## Why the flag is set, and why flipping it is not a one-liner

The opt-out is not an oversight. Per
`runtime/search.py::_cross_kind_excluded_kinds`, a kind opts out when it
carries a **per-kind result shape** the flat `SearchHit` substrate would
have to flatten and lose information from. `finding` genuinely does: the
`state` / `support` / `flags` trust columns (`handlers/finding.py::
_posture_cells`) exist precisely so a claim is never read without its
posture. Flipping the flag naively strips exactly those columns — you'd
surface claim hubs in the default search stripped of the signal that
tells you whether anything supports them, which is worse than not
surfacing them.

So this is a design question, not a flag flip.

## Resolved 2026-08-29 — option 1, in the `retraction_status` mould

**Option 2 is dead; its premise was wrong.** The fan-out does *not*
render per-kind sections. `runtime/search.py::_dispatch_cross_kind`
RRF-fuses every stream into a single TOON table
(`search_merge.py::_render_toon_table`: `id | summary | remaining_words
| links`) with one `_(per kind: …)_` count line above it. A separate
"Claims" block would sit outside the merge and give the agent two
rankings to reconcile, destroying the one comparison that matters —
*is there a settled claim about this, or only raw passages?*

**`SearchHit.retraction_status` is the working precedent for option 1**
and answers this item's own blast-radius worry. Same shape: a per-hit
signal only one kind populates, that must ride along and must not be
silently lost. It adds no column — `_merge_rrf` applies a multiplicative
penalty (`_RETRACTION_SCORE_FACTOR`) and `_render_toon_table` prefixes
the *summary cell*. Its comment states the rule: a dedicated column
"would render an empty cell on ~every row — pure token waste."

Prod (2026-08-29, 1552 hubs) says which posture signal to carry:

| | count |
|---|---|
| `unminted` / `candidate` / `signed` / `published` | 1413 / 135 / 3 / 1 |
| hubs with ≥1 verdict | 1478 |
| refuted | 48 |

`state` is ~constant (91% "unminted") and `unminted` ≠ unverified — 1347
of those 1413 carry verdicts. Putting `state` in a search row would
actively mislead. The dense signal is **support counts**; the rare
high-value one is **refuted**.

The shape:

1. `SearchHit.posture: str | None` — a pre-rendered terse token, `None`
   for every non-finding hit.
2. Render as a summary-cell prefix (`◆ 4✓ unopposed — <claim>`), not a
   column. `state` stays out of the cross-kind row.
3. Ranking gets a **positive** lever, not just a penalty: verified-and-
   unopposed boosts modestly, refuted sinks, `disputed` stays near
   neutral (a contested claim is what a drafting agent most needs to
   see). Start conservative — with 1478 boostable hubs an aggressive
   factor turns every wildcard search into a claim list.
4. `FindingHandler.search_hits` override — it already inherits a working
   one from `NumericRefHandler`, which is why the naive flag flip
   "works" and returns posture-stripped rows. The override adds the
   claim-hub filter and populates posture from one batched
   `nanopub/overview.py::hub_rows(ref_ids=[…])` per page, mirroring the
   `link_summary` pattern.
5. Flip `supports_search_hits=True`.

**All hubs enter, not just verified ones** — defaulting the stream to
`trust='verified'` would hide the 74 unverified hubs and recreate the
invisible layer this item exists to fix.

Blast radius is small: no test pins `finding` in the excluded list, and
the wildcard footer drops it automatically.

Narrower than this item implies: `search(tags=[…])` *already* returns
findings via `_dispatch_cross_kind_tags_only`'s numeric-ref kind list.
Hubs are reachable by tag fan-out today, just not by query fan-out.

## Related

`docs/backlog/uncited-facet-patent-edgar-wiring.md` records the adjacent
hazard in the same subsystem: a handler's `**_kw` catch-all *swallows*
an unhonoured search filter rather than raising, so any new facet is
silently partial by default.
