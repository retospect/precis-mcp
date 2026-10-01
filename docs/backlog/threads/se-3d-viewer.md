# se 3D viewer

**Status:** ends when a reader can click any block of an se design and
read its pose, envelope, ports, findings and load path in one panel, and
the same scene yields publishable figures. It FEEDS
backlog/se-feasibility-and-cost.md and does not own it — INDEX.md records
that as the far end all three se threads serve. Today the affordances work
and are verified by canvas pixel-diff at the deployed sha. As of 2026-09-30
that is no longer only against local fixtures: prod's `unicycle-c1` was
copied down and pixel-diffed, and every affordance held EXCEPT the live
level change (gr458329, Do-next 3) — the first defect this thread found on
real data rather than by reasoning. The list is about making that
verification repeatable by someone other than the author, then the deferred
features. gr457931 shipped 2026-09-30 and changes the shape of that gap:
`get(kind='se', view='ops')` emits a design as a replayable ops list, so a
prod design can now be copied into a local DB over the read-only MCP and
pixel-diffed there. That is a different question from td458066, which asks
whether an agent drives a BROWSER against prod — a check against prod DATA
no longer needs one. No live corruption. gr457995, which used to sit
here as "hexfold join corruption renders as a wrong picture", was refuted
2026-09-30 — there is no join corruption. The real cause was gr458061: a
session MCP process serving stale in-memory code for hours while every
cheap check (mtime, grep, a fresh import in the same container) reads
current. For this viewer that inverts the first question about a wrong
picture — suspect a stale server upstream before suspecting the data.
gr458061 is another thread's item.
**Last reviewed:** 2026-09-30 (gr457931 shipped; dogfooded on prod data;
pillar review same day added three orphan gripes and the property-layer
seam note; gr458393 adopted from local-compute and SHIPPED same day)
**Worktree:** `se-3d-viewer`

## Do next

0. **Dogfood Reto's 2026-10-01 prod pass after the next deploy.** All of
   it is built; what is left is Reto's look on prod:
   - pick steps 1–3 (atom click → panel → cite), which his pass predated;
   - page scroll (vendored three-cad-viewer.css's global
     `body{overflow:hidden; user-select:none}`, undone in
     blocktree-3d-overrides.css);
   - export PNG / SVG beside `explode` — the view as on screen, scale bar
     included; the SVG holds the scene as an embedded PNG and the scale
     bar as vector. A fully vector scene (projected edges, hidden-line
     removal) was not asked for and is not filed;
   - atom hover tip (element · atom name · residue (chain) for a
     realize_chain structure, the scene label otherwise —
     `atomic3d.json` `hover`, from `pick.atom_hover_names`);
   - the atoms checkbox is in the control row on any structure-bound
     design (prod `dogfood-fold-3` is bound, so it renders there).
   Reto ruled 2026-10-01: a tinted container STAYS pale (0.25 opacity) —
   no opaque-while-tinted change.

1. **backlog/se-viewer-browser-level-check.md** — BUILT 2026-10-01
   (viewer-check.yml, nightly); delete once a scheduled GitHub run is
   green. Before it, no browser-level check
   existed, so every correctness claim rests on a hand-built harness in a
   worktree that will be reaped, including the scripts that resolved
   gr458329. Leverage: makes 2–4 verifiable instead of assertable, and is
   the only guard against the defect that started the thread (a dead viewer
   behind a green suite). The item now carries three REQUIREMENTS the hand
   harness paid for — a phase waits on an observable condition rather than
   a fixed timeout, the noise floor is measured in the same run, and canvas
   AND tree are both witnessed — the first of which is what a 2.7 s settle
   on a 3.5 s swap cost (see below). SCOPE SETTLED (Reto, 2026-10-01):
   **a nightly GitHub-hosted workflow against a local fixture, the
   `unicycle-c1` design.** td458066 means agents touch prod data through
   the MCP, not that testing is barred from prod — prod is ruled out here
   for two other reasons: CI would have to hold the Basic credential, and
   a prod run tests the deployed tree after the fact instead of `main`
   before it ships. Prod DATA still reaches the fixture through
   `view='ops'`.
2. **backlog/se-3d-viewer-ux-batch.md**, visibility via the public setState
   API — applyContainerMode drives visibility through private
   `_rendered.nestedGroup.groups[path]` handles that do not survive a later
   setState(). Same class as the original inert toggle, fails silently.
   No reproducer yet: gr458329 looked like one and turned out to be a
   measurement artifact (the swap works), so this item is back to needing
   a trigger found rather than reasoned.
3. **backlog/se-3d-viewer-ux-batch.md**, per-block level chips — new work is
   server-side: scene3d.json must carry, per block, which rungs differ.
   Rule settled (td458168): the literal rule wins over its worked example,
   and the shallowest member of an identical run keeps its letter.
4. **backlog/se-3d-viewer-ux-batch.md**, bidirectional hover — the vendored
   bundle has no hover callback, so this needs an own throttled raycaster;
   the addressing half shipped. Last feature because no design is decided.
5. **backlog/se-mechanical-drc.md** — fastener_insertion_path, final-state
   only, rulings 1–7 in the file. Asks whether a fastener can REACH its
   seat; `toolaccess.access()` only ever asked whether a seated screw can
   be TURNED. Ruling 6 (Reto, 2026-09-30) puts the swept-volume RENDER in
   that item too, not here — this thread only consumes it — so the item is
   self-contained. Independent validator pass and the largest piece of
   work, hence last.
6. **backlog/se-tool-sector-and-lkey-access.md** — the one tool class left
   modelled by a volume nobody believes: an L-key or wrench that only needs
   a ratchet SECTOR is refused by the full-circle disc. Split out of the
   DRC file, which deferred it in two rulings without giving it a home.
   Blocked by 5 (ruling 2 intends the same per-tool-class
   volume model to carry it), hence after it.

## Horizon

One keystone, then everything that has been waiting on it. If cut short,
cut from the bottom.

Pillar-2 human-surface acceptance — "click any block → pose, envelope,
ports, properties, findings, load path in one panel" — spans 1
(se-pick-hierarchy) → 2 (selection inspector) → 3
(se-interface-reaction-forces); the properties row depends on
se-machine-design's `backlog/se-region-property-layer.md`.

1. **backlog/se-pick-hierarchy.md** — click anything, get every level it
   belongs to. Keystone: three parked and two Do-next items have nowhere to
   render until a block can be selected and addressed. Unblocked today
   (id/name unification shipped). Seam with se-nucleic-chain (its Do-next
   4): this thread owns the selection mechanism, that one owns the
   residue/base-pair instance — build the mechanism generic or the second
   instance forces a rewrite. The resolver half has SHIPPED from that
   thread (`precis_se.pick.atom_levels`, also `get(kind='se', view='pick')`;
   contract in the item file). Render half BUILT 2026-10-01: pick route,
   atom raycast, pick panel, "cite" into the design chat, and both tint
   gaps below closed. Left: shift-click shortcut, marking the picked atom
   on the canvas, the hexfold region level, the datum glyph.
2. **backlog/se-3d-viewer-ux-batch.md**, selection inspector + per-block
   findings + honesty banner — waits on 1; the first panel where a block's
   pose, envelope, ports, connects and findings read at once; unparks three
   parked items in one go.
3. **backlog/se-interface-reaction-forces.md** — waits on 2 for somewhere to
   put the number; delivers Reto's original ask and the load-path data 4
   needs.
4. **backlog/se-mechanical-drc.md** beyond fastener_insertion_path — phase
   2: declared install-order boundaries, load-path-aware tuning. Waits on
   3, which answers the file's open question (c).
5. **backlog/se-container-block-is-not-first-class.md** — three subsystems
   each guess what a container is. Waits on nothing; ranked here because
   the payoff shows only once viewer, validator and DRC all read
   containers (from 4 on).
6. **gr458084** — the revision scrubber still full-page reloads; second
   consumer of the live-scene seam, where losing the camera hurts most.
   Convenience, not correctness.
7. **backlog/se-view-figures.md** — waits on 1–2 to know what a figure
   captures; publishable figures out of the same scene.
8. **backlog/se-feasibility-and-cost.md** — the far end the arc serves;
   waits on 3 and 4.
9. **gr450675** — atomic↔smooth view slider with scaffold-deviation
   coloring; a feature request, not blocking anything above.
10. **gr341482** — retire the mermaid `graph LR` topology-panel fallback
    once the force-directed node cloud (already shipped) proves out in
    prod; cleanup, waits on production mileage rather than code.
11. **gr451278** — pcb web viewer: a pad is unidentifiable on the
    rendered board (no mouseover naming refdes/pin/net). Filed against
    the pcb viewer, not this one, but ranked here for the shared
    "identify what you're pointing at" affordance with
    `backlog/se-pick-hierarchy.md` (Horizon 1) — worth checking whether
    one mechanism serves both before building two.

## Parked

- **backlog/se-3d-viewer-ux-batch.md**, colour-channel selector / clip
  plane — unparks with Horizon 2, when a block-selection surface exists.
- **backlog/se-3d-viewer-ux-batch.md**, hide the picking dropdown + label the
  tree icons — CSS-and-copy only; unparks the next time that template is
  edited for another reason.

## No action needed

- **backlog/se-3d-viewer-ux-batch.md**, client-side isolate — shipped, and
  re-verified 2026-09-30 on prod's own `unicycle-c1` copied down via
  `view='ops'`: n=44829 on isolate with NO scene refetch, pixel-identical
  when cleared. The LIVE-LEVEL half of the same item is settled too, after
  a false alarm: gr458329 read it as broken on a real design and the swap
  turned out to work (43 → 32 tree rows, n=643, ~3 s) — see below.
- The "Multiple instances of Three.js" console warning — expected, documented
  in blocktree-3d.js; no gripe.

## What gr458329 turned out to be (2026-10-01)

Resolved as NOT a viewer defect, and the correction is worth keeping
because the mistake is cheap to repeat. The live level change works: on a
copy of prod's `unicycle-c1` the tree row set goes 43 → 32 (matching the
server's 32-node `interfaces` payload against its 43-node `refined` one),
`location.search` picks up `?level=interfaces`, and the canvas moves by 643
pixels in a 48×125 region. All three land at ~3 s. The probe sampled at
2.7 s, saw none of it, and the report read as a silent swap failure.

What was real is smaller and now fixed: those ~3.5 s passed with no
spinner, no disabled control and an empty console, and `loadScene`'s
`reloading` guard dropped a second change inside the window without a
trace. `#bt3d-busy` plus disabling `level`/`overrides` during a refetch
closes it. The durable lesson — a browser phase must wait on an observable
condition, never a timeout — is a requirement on Do-next 1 rather than a
line in a closed gripe.

## Selection highlighting never reached the screen (2026-10-01)

Clicking a topology-cloud node highlighted the cloud node and changed zero
canvas pixels, on every design, since highlighting existed. `selectPath`
tints each partner through `recolour`, which called the vendored
`viewer.updatePart(path, {...part, color})` — and `updatePart` writes
`color` only into its own bookkeeping copy of the parts tree, then rebuilds
geometry; it never touches a material. Partly hidden by eight console
errors per click from the two kinds it rejects outright (connection leaves
are edges-only, containers are not leaf groups), which read as the cause
and were not.

Now: `recolour` tints the leaf's own `front`/`back` material the way the
vendored `highlight()` does, skips connection leaves and non-leaf blocks up
front, and repaints once per selection. Measured on the prod copy: select
`axle` → n=6725 (wheel and both bearings amber), select `wheel` next →
n=1 against base (wheel restored, only a sliver of the axle visible), zero
console errors. Mermaid clicks and viewer picks go through the same
`selectPath`, so they were dead too and are fixed by the same change.

Two gaps left then, closed 2026-10-01 with the pick render half: a
CONTAINER partner is now tinted through its `(envelope)` leaf (selecting
`axle` marks both cranks), and the selected block ITSELF is tinted sky
blue against its partners' amber. The selection is re-tinted after a
re-render (level change, isolate) instead of silently dropped. A
container envelope keeps its translucent 0.25 opacity, so a tinted crank
reads as pale tan, not amber — Reto ruled 2026-10-01 that it stays so.

The scale bar was checked at the same time and is correct, against a
witness that does not trust its arithmetic: across five unclipped zoom
steps, label metres ÷ bar pixels × the model's own pixel height lands at
0.96–1.14 m for a design whose root envelope is 1.05 m tall. The residual
drift tracks render size (anti-aliased edges lost at small zoom), not the
bar.

## explode was inert from its own feature commit (2026-10-01)

Found by probing two affordances nothing had ever driven in a browser —
`explode` and container mode `solid`. `solid` is fine (n=35756, restores
pixel-identically). `explode` had never worked: `initAnimation`'s 4th
argument is the loop mode, not autoplay, and it never plays the clip, so
the click built tracks, showed the vendored transport bar, flipped the
label and left every block where it was. Fixed with `viewer.setRelativeTime(1)`
— the button is a state, not a transport, so it goes to the end of the clip
and holds rather than playing once and snapping back. Now n=41280 with the
model separating, and un-explode restores to n=0.

Two traps it leaves behind, both written up in
`backlog/se-viewer-browser-level-check.md`: the vendored transport bar
appears along the bottom edge on interaction, so an uncropped canvas diff
reports a healthy n=2479 for a completely dead control; and an animation
sampled once can be dead or merely back round the loop, so it needs a
series. The first cost a wrong fix that measured identical before and
after.

## Busy-state follow-ups (2026-10-01)

The `#bt3d-busy` mark that closed gr458329 cost a smaller thing on the way
in, found by probing its own new code the round after: `setBusy` disables
`#bt3d-overrides`, disabling a focused element blurs it, and browsers do not
hand focus back on re-enable. Since Enter in that box is what starts the
refetch, the normal path left `document.activeElement` at `(none)` for the
whole ~3.5 s rebuild and after it. Fixed by carrying the element and its
selection range across `setBusy` and restoring both. Measured either side:
`caret` 14 and the value intact throughout, `active` back to
`bt3d-overrides` afterwards.

Worth keeping as a pattern rather than a changelog line: a control that is
disabled to communicate busy-ness has to give back whatever disabling took,
and for a text field that is focus and caret, not just the value.

## Seam

`src/precis/_pagination.py` is adopted here, not owned here. gr458393 was
fixed under this thread because no thread owned core response chunking and
this thread's dogfooding found it. Reto's call (2026-09-30): re-home it to
a runtime/platform thread deliberately when one exists — a file every MCP
response passes through should not be inherited by a 3D-viewer thread by
accident.


The non-geometric property layer (hydrophobic, charge, field, optical) is
`se-machine-design`'s model to build; this thread renders whatever the
model carries. Do not design the property layer here.
