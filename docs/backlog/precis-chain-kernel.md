---
status: ready
title: precis_chain — pure-numpy polymer-chain kernel (paths, RMF frames, curvature, capsules, clash, register, loops, bead relax, PDB)
prio: high
model: opus
---

# `precis_chain` — polymer-chain geometry kernel

## Motivation / why

Nothing in the repo can describe a chain: no splines, no rotation-minimizing
frames, no tube/sweep, no helix curve, no bend-radius rule, no capsule clash,
no PDB writer (`src/precis/cad/primitives.py` is rigid-transform CSG;
`src/precis_surface/` is periodic-surface machinery). DNA/RNA origami, walker
tracks and protein Cα traces all need the same chain arithmetic before any
chemistry. Precedent for the shape: `src/hexfold/` — numpy only, imports no
`precis*`, unit-agnostic, pure functions over passed-in arrays, boundary
enforced by `tests/test_hexfold_import_boundary.py`, with the domain binding
living elsewhere. This item is the kernel; `se-nucleic-acid` is the first
binding, `se-protein-chain-import` the second.

## In scope

Package `src/precis_chain/` (wheel package list in `pyproject.toml`; new
`[tool.importlinter]` root package + contract: `precis_chain` imports no
`precis*`/`asa_*`):

- `path.py` — `Path(points, tangents, s)`; `polyline`, `catmull_rom(waypoints,
  closed, samples_per_span, alpha=0.5)`, `hermite`, `resample_arc_length(path,
  step)`.
- `frames.py` — `rmf_double_reflection(points, tangents, r0)` (Wang et al.
  2008) → (N,3,3) columns (t,n,b); `apply_twist(frames, twist_per_length, s)`;
  `twist_between(a, b)` signed, wrapped to [−π, π).
- `curvature.py` — `discrete_curvature` (circumscribed circle of consecutive
  triples), `bend_radius`, `min_bend_radius_violations(points, r_min)`.
- `motif.py` — `Motif(name, rise, twist, radius, min_bend_radius,
  persistence_length, contour_per_unit)`; `units_for_length`,
  `length_for_units`, `turns`. No DNA numbers here — the binding owns them.
- `register.py` — `phase_after(motif, n)`, `commensurate(motif, n,
  pitch_units, tol)`, `crossover_positions(motif, n, lattice)`; per-unit
  rise/twist perturbation hook (insertions/deletions) reserved, unused.
- `envelope.py` — `Capsule(a, b, r)`; `capsules_along(path, radius,
  max_seg_len, max_turn_rad)` adaptive split; `capsule_pose(c)` → (origin,
  euler_xyz, length) with local +z = b−a.
- `clash.py` — `capsule_distance(a, b)` = segment–segment distance − (ra+rb);
  `clashes(caps, skip_pairs, tol)` with a uniform-grid broad phase.
- `fibre.py` — `unit_frames(path, motif, n_units, phase0)` per-unit frame +
  origin; `backbone_exit(frame, motif, azimuth)` where `azimuth` is the
  strand's angular offset in the bp frame (two values for a duplex; a third
  for a major-groove strand is the binding's business).
- `loop.py` — **contour convention: a loop of n nucleotides spans (n+1)
  backbone bonds**, so `contour(n) = (n+1)·c`; `loop_feasible(p, q, n, c)` ⇔
  `|p−q| ≤ (n+1)·c + tol`; `loop_slack_energy` (Gaussian chain, kT units);
  `loop_curve(p, tp, q, tq, n)` Hermite tangent to both backbones.
- `relax.py` — `relax_bundle(bodies, hinges, loops, pins, radii, iters)`:
  **rigid bodies per segment** (each a capsule with two end frames), hinge
  springs between consecutive bodies of one chain (bending stiffness from the
  motif), soft loop springs at contour rest length between backbone exits,
  pin constraints, capsule excluded volume; FIRE-style descent, idiom of
  `precis.structure.georelax.relax_graph`. Bead count = 2 × segments, not per
  unit.
- `pdb.py` — `write_pdb(elements, coords_A, names, resnames, resseq,
  chain_ids)`, `write_mmcif_atom_site(...)`, `read_trace(text, atom_name)`
  (PDB or mmCIF → chain_id → (n,3)).

Tests `tests/test_precis_chain_*.py`, theorems recomputed from returned
arrays; `tests/test_precis_chain_import_boundary.py` copies
`tests/test_hexfold_import_boundary.py`.

## Explicitly NOT in scope

- Any nucleic-acid or protein constant, vocabulary or op (→ `se-nucleic-acid`,
  `se-protein-chain-import`).
- scipy, trimesh or any new dependency. numpy only.
- Thermodynamics, sampling, knot/threading detection — `relax_bundle` is a
  mechanical settle and will pass a loop through a helix; the binding states
  this limit.
- Per-unit bead relax (12 k beads on a 7 kb design) — segment rigid bodies
  only.
- A constraint solver that chooses unit counts ("spacers are the slack",
  `src/hexfold/spec.md` §22.3) — later slice on the binding side.
- Rendering. Consumers project capsules/frames into `precis.cad` DSL or
  `precis.viz3d`.

## Acceptance criteria

- Helix `(r cos t, r sin t, ct)`: `discrete_curvature` → `r/(r²+c²)` within
  1e-3 rel at 64 samples/turn; `bend_radius` → `(r²+c²)/r`; RMF holonomy over
  one turn → `2π(1 − c/√(r²+c²)) mod 2π`, compared after wrapping, within
  1e-3 abs.
- Circle radius R: `min_bend_radius_violations(·, 1.01R)` non-empty,
  `(·, 0.99R)` empty.
- Planar S-curve: `|twist_between(first, last)| < 1e-6`; doubling samples
  shrinks frame error ≥ 3×.
- `apply_twist` at 34.286°/unit over 21 units → 720° within 0.1 rad; 32 units
  at 33.75°/unit → 1080° within 0.1 rad.
- `catmull_rom` passes through every waypoint; `resample_arc_length` spacing
  uniform to 1e-9 rel.
- `capsules_along` on straight L with `max_seg_len=L/4` → 4 capsules, every
  path sample inside their union; arc of bend radius `R_bend` at
  `max_turn_rad=5°` → chord sagitta ≤ `R_bend(1−cos 2.5°)` per capsule.
- `capsule_distance`: parallel at spacing d → `d−2r`; perpendicular crossing
  → `−2r`; `clashes` grid == brute O(n²) on 500 random capsules.
- `loop_feasible(|p−q|=3 nm, n=3, c=0.63 nm)` False (4×0.63 = 2.52),
  `n=4` True (5×0.63 = 3.15); `n=0` True iff `|p−q| ≤ c + tol`;
  `loop_slack_energy` monotone in n.
- `relax_bundle`: two rigid 21-unit bodies joined by a 2-unit loop whose
  backbone exits start 6 nm apart settle to exit-to-exit ≤ `3·c` (1.9 nm)
  while their axes stay ≥ 2r apart; an unloaded straight 8-body chain keeps
  bend radius > 1 µm; 400 bodies relax in < 10 s.
- `write_pdb` → `read_trace` round-trips coordinates to 1e-3 Å and chain ids.
- Import-boundary test + import-linter contract green; package importable in
  a venv with numpy only.

## Target + blast radius

New package only. Touches `pyproject.toml` (wheel packages, importlinter
`root_packages` + contract) and `tests/`. No handler, migration, worker,
deploy role or skill.

## Open questions / decisions log

- 2026-09-27 kernel name: `precis_chain` (mirrors `precis_surface`; neutral
  across DNA/protein/polymer). Decided.
- 2026-09-27 `pdb.py` lives in the kernel, not `precis/structure/export.py`:
  the protein importer needs the reader with no `Scene`; `structure` gets a
  10-line `to_pdb(scene)` adapter in the binding item. Decided.
- 2026-09-27 relax in the kernel (not only the binding): body/spring/capsule
  arithmetic is domain-neutral; the binding builds the body graph. Decided.
- 2026-09-27 (review) loop contour = (n+1)·c so a 0-nt crossover is feasible
  when exits are within one bond; relax bodies are segments, not units;
  precedent corrected to `hexfold` (`precis_surface` imports
  `precis.cad._mc_tables`). Decided.
