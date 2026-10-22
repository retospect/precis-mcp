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
   `run` = `sandbox_run mode:script` with `workspace:<slug>` on castor or
   pollux — never a Mac, never melchior (`sandbox_run.py::_sandbox_hosts`).
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
P = params()
p = prism(seams="cccRcccRcccR", rows=P["R"], periods=P["P"], junction="sp3-z")
out.block("p", p, ports=p.rims(), relax={"method": "gfn2", "periodic": True})
guest = blocks.get("guests#c60")          # read MCP, existing block
out.instance("guest", template="guests#c60", parent="p", pose="[0 0 2] nm")
```

Loop latency target: < ~10 s for small structures. Budget: claim tick ≤ 2 s
(the `job_claude_docker` lane polls on its own interval), warm venv 0–1 s
(a pre-built venv per lock sha, §Packaging), build 1–5 s, import 1–2 s.
Staging a new lock is the existing async stage job (gr496426) and happens
once per dependency set.

## Import boundary (trusted side)

`src/precis_se/workspace_import.py` (new; pure over the harvested tree,
store access only through the existing persist path):

- **Manifest** `out/manifest.json`: `{schema: 1, blocks: [{name, file,
  scale: 'atomic'|'macro', ports?, relax?, desc?}], instances: [...],
  connects: [...], notes?}`. `additionalProperties: false`; a bad manifest
  is the run's failure with `next=` naming the key.
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
`cap`, `decorate` ops of the modules spec. Timeline: callable both ways
from slice 1; once `tool_ledger` shows 30 days without a `generate`/`join`
call outside tests, the op refuses with `next=` naming the workspace path
(the `seam.sp2_retired` pattern); stored blocks keep their coordinates and
`regenerate` keeps working through the core wheel (same functions).

Inspection stays and grows in MCP: `validate`, `drc`, `clearance`, `sites`,
`catalogue`, `mold`, `stability`, `freedom`, renders, `view='block'` now
rendering the workspace provenance with the one `run` call that reproduces
the block.

## Guardrails, checked

- DRY: one builder layer (`sheetnet`), one op table, one relax interface,
  one import path; definitions are functions, examples are tests.
- Tools, not black boxes: every block is a script sha + params anyone can
  re-run; the echo names the reproducing call.
- Retire broken things: `prism-k3-120-z` refusal stands; `generate`/`join`
  retire on the ledger evidence above.
- Secrets: the container has none; the read MCP token rides the child's
  env (`_sandbox_read_mcp.py::build_serve_env`); the agent sees a job id.
- One way: a structure is made by a script or by a surviving small op,
  never by both for the same block (`generated.generator` says which).
- Reproducibility: `regenerate` = re-run at `script_sha` with `params`
  against `lock_sha` and `image_digest`.

## Slices (smallest useful first)

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 0 | **Importer + goldens, no container.** `precis_se/workspace_import.py`, manifest schema, extXYZ reader, cad graph import, replace/retire semantics; `tests/workspaces/` with a fixture tree and golden JSON (counts, coordinate SHA as `tests/hexfold/data/goldens`, findings) | `atomic/generate.py::finish_generate` (bind path), `persist.save_tree`, `handlers/structure.py` readers | Importing the fixture into a dev design makes blocks with full `generated`; a second import with one block dropped retires it and `restore_ref` brings it back; a manifest with an unknown key refuses naming it; goldens pass on main | M, 1 build |
| 1 | **Workspace kind + `run`.** `folder`-backed workspace ref with `edit` on files; `sandbox_run` `params.workspace`; staged files committed to the host git; import on exit; `requested_by` from `caller()` when present; inline result within `wait_s` | `sandbox_run.PARAMS_SCHEMA`/`semantic_rejection`, `_sandbox_inputs.stage_code`, `_sandbox_harvest`, `claude_docker` terminate path | The loop above runs end to end on the dev lane; the block's `generated.script_sha` equals the host repo's HEAD; two runs on one workspace serialize (the agent-workspace-containers lease) | L, 2 builds; needs sandbox slice 4 (lane live) |
| 2 | **`precis-geom` wheel, index dir, warm venv.** `packaging/geom/`, import-graph test, `scripts/ws-promote` (build only), venv per lock sha | `_sandbox_script.stage_deps`, `build_script_argv`, release scripts | Container imports `hexfold.sheetnet` and `precis.cad` offline; a small prism run completes in < 10 s wall from `run` to echo on a warm lane (measured, written into this item) | M, 1–2 builds |
| 3 | **Proof: the 12-wall H-column prism both ways.** `cccRcccRcccR` at (4, 6) via `generate` and via a workspace script; record tokens (`tool_ledger` bytes per path), wall time, errors/retries, and SHA equality of the two blocks; then the retirement clock starts | multicell slice 3 (fold tokens) must have landed | Numbers in this item; both blocks byte-identical; the script path is cheaper or the gap is explained | S, after multicell slice 3 |
| 4 | **Relax interface, atomic + cache.** Manifest `relax:`, `relax_cache` migration, `derived-from` link, read-MCP `relaxed` lookup, importer dispatch to the existing job types | `hexfold_relax_job.py`, `handlers/structure.py::_dispatch_relax`, `_sandbox_read_mcp.kind_refusal` | A second run after the relax finished reads the relaxed geometry; the variant links to its seed; the cache key matches across runs with equal params | M, 1 build; GFN2 route is the known dependency |
| 5 | **Macro relax v1: check-and-report.** `method:'assembly'` = findings from the table above as one list; new redundancy analysis and RSS; formfind where axial | `drc`, `freedom`, `cad.relate`, `measures.stackup`, `mold`, `formfind` | A fixture with a redundant mate names the redundant set; an over-tight stack names the failing chain under worst-case and RSS; an interfering pair reports depth | M–L, 2 builds |
| 6 | **Agent modules + skill.** `module` tag, catalogue search, `ws-promote` publish, `precis-workspace-help` (loop, manifest, relax, packaging, the Å-vs-macro table from the modules spec), pointer lines in `precis-se-help`, `precis-se-atomic-help`, `precis-job-compute-help`; glossary: workspace, run, promote | `src/precis/data/skills/`, `docs/glossary.md`, `precis_se/library.py` | A second workspace depends on a promoted module by name and runs | M, 1 build |
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
- **Latency misses 10 s** if the lane polls slowly or the venv is cold.
  Measured in slice 2; the fallback is a dedicated `ws` lane with a 1 s
  poll.
- **Host lane conflict.** This item and the sandbox spec say castor +
  pollux (2026-10-18); `threads/local-compute.md` 2026-10-15 made castor
  vLLM-only and moved `job_claude_docker` to spark. Open question 1.
- **Import caps vs real structures.** A 7k-atom tunnel scene is normal;
  caps are per-block atoms and bytes, set from the largest stored block
  plus margin, not a guess.
- **Core wheel drift.** A script pinned to `precis-geom==N` regenerates
  differently under N+1. `regenerate` pins the recorded version; the
  goldens catch the drift in CI.
- **Agent modules as a dumping ground.** Promotion needs review; an
  unpromoted module is not importable elsewhere and not cacheable.

## Open questions for Reto

1. **Hosts.** castor + pollux (the brief) vs the 2026-10-15 lane ruling
   (castor = vLLM only; sandbox on spark). Recommendation: pollux + spark
   for `ws` runs; castor only if the vLLM exclusivity is lifted.
2. **Where the private index lives.** Recommendation: a `--find-links`
   directory on the sandbox hosts' shared dir, no server; revisit only if a
   non-sandbox consumer needs it.
3. **Workspace file storage.** DB folder ref + host git as a run log
   (recommended: editable from the session MCP, no cross-host write path)
   vs host-git-only with edits as jobs (simpler trust story, slower loop).
4. **Macro relax scope.** v1 check-and-report plus formfind (recommended),
   or build a mate solver now. A solver is a 2–3 build item with no
   current consumer; the findings are what fail today.
5. **Retirement clock for `generate`/`join`.** 30 days of zero ledger
   calls (recommended) vs retire at the proof slice's landing.
6. **Agent-in-container.** Stay out until a case needs file-level
   iteration faster than edit+run (recommended); the socket bridge is the
   only missing piece when it does.
