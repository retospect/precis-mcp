---
status: ready
title: Molecular workspaces — fragments, decorators, a shared step cache, a cached collapse check, and one inspect family across scales
pillar: 3d-design
prio: high
model: fable
blocked-by: structure-workspaces
---

# Molecular workspaces — the workspace model applied to molecules

Reto, 2026-10-29: "we have done code type improvements for se macro and
pcb. We want to do the same for molecular. Share as much infra as
possible. We want hex tiler and other tools to work from code, and
quickly, to assemble building blocks and add decorators. We want to be
able to store and recover from intermediaries." Same day, on the gap
list: "Fast relax is good — we can probably see if it collapses quickly
and caching is good", a separate spec, and "integrate the improved
observation beam. Keep it DRY."

Applies [structure-workspaces](structure-workspaces.md) (workspace
kind, `run`, import boundary, construction traces, `pga`, relax as a
service, frozen units) and mirrors
[pcb-workspace-code](pcb-workspace-code.md) (a typed builder package
inside the `precis-geom` wheel, checks at `finish()`, offline program,
goldens). Nothing here adds a second workspace mechanism, importer,
trace format, relax interface, validator, probe family or step cache;
every slice extends one that exists or is already specced.

Concrete dependencies: structure slice 0 (importer, landed), 2 (wheel +
trace, landed) and 2a (`pga`, landed); the `import_workspace` edit op
and tar upload (e9ded51d0, landed). Nothing waits on the sandbox lane
except slice 0b.

## What exists, what is missing

Built and shared by se, pcb and the atomic leg today:

- `precis_geom.out.Run` (`block`/`instance`/`connect`/`ray`/`board`,
  manifest, `finish()` with the fit check), `precis_geom.trace`
  (`@traced`, `maker` per atom, frame keys `sha8(parent.key, qual,
  occ)`, `trace_schema.py`), `precis_diag`/`run_diag`.
- The host loop: `uv run` → `scratch/out/<run>/` → the `import_workspace`
  edit op (tar via upload token); `examples/hexfold/{prism,nanobud,
  pillar}` with the README recipe.
- `precis_geom.assembly` (`pga`): `geom` (reads a hexfold `Net`/`Strip`
  or a `Scene`, `coords=`, `ports=`), `tile`, `mirror`,
  `translate`/`rotate`/`transform`, `place`, `assembly`, `instance`,
  `Ray`, `find_holes`.
- The hex tiler and builders: `hexfold.prism.prism_tube`, the
  `hexfold.sheetnet` primitives (`strip`, `pore`, `place`, `junction`,
  `assemble`), the generator registry
  (`precis_se.atomic.generators.GENERATORS`: `hexfold_scene`, `cnt`,
  `fullerene`, `cone`, `cyclodextrin`, `tpms`, `smooth_drum`,
  `schwarzite`, …), all in the wheel.
- H capping: `hexfold_spec._terminate_open_edges` (every C short of
  valence, `ports-open` skip), the construction half `termination.py`
  imports.
- DB-free chemistry checks: `precis.structure.validate.validate`
  (overlap at `OVERLAP_FRACTION` 0.6 × covalent, over-coordination,
  bond length, valence budget), `precis.structure.vsepr.advisories`
  and `infer_hybridization`, `probe.fragments`, `probe.detect_bonds`.
- Local relax rungs in `precis.structure.relax`: `clean`, `geo`
  (geometry repairs, no forces), `emt` (ours; `EMT_ELEMENTS` incl.
  H/C/N/O), `xtb` (where tblite installs), `ml` (MACE, `[dft-ml]`
  extra), `jiggle` (fake); `on_step` on `clean`/`emt`/`xtb`/`ml`.
- Probes: `precis.cad.inspect` (`ray`/`section`/`overlap`/`thickness`
  over the folded SDF, `FeatureRef(label, role)`, `profile_samples`,
  `probe_solid` — the shaped-beam tool, [cad-inspect](cad-inspect.md))
  and the atomic `precis.structure.probe` (`ray_hits` with
  `pass`/`pi`/`frame` events, `plane`, `bonds_through_plane`,
  `bonds_in_sphere`, `diff`).

Missing, and the scope of this spec:

| gap | today | slice |
|---|---|---|
| store and recover intermediates | no step-key code; se-flow-kernel 4b specs the cache, unbuilt | 0, 0b |
| a molecule as a building block | `out`/`pga` know no SMILES; `structure.ops::_op_from_smiles` is an op, not a `Geom` with ports | 1 |
| decorators as functions | capping exists only as `_terminate_open_edges` on Nets; no functional groups; `sheetnet.decorate` (multicell slice 2) unbuilt | 2 |
| a quick collapse check | `jiggle` only; structure-workspaces §Core design 6 "scripts never relax" | 3 |
| chemistry checks before import | `structure.validate` runs on `view='validate'`, not at `finish()` | 4 |
| hex tiler driven by a recipe, cached | generators reached through the `generate` op | 5 |
| one inspect family at both scales | `cad.inspect` and `structure.probe` are separate | 6 |

## Core design

1. **A molecular workspace is a structure workspace.** Same kind, same
   `run`, same importer; blocks are `scale: 'atomic'`. No `kind: 'mol'`
   manifest (unlike pcb, whose board is a different product).
2. **Package `precis_geom.mol`, alias `mol`.** Fragments, attach,
   sites, decorate, cap, the collapse check and `check`. `pg`, `pga`
   and `pcb` keep their meaning. Step cache: `precis_geom.steps`,
   imported as `from precis_geom import steps`.
3. **Every costly build step is cached and recoverable** (§Step cache):
   one module for all three scales. Re-running after a crash or an edit
   starts at the first changed step.
4. **The collapse check is a cached quick relax; the service relax is
   the record** (§Collapse check). Amends structure-workspaces §Core
   design 6 for the check rungs only (Reto 2026-10-29).
5. **Checks run at `finish()` and on import with the same functions**
   (pcb's `b.check()`; se-native-fields §Server-side re-check).
6. **One inspect family at both scales** (§Inspect): the atomic leg
   supplies a field provider to the `cad.inspect` kernel.
7. **Loop latency.** A small decorated structure (≤ 2k atoms) builds,
   checks and writes `out/` in < 10 s on a laptop cold, < 2 s on a
   re-run with one late step changed — the parent's < ~10 s target,
   measured on the host loop.

## The API (`import precis_geom.mol as mol`)

| object | call | returns | notes |
|---|---|---|---|
| fragment | `mol.frag(smiles='[*:1]C(=O)O', *, name=, conf_seed=0)` / `mol.frag(scene=Scene, ports={...})` / `mol.frag(ref='structure:<slug>')` | `Geom` with `scene` + ports | `[*:n]` dummy atoms become ports `a1..an` (`atom` = attachment atom, `direction` = bond vector, `annotations.order`); the dummy is removed. Embedding is `_op_from_smiles`'s path, moved into `mol` and called back by the op. `ref=` reads through the read MCP (`blocks.get`) |
| library | `mol.lib('COOH')`, `mol.lib('PEG', n=4)`, `mol.lib('C2H4')`, `mol.lib('c60')` | `Geom` | a small named set (OH, NH2, CH3, COOH, Ph, PEG_n, (CH2)n, C2H4, C60 via `GENERATORS['fullerene']`). The spacers are [se-seam-spacers](se-seam-spacers.md)'s. Names beyond the set: [chem-name-lookup-verb](chem-name-lookup-verb.md) through the read MCP |
| attach | `mol.attach(host, site, frag.port('a1'), *, torsion=0.0, bond=None)` | `Geom` (host + fragment, one scene) | bond length from covalent radii unless `bond=`; alignment is `pga.place`'s solve; `torsion` about the new bond; one `attach` frame |
| sites | `mol.sites(geom, '<selector>')` | site list (atom, free direction, face, class) | §Decorators — the grammar |
| decorate | `mol.decorate(geom, sites='<selector>', group=<frag\|SMILES\|'H'>, *, pattern=None, seed=0, torsion='scan')` | `Geom` | §Decorators |
| cap | `mol.cap(geom, group='H')` | `Geom` | `decorate(sites='open')`; on a Net-built Geom it is `terminate_open_edges` |
| collapse check | `mol.relax(geom, rung='auto'\|'uff'\|'emt'\|'xtb'\|'geo', *, steps=200, fmax=0.1)` | `(Geom, CollapseCheck)` | §Collapse check; a cached step |
| check | `mol.check(geom)` | findings | §Check |
| emit | `out.block(name, geom, relax={...})` | manifest row | the row gains optional `relaxed_by` (slice 3) |

`pga.tile`/`mirror`/`place` work on every `Geom` above unchanged. Every
call is `@traced`; atoms keep their `maker` through attach, decorate and
relax (relax moves atoms, it never re-makes them — the relax frame is a
modifier, structure-workspaces §Construction traces).

### Worked example — decorated prism with a guest (`examples/hexfold/decorated-prism/main.py`)

```python
import precis_geom.assembly as pga
import precis_geom.mol as mol
from hexfold.prism import prism_tube
from hexfold.stick import stick
from precis_geom import out, steps

R = steps.recipe()                                    # recipe.json
with out.run("decorated-prism") as o:
    net, _tube = prism_tube(R["periods"], R["rows"], R["strip_rows"], R["seams"])
    p = pga.geom(net, coords=stick(net))
    p = mol.decorate(p, sites=f"open & face:out & every:{R['every']}",
                     group="[*:1]C(=O)O")
    p = mol.cap(p)                                    # H on the rest
    p, cc = mol.relax(p)                              # auto: finite → uff
    cc.raise_if_collapsed()                           # Run.fail records it
    o.block("p", p, relax={"method": "gfn2"})
    o.block("c60", mol.lib("c60"), template=True)
    o.instance("guest", template="c60", parent="p", pose="[0 0 2] nm")
```

`raise_if_collapsed()` raises `RelaxCollapsed`, whose message names the
broken or formed bonds by maker frame; `out.run`'s `except` writes the
failed run record (`Run.fail`) with those frames error-marked. A second
run with `R["every"]` edited hits `pga.geom` (the stick relax is the
cost), rebuilds decorate, cap and the check, and the import echo says
`steps: 1 hit · 3 rebuilt (from decorate#0)`.

## Step cache — store and recover intermediates

The build-step stack ruled 2026-10-23 (se-flow-kernel slice 4b
§Build-step stack cache; structure-workspaces §Frozen units/modules,
in-build counterpart). This spec builds the cache once as
`precis_geom.steps`; 4b's kit (`params`, `optimize`, the per-step
cost/rebuild report) consumes it — 4b keeps the report, reading the
cache's per-step `wall_s` and hit flags.

- **Key.** The ruling's `hash(step name, params, parent step keys)`,
  plus `precis_geom.__version__` and the sha of the step function's
  own source (so a host edit to a step's body misses). Parents are the
  keys on input `Geom`s (`Geom.meta['step']`); a `Geom` built outside a
  step has a content key (coordinate SHA, the goldens' rule). Exact
  keys; a relax miss warm-starts from the nearest key on the same
  lineage (`warm_from=<key>`, 4b's rule). Wheel sha stays in the
  frozen-unit key (structure §Frozen units), not here.
- **What is a step.** `@steps.step` on a pure function returning a
  `Geom`, `SceneSpec` or `Board`. The costly `mol` calls (`frag` embed,
  `decorate`, `cap`, `relax`), `pga.geom` over a Net with `coords=`,
  `pga.tile(fused=True)` and slice 5's generator wrappers are steps.
  Hexfold `Net`/`Strip` values are not cached directly (extXYZ drops
  `seed3`, `hyb`, rims); they enter the cache through `pga.geom`. Cheap
  transforms (`translate`, `rotate`) are not steps.
- **Storage, one format per scale** (gr506996): atomic → `extxyz` +
  `.bonds.json` (`out._write_atomic`); macro → what `out._write_macro`
  writes for the object (DSL `.cad` text preferred, `.cad.json` for a
  `SceneSpec`; no `Design.to_json`); pcb → `board.json`
  (`Board.to_batch`). Beside it `step.json`: `{key, name, params,
  parents, ports, findings, wall_s, trace}`, `trace` the step's frame
  subtree stored relative to its root. Content-addressed under
  `<cache>/<key[:2]>/<key>/`.
- **Where.** Host runs: `scratch/cache/` (gitignored like `scratch/*`),
  LRU-capped (default 2 GB) — the 2026-10-23 "in-container LRU only"
  ruling covers the sandbox lane, not the laptop. Lane runs: the ruled
  in-process LRU (slice 0); a persistent per-workspace dir is slice 0b,
  gated on open question 1. The DB never holds steps; a step worth
  keeping across workspaces is promoted as a frozen unit (structure
  slice 4u).
- **Recover, in the script.** Re-running `main.py` is resume: every
  completed step before a crash or an edit is a hit. `steps.load(key)`
  returns the object, `steps.ls(name=None)` lists keys with name,
  params digest, `wall_s` and hit count.
- **Recover, from MCP.** The trace carries each step's key on its
  `step` frame, so `view='trace'` on an atom or frame shows `step
  3fa9… (hit)`. Landing an intermediate in a design goes through the
  importer like any geometry: a script `o.block("mid", steps.load(key))`
  and `import_workspace`. No trusted-side code reads a cache dir; the
  sandbox still ends at `/work/out` (structure §Core design 3).
- **Trace on a hit.** The stored relative subtree is grafted under a
  live `step` frame (`hit: true`, producing key as data); frame keys and
  ids are re-minted under the live parent (keys embed the parent chain,
  `trace_schema.py`), and the artifact's `maker` column is remapped
  through the same table. A graft that cannot remap (schema mismatch)
  is a miss, never a wrong maker.
- **Report.** `Run.finish()` writes `steps: {hits, rebuilt, first_miss,
  wall_saved_s}` into the trace doc's `run` header (`tr.dump(run_meta)`);
  the import echo prints the one-line form.

## Decorators

A decorator is a function `Geom → Geom` that adds atoms at selected
sites. One primitive, `mol.decorate`; `mol.cap` and multicell slice 2's
`sheetnet.decorate` are spellings of it.

- **Site grammar (new, in the wheel).** Tokens over `sites.py`'s
  existing taxonomy (ring class, face out/in/axial, exposure):
  `open` (atoms below their valence), `face:out|in|axial`,
  `ring:<class>`, `element:<sym>`, `frame:<key>` (shared with the se
  region grammar), `every:k`, combined with `&`. The pure enumeration
  and taxonomy move out of `precis_se/atomic/sites.py` (which imports
  `precis.handlers._interpret`/`_slice_args` and cannot ship) into
  `precis.structure.sites`; `sites.py` keeps rendering and calls the
  moved code. The se region grammar (`datums.py`) is unchanged; slice 2
  adds no token to it.
- **Placement.** Per site: free direction and face from the moved
  `sites` code, hybridisation from `vsepr.infer_hybridization`; then
  `attach` with the group's port; `torsion='scan'` picks, of 12
  torsions, the one with the largest minimum non-bonded distance;
  `pattern=` cycles groups across sites; `seed` breaks ties.
- **Capping.** On a Geom built from a Net, `mol.cap` calls
  `_terminate_open_edges`, promoted public as
  `hexfold_spec.terminate_open_edges`; on a plain scene it caps open
  valences with the same placement rule. Parity with the `terminate` op
  holds by construction (same function).
- **sp3 relabel.** On a Net, H on carbons relabels `hyb='sp3'` through
  `sheetnet.decorate` (multicell slice 2's spec, built here), which adds
  `geom.sp3.mismatch` to `hexfold.check`; `counting_residual` stays the
  census.
- **Findings.** `decorate.site_skipped` when the best torsion still
  overlaps under `validate.OVERLAP_FRACTION` (0.6 × covalent, the one
  clash rule), `decorate.valence` (site saturated); counts in the
  frame's `made`.

## Collapse check — a cached quick relax

Reto 2026-10-29: "see if it collapses quickly and caching is good."

- **Rungs.** Energy rungs only, so bonds can actually break: `uff`
  (new, ours — RDKit UFF on the scene's bond graph; RDKit is a wheel
  dep; **finite only**, raises `RelaxUnsupported` on a `pbc` scene, as
  structure §Relax already says), `emt` (periodic-capable, H/C/N/O and
  the fcc metals), `xtb` where tblite installs. `rung='auto'`: finite
  → `uff`; periodic → `xtb` if installed, else `emt` if every element
  is in `EMT_ELEMENTS`, else refuse with `next=` naming `xtb` or the
  service relax. `geo` is allowed but is a geometry repair on the
  declared graph: it cannot break a bond, so its verdict covers only
  displacement and ring census, and it takes no `fmax` and no early
  exit (no `on_step` in the numba loop).
- **Verdict** (`CollapseCheck`): `probe.detect_bonds` on the relaxed
  positions vs the declared graph (positions move, `scene.bonds` do
  not — `probe.diff` alone sees nothing), plus ring census, fragment
  count and max displacement (default `2.0 Å`). `collapsed` when a bond
  breaks or forms, a ring is lost, the fragment count changes or the
  displacement bound is exceeded. Each changed bond is named by both
  atoms' maker frames: `junction#3 ← main.py:9 lost C12–C13 (1.42 →
  2.31 Å)`. Otherwise `ok`, with energy drop, steps and displacement.
- **Early exit.** On energy rungs the check re-perceives bonds every 10
  steps through `on_step` and stops at the first change.
- **Checkpoint.** A long check writes a step-local checkpoint through
  `on_step` into `<cache>/<key>/checkpoint.json` and resumes from it on
  a re-run (new and small; the job-side `_CkptWriter` in
  `hexfold_relax_job.py` stays job-bound).
- **Cached.** A step like any other; the key includes rung, `steps`,
  `fmax`. A rejected recipe later reverted is a hit.
- **What it is not.** The checked geometry may be the block's emitted
  seed; the manifest row then carries `relaxed_by: {rung, key,
  verdict}` (a new optional key in `_BLOCK_KEYS`, slice 3) and
  `view='validate'` shows it, so a UFF check is never mistaken for
  GFN2. The record is the service relax the row's `relax:` asks for.

## Check

`mol.check(geom)` composes what exists: `structure.validate.validate`
+ `vsepr.advisories` + `probe.fragments` + open valences not declared
as ports or radicals. No new validator; any new rule goes into those
modules. `Run.finish()` runs it on every atomic block (as the fit check
today; `check=False` opts out) and the importer runs the same functions
on arrival. Each finding carries the nearest user frame (the 2t `frame`
column). `precis_se/atomic/validate.py` keeps its se-tree checks (ports,
envelopes, connects, cross-block clash); nothing moves out of it.

## Inspect — one probe family, both scales

The improved observation beam ([cad-inspect](cad-inspect.md): `ray`,
`section`, `overlap`, `thickness`; a zero-radius beam is a ray) becomes
the single probe family; the atomic leg plugs in.

- **Seam.** `cad.inspect`'s marchers take `(design, expr)` today; they
  are refactored to take a field provider: `sdf(pts) → d` plus
  `features_at(pts) → FeatureRef[]`. The cad provider wraps the folded
  SDF and `fold.classify` (byte-identical results — the existing
  inspect tests are the golden). The atomic provider is new: a signed
  distance to the union of vdW spheres (`probe.vdw_sdf`, beside the
  boolean `_spheres_inside`), with `FeatureRef(label=<maker frame
  key>, role='owner')`.
- **Facade.** `pg.inspect.ray/section/overlap/thickness(geom, ...)`
  dispatch on `Geom.atomic`; mixed assemblies concatenate in `s` order
  as the 2r rays do. Same result dataclasses.
- **Beam radius at Å.** `ray(..., radius=r)` sweeps the cylinder
  (`overlap` with `probe_solid`); with `profile_samples=` the SDF along
  the axis is the free radius to the nearest vdW surface, so its
  minimum is the channel bottleneck and its `FeatureRef` names the wall
  frame that pinches it — "does the guest pass" without a pore tool.
- **Atomic events stay.** `probe.ray_hits`' `pi`/`charge`/`frame`/
  `relax` events are not SDF questions; they merge into the same
  `RayHit` list (2r already wraps them into the §5.4 `Hit` shape).
  `probe.plane`/`bonds_through_plane` become the atomic `section`'s
  bond-crossing annotation, not a second section.
- **MCP.** cad-inspect deferred MCP surfacing until a second consumer;
  molecular is it. Stored rays keep `view='ray'` (2r's row shape is a
  ray). Ad hoc probes go to a new `view='inspect'` on se and structure
  (`probe=`, origin/axis/normal/pitch/radius, any region selector or
  `~` handle as the target), computed on demand, nothing stored (open
  question 4).
- **In scripts.** `pg.inspect` results are plain data; a script asserts
  on them or attaches a finding string to the block's `findings`.

## Import and MCP surface

- **Manifest.** One new optional block key, `relaxed_by` (slice 3,
  `_BLOCK_KEYS`); everything else rides existing keys or the trace
  doc's `run` header. Unit promotion is structure slice 4u's row.
- **Ops retired (W5).** Owned by structure-workspaces slice 1, not
  here. Ruled 2026-10-29 (question 2): the removal no longer waits for
  the sandbox lane — it ships once slices 1, 2 and 5 here cover what
  the ops build, with `next=` naming `import_workspace`; the parent's
  slice 1 row says so.
- **Script-made rows are read-only to construction ops**, as
  structure-workspaces §MCP surface.

## Slices

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 0 | **`precis_geom.steps`, all three scales, host + in-process.** `@steps.step`, key (ruling's three parts + version + function source sha), store/load for Geom/SceneSpec/Board, `ls`/`load`/`recipe`, relative trace subtree + re-minted graft + maker remap, `scratch/cache/` LRU, in-process LRU on the lane, `steps` digest in the trace `run` header and the import echo, `step` key on trace frames | `precis_geom/out.py` writers, `precis_geom/trace.py`, `trace_schema.py`, `Board.to_batch` | Two host runs of the §Worked example with one recipe edit: the first miss is the edited step, every step below is a hit, wall time drops by the hits' `wall_s` ± 10 %; a run killed mid-build resumes with every completed step a hit; a hit atom's `view='trace'` equals the fresh build's plus the `step … (hit)` tag; `mol.lib('COOH')` cached from one call site and hit from another grafts keys valid under the second parent; a Board and a cad step round-trip byte-identical; editing a step function's body misses; a trace-schema mismatch is a miss | M, 1–2 builds |
| 0b | **Persistent step cache on the lane** (open question 1, approved 2026-10-29). `/var/lib/precis-sandbox/ws/<slug>/cache/` mounted `/work/cache`, per-workspace LRU cap | `claude_docker.build_script_argv`, agent-workspace-containers host dirs | Two lane runs of one workspace: the second hits steps from the first; the cap evicts oldest; no trusted-side code reads the dir | S, after structure slice 1 |
| 1 | **Fragments + attach + library.** ·landed 2026-10-29 (worktree-agent-a78e9756711235299)· `mol.frag` (SMILES with `[*:n]`, Scene, `ref=`), `mol.attach`, `mol.lib`, `_op_from_smiles`' embed moved into `mol` (the op calls it), traced; the step cache arrives with slice 0 (TODOs in `frag`/`lib`) | `structure/ops.py`, `precis_geom/assembly.py::place`, `packaging/geom/` | `mol.frag('[*:1]C(=O)O')` has one port with the C–C bond vector; attaching it to a benzene site gives benzoic acid whose RDKit canonical SMILES matches; `torsion=90` moves only fragment atoms; the `from_smiles` op golden is unchanged | M, 1 build |
| 2 | **Decorators.** ·landed 2026-10-29 (worktree-agent-a27496041e346c77d)· Site grammar, `mol.sites`/`decorate`/`cap`, pure enumeration + taxonomy moved to `precis.structure.sites` (`sites.py` calls it), `terminate_open_edges` promoted public, `sheetnet.decorate` + `geom.sp3.mismatch` (multicell slice 2) | `precis_se/atomic/sites.py`, `generators/hexfold_spec.py`, `hexfold/sheetnet.py`, `hexfold/check.py` | `mol.cap` on `prism_tube(4, 4, 2, '++++++')` equals the `terminate` op's output (same function, SHA equal); `decorate(sites='open & face:out & every:3', group='COOH')` places ⌈n/3⌉ groups on the fixture with no `decorate.site_skipped`; `view='sites'` output byte-identical before/after the move; the wheel import test covers `precis.structure.sites` | M–L, 2 builds; after 1 |
| 3 | **Collapse check.** `uff` rung (finite only), `mol.relax` as a step with `rung='auto'`, `CollapseCheck` on re-perceived bonds + rings + fragments + displacement, early exit via `on_step`, step-local checkpoint, `relaxed_by` in `_BLOCK_KEYS` and on `view='validate'` | `precis/structure/relax.py`, `structure/probe.py::detect_bonds`, `workspace_import.py::_BLOCK_KEYS`, `out.block` | A capped finite prism checks `ok` under `uff` in < 10 s on a laptop; a fixture with a 0.9 Å C–C contact checks `collapsed` before step 30 naming both maker frames; `uff` on a `pbc` scene refuses and `auto` picks `xtb`/`emt`; `geo` refuses `fmax`; an identical second call is a hit; the imported block's `view='validate'` shows `relaxed_by uff ok` | M, 1 build; after 0 |
| 4 | ·landed 2026-10-29 (worktree-agent-ae1b2530db4018763)· Code lives in `precis/structure/check.py::check` (the `precis_geom.mol.check` alias is added with the `mol` package); findings go to `out/check.json` beside `fit.json`; `Run.check_chem`, `check={'strict': True}` raises. **Check at finish + on import.** `mol.check` composing `validate`/`vsepr`/`fragments`/open valences; `Run.finish()` default-on for atomic blocks; the importer calls the same functions; nearest-frame column | `precis/structure/validate.py`, `vsepr.py`, `precis_geom/out.py::finish`, `workspace_import.py` | A block with an under-valent C fails `finish()` with a finding naming `main.py:<line>`; the same block through `import_workspace` yields the same finding text; `view='validate'` on prod fixtures is byte-identical before/after | S–M, 1 build |
| 5 | **Hex tiler from a recipe.** `GENERATORS` entries (`hexfold_scene`, `cnt`, `fullerene`, `cyclodextrin`) and `prism_tube` wrapped as steps, `steps.recipe()`, examples `examples/hexfold/decorated-prism/` and `examples/hexfold/cnt-guest/` with goldens in `scripts/test` (structure slice 7's pattern) | `precis_se/atomic/generators/`, `examples/hexfold/`, `tests/workspaces/`, `scripts/impacted-tests` | `uv run` on each example reproduces its golden (counts, SHA, findings, trace keys); a `sheetnet` change that alters one fails the selected test naming the example; the decorated-prism example meets the §Core design 7 latency (cold < 10 s, one-late-step re-run < 2 s) | S–M, 1 build; after 2, 3 |
| 6 | **One inspect family.** Field-provider seam in `cad.inspect`, `probe.vdw_sdf` + maker attribution, `pg.inspect` facade, `radius=` + `profile_samples=` bottleneck at Å, atomic events merged into `RayHit`, `view='inspect'` on se and structure | `precis/cad/inspect.py`, `structure/probe.py`, `precis_geom/assembly.py::Ray`, se/structure handlers | Existing `cad.inspect` tests and the `fasten.py` ray golden unchanged; on a (10, 10) tube the axis profile minimum equals tube radius − C vdW within 0.05 Å and names the wall frame; C60 in a too-narrow tube reports `overlap.volume_m3 > 0` naming both frames; a benzene `section` in the ring plane has one loop and `hole_count` 1; `view='inspect'` `probe='section'` renders ≤ 400 tokens | M–L, 2 builds; after 1 |
| 7 | **Skill + glossary.** A molecular section in `precis-workspace-help` (frag, sites, decorate, steps, collapse check, inspect), pointer lines in `precis-se-atomic-help`, glossary (step key, collapse check, fragment port, site grammar) | skills, `docs/glossary.md` | The §Worked example runs from the skill text as written; no skill text contradicts the site grammar | S, 1 build; after 2–6 |

Order: 0 → 1 → 2 → 3 → 4 → 5 → 7; 6 after 1, beside 2–5; 0b after
structure slice 1.

## Risks

- **Maker remap on cache hits.** A wrong remap attributes atoms to the
  wrong line — worse than no trace. Remap is table-driven from the
  relative subtree, any mismatch is a miss, and slice 0 pins equality
  with a fresh build.
- **Stale hits on host edits.** The key covers the step function's own
  source, not its callees; an edit to a helper the step calls can hit
  stale. `steps.clear()` and `main.py --fresh` exist; the echo prints
  the version and source sha of every hit so a stale one is visible.
- **UFF on carbon frameworks.** UFF types strained seams badly and may
  break a bond a GFN2 relax keeps. The verdict is bond-graph based and
  names the bond; slice 3 records UFF vs GFN2 agreement on the hexfold
  fixtures, and `auto` switches finite hexfold nets to `xtb` where
  installed if they disagree.
- **Moving `sites.py` code.** A large file with handler imports; a
  careless split changes view output. Acceptance is byte-identical
  `view='sites'` on fixtures.
- **The vdW SDF is a model.** Free radius is to the vdW surface, not
  to density; the profile header says so. Density surfaces are
  multiscale §4's.

## Open questions for Reto (with recommendations)

**Ruled (Reto 2026-10-29): all five approved as recommended.**

1. **Persist the step cache on the lane across containers (slice 0b)?**
   The 2026-10-23 ruling is "in-container LRU only"; host runs persist
   under `scratch/cache/` either way. Rec: yes, per workspace on the
   sandbox host dir, LRU-capped, never the DB, never read trusted-side;
   cross-workspace reuse only through frozen units.
2. **Decouple structure slice 1's removal of `generate`/`join`/
   `terminate` from the sandbox lane?** Rec: yes — `import_workspace`
   already lands script-made blocks with provenance, and slices 1, 2
   and 5 here cover what those ops build.
3. **`uff` as a rung of ours** (RDKit, already a dep) rather than the
   rented `ff`? Rec: yes; `ff` stays the name for a rented force field.
4. **Ad hoc probes as a new `view='inspect'`, stored rays staying on
   `view='ray'`?** Rec: yes — the 2r ray row is a stored ray; putting
   `section`/`overlap` on it means a ray-row migration for probes that
   need no storage.
5. **May a collapse-checked geometry be the emitted seed?** Rec: yes,
   labelled `relaxed_by`; the record stays the service relax.

## Rulings (Reto, 2026-10-29)

- Quick relax in the loop: yes, to see whether a structure collapses;
  cache it.
- A separate spec (this file), consistent with
  [structure-workspaces](structure-workspaces.md) and
  [pcb-workspace-code](pcb-workspace-code.md).
- Integrate the improved observation beam ([cad-inspect](cad-inspect.md))
  — DRY, one probe family.
