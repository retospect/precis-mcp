---
status: ready
title: Scale factor on the se print 3MF writer and route
thread: se-machine-design
model: sonnet
pillar: 3d-design
prio: normal
---

# Scale factor on the se print 3MF writer and route

## Motivation / why

Every 3MF writer converts metres to millimetres 1:1, so a nanometre or micrometre se block exports as sub-micron geometry. Reto wants scaled print files: atomistic blocks scaled (e.g. 1e7), 1 µm–1 mm blocks fit to ~100 mm, above 1 mm 1:1. se-3d-viewer owns the dialog that suggests the factor; this item owns the writer, file name, metadata, route parameters and the print check at printed size.

## In scope

1. `scale: float = 1.0` on `write_mesh` and `_write_3mf`. Geometry is multiplied by `scale` after the m→mm conversion. 3MF metadata gains `precis:scale_factor` (the factor as written, e.g. `2e7`).
2. The print check (floating islands, slicer cantilever, mesh cleanup, package check) runs on the mesh at its printed (scaled) size, so it judges what the printer makes.
3. File name: `<design>-<block>-x<factor>.3mf` when scale ≠ 1 (factor formatted like `2e7`, `100`); unchanged at 1.
4. Route: `GET /se/{slug}/print/{block}.3mf?scale=<f>&model=<vdw|ballstick>`. `model=` is accepted and passed through only for atom blocks; until printable-atomic-models lands, an atom block answers 409 "not printable yet". A scale below the printable floor answers 422 with JSON `{"min_scale": <f>}` (Reto C1: refuse and name the smallest factor that prints).
   - The floor is the atom models' strut floor, which printable-atomic-models defines. A solid block has no floor: `min_scale` is `null`, and a too-small solid is reported by the print check at printed size, not refused.
5. `GET /se/{slug}/print/{block}.json` returns `{real_size_mm: [x,y,z], suggested_scale, min_scale, models: [...]}` for the dialog. Suggested factor: fit the longest side to 100 mm, rounded DOWN to a 1-2-5 step (…1e7, 2e7, 5e7…), never below `min_scale`; solid blocks above 1 mm suggest 1, or a reduction only when larger than the print bed. The 1 µm / 1 mm thresholds only pick the suggestion, never the formats offered.
6. Handler: `view='print'` accepts `args={'scale': f}` with the same semantics.

## Explicitly NOT in scope

- Atom geometry (consumed by `docs/backlog/printable-atomic-models.md`, not written here).
- Per-element colour (later, Reto D1).
- Dialog UI (se-3d-viewer).

## Acceptance criteria

- A test writes a 1 mm cube at scale 100 and reads back a 100 mm cube with `precis:scale_factor` = 100 in the package.
- The route returns the `-x100` filename.
- `?scale=` below `min_scale` returns 422 with `min_scale`. Until atom models exist, test it with a stubbed floor.
- The `.json` endpoint returns a 1-2-5 suggested factor for a 5 nm block that fits ≤100 mm (expect 2e7 for 5 nm).
- The print check reports at printed size (a feature below the slicer threshold at 1:1 passes at a scale that makes it printable).

## Target + blast radius

- Existing writer: `precis/cad/export.py::_write_3mf`.
- Route module: `src/precis_web/routes/se_print.py` (lands with the print-check branch).
- Print check integration: `precis_se/printing.py::export_block_mesh`, `write_mesh(built=, source_pitch=, title=)` (lands with the print-check branch).
- Depends on: the print-check + download-button branch, which provides the above interfaces.

## Verified prerequisites (se-print contract review, 2026-10-04)

Reviewed HEAD `11f05f30a358d3882cad91ec9344b6e78b467071`;
implementation history `30a4a2bb9` supplies the checked-download seam.
This review adds no implementation or deployment claim.

| Surface | Present implementation | Remaining work |
|---|---|---|
| Discovery | `routes/se_print.py::print_files` filters single FDM blocks; `blocktree_view.py` exposes live-revision links only | A separate cheap capability descriptor must discover non-FDM/atomic candidates, including unavailable reasons; changing suggestion thresholds cannot change eligibility |
| Download | `se_print_download` supports `.3mf`/`.stl`; query parameters are unread; `.json` returns 404 | JSON descriptor, validated scale/model, shared handler/export semantics |
| Models | `printsolid.py::printed_solid` rejects `bound_kind='structure'`; `printing.py::report_for` rejects non-FDM | `vdw`/`ballstick` are prospective until printable-atomic-models supplies actual geometry and selected-model floors |
| Mesh | `report_for` → `build_print_mesh` → `export_block_mesh` shares the judged mesh with `write_mesh` | Scaling only `_write_3mf` would invalidate the report; prepare/check the scaled mesh once and ship it unchanged |
| Package | `_write_3mf` declares millimetres and Title/Application/CreationDate; `package_findings` is called by tests only | Add scale metadata; explicitly decide runtime package-validation policy |
| Errors/name | 404/409 are `text/plain`; attachment is `<design>-<block>-print.3mf` | Resolve the scaled suffix placement while retaining the scale-1 name; define new parameter errors and 409/422 ordering |

Read-only inspection of all 37 registered worktrees found no dirty changes
or unique branch commits for the route, printing module, writer, or either
owning print spec. Divergent legacy snapshots predate these changes; preserve
them. Backend owner: se-machine-design/se-print; dialog owner: se-3d-viewer.

## Consumer contract decisions awaiting coordinator review

User-approved A1/B1/C1/D1 rules above and printable-atomic-models' decisions
remain authoritative. The following wire details are proposals, not shipped
behavior or new physical decisions.

- **Available models:** `models` lists only implemented/exportable model IDs;
  an unavailable atomic block must not advertise prospective geometry as
  downloadable. Discovery must expose its disabled reason even when the
  current FDM-only `print_files` list omits it. Exact discovery shape is open.
- **Selection:** propose `.json?model=vdw|ballstick`, returning the four
  existing proposed fields plus `selected_model: string|null`. Solid: `models=[]`,
  `selected_model=null`, `min_scale=null`. Atomic: floor and suggestion belong
  to the selected model; the backend chooses and echoes the default. Model
  changes refetch the descriptor. No browser floor/radius/strut formulas.
- **Floor:** printable-atomic-models supplies the authoritative floor per
  selected model; its approved `1e7`/about `5e7` examples are not a new generic
  floor implementation. Until that adapter ships, atomic downloads return
  409 "not printable yet"; a stubbed available model can exercise 422.
- **Suggestion:** round the 100-mm fit down to 1-2-5, then clamp against the
  selected model's floor. Open: exact floor clamp (possibly non-1-2-5 and
  over 100 mm) versus next preferred step; floor must win. Open: print-bed
  source/orientation, threshold boundaries, and behavior for missing/zero
  bounds. Never quietly lower the floor to fit a bed.
- **Dimensions:** raw scale is dimensionless; se source lengths remain metres,
  atomic structure source remains Å, `real_size_mm` is unscaled millimetres,
  output is millimetres. Open: whether the descriptor's bounds are the realized
  printable representation in build frame (recommended) or the envelope;
  selected atom representations may have different extents.
- **Validation/order:** propose 404 for missing targets, then 422 for malformed,
  nonfinite or nonpositive scale / unknown model / model on a solid, then
  409 for known unavailable geometry/backend/target, then 422 for a valid
  available model below its floor, then checked export. Known unavailable
  atoms must return 409 even at a positive scale below a prospective floor.
  Invalid-input 422 must not masquerade as a floor error.
- **Error payload:** preserve existing `text/plain` 409 reasons (instance,
  non-FDM, group root, unrealized/net-empty, backend/export failure), add the
  atomic unavailable reason, and keep floor refusal as
  `application/json {"min_scale": number}`. Proposed other 422 payload:
  `{"error":"invalid_scale"|"unsupported_model","reason":string}`.
  Open: coordinator acceptance of these payloads and precedence.
- **Names:** scale 1 preserves the current HTTP `-print.3mf` name. Open:
  approved scaled pattern `<design>-<block>-x100.3mf` versus placing `-x100`
  after existing `-print`; one canonical factor formatter must serve filename
  and `precis:scale_factor`. Handler paths currently omit `-print`.
- **Format boundary:** `.stl` exists; scope explicitly whether scale applies
  there too. Default shared CAD writer behavior stays scale 1; print groups,
  manufacture roots and PCB exports are outside this slice.

## Smallest implementation slice after contract review

1. Shared se-only descriptor/validation helper: solid `null` floor,
   selected-model adapter boundary, backend suggestion, unavailable reasons.
   Atomic geometry stays in printable-atomic-models.
2. Thread scale through `report_for`/mesh preparation, `export_block_mesh`,
   `write_mesh`, and handler single-block export; optional `_write_3mf` scale
   metadata preserves default callers. Scale before cleanup/lift/slicing;
   printer layer/line-width rules remain physical, source field pitch follows
   geometry. Reconcile primitive/process findings and `mode_scale_mismatch`
   with printed size; no source geometry mutation or second writer multiply.
3. Add descriptor and validated download parameters in the backend route;
   preserve download finding headers and checked web/handler mesh identity.
   SE-viewer consumes this contract without editing the route.

## Additional acceptance for that slice (not executed in this review)

- Parse ZIP/XML for the 1-mm cube at ×100: all extents 100 mm, unit
  `millimeter`, one scale-factor metadata entry, valid core package, existing
  metadata retained, no slicer config; ×1 geometry/name compatibility.
- Web/handler share scaled vertices and finding counts; fixture proves a
  physical feature finding changes at printed size, and the shipped mesh is
  exactly the scaled/lifted/cleaned mesh judged. Keep physical layer height
  fixed; exercise source-field pitch and the `built=` reuse path.
- Solid floor is JSON `null`; positive tiny scale proceeds to checks.
  Stubbed implemented models have distinct floors: descriptor selection and
  download refusal agree. Unknown model differs from known unavailable atoms.
- Assert status, content type and body for missing target, unavailable atom,
  below-floor model, malformed/NaN/infinite/zero/negative scale, unsupported
  model and model-on-solid; prove refusal order without mesh construction.
- Suggestion: 5 nm → `2e7` absent a higher selected floor; floor-winning and
  missing-bound cases follow the reviewed decisions. Discover unavailable
  atomic/non-FDM candidates without constructing a print mesh.
