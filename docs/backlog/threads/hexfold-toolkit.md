# hexfold toolkit

**Status:** ends when hexfold composites join, catalogue and validate
themselves end to end with trusted measured rows, the `spec.md` §28 roadmap
reaches its three named test pieces (the box, the rotary ratchet valve and
the T-handle bearing), and the se + hexfold paper (td344088) reports it.
Today the join op and the environment catalogue are **verified working
against prod** — the 2026-09-30 dogfood's two alarming findings were
artefacts of a stale MCP process and are refuted, and the damage that
process did is now detected (`composite_part_stolen`) — so the order below
is: make the execution environment trustworthy, then diagnosable, then
make measured rows trustworthy.
**Last reviewed:** 2026-10-01 (09-30 re-ranks: gr457995/gr457996 refuted;
integrity check shipped and gr458061 moved to the
`session-mcp-shared-server` thread; pillar review added the T-handle
bearing, four gripes and the instrumentation leg; 22:30Z dogfood round
shipped gr458713, confirmed gr454650 harder, closed gr454563. 10-01
round: gr456213 **closed** — it was fixed on 09-29 and I had ranked it 2
off a stale auto-diagnosis, see "No action needed"; gr459058 and gr459057
filed from that round, then both fixed in the 01:18Z round, leaving only
gr459058's cascade-vs-refuse ruling at 4. 02:16Z round: gr454650
**closed and my escalation of it retracted** — already fixed, with
regression tests I never looked for; the one real defect under it was a
ring-less net crashing the stick pass, also fixed — note at the bottom)
**Worktree:** `hexfold-toolkit` (live work is currently in `hexa`)

## Do next

1. **gr458061** — HANDED OFF 2026-09-30 to the `session-mcp-shared-server`
   thread; the fix is in `src/precis/`, not this surface. **Re-derived
   2026-09-30 after a drift report, and the gate is now narrower than this
   item used to claim.** Half one, **gr457361** (truthful status about
   which sha a session is served), is `STATUS:done` and live on the shared
   HTTP server. Half two,
   `backlog/mcp-staleness-title-roundtrip-guards.md` item 2, is no longer
   a fix at all: per **td458385** it closes *by removal* — sessions move
   to the shared HTTP endpoint and the per-session stdio containers die
   with their sessions, so the population it would have hardened stops
   existing. The shared server already carries `--restart unless-stopped`
   plus `PRECIS_CHECKOUT_WATCHDOG`.
   **So the precondition is per-session-transport, not fleet-wide:** a
   session already on `http://127.0.0.1:8765/mcp` has a trustworthy
   execution environment today; a session still on stdio does not. This
   tree is still on stdio, so its dogfood results remain provisional, and
   the concrete unblock for *this thread* is moving this session's MCP
   config to the HTTP endpoint — not waiting for td458385's fleet-wide
   migration, which is `STATUS:open` and `waiting-for:reto` at prio 4.
   Ranked 1 as a precondition, not as work.
2. **backlog/se-join-observability.md**, **slice 1** (`view='report'`) —
   a join's findings live only in the minted structure's meta and there is
   no `view='catalogue'` despite §25.3 specifying one. The dogfood spent
   six SQL queries and a container exec on "which row governed this
   seam?", and never asked the question that mattered because asking was
   expensive. `status: ready` and both open questions decided 2026-09-30:
   three slices in the one file, shipped in order, and `view='report'`
   lives on `se` addressed by block. Slice 1 ships alone and is the
   unblocker; slice 3 (the join dry-run) goes last, when there is a
   reading surface to prove it wrote nothing with.
3. **backlog/se-join-observability.md slices 2 and 3** — `view='catalogue'`
   (SPEC §25.3) then the join dry-run, after slice 1 at rank 2. Slice 3
   goes last by the file's own decision: a dry-run needs a reading
   surface to prove it wrote nothing with.
4. **gr459058, remaining half** — the reporting fix shipped 2026-10-01
   (see "No action needed"), so a retire now announces the structures it
   leaves live. What is still open is the product call it exposed:
   should a design retire **cascade** to those structures, or **refuse**
   while they are live? Both change a write path, the announcement takes
   the pressure off, and deletes here are soft — so this waits on a
   ruling rather than on work. **td458221** is where that answer belongs;
   it is linked to the gripe, and that row is also re-scoped by this from
   a one-time tidy of two orphans to the residue of an ordinary-path
   leak.
5. **gr454488** — five residuals from the 2026-09-28 dogfood: every
   `generate` block trips `mode_binding_mismatch` because generate never
   sets mode; sheet rim port direction is centroid noise; the persisted
   build record drops geometry findings the check-mode echo has; "dry-run"
   wording survives past its rename; generator-declared measures claim
   `origin=user`. Five independent one-line fixes, bundled because one
   dogfood found all five.
6. **gr456641 + gr457997** — one root cause: `EnvKey` records no
   measurement extent, so the seam radius and the armchair leak threshold
   (2.9° against zigzag's 0.025°) are both tube-length artefacts keyed as
   rim-type properties. Do them together. Precondition for
   `trust_measured`, which is the entire point of the catalogue.
7. **gr346966** — stick-rung seam-adjacent angles relax to 82–93° on every
   cap fuse. Independent of everything above, and it caps how far any
   stick-rung number can be believed — including 6’s re-measurements and
   the valve's Q4 clearance stub, which is explicitly gated on it.

## Horizon

1. **`spec.md` §28.3 armchair lids** — the flat-lid family is done for
   zigzag `(6k,0)`; the armchair half is open. Delivers caps for
   armchair-rim parts, and the valve rotor is a lid pair, so the valve
   test piece waits on it.
2. **`spec.md` §28.3 `opening(port=)` + `junction(3)`** — solve a host hole
   from a target rim, then opening + fuse as a tee. Delivers the tilted
   pill (`geom.join.angle`) and the first branching topology. Also what a
   sheet-with-a-hole needs before a non-zigzag tube can protrude from it:
   the `hex(k)` ↔ `(6,0)` route already works (`examples/pillar.hx`,
   `{7:6}`, no collar), but an armchair or chiral end is a mixed rim and
   cannot enter a C6 hole without this.
3. **`spec.md` §28.3 elbow + closure → genus-1 torus, then genus-N** — the
   first real `registry.closure` test; delivers the junction/tube algebra
   the periodic cell and schwarzite nets reuse.
4. **`spec.md` §28.3 box + valve test pieces** — the ~4 nm pillbox and the
   radius-changing shell with a two-lid rotor, each as separate blocks with
   a revolute joint. The acceptance artefacts for the whole discrete half;
   waits on 1.
5. **backlog/hexfold-t-handle-bearing.md** — the third test piece (Reto,
   2026-09-30), alongside the box and the valve.
6. **backlog/hexfold-seam-type-catalogue.md** — the seam-motif rows the
   catalogue's third row type exists for. Waits on Do-next 6, since a
   motif measured at one extent has the same defect the radius had.
7. **`spec.md` §28.8 valve tool set** — clearance field → pocket extractor
   → attachment-site enumerator → complementarity scorer → bond-energy
   audit → drag-vs-torque. Delivers the valve's design surface; its Q4
   clearance stub is gated on Do-next 7.
8. **rotary-ratchet-valve.md Q2** — scrubber cadence per poison species,
   decided by instrumenting the first lining, so it waits on 7.
9. **backlog/hexfold-sp3-seam.md + backlog/hexfold-sp3-isolation-band.md**
   (§28.7) — three- and four-sheet joins at an atom, and the valve's sp³
   isolation loops. Sequenced here by choice, not blocked: §28 lists it as
   unblocked since step 2 landed. `JOINERS` is already keyed on a lattice
   *pair* for it.
10. **backlog/hexfold-instrumentation-leg.md** — the instrumented first
    lining rotary-ratchet-valve.md Q2 (item 8) needs to decide scrubber
    cadence; sequenced right after the seam it instruments.
11. **`spec.md` §28.5–28.6 smooth solve + direction field** — discrete-mesh
    smooth solve, curvature bound, bent collar; delivers the tapered
    (collar-driven) shell the discrete washer step stands in for.
12. **td344088** — the se + hexfold paper. The thread's end state; it
    reports the above rather than waiting on all of it.

## Parked

- **backlog/se-op-handler-test-fixture.md** — was ranked for gr457996,
  which is refuted, so its motivating case is gone. The prepare/finish
  seam is still untested; unparks the next time a phase bug is suspected,
  with a real instance to write the first test on.
- **backlog/se-join-unknown-op-in-web-proposal.md** — `status: ready`, but
  viewer-surface work; unparks when someone is in `precis_web`.

## No action needed

- **gr457995**, **gr457996** — both REFUTED 2026-09-30. The
  `join.part_addressed` guard and the catalogue seed are both correct; a
  16-hour-stale server process produced both symptoms. Kept as the worked
  example behind Do-next 1.
- **gr456201** — ruled: regeneration is the remedy, no new write path.
  Closed on prod 2026-09-30 with that ruling as its resolution; it had
  been left open under this heading, which is the drift Do-next 10 exists
  to stop repeating.
- **gr456202** — not a bug; `rim_word` takes `abs(turn)` by design.
  Closed on prod 2026-09-30, same as above.
- **backlog/se-composite-integrity.md** — SHIPPED 2026-09-30 as
  `composite_part_stolen` (error) on `view='validate'`. The prod sweep it
  called for found **1 violating row in all of prod**, the deliberately
  corrupt `hexfold-catalogue-dogfood` (ref 457890); result recorded on
  gr458061. Kept here one review cycle because the sweep's first run
  returned a confident 0 — it joined `refs.handle` instead of
  `ref_identifiers` where `id_kind='cite_key'`, and matched nothing.
  Re-verified against the deployed code after the 2026-09-30 gate: the
  SQL oracle still returns that one row, and `view='validate'` on
  `hexfold-catalogue-dogfood` reports `1 error(s)` — the
  `composite_part_stolen` finding, naming both composites. **The cause is
  Do-next 2** (gr456213); shipping the detector without it means new
  corruption is reported rather than prevented.
- **gr456203** — CLOSED 2026-10-01 after verifying, not on its label:
  `_leak_finding` names the breached measure(s) and prints each value
  against its own threshold, and
  `test_seam_leak_names_the_breached_measure_and_its_threshold` pins it.
- **gr456212** — CLOSED 2026-10-01, and the "auto-diagnosed as already
  fixed" label this file carried was half wrong. The sigma half was done
  (`seam.sigma`); the element half never landed — nothing in the join
  path compared the two rims' elements. Shipped `seam.element` (WARN,
  matching `seam.sigma`, because `JOINERS` is keyed on a lattice pair so
  heterojunctions can exist later; promoting it to ERROR is a product
  call). The load-bearing test is the negative: two carbon rims draw
  nothing, since every ordinary join passes through it.
- **gr454650** — CLOSED 2026-10-01, **and my 09-30 "confirmed, worse than
  filed" comment on it is retracted.** It was already fixed by
  `build._split_degenerate_tube_rims`, with four regression tests in
  `tests/hexfold/test_len1_rims.py` that all pass on main — I escalated
  it to Do-next 3 without ever checking for existing coverage. The
  "five cyclobutanes" reading was wrong too: SPEC puts
  `ring.size.unusual` *outside* `[4, 8]`, so a four-ring in a seam is
  inside the library's accepted band by design, and that test file pins
  `seam.rings {4:5, 6:5}` as the intended result (every face in `[4, 8]`,
  `sum == 10`, one per fused bond pair). `rings={}` for the bare `len=1`
  armchair tube is likewise consistent — every atom on a rim, no face
  closed, `chi=0 rims=2 residual 0` is what an open band reports.
  Whether hexfold's `[4, 8]` band *should* admit cyclobutanes in an sp²
  seam is a spec question needing a citation, not a defect; not filed as
  one. **The real residue** was a different bug and is fixed: see
  `gr454650`'s own comment and the stick-geometry entry below.
- **ring-less nets and the stick pass** — FIXED 2026-10-01 in the same
  round. `tube(5,5,len=1)` with geometry on raised `IndexError` from
  `stick.py`'s `springs[:, 0]`, because `_angle_springs` returns `[]` for
  a net with no rings and `np.array([])` is shape `(0,)` against a
  documented `(K,3)` contract. One-line `.reshape(-1, 3)`; tests in
  `tests/hexfold/test_len1_geometry_crash.py` assert the report comes
  back *and* that `geom.summary` is present, so a future "fix" that skips
  the geometry pass instead of running it fails the test. It surfaced as
  "hexfold internal error while compiling the spec" on a legal spec.
- **gr459057** — FIXED 2026-10-01. `generate` then `join` in one ops list
  can never work (the mint is deferred until the list validates) and the
  error now says so and names the remedy. Note for a later pass: the
  same-batch case is **not** detected precisely, because
  `finish_generate` sets `bound_kind` and `bound` together, so a block
  generated in the same list is indistinguishable from a bare one at join
  time; the message names the deferral as the likely cause and keeps a
  correct fallback clause. Threading the pending-generate set through
  would allow a precise, separate error.
- **gr459058, reporting half** — FIXED 2026-10-01: a retire now prints
  the structures it left live and the `delete(kind='structure', …)` call
  for each. One test pins the leak itself, so a later cascade fix has to
  update the message in the same change rather than quietly making it a
  lie. The cascade-vs-refuse ruling is Do-next 4.
- **gr456213** — CLOSED 2026-10-01, and the closure is a correction of
  this file. It was fixed on 2026-09-29 by Reto's own ruling that a part
  may not belong to two composites: `prepare_join` refuses via
  `_addressed_part_redirect` as `join.part_addressed`, before either side
  is rebuilt, and names the owning composite's already-exposed port to
  use instead. Verified live on prod — a join addressing a recorded part
  was refused with exactly that message. The unconditional
  `node.parent` assignment the auto-diagnosis pointed at still exists,
  but every endpoint reaching it has cleared the gate, so its old parent
  is either `None` or an ordinary non-composite layout parent, reported
  as a `join.reparented` INFO.
  **Why this file had it at Do-next 2:** I took comment 1's auto-diagnosis
  at face value. That comment describes pre-fix code and was never re-run;
  comment 3 had reset the row to open for an unrelated reason (a false
  "branch pushed" claim, gr458326). So the ranking rested on a stale
  report — the same failure this thread's re-rank note is about, committed
  by the person who wrote the note. The method that caught it was running
  the scenario against prod instead of reading the diagnosis. **Follow-on:**
  ref 457890's corruption is therefore historical damage from the
  stale-process window, not evidence of a live write path, and current
  code cannot reproduce it. Also: the refuse-vs-reconcile question I was
  about to put to Reto was already answered by him on 09-29 — refuse.
- **gr458713** — SHIPPED 2026-09-30 in the 22:30Z dogfood round as
  `net.components` (INFO at one piece, WARN above) on the hexfold check
  report, documented in `spec.md` §13. The obvious implementation is
  wrong and the test file says why: a "sheet" breaks at every bond-verb
  attachment per §6.3, so `len(net.sheet_atoms)` calls the library's own
  `sheet_bud_22` nanobud disconnected. Connectivity follows `net.bonds`,
  which carries the attach edges. That bonded-bud case is the
  load-bearing test, not the disconnected one. Verified on prod against
  the exact spec behind Reto's report: `net is 2 disconnected pieces
  (240, 110 atoms)`, and 240 + 110 = 350, the design's atom count.
- **gr454563** — REFUTED 2026-09-30 by the same round: the parser now
  raises `5:13: unknown parameter 'length' for tube — known: hand, len,
  m, n`, which is the remedy the gripe asked for. Closed. Whoever fixed
  it never linked it, which is why it sat ranked for two days.
- **`hx-sheet-tube-trial`** — not a defect. Reto read it in the viewer as
  a sheet plus a blob; its four-line spec fuses tube to cap and never
  joins the sheet to anything, so two loose components in one se block is
  a faithful render of what was asked for. What it *did* surface is
  gr458713 (Do-next 5).

<!-- Re-rank note, per the README: the first version of this list ranked a
join-side corruption bug at 1 and a test fixture at 5. Both rested on
findings a 16-hour-stale MCP process had manufactured. The lesson that
changed the order is that an unverifiable execution environment outranks
the defects you think you found in it. The 2026-09-30 evening re-rank added
the second half of that lesson: three separate items here — the stolen
part, the two-component net, and (on a sibling thread, same day) a
basepair check with zero callers — were all cases where the code was
willing to report success without having asked the question. Prefer the
item that makes a wrong answer impossible over the one that makes a wrong
answer visible, and prefer both over re-measuring.

The 10-01 rounds added the symmetric lesson, learned the expensive way at
ranks 2 and 3 of this very list. gr456213 sat at 2 because I trusted an
auto-diagnosis of code that had since been fixed; gr454650 sat at 3
because I "confirmed" a defect against prod without checking whether a
test file already asserted the behaviour was intended — it did, and the
chemistry objection I built on top of it contradicted the spec's own
stated tolerance. Both were caught, but only after they had steered the
ranking and, in the first case, been relayed to another session as fact.
So: a gripe's status and its diagnosis are two different claims, and
neither is evidence about current code. Run the scenario, and read the
tests around it, before ranking anything — and when a dogfood "finds"
something in a mature area, the first hypothesis should be that it is
already known. Of nine items probed across the 10-01 rounds, three were
already fixed and one was intended behaviour. -->
