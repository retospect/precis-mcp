---
status: draft
title: view='pinout' + explicit signal↔pad capture for user-requested connectors
prio: high
model: opus
---

# `view='pinout'` and connector intake

> **Consider for the paper.** Several positions in this file are paper
> material — silkscreen legibility as a *placement* input, text as an IR
> primitive with strokes derived, and conflicts resolved by cost-curve
> shape rather than coefficient tuning. They are collected with their
> justifications in `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE
> PAPER. Keep that section in sync if the design here changes.

## The gap

When a user asks for a connector in prose — *"2x3 2.54 header, horizontal,
pin signals clockwise from top left a, b, c, f, e, d"* — the LLM must turn
that into `{refdes, pin}` netlist members. Today it does so **blind**:

`pcb_graph` surfaces `instances: [{refdes, x, y, layer, roles, label,
height_mm, n_pins}]` and `nets: members:[{refdes, pin}]`. There is a pin
*count* and pin *identifiers*, but **no pad geometry** — nothing says where
pad 3 physically is. The EasyEDA footprint parser already has pad
coordinates; they are simply never surfaced to a view.

There is also no ERC. So a mis-mapped connector routes cleanly, passes DRC,
and is discovered with a scope.

## Why prose cannot be the source of truth

A 2x3 header has at least two live numbering conventions — zig-zag
(1,3,5 / 2,4,6) and row-major (1,2,3 / 4,5,6). "Clockwise from top left"
matches **neither**: it runs a,b,c along the top and f,e,d back along the
bottom. An LLM that pattern-matches "2x3 header" onto a remembered
numbering silently swaps signals.

Consequences for the design:
- The **stored** form is explicit `signal → pad-number` pairs. Prose is an
  input to derive them, never the persisted representation.
- The derivation must be **checkable**, not trusted — hence the view below.
- "Horizontal" (right-angle) vs vertical selects a *different footprint*.
  It is a part-selection term, not a geometric modifier.
- **Mating orientation** — which way pin 1 faces relative to the board edge
  or enclosure — cannot be inferred from geometry. Capture it explicitly or
  connectors come out reversed.

## Proposal

1. **`view='pinout'`** on `kind='pcb'` (and ideally on `kind='part'` for a
   footprint not yet instantiated): render each pad as number, x, y, side,
   plus a small ASCII map laid out in true relative positions. Generated
   from the parsed footprint — never authored, never inferred.
2. **Echo-back on connector intake.** After mapping prose → pads, render
   the result spatially ("pad 1 top-left, pad 2 to its right, …") so a
   mismatch against the user's description is visible *before* routing.
3. **Store signal↔pad explicitly**, so the mapping is reviewable later and
   a future reader is not re-deriving it from remembered prose.

## Silkscreen labelling — mostly code, but one piece is missing

For a "user plugs wires in here" connector, the pads need labels on the
silk. Splitting what is derivable from what is not:

**Derivable (code).** Label *placement*: per pad, take its (x, y), pick an
outward direction away from the footprint centroid toward free space, place
text there. Deterministic given pad geometry — the same geometry
`view='pinout'` exposes.

**Not derivable.** What the label *says* (from the signal mapping) and
which physical end is "pin 1" for the human — answered by a **pin-1
marker** (dot / square pad / chamfered outline), the universal convention
and the thing that makes the connector unambiguous in the field.

**Missing: there is no text rendering.** The gerber writer's silkscreen
model is stroke polylines only — `{"width_mm", "segments":[{"shape":
"line"|"arc", ...}]}`. There is no text primitive, and Gerber has none in
normal fab practice either (text is emitted as strokes or polygons). So
labelling needs a **stroke font** (Hershey-style vector glyphs, or polygon
text) that does not exist yet. This is the real work item behind "is that
just code".

### Text is an IR primitive; strokes are derived (sketch-as-canonical)

Store **text**, render strokes at export — the same split as copper. A
primitive of roughly:

    Text{content, layer, anchor(x,y), rot, height_mm, stroke_w_mm,
         mirror, owner_refdes}

`mirror` is load-bearing: bottom-side silk must be mirrored or it reads
backwards on the finished board.

Storing baked strokes instead would break four things:
1. **The optimizer** — moving a stroke set is hundreds of segment
   recomputes per proposal; moving an anchor + rotation is O(1). The
   bounded-delta property the SA loop requires only holds for the
   primitive form.
2. **Collision cost** — the anneal wants a cheap bounding box, final DRC
   wants exact glyph outlines. A clear bbox is an **UPPER** bound (looks
   clear ⇒ is clear), so it is safe to anneal against and re-check
   exactly at the end. Declare the direction per §Cost's two-sided
   admissibility rule.
3. **Fab re-checking** — minimum silk line width and minimum text height
   come from the capability table. Text re-renders when the fab or copper
   weight changes; baked strokes cannot.
4. **Round-tripping** — stroke soup cannot be relabelled or restyled.

The gerber writer itself needs **no change** — its silkscreen path already
accepts `{width_mm, segments}`, so the font renderer feeds the existing
surface.

### Label placement is a move class, not a post-pass

Silk labels are objects in the joint optimizer, not a step after it:
rubber-band attraction to the owning part (the same per-connection
objective machinery as bypass caps), hard exclusion from pads, mask
openings, vias and component bodies, soft readability preference on
orientation. Cost decomposes locally with a bounded delta, so it drops
into the existing engine as TRANSLATE/ROTATE on label objects.

**Silk is just another layer index**, and a keepout is already a
layer-masked obstacle region — so "no text on vias" needs no new
primitive, just the existing obstacle machinery with the silk layer in
the mask.

Resistor arrays are the motivating case: dense parts whose labels compete
for the same free space, where greedy per-part placement collides and a
joint pass resolves it by nudging both.

**Labels are optimized SIMULTANEOUSLY with placement and routing** — in
the state from the start, not a late stage. Same argument that fused
place and route: staging creates decisions later stages cannot undo. If
labels arrive late, a part crammed into a corner has *already* lost its
label space, and recovering means moving the part. Label demand must be
able to push a component.

The via dependency is **not** an argument for lateness — it is the
progressive-enrichment pattern. A label does not need *realized* via
geometry, only the current level's fidelity: coarse bbox against
approximate via positions at L3, exact glyph outlines against realized
copper at L5, with the admissibility direction declared (a clear bbox is
an UPPER bound ⇒ safe to anneal against). Identical treatment to every
other term.

Staging survives only as a move-mix **schedule** — label moves may still
be weighted later in the temperature ramp for convergence — but that is
tuning, not architecture.

**Dependency:** "no text on vias" is unenforceable until via geometry
exists (gap 1 of the master spec) — no via is persisted today.

### Board identity block (name · revision · serial write-in area)

A small block carrying board name, revision/date, and a blank field for a
hand-written serial. Second consumer of the text primitive, and it forces
two capabilities the label work does not.

**Negative text is a polarity trick, not a font trick.** Gerber has
`%LPD*%` (dark) / `%LPC*%` (clear): draw the filled block dark, then draw
the glyph strokes in *clear* polarity to knock them out. The writer emits
G36/G37 regions already but has **no polarity support** — small addition,
far cheaper than boolean geometry. Verify LPC-on-silk renders correctly in
JLC's CAM before relying on it.

**Reversed silk needs a LARGER minimum than positive silk.** Thin cleared
strokes fill in during screen printing and the knockout closes up. Give it
its own capability-table entry; keep positive text the default and make
negative opt-in with the larger floor enforced.

**A serial number cannot live in the gerbers.** Every board off a panel
gets identical artwork, so a varying serial is impossible without
per-board variable data. Hence the block is two zones:
- *printed constants* — board name, revision, git sha
- *a deliberately blank filled-silk rectangle* — white silk takes pen ink,
  giving a human somewhere to write the serial.

**Prefer a revision identifier over a build date.** Gerbers are static, so
a "date" degrades to whenever artwork was generated. A short **git sha**
maps a board in hand back to the exact source that produced it — the same
traceability discipline the paper argues for, applied to a physical
artifact, at no cost.

**In the optimizer:** another text-like object. Hard requirement to exist,
very shallow position cost (it can go almost anywhere with room), hard
exclusion from pads and component bodies, preference for a face still
visible after assembly.

### The constraint is bidirectional, and it competes with the electrical objectives

A via keeps text out *and* text keeps vias out. One-way ("text avoids
vias") lets routing win every tie and squeezes labels somewhere useless.
Both objects want the same square millimetre, so it belongs in the cost
function on both sides.

That puts label-proximity in direct competition with the electrical
objectives — the bypass-cap loop wants the cap and its vias hugging the
pin; the label wants space by the same part. **Resolve this with curve
shape, not coefficient tuning:**

- **Loop/RCL cost: steep and narrow.** 0.5 mm of via displacement
  measurably raises loop inductance. Small displacement, big penalty.
- **Label cost: shallow and wide.** A label can slide, rotate, shrink to
  the fab minimum text height, move to a leader line, or degrade to an
  off-part legend. Many acceptable positions, each gently preferred.

Shaped that way the conflict resolves itself: the label flows around the
via because its penalty surface is nearly flat, while the via holds
station because its surface is not. No hand-tuned tiebreak. This is the
concrete answer to "do all these terms just get coefficient 1?" — the
asymmetry is *derived* (physics on one side, an available degrade path on
the other), not asserted.

The label's penalty is also **bounded** — worst case, drop it and put the
legend elsewhere. Electrical objectives are not bounded the same way, so
the priority ordering falls out rather than needing to be stated.

**Label importance is per-object.** A user-facing connector's label is
functional (someone is plugging a wire in); a resistor's refdes is
nice-to-have. Same annotation pattern as the per-connection electrical
objectives — the LLM sets it.

**Keep the hard/soft line crisp:**
- HARD (L5 DRC, fab-rejected): silk over a pad or mask opening.
- SOFT (cost): silk displaced by a via, label distance to owner.
- A via blocks silk on an outer layer **only if its span reaches that
  layer** — falls straight out of the `layers`/`span` data. A buried via
  does not interfere with silk at all.
- Whether *tented* vias block silk is a fab option → read it from the
  capability table, do not hardcode.

**Constraints the labeller must respect:**
- Silk over a pad or mask opening is clipped or rejected by the fab —
  labels must dodge both.
- Silk under a component body is invisible. Right-angle connectors make
  this acute: the label must land on the side visible with the part fitted.
- **A 2x3 header cannot carry six readable labels** — the inner row has
  nowhere to go. Real boards mark pin 1, number the corners, and put the
  legend elsewhere. The labeller needs a documented degrade path (end
  numbering + pin-1 marker + off-connector legend) rather than emitting
  overlapping mush.

## Sequencing

Build **after** the current per-net width/clearance and via-geometry gap
work lands — those touch `realize.py`/`drc.py`/`cost.py`, and this touches
`handlers/pcb.py` + `eyes.py`. Concurrent edits to the same files already
cost two cleanups this session; serialize.

Directly relevant to `pcb-usb-c-pd-nano-testboard.md`, which has a USB-C
receptacle and headers whose pad mapping must be right the first time.
