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
**Last reviewed:** 2026-09-30 (gr457931 shipped; dogfooded on prod data)
**Worktree:** `se-3d-viewer`

## Do next

1. **backlog/se-viewer-browser-level-check.md** — no browser-level check
   exists, so every correctness claim rests on a hand-built harness in a
   worktree that will be reaped. Leverage: makes 3–4 and 6–7 verifiable
   instead of assertable, and is the only guard against the defect that
   started the thread (a dead viewer behind a green suite). 3 is the case
   for it: that harness is what found gr458329, and it took a real design
   to do it.
2. **td458066** — Reto decision: whether prod is ever browser-checked by an
   agent and how the Basic credential is handled. Still shapes 1's scope
   (local fixture lane vs prod lane are different builds), but it is a
   smaller question since gr457931 shipped: prod DATA reaches a local
   harness through `view='ops'`, so what is left to decide is only whether
   a browser is ever pointed at the prod deployment itself.
3. **gr458329** — a live level change on a REAL design (prod's `unicycle-c1`,
   copied down via `view='ops'`) fetches the new scene, then does nothing
   visible (n=0 against a measured n=0 floor) and never writes `level` back
   into the URL; the local fixture does both (n=222, URL updated). No console
   error. Promoted above the setState item because it is that item's missing
   reproducer — a real trigger, found by dogfooding rather than reasoning.
   Next step is in the gripe: re-capture the phase-d tree dump at equal
   expansion, which decides between "renders identically for this design" and
   "the swap silently failed".
4. **backlog/se-3d-viewer-ux-batch.md**, visibility via the public setState
   API — applyContainerMode drives visibility through private
   `_rendered.nestedGroup.groups[path]` handles that do not survive a later
   setState(). Same class as the original inert toggle, fails silently.
   Waits on 3, which may already be an instance of it.
5. **td458168** — Reto decision: the level-chip `—` rule and the worked
   example in the same ruling disagree at a leaf, and the mockup is
   inconsistent about which rung of an identical run keeps its letter.
   Blocks the chips below it; a wrong guess means re-emitting a per-block
   field of scene3d.json.
6. **backlog/se-3d-viewer-ux-batch.md**, per-block level chips — new work is
   server-side: scene3d.json must carry, per block, which rungs differ.
   Waits on 5 only for the rule; everything else is specified.
7. **backlog/se-3d-viewer-ux-batch.md**, bidirectional hover — the vendored
   bundle has no hover callback, so this needs an own throttled raycaster;
   the addressing half shipped. Last feature because no design is decided.
8. **backlog/se-mechanical-drc.md** — fastener_insertion_path, final-state
   only, rulings 1–5 in the file. Independent validator pass and the largest
   piece of work, hence last.

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
