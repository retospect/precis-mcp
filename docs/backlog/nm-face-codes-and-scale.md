---
status: draft
title: nm slice 5 — face codes, fit/reject, and getting off O(N²)
prio: high
model: opus
---

# Face codes, fit/reject, and scale

Design session 2026-09-01 (Reto + agent), nano3d worktree. Sequenced
**after** slice 4b (LLM fill). Companion to `nm-kind.md` — that doc stays
canonical for the kind; this one holds the boxel-alignment slice.

Motivating consumer: the **boxel** programme (`draft:nano-computer`, quest
`qu161909`). A boxel is a purpose-designed cage implementing one primitive
(WIRE/NAND/memory/…), six wall panels + eight vertex pieces, an internal
cassette carrying the active function. **Reto's scoping corrections
(2026-09-01), binding on this slice:**

- Boxels are **hypothetical** — we are not implementing someone's fixed
  design, we are exploring the space.
- **Edge length is a free parameter, not 5–7 nm.** Boxels may be as small
  as 2–3 nm; *finding the floor is part of the job*. So the cage generator
  takes `edge_nm` as a parameter and we sweep it. (Note the coupling: the
  draft's 5 nm rotaxane cassette cannot fit a 3 nm cage — below some edge
  the switch must shrink to something like a diarylethene, or the cage
  itself becomes the bistable element. The sweep is how we find that
  honestly, rather than asserting it.)
- **No DNA face-codes.** The draft's DNA-anchor path is out of scope. Face
  codes here are either an abstract pattern on a hierarchical block's port,
  or a real **atomic surface pattern** — H-bond donor/acceptor arrays,
  steric knob/hole, charged patches. Both, ideally, as two views of one
  thing.

## Face codes — the two-level contract

**Abstract (on the port).** A face code is a small grid over the face (e.g.
4×4) over an alphabet {donor, acceptor, bump, hole, +, −, null}.
Complementarity is elementwise (donor↔acceptor, bump↔hole, +↔−). Fit =
overlay A on B in every relative orientation, score the matches: ≤4
rotations × 2 reflections × 16 sites is arithmetic, and it returns a
**graded** score (a binding proxy), not a boolean — which is exactly what
the annealer below needs as its energy term.

**Atomic (the realization).** A generator stamps the code as a real site
pattern on a face; fit/reject becomes geometric docking — rigid-align,
per-site type+distance match, clash via the cad SDF already used for
envelope clearance. `check_fit(face_a, face_b)` returns best orientation,
match score, clash volume.

The two levels **cross-validate**: the abstract code predicts the fit, the
atomic docking confirms it. Same L-level discipline as the rest of nm.

### The three theorems the checker must enforce

Not decoration — each is a bug generator if skipped (cf. the cone ω=0 seam:
symmetry the designer forgot).

1. **Orientation uniqueness.** A square face has C4 symmetry. A code with a
   nontrivial rotational stabilizer binds its partner in more than one
   orientation → misassembly. Compute each code's stabilizer; demand it be
   trivial.
2. **Chirality.** A face seen from outside is the mirror of the same
   pattern seen from inside. The complement rule must apply to the
   *reflected* grid, or every code pairs with the wrong enantiomer.
3. **Orthogonality margin.** For a code *set*: on-target score vs. best
   off-target across all pairs × all orientations. The **gap** is the
   assembly-error budget — Winfree sticky-end energetics generalized from
   1D sequences to 2D patterns. Generate maximal-gap sets greedily; store
   the margin as an invariant alongside b₁ and the disclination budget.

## Getting off O(N²)

`structure/validate.py` rule 1 is an honest O(N²) Python double loop. Fine
at ~300 atoms; wrong tool at a 10³–10⁴-atom cage; hopeless for a 4-boxel
assembly.

1. **Cell lists → O(N).** Cutoffs are ~2.5 Å, so bin into a ~3 Å grid and
   check 27 neighbouring cells. `generators/sp2.py::build_cnt` already does
   exactly this for bond detection — it just never got ported into
   `validate`. No new deps (ASE neighborlist is a fallback).
2. **Hierarchy as broad-phase.** Never compare atoms across blocks: test
   block *envelopes* first (SDF pairs / BVH over the block tree), descend
   to atoms only where envelopes touch. Two 10⁴-atom cages meeting on a
   face share ~N^(2/3) interface atoms — cross-block cost scales with
   contact **area**, not volume. This was always the nm bet; boxel is the
   first customer that requires it.
3. **Incremental revalidation → O(k).** Scene is in-memory; keep the hash
   live, recheck only the neighbourhood of the k atoms an edit touched. For
   LLM edit loops (hundreds of small ops) this matters more than the
   asymptotic class.

## Annealing — anneal blocks, never atoms

State = placement + orientation of each typed block on a lattice. Energy =
Σ face-code match scores. A Metropolis move (swap/rotate one block) changes
only its 6 face terms ⇒ **ΔE is O(1) per move**, independent of assembly
size. Run it rejection-free/KMC (event queue, O(log M) per event) and it
yields assembly **kinetics** — nucleation, misbinding frequency vs. code
margin — i.e. the yield/error-rate numbers `qu161909` wanted. Atoms are
instantiated only after the block-level anneal settles; the existing relax
ladder handles local geometry from there.

**No potential is ever called inside the anneal loop.** Face-pair
interaction energies are computed once per code motif at the best
affordable fidelity and cached into a table; the O(1) move property is
table lookup.

Net: no stage is quadratic, and every stage emits a legible invariant the
next stage can check.

## Scope of this slice

- `face_code` attribute on ports + complementarity check in the existing
  name-keyed connect path (cheap: the connect machinery exists).
- Code-set designer: stabilizer check, chirality-correct complement,
  greedy maximal-margin set generation, margin as a stored invariant.
- `check_fit(face_a, face_b)` atomic docking op.
- Parametric cage generator taking `edge_nm` + `code`, for the size sweep.
- The three scale fixes above.
- Block-lattice annealer + KMC, energies from the cached motif table.

Deferred/adjacent (not this slice): conjugated-path probe for WIRE boxels
(graph reachability over order ≥1.5 bonds face-to-face, redundancy from the
existing min-cut machinery — answers the draft's "bulk pellet conductivity
doesn't validate boxel-level function" complaint with a per-design
readout); MOF/COF wall generators; DNA anything.

## Blocked on / see also

The **DRC and MLIP gaps** this slice depends on are filed as gripes
(2026-09-01): **gr285774** (ml rung is materials-only with
`dispersion=False`; preflight gates elements not chemistry) and
**gr285775** (no formal charge; metal coordination unchecked). Face-code
*binding energies* are meaningless until at least the dispersion gap is
closed, so do not compute motif tables before then.
