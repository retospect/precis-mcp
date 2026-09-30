---
status: idea
title: se 3D viewer: UX batch — honesty banner, selection inspector, non-reloading controls, clip plane
pillar: 3d-design
---

# se 3D viewer: UX batch

Surface: SE 3D block-tree viewer at `/se/<slug>`. Identified in commit 554aeb54 (2026-09-28) along with the acute viewer bugs that were fixed; these UX improvements were recognized but deliberately deferred.

Owner anchors: `src/precis_web/templates/blocktree/detail3d.html.j2`, `src/precis_web/routes/blocktree_view.py`, `src/precis_web/blocktree_3d.py`, `src/precis_web/static/blocktree-3d.js`, `src/precis_web/static/topology-cloud.js`.

## Requested features (checkbox form)

- [ ] **Port the 2D honesty banner to 3D.** The 2D block-tree view already renders a banner stating what the drawing does and does not assert. The 3D view shows solid-looking geometry with no such caveat, which reads as more committed than the model actually is — envelopes are envelopes, not shapes.

- [ ] **Per-block findings in the viewer.** Commit 554aeb54 added validator finding badges to the topology cloud (`topology-cloud.js`, `worstSeverity()`, `nodeTooltipLines()`) and a `findings` key to the scene response. The 3D tree itself still shows nothing — selecting a block should show its findings.

- [ ] **A selection inspector panel.** Click a block, get its pose, envelope dimensions, ports, connects, and findings in one panel. This is also where the interface reaction forces from `docs/backlog/se-interface-reaction-forces.md` would land — the user's original request was "click on the saddle and get something like −500N to +2000N V".

- [ ] **Stop full-page reloading for `level` / `isolate` / `overrides`.** These are query-string params today, so changing the abstraction level or isolating a subtree reloads the whole page and loses camera state. They should refetch the scene and re-render in place.

- [x] **Per-block abstraction level gets an inline segmented chip.** DECIDED (Reto, 2026-09-29). Chosen over a per-row dropdown and a click-to-cycle column, because availability and the current level are both readable at a glance with no interaction. Costs horizontal tree space. Approved mockup:

```
shape edge  name                level

 ●    ○    unicycle            [E·I·R·z]
                                     ▔▔
 ●    ○      fork               [E·I·R·z]
                                 ▔▔
 ●    ○      crown              [E·I·—·—]
                                   ▔▔
 ●    ○      flange_bolt_left   [—·—·R·z]
                                     ▔▔

 E envelope  I interfaces  R refined  z realized
 —  level not available for this block
 ▔  currently active
```

RULING (Reto, 2026-09-29) on what `—` means, asked because the mockup
admits two readings: a rung is `—` when it would render that block
IDENTICALLY to its neighbour rung — clicking it changes nothing visible.
NOT "the block is invisible at that level". So a childless leaf carrying
no realization record shows `[E·I·R·—]`: R and z are the same picture for
it. This is the per-block datum `scene3d.json` has to start carrying.

RESOLVED (Reto, 2026-09-30, td458168) — the contradiction above is
settled, both halves, and the item is buildable.

The rule wins; the worked example is wrong. A childless leaf renders
identically at `envelope` and at `interfaces` (`plan_visibility` gives any
childless node "shape" whatever the cutoff), so it dashes one of them —
`[E·I·R·—]` in the mockup above is NOT what such a leaf should show.

When a run of rungs is one picture, the **shallowest** member keeps its
letter and the rest dash: it is the cheapest rung that produces that
picture, so the letter marks what you would actually click. The mockup's
`flange_bolt_left [—·—·R·z]` (deepest kept) is the inconsistent one;
`crown [E·I·—·—]` is right.

Consequence for the payload: the per-block datum `scene3d.json` carries is
which rungs DIFFER from the next-shallower one — not which rungs "exist".
The chip renders a letter at the shallowest member of each identical run
and a dash everywhere else, which falls straight out of that datum.

Note this is largely a FRONT END for an existing capability: the `overrides` query param already applies per-block level overrides server-side (see `plan_visibility` in `src/precis_web/blocktree_svg.py`, called from `_build_scene3d`). The genuinely new data needed is, per block, WHICH levels actually exist — that must be added to the `scene3d.json` payload. The raw `overrides` text box is replaced by this chip.

- [ ] **A colour-channel selector.** Colour blocks by material, by process, by finding severity, or by change-vs-previous-revision, rather than one fixed scheme.

- [ ] **A clip plane.** Even with the new translucent container mode, a deep assembly is hard to read; a draggable section plane is the standard fix.

## New items (Reto, 2026-09-29)

- [ ] **Hide the `All/Vertex/Edge/Face/Solid` dropdown.** It is a vendored PICKING FILTER constraining which topology type a click selects, intended for BREP models with real topological sub-elements. Our scenes are mesh-only, so every pick returns the leaf path regardless of the setting and the control has no effect. Hiding it is CSS-only.

- [ ] **Label the two tree icons.** Verified from the vendored bundle: index 0 is the SHAPE/fill toggle, index 1 the EDGE/wireframe toggle, built at runtime as `` `tv-icon tv-icon${e}` ``. They are independent because the viewer renders edges as separate geometry from faces — which is why turning faces off and leaving edges on shows the interior. Nothing on screen says this; they need labels or tooltips.

- [ ] **Move visibility to the public `setState` API.** `viewer.setState(path, [shapeState, edgeState])` is the public, TREE-SYNCING visibility API (state slots: 0 hidden, 1 visible, 2 mixed, 3 not-applicable). Our `applyContainerMode` in `src/precis_web/static/blocktree-3d.js` currently uses the PRIVATE `viewer._rendered.nestedGroup.groups[path]` handles, which do not update the tree checkboxes. Record the trap: private `setShapeVisible(false)` does NOT survive a later `setState()` or eyeball click, so the two mechanisms fight; but private transparency/opacity DOES survive, which is why the translucent container mode legitimately stays on the private path.

- [x] **Live scene, client-side isolate.** SHIPPED 2026-09-29. `level`/`overrides` refetch `scene3d.json` and swap the scene in place with the camera preserved; `isolate` is a client-side prune of the shapes tree (`isolateSubtree` in `blocktree-3d.js`) and the server ignores the param. Two things the build had to get right, both recorded in the code: the prune KEEPS the ancestor chain, because re-rooting the subtree would make the retained `id`s disagree with the treeview's own name-join and reintroduce the disjoint-key-spaces defect; and every render re-applies container mode / connections / explode state, since a re-render that dropped them would leave the controls lying about the scene. Below is the original spec.

  `GET /se/{slug}/scene3d.json` already exists and returns shapes, connections, explode, nodes, forces, findings, mermaid, scale, container_paths, changed_uids. The page does not use it after first load; `level`/`isolate`/`overrides` are query params submitted by a plain HTML GET form, causing a FULL PAGE RELOAD. Decision: fetch the full scene once and make isolate a client-side filter, retiring the `isolate` query param. Record the trade honestly: server-side isolate today prunes via `plan_visibility` BEFORE geometry is built, saving per-block tessellation, up to 64 SDF witness-point queries, a stability solve and a validator pass — that is what makes a rebuild slow. Paying it once up front buys instant interaction thereafter, which is the explicit goal ("that should be live because rebuild takes a while").

- [ ] **Bidirectional hover highlight, priced separately as the hard part.** Two obstacles, both verified against the vendored bundle:
  (a) There is NO hover callback of any kind. Picks fire a `notify` callback carrying `{path, name}` but only on DOUBLE-CLICK. Tree→3D highlight is easy (`viewer.highlight(path)` exists), but 3D→tree hover requires building our own throttled raycaster on mousemove.
  (b) ~~ADDRESSING MISMATCH~~ — GONE as of the id/name path unification (2026-09-29, verified against a live viewer by canvas pixel-diff). Scene `id`s and the treeview's `data-path` are now the same "/"-joined name chain, so hover in either direction addresses a row and a mesh with one string and needs no map. The invariant that keeps it that way lives in the `precis_web.blocktree_3d` module docstring. What remains of this item is obstacle (a) alone.

Test: n/a — UX improvements without new invariants; verification is interaction testing on `/se/<slug>` with a multi-level design.
