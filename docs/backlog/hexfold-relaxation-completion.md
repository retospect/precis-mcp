---
status: draft
pillar: 3d-design
title: hexfold relaxation completion — relax the r5 blocks without collapse, show relaxation state in viewer and MCP
---

# Hexfold relaxation completion — open remainder

Provenance: Reto's completion request 2026-10-07 (gr472846): relax the saved
`hexfold-dogfood-r5` blocks as additional candidates, preserve originals, show
the real relaxation state in viewer and MCP, recommend the next phase. Rule
(Reto, 2026-10-13): **we should relax, and the structure should not collapse.**

## Already on main

- Read-only completion state and wire contract (cefcce1bd):
  `src/precis/structure/completion*.py`, `src/precis_se/completion.py`
  (`se_completion_envelope`), `Store.structure_completion_snapshot`, fixture
  `tests/fixtures/structure/completion-wire.json`. Types, identity hashing,
  aggregation and periodic comparison are settled there; the types module and
  fixtures are the record.
- `fidelity='xtb'` rung in `src/precis/structure/relax.py` (669d615cf): tblite
  GFN2-xTB, Te 1000 K, FIRE, fmax 0.05 eV/Å, in-process only, refuses `cell=`,
  raises `Unsupported` where tblite is absent (linux-aarch64 cluster). Replaces
  the old cached-MACE-small baseline (Reto, 2026-10-11: free GFN2-xTB relax,
  H caps kept).
- Relax campaign 2026-10-13: table in `## Relax campaign 2026-10-13` of
  [hexfold-toolkit](threads/hexfold-toolkit.md). Takeaway: fins converge as
  tetrahedral grafts, the sp2 120° seam relaxes into an sp3 dimer chain, the
  open-pole nanobud H crown holds. The 2026-10-07 MACE pilot (`fi473095`,
  `fi473189`, `fi473386`) never converged in 300 steps.

## 1. Relax the r5 blocks without collapse

Blocks of `hexfold-dogfood-r5`: `ball12` (2,406 atoms), `ball12r11` (2,466),
`cyl12open` (1,950). Relax each **whole** as a separate versioned candidate;
originals untouched. Inspect real connectivity before separating anything; a
combined-scene relax is justified only for real inter-block contacts (the
separated display poses are not an interacting device).

Acceptance, all of:

- converged at fmax 0.05 eV/Å (a step/time-capped stop is attempted, not
  converged; force convergence exactly at the last allowed step counts);
- no collapse versus the authored geometry: tube radius profile (mean and
  spread along the axis), ball sphericity/radius, no flattening or bending
  beyond tolerance, no new close contacts or bonds outside the seam
  dimerisation (1.59–1.64 Å pairs, per the campaign), joint shape kept;
- bond-list identity is not evidence: compare geometric coordination and
  contacts, H caps included.

Route under consideration (unruled): GFN-FF (`xtb --gfnff`) whole-block
pre-relax in minutes, then GFN2 on H-capped joint/seam fragments of 500–900
atoms (1–4 h each on the Mac), plus one overnight full-block GFN2 on the Mac
as verification. Full-block GFN2 is ~10–20 h on the Mac, 1–4 days on a DGX
Spark CPU; no Metal backend; an MLIP on GPU only as a screen. A 30-min Mac
timing probe on `cyl12open` is proposed, waiting on Reto.

If a block collapses under a local minimiser from the authored geometry, that
is a design finding (diameter / wall stiffness; cf. the pillar-diameter note in
the toolkit thread), not a relax-settings problem. Report it as such.

### Learning from the run

Record before/after snapshots with atom identities; method, backend,
precision, optimiser settings, runtime; initial/final energy and max force and
stop reason; composition and connectivity; bond-length/angle distributions,
coordination, rings, all clash families including H caps; POAV summaries.
Never read a missing metric as zero. Align equivalent atoms for displacement
without fitting away the deformation. Surface deviation from the authored
target only when a receipt exists (legacy r5 has none: report unavailable).
Report constrained and free relaxation separately. A minimum of a model is not
measured stability, finite-temperature robustness or DFT.

## 2. Completion policy and level ladder

Generated / relax-attempted / converged / post-validated are distinct states.
Timeout, step exhaustion, failure, unavailable backend or hold never becomes
converged. Never infer relaxation from labels, filenames, prose or stick
preview (r5 labels say `fidelity=stick`, tethered: spring preview only).

| level | what |
|---|---|
| preview | stick/clean, constraints visible; readable geometry only |
| screening | `fidelity='xtb'` (GFN2-xTB, FIRE) to fmax < 0.05 eV/Å, then the collapse check above |
| refinement | tighter 0.02 eV/Å only if residual forces or observable changes matter |
| independent validation | separately suitable method (DFT) for a scientific claim; separate approved phase |

Levels depend on method, convergence and constraints, not one `relaxed` flag.
For constrained runs record raw and unconstrained forces so frozen atoms cannot
fake a free-relaxation claim.

## 3. Viewer

No relaxation-state UI exists in `src/precis_web/` (checked: `item_view.py`
shows only run count and last successful rung; `topology-cloud.js` is the
unrelated graph layout). Consume the shipped completion contract:

- per-block state badge: unknown, preview-only, queued, running, converged,
  stopped-unconverged, failed, held/unavailable;
- method / model / force target vs achieved, steps, constraints, run and
  candidate ids, original version; details on request;
- before/after toggle or comparison; progress and terminal reason while
  running;
- assembly summary that exposes mixed block states;
- editing coordinates or bonds invalidates the current version's status
  without erasing history.

## 4. MCP

When a generation or finish response leaves preview-only geometry, recommend
the next relax phase (rung, target, resource estimate) or state the hold.
Reuse structure history/stats and `se_completion_envelope`; keep seven verbs;
JSON fields additive only.

## 5. Worker writeback correctness

Still true: `src/precis/workers/job_types/struct_relax.py` records the run with
`converged=bool(scalars.get("converged", True))`, and its success event reads
"converged" unconditionally. Missing convergence must default to not
converged, and the message must follow the flag. Legacy external-import rows
and scratch-derived structures need explicit evidence provenance; legacy
success is not retroactive proof.
