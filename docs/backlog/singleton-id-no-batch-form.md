---
status: ready
title: numeric-ref verbs take one id — 58% of tool calls sit in singleton loops, and the batch form agents try crashes
prio: high
---

# No batch form on numeric-ref verbs (and `id=[...]` crashes rather than refusing)

`get` / `put` / `tag` / `search` on numeric-ref kinds (gripe, todo, alert,
draft, se, job…) accept exactly one `id`. An agent holding N known ids has to
make N sequential calls; `search` is the only multi-result path and it is
relevance-ranked, not "fetch exactly these N".

## Measured — surface-review pass #1, 2026-09-29

Over 390,590 events / 99 sessions with resolvable `(verb, kind)`:

- **1,608 of 2,770 calls (58%) sit inside a run of ≥4 consecutive
  identical-shape singleton calls**, across 52 of 99 sessions (150 runs).
- Verified these are per-item loops, not retries — every call in a sampled
  run addresses a distinct id.
- Heavy tail: single runs of 154, 100, 71, 50, 43, 42, 38 calls.

Ranked by calls spent inside such runs:

| (verb, kind) | calls | runs | note |
|---|---|---|---|
| `get, gripe` | 483 | 29 | one bulk-close session, **plus** 21 distinct prod doctor-tick jobs each looping |
| `tag, todo` | 178 | 5 | dominated by one 154-call run |
| `get, alert` | 173 | 21 | **the systemic one** — 14 distinct recurring doctor-tick job runs, every tick |
| `put, draft` | 141 | 4 | the term-glossary case: a draft with 508 undefined abbreviations, one `put(chunk_kind='term')` each |
| `put, gripe` | 100 | 16 | |
| `search, gripe` | 98 | 16 | |
| `get, job` | 94 | 15 | |

`get(alert)` is the sharpest: it is not one power user, it is the same
automated job type re-running a singleton loop on every tick, forever.

## The narrower bug — file this half even if the batch form doesn't happen

An agent that has made 150 singleton `tag` calls will try the obvious shape:

```
tag(kind='todo', id=[399851, 347579, 348200], add=['STATUS:done'])
→ [error:Internal] internal error in tag: AttributeError (see server log)
```

and then gave up in its own words: *"No batching. Let me generate the exact
id lists, then hand the mechanical pass to a cheap agent."*

Root cause: `src/precis/handlers/_numeric_ref.py::NumericRefHandler._coerce_id`
raises a clean `BadInput` for `id=None` and for an unparseable string, but
then does `s = id.strip()` on anything else — and a list has no `.strip()`.
So the one wrong-but-reasonable call shape an agent will actually attempt is
the one that produces an unhandled `AttributeError` surfaced as
`[error:Internal] … (see server log)`.

Two things are wrong with that, independent of batching: an `Internal` is a
server bug, not caller error, so it reads as "precis is broken" rather than
"you held it wrong"; and "see server log" is advice an MCP client cannot act
on — it has no server log.

## Generalize at the `_coerce_id` seam — not per verb

The crash and the missing batch form are the same fact, and both live at one
place: `_coerce_id` is the single funnel every numeric-ref verb's `id` passes
through. Fixing them per-verb would mean N guard clauses that the N+1th verb
forgets — the same trap as the render caps (see
`gripe-comment-timeline-uncapped.md`).

Make `_coerce_id` **the normalizer**: accept a scalar or a list, always return
a list, and reject anything else with a clean `BadInput`. Then

- the `AttributeError` becomes impossible by construction rather than by a
  new guard clause,
- each verb opts into fan-out by handling a multi-element list, and the ones
  that haven't yet can reject `len > 1` with a message that names what they
  *do* support — a degradation, not a crash,
- "which verbs are batch-capable?" becomes readable from one place.

## In scope

1. **The clean rejection** (small, do it regardless, and it falls out of the
   normalizer above): `id=[1,2]` → `BadInput` naming the correct form, never
   `Internal`.
2. **A real batch form**, highest-leverage first: `get(kind=…, id=[...])` and
   `tag(kind=…, id=[...])`. `get(gripe)` + `get(alert)` alone are 656 of the
   1,608 run-calls. `tag` is already transactional per its own docstring.
3. A batch `put(kind='draft', chunk_kind='term', terms=[…])`, which is the
   500-round-trip case.

## Explicitly NOT in scope

- Changing `search`'s ranked-result contract.
- Batch `delete` — destructive, wants its own decision.

## Acceptance criteria

- `tag(kind='todo', id=[1,2])` returns `BadInput` naming the correct form —
  never `Internal`.
- Whichever batch verbs ship are transactional and report per-id outcomes, so
  a partial failure is legible.
- A recurring doctor-tick's `get(alert)` loop collapses to one call.

## Open questions

- Does a batch `get` return one concatenated render or a list? The render
  size question interacts with `gripe-comment-timeline-uncapped` — a batch
  `get` of 50 uncapped gripe renders would be worse than the loop it
  replaces. Cap first, or make batch `get` summary-shaped by default.
