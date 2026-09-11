---
status: draft
title: structural solution space — tensegrity checking + in-tree form-finding + SIMP generative fill, on the existing kinds
prio: high
model: opus
---

# Structural solution space

Design session 2026-09-09 (Reto + agent). Reto's ask: *"atomic and physical
level planning for tensegrity like structures — building block solver etc, as
part of the solution space we can have"*, *"integrate with the existing
kinds"*, and *"the ntop thing that makes cool bone organicy solutions — maybe
with 3d constraints"*.

This doc EXTENDS two existing designs of record and must never fork them:

- `se-tension-elements-and-prestress.md` owns the tensegrity *model* (one
  axial member with an asymmetric capacity pair; the rung ladder; the
  mobility tripwire contract shared with `cad-machine-spec.md`). This doc
  re-opens exactly one of its deferrals — form-finding — with Reto's
  explicit decision (2026-09-09): **the solvers live in-tree** (numpy/scipy,
  the pcb-SA posture), not in an external engine repo.
- `blocktree-library-build-plan.md` owns the block library (cross-design
  instancing, discrete states, ranked search). The molecular use case below
  *consumes* its slice 2; it does not restate it.

Decisions made 2026-09-09: solvers in-tree; nTop leg = **density-field SIMP**
(voxel FEA, not the ground-structure shortcut); slice 1 = the axial member +
the Maxwell/Calladine classifier (shipping with this doc).

## The two use cases are the spec

**Steel pipes and cables (se, metres).** A pipe strut is an se block (cad
envelope, `realized-by` a `component` in the `pipe`/`profile` category); a
cable is a connect with the new `axial` kinematic class, tension-only
capacity pair, `mechanism: cable` (demands a BOM line — a cable joint with
nothing to buy is a drawing). The classifier answers the question that
defines the family: rigid / mechanism / **prestress-stabilized**. Prestress
magnitudes arrive at rung 5 (preload facet); slice 1 already checks
*sign-feasibility* of the self-stress state against each member's capacity
pair, which is the tensegrity-defining check (cables must end up in tension,
struts in compression, in the null-space state).

**Azobenzenes and bistable structures (nm, Å).** A photoswitch block gets
discrete states (`{trans, cis}`, blocktree plan slice 2) whose Δ(end-to-end)
changes an axial member's free length. The composition is the payoff:
**state-dependent stability** — run the classifier once per declared state.
A tensegrity whose tie is a photoswitch is an *actuated* tensegrity;
actuation = the equilibrium/prestress state changing between block states,
and "does the cis frame still hold shape" is the same `m − s` question asked
twice. Physics stays in `photoswitch-states-and-spectral-dof.md`; nothing
about spectra or fatigue is duplicated here.

## One axial-member model, two capacity sources

The capacity pair `(tension_capacity, compression_capacity)` is the
cross-scale contract:

- at metres, the numbers come from `component` specs (breaking load;
  Euler/crush on the bound profile) — sourced, procurement-side;
- at Å, they are *computed*: `precis_nm.mechanics.min_cut` (tension
  ceiling) and `euler_buckling_ceiling_nN` (compression), advisory
  pristine-lattice ceilings under the nm `HONESTY_NOTE`.

Same model, different provenance and honesty tier. Per the prestress doc's
settled lean: **share the shape, not the module** — revisit at three
consumers.

## The solvers: in-tree, and where they live

New core package `src/precis/structsolve/` — numpy/scipy only, **no store
access** (the cad posture: pure functions over passed-in data; handlers own
IO). Two engines:

- **`formfind.py` — force-density form-finding.** Given topology (members +
  anchors) and force-density ratios, one sparse linear solve returns node
  geometry in equilibrium. Unit-agnostic (the blocktree discipline); se
  feeds metres, nm feeds Å. This is the *generator* whose output slice 1's
  classifier *checks* — the generator/checker split is why form-finding was
  once deferred out of se, and keeping the module outside `precis_se`
  preserves that separation while still being in-tree.
- **`simp.py` — 3D density-field SIMP (the nTop leg).** Domain = a **cad**
  keep-in expr voxelized by `cad.relate.component_sdf` sampling (keep-outs
  the same way — the "3d constraints"); loads/supports from se block
  declarations (`objectives.force`, `objectives.fixed`); 8-node hex FEA
  with a scipy.sparse CG solve; SIMP penalization + sensitivity/density
  filtering; returns the density field + compliance history under an
  iteration budget. **Advisory tier, never a hard DRC** — a compliance
  number from a voxel model is an estimate, and the honesty header says so.

The equilibrium-matrix classifier itself stays in `precis_se/stability.py`
(one consumer today; extraction into structsolve happens when nm's
state-dependent stability lands and actually calls it).

## Storage of solver output

- **Form-found geometry** writes back as ordinary block poses stamped
  `origin: 'proposed'` (the 0005 origins facet) — a human-set pose is
  contract and is never overwritten. No new storage.
- **SIMP density fields are not blocks.** Store a content-addressed run
  summary (inputs hash incl. voxel pitch and engine version, compliance,
  volume fraction, iterations) on the se ref's meta — the pathway
  `results_json` posture. The field itself is *recomputable* (the structure
  relax run-cube precedent: converged run = cache hit); render section
  previews through the existing SVG path. **Open question for Reto:**
  whether the full field wants a `folder`-kind artifact once the
  sandbox-harvest slice exists — deferred, not decided here.

## Solution-space integration

Solver outputs are minted as ordinary se/nm designs — candidates, linked
`serves` → a quest — and `quest/frontier.py` Pareto-ranks them against
human-set `rubric_objectives` (mass via `view='bom'`/`view='mass'`,
compliance, member count, cost). The ranked library search is blocktree plan
slice 4, unchanged. House discipline carries: **weights are human-set; a
solver may not tune its own objective.**

## Assembly honesty

A tensegrity cannot be assembled one member at a time against a rigid
partial assembly — every intermediate state is a mechanism. When
`se-feasibility-and-cost.md`'s assembly-order existence check lands, a
prestress-stabilized design must report "requires simultaneous tensioning /
a jig / a tensioning sequence" as a **cost**, never an infeasibility.
(Carried forward from the prestress doc so the feasibility implementer
cannot miss it.)

## Deliberately deferred, named so they are not re-derived

Dynamics/vibration of prestressed structures; cable sag/catenary; creep and
stress relaxation (a `material` question first); buckling FEA;
multi-material / multi-load-case SIMP; marching-cubes mesh export of the
density field; form-finding for non-axial continua; port-offset node
positions in the classifier (v1 pins nodes at block poses).

## Build order

1. **Slice 1 (this ship):** `axial` kinematic class + capacity-pair params;
   `fixed` support objective; `stability.py` (equilibrium matrix, one SVD →
   `m`, `s`, sign-feasibility, Pellegrino–Calladine second-order test);
   `view='stability'`; DRC capacity findings. Satisfies the two-party
   tripwire contract by construction.
2. **SHIPPED 2026-09-09:** force-density form-finder
   (`precis/structsolve/formfind.py`, pure/unit-agnostic, per-coordinate
   anchoring, loud singular refusal) + the se `formfind` op
   (`precis_se/formfind.py`): anchors from `objectives.fixed`, role-default
   force densities (tie +1 / strut −1 / rod +1; undeclared demands an
   explicit `q`), per-member `q=[{'a','b','q'}]` overrides, solved poses
   stamped `origin: 'proposed'` — by default only already-`proposed` poses
   move; `move=[...]`/`'all'` authorizes more; collapse refused, nothing
   written.
3. **SHIPPED 2026-09-10:** null-space prestress DRC
   (`stability.prestress_report`): declared `preload`s must be a
   self-stress state (balance at every free node, within 1 % of the
   largest declared value); undeclared members completed by least
   squares, implied forces vetted against role sign + capacity pair;
   prestress section in `view='stability'`, warn-tier `prestress_state`
   DRC rule. Rung 5's other half — bolted-joint load-sharing/separation —
   remains open in the prestress doc.
4. SIMP engine + cad-domain voxelization + run-summary storage.
5. nm state-dependent stability (blocked on blocktree plan slice 2 states).
6. `spring` component category (prestress doc rung 3, DIN 2098/2095).
