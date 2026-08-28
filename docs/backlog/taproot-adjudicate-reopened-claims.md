---
status: draft
title: Adjudicate — nothing decides whether a reopened claim's contradiction actually holds
prio: high
---

# Adjudicate — nothing decides whether a reopened claim's contradiction actually holds

## Motivation / why

Stage 8 of the claim lifecycle (`src/precis/taproot/__init__.py`) is
**absent**: *"is the conflict real, and who wins?"*. That was tolerable while
stage 7 (Oppose) barely fired — a contradiction was rare and a human noticed
it on the `/nanopub` disputed strip.

`08bbc7a3` changed the arithmetic. The widening arm now *writes* the
`contradicts` edges it used to discard, and every such edge triggers
`precis.nanopub.demote`: a `reviewed`/`signed` hub reopens to `candidate`,
an `anchored`/`published` one raises a `nanopub-demote` alert. So claims
will start reopening automatically, at whatever rate the judge finds
opposition — and **nothing downstream decides whether the opposition was
right**. A reopened claim is *unblessed*, never *adjudicated*: it sits at
`candidate` with a contradicts edge that blocks publication (the gate is
admissibility-only) until a human happens to look.

Two failure modes follow, and they pull in opposite directions:

- **A wrong CONTRADICTS is unappealable by machinery.** The verifier's
  `contradicts` flag is a MEDIUM-tier LLM verdict. Today the only way to
  undo one is a human editing `links` by hand. At scale a modest false-
  positive rate silently un-approves good claims.
- **A right CONTRADICTS goes nowhere.** No mechanism retires, refines, or
  re-scopes the claim the evidence actually refuted. It just stops being
  publishable, which reads identically to "nobody got round to it."

Both are the same missing verdict.

## In scope

- A verdict on a `(hub, contradicts-edge)` pair: *the contradiction holds*
  (→ the claim is wrong or over-scoped: retire, or `refines` into a
  narrower hub) vs *the contradiction is spurious* (→ demote the edge,
  restore the prior posture).
- Whatever the verdict is, it must be **recorded and re-readable**, so the
  same edge is never re-adjudicated from scratch — the `reground_seen` /
  `taproot_rejected` memo shape is the local precedent.
- Deciding whether adjudication is human-only (a `/nanopub` workbench
  action, like sign) or has a machine tier under a confidence floor.
  Note the asymmetry that argues for human-only at first: a demoter
  wired to a bad judge un-approves the corpus at machine speed.

## Out of scope

- Re-opening mechanics themselves — shipped in `08bbc7a3`.
- `chase_trigger`'s `TAPROOT_DUE` marking (still dark; separate).

## Open questions

- Does a spurious-contradiction verdict **delete** the edge or flip it to
  a recorded-but-inert relation? Deleting loses the fact that a judge once
  read it that way and invites a re-find next pass; keeping it needs the
  publish gate to learn "adjudicated-spurious" so it stops blocking.
- Does adjudication restore the *prior* publish state, or force the claim
  to re-earn `reviewed`? Re-earning is safer and matches how
  `nanopub_reopen` already discards the frozen fields; restoring is
  kinder to a human who signed once and was second-guessed by a bad
  judge.
- Above the freeze line the verdict cannot restore anything — the bytes
  are out. Does "contradiction holds" on a `published` claim mandate a
  retraction, or is that always a separate human call?

## Notes

The demotion trigger only fires from `hub_refine`, which ships **dark**
(a `service_config` prio row, one host). So the reopen rate is zero until
that service is enabled — which is the window in which to build this, not
a reason to defer it.
