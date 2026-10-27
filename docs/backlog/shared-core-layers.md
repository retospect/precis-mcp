---
status: draft
title: Shared core layers — one Diag/probe/handle/check core under cad, se nano/hex, pcb and the layer-stack kinds
pillar: 3d-design
prio: normal
---

# Shared core layers for cad / se / pcb

Draft for Reto's review. Design call, not a build order. Parent gripe:
gr522030. Owning thread: `threads/shared-core-layers.md` (`design-review:
yes`). Reto ruled Q1–Q8 on 2026-10-27 (gr522030 comment 2).

## Motivation / why

SE CAD, SE nano/hex and PCB each need the same machinery: structured
failure records, a way to ask what a selector resolves to, stable handles,
checks that report instead of raising. Several finding shapes already
exist and none is shared:

| Shape | Fields / notes |
|---|---|
| `hexfold.report::Finding` + `Report.render/to_dict` | `code, severity (IntEnum INFO/WARN/ERROR), message, where, span, data, fix`; stdlib-only, already in the wheel |
| `precis_se.validate.ValidationIssue` | mirrors `precis_nm.validate.ValidationIssue` |
| `precis.structure.validate.ValidationIssue` | `rule, atoms, measured, expected, suggested_fix, severity: str` |
| `precis.cad.printability.PrintFinding` | hand adapter to `ValidationIssue` |
| `precis_geom.pcb_shape.FitFinding` / `FitResult` | carries checked/unchecked counts |
| `precis.pcb.drc.DrcFinding` | `rule, severity: str, where, detail, objects, margin_mm` |
| `precis_se.drc.DrcReport` | se-side DRC report |
| `precis_se.chain.findings.HandlerFindings` | chain handler findings |
| `precis_se.atomic.join` | consumes `hexfold.report` as `HxFinding` / `HxSeverity` |

Three severity spellings: IntEnum (hexfold), `"error"|"warn"` strings
(ValidationIssue, DrcFinding). What is missing is not a finding type but a
run-level record: CAD script run and import failures reach the agent as
prose only (gr522025), PCB edit/route/place/DRC ops fail as prose
(gr522027), hex/structure workspace runs likewise (gr522031), and no
domain can be asked what a selector resolves to (gr522026, gr522028,
gr522032). Layer-stack kinds are coming (EWOD, flex, cartridge/microfluidic
laminates: `pcb-ewod-multitile.md`, `cross-scale-single-assembly.md`) and
would each re-grow the stackup model inside pcb. Built separately these
drift.

Reference design: Onshape FeatureScript (per-feature regen status,
`debug(context, query)`, typed preconditions, `qCreatedBy`);
perplexity-research:522024, :522029 (522029's FsDoc details unverified).

## Principle

Package layers plus small protocols. No class hierarchy for entities: a
base class would freeze one domain's entity model (atom, pad, part) onto
the others. Two kinds of type, kept apart:

- `Diag` / `RunResult` are concrete frozen dataclasses, shared value types.
  Every domain constructs them directly.
- Protocols exist only for adapters: `resolve(selector)` and `digest(result)`
  per domain. A domain adopts the core by passing the conformance suite.

## Layers

Imports go downward only: core < families < domains.

### core (new leaf package, name open — Q1)

Stdlib-only (as `precis_geom` is pinned); may import `precis.utils.units`
only, mirroring the `precis_geom` allow-list. Ships in the precis-geom
wheel (workspace scripts run client-side), so nothing server-side.

| Piece | Shape |
|---|---|
| `Diag` | `status: ok\|info\|warn\|error\|not_checked`, `anchor{frame_key\|op_id, file, line}`, `refs[]`, `message`, `next`, `data` (scalar map: measured, expected, margin_mm…), `schema_version` |
| `RunResult` | list of Diags plus a run id pointing at the run's `trace.json`; run identity (`script_sha`, `lock_sha`, params hash) stays in `precis_geom.trace_schema`, which sits above core |
| renderer | one text renderer for Diag lists (MCP error path); see S1a on `view='trace'` |
| probe | `probe(selector, label) -> {count, digest, sample}` through a domain resolver |
| handle grammar | frame key + local id, extending `kind:id~selector`; opaque to core |
| param schema | type, unit, min/max, description; units via `precis.utils.units::parse_quantity` |
| checks harness | declared post-build checks -> Diags; DRC becomes a rule pack |

`status` mapping (S1 fixes it, one table in the core docstring):

| core | hexfold `Severity` | `ValidationIssue` / `DrcFinding` |
|---|---|---|
| info | INFO | — |
| warn | WARN | `"warn"` |
| error | ERROR | `"error"` |
| not_checked | — | `FitResult` unchecked count |
| ok | — | no finding |

Relation to `hexfold.report.Finding` (the closest existing core): S1
decides between lifting `Finding` into core or defining `Diag` as its
superset with a stated, tested conversion. Recommendation: superset plus
conversion, since `Finding.span`/`code` don't carry `anchor`/`refs`.

Not in core (later, or no layer): edit envelope (dry-run exists in
`precis_se.workspace_import::_DryRunDone` and in pcb sessions; unify only
once two implementations are compared), viewer highlight protocol,
per-frame cost (gr521584, trace-b), run-to-run diff (seed:
`precis_se.traceview` `_DIFF_FIELDS`), selector algebra beyond S3.

### se-common (SE CAD + SE nano/hex)

3D frames and transforms; trace and frame keys (`precis_geom.trace`,
`precis_geom.trace_schema::frame_key`, reads in `precis_se.traceview`);
3D selectors (faces, atoms, cells, regions); the solid assembly model
(`precis_geom.assembly`). Largely existing code; this layer mostly names it.

### laminar-common (PCB + EWOD + flex + cartridge stacks)

Stackup model (ordered layers, material, thickness); per-layer 2D geometry
plus vias; layer-aware selectors; per-layer fab outputs (gerber, coverlay,
stiffener); layer-aware DRC primitives. Today the stackup lives in pcb
(`pcb_capabilities.json`, `ir.py::layer_is_routable`).

### domains

Physics, rule content, routers/placers/relax, file formats. Core and
families know no pad, atom or net.

### Cross-family relations

The families are independent of each other. The PCB->SE binding is not
"core handles only": `precis_se.persist` imports `precis.pcb.export` and
`pcb-se-binding.md` derives BoardDerived from geometry. The rule is
one-way: `precis.pcb` never imports `precis_se`.

## Enforcement

- `pyproject.toml` `root_packages` today lists `precis`, `precis_web`,
  `asa_bot`, `asa_slack`, `precis_chain`, `precis_flow`, `precis_geom`.
  `precis_se` (AST-belted only) and `hexfold` are not roots, so no
  contract sees them. Add `precis_se`, `hexfold`, core and both families as
  roots. The families are separate top-level packages: a `layers` contract
  cannot place a package above its own sub-package.
- Contracts: `layers` (core < families < domains); `independence` between
  se-common and laminar-common; a core `forbidden` contract mirroring
  "precis_geom imports only the precis-geom wheel's carved precis subset"
  (allow `precis.utils.units`) and the server-side fence "precis_geom never
  reaches server-side precis (store/fetch/web) even via carved modules";
  one-way `forbidden` `precis.pcb` -> `precis_se`.
- Core gets a `packaging/geom/pyproject.toml`
  `[tool.hatch.build.targets.wheel.force-include]` entry.
- One parametrised conformance suite per layer; every registered adapter
  passes: failure -> valid Diag with an anchor; probe -> count + digest;
  renderer golden.
- Ratchet test counting prose raises in domain op paths; may only go down.
- `schema_version` on Diag. The laptop wheel lags the server, so readers
  accept every older version (the `trace_schema.SCHEMA_VERSIONS` pattern)
  and refuse only newer ones, with a next step.
- One skill, `precis-diag-help`; domain skills link to it.

## Slices

Ordered; each ships alone. The Codex lane trace-b (gr521583, gr521584,
gr521585, gr521580) bumps the trace schema; S1a builds on the post-bump
format (today's schema 2 key is `sha8(parent.key, qual, occ)`), so land
after it or rebase onto it.

### S1a — core types, renderer, conformance, CAD run failure (gr522030, gr522025)

`Diag`/`RunResult`/status mapping, renderer, conformance suite, wheel
packaging and linter contracts, plus the CAD workspace run-failure adapter.
`view='trace'` renders frames, not Diags: S1a does not change it. The
"one renderer serves view='trace'" claim is dropped; if wanted later, the
trace view gains a Diag section (decision for Reto, Q7).

Acceptance:
- A CAD script failure returns a `RunResult` whose Diag has
  `anchor.frame_key` and file/line.
- Core imports pass the new forbidden contracts; core is in the wheel.
- Renderer golden; conformance suite passes with the CAD adapter registered.
- Diag/Finding decision recorded in the core docstring, with a tested
  conversion if `Diag` is a superset.

### S1b — pcb adapter + DrcFinding round trip (gr522027)

Board scripts are traced (`precis_geom.pcb` uses `@traced`, dumps
trace.json), so the anchor rule is: `frame_key` whenever a tracer was live;
`op_id` only for scriptless server ops (edit, route, place, drc). The pcb
adapter covers both.

Acceptance:
- `diag_to_drc(drc_to_diag(f)) == f` for every finding on the DRC test
  boards (`data` carries `objects`, `margin_mm`).
- A failed pcb edit op returns a Diag with `anchor.op_id`; a failed
  board-script run returns one with `anchor.frame_key`.

### S1c — probes (gr522026, gr522028)

One slice, two resolvers: workspace selectors (`frame:` in
`precis-se-regions-help`, `precis_se.traceview`) and pcb selectors
(`precis.pcb.selectables::build`).

Acceptance: `probe` on a CAD selector and a PCB selector each return
count + digest + sample; an unresolvable selector yields a Diag with
`next`.

### S2 — hex run failure + probe (gr522031, gr522032)

The hex->se finding adapter exists (`precis_se.atomic.join` consumes
`hexfold.report` findings). Missing: the hex/structure run failure Diag and
a selector probe. The adapter lives in `hexfold` itself (stdlib, in the
wheel). Freeze the Diag shape (`schema_version` pinned) only after three
domains fit; field changes before then are free.

Acceptance: hex run failure and probe pass the conformance suite; the
frozen field set is recorded in the core docstring.

### S3 — stable handles + selector core

Handle = frame key + local id; selector algebra (union, intersect, filter,
`made_by(frame)`, FeatureScript `qCreatedBy`). Overlaps the `frame:<key>`
selector (`precis-se-regions-help`): `made_by` generalises it, no second
spelling. Depends on the open Frame->Part link in
`trace-frame-structural-keys.md` (option A, `parts` in
`trace_schema.ENTITY_TYPES`; its slice 4, unshipped).

Acceptance: `made_by(frame)` resolves identically to `frame:<key>` on se;
the same expression works on a pcb adapter; handles round-trip through
`kind:id~selector`.

### S4 — checks harness, DRC as the proof

Declared checks produce Diags; port `precis.pcb.drc` as a rule pack. A
check that cannot fire reports `not_checked`, never clean.

Acceptance: harness output equals the pre-port `DrcFinding` set on the DRC
test boards; one se check (`precis_se.assembly_check`) runs on the same
harness.

### S5 — laminar-common extraction

Decision (Q4): pcb first; extract when the second laminar kind lands, not
big-bang. EWOD slice 3 (`pcb-ewod-multitile.md` §Slice 3, stackup
authoring) does not wait.

Acceptance: pcb reads its stackup through laminar-common; independence
contract passes; gerber golden unchanged.

### S6 — typed params, units adoption, run diff

Param schema via `parse_quantity`; run diff keyed on handles.

Acceptance: an out-of-range param yields a Diag; a rerun diff reports
changed handles and params, not line shifts.

## Explicitly NOT in scope

- A shared entity base class or common atom/pad model.
- Moving physics, routers, placers, relax or file formats into core or families.
- Wholesale rewrite of domain error paths; adapters wrap, the ratchet
  drives conversion.
- Error->fix memory (Q5).
- The trace-b schema bump (gr521583/4/5/0).

## Target + blast radius

New: core package (in the precis-geom wheel, `packaging/geom/pyproject.toml`
force-include), two family packages, `precis-diag-help`, `pyproject.toml`
roots and contracts, conformance + ratchet tests. Touched by adapters:
`precis_geom.trace`, `precis_geom.pcb`, `precis_se.traceview`,
`precis.pcb.drc`, `precis.pcb.board_script` (`BoardScriptError`),
`precis.pcb.local`, `precis.pcb.handles`, `hexfold.report`, and the cad,
pcb and se handlers' error paths. Docs: package docstrings,
`docs/codebase.md`, domain skills (link only).

## Open questions / decisions log

1. **Names.** Core `precis_diag`? Families as separate top-level packages
   (required by the `layers` contract). *Reviewer recommends:* `precis_diag`;
   keep core narrow; families as top-level packages.
2. **Where core lives.** *Reviewer recommends:* in the wheel next to
   `precis_geom`, stdlib-only, with its own root, contract and force-include.
3. **Scriptless PCB op anchor.** `op_id` in the same `anchor` field. *Reviewer
   recommends:* `frame_key` when traced, else a content-derived `op_id` (op
   kind + ordinal + args sha8), since row ids don't exist on the laptop.
4. **EWOD slice 3 ordering.** Decided: pcb first, extract at the second
   laminar kind (S5).
5. **Error->fix memory** (automatic finding vs agent-written). *Reviewer
   recommends:* separate item; needs Diag `data` and stable keys first.
6. **Core vs per-design checks.** *Reviewer recommends:* core ships only
   domain-free checks (units present, param in range, handle resolves,
   `not_checked` discipline); geometry checks stay per-design.
7. **`view='trace'` and Diags.** Add a Diag section to the trace view, or
   keep the trace view frame-only (S1a as written)?
