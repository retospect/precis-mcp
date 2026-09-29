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

## Intended direction — SUPERSEDED, see the RESUME POINTER below

This section recorded the original preference: bridge the two schemes with a
name-path ↔ uid-path map emitted server-side, on the reasoning that
name-derived ids would make node identity the block *name* and so break the
revision scrubber's stable-uid keying (`changed_uids`).

**That reasoning was wrong and the decision was reversed.** The uid does not
have to be encoded *in* the path — it can ride as a field on the node, which
keeps identity stable under renames while letting the path mirror the name
chain. A map would also have been the least DRY option available, restating
information every node already carries. The implemented fix makes `id` equal
the name chain; see the RESUME POINTER.

This fix is a prerequisite for most of
`docs/backlog/se-3d-viewer-ux-batch.md`, where it is currently tracked only
under the hover item — it belongs first, not last.

Owner `src/precis_web/blocktree_3d.py`, `src/precis_web/static/blocktree-3d.js`.

## RESUME POINTER (2026-09-29, session wind-down)

**State: fix LANDED on `main` (ungated qland, 2026-09-29). Verified at the
pytest/mypy level only — full suite green apart from three unrelated
pre-existing failures, clean typecheck over 2259 files, secret gate clean.
NEVER verified against a running viewer.**

**The live canvas verification in step 2 below is still OWED.** A green suite
is not sufficient evidence for this change: the old tests passed green, with a
clean console, for the entire period during which every visibility toggle in
this viewer was dead (see "Why the suite stayed green"). Until someone runs the
pixel-diff checks, treat the repair as plausible, not confirmed.

The measurement phase is COMPLETE; do not redo it. Settled facts:

- `viewer.setState(<uid path>, …)` is a silent no-op. `setState` → `treeview.
  setState` → `findNodeByPath` walks `children[segment]` **by name**, so a uid
  path dies at segment 1 and returns null.
- The tree→scene bridge is `Viewer.setObject(path)`, whose first line is the
  **id-keyed** `nestedGroup.groups[path]` — but it is handed a **name** path.
- Proven back to back in one page session: `setObject('/Structural envelopes/
  unicycle/wheel', 0, 0)` → canvas diff `n=0`; `setObject('/se-unicycle-c1/66/
  67', 0, 0)` → `n=6351`, matching the positive control. `setObject` is correct
  and is only ever called with the wrong key type.
- Correct uids on the test design: `wheel` = `/se-unicycle-c1/66/67`,
  `saddle` = `/se-unicycle-c1/66/79`. An earlier write-up mislabelled the
  saddle measurement as the wheel.

**What the WIP commit does.** Makes every emitted `id` path equal the "/"-join
of the `name`s above it, so the treeview and `nestedGroup` key on the same
string and the vendor works as designed — no vendored patch, no translation
layer. The uid moves out of the path onto the node as a `uid` FIELD, so the
revision scrubber keeps stable identity across renames. Adds a load-time
self-check that every emitted path resolves in both key spaces.

**Next steps, in order.**

1. The `scripts/test` run was still in flight at wind-down; its result was never
   seen. Re-run `scripts/test --impacted` and `scripts/test --typecheck`.
2. Verify on the harness by canvas pixel-diff — NOT by screenshot alone and NOT
   by the test suite. Containers were left running: `precis-web-demo` on port
   9123, `pwtest` (playwright 1.55.0 + pillow) on the `dev_default` network;
   target `http://precis-web-demo:9123/se/unicycle-c1?level=refined`. Probe
   scripts from the measurement phase are in `.claude/scratch/` under the
   `verify2-` and `setstate-` prefixes (that directory is SHARED with other
   workers — do not assume a file there is yours).
   Re-check, all against a pristine reload with no orbit drag: (a) the shape
   icon on `wheel` now hides the wheel; (b) the container "hidden" mode still
   works AND now survives a subsequent tree click — it previously survived only
   because the mechanism that would clobber it was itself broken, so this is the
   regression to watch; (c) the connections checkbox, which called
   `setState(c.path, …)` with an id path and should have been inert, now works.
3. Delete THIS FILE in the landing commit (delete-on-ship) and make sure the
   invariant survives in the `blocktree_3d` docstrings, not here.

**Why the suite stayed green.** The old tests asserted the SHAPE of the emitted
scene JSON, which was never wrong. The defect was two disjoint key spaces, which
no output-shape assertion can catch. Tests added by the WIP commit assert the
INVARIANT generically instead. Keep it that way.

**Still open, not started:** live scene + client-side isolate, per-block level
chips, bidirectional hover — all in `se-3d-viewer-ux-batch.md`, all of which
were blocked on this path fix. Then `se-mechanical-drc.md`.
