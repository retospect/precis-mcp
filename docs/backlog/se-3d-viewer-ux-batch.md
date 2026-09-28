---
status: idea
title: se 3D viewer: UX batch — honesty banner, selection inspector, non-reloading controls, clip plane
---

# se 3D viewer: UX batch

Surface: SE 3D block-tree viewer at `/se/<slug>`. Identified in commit 554aeb54 (2026-09-28) along with the acute viewer bugs that were fixed; these UX improvements were recognized but deliberately deferred.

Owner anchors: `src/precis_web/templates/blocktree/detail3d.html.j2`, `src/precis_web/routes/blocktree_view.py`, `src/precis_web/blocktree_3d.py`, `src/precis_web/static/blocktree-3d.js`, `src/precis_web/static/topology-cloud.js`.

## Requested features (checkbox form)

- [ ] **Port the 2D honesty banner to 3D.** The 2D block-tree view already renders a banner stating what the drawing does and does not assert. The 3D view shows solid-looking geometry with no such caveat, which reads as more committed than the model actually is — envelopes are envelopes, not shapes.

- [ ] **Per-block findings in the viewer.** Commit 554aeb54 added validator finding badges to the topology cloud (`topology-cloud.js`, `worstSeverity()`, `nodeTooltipLines()`) and a `findings` key to the scene response. The 3D tree itself still shows nothing — selecting a block should show its findings.

- [ ] **A selection inspector panel.** Click a block, get its pose, envelope dimensions, ports, connects, and findings in one panel. This is also where the interface reaction forces from `docs/backlog/se-interface-reaction-forces.md` would land — the user's original request was "click on the saddle and get something like −500N to +2000N V".

- [ ] **Stop full-page reloading for `level` / `isolate` / `overrides`.** These are query-string params today, so changing the abstraction level or isolating a subtree reloads the whole page and loses camera state. They should refetch the scene and re-render in place.

- [ ] **Tree actions instead of the `overrides` text box.** The overrides control is a raw text field; the operations it expresses (show/hide/pin a block at a level) belong as per-row actions in the tree.

- [ ] **A colour-channel selector.** Colour blocks by material, by process, by finding severity, or by change-vs-previous-revision, rather than one fixed scheme.

- [ ] **A clip plane.** Even with the new translucent container mode, a deep assembly is hard to read; a draggable section plane is the standard fix.

Test: n/a — UX improvements without new invariants; verification is interaction testing on `/se/<slug>` with a multi-level design.
