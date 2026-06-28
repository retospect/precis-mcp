# cad kind — v1 build plan

Status: `proposed` — operational decomposition of
[ADR 0041](../decisions/0041-cad-kind-analytic-ir.md). Owner: Reto + agent.
This doc is the build-order artefact and progress tracker; the *why*
lives in 0041, the *how/what-files* live here.

Sibling models to copy from:

- `draft` (ADR 0033/0032) — chunk-native, soft-deletable nodes; the
  `DraftMixin` store ops + `0031_draft_kind.sql` migration are the
  template for the node-as-chunk storage and the derived-recompute
  cascade.
- `calc` — the stateless-handler skeleton (lazy optional-dep import in
  `__init__`, clean `InitError`/drop on missing dep).
- `job` + `workers/job_types/*` — the export-worker shell-out shape.

---

## 0. Scope (this plan == ADR 0041 v1)

In: frustum (rect + n-gon/circular) + sphere + torus; chamfer;
`merge`/`subtract`/`intersect`, `move`, `pattern`, `instance`;
point/ray/arc/section probes; clearance + interference + translational
DOF; datums; persisted observers; eager `put` validation; STL export
(OpenSCAD); geometric volume. mm / float64 / rigid-only.

Out (phase 2, do not build): thread/gear, rotational DOF, fillets,
STEP/OCCT, library instancing, vectorized bulk eval, 2D contours, mass,
the three.js web viewer (web is a fast-follow, not v1-blocking).

---

## 1. Module layout

A self-contained analytic kernel package, kept **free of any precis/DB
import** so it is unit-testable in isolation and swappable (ADR 0041 §9
"pure evaluator swap"):

```
src/precis/cad/
  __init__.py
  vec.py          # float64 vec3 + rigid transform (rotate+translate, no scale)
                  #   - small, numpy-backed; Transform.compose/inverse/apply
  primitives.py   # Primitive ABC + Frustum, Sphere, Torus, Chamfer(half-space)
                  #   each implements the membership card (§4 ADR):
                  #   contains(p), ray_hits(o,d)->intervals, distance(p),
                  #   section(plane)->loops, faces()->[(normal, ...)], aabb()
  dsl.py          # config mini-DSL parse/format (box:w40d20h10, cyl:r3h12,
                  #   frustum:n6rb4rt2h5, chamfer:1x45) — grammar table here
  graph.py        # Node, NodeKind, Design(in-memory DAG over a flat node list);
                  #   topological fold for booleans; pattern expansion
  fold.py         # boolean fold: merge=any, subtract=first ∧ ¬rest,
                  #   intersect=all — with node attribution (blocking node)
  probe.py        # point/ray/arc/section probes over a Design; interval math
  relate.py       # clearance / interference / translational DOF (component pairs)
  bulk.py         # sampled volume + centroid (labelled sampled)
  export_scad.py  # Design -> OpenSCAD source (bake world transform, multmatrix)
  datums.py       # auto-exposed axis/plane/point refs per primitive

src/precis/handlers/cad.py     # the kind: put/get/edit/delete/tag/link + views
src/precis/handlers/_cad_*.py  # split helpers if cad.py grows (format/probe-render)
src/precis/store/_cad_ops.py   # CadMixin: node CRUD over chunks, eval cache
src/precis/workers/job_types/cad_export.py  # OpenSCAD shell-out export worker
src/precis/migrations/0041_cad_kind.sql      # see §3
src/precis/data/skills/precis-cad-help.md    # verb mechanics + DSL + probe model
tests/cad/...                                # kernel unit tests + handler tests
```

Rationale for the split: ADR 0041 §1's accepted cost is "a small
analytic geometry kernel of our own" — isolating it under `precis/cad/`
(no DB) keeps that cost bounded and the membership contract (§4)
enforceable in pure unit tests with analytic oracles.

---

## 2. Node / DAG encoding (the load-bearing decision)

A design is a `refs` row (`kind='cad'`, slug-addressed, `ca…` handle per
ADR 0036). Each **node is a chunk** (soft-deletable, ADR 0033 model). The
DAG lives in operand-id fields stored in `chunks.meta` JSONB — **not** in
`parent_chunk_id` physical nesting (ADR 0041 §3: "the list *is* a DAG").

Per-node `chunks.meta` shape (JSONB):

```json
{
  "role": "add|cut|intersect|result|move|pattern|instance|observer|datum",
  "shape": "frustum|sphere|torus|chamfer|null",      // null for operators
  "alias": "box|cyl|cone|hex|pyramid|null",           // frustum alias surface
  "dims": {"r": 8, "h": 10},                          // shape params (mm)
  "pose": {"at": [0,0,-1], "rot": [0,0,0]},           // location + angle (deg)
  "operands": ["ca3", "ca4"],                          // node handles / names
  "pattern": {"kind":"polar","n":6,"r":18,"axis":"z"}, // pattern-only
  "name": "hub_bore",                                  // optional, unique/design
  "component": true                                    // top-level part flag
}
```

- `chunks.text` per node = the LLM-authored one-line description (feeds
  search; see §5). Geometry is in `meta`, not in `text`.
- Operand references resolve in this order: node handle (`ca3`) → name
  (`hub_bore`) → chunk anchor. `name` is a friendly alias over the id.
- **Derived back-refs** (`fused-with` / `cut-by`, ADR 0041 §6) are
  *computed on eval*, never stored — they live only in the eval cache.

Eval cache: a derived artefact recomputed on any node change (ADR 0035
recompute boundary). v1 keeps it in-process (memoized per
`(ref_id, content_sha-of-all-nodes)`); a persisted derived row is a
later optimization, not needed at tens-of-primitives scale (ADR 0041 §9).

---

## 3. Migration `0041_cad_kind.sql` (additive, forward-only)

Mirror `0031_draft_kind.sql`. Reuse the draft chunk machinery wholesale
(`handle`, `pos`, `parent_chunk_id`, `content_sha`, `retired_at`,
`chunk_events`) — **no new chunk columns**. Only:

1. `INSERT INTO kinds ('cad', FALSE, 'CAD', '<desc>')` — named, like draft.
2. New `chunk_kinds`: `cad_node` (geometry + operator nodes,
   discriminated by `meta.role`), `cad_observer` (geometric + relational
   observers), `cad_datum` (named axis/plane/point refs). Three kinds so
   observers/datums filter at the SQL layer without reading `meta`.
3. `_KIND_ALLOWED_AXES['cad'] = frozenset()` in `store/types.py` (no
   closed tag axes; `note_like=False`).
4. `corpus_role='none'`.

No new tables in v1: nodes are chunks, the DAG is `meta`, observers are
chunks, the eval cache is in-process, export artifacts land on
`PRECIS_CORPUS_DIR` (ADR 0029) with paths recorded in `refs.meta`
(matching the draft/job export pattern).

---

## 4. The membership contract (kernel correctness spine)

Every `Primitive` implements, exact under rigid transform (inverse-
transform the probe into the primitive's local frame first):

| method | returns | used by |
|---|---|---|
| `contains(p)` | bool | point probe, fold |
| `ray_hits(o,d)` | sorted `[(t_in,t_out)]` | ray/arc probe, fold |
| `distance(p)` | signed float (− inside) | clearance/DOF |
| `section(plane)` | `[loop]` (2D polylines/arcs) | section probe |
| `faces()` | `[(normal, tag)]` | draft check |
| `aabb()` | `(lo,hi)` | optional AABB pre-filter |

Boolean fold (`fold.py`), walked in topological order over the node
list (ADR 0041 §6):

- `merge` = union of intervals/membership (`any`)
- `subtract` = `first ∧ ¬rest` — a carved interval is **void, attributed
  to the removing node** (the "removed by `bolt#1`" guarantee)
- `intersect` = `all`

This is what makes a drilled bore read as a bore without computing the
merged solid. Tested with hand-computed interval oracles.

Frustum is one kernel under aliases (box/cyl/cone/hex/pyramid/taper);
the **side-face normal tilt is the draft angle** — draft check is free.

---

## 5. Verb surface (handler `cad.py`)

`KindSpec(kind='cad', supports_get/put/edit/delete/tag/link=True,
supports_search_hits=True, is_numeric=False, id_required=False)`.
Views: `('tree','probe','section','clearance','dof')`.

- **put** — create design (no parent) or add a node
  (`parent=`, `name=`, `config=`, `location=`, `op=add|cut|intersect`,
  or `pattern=`). Returns the eager-validation row
  `{handle name role shape dims pose check}` with `check=ok | ⚠ interferes…`
  (interference is the eager check, ADR 0041 §11). TOON for row-lists,
  JSON for a single node (`precis-toon`).
- **get** — no id: list designs. `id='ca1'`: whole tree (root-expression
  row + flat node rows; a pattern is one row). `id='ca3'` / `flange#bore`
  / `flange/bolts/bolt#3`: single node JSON incl. derived back-refs.
  `view='probe'|'section'|'clearance'|'dof'` with `args={...}`.
- **edit** — change a node's `config`/`pose`/`op` (DELETE+INSERT the
  chunk per the append-only-body rule; re-eval cascade).
- **delete** — soft-retire a node (`retired_at`); re-eval.
- **tag/link** — standard; `link` for `derived-from` paper /
  `produced-by` todo (ADR 0041 §12 agent loop).

`card_combined` (search) = component names + the LLM one-liner + overall
bbox dims, so `search(kind='cad', q=…)` works on intent (ADR 0041 §12).

Output format helpers reuse `precis.format.toon`. Exact table shapes are
copied verbatim from ADR 0041 §11 (tree / ray / arc / point / section).

---

## 6. Export (`cad_export` worker)

STL-via-OpenSCAD first to prove the loop (ADR 0041 §10). Gated on
`shutil.which('openscad')` at **export time only** — the design/probe
loop has zero external dep (ADR 0041 §12). Shell-out shaped like
`export/compile.py`→`latexmk`: returns a `CompileResult`-shape with
`log_tail`, writes artifact to `PRECIS_CORPUS_DIR`, records the path in
`refs.meta`. `export_scad.py` bakes the accumulated world transform into
`multmatrix(...) <prim>(...)`. STEP/OCCT is phase 2.

---

## 7. Build order (each step = one reviewable edit + its tests)

1. **kernel-vec+primitives** — `vec.py`, `primitives.py` + unit tests
   (membership/ray/distance/section/faces with analytic oracles). No DB.
2. **dsl** — `dsl.py` parse/format round-trip tests; finalize grammar
   (Open-Q 2 of ADR).
3. **graph+fold** — `graph.py`, `fold.py`; boolean attribution tests
   (drilled-bore-reads-as-void oracle).
4. **probes** — `probe.py` (point/ray/arc/section) against the flange
   example from ADR §11; assert the exact tables.
5. **relate+bulk** — clearance/interference/translational DOF; sampled
   volume/centroid (labelled sampled).
6. **migration + store** — `0041_cad_kind.sql`, `_cad_ops.py` (node CRUD
   over chunks, eval-cache memo). `precis migrate --dry-run` on throwaway
   DB.
7. **handler** — `cad.py` wiring put/get/edit/delete/tag/link + views +
   TOON; register in `dispatch.boot`. Eager `put` validation.
8. **export worker** — `cad_export.py`; STL smoke (skipped when no
   openscad binary).
9. **skill + docs** — `precis-cad-help.md`; index rows in
   `precis-overview`/`precis-help`; README line; CHANGELOG; `uv version`.

Gate after each: `uv run ruff check . && uv run ruff format --check . &&
uv run mypy src tests && uv run pytest` (per AGENTS.md DoD).

Dependency note: numpy is already a *core* direct dep
(`pyproject.toml` `numpy>=1.24`) — the kernel uses it, no ADR needed
(Open-Q 3 resolved).

---

## 8. Open design calls (resolve before/within the relevant step)

1. **chunk_kind granularity** — RESOLVED: **three chunk_kinds**
   (`cad_node` / `cad_observer` / `cad_datum`), so observers/datums filter
   at the SQL layer without reading `meta`. `meta.role` still records the
   boolean op (add/cut/intersect/move/pattern/instance) within `cad_node`.
2. **config DSL grammar** — finalize per-shape letters in step 2 /
   the skill (ADR Open-Q 2). Draft: `box:wWdDhH`, `cyl:rRhH`,
   `cone:rRhH`, `frustum:nN rb<rB> rt<rT> hH`, `sphere:rR`,
   `torus:R<major>r<minor>`, `chamfer:<size>x<angle>`.
3. **numpy vs pure-python** — RESOLVED: **numpy**. Already a *core*
   direct dependency (`pyproject.toml` `numpy>=1.24`), so no ADR / new-dep
   friction. Kernel vec/transform math is numpy-backed.
4. **eval cache** — in-process memo (v1, this plan) vs a persisted
   derived row now. Lean in-process; revisit when parts get big.
5. **deep-CSG TOC legibility** (ADR Open-Q 1) — validate the flat-row +
   root-expression shape on a nested example in step 7; add collapse/
   drill only if it reads badly.

---

## 8b. Build progress (live)

- [x] **Step 1** — `vec.py`, `interval.py`, `primitives.py` (Sphere,
  CircularFrustum, PolyFrustum/box/prism/pyramid, HalfSpace, Torus, Placed).
  38 tests.
- [x] **Step 2** — `dsl.py` config mini-DSL. 36 tests.
- [x] **Step 3** — `fold.py` (CSG classify + ray attribution) + `graph.py`
  (`Design`, pattern, instance). 10 tests.
- [x] **Step 4** — `probe.py` point/ray/arc/section(z=const)/draft. 11 tests.
- [x] **Step 5** — `bulk.py` sampled volume/centroid (5 tests) +
  `relate.py` analytic clearance / interference / translational DOF
  (6 tests). Clearance uses the per-component CSG signed-distance field
  (exact sign; exact magnitude on the governing surface), minimised by
  grid-seed + gradient descent — so shaft↔bore reads the radial wall gap
  (0.1 mm) and a press fit reads negative. A subtle bug fixed here: the
  circular-frustum meridian reduction treated the ``rho=0`` axis as a
  surface (`signed_dist_frustum_meridian` now excludes it).
- [ ] **Steps 6–9** — migration, store, handler, export, skill.

106 kernel tests green; `ruff`/`mypy` clean on `src/precis/cad`.

**Step-5 fork (clearance/interference/DOF exactness).** ADR §8 calls these
"exact", resting on §7's "analytic directional clearance, min over
primitive pairs". But the flagship *shaft↔bore* case measures the gap to a
*subtracted* feature, and GJK-of-solids returns 0 for a shaft nested in a
bored hub (they overlap as solids) — so naïve primitive-pair GJK is subtly
wrong there. Exact CSG-aware clearance is real work. v1 options:
(a) sampled clearance/DOF over the folded membership (robust, honest,
labelled *sampled* — consistent with §8's sampled tier), upgrade to
analytic later; or (b) implement the analytic min-over-primitive-pairs with
feature-aware signing now (more code, the "exact" promise). Leaning (a) for
v1 with a clear label.

## 9. Definition of done (AGENTS.md)

- [ ] this plan reviewed
- [ ] `0041_cad_kind.sql` applies clean to a fresh DB; only-new-file dry-run
- [ ] ruff check / ruff format / mypy / pytest all green
- [ ] `cad` subcommand surface has skill help + handler tests
- [ ] README line + CHANGELOG entry + `uv version` bump
- [ ] ADR 0041 stays the decision record (this is design, not a new ADR)


## 8c. Build complete (2026-06-28)

All steps landed and verified against the live Postgres (container
`precis-pgtest`, port 55432) and the real `openscad` binary:

- Kernel (steps 1–5): `vec`/`interval`/`primitives`/`dsl`/`fold`/`graph`/
  `probe`/`bulk`/`relate` — pure, DB-agnostic.
- `scene.py` — the text design language ↔ `SceneSpec` ↔ `Design` bridge.
- Migration `0041_cad_kind.sql` — `kind='cad'` + the `cad_nodes` table.
- `_cad_ops` store mixin, `cad.py` handler (put/get/search/delete + views
  point/ray/arc/section/clearance/dof/volume/scad), dispatch wiring.
- `export.py` — OpenSCAD `.scad` generation + binary-gated STL (real STL
  verified).
- `precis-cad-help` skill + `precis-overview` row + README + version 8.20.0.

**ADR 0041 Amendment 1 (adopted by decree).** Nodes are NOT chunks — they
live in the dedicated `cad_nodes` table (the chunk indexers claim
kind-blind, so chunk storage would embed/keyword/salience-rotate every
frustum). The design keeps **one** `card_combined` chunk — an auto-built
summary from the author's node names — so intent-search works at the
design level with one vector per design. `_cad_ops`/`cad.py`/migration/
handle-codes reworked accordingly; the pure kernel was untouched.

Test count: 157 cad tests green; 227-test draft/store/dispatch/migrate
regression green; `ruff` + `mypy` clean.


## 8d. Direct export backends (2026-06-28, ADR 0041 Amendment 2)

Replaced the OpenSCAD-binary STL route with two in-tree backends, both
optional extras, both fed by the same `SceneSpec` fold as `build_design`:

- `precis/cad/tessellate.py` — analytic primitive → watertight,
  outward-oriented triangle mesh (numpy only; orientation enforced by a
  signed-volume check). Covers every shape in the DSL.
- **Printable** (`view='stl'|'3mf'`) — `manifold3d` CSG folds the boolean
  DAG; precis hand-writes binary STL + 3MF (zip/XML). Extra `[cad-export]`
  (in `[all]`). No external app, no trimesh.
- **Exact STEP** (`view='step'`) — `precis/cad/_occt.py`: OpenCASCADE B-rep (primitives→make-API, `Transform`→`gp_Trsf`, DAG→Fuse/Cut/Common,
  `STEPControl_Writer`). Extra `[cad-step]` = the bare OCCT wheel `cadquery-ocp-novtk` (raw OCP, no build123d); heavy, out of `[all]`.
- `.scad` text view retained (zero-dep). Handler `get` gains
  `stl/3mf/step` views; missing extra → `Unsupported` with install hint.

Verified live: flange → 2072-tri watertight STL (bbox-exact) + 3MF, and a
1616-entity `MANIFOLD_SOLID_BREP` STEP in mm. 183 cad tests green (2
skipped = backend-absent negatives, both backends installed here).

