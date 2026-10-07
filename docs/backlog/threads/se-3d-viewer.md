# se 3D viewer

## Resume

- **Pillar:** 3d-design
- **Next:** Build gr462129 (/drive resolves a typed handle or DOI to its page) — status review 2026-10-07: prod is R16 and main has no handle/DOI resolution in `precis_web/routes/drive.py`, so the "verify three 302s" step has nothing to verify yet. Then the print dialog when writer scale lands, otherwise fastener_insertion_path.
- **Blocked by:** Nothing for gr462129; [se-machine-design](se-machine-design.md#resume)’s writer scale for the print dialog.
- **Unblocks:** A human inspection surface for machine designs.
- **Acceptance:** Use [the latest handoff](#thread-context): all three /drive queries return 302 on prod; preserve the standing viewer rulings and use [ranked work](#do-next) for the next build.
- **Worktree:** `se-3d-viewer`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

**Status:** ends when a reader can click any block of an se design and
read its pose, envelope, ports, findings and load path in one panel, and
the same scene yields publishable figures. It FEEDS
backlog/se-feasibility-and-cost.md and does not own it — INDEX.md records
that as the far end all three se threads serve. Today the affordances work
and are verified by canvas pixel-diff at the deployed sha. As of 2026-09-30
that is no longer only against local fixtures: prod's `unicycle-c1` was
copied down and pixel-diffed, and every affordance held EXCEPT the live
level change (gr458329, since resolved as a measurement artifact) — the first defect this thread found on
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
**Last reviewed:** 2026-10-02 (strain layers and the xyz/pdb export live
since 567f207f; Reto approved both as they are, review item
se-3d-viewer-1; level chips landed for round 2)
**Worktree:** `se-3d-viewer`

**Prod dogfood, round 2 (deployed 63301c5c, 2026-10-03).** Against prod
data through guide-web: all 39 nightly viewer checks pass (`strain` and
`nowebgl` on `hexa-smooth-drum-v2`, 6123 atoms; `probe` on
`unicycle-c1`); the drum's atom payload serves cold in 0.97 s. `view='drc'`
and `view='fasten'` on prod's `unicycle-c1` match the local result
(flange_bolt_left error, flange_bolt_right warning). Found: `view='fasten'`
drew the error with the same ⚠ as a warning; fixed in round 3 (✗).

**Resume (2026-10-03, session closed to save usage).**
- **Done:** round 3 is deployed and dogfooded (below). gr462129 /drive
  redirect landed in 063421d9, not deployed yet.
- **Next:**
  - after the follow-up deploy, check `/drive?q=pa5303`, `?q=fi<id>` and
    `?q=10.1021/acscatal.3c0196` each 302 on prod, then close gr462129;
  - Do-next 7, the print dialog, once se-machine-design confirms their
    writer scale has landed;
  - otherwise Do-next 5, fastener_insertion_path.
- **Waits on:** the follow-up deploy (gr462129) and se-machine-design's
  writer scale (the dialog).

**Prod dogfood, round 3 (deployed 929107f3, 2026-10-03) → pass.** Against
prod data through guide-web (the viewer code is the same as the deploy;
the /drive redirect 063421d9 is not deployed yet and goes in the follow-up
deploy): all 48 nightly viewer checks pass. The drum's `atomic3d.json` is
241 KB gzip in 0.85 s cold (it was 1.09 MB), the ETag 304 still works, and
`target3d.json` is served separately (297 KB). Drum, dogfood-fold-3 and
unicycle-c1 load one three.js (r184) with no warnings or errors, and the
`bt3d-*` marks fire in order. Open: gr462129's prod 302 check after the
follow-up deploy.

**Resume state (2026-10-02).** Round 2 dogfood (prod 63301c5c): ETag on `hexa-smooth-drum-v2/atomic3d.json` through guide-web on prod data → pass (200 gzip 1.09 MB 0.93 s; If-None-Match 304 0 bytes 0.04 s; stale tag 200); WebGL-off fallback at 390/1280/1600 px → pass, 13/13 (`nowebgl` now checks all three nightly). Reto's look (td461212, 2026-10-03) passed; its follow-ups shipped in
round 3 as he ruled them (se-3d-viewer-5): the atom hover is a field |
value table (atom N9 (N) / residue DG 1 = deoxyguanosine / chain A =
strand hp), the pick header and residue row name the atom and base, an
"axes" checkbox drives the corner marker and the PNG/SVG export follows
it, and the smooth/strain rows stay on every structure with their
references in the labels ("keep it as is, it's cool"). gr462702 is
closed: Reto no longer has the failing browser, so the shipped fallback
stands (se-3d-viewer-3, option 2). Item 2's three speed levers
(se-3d-viewer-6) shipped in round 3; the next build is 5
(the fastener insertion DRC); 3 needs a reproducer and 4 a design. The nightly viewer check covers the atomic overlay
since 2026-10-02; `strain` checks atom hover since instancing, so atom
click-pick is its remaining blind spot. The viewer and the atom overlay
share one three.js (`/static/three-r184/`, import map in
detail3d.html.j2): bump three-cad-viewer and three together, or the page
carries two copies again. Two traps for whoever picks this
up:
- Checking the viewer against prod data without the prod web's Basic
  credential: `scripts/guide-web --db prod --port 9110` (local
  `precis web` as the read-only `agent_ro` role, auth off), then drive
  it from the Playwright container with
  `--add-host=host.docker.internal:host-gateway`. Never route around
  the prod credential instead.
- The local viewer harness designs (`unicycle-c1`, `hairpin-pick`) live
  in the shared test DB, which every `scripts/test` run may wipe —
  re-seed with `scripts/viewer_check.py seed <ops.json> <slug>` before
  a browser check, or it reads as a 404.

## Do next

0. **Standing rulings on the viewer.** Reto ruled 2026-10-01: a tinted
   container STAYS pale (0.25 opacity) — no opaque-while-tinted change;
   2026-10-03: the smooth slider and the deviation/strain rows stay on
   every structure, DNA included (only their labels name the reference).
   Round 2 (deployed): the per-block level chip `[E·I·R·z]` on every
   tree row — click a letter to open or collapse that block alone, click
   the underlined one again to undo. z is a dash everywhere in 3D; whether
   to drop it is review item se-3d-viewer-2; Reto asked whether that is
   only because realized has no 3D drawing yet (answered: yes, the viewer
   draws envelopes at every rung; recommended keeping the dash column). The chips are small at the
   tree's 220 px width — say if it needs widening (it shrinks the canvas).

1. **gr462129** (Reto 2026-10-03, read comment 2) — the /drive search box
   resolves identifiers before the chunk search. Any handle_registry handle
   (pa, fi, qu, dr, gr, td, se, …) or a DOI, whole or as a unique prefix,
   302s to the item's page. A retired ref follows its live slug. An
   ambiguous prefix or no match falls through to today's search. Mirrors
   `FusedBlockSearch.run` in `handlers/_paper_search.py`. Here because
   ingest-and-fetch owns /drive but is held for the demo. One build.
   Acceptance: `/drive?q=pa5303`, `?q=fi<id>` and
   `?q=10.1021/acscatal.3c0196` each 302 on prod.

2. **gr462703** — a progress bar from request to the atoms drawn (Reto).
   Design note `reviews/se-3d-viewer.md` §1–1c, verdicts beside it. Server
   phase SHIPPED: vectorised smoothing (1.0 s → 0.04 s), payload and gzip
   body cached per structure revision with an ETag (f9a8735a, 985cd41b).
   First visit on the drum 3.6 s → 1.55 s (local). The bar SHIPPED in
   round 2: scene → render → server (stripe + seconds, stall text at 30 s,
   2D fallback at 2 min) → download (running MB) → building N atoms
   (chunked per 1000), checked nightly by `strain`'s `progress_*` checks.
   Its 30 s / 2 min paths are unexercised (they need a slow server), and
   `progress_build_repaints` needs a fixture over 1000 atoms (the small
   drum has 1304). Instanced atom and bond meshes SHIPPED in round 2: one
   InstancedMesh per block per kind; on the 6123-atom drum the client build
   went 0.78 s → 0.24 s, first visit 1.55 s → ~0.9 s, revisit 1.30 s →
   ~0.7 s (local). Reto's prod reading (Safari, macOS, after the round-2
   deploy; se-3d-viewer-4): atom payload done at 3.13 s =
   - ~0.9 s before the request starts (html 0.29 s, then module load);
   - 1.09 s server: a cold build. Local cold is 0.96 s, warm 0.03 s; the
     cache is per process and empty after a deploy;
   - 1.14 s download of 1.09 MB gzip (3.86 MB raw). Locally gzip is served
     to Safari's Accept-Encoding; prod behind its proxy is unchecked.
   Payload SHIPPED in round 3: the target surface moved to
   `target3d.json`, fetched on the first tick of its checkbox; positions
   are quantised to an absolute 0.001 Å step, not to significant digits,
   because a nanometre structure can sit a metre from the origin.
   On prod data the drum's `atomic3d.json` went from 1090 KB to 241 KB
   gzip (3.86 MB → 0.73 MB raw).
   The early fetch SHIPPED too: an inline script starts `scene3d` and
   `atomic3d` before mermaid and the module bundle load
   (`_takePrefetch`). On prod data the atom request now starts at 155 ms,
   where it used to wait for the bundle (436–705 ms) and the scene fetch.
   One three.js SHIPPED last: three-cad-viewer 5.0.6 rebuilt from source
   with `three` external, so the viewer and the atom overlay share one copy
   of three 0.184 through the page's import map
   (`static/three-cad-viewer/README.md` has the rebuild recipe). The
   "Multiple instances" warning is gone, and three.js costs 440 KB gzip
   instead of 601 KB. Deployed in round 3 and verified on prod data
   (headless). Left: Reto's own Safari reading of the `bt3d-*` marks, to
   compare with his 3.1 s from before. Optional; ask only if he says it
   is still slow.
3. **backlog/se-3d-viewer-ux-batch.md**, visibility via the public setState
   API — applyContainerMode drives visibility through private
   `_rendered.nestedGroup.groups[path]` handles that do not survive a later
   setState(). Same class as the original inert toggle, fails silently.
   No reproducer yet: gr458329 looked like one and turned out to be a
   measurement artifact (the swap works), so this item is back to needing
   a trigger found rather than reasoned.
4. **backlog/se-3d-viewer-ux-batch.md**, bidirectional hover — the vendored
   bundle has no hover callback, so this needs an own throttled raycaster;
   the addressing half shipped. Last feature because no design is decided.
5. **backlog/se-mechanical-drc.md** — fastener_insertion_path, final-state
   only, rulings 1–7 in the file. Asks whether a fastener can REACH its
   seat; `toolaccess.access()` only ever asked whether a seated screw can
   be TURNED. Ruling 6 (Reto, 2026-09-30) puts the swept-volume RENDER in
   that item too, not here — this thread only consumes it — so the item is
   self-contained. IN BUILD: design note `reviews/se-3d-viewer.md` §2,
   ACCEPTED with one change (verdict §2): a material-ancestor blocker is
   the `material_parent_not_walked` warning, not an error, until
   `_walk_axis` stamps holes in material parents. The validator half
   SHIPPED in round 2 (`toolaccess.insertion_path`, run by `fasten`, so in
   `view='drc'`/`view='fasten'`, not `validate`; findings carry
   `geometry`). On unicycle-c1: flange_bolt_right gets the warning;
   flange_bolt_left gets the ERROR, its body sweep crossing
   seatpost_clamp_bolt (a real hit as drawn: the pinch bolt runs through
   its path). Error findings gate nothing (order, bom, realize never read
   them). Next: the render, from an on-demand endpoint fetched when a
   finding badge is clicked. Scene3d must not run `fasten` per page load.
   Budget: the insertion check took `fasten` on unicycle-c1 from 0.35 s
   to 1.39 s; the render slice must not add to that. The flange_bolt_left
   error is an assembly-order fact (it goes in before the pinch bolt);
   whether to model order is open in the backlog item.
6. **backlog/se-tool-sector-and-lkey-access.md** — the one tool class left
   modelled by a volume nobody believes: an L-key or wrench that only needs
   a ratchet SECTOR is refused by the full-circle disc. Split out of the
   DRC file, which deferred it in two rulings without giving it a home.
   Blocked by 5 (ruling 2 intends the same per-tool-class
   volume model to carry it), hence after it.
7. **Print (3MF) dialog with a scale factor** (Reto 2026-10-03, ruled on
   se-3d-viewer-7: dialog with an editable suggested factor; XYZ/PDB
   whenever a block has atoms; below the printable floor the export is
   refused, naming the smallest factor that prints; single colour now).
   Blocked on se-machine-design's writer scale (`?scale=` on their route).
   One button per printable block. It shows the real
   size and an editable factor, suggested by fitting the longest side to
   ~100 mm rounded down to a 1-2-5 step, above the floor for the chosen
   model type (vdW from 1e7, ball-and-stick ~5e7). It also shows the
   printed size. Above 1 mm it suggests 1:1. ½ build. XYZ/PDB already ship
   (`/se/{slug}/atoms.xyz|pdb`). The writer scale, the print check and the
   atom geometry are se-machine-design's: after their print-file button
   (`routes/se_print.py`) and writer scale. Atom geometry is in
   `backlog/printable-atomic-models.md`. The floor is computed on the
   server (one source of truth), so the dialog asks the route for it and
   does not hard-code 1e7/5e7.

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
condition, never a timeout — is a requirement on the browser check (now built) rather than a
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
`scripts/viewer_check.py`'s docstring: the vendored transport bar
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
