---
status: ready
---

# Atomic overlay follows the viewer Clip tab — gr459593

## Resume

Owner: se-viewer17. Base: staged R17
`7265b9ac0502cd007affdae26f61256877b547d9`; branch
`work/se-viewer/atomic-overlay-clipping`. Narrow implementation authorized by
Reto/orchestrator. Independent review and integration remain pending.

Implementation candidate covers the four mesh kinds. Canvas acceptance is
blocked before navigation: cached Chromium is absent; cached WebKit aborts in
the sandbox and times out after normal escalation with a ClientCallsAuxiliary
XPC listener error. No design payload, camera/plane or screenshot was obtained.
Capability receipt: task scratch `atomic-clipping/visual-capability.json`.
No green-suite substitute; source review may proceed, integration acceptance
still needs the existing canvas harness on a supported browser host.

## Verified premise and contract

`blocktree-3d.js::_setupAtomicOverlay` adds atom/bond/smoothed meshes directly
to the scene. The lazy target overlay is separate too. The vendored
`NestedGroup.setClipPlanes` only visits its registered CAD meshes. Existing
overlay materials therefore never receive the viewer planes.

Before an overlay mesh renders, synchronize its material to the existing
viewer clipping plane array and intersection setting. Use a mesh render hook
instead of replacing the vendored controls or polling: it covers plane-array
replacement and meshes created after asynchronous payload/build yields. Plane
movement is observed through the shared live Plane objects. Shader invalidation
is necessary when plane count or intersection mode changes. The existing
renderer local-clipping flag controls enable/disable; no independent switch.

Apply to atoms, bonds, smoothed surfaces and lazy target surfaces. Use existing
per-fragment clipping, without new atom-center rejection or cap geometry.
Clearing/disabling clipping must restore the same unclipped picture. No new
geometry, schema, physics, route or picking behavior is assigned.

## Canvas acceptance

Use the existing `scripts/viewer_check.py` screenshot/diff/settle helpers:
channel threshold 8, toolbar crops 40/60 rows, measured noise <=50 pixels,
changed >=1000 pixels and restored <=200 pixels. Fix camera, viewport and
render controls. A through-tube plane must fail on base and pass on candidate;
test axial and rotated planes, disable/restore, and overlay arriving while
clipping is already active. Exercise smooth/lazy target materials where the
fixture actually supplies them. Record source SHA, real design handle and
payload hashes, URL, camera/plane settings, shots and pixel counts.

The documented `scripts/guide-web --db prod` read-only role may supply the real
`hexfold-dogfood-r5`/`cyl12open` payload, without production browser credentials.
Reto's reported positive appearance is human dogfood, not this acceptance.
No local refused test preflight retry, browser-auth workaround or production
mutation. If a supported visual engine is unavailable, explicitly retain
canvas acceptance as blocked; source assertions or a green suite cannot replace it.

## Holds

DRY quiet permits this static JS repair but freezes package docstrings and
version metadata. Keep rationale in the owning JS and this contract; root owns
version/release metadata and full gate. No main landing, rebase or deployment.
