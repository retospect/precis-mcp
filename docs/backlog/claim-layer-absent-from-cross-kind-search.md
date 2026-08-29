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

So this is a design question, not a flag flip. Sketch of the options:

1. **Carry posture into `SearchHit`.** A nullable posture field on the
   shared substrate, rendered only for kinds that populate it. Widest
   blast radius, best outcome.
2. **Union hubs into the fan-out as a distinct, clearly-labelled
   block**, keeping the finding table shape — the fan-out already
   renders per-kind sections, so a "Claims" block with its own columns
   may fit without touching `SearchHit`.
3. **Leave the flag and fix discoverability instead** — the wildcard
   footer already names excluded kinds
   (`_cross_kind_excluded_kinds`); make it say *why* an agent should
   look at `finding` for settled claims. Cheapest, least effective.

Do not pick by guessing. Read how the fan-out renders per-kind sections
first — option 2's viability turns entirely on that.

## Related

`docs/backlog/uncited-facet-patent-edgar-wiring.md` records the adjacent
hazard in the same subsystem: a handler's `**_kw` catch-all *swallows*
an unhonoured search filter rather than raising, so any new facet is
silently partial by default.
