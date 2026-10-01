# pcb platform

**Status:** ends when the pcb kind's component model, agent interface and
reuse layer are as solid as its routing engine — the platform half of
`backlog/pcb-global-codesign-north-star.md`, pillar 3d-design. Today
sequenced BEHIND `ewod-pcb` on `src/precis/pcb/generators.py`, `drc.py` and
`realize.py`/`maze.py` (the same rule as the easyeda seam — sequence, do
not merge). Peer session EWOD reported 17 open pcb items with no thread
owner on 2026-09-30, 10 prio high; this file gives them one.
**Last reviewed:** 2026-09-30
**Worktree:** `pcb-platform`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/pcb-lazy-netlist-and-checks.md** — `status: ready`; the
   netlist/role/one-check-surface model the rest of this list assumes
   exists, and it already closes three gripes (gr449483, gr449579,
   gr346004) on landing.
2. **backlog/pcb-component-model.md** — `draft/high`; the
   Component/LandPattern/Instance reframe that agent-interface-gaps,
   pinout-view-and-connector-intake and meta-blocks all build on.
3. **backlog/pcb-agent-interface-gaps.md** — `draft/high`; companion to 2,
   names the MCP-surface gaps the reframe creates.
4. **backlog/pcb-pinout-view-and-connector-intake.md** — `draft/high`;
   consumes 2's port/pad contract for prose-to-connector capture; also
   feeds the paper (its own §CONSIDER FOR THE PAPER cross-ref).
5. **backlog/pcb-meta-blocks.md** — `draft/high`; the reuse layer (schematic
   blocks + routed bundles) sits on top of 2–4, not before them.
6. **backlog/pcb-courtyard-polygon.md** — `draft/medium`; mostly shipped
   (items 1–6 landed 2026-08-30), one open cost-curve question survives.
7. **backlog/pcb-card-edge-outline.md** — `draft/medium`; footprint-driven
   outline modification, waits on 2's footprint contract.
8. **backlog/pcb-usb-c-pd-nano-testboard.md** — `draft/normal`;
   deliberately filed to be built only after the engine gaps close —
   waits on ewod-pcb's routing work, not on anything in this file.
9. **backlog/pcb-paper-benchmark-selection.md** — `draft/normal`;
   sequenced after 8 by the item's own stated order.
10. **backlog/pcb-0042-implementation.md** — residual ADR slices; its
    routing story is superseded by `pcb-guided-place-route.md`
    (ewod-pcb's), so what survives here is the `datasheet` kind slice and
    similar leftovers.

## Horizon

- **gr458878 — pin-level datasheet provenance + the two-source pin-name
  cross-check.** Filed 2026-09-30 on Reto's word. The `datasheet` kind and
  `link(rel='datasheet-of')` already bind a datasheet to a PART; what is
  missing is a pin-level link target (links are
  `kind:identifier[~selector]` and pcb has no pin selector), an extraction
  pass, and the cross-check between the footprint's pad->function and the
  datasheet's. Sequenced behind Do-next 2 (`pcb-component-model.md`), which
  owns pin roles/capabilities and already rules that the pass only unlocks
  "with provenance"; this is WHERE that provenance lives. Extend
  `pcb-argue-with-design.md`'s existing anchor grammar, do not mint a second
  scheme.
- **The prod `parts` catalog is EMPTY (0 rows)** — `precis pcb
  refresh-parts` has never populated it, so `search(kind='part', ...)`
  returns nothing and a C-number can only be CONFIRMED (via the EasyEDA
  footprint path), never chosen on price/stock/assemblability. Blocks
  anything in Do-next 2/3 that assumes part selection is a search. Recorded
  in gr458878.

## Parked

- (none)

## No action needed — ranked in ewod-pcb

- **backlog/pcb-always-valid-board-invariant.md** — ewod-pcb Do-next 2,
  and folds in `pcb-placement-must-be-valid-before-routing.md` (same
  defect, narrower — implement one, not both).
- **backlog/pcb-placement-must-be-valid-before-routing.md** — folded into
  the above.
- **backlog/pcb-generator-version-is-a-manual-bump-with-no-tripwire.md** —
  ewod-pcb Do-next 6.
- **backlog/pcb-layer-preferred-direction.md** — ewod-pcb Horizon 3.
- **backlog/pcb-footprint-pad-layer-unvalidated.md** — ewod-pcb Horizon 4.
- **backlog/pcb-tapeout-checklist-seed-items.md** — ewod-pcb Horizon 6.

## No action needed — ranked in pcb-easyeda-round-trip

- **backlog/pcb-design-source-provenance.md** — that thread's Horizon 8.
- **backlog/pcb-flexboard.md** — that thread's Horizon 9.
- **backlog/pcb-engine-plan.md** §5 — that thread's Horizon 7 (2-layer and
  n-layer stackups specifically; the rest of the file is unclaimed and
  folds into this thread's Do-next 10).
