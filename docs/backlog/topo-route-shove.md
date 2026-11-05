---
status: draft
title: "topo route — slice S: trace shove as pinned rip-up in bounded rounds"
pillar: 3d-design
prio: high
model: opus
blocked-by: topo-route-geometry-truth
---

# topo route — slice S: trace shove

## Round 13 resolved retained branches

Compare retention keys at equivalent planning stages: the current branch's
actual resolved runs/cuts and the replacement's candidate runs/cuts. Nominal
input annotations are insufficient after corridor legalization or planner
moves. Carry this state through the rethread seam without changing routing
policy, erasing layers or replacing the existing descendant identity. Keep
ancestry separate. Reproduce all four round-twelve review failures before
editing; preserve earlier probes and actual shaped keepout offences. Cover
both physical layers, implicit and nominal annotations, clearance/no-via/
crossing attribution, and different final layers/via transitions. Checkpoint
locally before the complete serialized round-twelve gate; mark validating
until every gate and exact evidence/census audit completes.

## Round 12 equivalent retained branches

Match retained descendants using the planner's shared effective-layer
interpretation. An implicit nominal assignment and its explicit tuple must
keep the same identity through rethreading; ordinary crossing parameters
can normalize while exact copper points remain unchanged. Preserve corridor,
endpoints, physical layer transitions, planted vias and unplanted join sites
as distinctions. The matching key recovers the existing identity, including
after flips; it never replaces per-descendant identity or transitive ancestry.
Raw WirePath equality falsely rejected unchanged keepout offences. Retain the
real planner/shaping/guard repro, all seven review probes and independent
equivalence/physical-change controls. Replay the same matrices and gates.

## Round 11 descendant preservation

Keep concrete planned-descendant identity alongside input ancestry, including
branches withdrawn before emission. Seed identities from input `WirePath`s;
flips and shaping retain them. Each new rethread branch gets its own wire
identity, while an identical retained branch keeps its previous identity.
Keep whole-tree ancestry unions for provenance, never as the failure identity:
two descendants may share all ancestors, and an unchanged descendant's ancestry
may expand. Clearance, crossing and missing-via withdrawals compare descendant
sets on untouched nets; changed nets must still have no withdrawals. Concatenate
both maps with the full multipass layer index space, including discarded rows.
Pin the three review failures, crossing attribution, partial/complete/transitive
rethreads, retained offences/index shifts, flips and multipass copies. Reproduce
before fixing, then remeasure the unchanged round-ten gate against `bdb8e072a`.

## Round 10 planner lineage

Layer planning carries input `WirePath` identities through flips and rethreads
to the final plan. A rethread replaces a whole net tree, so each replacement
branch inherits the union of that tree's input identities, transitively across
repeated rethreads. This ancestry records provenance, separately from the
round-eleven concrete descendant identities used for withdrawal preservation.
Pin the real partial-tree crash and a swap introducing
a guarded descendant on an untouched net; rerun the round-nine matrix and gate
against the hash-verified `bdb8e072a` baseline.

## Round 9 preservation contract

Every accepted swap, direct regrow and partial-copper withdrawal preserves
untouched branches through the final shaped clearance guard. Compare guard
offences by stable `WirePath` identity, not ephemeral plan indices or only
net names: a second lost branch on an already guarded net is new harm.
The order-loop negotiation uses the same preservation check. Refused strips
retain their copper and diagnostics. Counterfactual gauges compare each row
with the shove bypassed, then compare the bypassed order loop against the
pinned coordinator baseline `bdb8e072a`. Report both guard deltas separately;
neither may introduce a guard. Keep the rule even if the measured gain falls
below the +30-net target.

Extends [topo-route-geometry-truth](topo-route-geometry-truth.md) (G2h's
one negotiation, `_Occupancy`, the (edge, interval, layer) search). Evidence:
`scratch/review/topo-ab-web-report.md` and, filed with this design,
`scratch/review/shove_neg.py` (every `_grow_net` call per cell, each
refusal named), `shove_emit.py` (what fails a net the search built clean),
`shove_swap.py` (the failed net routed ahead of its blocker, 146 instances);
their `.json`/`.log` beside them. HEAD 7c2e5e10e, real rules, 4 boards × 5
orders.

## Bottom line

"Shove" here is **rip-up with the failed net pinned**: the net that cannot
route is grown first, its blockers are re-grown around it, in bounded rounds
over the failed set, accepted when the cell's failure count does not rise.
Not a sideways push of a taut wire, not a global re-order. The 211 strict
failures at HEAD (`_plan_failed_nets`; the gauge's 219 adds 8 emitted live
crossings) split: 98 unreachable in the search (20 static walls no router
fixes), 82 = 41 pairs dropped for an unresolved crossing the search itself
admitted, 26 guard hits on copper the search built clean, 5 no-via. 180 of
211 are decided inside `build_wires`; a geometric push would address 26. G2h's negotiation fired 167 times and was kept 17 (10 %): the
partner cannot re-route under the wall 98 times — and in 76 of those the
partner fails in the final result anyway, so the refusal protected nothing.

## Why not the other two

- **Sideways push inside the homotopy class.** The search's legality is
  topological (G2h chords, closures, a plantable via point); where a wire's
  position matters it is via room (stage C of `_ViaPlanter`). The 26
  emit-level hits are two wires on the same corridor edges the spread could
  not separate (24) — a capacity-truth defect, slice S3, not a dodge.
- **Re-ordering.** Five orders give the same count per board (g6 strict
  failed, prod/rev/shuf1–3: sanity 4/10/4/4/2 of 20; local-66 12/15/11/11/
  13; local-61 10/17/15/13/14; x2 9/15/10/14/16), the same nets failing and
  blocking (VSTOR, VRDIV, DEC4,
  XC2, VBAT top both lists; median blocker position 11/31). A global order
  moves who loses the shared channel. The pinned rip-up IS a local re-order
  (k ahead of j), measured directly: the pinned net routes in 83 of 146
  cases, blind swaps are +0.25 nets each, and one accepted swap per cell
  is worth +48 of 550.

## What the measurements say

Wires level (shove_neg; n → strict failures): unreachable (`len(comps) >
1`) 98 → 98; routed with a same-layer crossing the search could not dodge
(`best.cross`) 53 → 46; clean but the other side of such a crossing (branch
dropped → severed) 43 → 43; clean, guard hit (`track:` on shared corridor
edges 24, apart 2) 26 → 26.

Negotiation (167 fired): kept 17; `partner-unroutable-under-wall` 98 (chord
trigger 36, closure 62; partner fails anyway 76); `no-partner-in-builts` 31
(closure 28: a via barrel or static body walls it, no wire to ask);
`self-still-unreachable` 17; `partner-key-worse` 4. The chord-free probe is
adopted "for `assign_layers`" 52 times; `assign_layers` resolves 7, and 71
of the 84 crossing partners fail: adopting the probe makes two failures of one.

Pinned swap (`shove_swap.py`, k routed just before its blocker j, same order
otherwise, 146 instances): k routes 83 (57 %), k and j both 27 (18 %);
cell strict delta mean +0.25 (better 65, same 45, worse 36; range −5..+5);
the best single swap per cell sums to +48 (sanity 8, local-66 10, local-61
12, x2 18) — the yield of one accepted swap per cell, before rounds.

Cost (`scratch/review/shove_budget.py`, real rules, HEAD): a `_grow_net`
call is 0.09–0.12 s mean, 0.5 s max (x2 rev 58 calls 6.9 s of an 8.8 s
`build_wires`; local-66 rev 56 calls 6.8 s of 8.2 s); an
`_Occupancy.rebuild` is 0.01 s. The grow count is the cost; everything
else in a try is noise.

## The mechanism

In `wires.build_wires`, after the order loop, a **shove phase** over the
failed set F = {k : `keys[k]` has `comps > 1`} (after S1 a crossing is a
`comps > 1` failure), in net order, at most `_SHOVE_ROUNDS = 4` rounds,
each net's try bounded by `_SHOVE_PARTNERS = 4` partner sets:

1. Partners P(k): the chord trigger's crossing nets (nets of `probe.cross`,
   `most_common`), else the closure trigger's `occupancy.nets_on(cut)`;
   tie-break by net index. Partner sets tried: {p1}, {p1, p2}, {p1, p2,
   p3}, {p1, .., p4}; a set is skipped when it has no new partner.
2. Pin k: `occ' = occupancy.rebuild(kept − P − {k})`; `retry_k = _grow_net(k,
   occ'.view(k))`. Require `comps == 1 and not cross` — the pinned net never
   carries a crossing.
3. Re-grow each p ∈ P in its original order on `occ'` + k (chords, closures,
   planted vias of k and of the p's before it). A p with `comps > 1` or
   `cross` is a failure; no probe is adopted inside the phase.
4. Accept iff `fails(after) ≤ fails(before)` over {k} ∪ P, with ties broken
   by `(Σ cross, Σ vias, Σ length)` strictly lower — a swap that only moves
   the failure to j is refused. Accepted: `builts`, `keys`, `used`,
   `occupancy`, `collisions`, `unreachable` replaced wholesale for {k} ∪ P
   (the existing kept-branch bookkeeping), `negotiated` gains
   `(k, p, True)` per partner.
5. A later round runs only over the nets still in F and only if the round
   before accepted something. The grow budget is a count, never wall
   time — deterministic: `_SHOVE_GROWS = 20 × |F0|`, F0 the failed set
   when the phase starts (Reto's ~20, 2026-11-03, read per failed net:
   the old `4 × n_nets` = 124 on a 31-net board with 10–15 failures was
   8–12 per failed net). Derivation: one try costs 1 + |P| grows, and a
   pinned grow that fails ends the try (57 % route pinned, shove_swap),
   so a net's four sets cost ≤ 14 grows, ≈ 9 expected; four rounds over
   a shrinking F fit inside 20 per net, so the cap binds only on a
   board where nothing converges. Wall at the cap, 0.12 s per grow: x2
   rev F0 = 14 after S1 → 280 grows ≈ 34 s on top of an 18 s pass;
   production order F0 = 4 → 80 grows ≈ 10 s on 14 s. `20 × n_nets`
   would be 620 grows ≈ 75 s with no bound from F — rejected.

The main loop's one-shot negotiation stays as the cheap first try (its
accept rule becomes the same count rule). `realise.py` is untouched: the
phase's output is ordinary `Wires` (branches, `unreachable` with
`blocked_by`, `negotiated`, refused cuts), so G5a can still delete
`_route_signals`' passes 2..N.

Frozen: inside a try every net outside {k} ∪ P (its wires are chords and
closures to the grows); between tries and rounds nothing is. The
crossing-probe adoption (`best = probe` in `build_wires`) goes in S1: a
crossing the search cannot make is the later net's failure, named
`Unreachable(blocked_by=partners, layered=True)`; the earlier net keeps its
clean copper.

## Invariants

- Near-zero stays 100 %: at near-zero sanity, x2 and local-61 (prod–shuf2)
  have F = ∅, so the phase is a no-op; local-66's trio and local-61 shuf3
  are the gate rows that may only improve (summed per board ≥ base).
- No new DRC: every grow goes through the same planter and the emitted
  copper through `_guarded_branches`/`_severed_nets`; the gauge gains a local
  DRC column (S1) so a slice cannot trade clean nets for clearance findings.
- Deterministic: net order, `most_common` with index tie-break, count-bounded
  budget, no wall-time cut-off. `failed_strict` byte-identical across 3
  reruns (the A/B probe's test).
- Wall is the grow count: every row reports `shove_grows` and the gate
  is `build_wires` ≤ S1's + 0.15 s × grows used, `_route_pass` ≤ 30 s
  at production order and ≤ 60 s at the worst order on 2× (geometry-
  truth criterion 6; S1's rows: 13.6 s and 18.0 s).

## Slices

Gates: strict-clean summed over the five orders per board at real rules
(HEAD: sanity 76/100, local-66 88/150, local-61 81/150, x2 86/150; sum
331/550), at near-zero (100, 135, 144, 150), and `drc_local` summed per
board at real rules — base measured by S1 before anything else lands.

**S1. Honest crossing failure + DRC in the gauge.** `build_wires` no longer
adopts the chord-free probe; the later net is `Unreachable(blocked_by=…,
layered=True)` and the partner keeps its wires. `zr_gauge` row gains
`drc_local`: error findings of `run_geometric_drc` (rules clearance,
via_mask_web, via_pad_keepout, via_via_keepout, board_edge_clearance,
keepout_copper) over the strict pass's `RealizeResult` + fanout stubs at the
resolved net rules; `g6_table` prints it; the invariant test asserts
`drc_local == 0` on every real-rule row's clean nets. Gate: real-rule sum
≥ 331 + 25 (41 dropped partners, less the 7 `assign_layers` rescues and the
partners failing for a second reason); no board below HEAD; near-zero
unchanged; `drc_local` ≤ base per board. Tier: coder.

Measured on `worktree-topo-shove-s1` (uncommitted, `g6_s1_{base,after}_*.json`):
real sum 331 → 376 (sanity 81, local-66 97, local-61 98, x2 100), every
board up; near-zero 100/135/149/150 (local-61 shuf3 24 → 29), none
down; `drc_local` 0 on all 20 real rows both sides; crossings 0+0 on 16
of 20 real rows; x2 real wall prod/rev 19.8/59.1 → 13.6/18.0 s.
Reviewer's checks before it lands: the near-zero `drc_local` of 28–50
per row is `via_mask_web` + `via_pad_keepout` at the fab figures while
the search ran at the near-zero web — a gauge artefact, identical both
sides; make the column read the rules the row ran at, or say it only
means something at real rules. And the column must run after
`_emit_escapes` once G2d lands (geometry-truth risk 2).

**S2. The shove phase** as above, in `wires.build_wires` (new
`_shove_rounds`, `_pinned_swap`), G2h's accept rule unified with it.
First step, before the phase: re-run `shove_swap.py` at S1 HEAD — the
+48 oracle was measured at base and S1 took +45 of the same pairs, so
the S2 gate is 60 % of the oracle re-measured, floor +20 over S1 (sum ≥
396) and ceiling +30. The +20 floor: ~120 congestion instances remain
after S1 (174 failures less 20 walls, 5 no-via, ~26 guards); 18 % of
pinned swaps route both nets, i.e. ~22 pure recoveries before
cascades. Per board no board below S1 and at least local-61 +5, x2 +6;
near-zero unchanged (local-61 shuf3 29 may only rise); `drc_local` ≤
S1 per board; the wall invariant above on every row, `shove_grows`
printed; `negotiated` kept ≥ 40 of the 167 (S1's own count first).
Report the per-round yield: if rounds 3–4 together add < 3 nets over
the table, `_SHOVE_ROUNDS` drops to 2 in the same commit. Tier:
main loop for the phase, coder for the bookkeeping.

**S3. Capacity truth on shared edges** (the 24 `guarded-shared`):
`_Occupancy.view` closes an (edge, layer) when the free stretch of the
sub-interval the wire would take is shorter than the slots in it (today
`used + weight > cap` on the whole edge), so the search detours or hops
where the spread cannot fit and the shove phase gets the net instead of the
guard. Gate: `guarded` with `track:` reasons 26 → ≤ 6 over the 20 rows;
real-rule sum ≥ S2; near-zero unchanged. Tier: coder. Measured first: if S2
already lowers `guarded-shared` below 10, S3 is dropped.

## Risks

Round 7 acceptance: dropped same-net via joins count as failures, including
committed state bookkeeping. Multi-start uses failure-set inclusion: a strict
subset wins, equal sets use crossings/vias/length, incomparable sets retain
the earlier incumbent. Swaps and direct regrows must clear the final emitter's
shaped-copper guard; untouched nets may not gain guard failures. Beam retains
its best completed state when a later round stalls. Pin each fix with a
regression that fails when removed, repeat the round-6 gate, and attribute
every final guarded net (round 6 was 34 first-attempt / 6 shove-introduced).

Round 6 integration: merge coordinator `87cb57b49` (fanclear round 7 and
G2d), retaining the shove's safety checks and the deferred plane escapes.
Re-run the coordinator's current real escape gauge: 40 rows, real sum at
least 397, per-board floors 81/93/99/93, near-zero 100/135/149/150,
zero local/rule DRC, and real GND-missing total at most 32. Retain complete
census, three identical determinism runs and five x2 route-design timings
at most 300 seconds, plus both topo test families, types and ruff checks.

Round 5 review fixes: exclude legal same-net contacts from the witness; an
untouched net's foreign-crossing set may only shrink. Use the occupancy's
spatial index to find nets touched by either old or new changed geometry,
refresh their saved keys after swaps and withdrawals, and select multi-start
winners by the actual plan failure set. A zero-accept first start skips the
shuffles only after exhaustive failed-net tries with no partial-copper drop.
Pin each regression synthetically and repeat the 40-row gate after merging
fanclear rounds 5–6; keep the drop-partial flag pending Reto's ruling.

- **Mutual blocking is real capacity** — the top risk. Both sides route in
  only 27 of 146 swaps; in 76 of 98 refusals the partner fails anyway. The
  shove mostly chooses who loses; its gain is the cases where the loser had
  already lost (k's recovery is pure) plus cascades, and a blind swap is
  worse 36 times in 146, so the accept rule is load-bearing, not polish.
  The binding limit behind the pairs is via room — the 0.45/0.2
  fab-floor via (via-current, landed) with its 0.20 mm web at 0.5 mm
  pitch — which S cannot move and no smaller via exists to move it.
- **The 53 web-caused and 47 control-only failures** say via siting is
  globally sensitive: a 0.20 mm web change re-sites hops board-wide and
  creates failures elsewhere. S2 re-grows nets, so it re-sites hops the same
  way; per-net churn is expected and the gate is the per-board sum. The 53
  are V's (re-siting along the finished wire); S recovers some by accident
  (a different corridor, a different hop) and may lose others.
- **V before S?** No. S lives in `build_wires`, V (vias-first legaliser) on
  finished wires in plan/emit; no shared code. S first: it changes which
  wires exist and where their hops are (V would legalise vias S then
  moves), and S1's `drc_local` is the gauge V needs. The 5 no-via and 8
  same-net collisions stay V's.
- **Budget on the 2× board at bad orders**: x2 rev after S1 is an 18 s
  pass with F0 = 14; the cap adds ≤ 34 s there and ≤ 10 s at production
  order (mechanism step 5). Criterion 6's worst-order allowance is a
  decision below; the gate is measured at the cap.
- S1's cost (the 7 `assign_layers` rescues) was paid inside its +45.

## Explicitly NOT in scope

PathFinder over all nets (budget: one extra pass of grows); soft chord
legality; moving parts (G5b); fanout sites (G2d); via re-siting on finished
wires (V); the corridor-limit 20 (placement/keepouts).

## Decisions for Reto

Accept on "not worse" with tie-breaks (more swaps, some churn) or on strict
improvement only; and the count budget (3 partners, 2 rounds, 4 × n_nets
grows) in place of seconds.

Ruled 2026-11-03 (Reto):
- Accept on "not worse" with tie-breaks.
- The budget is count-bounded but much larger, about 20 instead of 4
  ("I think we can do many more? 20 or so"). Cost measured in
  `shove_budget.py`.
- Later the same day: the 20 is **per net**, `_SHOVE_GROWS = 20 ×
  n_nets` (≈ 620 grows ≈ 75 s on x2), not `20 × |F0|`. Mechanism step 5
  above still shows the `|F0|` derivation. S2 implements `n_nets`.
- Time: "longer is fine. less than 5 min? we don't need to rush.
  Optimize anyways, but ... working result beats failure that is quick."
  The wall gate is now `route_design` ≤ 5 min on 2× at every order, and
  every row reports wall time and grows used. A faster equal result is
  still preferred.
- Variants before materialising (Reto: "we can shove the crossovers
  around a lot before materializing anything, are we doing variants
  there"). The shove phase already runs before any copper exists: it
  re-grows wires in the mesh, not emitted copper. As designed it is one
  greedy sequence of swaps. With the larger budget, S2 must also measure
  a variant search: keep the best K cell states (beam, K = 4) across
  rounds instead of a single greedy accept, and multi-start over a few
  seeded net orders, keeping the best by the accept rule. Report greedy
  vs beam vs multi-start on the table at equal grow counts. Ship
  whichever wins; keep it deterministic.
