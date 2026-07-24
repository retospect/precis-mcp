---
status: draft
title: Close the CAD Ask loop — diagnose to apply
model: sonnet
---

# Close the CAD Ask loop — diagnose to apply

## Motivation / why
CAD "Ask>" (`cad_discuss`) can diagnose a design and describe exact fixes in
prose, but has no path to apply them — the user has to retype the fix as a
fresh Propose request. The two halves already exist in the code; they are
structurally disjoint, not just missing a button:

- `cad_discuss` (`src/precis/workers/job_types/cad_discuss.py`) is read-only
  by construction. Its prompt explicitly instructs the model not to output a
  full rewritten source or a diff — explain in prose only, referencing the
  model. Its result payload is unstructured `{answer, instruction,
  cad_ref_id}`: free-form prose, no JSON contract, no `dry_run()` validation.
- `cad_apply_in_place` (`src/precis_web/routes/cad.py`, `~L708`) resolves its
  source via `_proposal_by_job`, whose SQL hard-filters `meta->>'job_type' =
  'cad_propose'`. A `cad_discuss` job id is invisible to that query — passing
  one returns `None` and the route 400s.
- `cad_propose` is the only producer of an apply-able payload: an enforced
  `{source, rationale}` JSON contract, `dry_run()`-validated (parse + build)
  before a human sees it, with validity gating whether the Apply buttons
  render.

So diagnosis dead-ends: the user gets a correct prose answer from Discuss but
must manually re-describe the fix to Propose to get something applyable.

## In scope
A discuss-to-propose bridge that reuses `cad_propose`'s existing
validated/reviewed/explicit-Apply pipeline — not a new apply path. Concretely:
some "Propose a fix from this" affordance on a discuss turn that mints a
**new** `cad_propose` job, carrying the diagnosis as context, rather than
making `cad_discuss` itself emit applyable source.

## Explicitly NOT in scope
- Making `cad_discuss` emit applyable source or a diff — this would break its
  "read-only by construction" invariant and blur the Discuss/Propose
  distinction.
- Dropping or weakening `cad_discuss`'s read-only invariant.
- A new apply path that bypasses `dry_run()` validation — all applyable
  source must still go through `cad_propose`'s existing validate-before-apply
  gate.

## Acceptance criteria
- From a discuss answer, one action launches a validated `cad_propose` job
  carrying the diagnosis as context.
- The existing Apply / Apply-in-place review flow (validity gating the Apply
  buttons, `_proposal_by_job` lookup) is unchanged.

## Target + blast radius
- `src/precis_web/routes/cad.py` — `_proposal_by_job`, `cad_apply_in_place`,
  the discuss/propose job-launch routes.
- `cad_discuss` / `cad_propose` job types
  (`src/precis/workers/job_types/cad_discuss.py`, `cad_propose.py`).
- `cad/detail.html.j2` template — the discuss-turn affordance.

## Open questions / decisions log
1. Auto-fire the `cad_propose` job from the discuss turn, or just pre-fill +
   focus the existing Propose input for the user to submit (keeping today's
   opt-in click but removing the retype)?
2. Should the discuss Q&A be folded into the propose prompt as context?
3. Is any change to the "Discuss = read-only, proposes nothing" framing
   (docstring, panel copy) in scope, or must Discuss stay strictly read-only
   with loop-closing done entirely as a launch-a-Propose shortcut?
