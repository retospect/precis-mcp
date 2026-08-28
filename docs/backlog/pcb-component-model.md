---
status: draft
title: Component model — Component / LandPattern / Instance, roles + capabilities, assumption ledger
prio: high
model: opus
---

# The component model

Design session 2026-08-28. Supersedes the "footprint" framing: a footprint
is one *part* of a component, not the whole of it. Companion to
`pcb-feature-model-vs-layer-films.md` (intent canonical, films derived) —
that item covers the geometry projection, this one covers what a component
*is*.

## The reframe

A footprint is not a picture. **It is a contract between a part and a
board**: copper here, no copper there, solder here and this much, this
volume is mine. Model the obligations, not the shapes.

## Three objects, not one

| object | grain | holds |
|---|---|---|
| **Component** | per LCSC | ratings, pin functions, pin capabilities, swap classes, provenance |
| **LandPattern** | per *package* | `Land` / `Hole` / `Body` / `Marking` |
| **Instance** | per design | refdes, x/y/rot, **board side**, fixed flags |

`part_footprints` is keyed by `lcsc` today, which is the wrong grain for
geometry: every 0402 caches its own byte-identical land pattern and a
correction to one propagates to none. Component facts genuinely are
per-LCSC. **Open question**: the join key. EasyEDA hands geometry out
per-part and package names are not normalized, so per-package keying needs
a normalization pass that does not exist. Do not split until that key is
real — a wrong merge is worse than duplication.

## Features

```
Land    {ident, shape, at, side, span, paste: full|windowpane|none}
Hole    {at, dia, plated, span}
Body    {extent_xy, height_mm, model_ref}      # courtyard DERIVED from this
Marking {role, anchor, height, side}           # slot; content from the design
                                               # extent computed, never stored
```

**`Land` and `Hole` are independent, not fused.** A THT pin is both at one
point; a solder-on nut is a hole plus a land (which **may have a net** —
a chassis-ground bond is normal); an NPTH mounting hole is a hole alone.
Today all three are "a pad with a drill" and `HOLE` primitives are dropped
at ingest, which is the only reason the nut ever looked like it needed
bespoke handling. It does not. It is a part.

**No `function` enum on `Land`.** It was considered and rejected. The
first argument for rejecting it ("mechanical is derivable — it is the land
with no net") is **wrong**, per the nut. The real reason is that **no
consumer needs it**: routing connects it if it has a net, DRC applies the
same bridging threshold, BOM/CPL place it as a part, width comes from the
net's current. The round-coordinate preference attaches to the *part*, not
the land. An unearned field is the drift generator.

**`paste` does the work `function` looked like it was doing.** A thermal
pad is not a category of land — it is a land whose paste must be a
windowpane grid or the part floats on molten solder. Store the treatment.

**`Body` subsumes courtyard**, but not trivially: courtyard is the body
*plus an assembly margin*, and the margin is a process choice (IPC-7351
density levels). Derive it with a density parameter; do not store both.

**`Marking` is a slot.** A footprint's marking has no content — the refdes
is not known until placement. The footprint supplies `{role, anchor,
height, side}`; the design supplies `content`. The **extent is computed**
from (content, height, rotation), never stored: a stored bbox and its
content are two representations of one fact. Computed, it is also the
UPPER-bound estimate the anneal needs (a clear bbox ⇒ genuinely clear),
with exact glyphs only at final DRC.

**`Keepout` is NOT in this list, and is NOT `add_via(net_id=NO_NET)`.**
See §"Vias are not keepouts" below — an earlier draft of this design got
that wrong.

## span

An interval over the stackup, **defaulting to `0..len(stackup)-1`** —
derived from the stackup, never a literal `[0, 3]` (that breaks on every
2-layer board, and 2-layer is a user call that is coming).

The reason to have `span` now is **not** blind/buried vias. It is that a
through-hole pad on a 4-layer board currently says `F.Cu` plus a drill,
and "goes through" is re-inferred downstream from `drill is not None` —
the same scalar-where-an-interval-belongs mistake that made every via
invisible to clearance DRC. It pays for itself on ordinary boards.

Blind/buried: **represent but reject.** `pcb_capabilities.json` has
exactly two process rows and zero mention of blind or buried, so a span
like `1..2` could be neither DRC'd nor priced and would sail through as
ordinary. Validation rejects any span not touching an outer layer.

**Keep one non-full-span fixture anyway.** Not a producer, not a move
class — a fixture. If span only ever takes its default, every
interval-overlap branch in clearance DRC becomes unreachable code that
passes by never running. That already bit once: the O(n²) oracle's own bug
was in shared-layer detection for *partially overlapping* spans.

## Schematic pins — already in the cache

`result.dataStr` (docType 2, the schematic symbol) arrives in the **same
document** `easyeda.fetch_component` already fetches, and `raw` retains
the whole doc. `parse_component` reads only `result.packageDetail.dataStr`
(docType 4). The module docstring's warning is about *confusing* the two
alphabets, not about the symbol being unavailable.

So functional pin names need **a parser, not a network call**, and are
backfillable over every cached row. Today `pin_map` sets `name` = the pad
*number*, which means "put the bypass cap near VDD/GND", escape routing by
pin function, and differential pairing are all keyed on data we do not
have. **This is the largest single gap between the system and a real
board** — larger than anything in the feature model itself. Do it first.

## Roles and capabilities — swappability without the lattice

**Swappability is a labelling, not a relation.** An equivalence relation
over pins is O(n²), has no natural boundary, and under enough firmware
hacking its transitive closure swallows the whole chip (an Arduino Nano's
pins are almost all almost anything). Enumerating it at ingest is the
YAGNI trap.

The part-level fact is **pin → set of roles it affords** (`D9:
{digital_io, pwm}`). O(pins × roles), bounded, and *literally what the
datasheet prints* — the alternate-function table. That makes the LLM's job
**table extraction**, the most reliable thing it does with a datasheet,
rather than unbounded reasoning with an uncheckable output.

The relation is then **derived at query time**: two pins are swappable for
a net iff both afford the role that net needs. Six wires in a design = six
capability lookups, not a lattice.

### Where the answer is captured

**At netlist binding.** The LLM connecting a net to a component is
*already* choosing a pin, with demand and capability both in hand.
Capturing the freedom costs nothing at that instant and is expensive to
reconstruct later.

- **Role of the net** → from whoever wired it. Free, reliable, no
  extraction. They know it is PWM.
- **Capability of the pin** → from the part. Extracted once, amortized.
- **Legal alternates** → derived on demand, **never stored**. A stored
  alternates list freezes a stale answer: correct the capability table and
  every cached list keeps the old one.

Two consequences: `PIN_SWAP` finally gets a legality source (it has **none
at all** today), and a netlist imported without role annotations simply
has no swap freedom — degrading to today's behaviour rather than guessing.

### A second kind of swap, free and unasked-for

**Inter-instance equivalence**: two 100 nF caps on the same rail are
interchangeable *with each other*. No part-level fact can express it; it
is computable from the netlist (same part type, identical net membership)
with **no LLM at all**. On a real board this is plausibly the larger
routing win — bypass caps and termination resistors are exactly what
clogs the area around a BGA.

### Default direction

**Nothing swaps unless unlocked.** Assuming not-swappable is merely
suboptimal; assuming swappable when it is not produces a board that
manufactures perfectly and does not work. The pass only ever *unlocks*,
with provenance, and low confidence stays locked.

Same-functional-name is a swap **candidate**, not a guarantee — two pins
named `GND` on a power IC can be analog and power ground, internally
separate. Which is the right shape: the LLM *confirms candidates* rather
than enumerating possibilities.

## Power: ratings, defaults, and the assumption ledger

Power flows **through the net, not the part**. A part contributes a
*bound* (iMax, Vrange); the net carries the *value* (`net_current_a`);
width derives from that via IPC-2221.

**Ratings' primary use is validation, not defaulting.** A declared net
current above a series part's iMax is a design error nothing currently
catches. Same for working voltage against Vrange. Unambiguous, cheap, no
inference about current paths required.

### The default direction rule

**Default toward the failure that is loud at design time.**

- Too low → thin trace → fabricates fine, works on the bench, **fuses in
  the field**. Silent, late, dangerous.
- Too high → wide trace → does not route, or is bulky. **Loud, now.**

So: **declared `net_current_a` always wins; absent it, MAX over
series-capable pins.**

**`min(iMax)` over the net's pins is tempting and UNSAFE.** It looks
tighter and still safe — a 10 A MOSFET feeding a 0.5 A connector cannot
exceed 0.5 A. But a 100 kΩ pull-up on a 3 A rail is rated ~50 mA and would
drag the estimate to 50 mA, deriving a **dangerously thin** trace for a
3 A net. What separates them is whether a pin is *in series* or *taps* the
net, which is not derivable from the netlist. That is the same shunt-device
exception that governs topological order, and it is a good per-pin LLM
annotation: `current_role: series | shunt | signal`.

### When to prompt — computable, not a judgement call

Measured 2026-08-28 via `rules.ipc2221_track_width_mm`:

| current | outer ΔT10 | outer ΔT20 | inner ΔT10 |
|---|---|---|---|
| 5 mA | 0.000 mm | — | 0.001 mm |
| 100 mA | 0.013 mm | 0.008 mm | 0.033 mm |
| 1 A | 0.300 mm | 0.197 mm | 0.781 mm |
| 10 A | **7.194 mm** | 4.724 mm | **18.715 mm** |

A 3.3 V pin at 5 mA with a bypass needs no prompt, and the reason is
exact: the IPC width is 0.000 mm against a 0.10 mm fab floor, so the
assumption **cannot change the geometry**. The floor dominates below
roughly **0.45 A** on an outer layer.

**Rule: prompt iff the assumption is load-bearing** — iff the derived
width exceeds the fab minimum. Same reachability discipline we apply to
cost terms, pointed at prompts. Testable: assert no prompt fires when the
derived width equals the floor. It also solves the noise problem, which
would otherwise kill the feature — an LLM hinted at on every 0402 stops
reading hints.

### Two moments, different content

Add-time knows the rating but not the consequence; route-time knows the
consequence but placement is done. Emit both, differently:

- **On add**: "Q1 is rated 10 A. Unless you declare the actual current,
  nets on its power pins will be sized for 10 A."
- **In the digest**: "VBUS sized from a part rating, not a declared
  current: 7.19 mm on F.Cu. Declaring 3 A gives 1.37 mm; accepting
  ΔT 20 °C instead of 10 °C gives 0.90 mm."

The second earns its space — it quotes the number you would act on.

### ΔT is a hidden third parameter

`ipc2221_track_width_mm(..., temp_rise_c=10.0)` buries ΔT in a signature.
Declaring 3 A silently gets 3 A *at a 10 °C rise*, and accepting 20 °C is
worth a third of the width. A hint promising "you can reduce this" while
concealing one of the two dials is half honest. Name all three: current,
margin, ΔT.

### Store the assumptions, do not just print them

Printing is where assumptions go to be forgotten. Record each on the
design with provenance (`declared` vs `assumed_from_rating`) so the digest
can say *"9 nets sized from part ratings, 3 from declared currents"* — how
much of the board is real, at a glance. Also gives the paper a checkable
claim rather than a stylistic one.

### Where the pattern must NOT be applied

Some wrong assumptions yield a board that manufactures cleanly, passes
every check we have, and is scrap: **pin 1 orientation and polarity**.
No default. Refuse and ask. State this explicitly — assume-loudly is
seductive enough to spread where it must not.

## Vias are not keepouts

`ir.py`'s docstring claims keepouts and vias are one primitive (a
layer-masked obstacle region; a via carries a net, a keepout has
`NO_NET`). **That unification is wrong as a domain model.** They share
exactly one thing — the spatial query "does this layer-masked region
overlap that one".

- A via is a **conductor**; a keepout is a **constraint**. One has a
  barrel, an annular ring and an ampacity; the other is not a thing.
- A via is **derived output** (the router makes it); a keepout is **given
  input** (enclosure, mechanics, footprint).
- A via is simultaneously obstacle *and* routing resource. A keepout is
  only ever an obstacle.
- The DRC rules do not overlap: annular ring, drill-to-copper and
  via-in-pad are meaningless for keepouts.
- `via_net == NO_NET` double-duties as "not a conductor" and "net
  unassigned" — and `NO_NET` already means the latter for pins in
  `unconnected_items()`. A sentinel overload waiting for its first caller.

Defensible version: share the obstacle **index** (it is the hot path);
do not share **identity**. Same features-vs-films split — a uniform
layer-masked region is a *lowered* form, right for the spatial engine,
wrong as canonical storage.

## The datum — cheap now, impossible to retrofit

`view='gerber'` warns: *"no outline feature — the board edge is the placed
parts bounding box."* So the board origin is **derived from wherever the
parts happen to sit** and moves whenever a part moves.

Nothing in an enclosure can ever align against that — and worse, it would
align *approximately*, which is the expensive kind of wrong. **Declare the
datum, do not infer it.** Same concern as the round-number coordinate
preference for mounting holes and connectors: both are about surviving the
handoff to CAD.

## Direction, not scope

### Firmware/board codesign

The firmware is the **executable oracle for the pin assignment**. DRC
checks that the board can be *made*; nothing checks that it can be
*configured*, and neither implies the other. Firmware that compiles and
resolves its peripheral allocation is a real running test of the netlist's
pin choices — better than any mux model we would write.

It also repairs the weakest link: a wrong alternate-function extraction
gives a plausible board that is wrong, **but if we claim D9 does hardware
PWM and it does not, the firmware fails to configure it.** Codesign turns
a silent extraction error into a loud compile error.

**Do not co-optimize.** Firmware costs are step functions (works / does
not; hardware / bit-banged); routing costs are continuous. Layer them:
firmware emits a **tiered feasible set**, routing optimizes within the
best tier and drops a tier only if it will not route. Terminates, simple.

**Peripheral mux exclusivity is deliberately NOT modelled.** General form
of the reason, worth keeping: *do not model a constraint you can arrange
to discover on the cheap side of an irreversible step.* Fab is
irreversible; firmware iteration is free. The caveat is a **sequencing**
requirement, not a modelling one — the firmware's pin configuration must
exist *before* the board is ordered, or the discovery lands on the wrong
side and a timer conflict becomes a respin.

**One generated pinout artifact** that both netlist and firmware derive
from. "The LLM writes both consistently" is a discipline, and disciplines
decay; a board fabbed at revision N against firmware at N+1 that moved one
pin is a silent mismatch found with hardware in hand. This promotes
`pcb-pinout-view-and-connector-intake.md` from a convenience view to a
load-bearing artifact.

**"Ready to order" becomes checkable**: DRC clean, fab capabilities clean,
firmware builds, peripheral allocation resolves — a real gate in front of
the only irreversible step, rather than a vibe. Order placement already
requires explicit human confirmation; this is what is asserted before it.

### 3D / enclosure integration

The `cad` kind carrying semantic zones, locations and axes ("boss for PCB
mount", "opening for cable", "microfluidic heater zone", "transillumination
LED location") is the same pattern one level up. A boss and its matching
board hole are **one fact with two projections** — *these parts are
fastened here* — not two coordinates that must agree.

The naming is the payoff, and it is the 3D version of functional pin names
replacing pad numbers: unnamed geometry cannot carry a constraint, a named
affordance can. "The heater zone must not overlap the LED window" is
checkable and wrong-able; `(x=12.4, y=8.1)` supports neither.

## The principle underneath all of it

**Where two artifacts must agree, model the agreement — not the two
sides.** Instances in this session alone: sketch canonical / copper
derived; intent canonical / films derived; one pinout source for firmware
and netlist; one interface for enclosure and board; capability + role /
alternates derived. The failure is always the same, and it is the
generator behind five of this build's nine silent defects: **one rule
living in two places that drift.**

## Persisting this as skills — NOT YET

Most of this must be **code, not prose**. This build produced nine defects
that passed human review; a skill saying "vias cost area" is a suggestion,
a cost term that charges for area is a fact. Triage:

- **Enforce in code**: the load-bearing-prompt threshold, the assumption
  ledger, conservative defaults, ratings validation, one pinout source,
  the pin-1/polarity refusals.
- **Skill**: only what a runtime agent must *decide* and the system
  cannot — layer count, ΔT acceptance, confirming a swap candidate — plus
  the interaction protocol (that hints exist, what they mean, the exact
  call that narrows each).
- **Repo docs**: rationale, deleted on ship.

**Write no skills until the corresponding slice ships.** Almost all of
this describes a system that does not exist. A skill describing target
state tells agents to call things that are not there — exactly the
`precis-draft-help` failure of 2026-08-28, where stale "these chunks are
unreachable" guidance caused **two agents to correctly refuse work that
was possible**. A wrong skill does not degrade behaviour, it confidently
misdirects it.

Homes when the time comes (no new skill unless it has a distinct
`answers:` entry): `precis-pcb-help` for the assumption protocol,
`precis-part-select-help` for ratings and swap unlocking,
`precis-datasheet-help` for extraction discipline.

Standing rule: **a skill stating a limitation is updated in the same
commit that removes the limitation.**

## Sequencing

1. **Schematic-pin parser** (`docType 2`) — self-contained, backfillable
   over the existing cache, unblocks everything keyed on pin function.
2. **`inst_layer` in the IR** — see `pcb-missing-constraint-classes.md`;
   prerequisite for the second-side assembly cost.
3. Component/LandPattern/Instance split + features.
4. Roles/capabilities + the assumption ledger.
5. Films lowered at export (`pcb-feature-model-vs-layer-films.md`).

> **Consider for the paper.** The default-direction rule (default toward
> the loud failure), the load-bearing-prompt threshold (prompt iff the
> assumption changes the geometry), and "capability labelling, not
> equivalence relation" are all general beyond PCB. See
> `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.
