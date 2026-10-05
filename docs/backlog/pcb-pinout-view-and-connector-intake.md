---
status: draft
title: Connector intake, signal-to-pad capture and pinout extensions
prio: high
model: opus
pillar: 3d-design
---

# Connector intake and pinout extensions

> **Consider for the paper.** Several positions in this file are paper
> material — silkscreen legibility as a *placement* input, text as an IR
> primitive with strokes derived, and conflicts resolved by cost-curve
> shape rather than coefficient tuning. They are collected with their
> justifications in `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE
> PAPER. Keep that section in sync if the design here changes.

## The gap

### gr467885 — explicit pad ownership: atomic refusal (reviewed A)

At R11 `1b4d8b5c7d12b82cb476400bfe1f7d54df89226e`, an existing
`put(kind='pcb', id='<isolated-board>', args=...)` with one partless instance,
footprint pad 2 mapped to CLK, and declared CLK.pad='2'/OTHER.pad='2' accepts
both connections but persists OTHER.pad=NULL. A focused canonical regression
reproduces the native `dogfood-p1-pinout-v1#J_UNPLACED` loss at authoring,
database readback, legacy instance read and `view='pinout'`.

Cause: `_pcb_insert_pins` passes explicit pads unchanged to INSERT, but its
unqualified `ON CONFLICT DO NOTHING` silently skips OTHER against the active
unique `(component_id, pad)` index `pcb_pins_comp_pad_key` (migration 0047;
current baseline retains it). `_pcb_pin_id` then creates OTHER with NULL pad
when its connection is added. This is not failed footprint inference.

Reviewed contract A supersedes the earlier both-bindings acceptance: reject
conflicting canonical electrical declarations with actionable BadInput and
roll back the complete authoring operation. Identify refdes/component, pad,
both names and correction (one canonical name in pins/connections or correct
the erroneous pad). Reject contradictory same-name pad declarations too.
Validate supplied declarations before existing-refdes skip; valid re-put is
create-or-extend, never a pin editor/backfill. Identical name+pad declarations,
multiple NULL-pad names, lazy unbound pins and duplicate physical footprint
pad rows remain legal. Preserve both DB indexes as final authority, narrow
conflict suppression to name idempotency, and unwind SQL failures before
translation. Refusal rolls back refs/identifiers, title/meta, board, footprints,
earlier components/pins, nets/connections/cards and dependent writes; no
post-commit jobs/providers follow. No inference/read-side/schema change.

Alternative B (both canonical bindings) is deferred: routing/IR/fabrication
and import consumers depend on one canonical electrical pin per physical pad.
Aliases/conflicts need separate model and consumer design, not a weakened
index or metadata workaround. Do not choose a winner, merge nets, clear a
loser, edit sealed migration0047 or backfill historical damage.

Acceptance: actual DB snapshots for fresh and existing-board refusal (including
earlier valid writes/title/meta/footprints), direct store/supplied transactions,
reversed ownership/no connections/same-net conflicts, existing-refdes invalid
input, same-name contradiction, identical/idempotent and NULL/lazy controls,
and duplicate physical rows visible through pinout. Postdeploy use a fresh
labelled invalid synthetic request with typed refusal/no partial state plus
valid controls. Prior 23 P1 PASS stand; duplicate-canonical native ambiguity
remains unsupported/UNVERIFIED. gr467885 stays OPEN until deployed replay.

The shared store now validates normalized names/pads, including repeated
entries for one refdes, before each existing-refdes skip. SQL only suppresses
name-idempotency conflicts; the pad index remains authoritative and its
violation is translated after savepoint rollback. Mixed bound/unbound repeated
declarations for one name also refuse rather than silently choosing one.
Canonical regression: `tests/test_pcb_explicit_pad_retention.py`; original
loss/before-refusal logs remain historical in task `.scratch/pcb-pad-retention/`.
`inbox/pcb-pad-retention-ready.md` records focused validation and the deferred
deployed replay. gr467885 remains OPEN.

Connector intake still needs checkable signal-to-pad assignment from prose.
The per-instance `get(kind='pcb', id='<board>#<REFDES>', view='pinout')`
reads stored physical pad geometry, placement and mapping evidence; see
`src/precis/pcb/__init__.py` and `precis-pcb-help` for its current contract.
The remaining work below concerns intake and presentation extensions, not
rebuilding that view or changing the component model.

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

1. **Pinout extensions:** consider a part-kind view for an uninstantiated
   footprint and an optional spatial map. Keep parsed/authored provenance
   explicit; do not infer missing pad geometry or mating orientation.
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
