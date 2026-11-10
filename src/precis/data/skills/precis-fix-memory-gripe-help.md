---
id: precis-fix-memory-gripe-help
family: memory
title: precis — resolve a memory gripe by replaying its routes
summary: classify conflicting or unclear memories, preserve scoped truth, replay the reader's routes, and close or ask
answers:
  - how do I fix a project:memory gripe?
  - what if two memories disagree about the same thing?
  - what if apparently contradictory memories name different referents?
  - how do I handle an undecidable memory dispute?
  - what does a sound memory body need for its section type?
applies-to: get/search/edit/link/tag (kind='memory'), get/put (kind='gripe')
tags: [workflow, troubleshooting]
kinds: [memory, gripe]
status: active
---

# precis-fix-memory-gripe-help — resolve a reader's memory complaint

## Read the story and replay the routes

1. Read the `project:memory` gripe: Goal, Route and the claims each route
   returned. Read `meta.route` for the recorded calls and their arguments.
2. Open every named memory with `view='fisheye+1hop'`, every named entity
   ref, and the evidence they cite. Capture the hub's recall set before
   changing anything.
3. Replay both routes as written. A route is the test, not the target:
   a circuitous route is fine. Establish what each route answers and why.
4. Name the disagreement shape below. Every change goes through the
   review ledger, pinned to the content sha and undoable; see
   [[precis-gardener-help]]. If the required apply door is unavailable,
   leave a proposal and ask; do not bypass the ledger with a direct edit.

## Choose the disagreement shape

| Shape | Evidence and repair | Edge |
|---|---|---|
| Same referent, one wrong | Resolve against the entity ref, the repo at main or a dated commit. Rewrite the wrong claim to the truth with one live anchor and a date; supersede when the right node already states it. | `supersedes` winner → loser; the tombstone redirects. |
| Same referent, true at different times | Verify both dates. The entity ref carries current state; each memory keeps its dated event and the entity handle. | None, or `supersedes` when the older node is pure history. |
| Different referents (Joe's dog, Brenda's dog) | Put the referent in each note's title and first line, and its handle in the body. Copy no facts from the sibling. | Mention note → referent; `related-to` between siblings with `meta.why='same question, different referent'`. |
| Unclear, not contradictory | Rewrite to the standard of done below. A grab-bag of claims needs a split, preserving every still-true claim. | None required. |
| Undecidable | Record what was checked. Mark both `AUDIT:contested` when that value is registered, then ask through a `memory-review` todo linked to the gripe. | `contradicts` between the two memories so both fisheyes show the dispute. |

`AUDIT:contested` is not currently a registered value. Report this missing
vocabulary in the handoff; keep the dispute explicit in the edge and review
todo, and leave the gripe open. Do not invent a substitute tag or silently
close it.

## The memory-body checklist

Judge a memory body against its section type before proposing any
action. The candidate's detector evidence says what was checked; this
table says what a sound body needs. Title shape applies on every type:
the title states the body's conclusion in about twelve words, with no
dangling it/this.

| Type | Checks |
|---|---|
| reference | Route present (an evidence edge, a `Source:` line or a `route:` tag); confident wording on one root; the quoted span supports the claim; anchors resolve. |
| gotchas | One dated case — a handle resolving to a dated gripe/todo counts as the dated case, so resolve handles before claiming "no dated case"; the cause is already fixed; the scope is stated. |
| runbooks | Every named script/flag exists; preconditions stated; last run dated. |
| workflow, rulings | Who, when, in force, absorbed where; no conflict with another in-force ruling. No route check: an imperative has authority and scope, not truth. |
| threads | `Left:` current; due items linked. |
| DREAM | Duplicate/merge only — never bulk-retire dreams. |

On every type:

- A rewrite states the claim, never a label: "put ops wipe" is a trap's
  name, not the trap — write what bites, when, and what to do, or point
  at the atomic node whose title says it. A list of child titles in a
  body restates the `part-of` edges; drop it.
- Unanchored plan labels ("slice 4a", "round 6", "R17", "leg C", "wave
  3") with no handle or date: resolve to the todo/gripe/spec section or
  drop. A slice is a unit of work and has its own ref.
- Commit shas in standing-claim sentences age out after 30 days: keep
  the date, drop the sha. A sentence that only records "X landed at
  <sha>" is history and goes.
- Handoff/resume blocks and dated campaign narratives older than 14 days
  in a non-thread node are history; the thread file and git hold them.
- Token grammars, naming standards, coding conventions and door
  descriptions already in a skill or `docs/conventions` are promoted (a
  promote-to-doc list for Reto) or deleted as a restatement; the memory
  keeps the pointer and the ruling date.
- An instance is one dated clause ("pa5837, 2026-08-27"); the repair
  narrative is history.
- One claim per node, the title is the claim. A grab-bag under one
  campaign title is a split candidate, not a rewrite target.

Forward rule for memory writes: the mention's role is the edge's
relation — evidence is `cites`/`derived-from`, scope is `qualifies`, a
bare prose mention means see-also only — so a future dangling edge
resolves by its relation.

## Rewrite to the standard of done

Resolve the faulty reference now: read its tombstone and successor, the
current entity ref, or the repo's python/md evidence. Write the claim as
currently true, with one live anchor and a date or sha. Keep dated events
and their handles; current state lives on the entity ref. Put the
investigation trail in the change's `reason`, readable in `view='log'`.
The body is no longer than before plus one clause. A cold reader gets the
truth; stripping the claim or adding "unverified since" does not settle it.

Never:

- Retitle to win a query.
- Write prose that restates another node's claim.
- Drop a still-true claim without moving it.
- Edit a `trust_origin='human'` node: hold and ask.

## Replay after and close or ask

Replay both original routes after applying the approved changes. Pass only
when all of these hold:

- Both routes still arrive where the story says.
- Answers are consistent, or each carries its scope in the title plus an edge.
- No live node states a losing claim.
- Every handle resolves.
- The hub's recall set finds what it found before; this is a regression
  guard, not a rank target.

Comment on the gripe with the shape, what changed, the sha and the replay
result, then close with `STATUS:fixed` (canonicalized to `STATUS:done`).
The closing comment starts `resolved:`:

```python
put(kind='gripe', id='gr42',
    text='resolved: <sha> — <shape>; <what changed>; both routes replayed, '
         'handles resolve, hub recall preserved',
    tags=['STATUS:fixed'])
```

Undecidable means ask, not a silent close: link a `memory-review` todo to
the gripe, name both memories and the evidence inspected, and keep the
gripe open. Follow [[precis-memory-help]] "Working a memory-review todo".
