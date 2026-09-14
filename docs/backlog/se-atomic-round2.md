---
status: draft
title: se atomic mode round 2 — apply + objective verdicts (nm-kind.md follow-on)
prio: medium
model: opus
---

# se atomic mode round 2 — apply + objective verdicts

Follow-on to `nm-kind.md` (the original nm-kind design doc, 2026-08-31),
carried forward by `nm-se-merge.md`'s Decided entry (2026-09-14): the
nm→se merge window stayed mechanical (fold the shipped domain layer into
`se`'s atomic mode, retire the `nm` kind) and explicitly trailed
`nm-kind.md`'s unshipped round-2 content — objective verdicts and Apply —
as this follow-on rather than building it mid-merge. This file is that
doc, renamed in place; the shipped-slice history it used to carry lives
in `git log` / the merge doc, not here.

## Already shipped (context, not this doc's scope)

- **`se_propose_atomic` job** (`src/precis_se/atomic/propose.py`, the
  merge's rename of `nm_propose`) — a tool-less propose-only job:
  targets one block, reads its envelope/ports/objectives/validate state,
  returns a candidate fragment (SMILES or an existing `structure` slug),
  a `structure` op script realizing it, and a port→atom map, having
  already applied those ops to a scratch scene and run the DRC. It never
  writes anything.
- **`view='literature'`** — deterministic (no-LLM) paper-search query
  from a block's own vocabulary, transferred verbatim.
- **Filled-fraction honesty** on `view='validate'` — an unfilled scaffold
  reads as unfilled, never as a false pass.
- **Generators** — `cnt`/`fullerene`/`cone`/`cyclodextrin`, the
  deterministic fill path (`src/precis_se/atomic/generators/`).

## In scope — unshipped

1. **Apply.** Turning an accepted `se_propose_atomic` proposal into a
   real design is still entirely manual (run the returned op script
   yourself, then `bind_structure`). Apply would do it in one step: mint
   the `structure` design from the job's op script, record `derived-from`
   lineage back to the job, then `bind_structure` it onto the target
   block. Open: does Apply auto-run a `clean` relax on the freshly minted
   structure (leaning yes — an unrelaxed proposal is a worse starting
   point than the dry-run scratch scene already checked); may a single
   proposal target multiple blocks when they share a template (leaning:
   propose against the template once, instances inherit the fill, same
   rule instancing already uses for envelope/ports/dof).
2. **Objective verdicts.** A connect's `objectives={'role': ...}` vector
   is currently declared-intent only — never checked once both sides are
   filled. Distance/geometry objectives ("crown 2–3 Å from rim") should
   compile to `structure` measures on the bound scene (the verdict
   machinery — target/tolerance, hard/soft/gauge — already exists in
   `structure`, this is wiring, not new machinery). Non-measurable
   objectives (e.g. `low_rotational_barrier`, which needs the mechanism
   analysis phase below) should render as `deferred`, not silently pass.

## Open question carried from the merge

- **`can_own_jobs`.** `SeHandler.spec.can_own_jobs` stayed `False`
  through the nm→se merge — byte-identical to the retired `nm` kind's own
  setting, not a considered choice for `se`. "Jobs parented on `se`
  designs" (an `se_propose_atomic` job filed as a child of the design it
  targets, discoverable from the design side, rather than floating
  unparented) is a new affordance this round could pick up — Apply's
  `derived-from` lineage above is exactly the kind of edge that would
  want it. Undecided; resolve before building Apply, not after.
- **`expected_hybridization` is display-only** (found in the merge's
  ship review): `bind_structure` gates only `expected_element`; the
  hybridization field is echoed in views/propose output but never
  compared against the bound atom — carried over unchanged from nm.
  Migration 0007's comment claimed otherwise. Decide whether to gate
  it (loud-failure-at-bind philosophy says yes) or document it as
  advisory-only.

## Later phases (unstaffed, still true)

- **Charge/optical panel** — Gasteiger/EEM partial charges, dipole, crude
  polarizability/chromophore flags; ladder to xtb/DFT rungs on the GPU
  node.
- **Mechanism analysis** — rotational-DOF torsion scan via the relax
  ladder (barrier profile about a declared axle), interlock verification
  via an SDF sweep, force/compliance chains from relaxed-geometry deltas
  under applied constraint displacement.
- **Dynamics** — no MD engine in-tree; rent one as a container job,
  consume trajectories as measure time-series over declared DOF.
- **Synthesizability** — `route` retrosynthesis as an advisory cost term
  / gate on chosen fragments and, later, the assembled design.
