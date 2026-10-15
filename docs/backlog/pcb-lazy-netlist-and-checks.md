---
status: ready
title: pcb — lazy netlist (supply/client roles, matching solver), one check surface (DRC+ERC), and fabric defects folded in as slices
prio: high
model: opus
pillar: 3d-design
---

# pcb — lazy netlist, one check surface, fabric defects

Design session Reto + agent, 2026-09-24, working `ewod-dogfood-2` through
route/DRC cycles on the prod web view. Part 1 is the model; the slices
follow from it. Gripes **gr449483, gr339236, gr346004** close against this
file and **gr347037** stays parked on it — each slice below carries its
measurement and repro, so this spec is the only record after closure.
(Each of those gripes carries a 2026-09-24 comment "folded into
`pcb-0042-implementation.md`"; that file does not mention any of them —
the comment is wrong, this file owns them.)

Hard dependency: the shipped finding ids (`precis.pcb.drc.finding_key`) (delta reporting in §1g).
Cross-referenced, not duplicated: `ewod-controller-and-hv-supply.md`
(Defect A, Slice 4 blockers, the RAW/VBUS and CH340N traps),
`pcb-ewod-multitile.md` (rulings 1–11), the shipped fabric-as-fixed-copper work
(`pcb_fixed_copper`, migration 0165; the known −0.025 mm residue).

## Motivation / why

Defects in one week on one board (gr346744, gr449483, gr449579 — the last since shipped) share
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
- **Evidence 2026-10-11 (gr347037; ruled 2026-10-12, Reto design-fold):**
  dogfood-6 failed 10/55 (7 no_path, 3 congestion at the right edge:
  R5C7, R6C7, R7C6). Offline replay on B.Cu: distance warm start 24
  failed, radial 33, none 33, different nets each time; opening In2.Cu
  leaves 4. The failure set tracks assignment and layer capacity, not a
  lost race, so this matching is the fix. **Do not patch routing order or
  retry depth for `no_path` nets** — it moves failures between nets and
  inflates the realized count (masking). Order/retry may be tried only on
  the congestion subset, as a measured experiment. ewod-pcb Do-next 0 (the
  unconstrained re-route) is the live test; the 2026-10-12 prod re-route
  on r24 left 4 failed (R1C5, R6C5, R7C2, R7C6, job 479966). Doc drift:
  5fc99982b dropped the plaza via B.Cu stub, but the old 30/57 → 10/58
  gain is still credited to it.

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
  the shipped finding ids (`precis.pcb.drc.finding_key`) — hard dependency. `pcb_drc_findings`
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
slices stay as the shortest path to an honest board, and the pad-geometry
discs named here are already closed (Slice 3, polygon-aware `_copper_item_polygon`
in both `check_via_pad_keepout` and `check_outline_containment`) — the
polarity tag remains the preferred shape for whatever approximation work
is left in the slices below. **Slice-3 closeout decision (2026-10-13):**
the same-net-fixed-via exemption STAYS — it is authored-copper policy
(a fixed via the author placed inside its own pad is a deliberate
via-in-pad, not a router accident), not a disc-approximation workaround
the polygon check made unnecessary.

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
  Undeclared pads being invisible to DRC was the bug Slice 2 fixed;
  marking them unused must not quietly reintroduce it.
- **`unused` means available to the matcher, not excluded.** Only `nc`
  means never. The ~24 spare channels per sink are free terminals and
  exactly the slack that makes a bijective matching easy instead of tight.

Slices 2, 5 and 8 landed 2026-10-13 (history of `bl-pcb-netlist-s23`;
behaviour documented where it lives: `_pcb_ops.py::_pcb_complete_footprint_pins`,
`drc.py` (`check_unconnected_pins`, `check_undeclared_pads`, the §1h
`check_*` ERC rules, `severity_at`/`PHASE_SEVERITY`), `handlers/pcb.py::
_render_check`, and skill `precis-pcb-check-help`; tests
`tests/test_pcb_declared_pins.py`, `tests/test_pcb_erc.py`,
`tests/test_pcb_check_view.py`).

**Open after Slice 2 — consumers that still read the PRIMARY pad alone:**
two pads sharing one name declare ONE pin; the second is that pin's
sibling (`ir.footprint_pad_set` claims it, `_sibling_pad_terminals_by_pin`
offers it to the router). Placement wirelength (`cost.py`),
`pinswap._instance_edges`, segment spans, and `layer_lock.layer_locked_pins`
(counts pads not pins, can call a sibling-served pin stranded) still read
the primary only — fold them in with the slice that first makes them bite.

## In scope — slices (dependency order; each independently shippable)

### Slice 1 — `realized` tells the truth; the F.Cu neck reaches its electrode (gr449483, PRIO high)

**Shipped (2026-10-11):** the route job asks `connectivity.net_islands`
(via `session.split_nets`, over the same quantized model the post-route
DRC gate judges) and a would-be-`realized` net whose copper is in more
than one piece lands `failed` with reason `disconnected`
(`workers/job_types/pcb_route.py`, `CODE_VERSION` 6). Tests: the
set-identity check in `tests/test_pcb_ewod_dogfood.py`, the shortened-copper
failure direction in `tests/workers/test_pcb_route.py`, and the dogfood-sizing
`−0.025 mm` clearance pin in `tests/test_pcb_ewod_generator_drc.py`. On
that fixture at seed=1: 37/54 escape nets realized, 17 split per
`view='drc'`, all 17 already failed for a named reason; the new rung fired
on 0 nets.

**The anchor hypothesis was wrong** (re-measured 2026-10-11 on the 8x8,
250 V, 2.25 mm fixture). All 25 diagonal and 30 cardinal neck starts sit on
the real `_electrode_polygon` ring (signed distance 0 to 4e-33 mm). The
F.Cu capsule overlaps its electrode by a quarter disc (0.00159 mm², w =
0.09 mm) on a diagonal and a half disc on a cardinal. The 13 "split" nets
were `connectivity._pad_primitives`' inscribed-disk false positive (fixed
aa861102, `test_diagonal_escape_neck_anchor_is_a_real_vertex_of_the_electrode_polygon`
pins the vertex).

**Open:**
- **Decision: pull the neck start inward?** Accept (c) asks for every
  diagonal neck start to sit strictly inside its polygon by ≥
  `stub_width/2`. The joint is an overlap today, not a tangency of
  copper. Pulling inward would change the geometry
  `test_diagonal_escape_neck_anchor_is_a_real_vertex_of_the_electrode_polygon`
  pins and bump the generator version. Not done: it is fab-margin
  hardening, not a fix for a measured defect.
- **(e) prod:** re-run `ewod-dogfood-2` with a new seed (`op='route'`
  dedups on `(design, op, content-hash, CODE_VERSION)`); `realized` count
  must equal the connected count in `view='drc'`.

### Slice 6 — supply/client roles and the matching solver

**Shipped 2026-10-15:** `pcb_roles` (`voltage_v`, `fanout`, authored on
`put`), `supplies` on component meta, `client_of` on pin decls;
`pcb/matching.py::solve` (scipy `linear_sum_assignment`, per role,
voltage-admissible pairs, prior as tie-break); the route job's
`_resolve_supply_matching` refuses to route on any unsupplied client
(`6 pad_driver clients unsupplied`) and otherwise applies the min-cost
Euclidean assignment over the wired pairs, cross-instance
(`PcbIR.permute_pin_nets`), persisted through `pcb_pin_swaps`. The EWOD
generator emits `supplies`/`client_of` (version 7); `pin_swap_groups` is a
deprecated alias for one release. `MoveKind.PIN_SWAP` and the
crossing-count matcher are gone.

**Open:**
- Cost is Euclidean only: add the `gap_capacity_term` congestion penalty
  (`solve(penalty=)` is the seam).
- Rip-up-and-reassign: on failed nets, re-solve with winners frozen,
  bounded by `route_passes`.
- Matching onto `unused` spare channels (a netless pin gaining a net)
  needs Slice 7's snapshot; today only wired supply pins permute.
- Blocks of size > 1 (differential pairs) raise; needs client tuples.
- Measure on dogfood (cluster `pcb_route`): matcher vs radial realized
  count on `ewod-dogfood-2`.

### Slice 7 — lazy netlist: resolved snapshot, prior, firmware export

**Shipped 2026-10-15:** `view='channel-map'` (`args.format` `csv|json`,
`matching.channel_map`), read off the swap-applied IR on every read, and
the `unsupplied_client` error on `view='check'` (`drc.check_unsupplied_client`,
the route job's capacity/voltage gate). `route-status` prints
`clients: N (a bound, b unresolved, c unmatched)` with the pins
(`PcbHandler._client_state_text`). Open below.

- `pcb_netlist_snapshots` (derived, per `resolve`): the effective
  `(pin, net)` set, the prior it was solved from, the `content_hash` of
  the intent. `_build_ir` reads the latest snapshot; `pcb_pin_swaps` is
  migrated into it and dropped (forward-only: a new migration, baseline
  regen via `scripts/bump`).
- Net naming rule: `_pcb_apply` refuses a touched net whose 2+ members are
  all supply pins, checked on every net whose membership the apply
  changed (shipped, `_pcb_check_free_ended_nets`). Left: `channel_map`
  reads a net carrying two same-role suppliers, or a multi-pin block's
  terminal, as unbound. The canonical default name has nothing to apply to: every net needs an
  explicit name today.

*Accept:* two resolves with identical intent and prior produce identical
snapshots; a 0.1 mm nudge with the prior supplied changes ≤ 1 assignment
on the dogfood; `view='channel-map'` lists 53 rows for `ewod-dogfood-2`
and changes only when the assignment changes; `pcb_pin_swaps` no longer
exists after the migration and every reader that consulted it now reads
the snapshot.

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
- **gr347037 itself** stays parked. Current state (2026-10-11,
  `ewod-dogfood-6` route-status): 10 of 58 nets failed — 7 `no_path`,
  3 `congestion` — down from the original 30/57. The radial breakout
  stubs (its own remedy) shipped. Remaining fix direction: route ordering
  and rip-up budget, maybe per-net clearance. Re-characterise the 7
  `no_path` after Slice 1 restores an honest `realized`, then after Slice 6.
  Do not fix before.
- **Multiplexed channels (min-cost flow)** — see §1e; perfect matching
  only.
- **Role algebra / inheritance** — §1b: name + voltage (+ current), nothing
  more.
- **`pcb-argue-with-design.md`** (`status: ready`, high) is a
  click-a-pad-into-a-textbox UI. Reto has since called that an old hat
  ("we throw out old hats that are useful to humans only"): the ARGUING is
  wanted, the clicking is not. What it actually needs is **stable quotable
  handles** so an LLM can say "R3C4's plaza via" and have it resolve — the
  same identity work as the shipped finding ids (`precis.pcb.drc.finding_key`), on design objects.
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
   the shipped finding ids (`precis.pcb.drc.finding_key`) does.
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
  the shipped finding ids (`precis.pcb.drc.finding_key`)), drop `pcb_pin_swaps`.
- Handler: `handlers/pcb.py` (`view='check'`, `view='channel-map'`,
  `op='phase'`, `_render_drc` becomes a preset).
- Skills: `precis-pcb-help`, `precis-pcb-route-help` (rewrite the
  `PIN_SWAP` section as roles/matching; the `realized` contract gains
  "one connected component"), `precis-pcb-ewod-help`.
- Sibling docs to trim to pointers in the same commits:
  `ewod-controller-and-hv-supply.md` Defect A / Slice 1 (→ Slice 2 here),
  its Slice 4 Blocker 1 (shipped as this doc's Slice 9: per-net clearance
  in `maze.OccupancyGrid`); `pcb-ewod-multitile.md` ruling 6
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
- **Decided 2026-10-13 (Slice 2):** duplicate pin names on the HV507 —
  one pin per NAME; the other same-named pads are that pin's siblings
  (`footprint_pad_set` claims them, the router offers them). No `(name,
  pad)` keying, no suffixing.
- **Open (Slice 6):** Hungarian from scipy vs in-tree. Check whether scipy
  is already a runtime dependency before adding it for one function.
- **Decided 2026-10-13 (Slice 8):** `phase` is both — `op='phase'` writes
  the authored claim to `pcb_boards.meta['phase']` (the only way `fab`
  exists), `derive_phase` is the default. `args.phase` on `view='check'`
  overrides either for a one-off look.
- **Verify before trusting any prod fabric number:** the design's
  `pcb_generators` params vs the ruled defaults (pitch 2.25,
  `drive_voltage_v` 250) — Slice 3's identical-coordinate finding says
  the prod expansion predates rulings 8/10/11.
- `SOURCE NEEDED` markers in this file: HV507 `V_IH`, BL/POL
  must-terminate (Microchip datasheet, import as a `datasheet` ref);
  decoupling distance default N; HV507 pad/name counts (verify at intake).

---

# Absorbed 2026-09-26

## The matrix was killed. Keep the diagnosis, drop the prescription.

_Grouped 2026-09-26; was `pcb-drc-keepout-matrix`, status draft, prio high._

**Audit 2026-09-26: item 2 shipped (drc.py check_via_via_keepout); open = item 1 canary pair-coverage test (slice 8, view='check'), soldermask_dam_mm/silk_width_mm still consumer-less, item 4 waits on pcb-engine-plan S9.**

**Original proposal (2026-08-29):** collapse six pairwise DRC rules into
one geometry adapter + a declarative `(class × class)` keep-out matrix with
a coverage test. **Reviewed by `fable` and rejected**, with code citations.
Recording the kill in full, because the idea is seductive and someone will
propose it again.

### The diagnosis was right and is worth keeping

Every DRC defect found 2026-08-29 was a **missing pair**: pad↔pour (every
pad shorted to the fill), pad↔track, pad↔via, pad↔pad, pad↔edge, via↔via
(two drills 0.16mm apart, 0.25mm wide). Six holes, same shape, none found
by review — all found by a human looking at a render. **Enumerability beats
vigilance.** That part stands.

### Why the collapse fails — six places the rules resist it

1. **The class list is a THIRD taxonomy.** The spec claimed alignment with
   settled decision 6 (`pad/hole/conductor/keepout/body/marking`) in the
   same sentence it proposed `copper/hole/silk/courtyard/edge` — which
   merges `pad` and `conductor`, renames `marking`, and invents `edge`.
   The merge is exactly where it breaks.
2. **"Same-net policy is per cell" is FALSE.** `check_via_pad_keepout` and
   `check_clearance` are both `copper × copper`, with opposite same-net
   policies and different tiering, selected by **subtype** pair. So cells
   must dispatch on subtype — i.e. the same six functions with extra
   indirection. `check_npth_clearance`'s plated filter splits `hole` the
   same way.
3. **"A drill spans the stack" is false.** `realize.py` emits blind/buried
   vias with real spans; two vias in disjoint spans may legitimately stack
   in x,y. The comparability predicate for holes is span-overlap.
4. **Silk is a phantom class** — zero silk rules in `drc.py`, silk is not
   in the DRC model, and avoidance is enforced at *construction* time in
   `silk.build_silk`. A silk row would duplicate a rule across two
   enforcement points: the exact drift disease this build keeps catching.
   Courtyards likewise arrive as a separate `courtyards=` argument, not off
   the model, and their exemption axis is **refdes**, not net.
5. **Emission arity and thresholds are per-rule.** `check_npth_clearance`
   emits one finding *per hole* (min over copper); clearance emits one per
   pair. Clearance's required gap is a **function of the pair's nets**
   resolved at check time, not a cap name. The edge cell's cap is chosen at
   runtime by `panel_type`.
6. **It would silently weaken the rule that is currently working.**
   `check_via_pad_keepout` uses a circumscribed circle **deliberately** —
   its own comment: overstating a pad costs a spurious finding a human
   sees; understating it hides a real via-on-pad. Unifying to the exact
   polygon yields *fewer* findings on the rule that just produced 55 of 57
   errors on the user's board. The spec's own acceptance criterion calls a
   drop in findings a regression, and its plan mandated one.

### The oracle argument was backwards

The spec claimed that generating the O(n²) oracle from the same table
means it "can only disagree about indexing, never scope — which is the only
thing an oracle is good for." Both halves are wrong. The oracle's real
historical catches (the `""`-layer via bucketing bug; its own
first-primitive layer bug) happened **because the two engines implement the
layer predicate independently**. Share the predicate and that entire class
of catch disappears — one implementation run twice.

Scope was never the oracle's job (the module docstring already says so).
**Coverage owns scope; an INDEPENDENT oracle owns geometry and indexing.**
The proposal weakened both legs and called it strengthening. Keep
`clearance_violations_naive` exactly as it is: no shapely, closed-form, its
own layer logic.

### Wrong week, structurally

Geometric DRC is currently **the only independent check on the maze
router's occupancy-grid guarantee**, the DRC baseline moved three times in
one day, and several agents are editing adjacent files. Rewriting the
verifier while everything it verifies is in motion makes a router
regression and a checker regression indistinguishable — and pins acceptance
against a baseline that is legitimately changing daily.

### DO THIS INSTEAD — the separable 20%

1. **Canary-fixture coverage test over the EXISTING functions.** No
   refactor. Enumerate the real pair universe **by subtype** —
   track / pour / pad / via-annulus / via-drill / NPTH / THT-drill / edge /
   courtyard — and for each pair either a minimal violating fixture
   asserting ≥1 finding of the right rule, or an explicit n/a **with a
   reason a reviewer can check**. "Two pads 0.01mm apart, different nets →
   expect a finding" would have caught **all six** of today's misses
   against the code as it already stood. This is where the whole value was;
   the collapse was never load-bearing for it.
   - Include the pair families the matrix itself forgot: **`soldermask_dam_mm`
     and `silk_width_mm` exist in `capabilities.py` with ZERO consumers in
     `src/precis/pcb/` — an entire pairwise rule family with no cell to be
     missing from.** Enumerating the wrong universe proves completeness of
     the wrong universe.
   - A test that passes the moment someone types `NA` is worse than no
     test: it converts absence of evidence into apparent evidence of
     absence, wearing a green tick.
2. **Ship via↔via as an ordinary `check_*` function** — and note it needs a
   **new capability field** (`hole_to_hole_mm`); `capabilities.FIELDS` has
   `drill_mm`, a minimum drill *size*, which is a different quantity. The
   matrix never removed this work.
3. **Keep the oracle independent.** Generate it from nothing.
4. **Revisit the geometry-class collapse only after engine-plan S9** (the
   vocabulary migration), so any DRC taxonomy is decision 6's enum rather
   than a third one — and evaluate the `via_pad_keepout` pad-shape question
   then, as its own measured decision, never as a rider.
