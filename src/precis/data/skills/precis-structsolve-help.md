---
id: precis-structsolve-help
family: se
title: precis — structsolve, the in-tree structural solvers (unilateral equilibrium, completeness, bistability)
summary: the precis.structsolve Python library — solve_complementarity (small-displacement equilibrium of a pin-jointed network whose members are tension-only / compression-only / bidirectional / must-contact), check_completeness (per-load-case verdict complete / unseated / incomplete / mechanism), probe_bistability (are two free-length assignments two stable equilibria of one structure, plus an energy-difference estimate), trace_equilibrium_branch (nonlinear continuation, real snap-through barriers); pure arrays in, unit-agnostic; only form-finding is reachable through a verb (se op formfind)
answers:
  - how do I find the equilibrium of a cable/strut network where cables can go slack?
  - what do the complementarity statuses taut / slack / bearing / separated / seated / unseated mean?
  - what does a mechanism verdict mean and how do I find the unresisted node and axis?
  - why is a tie that drives into compression reported incomplete rather than as a stressed member?
  - what does bistable=True mean in probe_bistability, and why are two free-length states usually not bistable?
  - is the probe_bistability barrier a real snap-through barrier?
applies-to: Python library precis.structsolve (no verb of its own); the se op formfind wraps form_find
kinds: [se]
status: active
tags: [design]
---

# precis-structsolve-help — in-tree structural solvers

`precis.structsolve` is a Python library: numpy arrays in, dataclasses
out, no store access, unit-agnostic (metres/newtons and ångström/nN give
the same verdicts). Only form-finding has a verb: the se op `formfind`
(see `precis-se-help`). Everything below is for code that imports the
library.

## Array conventions

| argument | shape | meaning |
|---|---|---|
| `coords` | `(j, 3)` | node positions — the linearisation point, not reshaped |
| `members` | `(b, 2)` int | node index pairs; no self-loops |
| `rate` | `(b,)` | axial stiffness, force/length, > 0 |
| `free_length` | `(b,)` | unstressed length, > 0; prestress `t0 = rate·(L − free_length)`, tension positive |
| `idiom` | `(b,)` object | `tension_only`, `compression_only`, `bidirectional`, `must_contact` |
| `fixed` | `(j, 3)` bool | **True = prescribed** coordinate; a node may ride a rail |
| `loads` | `(j, 3)` | external nodal force, default zero |

## solve_complementarity — unilateral equilibrium

```python
import numpy as np
from precis.structsolve import solve_complementarity, ComplementarityError

coords = np.array([[0, 0, 1.2], [0, 0, 0]], float)
fixed = np.ones((2, 3), bool); fixed[0, 2] = False   # node 0 free in z only
loads = np.zeros((2, 3)); loads[0, 2] = 5.0
r = solve_complementarity(coords, np.array([[0, 1]]), np.array([100.0]),
                          np.array([1.0]), np.array(["bidirectional"], dtype=object),
                          fixed, loads)
r.displacements[0, 2]   # -0.15: the bar settles at 1.05, rate·0.05 = 5
r.forces                # [5.0], tension positive; 0 for every inactive member
r.status                # per member, see table
r.residual              # solve-quality check, ~0
```

| idiom | engaged | not engaged |
|---|---|---|
| `tension_only` | `taut` | `slack` |
| `compression_only` | `bearing` | `separated` |
| `must_contact` | `seated` | `unseated` (a failed hard stop) |
| `bidirectional` | `taut` / `bearing` by force sign | — |

- Engagement is read off the solved force. A one-sided member installed
  on its illegal sign (a tie shorter than its free length) counts as
  engaged only if the solve stretches it strictly past its free length.
- Small-displacement model: member directions are fixed at `coords`;
  prestress stiffens transverse directions only, never the member axis.
- `ComplementarityError` = no complementary equilibrium: the active set
  cycles, or its reduced stiffness is singular. A singular refusal names
  the unresisted free coordinates, e.g. `unresisted free coordinates:
  node 0 x`.
- `ComplementarityInputError` (a subclass) = malformed request — fix the
  call, never treat it as physics.

## check_completeness — per-load-case verdicts

```python
from precis.structsolve import check_completeness
cases = np.zeros((2, j, 3))   # (n, j, 3); an all-zero case checks prestress alone
rep = check_completeness(coords, members, rate, free_length, idiom, fixed, cases)
rep.complete                       # True iff every case is "complete"
[(c.verdict, c.culprits, c.unseated, c.cause) for c in rep.cases]
rep.notes                          # what was and was not checked — surface these
```

| verdict | meaning | next move |
|---|---|---|
| `complete` | a legal-sign equilibrium exists; a slack cable is fine | — |
| `unseated` | it solves, but a `must_contact` stop let go (`unseated` lists them) | more preload or a different stop |
| `incomplete` | no legal equilibrium, but all-bidirectional carries the case; `culprits` = one-sided members driven onto their illegal sign | re-sign or brace the culprits |
| `mechanism` | even all-bidirectional fails; `cause` names the unresisted `node <i> <axis>` | add a member or support on that coordinate |

`complete` means a load path exists, not that members are sized: no
strength, buckling-capacity or cable-sag check.

## probe_bistability — one structure, two equilibria?

```python
from precis.structsolve import probe_bistability
res = probe_bistability(coords, members, rate, free_length_a, free_length_b,
                        idiom, fixed, loads, barrier_samples=21)
res.bistable        # one structure has two distinct stable equilibria
res.states          # (A, B): .solved, .stable (positive-definite tangent), .result, .notes
res.barrier         # |E_a − E_b| estimate when both states are stable, else None
res.notes           # the verdict's reason + the barrier method and its limits
```

- `bistable=True` requires both states stable and distinct, and at
  least one state also a stable equilibrium of the other's free
  lengths. Two assignments that each have their own single equilibrium
  (a photoswitch's trans and cis lengths, two prestress levels) are two
  structures: `bistable=False`, notes say "different structures".
- `stable` is second order: a state can solve yet be unstable when
  compression prestress drives a transverse stiffness negative.
- The barrier is always exactly `|E_a − E_b|` (a straight-line path in
  a linear model is convex) — an actuation-energy scale, never a
  snap-through barrier.

## trace_equilibrium_branch — real snap-through

```python
from precis.structsolve import trace_equilibrium_branch, barrier_over_kT
nl = trace_equilibrium_branch(coords, members, rate, free_length_a, free_length_b,
                              idiom, fixed, steps=200)
nl.limit_point                     # fold: kind 'newton_failure' | 'reaction_extremum', or None
nl.forward_barrier, nl.reverse_barrier, nl.escape_barrier   # None when monostable
barrier_over_kT(nl.escape_barrier, kT)                       # same energy units as kT
```

Geometrically nonlinear: member directions follow the displaced
geometry, so folds and interior energy maxima are visible. Use it
whenever the question is "does it snap, and how high is the barrier";
`probe_bistability` cannot answer that.

## Other solvers in the package

| function | does |
|---|---|
| `form_find` | force-density form-finding: geometry from topology, force densities and anchors (se op `formfind`) |
| `simp_optimize`, `lattice_fill`, `overhang_violations` | voxel SIMP topology optimisation and gyroid fill — screening estimates, propagate their `notes` |
| `evaluate`, `penalty`, `well` | preferred-number (round-value) penalty over declared measurements |
