---
status: ready
pillar: 3d-design
---

# se: mechanical DRC — a validator pass for missing fasteners and unconstrained joints

`unicycle-c1` in the `/se` 3D viewer shows a design with blocks that touch (`connects` edges between them) but no fasteners, welds, or glue declared at load-bearing interfaces. `se`'s `validate()` produces findings (envelope overlaps, no_tool_access, stability) but nothing that flags "this interface carries load and has no fastening declaration". PCB's DRC concept (clearance/short checks) has an architectural twin here: a new validator rule family that walks `connects`/joint edges and alerts when an interface with no fastening mechanism exists.

**Open design questions — do not answer, just sketch:**

(a) How does a designer declare "this interface is glued/welded/press-fit/bonded" so the rule does not false-positive on intentional adhesive/integral joints?

(b) Is the rule per-interface (check every connect) or per-load-path (only flag interfaces that carry actual reaction forces)?

(c) Does the rule need the reaction-force solver from `docs/backlog/se-interface-reaction-forces.md` to know which interfaces are load-bearing, or can it run standalone on all structural `connects` edges?

**Cross-link:** `se-interface-reaction-forces.md` is a sibling item from the same session (Reto, 2026-09-28); the reaction-force solver may be a prerequisite for load-path-aware rule tuning.

## Rulings (Reto, 2026-09-29)

**Ruling 1 — final-state accessibility is a design property, not an approximation.**
The first rule to build is a `fastener_insertion_path` check evaluated against the FULLY ASSEMBLED state: can each fastener reach its seat, and can its tool operate, with every other block present. This inverts the usual reading: "all bolts accessible all the time" is a desirable property of a design in its own right. A final-state failure is therefore NOT a false positive to be tuned away — it is the design reporting that it has lost that property. Only when final-state access is genuinely impossible does the design drop to declared subassembly/install-order boundaries, and that drop is a real, stated cost. Final-state access also implies subassembly installability by necessity, so it is the strictly stronger check.
Build order consequence: final-state-only FIRST. Declared install-order boundaries are phase 2, an escape hatch with an explicit cost. Automatic assembly-sequence derivation (searching for any feasible order) remains out of scope and is much larger than both.

**Ruling 2 — one combined hand+tool volume per tool class.**
Model a single swept solid per tool/tool class that INCLUDES the hand, rather than a tool envelope plus a separate hand torus. Shape is an extrusion-like body of revolution, roughly round. Scope for now is screwdrivers and electric screwdrivers only; wrenches and L-keys are not modelled yet but the same per-tool-class volume model is intended to accommodate them later.
Note the consequence explicitly: because a screwdriver is spun in place, its swept volume is already a body of revolution about the screw axis, so the "full 360° sweep versus ratchet sector arc" question does NOT arise for coaxial tools. That distinction only returns for L-keys and wrenches.

**Ruling 3 — this reverses a documented non-goal.**
`src/precis_se/toolaccess.py`'s module docstring currently states, as a deliberate exclusion: "Deliberately not modelled: the hand holding the tool; approach at an angle (every tool here is coaxial with the screw); ratchet arc, i.e. a handle that only needs a *sector* rather than a full circle." Adding a hand envelope overturns the first of those. Flag that the docstring MUST be rewritten when this is built, so the code does not quietly diverge from its own stated scope. Angle-of-approach and ratchet arc remain excluded.

**Ruling 4 — insertion motion is a straight axial sweep.**
The bolt's insertion path is swept along the screw axis only. Tilt-and-align or multi-step insertion is motion planning and is explicitly out of scope.

**Ruling 6 — the swept-volume render ships with THIS item (Reto,
2026-09-30).** Ruling 5's hook is owned here, not by the viewer thread.
`se-3d-viewer` only consumes it. The cross-link in ruling 5 pointed at
`se-3d-viewer-ux-batch.md`, which has no such item and never did — which
would have left a validator item blocked on a viewer item that does not
exist. Build the render alongside the finding that needs it.

**Ruling 7 — ruling 2's tool scope is ADDITIVE (Reto, 2026-09-30).**
Adding the hand for screwdrivers does not narrow the existing tool set.
`src/precis_se/toolaccess.py` models hex keys today — its headline case is
one ("a hex key turning an M3 sweeps a 66 mm circle") — and
`precis/data/driver_envelopes.json` keeps every tool it has, unchanged, on
the current no-hand treatment. The hand is added for screwdrivers and
electric screwdrivers first; other tools gain it later. Do NOT trim the
envelope data to match ruling 2's scope line.

**Ruling 5 — findings must be visually explainable.**
Tools are not rendered in the viewer, so today a `no_tool_access` or insertion-path finding is unexplainable — the user is told "no" with no way to see what blocked it. The swept tool/hand volume for a failing finding must be renderable on demand in the 3D viewer. This is a required hook to design in from the start, not a retrofit. Cross-link `docs/backlog/se-3d-viewer-ux-batch.md`.

The motivating case: in `unicycle-c1`, blocks `flange_bolt_left` and `flange_bolt_right` sit inside the `crown` block with no physical way to install them. Today nothing catches this — `toolaccess.access()` checks only whether a driver can TURN an already-seated screw, never whether the fastener can REACH its seat. `src/precis_se/fasten.py`'s docstring already names the gap in its deferred list: "assembly-order existence — including whether a nut trap can be *reached*".

Owner `src/precis_se/validate.py`, `src/precis_se/joints.py`.

## Open: a fastener blocked only by another fastener (2026-10-03)

`fastener_insertion_path` (built, round 2) checks the final state, so a
screw whose path crosses only another fastener reports an error even when
inserting it first would work. First case: unicycle-c1's
`flange_bolt_left` is blocked by `seatpost_clamp_bolt`; with the pinch
bolt out it goes in. The check has no notion of assembly order. To
decide: whether a blocker that is itself a fastener should become an
install-order finding (phase 2's declared boundaries, ruling 1) rather
than an error, or stay an error under ruling 1's "all bolts accessible all
the time" reading.

## Drift found while reviewing this file (2026-09-30, unicycle)

Three things to settle when this is built; none changes the rulings.

1. **[RESOLVED by ruling 6]** ~~Ruling 5's cross-link is dead.~~ It requires the swept tool/hand
   volume for a failing finding to be renderable in the viewer — "a
   required hook to design in from the start, not a retrofit" — and
   cross-links `se-3d-viewer-ux-batch.md`. That file has no mention of
   tools, swept volumes or DRC. The obligation lives only in this
   paragraph, and the `se-3d-viewer` thread's entry for this item does not
   surface it either, so a viewer obligation is invisible from the viewer
   thread. Either add the render item to the ux-batch file with a back-link
   here, or state that the hook ships with this item and the viewer thread
   only consumes it.
2. **[RESOLVED by ruling 7; the L-key/wrench sector question is filed as
   `se-tool-sector-and-lkey-access.md`]** ~~Ruling 2's tool scope is
   narrower than what already ships.~~ It scopes
   the combined hand+tool volume to screwdrivers and electric screwdrivers,
   and defers wrenches and L-keys. But `src/precis_se/toolaccess.py` models
   hex keys today and its docstring's headline case is one — "a hex key
   turning an M3 sweeps a 66 mm circle, which is the radius that decides
   whether a bracket can sit where the designer drew it". So the new
   hand-inclusive model covers a smaller tool set than the shipped
   swing-clearance check. Fine as a build order, but unstated: whoever
   implements ruling 2 must not narrow `precis/data/driver_envelopes.json`
   to match. The ratchet-sector question ruling 2 defers to L-keys and
   wrenches is on no item.
3. **Ruling 3's obligation is untracked.** The exclusion it says must be
   rewritten is still verbatim in `toolaccess.py` ("Deliberately not
   modelled: the hand holding the tool; …"). No drift yet, and nothing but
   this paragraph will catch it when the hand is added.

Open question (c) is answered — see below — and (a)/(b) remain open.

Also: open question (c) — whether the rule needs the reaction-force solver
to know which interfaces are load-bearing — is answered by the
`se-3d-viewer` thread's Horizon ordering (phase 2 waits on
`se-interface-reaction-forces.md`; final-state-only phase 1 runs
standalone). It still reads as open above.

Separately, the reach-versus-turn distinction is the whole gap and is worth
stating once in code terms: `toolaccess.access()` asks only whether a
driver can TURN an already-seated screw. Nothing asks whether the fastener
can REACH its seat — `src/precis_se/fasten.py`'s deferred list already
names it ("assembly-order existence — including whether a nut trap can be
*reached*").
