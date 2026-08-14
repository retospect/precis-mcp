---
status: draft
title: Taproot hub has no write door for `scope` — only `title` can be corrected after mint
model: opus
---

# Taproot hub has no write door for `scope`

Found by dogfooding during a manual taproot reground pass over draft 173020.

## The gap

A claim hub's `scope` dict (e.g. `scope.material`, `scope.method`) can be set at mint time via `put(kind='finding', title=…, scope=…, supporters=[…])`, but **no live write door updates it afterwards**. `edit(kind='finding', …)` accepts exactly one of `pick_candidate=` | `title=` | `unacquirable_note=` — there is no `scope=`. The maturity table in `precis-taproot-help` confirms it: hub reword-in-place (`hub.py::refine_claim_sentence`) is title-only, and no other live row touches scope.

## Concrete symptom

Hub `fi191307`'s claim text wrongly read "corannulene-derived nanobowls"; the source paper (ref 5887) never uses the word "corannulene" — the term leaked from an adjacent claim during minting. `edit(kind='finding', id=191307, title=…)` corrected the claim body cleanly (DELETE+INSERT of the `ord=0` chunk, embedding cascade re-run, old `pub_id` kept as alias). But `refs.meta` still holds `{"scope": {"method": "cage–cage approach, novel fused nanobuds", "material": "C60 fullerene, corannulene-derived nanobowls"}, "source": "taproot"}`. The stale term now survives in exactly one place, reachable by no supported verb.

## Why this matters

Per `precis-finding-help`, `scope` "filters search and dedups identical `(body, scope, cited_in)` re-submissions so two agents writing the same claim collapse". A hub whose title has been corrected but whose scope has not is therefore left in a state where (a) scope-filtered search returns the superseded term, and (b) **the dedup key no longer matches the corrected claim** — so a later mint of the same claim with the right scope would fail to converge onto this hub and could create a duplicate. Duplicate hubs have no automated merge door (`precis-taproot-help` says the `pub_id`-collision raise is the handoff, and merging is a manual three-step repoint/move/delete), so the cost of landing in that state is high.

## Suggested direction

The obvious candidate is to let `edit(kind='finding', …)` accept `scope=` as a fourth mutually-exclusive option, routed through the same write door as `refine_claim_sentence` so the change is auditable, and re-deriving the `pub_id` the same way a retitle does (keeping the old as an alias). Whether a scope change should mint a new `pub_id` at all is a real design question — scope participates in the dedup identity, so changing it arguably changes the claim's identity — and should be decided deliberately rather than by analogy to the title path.

## Workaround available

Direct `UPDATE refs SET meta = jsonb_set(…)` on the row. That bypasses the product's own write door and any audit trail it would leave, so it is a stopgap, not the answer.
