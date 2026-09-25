---
status: ready
title: Findings need a stable identity so "still broken" is distinguishable from "broken again"
prio: high
model: opus
---

# Findings need a stable identity

Reto, 2026-09-24, while working `ewod-dogfood-2` through repeated
route/DRC cycles: "Findings need stable identity — I love this, indeed.
That is a pain for people too, not applicable here. Also allows to see
if 'this error' or 'both these' have gone away. I like this. I feel we
want to make a gripe to make the se stuff also provide such ids."

## The problem

A check today returns a *report*: a fresh list, computed and discarded.
Nothing in a finding survives the next run, so two runs of the same
check produce two unrelated lists that can only be compared by eye.

That is tolerable for a one-shot verdict and useless for the loop the
design surfaces actually run in — add a part, place it, route some,
move something, route again. In that loop the question is never "what
is the total error count", it is **"what did my last move change?"**
With 132 errors and 162 warnings on one board (`ewod-dogfood-2`, DRC run
`b13770b5`, 2026-09-24) that question is currently unanswerable without
diffing two walls of text by hand, and the counts alone actively mislead:
a move that fixes three findings and creates three others reads as "no
change".

It also makes the honest reporting rule impossible to follow. There is no
way to say "these two specific findings went away and this one is new",
only "the number went from 132 to 130".

## The shape today

Both kinds already converged on nearly the same finding shape, neither
with an identity:

- `pcb` — `{severity, rule, where, margin_mm, detail}`, rendered by
  `view='drc'`, persisted to `pcb_drc_findings`.
- `se` — `{severity, rule, subject, detail}`, rendered under
  `view='drc'`/`view='clearance'` (`precis_se/handler.py`'s report
  rendering, `render_agent_table(rows, schema=["severity", "rule",
  "subject", "detail"])`).

`pcb` persists its findings; `se` recomputes. Neither can answer "is this
the same finding as last time".

## What to build

**A finding's identity is `(rule, participants)`, not a row id.** The
participants are the durable things the finding is *about* — the two nets
in a clearance violation, the pad and the via in a keepout, the block and
its neighbour in an `se` interference — canonicalised (sorted, so an
unordered pair hashes the same either way) and *independent of the
measured value*. `R0C2 clears RESV by -0.114mm` and the same pair at
`-0.090mm` after a nudge are THE SAME FINDING with a changed margin, not
two findings.

Concretely:

1. **`finding_key`** — a stable, content-derived key over
   `(rule, canonicalised participants)`. Deterministic, recomputable from
   the finding alone, no counter or sequence.
2. **First-seen / last-seen** on the persisted row, so a finding has a
   lifetime rather than only a present.
3. **Delta reporting** — a check can render `new` / `still` / `resolved`
   against the previous run for the same subject. This is the whole point;
   without it the key is bookkeeping.
4. **Cross-kind convention, not a `pcb` feature.** `se` gets the same key
   over its own `subject`, and any kind that grows a check inherits it.
   Write it once as the shared finding contract.

## Why participants and not position

Position is the tempting key and it is wrong: every finding here is about
things that *move*. A clearance violation between two nets keeps its
identity when the parts shift 0.2mm; keying on coordinates would report
it as resolved-and-recreated on every placement move, which is exactly
the noise this is meant to remove. Coordinates belong in the finding's
payload (they are how you find it on the board), never in its identity.

The margin is likewise payload, not identity — a finding getting *worse*
is one of the more interesting things to report, and it is only
expressible if the finding survives the change.

## Acceptance

- Two consecutive checks on an unchanged design produce identical
  `finding_key` sets.
- A placement move that changes a margin without changing participants
  reports `still`, with the old and new margin, not `resolved` + `new`.
- A check can answer "what changed since the last run on this subject"
  without the caller diffing text.
- `se` and `pcb` both emit keys under the same contract.

## Notes / dependencies

- This is a prerequisite for the continuous-check model
  (`docs/backlog/` pcb check-surface work): a layer that "keeps the state
  and complains about conflicts" must be able to say which complaints are
  new.
- It composes with, but does not require, phase-awareness (suppressing a
  finding that is expected at the current stage). Identity first —
  phase filtering is only meaningful once a finding persists.
- `pcb_drc_findings` already persists rows, so `pcb` needs a column and a
  backfill rather than new storage. `se` recomputes and would need either
  persistence or a caller-supplied previous set to diff against; the
  latter is the cheaper first slice and keeps `se` stateless.
