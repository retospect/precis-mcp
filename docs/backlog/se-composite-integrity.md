---
status: ready
title: check that every join composite's claimed parts are still its live children
prio: high
---

# Check that every join composite's claimed parts are still its live children

## Motivation / why

A join composite records `meta.generated.parts` — the blocks it was built
from — and `_rebuild_block` replays that record to reconstruct topology
whenever the composite is joined again or revalidated. The record is only
meaningful if those blocks are still the composite's children.

Nothing checks that. `precis_se/atomic/validate.py` contains no reference
to `parts` at all, and no finding of any severity is emitted when the
relation breaks. It breaks in practice. Both prod dogfood designs reached that state — a
later join took a block an earlier composite still claims — and both times
the damage was found by reading rows by hand, only because someone went
looking.

The join-side guard that should prevent it (`join.part_addressed`) is in
fact correct; gr457995 is refuted. What actually got through was a join
executed by an MCP process running code older than that guard (gr458061).
That makes this detector *more* valuable, not less: a guard only protects
the calls that reach it, and nothing at all notices when a stale process,
a restored backup, or a future regression writes a composite that cannot
replay itself.

The invariant is one query. The absence of it is why a silent corruption
survived a green test suite, a prod dogfood, and a deliberate follow-up
audit that asked the wrong question (it checked that the named part blocks
still existed *somewhere in the design*, which is always true, rather than
that each part's live parent is still the composite claiming it).

## In scope

- A check in `view='validate'`: for every block bound to a join-generated
  structure, every name in its `meta.generated.parts` resolves to a live
  block whose parent is that composite. Report the violation with both
  ends named — the claiming composite and the block's actual parent.
- The same check reachable as a sweep over a whole design, so existing
  prod designs can be audited rather than only new edits.
- A test that constructs the corrupted shape (a composite claiming a part
  whose live parent is a different composite) and asserts the check fires.

## Explicitly NOT in scope

- The join-side guard. It already exists and is correct; this item is the
  detector that runs regardless of whether a guard was reached.
- Healing a corrupted composite. Detection first; a repair path needs a
  decision about which composite keeps the part.
- Any change to what `parts` records or how a composite is minted.

## Acceptance criteria

- A design where a composite claims a part whose live parent is a
  different composite fails `view='validate'`, with a finding naming the
  claiming composite, the part, and the part's current parent.
- A well-formed chained join (composite joined onto a further block, the
  ordinary case) produces no finding — chained joins are legitimate and
  must not be flagged.
- A sweep over every live `se` design in prod runs and reports a count;
  the result is recorded on gr458061 so we know the real blast radius of
  the stale-process window.
- The check costs one query per composite, not a rebuild.

## Target + blast radius

`precis_se/atomic/validate.py` (the check), the `se` handler's
`view='validate'` rendering, and whatever sweep entry point the audit
uses. Read-only against existing data. No migration.

## Open questions / decisions log

- Severity: ERROR or WARN? Leaning ERROR — a composite that cannot replay
  itself is not a style problem — but ERROR on a `view='validate'` of an
  already-dirty prod design will make those designs loudly broken before
  a repair path exists. Decide before marking a sweep result actionable.
