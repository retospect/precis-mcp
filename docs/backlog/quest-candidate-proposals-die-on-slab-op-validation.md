# Quest candidate proposals die silently on structure-op validation

> Found 2026-08-14 reading spark's worker log while verifying the dossier
> formatting fix. Recurring across at least three days on quest 202469; the
> visible consequence is a permanently empty frontier tree.

## Symptom

`precis.quest.compute.ensure_candidate` logs, tick after tick:

```
WARNING precis.quest.compute ensure_candidate: StructureHandler.put raised
  for quest 202469 slug q202469cand-341cdb367b — candidate not created
```

Observed 2026-08-12 02:31, 2026-08-13 22:00, 2026-08-14 00:32 — every time
the tick reaches the compute step. The traceback (the call passes
`exc_info=True`, so it is in the log) is always the same shape:

```
precis.structure.ops.OpError: slab needs 'element' and 'size' as [nx, ny, nz]
  → precis.errors.BadInput: op error: slab needs 'element' and 'size' as [nx, ny, nz]
```

The model emits a `slab` op whose `element`/`size` don't satisfy `_op_slab`,
`StructureHandler.put` raises, `ensure_candidate` catches, warns, and returns
`(None, False, None)`. The candidate is dropped.

## Why it matters

Two distinct failures stacked, and the second is the expensive one:

1. **The frontier never fills.** Dossier 202546's frontier-tree chunk has read
   `_(No candidates yet.)_` for its whole life. It is not that no candidate was
   ever *proposed* — one is proposed most ticks and every one dies here. The
   empty frontier is a symptom of this, not of an idle quest. (It also means
   the `quest-frontier-tree-seed-indistinguishable-from-empty` item is masking
   a live defect, not just a cosmetic one.)

2. **The model never learns.** The failure is a warning on the worker's stderr.
   Nothing is written back to the quest, the ledger, or the next tick's prompt,
   so the next tick proposes the same malformed shape. This is an unbounded
   silent retry loop — cheap per occurrence, permanently unproductive.

There is a third, prior question: quest 202469's own dossier states
*"Mode: Literature tracking and synthesis; no compute lane, no simulations,
no frontier entries."* A literature-tracking quest should arguably not be
reaching the candidate-proposal step at all. If that mode is meant to gate the
compute lane, the gate is not firing.

## Shape of a fix

Roughly in order of cost:

1. **Feed the validation error back.** `ensure_candidate` already has the
   exception; surface it into the next tick's prompt (or as a `ruled-out`
   attempt-ledger node) so the model sees why its spec was rejected. This is
   the change that converts a silent loop into a self-correcting one, and it
   composes with the `ATTEMPT:` ledger that now exists.
2. **Honour the quest's declared mode.** If a quest declares no compute lane,
   skip the candidate step rather than proposing and discarding. Needs a
   decision on where mode lives — it is currently prose in the dossier, not a
   field.
3. **Validate the op shape before `put`.** A cheap pre-check with a targeted
   message beats a `BadInput` from three frames down, but on its own it only
   moves where the silence happens — do this *with* (1), not instead of it.

Do not "fix" this by loosening `_op_slab`; the validation is correct and the
spec really is malformed.

## Related

- `docs/backlog/quest-frontier-tree-seed-indistinguishable-from-empty.md` — the
  seed string this defect hides behind.
- `docs/backlog/quest-ledger-accumulates-duplicate-branches.md` — same family:
  the tick repeating itself because nothing feeds its own history back to it.
