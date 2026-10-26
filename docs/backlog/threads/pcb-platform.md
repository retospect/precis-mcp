---
roster: active
pillar: 3d-design
effort: medium
---
# pcb platform

## Resume

- **Next:** The lazy-netlist item's open slices (Do next 2: Slice 1's Reto decision and the `ewod-dogfood-2` re-run, then Slice 6).
- **Blocked by:** Sequence shared generator and DRC work behind the active PCB threads.
- **Unblocks:** A coherent component model and agent PCB interface.
- **Acceptance:** Use [pcb-lazy-netlist-and-checks](../pcb-lazy-netlist-and-checks.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `pcb-platform`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when the pcb kind's component model, agent interface and
reuse layer are as solid as its routing engine — the platform half of
`backlog/pcb-global-codesign-north-star.md`, pillar 3d-design. Today
sequenced BEHIND `ewod-pcb` on `src/precis/pcb/generators.py`, `drc.py` and
`realize.py`/`maze.py` (the same rule as the easyeda seam — sequence, do
not merge). Peer session EWOD reported 17 open pcb items with no thread
owner on 2026-09-30, 10 prio high; this file gives them one.
**Last reviewed:** 2026-09-30
**Worktree:** `pcb-platform`
**Allocation decision:** active — Reto 2026-10-17 (already landing).

## Do next

1. **backlog/pcb-workspace-code.md** — `status: ready` (Reto 2026-10-22,
   Q1–Q6 approved); construction moves to a Python script in a workspace,
   place/route/move/rip/import_epro/views/DRC stay in MCP. Ranked first
   because it retires or reshapes items 5 and 6 and constrains 3 and 4. Its
   ordered slices live in thread [structure-workspaces](structure-workspaces.md);
   this thread owns only the pcb-side checks (DRC `frame` column, request
   lanes) when they touch `drc.py`/`realize.py`.
2. **backlog/pcb-lazy-netlist-and-checks.md** — `status: ready`; the
   netlist/role/one-check-surface model the rest of this list assumes
   exists. **Slices 2–3 shipped 2026-10-13** (one commit on worktree
   `bl-pcb-netlist-s23`, committed not landed): every footprint pad is a
   declared pin (`_pcb_apply` completion + the EWOD sink's full
   `channel_pins` with `unused` on the unwired share), the §1j markers
   `nc`/`unused`/`must_terminate` are live, `undeclared_pad` is the safety
   net and `unconnected_pin` is phase-aware (error at `fab`). Slice 3 was
   already landed in code (polygon-aware `check_via_pad_keepout` 3c8db49a,
   `check_outline_containment` 5b91abe18; the same-net-fixed-via exemption
   stays as authored-copper policy) — verified and deleted from the doc.
   **Slices 5 and 8 shipped 2026-10-13 too** (`view='check'`, authored
   `op='phase'`, the etype/voltage/rail ERC rules).
   **Left:** Slice 1's Reto decision + the prod re-run of
   `ewod-dogfood-2`; Slice 6's open list (congestion cost,
   rip-up-and-reassign, spare channels) and Slice 7. Slices 9 and 6 core
   shipped 2026-10-15.
3. **backlog/pcb-component-model.md** — `draft/high`; the
   Component/LandPattern/Instance reframe that agent-interface-gaps and
   pinout-view-and-connector-intake build on. Check against the workspace
   builder API (`pcb.part`/`pcb.Footprint`) before building.
4. **backlog/pcb-pinout-view-and-connector-intake.md** — `draft/high`;
   consumes 3's port/pad contract for prose-to-connector capture; also
   feeds the paper (its own §CONSIDER FOR THE PAPER cross-ref). Check
   against the workspace builder API before building.
5. **backlog/pcb-agent-interface-gaps.md** — `draft/high`; the removal path
   (Gap 5) is superseded by the workspace (removal = delete the script
   line; `op='remove'`/`'disconnect'` stay unbuilt). The other gaps stay,
   re-read against the workspace surface before building.
6. **backlog/pcb-meta-blocks.md** — superseded by the workspace (meta
   blocks = a `@pcb.module`; `op='instantiate_block'` stays unbuilt);
   level 2 (pre-routed bundles) is pcb-workspace-code slice 3.
7. **backlog/pcb-card-edge-outline.md** — `draft/medium`; footprint-driven
   outline modification, buildable now on today's footprint dict (Reto
   2026-10-21), independent of 3's split.
8. **backlog/pcb-usb-c-pd-nano-testboard.md** — `draft/normal`;
   deliberately filed to be built only after the engine gaps close —
   waits on ewod-pcb's routing work, not on anything in this file.
9. **backlog/pcb-paper-benchmark-selection.md** — `draft/normal`;
   sequenced after 8 by the item's own stated order.
10. **backlog/pcb-guided-place-route.md** §From pcb-0042-implementation —
    ADR 0042 web ratsnest/BOM, the phase machine and the deploy binaries
    (merged 2026-10-21; the rest of that file is ewod-pcb's).
11. **backlog/pcb-graph-routing-fattens-traces.md** — `draft/normal`,
    filed 2026-10-23 (Reto's no-flood-fill ruling, me507871): the
    pour-removal half landed in `g4-pours-off`; the survey inside says
    what "fatten traces by net property" already has (current→width
    via IPC-2221) and lacks (impedance→width, return-path→geometry,
    per-edge widths). Do not build without a further ruling.
12. **backlog/pcb-design-flow.md** — `ready/high`, filed 2026-10-26 from
    gr519925 + gr519924 (TAZ leaf dogfood): the requirements-to-fab
    skill, selection record (graph memory, selects/rejects, ruled
    2026-10-26), budget convention, consigned BOM lines, mech/coating
    checklist. Slice 1 is a skill with no code and can go now; it is
    independent of items 1–11.

## Horizon

- **backlog/pcb-unified-place-route.md** — `draft/high`, Reto 2026-10-18
  ("anneal the totality of placement (including labels) routing and so on
  in one smooth space"): one smooth cost over poses, labels, track
  vertices and vias (money out), basin hopping with discrete topology
  moves, insertion that shoves by descent; consumes vector-router slices
  1–2, slot beside slices 3–5. Slices 0–2 landed in `field.py` (T1/T4,
  SAT T4 + T5/T6 + JLCPCB pair table, T9/T10 wells with θ as a real
  variable). 2026-10-21: Reto switched the default placer to it —
  `field_polish` on by default (placer-a); 2026-10-22 Reto took the
  default off again — opt-in until the silk gate lands (silk_missing on
  film/esp32c3 seeds); the annealer lives on as the bench's `anneal`
  column; slice 2 (θ, wells) is placer-b. 2026-10-25: Reto deleted the
  engine (`field.py` + `field_polish` accepted-ignored, bench column
  trimmed); the smooth-energy spec `pcb-unified-place-route` is parked
  Superseded — topo-place is the path.
  placer-c: ABNORMAL-stalled stages retry θ-pinned (gr503452) and the
  gate is legality + field energy, anneal `total()` report-only
  (gr503458) — polish applies on sanity free-bottom and p2u.
  placer-d+e: seed spreads shelves over the outline + rigid groups get
  a far-edge strip (gr503556); bin-density spread term (normalized by
  total part area after it out-weighed p2u's supply tree); bounded
  descent + snap clamp + partial revert (gr503640); mounting holes are
  T4 fixed obstacles on both sides (gr503640 fix 2). Bench: placer-e.
  placer-f: written rotations are grid values in [0,360) — seed-θ
  fallback, k=4 dominant + ROT_TILT_MIN_GAIN tilt gate, wirelength
  never-worse accept gate (gr503694 + the container-only sanity
  bottom-header divergence). Next: slice 3 labels as variables (T8).
  placer-i: gr503556 seed cover closed — groups take a top-anchored
  corner column sized to their rotated bbox (member axis along the
  board edge), parts shelf-pack the L around it; nano bands max
  34.5 % (was 59). `_signed` batched through shapely ufuncs:
  8-seed place+route 27:48 → 20:02.
  placer-j: Q7 rotation pricing — T10 = preferred-number well over
  (90,45,15)° tiers (90 free, 45 priced, 15 more), stage-1 rotation
  well switch (ROT_WELL_FROM_STAGE1) against early θ drift, tilt gate
  folded into priced E; snap_to_preferred shares the well's grid.
  placer-k: gr503556 polish knot — the (Σa)²-normalized BinDensity
  gradient is ~10³ below the T1+tree pull, so no portable fixed λ
  counters re-clustering (measured 0.03 vs 46; λ 0.1→50 all knotted).
  λ is now force-balanced at handoff (DENSITY_FORCE_FRAC=0.25 of the
  wirelength gradient, capped), ePlace-style — re-clustering pays for
  itself inside E.
  gr504889: a fully posed, legal import remains the incumbent across the joint
  place+route pass; reject re-placement when the T1+D14 wirelength
  accept metric is more than 2% worse than those input poses. A
  legalization/outline repair rules out restoring the illegal input.
- `pcb-workbench-layout` — shipped and deleted 2026-10-23: schematic
  left, layout right, one chat, cross-probing, shared zoom/pan module
  with se. Slices 1–3 landed (focus mode + pinning 124b468d9; sheets by
  placement group with off-sheet connectors + cross-probe sheet picking;
  se adoption — se 2D on `viewport.js` (`se-2d.js`), se 3D f/0/+/− keys
  + the gesture table (`blocktree-3d.js`)) and the Proposed items
  (part cards + cached thumbnails, `pcb_parts.py`) shipped with them.
- **backlog/pcb-datasheet-review.md** — `in-progress/high`, filed
  2026-10-13 from the taz15-blank dogfood: shared part facts, design
  intent in graph memory, re-derivable verdicts, review job, shortlist
  selection with lifecycle, design from facts, human review. Absorbed
  `pcb-design-source-provenance.md`, the ratings-validation part of
  Do-next 2 and the checklist lifecycle TTL. **Slices 2–4 shipped
  2026-10-14** (`ds-review`): `op='pins'` annotation + `max_current_a` +
  `over_current`; `passive_rating` — catalog-description parse, the
  `fixed-derating` margin policy, pin-declared `min_dielectric`; and the
  re-derivable-verdict substrate — `checklist_verdicts.origin` +
  `.inputs`, `stale (input changed: …)`, `op='forget'` (machine verdicts
  only) — with per-net "… unknown: ask the user" rows throughout. Slice
  1 (epro import keeps part identity) landed from `p1-epro` as
  `a7863eda3`; slice 5 waits on `datasheet-facts-mesh.md`.
- **Pin-level datasheet provenance + the two-source pin-name
  cross-check (gr458878 closed).** Filed 2026-09-30 on Reto's word. The `datasheet` kind and
  `link(rel='datasheet-of')` already bind a datasheet to a PART; what is
  missing is a pin-level link target (links are
  `kind:identifier[~selector]` and pcb has no pin selector), an extraction
  pass, and the cross-check between the footprint's pad->function and the
  datasheet's. Sequenced behind Do-next 3 (`pcb-component-model.md`), which
  owns pin roles/capabilities and already rules that the pass only unlocks
  "with provenance"; this is WHERE that provenance lives. Extend
  `pcb-argue-with-design.md`'s existing anchor grammar, do not mint a second
  scheme.
- **The prod `parts` catalog was EMPTY** — recorded in gr458878 (closed);
  the Open API walk has since populated ~2.0M rows and refills the low
  range (ewod-pcb handoff item 4).

- **preferred.py wells as an anneal cost term** — conditional leftover of
  the soft-pin/preferred-number snap item (shipped on `pcb-place`,
  7aa3886b5): build only if the snap pass's rejection rate shows parts
  left unsnapped. Also from that item: taz15 CN2/CN5 still needs the pin
  applied — pcbfixes-c2's job after the `_sides_clear` fix lands.

## Parked

- (none)

## No action needed — ranked in ewod-pcb

- **backlog/pcb-always-valid-board-invariant.md** — ewod-pcb Do-next 2.
- **backlog/pcb-footprint-pad-layer-unvalidated.md** — ewod-pcb Horizon 4.

## No action needed — ranked in pcb-easyeda-round-trip

- **backlog/pcb-flexboard.md** — that thread's Horizon 9.
- **backlog/pcb-engine-plan.md** §5 — that thread's Horizon 7 (2-layer and
  n-layer stackups specifically; the rest of the file is unclaimed and
  folds into this thread's Do-next 11).
