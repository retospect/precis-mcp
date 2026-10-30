---
status: draft
title: "pcb: per-design pad overrides on a placed footprint (op='pad')"
pillar: 3d-design
prio: normal
---

# pcb: per-design pad overrides on a placed footprint

## Motivation / why

gr520149 (Reto 2026-10-26: "yes we should have that"). Today a placed
part's footprint can only be replaced whole — `op='footprint'` writes the
SHARED `part_footprints` cache row, so every design using that C-number
sees the change — or have its pins renamed, removed or annotated
(`op='pin_rename'|'pin_remove'|'pins'`). There is no way to edit one pad
of one placed part: nudge a land a vendor moved between package
revisions, enlarge one pad for hand soldering, pull a thermal pad
inboard. The edit must be a design-local delta (other designs placing
the same part are unaffected), must flow into everything that reads pad
geometry (rendered pads, DRC, gerber), and must be revertible per pad.

## In scope (slice 1)

- `put(kind='pcb', id='<slug>', args={'op':'pad','refdes':'J1','pad':'3',
  'dx':…,'dy':…})` — move one pad of one placed instance by a
  footprint-local delta in mm (the frame `view='pinout'` reports
  `local_x_mm`/`local_y_mm` in; rotation and side apply on top).
- `'w':…,'h':…` — resize one pad to an absolute extent in mm. On a
  `shape='polygon'` pad the vertex ring is scaled about the pad centre
  so the drawn outline and `w`/`h` stay the same fact; other pads keep
  shape/rotation/layers/drill untouched.
- `args={'op':'pad','refdes':'J1','pad':'3','revert':true}` drops that
  pad's override; `revert` without `pad` drops all of the part's.
- Storage: `pcb_instances.meta['pad_overrides'] = {pad_number: {dx, dy,
  w?, h?}}` — a delta on the placed INSTANCE. The shared footprint row
  (`part_footprints` / `pcb_local_footprints`) is never written; no
  migration, `meta` is already the per-instance override column
  (`label_spot` precedent, gr496552).
- Application seam: `pcb_session.footprints_by_refdes` applies the delta
  onto a COPY of the resolved footprint row, so pin offsets, the IR pad
  set, land rects, courtyards, DRC and the gerber/SVG pads all render
  the overridden geometry — one rule, no second path.
- Judged-mutation behaviour: the op runs inside `_judged_mutation`
  (refuse=True, the gate's own default — NOT `op='footprint'`'s
  always-store mode). A move/resize that adds or deepens a validity
  error refuses and stores nothing; stored routed copper it collides
  with is ripped and reported (`<net> ripped: <rule>`), margins are
  listed. Revert is judged the same way: the restored catalog pad could
  collide with copper poured into the gap the override opened.
- Read side: `view='pinout'` renders overridden geometry, marks each
  overridden pad and says so in the head; `view='footprints'` gains a
  per-part override column.

## Shipped so far

- Slice 1 (8649f93e9): move (`dx`/`dy`), resize (`w`/`h`), `revert`,
  judged-mutation gating, `view='pinout'`/`view='footprints'` marks.
- Slice 2: dead overrides — a stored key no pad on the resolved row
  claims is surfaced (`dead_pad_overrides` on the applied copy;
  `view='pinout'`/`view='footprints'` print "override on pad <n>: pad
  not on footprint", revert drops it). `drill` re-drills a plated pad
  to an absolute diameter (refused on SMD lands; judged like
  move/resize). Non-plated holes: `hole='add'` + footprint-local
  `x`/`y`/`drill` appends an NPTH; `hole='<n>'` + `remove=true` drops
  one by its resolved `hole_index` (catalog or added) — stored under
  the map's `#holes` meta key, applied at the same seam, judged the
  same way.
- Slice 2 continued: pad add/remove — `add:true` + `pad:'9'` +
  `x`/`y`/`w`/`h` (+ `shape`/`rot`/`layer`/`drill`/`role`/`mask`/
  `paste`/`slot`/`poly`, validated by `normalize_pad`) authors a pad
  the catalog lacks; `remove:true` suppresses one, refused while it
  carries a net (`next=` names `pin_remove` / re-netting). Re-adding a
  REMOVED catalog number stores `{"remove": true, "add": <pad>}` — the
  replace `apply_pad_overrides` documents — so one pad <n> exists and
  `revert` restores the catalog pad. `revert` + `hole:'<n>'` is
  effective-indexed like `remove` (it drops an ADDED hole; a standing
  catalog hole is `unchanged`); a suppressed catalog hole comes back
  via `restore_hole:<catalog index>`, its own call that refuses any
  sibling arg, and `view='pinout'` lists removed catalog holes with
  their catalog indices. Sugar addressing `'<slug>#J1.pad:3'` /
  `'<slug>#J1.hole:2'` folds refdes/pad/hole out of the id into args
  (a disagreeing explicit arg refuses). Views: pinout marks authored
  pads `authored pad add` and gives hole rows an `origin` column;
  `view='footprints'` tokens show `+9`/`-3`/`holes+N-M` beside
  `(dead)` marks.

## Explicitly NOT in scope (later slices)

- Moving a non-plated hole (`dx`/`dy` on holes — remove + re-add is
  the move).
- Changing an EXISTING pad's `slot`, `shape`, `layer`, `rot`, `mask`
  or `paste` (the `add` path takes all of them).
- Per-field revert (`revert_fields=['dx']`); board-wide override
  listing; `drill` on an SMD land (add-or-suppress a plated hole is
  not a land edit).

## Acceptance criteria

- `op='pad'` move and resize on one pad of one placed part: rendered
  pads (`view='pinout'`) and DRC use the overridden geometry; a second
  instance of the same C-number, and any other design, is unchanged.
- A move/resize that creates or deepens a validity error refuses with
  the standard judged-mutation message and stores nothing; one colliding
  with stored routed copper rips the net and says so.
- `revert` removes the named pad's override (or the part's whole map);
  the views read catalog geometry again.
- `view='footprints'` shows a per-part override count; `view='pinout'`
  marks each overridden pad.

## Target + blast radius

- put dispatch: `_dispatch_op`, `PcbHandler.ARGS['put']`,
  `_INLINE_EDIT_OPS` (new `op='pad'`).
- Store: `pcb_instances.meta` only (no migration); `pcb_load` and
  `pcb_graph` hoist `pad_overrides` beside `label_spot`/`edge_mate`.
- IR/session: `PcbIR.inst_pad_overrides` parsed by `from_graph`;
  `pcb_session.apply_pad_overrides` + `footprints_by_refdes`.
- Views: `_render_pinout` (apply + mark), `_render_footprints_view`
  (override column).

## Open questions / decisions log

- dx/dy are DELTAS on the stored pad centre; w/h are ABSOLUTE new
  extents (decided — "move by, resize to").
- The override merges per pad: a later call patching only `w` keeps the
  earlier `dx`/`dy`. A stored `{dx:0,dy:0}` no-op entry is legal (the
  read side shows it as overridden; `revert` removes it).
- Revert runs through the gate rather than writing unconditionally —
  the restored catalog pad is a mutation like any other.
