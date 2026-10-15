---
id: precis-se-atomic-help
family: se
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

## Completion reads

`get(kind='se', id='<design>', view='completion')` returns the shared lossless
v1 all-block envelope; `completion_digest` summarizes it. Optional
`args={'shown_design_token':'<captured-token>'}` detects a stale shown scene.
The read includes hidden/isolated blocks and dangling bindings. Unsupported
instancing or an inventory above 256 blocks is explicitly incomplete; an empty
design cannot finish. Independent block evidence never establishes physical
assembly convergence. Reads do not hydrate a blocktree, change geometry or
submit jobs; `next.can_submit` stays false. Legacy run records remain unverified.

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
  Its exclusive typed entries (one feature, no mixed feet or `extra`;
  seam and neighbours pinned, the rest tethered; stored surface deviation
  unavailable, not zero) are the seam family — `k3-sp2-120-z` (three
  sheets on a straight equal-120 seam), `fin-sp3-z` (sp3 graft fin on a
  tube), `fin-k3-120-z` (the seam along a tube axis), `rail-k3-120-z` (N
  inward ribs, pores, `generated.sites`), `k4-sp3-90-a`, `k3-sp3-z` and
  `k3-sp3-a` (sp3 seam lines; an H cap under 1.0 Å from another atom is
  dropped, `terminate.dropped`). Exact params and retained findings:
  `precis-hexfold-seams-help`.
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

### `view='catalogue'` — which catalogue row a join's seam numbers came from

`get(kind='se', id=…, view='catalogue')` (optional `args={'block': '<label
or #uid>'}`) lists the `se_hexfold_catalogue` edge rows a join can see
(`preferred`) and the `source='measured'` rows the measured-row gate
withholds from `resolve_edge` (`withheld`), then for every `join` block
and each side `a`/`b`: the row consulted with its resolution label as
`resolve_edge` returned it (`pinned z`, `exact z10`, `nearest z12`),
the seam radius and leak threshold in force with what governed each
(`explicit` override, the label, or `table`), and which withheld row the
gate kept from that side. The label is read off the join record
`compose` wrote at join time, never re-resolved; only the withheld lookup
is live. Joins recorded before 2026-10-09 show `radius_source` alone.
Read-only, no geometry; the dry-run for `join` is a later slice.

## Atomic mode — `view='clearance'` on a bound pair (vdW gap, rotor sweep, pockets)

```python
get(kind="se", id="valve", view="clearance",
    args={"a": "shell6", "b": "pillbox"})
get(kind="se", id="valve", view="clearance", args={
    "a": "shell6", "b": "pillbox",
    "sweep": {"axis": "b", "step_deg": 15, "range_deg": 360},
    "pocket_gap_A": 3.5})
```

When `a` and `b` are both bound to `structure` designs, `view='clearance'`
takes the atomic path instead of the cad-kernel envelope gap: every atom's
nearest cross-block centre distance minus the two Bondi van der Waals radii
(C 1.70, H 1.20, N 1.55, O 1.52 Å). It reports the minimum gap and the pair
that makes it (block, element, scene ordinal each), the p05/p50 of the
per-atom gap distribution, and the counts under 0/1/2 Å — under 0 is real
interpenetration. Verdict: `free` (min gap ≥ 0.5 Å), `touching` (0–0.5),
`interpenetrating` (<0). Pockets: atoms whose nearest cross-block gap is
under `pocket_gap_A` (default 3.5 Å), clustered by 3.0 Å same-block
connectivity merged across the pair — one connected near-contact region per
void; the count at the loading angle is the valve's metering number. Each
pocket lists per-block atom counts, centroid (z, θ about the axis), angular
extent, and an approximate convex-hull volume.

`sweep` rigidly rotates `b` about the axis and re-measures per angle —
read-only, never a pose write. `axis`: `"b"`/`"a"` (that block's own
z-axis envelope through its posed origin) or `[point, direction]` in
metres; omitted, a declared revolute `joint.axis` between the pair wins,
else `b`'s envelope. The table is angle | min gap | worst pair | n<1Å |
pockets, plus the worst/best angle and a one-line verdict. On the no-args
CONNECTS digest, atomic pairs measure atoms and the rest measure
envelopes on the same table, worst first. `sweep`/`pocket_gap_A` on a
non-atomic pair is a rejection.

The header prints each bound structure's `last_relax` rung — `stick`
means preview geometry (the spring-relaxed stick model), not a relaxed
minimum; trust rung `geo`/`ml` numbers for placement decisions.

## Atomic mode — `view='sites'` + `lining` (attachment sites, complementarity stub)

```python
get(kind="se", id="valve", view="sites", args={"block": "pillbox"})
get(kind="se", id="valve", view="sites",
    args={"a": "shell6", "b": "pillbox", "pocket_gap_A": 3.5})
get(kind="se", id="valve", view="clearance", args={
    "a": "shell6", "b": "pillbox",
    "sweep": {"axis": "a", "step_deg": 30},
    "cargo_A3": 120, "pocket_gap_A": 3.5,
    "lining": {"shell6": [{"site": 137, "group": "OH"},
                          {"site": "ring:6-ring", "group": "CH3"}],
               "pillbox": [{"site": "ring:open-edge", "group": "OH"}]}})
```

`view='sites'` enumerates the carbons that can take a substituent on a
structure-bound block. Each row is one (atom, free-face) site: `ring` is
`open-edge` (fewer than three carbon neighbours — the rim dangling bond,
axial only for a zigzag rim) or the smallest C–C ring it closes (5–8,
else `non-ring`); `sp3` (a ≥4-coordinated single-bond carbon, e.g. a fin
graft junction) and `substituted` (carries a non-C non-H group) count in
the summary but never list — no free valence. `face` is
`out`/`in`/`axial`, the free direction's sign against the block's own
envelope axis (`n/a` without an axis-bearing envelope — ring class and
exposure still report, z/θ/r blank); `exposure` is the free fraction of
a probe sphere `exposure_A` (default 3.0 Å) out along the free direction
— 1.0 is wide open; `z`, `θ`, `r` are the same axis frame as the
clearance pocket report. Wall carbons show once per face.
`args={'block'}` lists a block alone; `args={'a','b'}` lists only the
sites that FACE the other block within `pocket_gap_A` — for the valve,
the rotor's `out` rows and the shell's `in` rows; read-only.
`face: 'in'|'out'|'both'` filters (default both).

`lining` on `view='clearance'` is the complementarity STUB — shape and
polarity match, NO energetics (those come later via the atomic-mode
ladder: relax rungs, charges). It decorates site carbons: `site` is the
atom `ordinal` from the sites table or `'ring:<class>'` for every carbon
of that class (`non-ring` excluded); `group` maps to a polarity class —
OH→polar-donor, NH3+→cationic, COO-→anionic, CH3→apolar,
H/undecorated→bare. `lining` alone implies the default sweep and the
lining is validated BEFORE the sweep runs. Each pocket scores per angle:
shape = pocket volume vs `cargo_A3`, polarity = COMPATIBLE decorated
contact pairs (+1: polar–polar, cationic–anionic, apolar–apolar,
polar–charged) minus unlike (−1: polar or charged vs apolar, same-sign
charges); `bare` never counts. The verdict names the admitting and
rejecting arcs as SWEEP-ANGLE ranges [t1°–t2°] when the term's sign
flips across the sweep (contiguous runs, wrapping at 360°), else "no
rectification". To score alternating shell arcs the pocket must VISIT
them — e.g. an offset rotor orbited about the shell's axis
(`sweep.axis='a'`).

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
