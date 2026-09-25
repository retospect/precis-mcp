---
status: ready
title: pcb — lazy netlist (supply/client roles, matching solver), one check surface (DRC+ERC), and four fabric defects folded in as slices
prio: high
model: opus
---

# pcb — lazy netlist, one check surface, four fabric defects

Design session Reto + agent, 2026-09-24, working `ewod-dogfood-2` through
route/DRC cycles on the prod web view. Part 1 is the model; the slices
follow from it. Gripes **gr449483, gr449579, gr346004** close against this
file and **gr347037** stays parked on it — each slice below carries its
measurement and repro, so this spec is the only record after closure.
(Each of those gripes carries a 2026-09-24 comment "folded into
`pcb-0042-implementation.md`"; that file does not mention any of them —
the comment is wrong, this file owns them.)

Hard dependency: `finding-stable-identity.md` (delta reporting in §1g).
Cross-referenced, not duplicated: `ewod-controller-and-hv-supply.md`
(Defect A, Slice 4 blockers, the RAW/VBUS and CH340N traps),
`pcb-ewod-multitile.md` (rulings 1–11), `pcb-pre-place-route-blocks.md`
(fabric as fixed copper, the known −0.025 mm residue).

## Motivation / why

Three defects in one week on one board (gr346744, gr449483, gr449579) share
a shape: geometry reasoned about at the wrong level of the pipeline, and a
check surface that cannot say which complaints are new. Underneath them the
netlist model is wrong for this board class: 53 electrodes must each reach
some HV507 channel, nobody cares which, and today that freedom is expressed
as 53 hand-written connections plus a `pin_swap_groups` patch table
(`pcb_pin_swaps`, migration `0141_pcb_pin_swaps.sql`: "a derived-assignment
table ... one row per physical pin whose EFFECTIVE net currently differs
from what `pcb_netconns` says"). That is back-annotation implemented
internally. The model below deletes it.

## Part 1 — the model

### 1a. A net is a constraint; a swap set is a freedom

Two different kinds of statement. A **net** says *these pins must end up
electrically common* — the router must **satisfy** it. A **swap set** says
*this assignment is not fixed; pick one* — the router must **exploit** it.
The axis is constraint-vs-freedom, not set-vs-bag; folding both into one
object is the error to avoid. Ground is a genuine net (every one of those
pins really is common); it only looks assignment-free because a plane
serves it.

### 1b. Supply/demand role tagging — no explicit pools

Reto: "This part supplies p2...p32 as a set of potential pad driver" and
"this pin is a pad driver client". A **part declares it SUPPLIES terminals
under a role name**; a **terminal declares it is a CLIENT of that role**.
Nobody writes "pool A ↔ pool B"; the matcher resolves clients against the
union of suppliers. It is a type system: role = type, supply = provider,
client = consumer, matching = resolution.

Why this beats an explicit bundle object:

- **Composes without edits.** Add a second HV507, it declares the same
  role, the pool grows. A bundle object would need editing.
- **Local.** The part author knows it supplies drivers; the array knows
  its electrodes want one; neither knows the other exists.

**What it deletes — one concept replaces four:**

| today | under supply tagging |
|---|---|
| `pin_swap_groups` on `pcb_components.meta` (`pinswap.PinSwapGroup`, resolved per run by `workers/job_types/pcb_route.py::_resolve_pin_swap_groups`) | gone — a supply declaration over the part's pins |
| explicit bundle / pool objects | never built; implied by role name |
| within-part swap (an AND gate's two interchangeable inputs) | the part supplies two `and_input` slots; the two incoming nets are clients |
| cross-part swap (ten HV507s, one pool) | same role name, one pool |

It also dissolves a live wart: ruling 6 (`pcb-ewod-multitile.md`) drops
unwired spare pins from the swap group — "an unwired spare pin has no IR
pin id to swap and is never listed" (the generator's own comment at the
`pin_swap_groups` emission in `generators.py::_expand_ewod_pad_array`).
Under supply tagging, supply is declared over the part's pins **from the
datasheet**, not over whichever pins happened to get wired.

Three things the model needs beyond the two tags:

1. **Roles carry constraints**, or two drivers at different voltages on one
   board match silently. A role is a name plus the attributes the matcher
   must respect; a client may demand a refinement (`pad_driver` at
   `>= 250 V`). **Keep it thin** — name + working voltage is probably the
   whole of it (max current if a board ever needs it). Resist schema
   sprawl: no role inheritance, no role algebra.
2. **Cardinality on the role**, default 1:1. Some roles fan out (a clock
   supplies many clients); declare `fanout: n | unbounded` on the role.
3. **Unmatched is an ERC error, and the best one in the set.** 53 clients
   vs 64 suppliers = fine, 11 spare. 70 vs 64 = `11 pad_driver clients
   unsupplied`, reported before any routing. The current model cannot
   express this; today it is a routing failure or nothing.

### 1c. Members are tuples, not pins

A supplier offers **blocks**: `[["HVOUT1"],["HVOUT2"],…]` (blocks of 1 — a
plain bag) or `[["D+","D-"],…]` (blocks of 2 — a differential pair carried
together), plus `within_block_swappable: bool` (polarity lock). A bag of
pins is the degenerate case. Paired swap needs no third class; this one
field covers it.

YAGNI judgement, recorded: the general machinery is a permutation group
acting on pins (ten chips × 64 channels is a wreath product), and
specifying arbitrary groups IS YAGNI. Block structure is NOT: differential
pairs are everywhere (the USB-serial bridge on the very next board —
`ewod-controller-and-hv-supply.md` ruling 13), and retrofitting blocks
later is a data migration. One field now, zero new concepts.

### 1d. Lazy netlist — the assignment is derived, so back-annotation never exists

Reto: "Lazy netlist generation I suppose." The engine already has the
pattern one level down — `precis-pcb-route-help`: "Sketch is canonical;
copper is derived." Apply it one level up:

| layer | status |
|---|---|
| intent — "these electrode terminals are `pad_driver` clients" | canonical, authored |
| assignment — `HVOUT17 → R3C4` | derived, solved each route |
| copper | derived from the assignment |

Nothing upstream ever named a channel, so nothing needs annotating back.
Sharp version: **`pcb_pin_swaps` IS back-annotation, implemented
internally** — the author says HVOUT1, the swap layer says "actually
HVOUT17", every reader reconciles (`PcbHandler._build_ir` had to be taught
to apply persisted swaps after DRC/gerber disagreed with the routed copper,
`pcb-ewod-multitile.md` rulings-6+7 measurement note). Delete the authored
assignment and the derived table stops being a patch and becomes the value.

Consequences:

- **Rule (Reto, 2026-09-24): a net is named after its CONSTRAINED end;
  the FREE end dangles until the solve binds it.** A principle, not a
  convention: in a supply/demand match the client is the fixed end (53
  specific electrodes, each a distinct thing needing a driver) and the
  supplier is interchangeable (any of 64 channels). Naming after the
  supplier would churn the name on every re-solve — exactly what the
  derived-assignment design exists to avoid. So the electrode names the
  net (`ARR1_R0C2`); the channel binds later. Current naming is already
  client-based; this makes it a rule enforced at `_pcb_apply`. It also
  underwrites handle stability (`pcb:…~net/ARR1_R0C2` is quotable
  precisely because R0C2 is the constrained end — see the
  `pcb-argue-with-design.md` note below).

  Two consequences:

  1. **Dangling is a first-class state, not an absence.** Three states
     collapse into one today and must be separated, or the check surface
     cries wolf:

     | state | meaning | class |
     |---|---|---|
     | `unresolved` | the solve has not run | EXPECTED — a phase fact (§1g), not a finding |
     | `unmatched` | the solve ran and could not bind this client | ERC ERROR — the "11 `pad_driver` clients unsupplied" case of §1b |
     | `unrouted` | bound to a terminal, no copper | DRC ERROR — the only one that exists today (`drc.py::check_unrouted`) |

     Only the third exists today, which is why an unbound net currently
     has nowhere to live except as a routing failure. This rule closes
     that gap.
  2. **Both-ends-free is an authoring error.** A connection where neither
     end is a named terminal has no stable identity — it cannot be named,
     handled or reported on. Require at least one constrained end; reject
     at `_pcb_apply` with a named reason. For hyperedges with several
     constrained terminals (electrode + driver + test point) take an
     explicit name if the author gave one, else the first constrained
     terminal in a canonical order (sorted `(refdes, pin)`, documented in
     the skill) so the name cannot drift between runs.
- **The solve emits a resolved netlist snapshot** — derived, regenerable,
  never hand-edited, exactly as `pcb_copper` relates to the sketch. DRC,
  gerber, `export.py::bom_csv`, the netlist export read that snapshot.
- **Purity vs stability.** A stability bias (do not permute 64 channels on
  a 0.1 mm nudge) would make the solve depend on history. Fix: the **prior
  assignment is an explicit input** to the solve, not hidden state — still
  a pure function, of one more argument; "same inputs → same netlist"
  survives. Schema consequence: the prior is stored and versioned with the
  snapshot.
- **Not lazy in the force-once sense.** ERC topology checks and the BOM
  need a resolved netlist, so forcing happens at a defined pipeline step
  (`resolve`), not at arbitrary access. Closer to an incremental build
  than a thunk: the netlist has two phases, the pipeline has an explicit
  resolve step, and `content_hash` (`session.py::content_hash`) covers the
  intent, not the assignment.
- **Authoring win.** The LLM writes 53 client tags and one role
  declaration instead of 53 connections that can each be transposed. Most
  of the ERC risk on this board disappears because a connection you never
  spelled out cannot be mis-wired.
- **What survives: the firmware channel map.** Something outside the board
  must learn which channel drives which electrode. Design it as an
  **export**, not back-annotation: one-directional, derived, regenerated
  every solve, nobody edits it — a file drop, not a merge conflict. **This
  export does not exist today**; without it every swap produces a board
  nobody can program. Needful, and cheap.

### 1e. The solver is a matching, not an anneal move

Once the pool spans components, assignment is **min-cost bipartite
matching**, exactly solvable. 64 electrodes × 64 channels is 64! ≈ 10^89
assignments; `PIN_SWAP` as a random anneal move (`optimize.py`
`MoveKind.PIN_SWAP`, weight 0.2) samples a vanishing fraction. Hungarian
(Kuhn–Munkres) is O(n³): 64³ ≈ 2.6·10⁵ operations; ten drivers at 640
terminals ≈ 2.6·10⁸, seconds. **Run it before the maze, not sampled
during it.**

- Cost starts crude: Euclidean terminal→supplier distance plus a
  congestion penalty from the existing `cost.py::gap_capacity_term` /
  `escape.py::gap_capacity` machinery. Cheap enough to re-solve in a loop.
- **Rip-up-and-reassign** (distinct from rip-up-and-reroute): route; see
  which nets failed; re-solve with the winners frozen as warm start and the
  failures re-costed against actual congestion; re-route. The existing
  `propose_radial_assignment` (`pinswap.py`) becomes the warm start.
- Decide now: each terminal takes exactly one net = **perfect matching**.
  If electrodes ever share a channel (multiplexing) the problem becomes
  min-cost flow — different solver, same interface; do not pre-build it.
- **Hypothesis, not fact:** this should largely dissolve gr347037. Its
  2026-09-24 re-measure shows interior electrodes (rows 1–4, columns 2–6)
  failing while the perimeter routes — the signature of a bad assignment,
  not a capacity wall. Test after Slice 1 restores an honest metric.

### 1f. No schematic/layout split — one continuous persisted context

Reto: "The schematic vs layout split is for dimensionally limited humans."
The process is iterative and persisted: add a part, place it, route some,
move something, add more parts, route more; DRC and ERC advise
continuously. **ERC is not a gate before routing.** The repo already
agrees: there is no schematic as source of truth (the netlist is authored
directly at L0 — `pcb-usb-c-pd-nano-testboard.md` says so), and
`pcb/schematic.py` is a renderer over the same design rows ("Renders the
netlist the way service manuals do when nobody hand-placed a schematic"),
a view not an input.

### 1g. One check surface, not two

Reto asked: "drc, erc separate call or one list with filter?" **One list**,
`view='check'`, filterable on `rule=` / `severity=` / `phase=`; `view='drc'`
stays as a preset filter for continuity.

- The DRC/ERC split IS the schematic/layout split wearing another hat.
  Deleting the division upstream and keeping it in the checker moves it,
  not removes it.
- **The DRC list already contains ERC-flavoured rules.** `drc.py`'s rule
  set is `annular_ring board_edge_clearance clearance connectivity
  courtyard_overlap npth_clearance octilinear outline_containment
  silk_edge_clearance silk_missing silk_printability synthesized_footprint
  trace_width unrouted via_pad_keepout via_via_keepout`. `connectivity`
  ("this net's copper is in 3 pieces", `drc.py::check_connectivity` over
  `connectivity.py`) and `unrouted` ("the netlist says connect, the copper
  doesn't", `drc.py::check_unrouted` reading `pcb_route_status`) are
  neither purely geometric nor purely electrical. Drawing the line now
  means moving them out — churn to create an unwanted boundary.
- Findings already share a shape: pcb `{severity, rule, where, margin_mm,
  detail}` (`PcbHandler._render_drc`), se `{severity, rule, subject,
  detail}`. An ERC finding is the same row with `margin_mm` empty.
- Counter-argument, stated honestly: netlist checks are cheap, realised-
  copper checks are not, and nobody wants full DRC on every "did I wire
  that right". That is a **filter** concern, not an interface concern —
  the caller names the phase and the rest is skipped. Do not split the
  surface to solve a performance problem.
- **Phase-awareness.** `unrouted` while still adding parts is a TODO, not
  an error. Without a phase dimension the check is noise through exactly
  the middle of the process. Same two-tier machinery as `jlc_min` /
  `house_default` (`drc.py::_two_tier`), one more axis: a finding's
  severity is a function of `(rule, phase)`.
- **Delta reporting** (`new / still / resolved`) depends on
  `finding-stable-identity.md` — hard dependency. `pcb_drc_findings`
  persists rows per `run_id` but has no identity column; the layer can
  only say which complaints are new once findings persist with identity.

### 1g-bis. A fourth oracle obligation: an approximation's soundness must match the predicate's polarity

`draft:llm-physical-grounding` states the external-checker contract as three
obligations — **total**, **separable**, **diagnostic** (`dc4109165`): total,
"because any expressible state the checker cannot judge is a hole the search
will find"; separable, so feasibility is never traded against an objective;
diagnostic, so a rejection gives the model something to revise.

Every defect in Slices 1-4 satisfies all three and is still wrong. The checker
judged every state, judged it independently of quality, and said exactly where.
It just said the wrong thing.

**Don't coin a name for this; it is sound over- vs under-approximation** from
abstract interpretation (Cousot & Cousot 1977, `pa449839`, DOI
10.1145/512950.512973). An over-approximation is sound only
for proving a property ABSENT; an under-approximation only for proving it
PRESENT. Our bugs are the two halves of exactly that:

| check | real pad | approximation | sound for | predicate asserts | result |
|---|---|---|---|---|---|
| `check_via_pad_keepout` (gr346004) | 1x2 mm rect | circumscribed disc — OVER | "definitely no overlap" | overlap | false positive |
| `check_outline_containment` (gr449709) | same | same disc — OVER | "definitely no overlap" | overlap | false positive |
| `connectivity._pad_primitives` (gr449483) | polygon | inscribed disk r=min(w,h)/2 — UNDER | "definitely touching" | not touching | false negative |

So the defect is **not that an approximation was used**. It is that the
approximation's soundness direction is opposite to the polarity of the
predicate consuming it.

Three sources, three different jobs — keeping them separate matters, because
each of the collapsed versions over-credits in one direction:

- **`pa449839` (Cousot & Cousot 1977)** — cite for the *framework*: Galois
  connections, fixpoints, soundness of an abstraction, and the discipline of
  naming a soundness direction at all. That discipline is the thing we were
  missing.
- **`pa449842` (Gottschalk, Lin & Manocha, OBBTree, SIGGRAPH 1996, DOI
  10.1145/237170.237244)** — cite for the *engineering vocabulary*: a bounding
  volume licenses conservative rejection only, never acceptance.
- **The geometric corollary itself — state it as a derivation and label it
  one.** "B contains S, therefore B-disjoint implies S-disjoint but not
  conversely" follows from plain monotonicity of the subset relation. It is
  more elementary than abstract interpretation; citing Cousot for it
  over-credits exactly as much as citing OBBTree for the theorem would
  under-credit. Bucket (c) in `precis-write-paper-help` — a logical step the
  reader checks in a line.

Interval arithmetic's outward rounding is the same corollary again, and gets
the same treatment.

Adjacent and separately citable: Shewchuk's robust geometric predicates
(`pa266379`, DOI 10.1007/PL00009321) — arithmetic round-off flipping a
predicate's SIGN rather than perturbing a magnitude. Same insight: what must be
correct is the sign of the decision, not the accuracy of the number. Ours is
the shape-approximation analogue.

#### The structural fix, not five point fixes

Five instances in a week in code that had the exact outline available means the
cheap-approximation habit is systemic. Point-fixing each one leaves the sixth.

**Have the primitive-extraction functions return the polarity alongside the
geometry — `exact` / `outer` / `inner` — and have each check assert the
polarity it requires.** That turns all five bugs into type errors instead of
silent verdict flips, and it makes the approximation visible at the interface
rather than buried in an implementation. An oracle that returns a bare boolean,
with its approximation hidden, cannot be audited by its caller and will
eventually be trusted in the unsound direction.

This supersedes the per-slice geometry fixes as the *preferred* remedy; the
slices stay as the shortest path to an honest board, but Slice 2's pad-geometry
work should land the polarity tag rather than a fourth ad-hoc pad path.

#### Consequences for the agent and for tests

**A wrong checker corrupts the agent, not just the report.** Three conclusions
in one session were inverted by trusting checker output as ground truth: a
"short" that was the reservoir merge as designed, "13 disconnected nets" that
were connected, and a generator bug filed against geometry that was exact. The
draft's fidelity-adequacy invariant (`dc4109032`) tells the agent to hold a
validity domain for each *method it selects*; nothing told it to hold one for
the *oracle it stands on*. An agent that cannot distrust its instruments will
file defects against reality.

**A test written against a checker's output inherits the checker's fidelity.**
`tests/test_pcb_drc.py` had baked the buggy disc margin into an expected value,
so it pinned the defect rather than catching it — the approximation was
invisible at the interface, so there was nothing for the test to disagree with.
Tests for a geometric check must assert against geometry computed
independently: a hand-worked distance, not a recorded one.

**Practical rule for every slice below:** before fixing anything a check
reported, confirm the check measured the real shape. Re-measure first.

### 1h. The ERC rules that earn their keep

The classic pin-type conflict matrix (two `power_out` on a net,
`output`+`output`, `input` with no driver, unconnected non-NC pin) is
table stakes. Worth more on this board:

1. **Voltage.** A net's `working_voltage_v` exceeding any connected pin's
   `max_voltage_v` → error. Catches 20 V VBUS into the Pro Mini's RAW pin
   and a USB-serial bridge powered from VBUS — both written in
   `ewod-controller-and-hv-supply.md` as traps a human must remember.
2. **Rail crossing.** A net joining pins whose logic references two
   different supply rails with no declared translator → error. Catches the
   HV507 `V_IH = VDD − 0.9 V` problem structurally (ruling 12's own
   datasheet note; `SOURCE NEEDED` there stands here too).
3. **Decoupling.** Every IC `power_in` pin needs a capacitor on its rail
   within N mm (Reto named bypass caps explicitly). N is a rule parameter
   with a house default — `SOURCE NEEDED` for the default value.

### 1i. Pin model — the shared foundation

ERC and the role matcher both need pin semantics. Today `pcb_pins` carries
`pad`, `name`, `tags text[]` (migration `0047_pcb_kind.sql` lists the
vocabulary `input|output|bidir|passive|power|nc|analog|clock|data|gnd|…`),
`description`, `note`, `meta`; `_pcb_apply` writes `tags` from the `pins`
block — and **nothing reads them**: neither `ir.py` nor `drc.py` consults
`pcb_pins.tags`. So the storage exists and is inert. Build ONE extension on
it and land both consumers:

| field | values | home |
|---|---|---|
| `etype` | `power_in power_out input output bidi passive open_drain tristate nc` | `pcb_pins.tags` (closed vocabulary, one etype per pin; the existing free tags stay) |
| `max_voltage_v` | float | `pcb_pins.meta` |
| `vih` / `vil` | float, where the datasheet gives them | `pcb_pins.meta` |
| `rail` | name of the supply rail the pin's logic references | `pcb_pins.meta` — **not `domain`**: `pcb_nets.domain` already means `electrical|thermal` |
| `supplies` | `{role, blocks, within_block_swappable}` | `pcb_components.meta` (replaces `pin_swap_groups` there) |
| `client_of` | `{role, constraints?}` | per pin, `pcb_pins.meta` |
| `unused` / `must_terminate` | bool | `pcb_pins.meta` (§1j) |

Declared at part-authoring time — the moment the LLM is arguing with the
datasheet anyway. `op='footprint'` intake keeps caching `pin_map` names;
this adds semantics to those names.

### 1j. Not-used markers

Half exists: `ir.py::NO_NET = -1`, `PcbIR.pin_net` documented "NO_NET if
unconnected", and `realize._realize_maze`'s pad stamping already gives each
netless pin its own sentinel owner (`ir.n_nets + pid`, distinct per pin),
so a declared-but-unwired pad is already a routing obstacle with correct
clearance — no code needed there. What is missing is **intent**: no way to
say why a pin has no net, so nothing distinguishes "deliberately spare"
from "you forgot".

- **Markers and declare-all-pads are the same slice.** The HV507 has 80
  pads (`ewod-controller-and-hv-supply.md` Defect A: 77 named of 80, from
  memory — verify at intake) and this design wires ~56; declaring all pads
  without markers means ~24 intentional "unconnected pin" errors per sink,
  noise that trains you to ignore the check.
- **Two meanings, kept apart** (KiCad conflates them): part-level `nc`
  (datasheet: no internal connection) vs design-level `unused` (real pin,
  this board does not use it).
- **The marker that earns its keep is the inverse: part-level
  `must_terminate`** — pins that cannot float (CMOS inputs, /OE, /CS, the
  HV507's BL and POL — `SOURCE NEEDED`: datasheet). ERC errors when such a
  pin has no net **even if marked `unused`**. A classic dead-board bug no
  geometry check sees.
- **"Not used" never means "ignore for geometry".** An unused pad still has
  copper, takes paste, grows the courtyard, and next to a 250 V neighbour
  still needs full B4 spacing because it is floating, not grounded.
  Undeclared pads being invisible to DRC is the bug being fixed (Slice 2);
  marking them unused must not quietly reintroduce it.
- **`unused` means available to the matcher, not excluded.** Only `nc`
  means never. The ~24 spare channels per sink are free terminals and
  exactly the slack that makes a bijective matching easy instead of tight.

## In scope — slices (dependency order; each independently shippable)

### Slice 1 — `realized` tells the truth; the F.Cu neck reaches its electrode (gr449483, PRIO high)

**Measured on prod 2026-09-24**, route job 449188, `ran_on` the deployed
sha `8.34.0@1ae63c570bf6` — which contains every router fix (e311fdbf
polygon touch, 8b899ca1 bottom mirror, 0600f679 layer-aware router,
65f4e27f true-shape pad claims, af2da257 rulings 3/10/11). Design
`ewod-dogfood-2`, DRC run `b13770b5`, 132 errors / 162 warnings.

`view='route-status'` says **32 realized / 30 failed** (up from 3/59 on
2026-09-18). Cross-referenced against the same run's `connectivity`
findings, **13 of the 24 realized electrode nets have copper in 2 or 3
disconnected pieces**: ARR1_R0C2, R0C3, R0C6, R2C0, R2C5, R2C6, R3C0,
R5C0, R5C2, R5C5, R6C0, R6C2, R6C5. Only **11 of 53** electrode nets are
genuinely complete — the headline overstates the finished board ~2×.

Contract violated: `precis-pcb-route-help` — "A net only ever reads
`'realized'` when it is **actually clean** — no residual same-layer
crossing, no over-capacity gap." Connectivity is not in that list. The
status ladder in `workers/job_types/pcb_route.py` (module docstring + the
`problems = chord_crossings + congestion + unrouted_fail + unstitched_fail`
ladder) never asks whether the net's copper forms one component;
`drc.py::check_connectivity` asks separately and afterwards, which is why
the two views disagree.

Witness geometry: for ARR1_R2C0 (status realized) the pieces are
`(-5.500,-4.500)` on B.Cu and `(-7.000,-3.000)` on F.Cu; the B.Cu witness
coincides with the net's own plaza via, `via[ARR1_R2C0] @
(-5.499818827088606, -4.500181172911394)` (same run's `annular_ring`
finding). So via + ruling-11 B.Cu breakout form one component, the
electrode body another, and the F.Cu neck bridges neither.

**New observation from checking the list against the plaza rule
(`r % 3 == 1 and c % 3 == 1`): all 13 broken nets are DIAGONAL escapes;
no cardinal escape is among them.** (R0C2 is NE of plaza (1,1), R2C0 is
SW of it, R0C3 is NW of (1,4), … every one maps to a diagonal slot.)
Whatever the mechanism, it is specific to the diagonal neck.

**Hypothesis (code-anchored, not verified by running it).** The neck's
start is computed against the NOMINAL square, not the real outline. In the
plaza variant the anchor is `generators.py::_edge_anchor` — "an edge
midpoint for a cardinal direction, the pad's own corner for a diagonal
one", both at `layout.sizing["half"]` from the cell centre; the rim
variant has the same idea inline (via direction normalised × `half`). The
real pad is `_electrode_polygon`'s output: chamfered at plaza-adjacent
corners by `plaza_corner_chamfer` (`sqrt(2) * corridor_target − gap`, not a
small margin), crenellated to depth `tooth_depth`, with zero-deflection
margins widened at shared corners (`_needs_diagonal_margin_widen`).
Candidate mechanisms for the investigator, in order: (a) the diagonal
escaper's own corner is not chamfered (`_needs_plaza_corner_chamfer` needs
one flat wall; both of its walls are `mesh`) — so check whether the
polygon vertex actually sits at `(half, half)` after `_meshing_wall`'s
`_s_curve` rounding and any `corner_radius`; (b)
`connectivity.py::_prim_polygon_gap` evaluated for a capsule whose
centreline starts exactly on a polygon vertex (`point_in_polygon` on a
vertex is numerically ambiguous — a tangency, not an overlap); (c) a
frame or rounding mismatch between the copper row and the pad polygon.
`_stub_track_row`'s docstring asserts a constant-width track "needs no
taper of its own to stay clear" because the chamfer "already does that
clearance job on the ELECTRODE side" — the chamfer moving the boundary is
exactly what the anchor formula ignores.

**Fix direction.** (1) Anchor the neck by ray–polygon intersection against
the real `_electrode_polygon` ring along the via direction, then pull
inward by a small overlap (≥ half the track width) so the joint is an
overlap, never a tangency. (2) **Add connectivity to the realizer's own
`realized` predicate**: run `connectivity` over the net's realised +
fixed copper inside the route job and put a split net into `problems`
with `reason: "disconnected"` — the two views then cannot disagree. The
job already does this for planes (`RealizeResult.unstitched` → `problems`);
extend the same path to signal nets.

**Likely same root cause, fold in:** the `−0.025 mm` stub gap, previously
pinned as a single known residue (`pcb-pre-place-route-blocks.md` Slice 2
geometry residue; ruling 1's corridor re-solve was meant to remove it), is
now the dominant error class — dozens of `clearance` errors
`track[ARR1_RxCy] <-> pad[ARR1_<neighbour>] on F.Cu, -0.025, copper
clearance 0.065mm < JLC min 0.090mm`, roughly one pair per realized
electrode. It scaled with the realized count instead of staying fixed.
Investigate with the anchor.

**Repro:** `put(kind='pcb', id='ewod-dogfood-2',
args={'op':'route','iters':8000,'seed':1})`, then compare
`view='route-status'` against `view='drc'` connectivity findings net by
net. **Trap:** `op='route'` is idempotent per `(design, op, content-hash)`
and `session.py::content_hash` covers instances/nets/stackup/params, NOT
the code version — a re-submit with an unchanged design and the same
iters/seed collapses onto the PRIOR job and returns the old result even
across a deploy. Vary the seed.

*Accept:* on the `ewod-dogfood-2`-shaped fixture (`tests/test_pcb_ewod_dogfood.py`),
(a) every net `route-status` reports `realized` has exactly one connected
component in `connectivity.py` — asserted as a set identity between the
two views on the same run, not a count; (b) a fixture that deliberately
shortens a neck produces `failed (disconnected)`, never `realized` (test
the failure direction); (c) every diagonal-escape neck's start point lies
strictly inside its electrode polygon by ≥ `stub_width/2`; (d) the
`−0.025 mm` clearance class is either gone or re-pinned with a stated
reason; (e) prod: re-run with a new seed, `realized` count == connected
count.

### Slice 2 — every footprint pad is a declared pin, with intent markers (Defect A, gr339236/gr346744 class)

Reto: "ARR1_sink_0_0 does not have all the pins, a package should always
have all the pins"; from the render: "most on the bottom and some on the
right are missing."

**Root cause, verified in code:** in `generators.py::_expand_ewod_pad_array`
the sink's `pin_decls` is wire-driven — it appends the channel pins in this
sink's `share`, then `serial_in_pin`, `serial_out_pin`, the optional
`top_plate_pin`, and the `power` map's keys. Every other HV507 pin (CLK,
LE, BL, POL, DIR, C, HVGND, every unassigned HVOUTn) is never declared.
With ruling 2's `channels_per_sink` balancing, a partly-filled sink
declares fewer than 64 channel pins; `channels_per_sink` assigns a
contiguous run of HVOUTn, so the undeclared remainder maps to contiguous
package edges — the observed bottom/right clustering.

**Why it matters:** `realize.pads_for_ir` is "every placed **pin** as a
pad", so a pad with no declared pin is invisible to `check_clearance`, to
connectivity, and to the router's occupancy stamping. Same blindness class
as gr339236 and gr346744, each of which cost multiple debugging rounds.

**Fix:** `ewod-controller-and-hv-supply.md` Defect A / its Slice 1 has the
store-side completion worked out (declare every `pin_map` name via
`_pcb_apply` when the footprint is cached; `undeclared_pad` DRC rule as the
safety net; NC pins already flow `ir.py::from_graph → NO_NET →` sentinel
owner). **This slice takes that over and adds the intent markers from
§1j**, because shipping declare-all without markers makes ERC noise the day
it lands:

- `nc` (part-level, datasheet) — never a matcher target, never an ERC
  complaint.
- `unused` (design-level) — no ERC complaint for "unconnected", **still
  full geometry**, **available to the matcher**.
- `must_terminate` (part-level) — ERC error when netless, even if `unused`.
- Undeclared-and-unmarked pad with no net → `unconnected_pin` ERC finding
  (phase-aware: warn while `phase=netlist`, error at `phase=fab`).

**Known nuances to carry:** courtyards grow (`ir.py::instance_courtyard_polygon`
is the hull of pad outlines), so hand placements may need a look; the
HV507 has 12 distinct non-channel names on 13 non-channel named pads
(memory, verify at intake) and `session.py::_real_pin_offsets` is
first-wins per name, so the second same-named pad stays invisible even
after this fix — **decision needed** before this slice ships: key pins by
`(name, pad)` in the IR (the multi-pad-per-pin story of gr339236), or
declare the duplicate under `name#2` and record it in the
`undeclared_pad` text. Do not silently accept either.

*Accept:* on the ring-sink fixture (`tests/test_pcb_island_terminal_polygon.py`)
`len(pads_for_ir(...))` for the sink == its footprint's pad count; two
`unused` pads adjacent to a routed track produce a `clearance` finding
when the track is pushed through them (geometry survives the marker); a
`must_terminate` pin with no net errors even when `unused`; an `nc` pin
never appears in any check; `esp32c3_reference`/`motor_power_reference`
pinned DRC counts unchanged or explained by courtyard growth (sweep seeds
before touching constants); `ewod-controller-and-hv-supply.md` Slice 1 is
reduced to a pointer here in the same commit.

### Slice 3 — via-pad keepout measures the real pad (gr346004, PRIO high)

**Re-measured on prod 2026-09-24**, DRC run `b13770b5`, deployed sha as in
Slice 1:

```
error via_pad_keepout via[ARR1_R0C2] @ (-4.500,-5.500) <-> pad[ARR1_RESV] on F.Cu  -0.204
  via clears pad[ARR1_RESV] by -0.114mm, needs 0.090mm (JLC min trace_spacing_mm, 4layer)
error via_pad_keepout via[ARR1_R1C0] @ (-5.707,-5.000) <-> pad[ARR1_RESV] on F.Cu  -0.304
  via clears pad[ARR1_RESV] by -0.214mm, needs 0.090mm
```

Reto saw the same region in the prod web view as "2 bottom left ewod cells
that are fused somehow". Both offenders are RESV's immediate neighbours
(R0C2 diagonal, R1C0 cardinal); RESV (`pad_sizes` cells `[0,0],[0,1]`) is
the only merged pad on the board. The module's own rule is "merged pads
never cover a plaza (would short the 8 escape nets)".

**Corrections from checking this against the code (the gripe's
"WORSE than filed" does not hold):**

1. The numbers are **byte-identical to the original filing** (DRC run
   `cc0f2ca9`): via coordinates, `-0.114`, `-0.214`. The `-0.204/-0.304`
   are `margin_mm` = shortfall − the 0.090 requirement, not a new
   measurement. Nothing got worse.
2. **Identical via coordinates across a supposed 2.0 → 2.25 mm pitch
   change** (ruling 10) and a re-derived slot family (ruling 8) mean the
   prod design's fabric rows were **not re-expanded** — `pcb_generators`
   stores the expansion as rows at apply time. Before any prod fabric
   claim, read the design's `pcb_generators` canonical params and
   `view='capability'` sizing block. "The pitch increase did not help" is
   unsupported: it never reached this design.
3. **`drc.py::check_via_pad_keepout` models every pad as a circumscribed
   disc, `pr = max(w, h) / 2.0`** — its own docstring calls this "the
   pad-radius approximation ... a false positive on real geometry" and
   exempts only same-net FIXED vias for it. For the 1×2 merged RESV pad
   (w ≈ 2·pitch − gap + 2·tooth_depth ≈ 4.02 mm) the disc has radius
   ≈ 2.01 mm about the pad centre (−6, −7), which reaches 1.06 mm past
   the pad's short side into the plaza. Check: `dist((−6,−7),(−4.5,−5.5))
   = 2.121`, minus via radius 0.225, minus 2.010 → **−0.114** ✓;
   `dist((−6,−7),(−5.707,−5.0)) = 2.021 − 0.225 − 2.010` → **−0.214** ✓.
   Both findings reproduce exactly from the disc model. Against the
   actual rectangle the R0C2 via clears by ≈ +0.33 mm.
4. The "fused cells" **are the reservoir merge as designed**.

So D3 is very likely a **DRC measurement artefact, not a short** — no
copper touches. Still a defect: two false errors on every board with an
elongated pad next to a via, and the rule is the one that protects against
via-in-pad. The same disc appears in `check_outline_containment`
(buffer by `max(w, h) / 2.0`).

**Fix:** polygon-aware via–pad distance in `check_via_pad_keepout` (shapely
is already used by `check_clearance` for polygon pads; `connectivity.py`
uses the real polygon for `shape == 'polygon'` pads and an inscribed disc
otherwise). Then drop the same-net-fixed-via exemption if it is no longer
needed. Keep the merged-outline suspicion as the fallback only if the
polygon-aware check still fires.

*Accept:* the two RESV findings vanish with no geometry change; a via
moved onto RESV's copper still errors (failure direction, criterion 4 of
`pcb-ewod-multitile.md`); a 1×3 merge next to a plaza is clean; the
dogfood DRC count drops by exactly 2 with the rest unchanged.

### Slice 4 — side-aware ratsnest (gr449579, PRIO high)

Reto, prod web view: "the wire from J_INSTR to U_TEMP to R_BLEED has 2
vias, but only needs one."

**Root cause, verified in code and against this design's placement:**
`ratsnest.py::_mst_edges` is Prim's MST over placed member positions and
`Airwire.length` is pure 2D Euclidean (`geom.dist`) — no term for which
side a member sits on. Module docstring: "Per net we build a **minimum
spanning tree** over its placed members."

From the design TOC, all three are collinear on x = 20: `J_INSTR @20,5
top`, `U_TEMP @20,0 bottom`, `R_BLEED @20,-6 top`. U_TEMP sits between the
two top parts, so the Euclidean MST chains J_INSTR → U_TEMP → R_BLEED
(5 + 6 = 11 mm), crossing top → bottom → top = 2 vias. The via-minimal
tree is J_INSTR → R_BLEED direct (11 mm, all F.Cu, 0 vias) plus one spur to
U_TEMP (1 via) — identical wire length, half the vias. The MST cannot tell
them apart.

Not `maze.VIA_COST_MM` (3.0): that prices a layer change within one
segment's path, after the net is already decomposed into pin pairs. The
decomposition is upstream and is where the cost is incurred.

**Fix:** add a via-cost term to `_mst_edges`' weight when two members sit
on opposite sides (reuse `realize._side_layer`). The weight is then **not a
metric** — fine for Prim's, but say so in the docstring so nobody assumes
the triangle inequality later. Scope is kind-wide: any net with ≥ 3 pins
spanning both sides; worse with fanout. On this board GND (fanout 5) and
HV_RAIL (fanout 3) are the other candidates; GND is plane-assigned
(In1.Cu) and should dog-bone rather than route.

*Accept:* a three-member fixture with the collinear top/bottom/top layout
yields a tree with one side change; a same-side three-member net yields
the unchanged Euclidean MST; `test_pcb_ratsnest` pinned lengths unchanged
for single-side nets.

### Slice 5 — pin model + ERC rules on the shared check surface

Build §1i's fields (vocabulary validation in `_pcb_apply`; `ir.py` carries
`pin_etype`, `pin_max_voltage_v`, `pin_rail`), then the rules of §1h as
`check_*` functions emitting the existing `DrcFinding` shape with
`margin_mm=None`: `pin_conflict`, `no_driver`, `unconnected_pin`,
`over_voltage`, `rail_crossing`, `no_decoupling`. Net-level inputs:
`working_voltage_v` on `pcb_net_classes.rules` (already proposed by
`ewod-controller-and-hv-supply.md` Slice 4(c) — one key, two consumers) and
a `rail` name per power net. A declared translator is a component whose
pins carry two different `rail`s on purpose (`roles: ["level_shifter"]`).

*Accept:* a fixture wiring a 20 V net to a pin with `max_voltage_v: 12`
errors `over_voltage`; a 3.3 V-rail output driving a 5 V-rail input errors
`rail_crossing`, and the same with a `level_shifter` between them is
clean; an IC `power_in` with no capacitor on its rail within N mm warns
`no_decoupling`; every rule has a "cannot fire" path that says so
(fixture with no `etype` declared → `not-checked` note, never clean).

### Slice 6 — supply/client roles and the matching solver

- Schema: `supplies` on `pcb_components.meta`, `client_of` on
  `pcb_pins.meta`, a `pcb_roles` row per `(design, role)` carrying
  `voltage_v`, `fanout`; `pin_swap_groups` accepted as a deprecated alias
  that expands to a `supplies` block for one release.
- `resolve` step: build the bipartite graph (clients × supplier blocks,
  block sizes must match), cost = Euclidean + `gap_capacity_term` penalty,
  solve with Hungarian (scipy `linear_sum_assignment` is already in the
  dependency set via shapely/numpy? — verify; else a 200-line pure
  implementation), warm-started from the prior assignment.
- Unmatched clients → `unsupplied_client` finding (error), before routing.
- The route job runs `resolve` first, then the maze; on failure,
  rip-up-and-reassign with winners frozen, bounded by `route_passes`.
- Delete `MoveKind.PIN_SWAP` and `_gen_pin_swap` from `optimize.py` once
  the generator emits `supplies` instead of `pin_swap_groups`.

*Accept:* the dogfood generator emits one `supplies: {role: pad_driver,
blocks: [[HVOUT1],…,[HVOUT64]]}` per sink and `client_of: pad_driver` per
electrode, no `pin_swap_groups`; a 70-client / 64-supplier fixture reports
`6 pad_driver clients unsupplied` with zero routing; two sinks at
different `voltage_v` under one role name refuse to match across; the
matcher's assignment for the dogfood is at least as good as
`propose_radial_assignment`'s on total Euclidean cost; same inputs + same
prior → identical assignment (purity test).

### Slice 7 — lazy netlist: resolved snapshot, prior, firmware export

- `pcb_netlist_snapshots` (derived, per `resolve`): the effective
  `(pin, net)` set, the prior it was solved from, the `content_hash` of
  the intent. `_build_ir` reads the latest snapshot; `pcb_pin_swaps` is
  migrated into it and dropped (forward-only: a new migration, baseline
  regen via `scripts/bump`).
- `view='channel-map'` (format `csv|json`): client → supplier terminal per
  role, regenerated on every resolve, never stored as authored data. This
  is the firmware export that does not exist today.
- Net naming rule enforced (§1d): a connection with no constrained end is
  refused at `_pcb_apply` with the rule text; a multi-constrained net
  without an explicit name takes the canonical first terminal.
- `route-status` gains the three-state split: `unresolved` (no solve yet,
  reported as phase, not finding), `unmatched` (solve ran, client
  unbound — `unsupplied_client` finding), `unrouted` (bound, no copper —
  unchanged).

*Accept:* two resolves with identical intent and prior produce identical
snapshots; a 0.1 mm nudge with the prior supplied changes ≤ 1 assignment
on the dogfood; `view='channel-map'` lists 53 rows for `ewod-dogfood-2`
and changes only when the assignment changes; `pcb_pin_swaps` no longer
exists after the migration and every reader that consulted it now reads
the snapshot.

### Slice 8 — `view='check'`: one surface, phase, delta

- `view='check'` with `args={rule?, severity?, phase?}`; `view='drc'` =
  `check` filtered to the geometric rule set (preset, documented as such
  in `precis-pcb-help`).
- `phase ∈ {netlist, placed, routed, fab}` is a design attribute set by
  the caller (`op='phase'`) with a derived default (no placement → netlist;
  no copper → placed; …). Severity table per `(rule, phase)`: `unrouted`
  is `todo` before `routed`, `error` at `fab`; `unconnected_pin` warns
  before `fab`.
- Delta (`new / still / resolved`) via `finding_key` — blocked by
  `finding-stable-identity.md`; ship the filter and phase first, the delta
  when that lands (`blocked-by` is per-slice, not per-file).
- `se` inherits the same `check` rendering through the shared finding
  contract; no se-specific work here.

*Accept:* `view='drc'` output is byte-identical to today's for an
unchanged fixture; `view='check' args={'phase':'netlist'}` runs only the
netlist rules and finishes without touching `pcb_copper`; the same
finding reads `todo` at `phase=placed` and `error` at `phase=fab`; with
identity landed, an unchanged design run twice reports `0 new, N still,
0 resolved`.

### Slice 9 — per-net clearance in the maze grid (blocker for the HV board)

`realize._realize_maze` builds the occupancy grid with ONE clearance:
`clearance = max(config.clearance_mm, max(r.clearance_mm for r in
rules_by_net.values()))`. Add a 0.4 mm HV class and every net inflates to
0.4 mm; the 0.099 mm fabric becomes unroutable. Per-net clearance needs
the grid to carry a per-net dilation, not one global number.
`ewod-controller-and-hv-supply.md` Slice 4 owns the HV class end-to-end and
names this as its Blocker 1; this slice is that blocker's fix, listed here
because the matcher (Slice 6) costs against the same grid. Do not
duplicate the HV-class work; land the grid change here, the class there.

*Accept:* the two-net fixture from that Slice 4's acceptance (one 0.4 mm
class, one default net) routes the default net at fab clearance and keeps
0.4 mm around the HV net; the dogfood's realized count does not drop.

## Explicitly NOT in scope (follow-ons, with reasons)

- **Shove placement.** There is none today: `courtyard_overlap`
  (gr267456) is a graded cost term with a spatial grid for pair queries —
  it penalises overlap, nothing displaces a neighbour. Its absence is
  already a documented failure: `optimize.py` records a rigid group that
  "collides with whatever the shelf pack put in its way, so the group is
  frozen" (an authored ~12×18 mm group ending with 25–31
  `outline_containment` errors at every seed tried). It matters MORE for
  the controller/HV board (fixed assignment, parts genuinely must fit)
  than for the array (a good assignment avoids contention), and it is the
  cheaper of the two shoves: the annealer already has courtyard geometry
  and a pair-query grid — it needs a displacement move, not a new router.
  Own item; after Slice 6.
- **Track shove.** The router shoves vias only (`RealizeConfig.shove_vias`,
  `via_shove_radius_mm` 0.5 mm, `_shove_vias`); no track shove exists, so
  the concept is accepted but not generalised. Rip-up is by re-ordering
  ("Rip-up and retry, by re-ordering", `_realize_maze` docstring). The
  architectural blocker is stated there: occupancy cells "are stamped, not
  owned, so ripping one net out of a settled grid is not a cheap
  operation" — you cannot cheaply remove ONE net's claim, which is the
  primitive a shove/incremental router needs. Getting it means per-cell
  net ownership or a topological/geometric router instead of a grid maze.
  Last. gr347037's congestion diagnostic is the shove use case in the
  tool's own words ("a corridor wide enough for this net exists ... once
  every OTHER net's copper is cleared away, so this connection lost a race
  for it to a net that routed first") — but Slice 6 should make most of
  that demand go away first.
- **gr347037 itself** stays parked. Its 30/57 congestion measurement
  predates rulings 10/11; its own proposed remedy (radial breakout stubs)
  has shipped; its 2026-09-24 re-measure flipped the dominant mode to
  `no_path` (23) over `congestion` (7). Re-measure after Slice 1 restores
  an honest `realized`, then after Slice 6. Do not fix before.
- **Multiplexed channels (min-cost flow)** — see §1e; perfect matching
  only.
- **Role algebra / inheritance** — §1b: name + voltage (+ current), nothing
  more.
- **`pcb-argue-with-design.md`** (`status: ready`, high) is a
  click-a-pad-into-a-textbox UI. Reto has since called that an old hat
  ("we throw out old hats that are useful to humans only"): the ARGUING is
  wanted, the clicking is not. What it actually needs is **stable quotable
  handles** so an LLM can say "R3C4's plaza via" and have it resolve — the
  same identity work as `finding-stable-identity.md`, on design objects.
  **Flagged for rewrite-or-kill; not rewritten here.** What the rewrite
  must target (Reto, 2026-09-24):
  - **Reuse the precis selector form `kind:identifier[~selector]`**, not a
    new grammar — `link()` takes it and the file kinds already address
    chunks/symbols as `id='slug~selector'`. Design-rooted and fully
    qualified: `pcb:ewod-dogfood-2~comp234/pin4/courtyard`, aspect
    (`courtyard`, `pad`, `net`, `keepout`) as the last segment so one pin
    can be pointed at unambiguously.
  - **Bidirectional resolution**: click → handle, handle → highlight.
  - **Durable vs derived referents.** Handles onto durable things (parts,
    pads, plaza slots, nets named after their constrained end) survive a
    re-solve; handles onto derived geometry (a track, regenerated every
    route) do not, and must degrade by resolving UPWARD to the durable
    owner — never dangle or error.
  - **Display**: the long handle shows while composing, then collapses to
    a readable label with the full handle on mouseover. Schema
    consequence: the handle **survives in the persisted message as
    structured data** (text with handle-bearing spans), not flattened to
    a string at submit — otherwise nothing to hang the mouseover on and
    nothing to re-resolve. The label is **derived at render time, never
    stored** (`comp234` is `U1` today, may be renamed tomorrow). Deleted
    referent → render as dangling with the raw handle visible.
- se back-port of the check surface beyond what the shared finding contract
  gives for free.

## Acceptance criteria (whole item)

1. On `ewod-dogfood-2` (prod, after deploy): `route-status` realized count
   == connectivity-clean count on the same run; the two RESV
   `via_pad_keepout` errors are gone; J_INSTR/U_TEMP/R_BLEED routes with
   one via; every HV507 pad is a declared pin and the unused ones raise no
   `unconnected_pin` finding while BL/POL without a net do.
2. The dogfood generator emits `supplies`/`client_of`, no
   `pin_swap_groups`; `pcb_pin_swaps` is gone; `view='channel-map'` exists
   and lists every electrode's channel.
3. A 70-electrode / 64-channel design reports `unsupplied_client` before
   any route job.
4. `view='check'` is the single surface; `view='drc'` is a documented
   preset of it; phase changes severity; delta reporting lands when
   `finding-stable-identity.md` does.
5. Reference fixtures `esp32c3_reference`, `motor_power_reference` stay
   green; pinned DRC counts change only with a stated reason.

## Target + blast radius

- Engine: `pcb/generators.py` (neck anchor, `supplies` emission,
  `pin_decls` completion), `pcb/drc.py` (polygon keepout, ERC rules, phase
  severity), `pcb/connectivity.py` (job-side use), `pcb/ratsnest.py`,
  `pcb/realize.py` (`_realize_maze` per-net clearance), `pcb/optimize.py`
  (delete `PIN_SWAP`), `pcb/pinswap.py` (warm start only, then retire),
  new `pcb/matching.py`, `pcb/ir.py` (pin semantics).
- Worker: `workers/job_types/pcb_route.py` (resolve step, `disconnected`
  reason, snapshot write).
- Store/schema: `_pcb_ops.py::_pcb_apply` (pin vocabulary, declare-all,
  net-naming rule), new migrations for `pcb_roles`,
  `pcb_netlist_snapshots`, `finding_key` on `pcb_drc_findings` (shared with
  `finding-stable-identity.md`), drop `pcb_pin_swaps`.
- Handler: `handlers/pcb.py` (`view='check'`, `view='channel-map'`,
  `op='phase'`, `_render_drc` becomes a preset).
- Skills: `precis-pcb-help`, `precis-pcb-route-help` (rewrite the
  `PIN_SWAP` section as roles/matching; the `realized` contract gains
  "one connected component"), `precis-pcb-ewod-help`.
- Sibling docs to trim to pointers in the same commits:
  `ewod-controller-and-hv-supply.md` Defect A / Slice 1 (→ Slice 2 here),
  its Slice 4 Blocker 1 (→ Slice 9 here); `pcb-ewod-multitile.md` ruling 6
  gets a "superseded by supply tagging" line.

## Open questions / decisions log

- **Decided 2026-09-24 (Reto):** constraint-vs-freedom is the axis; roles
  by supply/client tagging, no pool objects; roles thin (name + voltage);
  blocks-of-tuples for pairs, no general permutation groups; assignment
  derived (lazy), prior as explicit input; matching before the maze; one
  check surface with filters and a phase axis; ERC is advisory throughout,
  not a gate; `unused` ≠ `nc`, `must_terminate` is the marker that pays;
  unused pads keep full geometry and are matcher-available; firmware map
  is an export. D1 first, D4 second.
- **Open (Slice 2, blocker-severity for that slice only):** duplicate pin
  names on the HV507 — key by `(name, pad)` or suffix. Decide at build
  time against the cached `pin_map`; either is fine, silence is not.
- **Open (Slice 6):** Hungarian from scipy vs in-tree. Check whether scipy
  is already a runtime dependency before adding it for one function.
- **Open (Slice 8):** is `phase` authored (`op='phase'`) or purely
  derived? Start derived with an authored override; drop the override if
  nobody uses it.
- **Verify before trusting any prod fabric number:** the design's
  `pcb_generators` params vs the ruled defaults (pitch 2.25,
  `drive_voltage_v` 250) — Slice 3's identical-coordinate finding says
  the prod expansion predates rulings 8/10/11.
- `SOURCE NEEDED` markers in this file: HV507 `V_IH`, BL/POL
  must-terminate (Microchip datasheet, import as a `datasheet` ref);
  decoupling distance default N; HV507 pad/name counts (verify at intake).
