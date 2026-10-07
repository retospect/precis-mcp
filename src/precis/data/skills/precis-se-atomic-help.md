---
id: precis-se-atomic-help
title: precis — designing chemistry as a block tree (atomic mode)
summary: atomic mode extends an se block tree down to real chemistry — declare_threading/declare_dof record intent, bind_structure maps ports to atoms in a structure design, generate mints canonical fragments (cnt/fullerene/cone/cyclodextrin/hexfold/hexfold_scene/tpms/smooth_drum) with no LLM, view=mechanics gives advisory continuum ceilings, view=literature runs a deterministic paper search, and view='validate' carries the chemistry-tier findings (port_capability, dangling_binding, binding_element_mismatch, envelope_fit, connect_cycle, bond_length_sanity, bond_vector_alignment, composite_part_stolen)
answers:
  - how do I design a molecular machine as nested blocks before filling in real chemistry?
  - how do I record that a macrocycle is threaded onto an axle?
  - how do I bind a block's ports to real atoms in a structure design?
  - how do I find literature for a block before filling it with real chemistry?
  - how do I check whether a bound block's atoms actually fit its declared envelope?
  - how do I build a nanotube, fullerene, or cyclodextrin block without hand-placing every atom?
  - where are the nm molecular blocks (the old kind='nm')? — merged into se atomic mode; there is no separate nm kind or precis-nm-help
  - what does view='mechanics' actually check, and why is it never a gate?
  - why did my atomic design's `envelope_fit` check fail even though the atoms look fine?
applies-to: get/edit (kind='se'); read precis-se-help first for the op grammar
status: active
tags: verbs, design
kinds: se
---

# precis-se-atomic-help — chemistry inside the block tree

Atomic mode marks a block's realization as chemistry rather than a solid:
thread macrocycles, declare degrees of freedom, bind ports to atoms in a
`structure` design, and check the result with atomic-only views. Read
[[precis-se-help]] first for the op grammar.

## Atomic-mode ops (exact parameter lists)

- `declare_threading` / `remove_threading` — **atomic mode.** `a`, `b`
  (req): block `a` is threaded through block `b` (a macrocycle on an
  axle). Directional, per-pair, stored not derived from poses — the
  same rule `pcb` uses for its combinatorial embedding. Mutual
  threading (`a` through `b` *and* `b` through `a`) is rejected as
  physically impossible; fix a wrong-direction declaration with
  `remove_threading`, not by declaring the opposite pair on top.
- `declare_dof` / `clear_dof` — **atomic mode.** `block`, `kind`
  `rotational|translational`, `axis_ports` [exactly two of the block's
  **own** ports] (req). Records intent only — no torsion scan, no
  barrier estimate.
- `bind_structure` / `unbind_structure` — **atomic mode.** `block`,
  `design` (a `structure` slug), `ports` (`{port: atom_label}`, or the
  object form `{port: {'atom': atom_label, 'axis_atom'?: label,
  'phase_atom'?: label}}`) — every mapped port must exist on the block,
  every atom label must exist in the structure, and a port's
  `expected_element` must match the bound atom's element (a loud
  rejection at bind time). `axis_atom`/`phase_atom` are both-or-neither
  and `axis_atom` may not equal `atom` — one alone, or a degenerate
  (collinear) triple, is a `BadInput` naming the atoms. Binding again
  to the **same** design is incremental; binding to a **different**
  design first clears every port binding on the block. Both target an
  ordinary block only — bind via the template for an instance.
  `unbind_structure` clears a block's binding and every one of its
  ports'. A bind also **measures**: each mapped port takes its atom's
  block-local position as its own `pose` (`pose_source='bound'`, metres)
  — but only into an empty slot or over an earlier bind's measurement. A
  `pose_source='declared'` target is design intent and is never
  overwritten; a real disagreement is reported instead (echo line +
  `port_pose_mismatch`). Mapped with `axis_atom`/`phase_atom`, the SAME
  bind also measures the port's `rot` — z is the unit `atom→axis_atom`
  axle, x is `atom→phase_atom` projected off it — under `rot`'s OWN
  `rot_source`, independent of `pose_source` (a declared pose with no
  declared rot still gets its rot filled, and vice versa): a declared
  `rot` (`rot_source='declared'`, or, absent one, a declared `direction`,
  which carries no provenance of its own) is compared instead, and a
  disagreement past 10° is `port_rot_mismatch` (echo line + standing
  finding). Nothing is measured when the scene doesn't share the block's
  frame.
  `unbind_structure` drops the measured pose/rot, keeping declared ones.

## Atomic-mode ops — `generate` (exact parameter list)

- `generate` — **atomic mode.** `generator` `cnt|fullerene|cone|
  cyclodextrin|hexfold|hexfold_scene|tpms|schwarzite|smooth_drum`, `params` (dict), `name` (new block) · `parent`/`pose`/
  `rot` passthrough. One op = a canonical block whose atoms follow from
  math, no LLM: mints a `structure` design at `<design>-<name>` holding
  the generated atoms, adds the block (envelope + ports + topology
  facts), and binds it — the echo names the minted slug. A `structure`
  design already living at the target slug is a loud rejection —
  `generate` never overwrites. **Every hexfold-family generator
  (`hexfold`, `hexfold_scene` and its Y/fin entries) H-terminates open
  edges as its last step by default** (Reto 2026-10-07): after the relax
  and the judgement, every carbon short of its valence gets an H at
  1.09 Å along its missing bond; the block carries
  `topology.terminated` (`element`, `mode`, `count`, `hosts`) and the
  structure the tag `terminated:h`; the report still describes the
  carbon net. `params.terminate`: `"H"` (default), `"ports-open"`
  (join ports' rim atoms stay bare for a later fuse), `"none"`.
  `hexfold` takes `params.spec` (a `.hx`
  spec text: tubes/cones/fullerenes/holes/nanobud attachments as one
  topology-only notation — `precis-hexfold-help`) and `params.fidelity`
  (`check|stick`, default `stick`; `check` is a report-only preview that
  mints nothing; `dry_run` is a deprecated alias for `fidelity="check"`).
  `hexfold_scene` tiles an authored smooth surface (sheet + fillet/tube/top
  features) with carbon, relaxed under a tether to that surface
  (`params.sheet`, `params.features` — `precis-hexfold-help`); misses and
  caveats come back as `scene.bar` / `scene.top.joint` WARNs, also in the
  returned provenance. Unknown keys are refused.
  Its exclusive `features:[{name:'y',type:'k3-sp2-120-z',
  dihedrals_deg:[120,120,120]}]` entry makes three open sheets on one
  straight equal-120 seam; `sheet:[periods,row_pairs]` has integer counts
  in [2,30]. Analytic seed, minimally relaxed: seam and neighbours pinned, outer sheet
  atoms tethered to their sheet plane; no mixed feet or `extra`. See
  `precis-hexfold-help` for the exact op and retained seam/bond/angle report.
  Three-plane stored surface deviation is unavailable, not zero.
  The sibling exclusive entry `features:[{name,type:'fin-sp3-z',
  side:'out'|'in', rows?}]` with `tube:[n,periods]` grafts a graphene
  strip along an axial zigzag chain of an `(n,n)` tube by one radial sp3
  bond per period (the §11.1 `bond` attachment; `graft.rings`,
  `graft.geometry` retained; surface deviation unavailable); and
  `type:'fin-k3-120-z'` with `tube:[rows,periods]` runs the Y's
  equal-120 seam along a tube axis with the strip as the third sheet
  and the wall free to close around it (`tube.cross_section`).
  `smooth_drum` takes `neck` and `wall` (zigzag tube counts `n` of a
  `(n,0)` stalk and wall, `wall > neck`) plus optional `stalk_length_A`,
  `wall_height_A`, `sheet_radius_A`, `min_flat_A`, `relax`: a sheet →
  stalk → drum surface of revolution whose bend radii come from the tube
  and fullerene tables, with the atom net fitted to it (12 heptagons, 12
  pentagons) and relaxed. Its smooth target is kept on the structure
  (`meta.generated.surface_meridian`) and shown by the 3D viewer's
  "target surface" toggle. Necks below 10 on a `(60,0)` wall can leave an
  8-ring, which it refuses.

## Atomic mode — block trees over atoms

`set_mode(block=…, mode='atomic')` marks a block's realization as
**chemistry** rather than a solid: `mode='atomic'` on a block whose
binding is anything other than a `structure` design (or a `structure`
binding on a block whose mode says otherwise) is a `mode_binding_mismatch`
`view='drc'` finding, never a write-time rejection — the house posture for
a design that can be mid-thought about its own realization. The
six-level block tree — nested envelopes/poses/ports/connects, L2
threading + declared dof, an L5 binding into a real `structure` design for
the filled chemistry — is authored through `kind='se'` with **no
separate units convention**: atomic-mode `envelope`/`pose`/`rot` are the
same bare-metres/bare-radians numbers every other `se` block uses — see
[[precis-se-help]]'s "Units and geometry conventions". The one
surviving Å boundary is internal to `generate` (below): its generators
compute in ångström and the crossing to metres happens once, before the
block ever reaches the tree.

A block is *designed*, not bought (unlike `component`) — the library
grows by composition, a sugar defined once and instanced seven times, the
way a software module tree does. `add_block`/`instance_block`/`array_block`
/`set_pose`/`add_port`/`connect` etc. are the same ops as every other `se`
block — see [[precis-se-help]]'s Ops sections; atomic mode's own ops are
`declare_threading`/`remove_threading`, `declare_dof`/`clear_dof`,
`bind_structure`/`unbind_structure`, and `generate` (all documented
above), plus `add_port`'s `expected_element`/`expected_hybridization` and
`connect`'s `kind='bond'|'interaction'` capability gate.

### Generate — parametric block factories (deterministic fill)

```python
put(kind="se", id="tube1", text='''{"ops": [
    {"op": "generate", "generator": "cnt",
     "params": {"n": 10, "m": 10, "length_A": 40}, "name": "axle"},
    {"op": "generate", "generator": "fullerene", "params": {"atoms": 60},
     "name": "stopper"}
]}''')
```

`cnt` (chiral index `n ≥ m ≥ 0`, radius `a√(n²+nm+m²)/2π`; rim atoms
become `sp2-rim` ports), `fullerene` (`atoms: 60` only so far — truncated
icosahedron, 12 pentagons, Kekulé bond orders), `cone` (`pentagons: 1–5`
apex disclinations, opening angle `sin(θ/2) = 1 − P/6`; honest *open*
frustum — the apex is truncated at a derived floor, both rims get ports),
`cyclodextrin` (`variant: alpha|beta|gamma` — 6/7/8 glucose units, real
rotaxane macrocycles; seeded-rdkit conformer gated by an O4-ring diameter
check, `cd-primary-rim`/`cd-secondary-rim` ports, topology carries measured
`o4_ring_diameter_A` + derived `cavity_diameter_A` + `b1: 1`). Param
validation is theorem-loud (impossible chirality/size is rejected at op
time), an unknown generator lists the registered ones. Prefer a generator
over hand ops or LLM fill whenever the family has one. The `cyclodextrin`
torus envelope is **bore-preserving**, not fully containing: the hole is
pinned at the derived cavity radius so a threaded axle reads clear; rim
atoms folded toward the axis surface read as the warn-tier `envelope_fit`
finding instead of closing the bore.

## Atomic mode — views and scope limits

### `view='mechanics'` — advisory L4 ceilings, never a gate

Min-cut tensile (bond-graph max-flow × ~5 nN/bond), Euler buckling for
tube-shaped generated blocks, harmonic angle strain vs VSEPR ideal
angles — defect-free continuum estimates, never `validate` findings.
Unbound blocks read `unfilled`, cross-design connects read `not fused`,
and every number carries the pristine-lattice caveat.

### `view='literature'` — deterministic (no-LLM) paper search

```python
get(kind="se", id="rotax1", view="literature", args={"block": "hub"})  # one block
get(kind="se", id="rotax1", view="literature")                          # whole design
```

Builds a paper-search query from the target block's `name`/`desc`/`use`,
the design's own `description`, the objective vocabulary on any connect
touching the block (`objectives={'role': ...}`'s values), and — when the
block is already bound — its bound structure's element composition. Runs
the query in-process against the paper corpus and returns both the
generated query and the ranked hits. Naming no `block=` queries the whole
design instead.

## Atomic mode — `view='validate'`, the chemistry-tier findings

Atomic mode's findings (`precis_se.atomic.validate`) share `view='validate'`
with the block/structure-tier findings in [[precis-se-help]] (the header's
filled-fraction line counts atomic-mode blocks: `"N/M block(s) filled
(bound to real chemistry)"`, counting ordinary blocks only — an instance
is filled exactly when its template is):

| rule | severity | catches |
|---|---|---|
| `port_capability` | error | a stored connect violates its own endpoints' declared roles |
| `dangling_binding` | error | `bound_design` no longer resolves, or a `bound_atom` no longer exists in it |
| `binding_element_mismatch` | warn | a bound atom's element doesn't match the port's `expected_element` |
| `envelope_fit` | warn | a bound block's realized atoms protrude beyond its declared envelope + vdW margin — the L1↔L5 agreement has drifted. Or `cannot check — frames do not correspond` when the whole scene sits an envelope-width away (imported structure, no local-frame alignment): re-author the atoms near the envelope's origin (e.g. `from_smiles` `offset=`), do NOT widen |
| `connect_cycle` | warn | the connect graph closes a loop across the block tree (a macrocycle IS real chemistry — this names the path, never says "forbidden") |
| `bond_length_sanity` | warn | a `kind='bond'` connect's endpoints are wildly further apart than a plausible bond — the exact port-to-port distance when both ports carry a `pose`, else the block-pose gap approximation (see Scope below). The finding says which |
| `port_pose_mismatch` | warn | a port's `declared` target pose sits further from its bound atom than a quarter of the block's own envelope — the target is kept, so fix whichever is wrong (`set_port_pose`, or move the atom) |
| `bond_vector_alignment` | warn | a `kind='bond'` connect's two ports' `direction` vectors are far from anti-parallel (>60° off 180°) |
| `composite_part_stolen` | error | a `join` composite's build record names a part whose live parent is a different composite (or no block at all) — the composite can no longer be rebuilt from its own record, and its exposed `<part>_*` ports name a rim it no longer owns. Names both ends; picking which composite keeps the part is yours. The `join` op refuses the edit that causes this, so a finding here means something bypassed it (a stale server process, a restored backup) |

`bind_structure` also runs an `envelope_fit` **preflight** on bind (never
blocking — the same check as the read-time finding above, one call
earlier) at a ~1.7 Å vdW margin via the same `cad` SDF kernel
`view='clearance'` uses.

The **graph-tier** atomic findings — `dangling_threading` (a threading
pair names a block that no longer exists), `threaded_without_envelope` (a
threading pair where either side has no envelope), `mode_binding_mismatch`
(mode↔binding contradiction, above), and `dof_disagreement` — live on
`view='drc'` instead, alongside the rest of `se`'s graph tier (joint
contradictions, mechanism-implied demands): the split is by tier
(per-block/atom vs whole-graph), same as every other `se` finding.

`undeclared_interpenetration`/`bond_length_sanity`/`bond_vector_alignment`'s
thresholds are all fractions of the smaller involved block's own envelope
size (never an absolute figure — an atomic design spans sub-nm to
tens-of-nm blocks, so a fixed epsilon is scale-wrong at one end or the
other).

## Atomic mode — scope limits, stated plainly

- A block's clearance envelope is its own config only — no subtree union
  across children.
- **Propose exists; apply does not.** `se_propose_atomic` proposes a fragment for
  ONE block and dry-runs it — candidate fragment, a `structure` op script,
  a port→atom map, DRC already run against a scratch scene. It never
  writes anything; turning an accepted proposal into a real design is
  still manual (run the ops yourself, then `bind_structure`).
- **No charge, optical, or simulation views.** No mechanism/dynamics
  analysis, no torsion scan, no rotational-barrier estimate —
  `declare_dof` records intent only.
- **A port's own position is optional** (`add_port`/`set_port_pose`
  `pose` = `declared`; `bind_structure` = `bound`), and often absent —
  at box level the displacement from the block's origin to its
  attachment point is genuinely unknown, and a bind only fills a port it
  maps.
  Where both ends of a bond have one, `bond_length_sanity` measures the
  real port-to-port distance; where they don't it approximates from the
  two blocks' pose-to-pose gap, so it can read long for a legitimate
  off-axis port even on an otherwise-correct design. It warns, never
  gates, and names which measurement it used.

## See also

- [[precis-se-help]] — the call surface: op grammar, units, non-atomic views
- [[precis-structure-help]] — the atom-level design that `bind_structure` binds into
