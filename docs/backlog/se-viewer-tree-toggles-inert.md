# se 3D viewer: every tree visibility toggle is inert — node `id` and `name` produce two disjoint path schemes

BUG, root-caused by measurement 2026-09-29. Filed here rather than as a gripe
because the `precis` MCP was disconnected for the whole session; promote it to a
gripe when convenient.

**Symptom.** In the `se` 3D block-tree viewer (`/se/<slug>`), clicking the
shape/eye icon on any tree row does not change the rendered geometry. The icon's
own state flips correctly and the vendored treeview records the change, but no
mesh is ever touched. Reported by Reto against `unicycle-c1` as "messing with
the taxonomy tree does not make the box hide", which was read at the time as
specific to the container envelope. It is not — it is every row.

**Root cause.** Each scene node emitted by `build_shapes_node` / `_shape_leaf`
in `src/precis_web/blocktree_3d.py` carries both an `"id"` (uid-derived path)
and a `"name"` (the block name). The vendored three-cad-viewer builds its TREE
by joining each part's `name` with `/`, but builds its geometry registry
(`nestedGroup.groups`) keyed by `id`. For `unicycle-c1` the two schemes are

    tree / viewer.getStates() key   /Structural envelopes/unicycle/wheel
    nestedGroup.groups key          /se-unicycle-c1/66/79

They never meet, so state set through the tree lands in the tree model and is
never applied to a mesh.

**Scope — wider than the icons.** Every visibility affordance routed through the
vendored tree state is dead, *including the public `setState` API*. This is not
a private-versus-public API problem; the mismatch is upstream of both.

## Evidence

Canvas pixel-diff over the clip rect, threshold >8/channel, no orbit drag
between shots. Noise floor verified at `bbox None, n=0` across 10 control pairs,
so a null is a real null rather than capture jitter.

- Shape icon on `wheel`, `saddle`, `seatpost`, `crank_bolt_left`: canvas
  `bbox None, n=0` every time, while the icon itself changes
  `bbox (7,7,17,17), n=88` and its class flips `tcv_button_shape` →
  `tcv_button_shape_no`. `wheel` is the largest object on screen, so the null is
  not a "too small to see" artefact.
- Whole-scene walk of `viewer._rendered.nestedGroup.groups[*].children` (Mesh /
  LineSegments2 / Points; `.visible`, `.material.visible`, `.opacity`,
  `.transparent`) gives delta `{}` after each click.
- The tree model *does* record it: `viewer.getStates()` delta is
  `{"/Structural envelopes/unicycle/wheel": [[1,1], [0,0]]}`.
- CONTROL A — the render path is healthy:
  `groups['/se-unicycle-c1/66/79'].setShapeVisible(false)` + `update(true)` →
  `bbox (124,108,186,152), n=712`, reverting cleanly to `n=0`.
- CONTROL B — the public API is inert too:
  `viewer.setState('/Structural envelopes/unicycle/wheel', [0,0])` +
  `update(true)` → no error, `bbox None, n=0`, mesh delta `{}`.

## Two consequences

1. **Fixing this exposes a latent bug; they are one change, not two.** The
   container-envelope "hidden" mode (`applyContainerMode` in
   `src/precis_web/static/blocktree-3d.js`) survives subsequent tree clicks
   today — but only because the mechanism that would clobber it is itself
   broken. Private `setShapeVisible(false)` does not survive a public
   `setState()` that reaches `setObject`; at present no tree click ever gets
   there. Repairing the toggles without reworking hidden mode will regress it.
2. **The mismatch was known, its consequence was not.** It is already described
   in the comment block above the `[data-path$="/connections"]` rule in
   `src/precis_web/static/blocktree-3d-overrides.css`, and near the
   `cageEnvelope` comment in `blocktree-3d.js` — but only as a CSS-selector
   quirk. Nothing recorded that it also severs visibility.

## Intended direction

Bridge the two schemes with an explicit name-path ↔ uid-path map emitted
server-side in the `scene3d.json` payload, rather than making `id` name-derived.
Name-derived ids would satisfy the vendor's assumption and fix this at a stroke,
but node identity would become the block *name*, and renaming a block would then
break the revision scrubber, which deliberately keys on stable uids
(`changed_uids`). This is a preference, not a settled decision.

This bridge is a prerequisite for most of
`docs/backlog/se-3d-viewer-ux-batch.md`, where it is currently tracked only
under the hover item — it belongs first, not last.

Owner `src/precis_web/blocktree_3d.py`, `src/precis_web/static/blocktree-3d.js`.
