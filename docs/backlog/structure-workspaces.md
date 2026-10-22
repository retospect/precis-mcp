---
status: draft
title: Structure workspaces — scripts build in a sandbox, se stores and inspects, relax is a service
pillar: 3d-design
prio: high
model: fable
blocked-by: sandbox-python-script
---

# Structure workspaces — scripts in a sandbox, inspection in MCP

Reto, 2026-10-22 (approved direction): "agents that make structures — wean
them off structures and into script land, in a special sandbox? A container
with MCP-session-linked se context, where the agent can clear, run script,
use MCP to investigate the structure, edit the script in the box, repeat.
We don't need to replicate atom interfaces in MCP and Python. It allows
arbitrary modules, separate pips. Modularity lives in code, thinking and
inspection in MCP." Python is the only language (earlier finding, accepted).
Addition the same day: one `relax` interface across scales, macro included;
script outputs are testable artifacts, so CI gets cheaper.

Supersedes [se-modules-and-assemblies](se-modules-and-assemblies.md)
(§What happens to the modules spec). Builds on
[sandbox-python-script](sandbox-python-script.md) (`mode:script`, PyPI-only,
read MCP over a socket) and [agent-workspace-containers](agent-workspace-containers.md)
(persistent workspaces, disposable containers, host dirs under
`/var/lib/precis-sandbox/ws/`).

## Core design

1. **A workspace is a small Python repo linked to one se design.** Files
   live in the DB (a `folder` ref, `meta.workspace = {design, created_by}`)
   so the session MCP can edit them; each `run` stages them into the
   container exactly as `code_ref` folders are staged today
   (`executors/_sandbox_inputs.py::stage_code`) and commits the staged tree
   into the workspace's host git repo (`ws/<slug>/.git`, no remote, no
   creds) — git history is a by-product of running, the commit sha is the
   run's `script_sha`. One workspace ↔ one design; a design may have several
   workspaces; a run may target a scratch fork.
2. **The agent edits and runs; the container runs the script.** The agent
   never enters the container (v1; agent-in-container is the
   `mode:build` lane, gr329258 closed but the socket bridge is unbuilt).
   `run` = `sandbox_run mode:script` with `workspace:<slug>` on pollux or
   spark (W1: castor stays LLM-only) — never a Mac, never melchior
   (`sandbox_run.py::_sandbox_hosts`).
   No DB creds, `--network none`, cgroup caps, PyPI wheels only
   (`_sandbox_script.py::stage_deps`). The script reads existing blocks
   through the per-run read-only MCP socket (`_sandbox_read_mcp.py::spawn_read_mcp`).
3. **The script never writes the DB.** It writes `/work/out/manifest.json`
   plus one geometry file per block. Trusted service code validates and
   imports (§Import boundary) into the linked design as blocks carrying
   provenance. The sandbox ends at `/work/out`; the importer is the trust
   boundary.
4. **Replace, not clear.** A run replaces the blocks it made last time,
   keyed `(workspace, block name)`; blocks the new manifest omits are
   retired through the soft-delete trail (`store.retire_ref`/`restore_ref`,
   `src/precis/store/_refs_ops.py`; the se rows already carry `retired_at`,
   `precis_se/persist.py::save_tree`). An empty manifest = clear, undoable.
5. **No start-over.** se keeps storage, views, `validate`, `drc`, `measure`,
   renders, relax dispatch, catalogue. The generators become the importable
   core library (§Packaging); the construction-op surface shrinks on a
   timeline (§MCP surface).
6. **Relax is a service, cached.** Scripts never relax. A block's manifest
   entry declares `relax: {method, constraints}`; the importer dispatches
   the existing compute path and links the relaxed variant `derived-from`
   its seed. Lookups are keyed `(function, version, params sha, method)`
   so a script fetches "relaxed `prism(cccRcccRcccR, 4, 6)`, GFN2" instead
   of recomputing (§Relax).

Seam flavour, H side, cell lists, pitch: plain Python arguments.

## The loop, as the agent sees it

```
put(kind='workspace', id='hcol', args={'design': 'se:hcol-array-1'})
edit(kind='workspace', id='hcol/main.py', mode='replace', text='...')
put(kind='workspace', id='hcol', mode='run', args={'params': {'R': 4, 'P': 6}})
   → job id; the result inline when it finishes within wait_s (default 30)
get(kind='se', id='hcol-array-1', view='validate')      # inspect in MCP
get(kind='se', id='hcol-array-1', view='clearance', args={'a': 'p', 'b': 'guest'})
```

`main.py` is the entry: it reads `/work/params.json`, builds with the core
library and writes `out/`. A `precis_ws` helper (staged like
`/work/precis_client.py`) does the boilerplate:

```python
from precis_ws import out, params, blocks
from hexfold.sheetnet import prism
import precis_geom.assembly as pga
P = params()
p = prism(seams="cccRcccRcccR", rows=P["R"], periods=P["P"], junction="sp3-z")
out.block("p", p, ports=p.rims(), relax={"method": "gfn2", "periodic": True})
guest = blocks.get("guests#c60")          # read MCP, existing block
out.instance("guest", template="guests#c60", parent="p", pose="[0 0 2] nm")
pair = pga.assembly("pair", children=[p, pga.mirror(p, plane="yz")])
out.block("pair", pga.tile(pair, n=3, pitch="[0 0 1.2] nm"))
```

Core modules import under short stable aliases — `pg` for `precis_geom`,
`pga` for `precis_geom.assembly` — so examples, traces and skill text
spend no tokens on dotted paths (`pga.tile(...)` in a frame line).

Loop latency target: < ~10 s for small structures. Budget: claim tick ≤ 2 s
(the `job_claude_docker` lane polls on its own interval), warm venv 0–1 s
(a pre-built venv per lock sha, §Packaging), build 1–5 s, import 1–2 s.
Staging a new lock is the existing async stage job (gr496426) and happens
once per dependency set.

## Import boundary (trusted side)

`src/precis_se/workspace_import.py` (new; pure over the harvested tree,
store access only through the existing persist path):

- **Manifest** `out/manifest.json`: `{schema: 1, blocks: [{name, file,
  scale: 'atomic'|'macro', ports?, relax?, desc?, frame?}], instances: [...],
  connects: [...], notes?}`. `additionalProperties: false`; a bad manifest
  is the run's failure with `next=` naming the key. `out/trace.json`
  beside it is the run's construction trace (§Construction traces);
  a missing trace imports with `trace.missing` INFO, never a refusal.
- **Geometry files.** Atomic: `blocks/<name>.extxyz` (ASE is a core dep;
  `precis.structure` already exports extXYZ, the importer adds the reader
  beside `handlers/structure.py::_put_cif`). Macro: `blocks/<name>.cad.json`
  — the `precis.cad` graph (`precis/cad/graph.py`), the same object the
  DSL parses to, so a script may also emit the DSL string. Meshes (STL/3MF)
  are not accepted in v1: an imported mesh has no CSG distance field, so
  `relate`, `mold` and printability go blind.
- **Caps**, refused before any write: atoms per block, blocks per run,
  bytes per file, total `out/` bytes (the harvest cap, `_sandbox_harvest.py`).
- **Writes**, inside one `tree_mutation`: atomic → structure ref +
  `add_block` + `bind_structure` (the op `generate.py::finish_generate`
  already emits, `generate.py:1179`); macro → cad design + `add_block` +
  `set_binding(kind='cad')`; instances and connects as the ordinary ops.
  Every block's `meta.generated = {generator: 'workspace', workspace,
  run_job, script_sha, lock_sha, image_digest, params, requested_by}`
  (`generate.py::generated_record` gains the key set); `regenerate` on a
  workspace block means "re-run the workspace at that script sha".
- **Replace.** Blocks of this workspace absent from the manifest: retire
  (block rows via `save_tree`, the structure ref via `retire_ref`); the
  run echo lists them with the `restore` call. Same name present: the new
  block takes over the name, ports and connects re-resolve, a port that
  vanished is `port.missing` in the echo, not a silent drop.
- **Targets.** `args.target`: `'design'` (default) or `'fork'` — a copy of
  the linked design under `<slug>--<run>` (`persist.tree_to_json` →
  `tree_from_json`), linked `derived-from`; variants compare through the
  ordinary views and are promoted by the agent with `instance_block`
  (cross-design templates, `blocktree.types.parse_template_ref`) or by
  re-running against `'design'`.
- **Identity.** `requested_by` is the S10 `caller()` of the `run` call
  (`multi-user-overhaul.md` §S10: credential id for launched agents,
  `agent_id` for stamped subagents); until S10 lands, the connection
  principal. It is stored on the job and in `generated`, so a block says
  which agent, which workspace, which script made it.

## Code inside the design

Reto, 2026-10-22: the code and its uv environment are part of the se
design they generated — an se design is self-contained and regenerable
anywhere with `uv run`, not only referenced by sha.

- **Storage.** The workspace folder ref (`main.py`, helpers,
  `pyproject.toml`, `uv.lock`, `README.md`) is linked `part-of` the se
  design (the existing `part-of`/`contains` pair, `precis-relations`);
  `view='block'` and the design card list it. No new attachment kind:
  the folder is already what `stage_code` stages and the session MCP
  edits. Each run's import records `script_sha` + `lock_sha` on every
  block (`generated`), so the design always names the exact file set
  that made it; the folder's head may be ahead of the last run, and the
  echo says "2 files edited since run 17".
- **Self-contained export.** `persist.tree_to_json` gains a `workspaces`
  section: for each `part-of` folder, the files at the last run's
  `script_sha`, `uv.lock`, `params.json`, and the `precis-geom` wheel the
  lock pins (a few MB; `pyproject.toml` gets `[tool.uv.sources]` /
  `find-links = wheels/` so `uv run main.py` resolves offline except
  PyPI). Promoted agent-module wheels ride along the same way; OCP-sized
  deps do not (named, PyPI-resolved). `tree_from_json` restores the
  folder and relinks it. A design exported this way regenerates on a
  laptop with `uv run`, and on the cluster with `run`.
- **Fork.** `target:'fork'` and a design fork copy the folder ref to
  `<slug>--<run>` (`derived-from` the original workspace, `part-of` the
  fork) at the forking run's `script_sha`; the fork's host git repo is
  cloned lazily at its first run. Two designs never share a folder, so
  an edit in one cannot silently regenerate the other.

### Code storage — files in the DB, or git objects in the DB? (discussion, not ruled)

Reto, 2026-10-22, on W3: "I side with Fable" (files in the DB as a folder
`part-of` the design + a host git log — ruled) and "can we have git
blobs in the DB, or just the files in the DB? I kinda like that." The
three shapes, concretely:

| | (a) plain files in the DB (ruled baseline) | (b) git objects in Postgres | (c) files in DB + git bundle blobs |
|---|---|---|---|
| what is stored | one `folder` ref, one chunk per file, current content only; `script_sha` on blocks points at the host repo | tables `git_objects(sha PK, type, zlib bytes)` and `git_refs(repo, name, sha)`; a run = a commit row; se `design_revisions`/`generated.script_sha` FK to `git_objects.sha`; the DB **is** the repo | (a) plus a `git bundle` blob per run (or per N runs) attached to the run job |
| dedup across runs / forks | none in the DB (host git dedups on the host only); a fork copies every file | full: identical blobs share one row across runs, forks and workspaces; a fork is one new commit row | bundles repeat shared history unless incremental (`--since`), then restore needs the chain |
| diff / history cost | no history in the DB; "what changed since run 17" asks the sandbox host; history lost if the host dir dies | `git diff` semantics in SQL: walk two trees, compare blob shas — pure Python over rows or pygit2 with a custom Odb/Refdb backend; cheap, no host | unpack the bundle into a temp repo first (seconds, disk) |
| integrity | `script_sha` is a string nobody checks; the host repo can be reset (§Risks) | `generated.script_sha` is an FK; a block cannot name a commit the DB lacks; content-addressed rows verify themselves (`sha == sha1(header+bytes)`) | FK to the bundle row only; the sha inside is unchecked unless unpacked |
| tooling — can a human clone? | no; `tree_to_json` export + `uv run` | yes via export: `git fast-import` stream from the rows, or write loose objects + refs into an empty repo; pygit2 backends make it a live remote if ever wanted; no native `git clone pg://` | yes: `git clone <bundle>` directly, the most human-friendly |
| backup | rides the nightly dump | rides the nightly dump; rows are immutable so incremental backup is trivial | rides the dump; bundles are opaque blobs, larger |
| effort | 0 beyond slice 1 | M: migration (2 tables), a writer (`stage_code` → tree + commit rows, ~300 lines of plumbing: hashing, zlib, tree sorting), a reader for `view='source'`/diff, export via fast-import; pygit2 backends optional later | S: `git bundle create` on the host after each run, one blob column |

Recommendation (for discussion): **(b), as slice 1b after slice 1
lands** — not instead of it. (a) ships the loop; (b) gives the two
things (a) cannot: a real FK from an se revision to the code that made
it, and dedup when forks and reruns multiply. The host git repo then
becomes a cache materialised from the rows (no more two-sources-of-truth
risk; the host dir can vanish). Write path stays simple: the stager
already has the file set, hashing it into blob/tree/commit rows is
deterministic Python (no pygit2 needed to *write*); pygit2's custom
backends are the read/clone path only if someone wants `git clone`
against the DB. (c) is cheap but buys the least: humans can clone, the
DB still cannot check what a block cites. Not for this spec's slices
0–2; the decision gates slice 1b only.

## Assembly API — one implementation for ops and scripts

Reto, 2026-10-22: the programmatic interface includes tile (fused and
packed), mirror, rotate/translate/transform, and hierarchical assembly
(sub-assemblies as nested instances with their own frames, placed
relative to parent ports/anchors); each records a trace frame.

| operation | exists today | library call (`import precis_geom.assembly as pga`) | frame |
|---|---|---|---|
| nested instance, own frame | `instance_block` (`blocktree/ops.py::op_instance_block`, cross-design templates via `parse_template_ref`; blocks nest under `parent`) | `pga.assembly(name, children=[...], pose=)` / `pga.instance(template, pose=)` | `instance` |
| packed tile (copies at a pitch) | `array_block` (`precis_se/ops.py`, pitch × count) | `pga.tile(block, n, pitch=, fused=False)` | `tile#k` per copy |
| fused tile (one body, bonded across the seam) | atomic: hexfold `periods` + k2 fuse; macro: `cad.graph.Design.merge` | `pga.tile(block, n, period=, fused=True)` — atomic: translational copies + `junction(k=2)` across each boundary; macro: union | `tile` (one frame, `made` = seam bonds) |
| translate / rotate / transform | `set_pose` (`op_set_pose`); `cad.vec.Transform` (proper only) | `pga.translate(v)`, `pga.rotate(axis, deg)`, `pga.transform(T)` | `pose` |
| mirror | none | `pga.mirror(plane)` — improper transform; atomic emits `chirality.flipped` INFO, macro flips face normals | `mirror` |
| port-to-port placement | `connect` (ports; `clearance`, kinematic words) | `pga.place(child, at=parent.port('rim+'), align=child.port('rim-'))` — the same solve `connect` runs | `place` |
| fuse two blocks' atoms | `join` (`atomic/join.py`) | `junction(rims, k)` (multicell slice 1) | `junction` |
| bind geometry to a block | `bind_structure` / `set_binding` | `out.block(...)` (manifest) | `block` |

DRY rule: the geometry math (pitch expansion, pose composition, port
alignment, fused-tile seam) lives once in `precis_geom.assembly` (`pga`);
`precis_se/ops.py` keeps naming, validation and tree bookkeeping and
calls the library for the numbers. A script and an MCP op on the same
inputs produce the same block (a golden asserts it). Sub-assemblies: an
`assembly` is a block with children; poses compose through the parent
chain as today (`blocktree` parents); its own frame is the `assembly`
call, children carry their own, so `frame:` on the assembly selects the
subtree. Op-path calls record an `op` frame (`tool_ledger` call id as
`line`) so both paths answer "who made it".

## Relax — one interface, two scales

The script emits a seed plus declared constraints; relax is MCP compute,
never inside the sandbox; the service returns solved geometry plus
findings. The manifest entry is the same shape at both scales:

```json
"relax": {"method": "gfn2" | "uff" | "mlip" | "assembly",
          "constraints": [...], "periodic": true, "fmax": 0.05}
```

**Atomic (exists).** The ladder `precis.structure.relax.relax` (rungs
`ml/emt/xtb/geo/clean`), the hexfold job `precis_se/hexfold_relax_job.py`
over `precis_se/atomic/relax_block.py` (junction fragments from
`meta.generated.junctions`), tethered relax (`hexfold-tethered-relax-op.md`).
Constraints: `fixed` atoms, pinned zones, cell. New: the cache key and the
`derived-from` link from relaxed variant to seed; GFN2 dispatch from the
session host is the open dependency the multicell spec names (gap 5).

**Macro (`method: 'assembly'`).** Reto: "it fails if tolerances are
overconstrained, if things don't fit, and that sort of thing." What the
findings are and where each comes from:

| finding | exists | where |
|---|---|---|
| overconstrained — which constraints conflict | partly | word-level in `precis_se/drc.py` (one connect overconstrained by a kinematic word, ~L881); rank-aware for axial members only: `precis/structsolve/complementarity.py::check_completeness`. **New:** redundancy analysis over the connect graph (Jacobian rank of the mate constraints, naming the redundant set) |
| underconstrained — free DOFs | yes | `precis_se/freedom.py` (kinematic freedom inventory, `view='freedom'`); `check_completeness` → `mechanism` |
| interference / does not fit — pairs + depth | yes for cad-bound blocks | `precis/cad/relate.py` (CSG signed-distance clearance/interference per component pair); `precis_se/validate.py` `undeclared_interpenetration`; `view='clearance'` |
| clearance below minimum | yes | `view='clearance'` + drc minimums (`precis_se/modes.py` process rows) |
| tolerance stack-up — worst case, which stack fails | yes | `precis_se/measures.py::stackup` (worst-case linear) |
| tolerance stack-up — RSS | no | named in `measures.py` as the later refinement; small |
| mold pull / undercut | yes | `precis_se/mold.py` (`mold.undercut`), `precis/cad/mold.py` |
| geometry solve (move parts to satisfy mates) | partly | `precis_se/formfind.py` → `structsolve.form_find` for axial members only. **New:** a mate solver — not in v1 (§Open questions 4) |
| physical settle (gravity/contact), FEA deformation | no | later slices, named, not built |

So macro relax v1 is a **check-and-report** service: it runs the existing
checkers over the imported blocks as one findings list, adds the
redundancy analysis and RSS, and solves geometry only where formfind
applies. The script, being code, emits exact poses; a solver that moves
parts arrives when a case needs it. The result shape is the same object
at both scales: `{geometry, findings[], method, seed, constraints}`.

**Cache.** Table `relax_cache(fn, version, params_sha, method) →
structure/cad ref` (migration). The read MCP serves
`search(kind='structure', args={'relaxed': {fn, version, params_sha,
method}})`; a hit gives the relaxed coordinates through the existing
`extxyz` view, a miss returns the seed and the importer queues the relax.
`fn` and `version` come from the core wheel (qualname + package version),
so an agent module's function is cacheable only once promoted.

### Relax hierarchy — motifs, modules, interfaces, full

Reto, 2026-10-22: "we like the atomic assembly but pre-relaxed modules
are nice too — for compute? Or can we build them closer to relaxed
state?" Both: seeds land near the minimum, and assembly relaxes only
what assembly changed. Four levels; each atom's coordinates say which
level and method last moved them.

- **(a) Motif templates.** A motif is a local geometry the generators
  mint: fold `c`/`r`/`C`/`R`, k3 `sp3-z` and `sp3-a` seam, k4 seam,
  notch, cap rim (H termination), decorated column. `relaxed_motifs`
  stores, once per `(motif, method)`, the mean internal coordinates
  (bond lengths, angles, dihedrals over the motif column and its first
  neighbour shell) measured from relaxed units — the service finds each
  motif instance through the unit's trace (`frame:` of its `junction`/
  `decorate`/`terminate` frame) and averages. `sheetnet.junction`,
  `decorate` and `_block_from_net` take `motif=` and place atoms with
  those values before the stick/tether pass (the cheap geometric step);
  the hard-coded constants (`DIMER_A` 1.60 Å, 1.09 Å C–H) become the
  fallback template `method: 'seed'`. A frame records `args.motif`; a
  template is versioned and replaced when a new relax at the same
  method differs by > 0.02 Å / 2°.
- **(b) Module cache.** §Relaxed units: whole periodic units relaxed as
  infinite, reused by `(fn, params, method, cell)`.
- **(c) Assembly: interfaces only.** `relax.scope: 'interfaces'`: the
  free region is every atom within `r` (default 6 Å) of the `made ∪
  touched` set of the run's `junction`/`place`/`tile(fused)`/`terminate`
  frames — frames, not geometry heuristics, say where assembly changed
  things; module interiors are `fixed` (the `relax_block.py` fragment
  machinery with `fixed` masks, generalised from `generated.junctions`
  to frames). After convergence a single point measures forces on the
  frozen atoms; `max|F| > relax.residual_fmax` (0.1 eV/Å) widens the
  shell by 2 Å and repeats, at most three times; what remains is the
  finding `assembly.strain` with the force, the atom and its frame — so
  assembly strain is never hidden by freezing. Each pass is a `service`
  frame `relax(scope=interfaces, method, shell=r)` touching the free
  atoms; frozen atoms keep their unit's relax event.
- **(d) Full relax.** `relax.scope: 'full'` on request, for final
  numbers and validation; same frame record, `scope=full`.

Rules: never mix methods across modules in one relaxed assembly — the
importer refuses `relax.method_mix` naming the units and `next=` the
one-method fetch (a ladder *within* one module, `ml` pre-relax then
`xtb`, is fine and records both events). Coordinate provenance per atom
= its last `relax` modifier frame (`level ∈ motif|unit|interfaces|full`
+ method), read by `view='trace'` `touched: true`. MACE (`ml`,
`route_ml_model`) is the cheap pre-relax rung whenever
`PRECIS_MLIP_NODES` is set; `uff` stays finite-only preview.

Acceptance (numbers written into this item): for `cccRcccRcccR` (4, 6)
and the tri-tube, RMSD of motif-seeded vs fully GFN2-relaxed geometry
and GFN2 step count before/after templates (target: steps halve); an
interfaces-only relax of the tri-tube from three cached cell units vs
its full relax: RMSD < 0.05 Å and residual force under threshold, or
`assembly.strain` names where not.

### Periodic ("infinite") relax

Reto, 2026-10-22: "SE relaxations is a separate job; can we have DFT-like
relaxations — relax as if infinitely sized." Ends and caps distort a
finite relax; the unit of relaxation is the translational period, not
the block.

- **Declaring periodicity.** The block's extXYZ already carries a
  `Lattice`/`pbc` header (`precis.structure.export::to_extxyz`); the
  manifest selects the axes: `relax.periodic: ['z']` (tube/prism axis),
  `['x','y']` (a sheet), `['x','y','z']` (bulk). The script knows the
  period (`periods × lattice period` for hexfold) and writes the cell;
  the importer pads non-periodic axes to ≥ 10 Å vacuum and refuses a
  periodic axis whose rim atoms do not match across the boundary
  (`relax.period_mismatch`, naming the pair) — a bare `periodic: true`
  is the 1D tube case.
- **Fixed vs variable cell.** `precis.structure.relax.relax(cell=)`
  has `fixed | inplane | full` (Voigt masks, `CELL_MODES`). New:
  `axis` — frees only the periodic axis strain (mask `zz` for a tube),
  "optimise the lattice period too". Defaults by periodicity: 1D →
  `axis`, 2D → `inplane`, 3D → `full`; `fixed` on request.
- **Ladder.** `uff` (RDKit, finite only — RDKit's UFF has no periodic
  images; the prototype's preview rung) → `gfnff` (xtb-python, periodic
  since xtb 6.4; **new** optional extra, the gated `ff` rung in
  `_RENTED_RUNGS`) → `xtb` = GFN2-xTB (tblite, periodic, Γ only — tblite
  has no k-points; exists) → `ml` = MACE-MP (`route_ml_model`; periodic,
  no k-point concept) → `dft-fast | dft-tight` = GPAW in the container on
  `PRECIS_DFT_NODE` (`struct_relax.target_node_for`). Nothing on the
  request path takes k-points today.
- **k-points vs supercell.** One rule, applied by the service and
  recorded in the cache key: a Γ-only rung (`xtb`, `gfnff`) relaxes a
  supercell of `n` periods with `n·L ≥ 15 Å` along each periodic axis
  (the prototype used 4 periods, Γ); a DFT rung relaxes the unit cell
  with `kpts_i = ceil(2π / (L_i · kspacing))`, `relax.kspacing` in Å⁻¹
  (default 0.2). The script declares neither `n` nor a grid; it may pin
  `relax.supercell: [1,1,n]` for a convergence study.
- **Unit → finite object.** The relaxed periodic unit is a structure ref
  with `pbc`. A finite block is `tile(unit, n=P)` + termination
  (`_block_from_net`, H caps): interior atoms keep relaxed coordinates
  and their maker frames, the end rims are new atoms made by the
  `terminate` frame; an optional tethered end relax
  ([hexfold-tethered-relax-op](hexfold-tethered-relax-op.md)) with the
  interior pinned settles the caps. `tile` and `terminate` are `core`
  frames in the consuming run's trace. Relax stays a `struct_relax`
  compute job (`struct_runs` cache); never in the sandbox.

### Relaxed units — pre-built, pre-relaxed, with code refs

Reto, 2026-10-22: "because of assembly, pre-relaxed pre-built se things
should also be manageable (with their code refs somehow)." The relax
cache above becomes a catalogue of reusable **relaxed units**.

- **Entry.** `relaxed_units` (the `relax_cache` migration, widened):
  `fn` qualname, package `version` + wheel sha, `params_sha`, trace
  `root` key, `method`, `cell` mode, `supercell`/`kspacing`, energy per
  atom, `converged`, `fmax`, `structure_ref` (the periodic unit),
  `stale`. Each unit is also a block in the library design
  (`precis_se/library.py`), bound to that structure ref with
  `generated = {generator: 'unit', fn, version, params, trace: {run_job,
  root}}` — so it has `view='block'`, `validate`, renders and the
  regenerate path like any block.
- **Fetch.** `blocks.relaxed(fn, params, method, cell)` on the read MCP
  (the `search(kind='structure', args={'relaxed': ...})` above): a hit
  returns the unit's geometry and trace root — no recompute; a miss
  returns the seed, queues the relax, and the echo says the next run
  will hit. Assembly then `tile`s and `place`s it.
- **Trace across runs.** The consuming run's trace gets a `unit` frame
  (`args = unit id`) whose children are the unit's own trace, linked not
  copied; atom → trace reads `main.py:12 build → fetch(unit u123) ⇒
  prism(cccR…,4,6) → junction#3`, then the unit's `relax(gfn2, axis)`
  service frame as a modifier. Creator frames survive tiling (same
  maker, new ordinals); the tile index is the `tile` frame's `occ`.
- **Invalidation.** When the core wheel version moves, a unit whose `fn`
  golden (slice 7) differs between the two versions is flagged
  `stale`; a unit whose golden is byte-identical is re-stamped current.
  A fetch of a stale unit still returns it, with finding `unit.stale`
  and `next=` naming `regenerate`; `regenerate` builds and relaxes at
  the new version, the old unit retires. Never silent reuse, never a
  recompute storm on a no-op bump.
- **Listing.** `search(kind='se', tags=['unit'], q=...)` and
  `view='units'` on the library design: one row per unit — `fn`,
  params digest, method/cell, periods, energy/atom, converged, stale.

## Construction traces — who made this atom

Reto, 2026-10-22: "Any object in 3D space should have an origin — who
made it. If it was made from code, it should have a stack trace. The
stack trace is a first-class object — token efficient and navigable in
se. Pick an atom and ask which stack trace put it here; navigate the
trace and ask: this frame, which atoms did it place? Conversely, for a
set of atoms: the most specific frame that placed them, and what else
did it place."

**The object.** A run's **construction trace** is a tree of **frames**.
A frame is one call that placed or changed geometry: `{key, parent,
qual, file, line, occ, kind, args, made, touched}`. `kind` is `user`
(a function in `/work/**.py` — `main.py` and the agent's helpers),
`core` (a public `precis-geom` function: `sheetnet.strip|pore|place|
junction|assemble|decorate`, the layouts `prism|cells|...`,
`precis.cad.graph.Design.prim|merge|subtract|intersect|pattern|instance`,
`precis_ws.out.block|instance|connect`), `module` (a promoted agent
module's public functions) or `service` (relax, §Relax). Library
internals (`join._seam_k3`, `_block_from_net`) never appear — they
collapse into the public frame that called them. `occ` is the
occurrence counter of this `(qual, line)` under the same parent, so the
7th `junction` call in a loop is `#7`. `args` is a bounded digest: scalars
and short strings verbatim, arrays and blocks as `<strip 48 atoms>`
plus a sha8. `made`/`touched` are counts per entity type (atoms, bonds,
instances, faces, blocks, ports) for the frame alone; the subtree sum is
derived.

**Stable key.** `key = sha8(parent.key, qual, line, occ)` — the path of
call sites from the root. It does not depend on run id, memory address or
atom count, so an unchanged script yields identical keys on every run;
a loop that gains an iteration changes only the new sibling's key.
Run-local ids are small ints interned in the trace; keys are what
findings, diffs and selectors cite.

**Capture.** In the core wheel, `precis_geom.trace`: a `@traced`
decorator on every public primitive pushes a frame onto a context-local
stack, and on entry walks `sys._getframe()` upward to mint the *user*
frames between the previous traced frame and this call (filtered to
`/work/**`; `sys.setprofile` is rejected — it instruments every call,
2–5× on the agent's loops, and sees internals it would have to filter
out). Cost: one frame walk of ≤ 20 Python frames per primitive call;
primitives run hundreds of times per build, not millions. Every entity
a primitive returns is stamped with the leaf frame's id (atoms and bonds
on the `sheetnet` block / `Net`, instances on `Design`); a primitive
that changes existing entities — `junction` fusing two rims,
`decorate` relabelling carbons sp3 — records them in `touched` as
ordinal ranges, not lists. `precis_ws.out` writes `out/trace.json`
(`{schema: 1, run, frames: [...]}`) beside the manifest. `with
trace("name"):` is optional sugar that names a user frame; nothing
requires it. build123d scripts get frames only from the conversion
call (OCP internals are untraced) — a known limit, stated in the skill.

**Storage.** One trace per run, stored once as a chunk on the run job
(≤ 64 KB typical; hard cap 20k frames — past 64 same-site childless
siblings they run-length collapse into one frame with an `occ` range,
and past the cap the importer still imports and emits
`trace.truncated`). Per entity: **one small int**, the creator frame
id — atoms as a per-atom column of the structure (`maker`, beside the
extXYZ `label` column `precis.structure.export::to_extxyz` already
carries, so it rides relax versions), bonds in the bond table, cad
instances on `Instance`, se blocks/ports/connects in the manifest entry.
Faces and edges are derived from instances (`primitives.Face.tag` +
instance), so face → instance → frame. Modifier frames are not stored
on the entity: "which frames changed this atom" is a scan of the run's
`touched` ranges, served from the one stored trace. Every block's
`generated.trace = {run_job, root}`.

**Queries, as se views** (`get(kind='se', id=..., view='trace', args=...)`):

| ask | args | rendering (tokens) |
|---|---|---|
| atom → who put it here | `{'atom': 'p/w3/(2,1,A)'}` (an `AtomPath`, ordinal or `#uid:path`) | one line per frame root→leaf: `main.py:12 build → main.py:30 bundle(R=4,P=6) → cells(cells=3,fold_h={core:reflex}) → assemble → junction(k=2,fold,R)#7 · made 24 atoms 48 bonds, touched 96` (~20 tokens per frame, ≤ 8 frames) |
| frame → what it placed | `{'frame': '<key>'}` | the frame line, `made`/`touched` counts for it and its subtree, children as one line each with counts, three sample paths, and the selector `frame:<key>` — never the atom list |
| set → most specific frame + what else | `{'atoms': <selector or list>}` | the LCA over the frame tree of the set's creators, coverage (`covers 20/20; it also made 4 not in your set → frame:<key>/rest`), and the sibling frames the set strays into |
| atom → who changed it | `{'atom': ..., 'touched': true}` | the creator line plus one line per modifier frame (junction fuse, decorate, relax) in event order |
| run vs run | `{'against': <run_job>}` | by key: frames added / removed / with changed `args` or `made` counts; unchanged subtrees fold to one line |

**Selector.** `frame:<key>` joins the `patch:/ring:/sites:/atoms:`
family (`precis-se-regions-help`): the atoms the frame's subtree made
(`frame:<key>/touched`, `/rest` as above; `frame:<key>!` the frame alone,
not its children). Pockets, measures, `validate` scope, render highlight
and `clearance` then take a frame like any region, so no view needs a
trace-specific code path.

**Findings close the loop.** Every atomic finding's subject is already
an atom path (`atomic/validate.py` `subject=path_str`); the report gains
a `frame` column rendering the nearest *user* frame of the subject's
creator — `prism.hcol.clash  p/w3/H/(2,1,A)  main.py:30 bundle → junction#7`
— so the agent reads a finding and knows the script line to edit. Macro
findings (`drc`, `clearance`, `freedom`) subject blocks and ports; the
same column resolves through the manifest entry's frame. A set of
findings → LCA gives "the one call that made all the clashing atoms".

**Both scales.** Atomic: atoms, bonds. Macro: cad instances and the
named components, faces/edges by derivation, se blocks/ports/connects by
their `out.*` call. Surviving small ops (`add_block`, `set_pose`,
`connect`) write an `op` frame (parent root, the `tool_ledger` call id as
`line`) so "who made it" has one answer at both paths (§Guardrails, one
way).

**Join with the `python` kind.** Workspace code gets a python-kind root
`ws-<slug>` (beside `main`, `repo`, `wt-<tree>`;
`handlers/python.py::parse_python_roots` gains a DB-backed root that
indexes the folder ref at the last run's `script_sha`), so a user frame
`main.py:30 bundle` *is* `ws-hcol::main.bundle` — the trace line carries
that id (~8 tokens) and `get(kind='python', id='ws-hcol::main.bundle')`
works as for any symbol (`callers`, `source`, `impact`). The reverse is
`view='placed'` on a python id: per design built from this workspace
(latest run by default, `args.run=` for another), the frames whose
`qual` matches — occurrences, `made`/`touched` counts per entity type,
and one selector `frame:qual=main.bundle` (the union of occurrences) —
never atom lists. Opt-in: `placed` is its own view and costs one trace
read; no other python view changes, and workspace roots are indexed only
when the workspace has a run.

**Relax.** Relax never changes identity: relaxed atoms keep their
creator frames (the relax job writes the scene's next version in place,
`hexfold_relax_job.py` write-back). The importer appends a `service`
frame `relax(gfn2, periodic)` with `touched` = atoms displaced > 0.01 Å,
`made` = none; a cache hit copies that frame from the cached variant
with `args.cache = hit`. So "who moved this atom" lists the relax after
the junction that placed it.

**Risks.** *Trace bloat*: a per-atom loop calling `decorate` 2k times —
the sibling collapse and the cap above; the echo says how many frames
collapsed. *Nondeterministic keys*: keys never include ids or addresses;
`args` digests format floats `%.6g` and hash arrays by content. *Agent
reads a traceback*: no view renders internals, file paths outside
`/work`, or more than the frame line. *Cost*: measured in slice 2 beside
the latency number; the budget is < 5 % of build time.

## Packaging

- **Core packages** (maintained, golden-tested, in this repo): `hexfold`
  (the `sheetnet` layer after multicell slice 1 is the atomic API),
  `precis.cad` (dsl, primitives, relate, mold, printability; manifold3d
  CSG; OCP optional), `precis.structsolve`, `precis.structure` (scene,
  export, readers). They ship to the container as one wheel,
  `precis-geom`, built from this repo at the release sha
  (`packaging/geom/pyproject.toml`, packages listed, no `precis.store`
  import — a test asserts the import graph). RDKit and ASE ride along as
  deps; tblite stays a compute-node package.
- **Agent modules** are workspaces tagged `module`: `README.md`, `examples/`,
  tests, a `pyproject.toml`. Catalogued by the MCP
  (`search(kind='workspace', tags=['module'], q=...)`, the docs chunked
  like any folder). Another workspace depends on one by name once it is
  **promoted**: `scripts/ws-promote <slug>` builds the wheel after review
  and publishes it. Promotion is the review gate; core status (moving
  into this repo) is a later, separate decision.
- **Private index**: a `--find-links` directory on the sandbox hosts,
  `/var/lib/precis-sandbox/index/` (NFS-shared like the workspace dirs,
  S2 decision), fed by `ws-promote` and the release build. No server:
  `pip download --find-links` already reads it; `deps` may name
  `precis-geom==<ver>` and promoted modules. PyPI stays the only network.
- **Warm venv**: the stage job builds `/var/lib/precis-sandbox/venvs/<lock sha>/`
  once; `build_script_argv` mounts it read-only instead of `uv venv` per
  run (`claude_docker.py::build_script_argv`).
- **Scales.** Atomic: hexfold, RDKit, ASE. Macro: `precis.cad` is the
  core; build123d or CadQuery are allowed agent-module deps (PyPI wheels,
  OCP ~200 MB, inside the 500 MB wheel cap) but the importer accepts only
  the `precis.cad` graph, so a build123d script converts at the end — the
  conversion helper is an agent module until someone needs it in core.
  **pcb does not belong** in v1: boards have their own placer, netlist
  and round trip; a pcb block enters se through `set_binding(kind='pcb')`
  as today.

## MCP surface

Ops that **stay** (small edits, placement, declarations): `add_block`,
`remove_block`, `set_pose`, `set_desc`, `add_port`/`remove_port`,
`instance_block`, `array_block`, `connect`/`disconnect`, `set_joint`,
`set_load`, measures, pockets, `set_mode`, `set_binding`, bom, notes,
`declare_*`, states, chain ops, `formfind`, `set_envelope` with a DSL
string (a one-line macro edit is not a script).

Ops that **move to Python** (construction): `generate` (every
`hexfold_scene` feature type, `typed_features._BUILDERS`), `join`
(`precis_se/atomic/join.py`), `terminate`, and the never-built `stamp`,
`cap`, `decorate` ops of the modules spec. Ruled W5 (Reto, 2026-10-22):
they are legacy — "revision, uv and code will displace it; no real users,
all is dev for now". No 30-day clock, no thin-wrapper phase: the slice
that makes workspace `run` usable (slice 1) removes them in the same
change — the op refuses with `next=` naming the workspace path (the
`seam.sp2_retired` pattern) and their skill text goes with them; stored
blocks keep their coordinates and `regenerate` keeps working through the
core wheel (same functions). Until slice 1 lands they stay as-is (nothing
else to build with). Pose, connect and the small edits stay.

Inspection stays and grows in MCP: `validate`, `drc`, `clearance`, `sites`,
`catalogue`, `mold`, `stability`, `freedom`, renders, `view='block'` now
rendering the workspace provenance with the one `run` call that reproduces
the block, and `view='trace'` with the `frame:` selector (§Construction
traces).

## Guardrails, checked

- DRY: one builder layer (`sheetnet`), one op table, one relax interface,
  one import path; definitions are functions, examples are tests.
- Tools, not black boxes: every block is a script sha + params anyone can
  re-run; the echo names the reproducing call.
- Retire broken things: `prism-k3-120-z` refusal stands; `generate`/`join`/
  `terminate` retire with slice 1 (W5).
- Secrets: the container has none; the read MCP token rides the child's
  env (`_sandbox_read_mcp.py::build_serve_env`); the agent sees a job id.
- One way: a structure is made by a script or by a surviving small op,
  never by both for the same block (`generated.generator` says which).
- Reproducibility: `regenerate` = re-run at `script_sha` with `params`
  against `lock_sha` and `image_digest`.

## Slices (smallest useful first)

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 0 | **Importer + goldens, no container.** `precis_se/workspace_import.py`, manifest schema, extXYZ reader (with the `maker` per-atom column), cad graph import (`frame` on instances), `trace.json` reader stored once on the run, replace/retire semantics; `tests/workspaces/` with a fixture tree and golden JSON (counts, coordinate SHA as `tests/hexfold/data/goldens`, findings, trace key set) | `atomic/generate.py::finish_generate` (bind path), `persist.save_tree`, `handlers/structure.py` readers | Importing the fixture into a dev design makes blocks with full `generated` (incl. `trace`); a second import with one block dropped retires it and `restore_ref` brings it back; a manifest with an unknown key refuses naming it; a 25k-frame trace imports with `trace.truncated`; goldens pass on main | M, 1 build |
| 1 | **Workspace kind + `run`.** `folder`-backed workspace ref linked `part-of` its design, with `edit` on files; `sandbox_run` `params.workspace`; staged files committed to the host git; import on exit; `requested_by` from `caller()` when present; inline result within `wait_s`; fork copies the folder; `tree_to_json` embeds files + lock + wheel; **removes `generate`, `join`, `terminate` (refusal naming the workspace path) and their skill sections (W5)** | `sandbox_run.PARAMS_SCHEMA`/`semantic_rejection`, `_sandbox_inputs.stage_code`, `_sandbox_harvest`, `claude_docker` terminate path, `persist.tree_to_json`/`tree_from_json` | The loop above runs end to end on the dev lane; the block's `generated.script_sha` equals the host repo's HEAD; two runs on one workspace serialize (the agent-workspace-containers lease); an exported design restores with its folder and `uv run main.py` rebuilds the block byte-identical on a laptop; `generate` refuses with the workspace `next=` and no skill names it | L, 2–3 builds; needs sandbox slice 4 (lane live) |
| 1b | **Git objects in Postgres** (gated on §Code storage, not ruled). `git_objects`/`git_refs` migration, stager writes blob/tree/commit rows, `generated.script_sha` FK, `view='source'`/diff over rows, export via `git fast-import`; host repo becomes a materialised cache | `_sandbox_inputs.stage_code`, `persist`, `tree_to_json` | Two runs sharing files share blob rows; a block cannot cite a commit sha the DB lacks (FK); `fast-import` export clones and `uv run` rebuilds byte-identical | M, 1–2 builds; only if ruled |
| 2 | **`precis-geom` wheel, index dir, warm venv, trace capture.** `packaging/geom/`, import-graph test, `scripts/ws-promote` (build only), venv per lock sha; `precis_geom.trace` (`@traced`, user-frame walk, `occ` counters, sibling collapse) on every `sheetnet` and `cad.graph` public function, `precis_ws.out` writing `trace.json` | `_sandbox_script.stage_deps`, `build_script_argv`, release scripts; `sheetnet.py`, `cad/graph.py` | Container imports `hexfold.sheetnet` and `precis.cad` offline; a small prism run completes in < 10 s wall from `run` to echo on a warm lane (measured, written into this item, trace overhead < 5 % of build); two runs of an unchanged script give identical trace keys; three `junction` calls in one loop are `#0..#2`; a fixture with 2k same-site calls collapses past 64 | M, 2 builds |
| 2t | **Trace views + selector.** `view='trace'` (atom→trace, frame→placed, set→LCA + coverage, touched, run diff); `frame:<key>` in the regions family; `frame` column on `validate`/`drc` reports (nearest user frame) | `precis_se/handler.py` views, the region-selector parser (`precis-se-regions-help`), `atomic/validate.py` report render | A seam atom's trace names `junction#k` and a `main.py:<line>`; LCA of one strip's atoms is its `strip` frame at 100 %, of one atom from each of two strips the layout frame; a pocket over `frame:<key>` holds exactly the frame's `made` count; a finding row carries the script line; token count of a 6-frame trace ≤ 150 | M, 1 build; after 2, before 3 so the proof records trace cost |
| 2p | **Python-kind join.** `ws-<slug>` DB-backed root, python id on user-frame trace lines, `view='placed'` with `frame:qual=` selector | `handlers/python.py` roots, `_python_render.py`, `view='trace'` | `get(kind='python', id='ws-hcol::main.bundle', view='placed')` lists the design, 3 occurrences and counts; the selector resolves to the same atoms as the three frames' union; an unrun workspace has no root | S–M, 1 build; after 2t |
| 2a | **Assembly API.** `precis_geom.assembly`: `tile` (packed, fused), `mirror`, `translate`/`rotate`/`transform`, `place` (port-to-port), `assembly`/`instance`; `array_block`, `instance_block`, `set_pose`, `connect` rewired onto it; frames per call; `op` frames on the op path | `precis_se/ops.py`, `blocktree/ops.py`, `cad/vec.py` (improper transforms), `sheetnet.junction` | `array_block` and `tile(fused=False)` on one template give byte-identical trees; `tile(fused=True, n=3)` of a 2-period prism equals the 6-period prism's SHA; `mirror` of a chiral tube flips the chiral index and emits the INFO; a nested `assembly` places through its parent's port and `frame:` on it selects every child atom | M–L, 2 builds; after 2, before 6 |
| 3 | **Proof: the 12-wall H-column prism both ways.** `cccRcccRcccR` at (4, 6) via `generate` and via a workspace script; record tokens (`tool_ledger` bytes per path), wall time, errors/retries, and SHA equality of the two blocks (the `generate` leg runs at the pre-slice-1 sha or through the core wheel directly, since the op is gone by then) | multicell slice 3 (fold tokens) must have landed | Numbers in this item; both blocks byte-identical; the script path is cheaper or the gap is explained | S, after multicell slice 3 |
| 4 | **Relax interface, atomic + cache.** Manifest `relax:` incl. `periodic`/`kspacing`/`supercell`, `relaxed_units` migration, `derived-from` link, read-MCP `relaxed` lookup, importer dispatch to the existing job types, vacuum padding and `relax.period_mismatch` | `hexfold_relax_job.py`, `handlers/structure.py::_dispatch_relax`, `_sandbox_read_mcp.kind_refusal`, `structure/cache.py` key | A second run after the relax finished reads the relaxed geometry; the variant links to its seed; the cache key matches across runs with equal params and differs by `cell`/`supercell`/`kspacing`; relaxed atoms keep their creator frames and the trace gains one `service` frame whose `touched` equals the displaced set; a 1D-periodic manifest relaxes a `pbc` unit, not the capped block | M, 1 build; GFN2 route is the known dependency |
| 4p | **Infinite-relax ladder gaps.** `cell='axis'` mask; supercell rule for Γ-only rungs; `kspacing` → `kpts` on the `dft-*` request path and in the GPAW container; `gfnff` rung (xtb-python extra); `tile` + `terminate` + tethered end relax as the unit→finite path | `structure/relax.py` (`CELL_MODES`, `_CELL_MASKS`), `workers/job_types/struct_relax.py`, `sheetnet`, `hexfold-tethered-relax-op` | A 1D unit relaxed `axis` at `xtb` reports the period change; the same unit at `dft-fast` records `kpts` from 0.2 Å⁻¹; `tile(unit, 6)` + H caps gives the census of the (4, 6) A2 target with interior coordinates equal to the unit's; `gfnff` on a periodic cell runs or refuses with the install hint | M, 1–2 builds; DFT part needs `PRECIS_DFT_NODE` |
| 4m | **Motif templates.** `relaxed_motifs` migration, extraction from relaxed units through their frames, `motif=` on `junction`/`decorate`/`_block_from_net`, `seed` fallback, versioning | `sheetnet.py`, `hexfold_relax_job.py` write-back, `relaxed_units` | Motif-seeded A2 and tri-tube seeds: RMSD vs GFN2 minimum and step counts recorded before/after; slice-0 goldens unchanged under `method: 'seed'` | M, 1 build; after 4u |
| 4a | **Interface relax.** `relax.scope` interfaces/full, frame-derived free region, frozen-atom residual force loop, `assembly.strain`, `relax.method_mix` refusal, per-atom level in `touched` | `relax_block.py` fragments, `hexfold_relax_job.py`, `view='trace'` | Tri-tube from three cached units: interfaces relax within 0.05 Å RMSD of full or names the strain; a mixed-method fetch refuses; every atom reports its level | M, 1 build; after 4u, 2t |
| 4u | **Relaxed-unit catalogue.** Units as library-design blocks with `generated.generator='unit'`, `blocks.relaxed()` fetch, the `unit` trace frame linking the unit's trace, golden-diff `stale` on wheel bump, `unit.stale` finding, `view='units'`, `tags=['unit']` search | `precis_se/library.py`, `workspace_import.py`, `view='trace'`, slice 7 goldens, `scripts/release` (the stale pass) | Two runs with equal `(fn, params, method, cell)` compute once; a fetched unit's atom trace crosses into the unit's frames and ends in its relax frame; a wheel bump that changes `prism`'s golden flags its units stale and a no-op bump flags none; `regenerate` on a stale unit retires it | M, 1 build; after 4 and 7 |
| 5 | **Macro relax v1: check-and-report.** `method:'assembly'` = findings from the table above as one list; new redundancy analysis and RSS; formfind where axial | `drc`, `freedom`, `cad.relate`, `measures.stackup`, `mold`, `formfind` | A fixture with a redundant mate names the redundant set; an over-tight stack names the failing chain under worst-case and RSS; an interfering pair reports depth | M–L, 2 builds |
| 6 | **Agent modules + skill.** `module` tag, catalogue search, `ws-promote` publish, `precis-workspace-help` (loop, manifest, relax, packaging, the Å-vs-macro table from the modules spec), pointer lines in `precis-se-help`, `precis-se-atomic-help`, `precis-job-compute-help`; a trace section (the five asks, `frame:`, the build123d limit) and the alias convention (`pg`, `pga`; every example uses them) in the workspace skill and a line in `precis-se-regions-help`; glossary: workspace, run, promote | `src/precis/data/skills/`, `docs/glossary.md`, `precis_se/library.py` | A second workspace depends on a promoted module by name and runs | M, 1 build |
| 7 | **CI goldens for core scripts.** Each core generator gets an `examples/` script whose output goldens (counts, SHA, findings) run in `scripts/test` without a container — the multicell slice-0 goldens become this | `tests/workspaces/`, `scripts/impacted-tests` | A change to `sheetnet` that alters a golden fails the selected test naming the script | S per generator, with each slice |

Later, named: agent-inside-container (socket bridge for `mode:build`);
macro mate solver; physical settle and FEA; mesh import; Mac lanes.

## What happens to the modules spec

[se-modules-and-assemblies](se-modules-and-assemblies.md) is superseded.
What carries over: definitions are Python functions (`params → block`) in
core or agent modules; `examples` are golden tests (slice 7); `expose` is
the manifest's `ports`; `stamp` is a function call; `cap` is a function;
`decorate` is the `sheetnet` primitive (multicell slice 2), not an op;
`view='definition'` is the module's catalogued docs; the memory rule
("a reusable construction goes to code, the lesson to memory") moves to
the `remember` skill line with the workspace as the link target. se keeps
instances (`instance_block`, `array_block`), provenance and the catalogue.
The Å-vs-macro table lands in the skill (slice 6). The file stays until
slice 6 ships because `hexfold-multicell-prism.md` links it; its banner
says so. Dropped: `meta.role='definition'`, `$name`, the three refusals,
`def.*` findings.

## Risks

- **Two sources of truth for files** (DB folder vs host git). The DB is
  truth; git is a run log. A host repo that diverges (manual edits on the
  host) is reset to the staged tree at the next run, and the echo says so.
  Ruled W3: the DB folder is truth. Under §Code storage (b) the host
  repo becomes a materialised cache of the rows and this risk goes away.
- **Export size.** Embedding the `precis-geom` wheel per design export
  is a few MB each; a design with many promoted modules grows. The
  export lists wheel shas and skips a wheel already present in the
  target's `find-links` dir on restore.
- **Latency misses 10 s** if the lane polls slowly or the venv is cold.
  Measured in slice 2; the fallback is a dedicated `ws` lane with a 1 s
  poll.
- **Import caps vs real structures.** A 7k-atom tunnel scene is normal;
  caps are per-block atoms and bytes, set from the largest stored block
  plus margin, not a guess.
- **Core wheel drift.** A script pinned to `precis-geom==N` regenerates
  differently under N+1. `regenerate` pins the recorded version; the
  goldens catch the drift in CI.
- **Agent modules as a dumping ground.** Promotion needs review; an
  unpromoted module is not importable elsewhere and not cacheable.

## Rulings (Reto, 2026-10-22)

- **W1 hosts:** pollux + spark; castor stays LLM-only; never the operator
  Mac, never melchior.
- **W2 private packages:** a `--find-links` wheel folder on the sandbox
  hosts, no index server.
- **W4 macro relax v1:** check-and-report + existing formfind; mate
  solver, settle and FEA later.
- **W5 construction ops:** legacy; retired in the slice that makes `run`
  usable, no clock, no wrapper phase (§MCP surface).
- **W6 agent-in-container:** stays outside until a case needs it.
- **W3 workspace file storage:** files in the DB as a folder `part-of`
  the design + a host git log ("I side with Fable"). Git objects in
  Postgres is a discussion item (§Code storage), not ruled.
- **Trace granularity:** per-atom `maker` column; every `/work/**.py`
  frame is a user frame.
- **Relax cache:** holds the periodic unit; consumers tile and cap it —
  no per-`(unit, periods, cap)` copies.
- **Stale policy:** golden-diff — regenerate at the new wheel and
  compare; a unit goes stale only when its generator's output changed,
  never on version equality alone.
- **Export:** designs embed their `precis-geom` and promoted-module
  wheels (`uv run` offline except PyPI).
- **Interface relax:** 6 Å free shell around assembly frames, widened
  by 2 Å (≤ 3×) while residual force on frozen atoms exceeds 0.1 eV/Å;
  `assembly.strain` reports what remains.
- Decided in the text: loop collapse past 64 same-site siblings; `op`
  frames for surviving ops.

## Open questions for Reto (one left)

1. **Git objects in Postgres** (discussion, §Code storage): (a) plain
   files only, (b) blob/tree/commit rows with se revisions FK-ing to
   commit shas (recommended as slice 1b), (c) files + bundle blobs.

Everything else is ruled (§Rulings).
