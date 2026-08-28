---
status: draft
title: The pcb MCP surface — what the agent sees, what it ought to see
prio: high
model: opus
---

# The agent-facing pcb surface

Session 2026-08-28, from a roleplay walkthrough (find part → datasheet →
bypass cap → LED + headers + USB-C → connections → outline → BOM/netlist
→ pin mounting hardware → place + route). Companion to
`pcb-component-model.md` (what a component *is*) — this item is about the
*interface*.

## What exists today (verified against `handlers/pcb.py` + `precis-pcb-help`)

**Author** — `put(kind='pcb', id=<slug>, args={...})`, batch, re-runnable
(re-`put` extends; existing refdes/net names are reused, not duplicated):

```
components:  [{refdes, label, part?, footprint?, pins:[{name, pad?, tags?,
              description?, note?}], x?, y?, rot?, layer?, fixed?, roles?, note?}]
nets:        [{name, class?, current?, width?, domain?}]
connections: [{net, refdes, pin, note?}]
measures:    [{metric, direction, goal, strength, weight, operands, reason}]
features:    [{ftype, geom, x?, y?}]      # 'outline', 'mounting_hole', ...
net_classes: {name: rules}
meta:        {...}
```

**Operate** — `put(args={'op': ...})`: `place`/`route` **enqueue** a worker
job (idempotent per design+op+content-hash, never inline — ~880 moves/s
means minutes per board); `move`/`rip`/`pin_side`/`plane_net`/
`class_rules` are cheap inline edits.

**Read** — `get()` lists; `get(id=slug)` is the netlist TOC (board/stackup,
parts, nets with fanout/class/I/width, net_classes, route-status summary);
`get(id='slug#U3')` is the **hop** (one instance: each pin → its net →
neighbour instances); `get(id='slug@NET')` is membership. Eyes:
`crossings | ratsnest | drc | trace | proximity | measures | feasibility |
route-status | congestion | planes`. Figures: `svg`. Exports: `bom | cpl |
netlist | dsn | mechanical | gerber`, plus `route` (demoted Freerouting).

**Board frame** (documented in the skill): origin at the **board-outline
corner**, +X right, +Y up, rotation **clockwise from north**, pivot = the
component centroid. Exporters convert per fab (JLC CPL flips to CCW).

### Corrections to earlier session claims

- **The outline is author-time, not an `op`** — `features: [{'ftype':
  'outline', 'geom': {'path': [...]}}]` in the same `put` as components.
  An earlier note in this session guessed there was no verb for it. Wrong.
- **`fixed` exists and is documented** (`'xy'` or `'both'`), explicitly
  "for connectors / mounting / status LEDs". Pinning screw nuts and header
  centres is supported today.
- **The datum convention IS stated** (origin at the outline corner). The
  defect in `pcb-residual-defects-0828.md` is narrower than first written:
  the convention exists but is **not enforced** — with no outline feature
  the exporter silently falls back to the placed-parts bounding box, so
  the origin moves when a part moves. Declare-and-validate, not invent.

## Gap 1 — the surface is entirely PULL

Every check requires the agent to know to ask. `put` answers "+5
component(s), +3 net(s)". It does **not** say: 3 pins are NC, 2 nets have
no declared current, U2 has no cached footprint, your outline cannot
contain the placed courtyards, or a measure's operands don't resolve.

An LLM that doesn't know to ask never finds out — and the agents least
likely to ask are the ones that most need telling.

**The single highest-value change: `put` returns its own critique.** Not a
new view — the *write response*, at the moment the agent is already
paying attention. This is the assume-loudly pattern from
`pcb-component-model.md` applied to the write path.

### What the write response should contain

**Delta + exceptions. Never full state.**

- **Delta** — what this call changed, bounded by what was sent. It is the
  *confirmation* that intent landed ("connected 5 pins: U1.VDD→VCC3V3, …").
- **Exceptions** — what is wrong *now*: NC pins, nets missing current,
  parts with no footprint, unresolved measure operands, outline conflicts.
  Naturally bounded, because a healthy design has none.

Why not full state: it is unbounded, it buries the signal, and it trains
the agent to skim. Why not delta alone: an agent's context may have been
compacted, so it cannot reconstruct absolute state from a series of
deltas. The exception list has a further virtue — **shrinking to empty is
a progress signal** an agent loop can act on.

### Reuse the read renderer — one renderer, scoped

The write response must be rendered by the **same** code the read views
use, restricted to the touched subgraph — identical shape whether the
agent wrote or inspected, and one place to fix.

**Caution**: share a renderer that takes a *scope* parameter, not two
entry points. Two entry points is how the write path forgets a filter the
read path has — exactly the `retired_at IS NULL` bug shape that already
hit `pcb_rip_route`/`pcb_copper_list`.

## Gap 2 — no per-pin electrical annotation

`current` is on the **net** (`nets: [{name, class?, current?, width?}]`).
Pins carry `{name, pad?, tags?, description?, note?}` — nothing
electrical. So a user request like *"pins 5,6,7,8,9: 5 V and 50 mA each"*
**has no home in the schema at all**.

And **voltage has no home anywhere**, not even on nets. That is why the
pairwise voltage-separation rule
(`pcb-missing-constraint-classes.md` §E-1) cannot be computed today:
`required_clearance(a,b)` needs `|V_a − V_b|` and neither operand exists.

Per-pin current is also what makes the **series vs shunt** distinction
expressible, which is what makes a tighter-than-max current bound safe
(see `pcb-component-model.md` §Power). So this one field unblocks three
things.

## Gap 3 — the outline is not validated against the parts

`features: [{'ftype':'outline', ...}]` is accepted unconditionally. A
3 × 6 mm outline on a design containing a USB-C receptacle (~9 mm wide
alone) is accepted silently. The failure surfaces at `view='feasibility'`
if the agent thinks to ask, at route time when placement will not
converge, or never.

Validate at write: outline vs. the union of placed courtyards, reported
as an exception in the write response.

## Gap 4 — NC pins are computed but not surfaced

`ir.unconnected_items()` flags every pin with `pin_net == NO_NET`. The
machinery exists; nothing pushes it. First exception to wire into Gap 1.

## Gap 5 — there is no removal path at the authoring surface

*Found by the Fable review; missed by the roleplay walkthrough that this
whole item is built on.*

Re-`put` **extends**: "existing refdes/net names are reused, not
duplicated". `delete` (`handlers/pcb.py:1163`) is **whole-design only**.
Between those two there is nothing.

So a wrong connection, a mistyped refdes, or an abandoned part is
**uncorrectable except by rebuilding the design from scratch** — and
mistyped refdes / wrong-pin connections are precisely the errors an LLM
author makes. The walkthrough above traces the entire authoring lifecycle
and never once tries to *undo* anything, which is exactly why the gap
survived a careful read.

Needs: `op='disconnect'` (net, refdes, pin), `op='remove'` (refdes / net),
and removal for `measures`/`features`. Idempotent, and reported through
the same delta+exception response as Gap 1.

## What already works well — do not redesign it

- **Measures are the right shape** for design intent: `{metric,
  direction, goal, strength, weight, operands, reason}` with `operands` a
  **list**, so "from U1.VDD through C1 to U2.VDD, minimise impedance" is a
  three-operand chain.
  **CORRECTION (Fable review, same day)** — an earlier draft of this line
  claimed `loop_inductance` is "a registered cost term reading it" and
  called measures "the best-supported part of the workflow". **Both false.**
  Verified: the string `measures` appears **zero** times in `cost.py` and
  `optimize.py` (every grep hit is the English word in prose) and **25
  times in `place.py`**. `loop_inductance_term` resolves its objective from
  `net_class`/`net_domain` via `objectives_for_connection` with
  `return_net=net_id` — a v1 placeholder naming each net its own return
  path. **No cost term reads a measure operand.** The shape is right; the
  wiring does not exist. See `pcb-residual-defects-0828.md` §9.
- **`#REFDES` as the hop** is the right traversal primitive — walk the
  design instance-by-instance rather than ingesting it whole.
- **`roles`** free tags (`sensitive`/`noisy`) driving class-based measures.
- The **enqueue-not-inline** split for place/route.

## Priority order

1. **`put` returns delta + exceptions**, rendered by the scoped read
   renderer (Gaps 1, 3, 4 all land here).
2. **Per-pin current/voltage + a net working-voltage field** (Gap 2 —
   unblocks pairwise clearance and series-vs-shunt).
3. Functional pin names (`pcb-component-model.md` §Schematic pins) — note
   this outranks everything below it but is filed there, not here.
4. A `snap`/granularity hint so round coordinates survive optimization.

> **Consider for the paper.** "Pull-only tool surfaces fail LLM drivers"
> generalizes: an agent that must know to ask never learns what it did not
> know to ask about. The write-response critique — delta plus a bounded
> exception list that shrinks toward empty — is a reusable interface
> pattern, not a PCB detail. See `pcb-paper-benchmark-selection.md`.
