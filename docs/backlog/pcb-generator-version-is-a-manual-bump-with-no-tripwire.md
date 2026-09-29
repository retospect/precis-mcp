---
status: draft
title: A generator code change is invisible to already-authored boards unless someone remembers to bump version
prio: normal
---

# Generator output can change without any board noticing

## Motivation / why

Found by dogfooding prod on 2026-09-29, not by reading code.

`5fc99982` removed the EWOD generator's third copper row (the B.Cu
breakout stub). It landed, it deployed, the fleet ran it — and prod board
`pb345846` still held all 54 of the tracks it no longer emits:

```
 generator_name | ctype | layer | generator_version | count
 ARR1           | track | B.Cu  | 1                 |    54
```

The cause is `store/_pcb_ops.py`'s no-op test in the generator loop of
`_pcb_apply`:

```python
and existing_gen["version"] == expansion.version
and existing_gen["params"] == expansion.canonical_params
):
    continue  # unchanged -- no-op, nothing to merge/re-insert
```

The decision is `(generator, version, canonical_params)`. It never
compares the emitted rows. So a **code-only** change to what a generator
emits is unobservable on every board already authored at the old version,
and stays that way forever — re-`put`ting with the same params is
documented as a no-op (`precis-pcb-ewod-help.md`, "Re-`put`ting the same
`params` is a no-op"), which is true and is exactly the trap.

`f8f884d15` fixed *this instance* by bumping `GeneratorExpansion.version`
1 -> 2 and documenting the bump contract on the field, with a tripwire
test in `tests/test_pcb_ewod_fabric.py` that fails if the emitted shape
changes while the version does not. That test is generator-specific and
hand-written. The general hole is open.

**Second bump, 2026-09-29 — the contract held, by hand.** Version 2 -> 3
dropped `fixed='both'` from sink instances (Reto's placer ruling). The
bump was remembered because this item had just been written; nothing in
the type, the store or the gate would have caught forgetting it. That is
one datapoint for "manual works when the author happens to be looking at
the item about it", which is not a mechanism.

## What is actually wrong

1. **The bump is manual and the failure is silent.** Nothing in the type,
   the store, or the gate notices that a generator's output changed while
   its version did not. The only signal is a board in production quietly
   serving stale copper — which is how this one was found, hours late.
2. **There is no way to force a regeneration.** No `force=` on the
   generators block, no "regenerate at current code" op. The only escapes
   are changing a param (which changes the design) or bumping the version
   (which requires a code change and a deploy). An operator who *knows*
   the board is stale still cannot fix it from the tool surface.
3. **No way to ask "which boards are stale?"** Answering it required an
   ad-hoc prod query against `pcb_generators.version`. There is no view.

Only `ewod_pad_array` is in `_REGISTRY` today, so the blast radius is
currently two dogfood boards. This gets worse with the second generator,
not better.

## In scope

1. **Make the version bump enforceable rather than remembered.** The
   cheapest honest version is a test that hashes each registered
   generator's expansion for a fixed param set and pins
   `(version, digest)` — changing the output without the version fails
   the gate, and the fix is a one-line bump plus a new expected digest.
   Generic over `_REGISTRY`, so a new generator is covered on the day it
   is registered.
2. **An explicit regenerate affordance.** Something on the `put`
   generators block (or a `op='regenerate'`) meaning "re-expand at current
   code even if nothing I passed changed". It must stay explicit: the
   re-expansion retires the whole expansion and reinserts it, so placement
   and routing on that board are destroyed. That is the correct behaviour
   for a shape change, but it is not something to do implicitly.
3. **A staleness read.** `view=` on the board (or a fleet query) reporting
   each generator's stored version against the code's current version.

## Explicitly NOT in scope

- Diffing emitted rows at apply time to decide no-op. That makes every
  `put` expand the generator and compare potentially thousands of rows,
  to catch a case that a digest test catches for free at gate time.
- Auto-regenerating stale boards. Destroying a placement needs an
  operator, not a heuristic.

## Target + blast radius

`src/precis/pcb/generators.py` (`GeneratorExpansion.version`, `_REGISTRY`),
`src/precis/store/_pcb_ops.py` (`_pcb_apply`'s generator loop,
`_pcb_generator_retire_expansion`), and the `precis-pcb-ewod-help` /
`precis-pcb-help` skills, which currently document the no-op rule without
its consequence.

## Open / decisions log

- **OPEN — does the digest test belong in the gate or as an advisory?**
  Gate, leaning: a silent stale-copper failure took a prod dogfood to
  find, and the cost of the gate version is one line of churn per
  deliberate change.
- **Settled 2026-09-29 — the no-op rule itself stays.** Re-`put` being a
  no-op for unchanged params is what makes authoring re-runnable. The bug
  is that `version` is the only thing standing between that rule and
  stale output, and nothing watches it.
