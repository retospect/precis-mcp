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

1. **backlog/se-viewer-browser-level-check.md** — no browser-level check
   exists, so every correctness claim rests on a hand-built harness in a
   worktree that will be reaped. Leverage: makes 2–3 and 5–6 verifiable
   instead of assertable, and is the only guard against the defect that
   started the thread (a dead viewer behind a green suite). 2 is the case
   for it: that harness is what found gr458329, and it took a real design
   to do it. SCOPE SETTLED (Reto, 2026-09-30, td458066): **local-fixture
   lane only — no agent ever drives a browser against the prod
   deployment.** Prod DATA still reaches the harness through `view='ops'`,
   which is what the gr458329 dogfood used; the prod deployment itself is
   checked by Reto by hand at a release boundary if at all. Nothing here
   handles the Basic credential.
2. **gr458329** — a live level change on a REAL design (prod's `unicycle-c1`,
   copied down via `view='ops'`) fetches the new scene, then does nothing
   visible (n=0 against a measured n=0 floor) and never writes `level` back
   into the URL; the local fixture does both (n=222, URL updated). No console
   error. Promoted above the setState item because it is that item's missing
   reproducer — a real trigger, found by dogfooding rather than reasoning.
   Next step is in the gripe: re-capture the phase-d tree dump at equal
   expansion, which decides between "renders identically for this design" and
   "the swap silently failed".
3. **backlog/se-3d-viewer-ux-batch.md**, visibility via the public setState
   API — applyContainerMode drives visibility through private
   `_rendered.nestedGroup.groups[path]` handles that do not survive a later
   setState(). Same class as the original inert toggle, fails silently.
   Waits on 2, which may already be an instance of it.
4. **backlog/se-3d-viewer-ux-batch.md**, per-block level chips — new work is
   server-side: scene3d.json must carry, per block, which rungs differ.
   Rule settled (td458168): the literal rule wins over its worked example,
   and the shallowest member of an identical run keeps its letter.
5. **backlog/se-3d-viewer-ux-batch.md**, bidirectional hover — the vendored
   bundle has no hover callback, so this needs an own throttled raycaster;
   the addressing half shipped. Last feature because no design is decided.
6. **backlog/se-mechanical-drc.md** — fastener_insertion_path, final-state
   only, rulings 1–7 in the file. Asks whether a fastener can REACH its
   seat; `toolaccess.access()` only ever asked whether a seated screw can
   be TURNED. Ruling 6 (Reto, 2026-09-30) puts the swept-volume RENDER in
   that item too, not here — this thread only consumes it — so the item is
   self-contained. Independent validator pass and the largest piece of
   work, hence last.
7. **backlog/se-tool-sector-and-lkey-access.md** — the one tool class left
   modelled by a volume nobody believes: an L-key or wrench that only needs
   a ratchet SECTOR is refused by the full-circle disc. Split out of the
   DRC file, which deferred it in two rulings without giving it a home.
   Blocked by the item above (ruling 2 intends the same per-tool-class
   volume model to carry it), hence after it.

## Horizon

One keystone, then everything that has been waiting on it. If cut short,
cut from the bottom.

1. **backlog/se-pick-hierarchy.md** — click anything, get every level it
   belongs to. Keystone: three parked and two Do-next items have nowhere to
   render until a block can be selected and addressed. Unblocked today
   (id/name unification shipped). Seam with se-nucleic-chain (its Do-next
   4): this thread owns the selection mechanism, that one owns the
   residue/base-pair instance — build the mechanism generic or the second
   instance forces a rewrite.
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
  when cleared. The LIVE-LEVEL half of the same item is no longer settled —
  see Do-next 3 (gr458329).
- The "Multiple instances of Three.js" console warning — expected, documented
  in blocktree-3d.js; no gripe.

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
