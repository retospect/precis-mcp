---
status: draft
title: hypothesis findings — editable testable_by / motivation after mint
prio: normal
---

# hypothesis findings — editable testable_by / motivation after mint

From gr263258. Spec drafted 2026-09-09; awaiting Reto's review before
build.

## Motivation / why

Hypothesis findings are defined by falsifiability — `testable_by` is
mandatory at mint — but `edit(kind='finding')` accepts only
`pick_candidate`/`title`/`unacquirable_note`, so the discriminating
experiment can never be sharpened as literature accrues. Concrete case
fi262718: a literature pass confirmed the hypothesis stands but produced
two sharpenings that cannot be applied (cage-scaffold decomposition risk
per pa1863; ensemble-averaged readouts can't demonstrate per-cage
addressability — needs a site/particle-resolved readout). Today's
workarounds are both bad: supersede-and-remint (loses identity/history) or
hand-editing `refs.meta.proposed_payload` out of band.

## In scope

- Extend finding edit with hypothesis-only kwargs: `testable_by=`,
  `motivation=`, `add_motivated_by=`, gated on
  `meta.artifact_type == 'hypothesis'` (reject on non-hypothesis,
  mirroring `title=` behavior).
- Re-run the hypothesis lints on write.
- Edit trail: `meta.testable_by_history` appends the prior value + a
  timestamp, so a sharpened discriminator is visibly distinct from the
  original conjecture (silently changed falsification terms are their own
  epistemic hazard).
- Signed-hypothesis interaction: `nanopub/assemble.py` reads motivation at
  sign time — block the edit on an already-signed hypothesis with an error
  naming the re-sign path (no silent divergence between the signed artifact
  and the live ref).

## Explicitly NOT in scope

- Editing these fields on non-hypothesis findings.
- Auto-re-signing or mutating published nanopubs.
- A sharpening workflow/worker — this is only the edit door; fi262718's
  actual sharpenings get applied manually once the door exists.

## Acceptance criteria

- `edit(kind='finding', id=…, testable_by=…)` succeeds on a hypothesis,
  rejects on a non-hypothesis with a clear error, and appends to
  `meta.testable_by_history`.
- Hypothesis lints run on the new value; a lint-failing value is rejected
  whole (no partial write).
- Editing a signed hypothesis fails with the re-sign guidance.
- fi262718's two sharpenings can be applied via the CLI/MCP (manual
  verification against dev DB).

## Target + blast radius

Finding handler edit path (`tools/` finding edit + store op), hypothesis
lints, `nanopub/assemble.py` sign-state check. Draft-edit cascade
unaffected (findings are not corpus body chunks). Doc updates:
finding-edit section of the relevant skill.

## Open questions / decisions log

- Does `add_motivated_by` create a link row or a meta list entry? (follow
  whatever mint does today — parity, not new design.)
- History cap? (proposal: unbounded; these edits are rare and small.)
