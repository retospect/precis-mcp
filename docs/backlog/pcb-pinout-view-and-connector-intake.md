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

Authored explicit signal-to-pad echo is already available through P1 before
routing. The remaining narrow gap is inspecting an **unsaved explicit proposal**
against an existing instance's stored geometry without an authoring put.
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
2. **Unsaved explicit intake echo:** proposed `view='pinout-preview'` below.
   P1 already echoes authored mappings; do not rebuild that workflow or infer
   mappings from prose. Preview adds a non-authoring inspection step.
3. **Store signal↔pad explicitly**, so the mapping is reviewable later and
   a future reader is not re-deriving it from remembered prose.

### Proposal 2 selected slice — unsaved explicit pinout preview (R13 authorized)

Premise checked at integrated main `04c13fd28998b9e2e10891a536da100a4890bfa3`;
deployed R12 `cdd1980c0731a2874bc824b64aecd765c9061045` is a separate anchor.
`PcbHandler.get` (pcb.py:711–733) dispatches P1 with no draft args;
`_render_pinout` (:4474–4518) loads stored neighbors and raw cached/local pads.
`eyes.pinout` (eyes.py:43–165) already provides spatial rows and provenance.
Thus `put(...components[].pins...)` then `get(...#J1, view='pinout')` already
checks **authored** input. That put changes state and can queue catalog-part
datasheets; it cannot supply a zero-authoring preview. Only the latter is new.

Reto via Claude R13 execution brief authorizes this bounded implementation,
superseding the earlier withdrawal/spec-only hold. Codex source review remains
required before merge; root owns version/full gate/exact deploy and subsequent
same-owner native/browser dogfood. No live authoring in this implementation turn.

**Selected API, not deployed:**

```python
get(kind='pcb', id='<existing-board>#<existing-REFDES>',
    view='pinout-preview',
    args={'pins': [{'name': 'CLK', 'pad': '2'},
                   {'name': 'DATA', 'pad': '3'},
                   {'name': 'UNKNOWN', 'pad': None}]})
```

Reuse the single-instance selector before legacy fragment dispatch, exactly
as P1 does. Missing/empty/multiple selectors or `@NET` give short BadInput with
the complete example; unknown board/refdes gives NotFound plus its supported
parent read. No alternate default view or fragment behavior changes. The
existing `get(..., view='pinout')` continues to report only persisted evidence.
Implementation seam is the handler's existing stored-footprint/instance
read path and pure row computation, not pcb_apply, IR, routing or providers.

`args` accepts only `pins`, required list of at most32 objects. Each object
accepts only `name` (nonempty stripped string, at most64 characters) and `pad`
(required exact nonempty string at most32 characters, or explicit null).
Reject malformed/extra keys with typed correction, not ignored args. Do not
infer pad numbers, coerce prose/numbers into numbering, or accept net/placement/
mating declarations here. `name` is an explicit proposed semantic pin/signal
label; it does not assign a net. Null pad means proposed binding unknown, not
NC/unconnected. Label NC does not establish electrical disconnection.

Read stored physical pads in original order and reuse authoritative
`padplace.place_pad_point` (padplace.py:194): mirror local X on bottom, rotate
CW, then translate. Local +X right/+Y up; board frame is top-view. Return raw
pad number/index, local and available board mm, side/rotation/layers, physical
duplicate indices and existing stored pin/net/mapping-source/geometry evidence.
Add separate proposed names and proposal-entry indices, provenance
`proposed-explicit` and proposal state. Never replace stored names/nets or
present a proposal as canonical/cache/vendor evidence. Preserve stored P1
ambiguity independently; disagreement with footprint naming or stored binding
is an observed difference, not a mapping decision or alias policy.

For proposed evidence: two names on one non-null pad or one name on different
pads is `conflicting`; echo all entries with a one-canonical-name/correct-pad
hint and say canonical authoring would refuse. Identical repeated pairs are
labelled repeated proposals, not extra canonical pins. Duplicate **physical**
pad numbers remain separate rows; one proposal appears on each matching row.
Null proposals are listed as unknown; nonexistent pad IDs are listed unmatched
with an explicit correct-pad hint. No winner, net merge, hidden metadata storage
or canonical-ownership relaxation. Canonical gr467885 refusal remains unchanged.

Missing/invalid stored pad geometry stays unavailable; keep proposal and
unknowns visible with an explicit manual authoring/cache-inspection hint. No
automatic fetch/footprint synthesis. Missing pose leaves board coordinates
unavailable, never origin. Stored side/rotation follow the existing transform;
unknown/missing pose is identified as such. Connector mating side/orientation,
vendor numbering correctness and proposed nets remain unknown even when the
stored instance has a known pose. Geometry is not a mating/numbering claim.

Bound response to one instance, at most64 raw physical rows and32 proposals;
no silent row/proposal truncation. Target maximum16,384 returned text characters;
overflow returns short actionable BadInput (use existing P1 for large stored
instances or reduce proposals). Heading must say unsaved/read-only proposal,
geometry provenance (design-local-authored or catalog-cache/source), and unknown
mating/net semantics. No SVG/browser renderer, font engine or new schema.

**Numbered acceptance and future native replay (no calls in this spec turn):**

1. Existing P1 authored echo remains supported and unchanged; preview works on
   a stored instance without authoring its proposed pins. Use an already
   provisioned labelled synthetic fixture; if absent, stop for fixture setup
   authorization rather than creating it during zero-write replay.
2. Exact asymmetric2x3 oracle below distinguishes row-major from zigzag with
   proposed VCC/CLK/DATA/GND/NC/AUX bound explicitly to pad1…6. Echo CLK.pad2
   at(0,1) for row-major and(-2,-1) for zigzag. Never choose a convention from
   prose or rename/reorder raw pad IDs.
3. Top J_TOP=(10,20),rot0; bottom J_BOTTOM=(30,40),rot90, B.Cu. Compare every
   raw row against the independent table. Unplaced pose retains local rows
   with unavailable board mm; mating orientation remains unknown throughout.
4. Unknown/null proposal, absent pad99, missing geometry and malformed selector
   show explicit unknown/unmatched/actionable hints, no provider/synthesis call.
5. Append a second physical pad6 at local(4,-1): preserve both pad6 rows and
   duplicate indices; bottom extra point=(29,44). Proposed CLK.pad2/OTHER.pad2
   and contradictory CLK.pad2/CLK.pad3 return all conflicting draft evidence,
   never canonical bindings. Existing stored-versus-footprint ambiguity remains
   separate. Identical proposals do not claim extra electrical identities.
6. Provenance separates proposed-explicit from stored explicit-pin-pad,
   footprint-pin-map and identity fallback; unknown proposed nets/NC are not
   inferred from a label. Stored rows and raw footprint source are unchanged.
7. Supported native board/legacy/P1 snapshots before/after identical preview
   calls are byte-equal; repeated preview is deterministic. Focused future
   read-only transaction/snapshot plus provider/enqueue tripwires prove zero
   authoring/provider/job side effects; native snapshots alone prove only
   exposed state, not hidden DB internals. No live job/provider query needed.
8. Validate input/output bounds and typed errors; ordinary board/#REFDES/@NET
   and P1 get behavior unchanged. Browser is optional display-only consumption
   of the same rows, no browser editor/physical claim or new browser scope.

| Physical row | Local(x,y) | Row-major pad | Zigzag pad | Top board(x,y) | Bottom board(x,y) |
|---|---|---|---|---|---|
| upper-left | (-2,1) | 1 | 1 | (8,21) | (31,38) |
| upper-middle | (0,1) | 2 | 3 | (10,21) | (31,40) |
| upper-right | (3,1) | 3 | 5 | (13,21) | (31,43) |
| lower-left | (-2,-1) | 4 | 2 | (8,19) | (29,38) |
| lower-middle | (0,-1) | 5 | 4 | (10,19) | (29,40) |
| lower-right | (3,-1) | 6 | 6 | (13,19) | (29,43) |

All values describe supplied synthetic stored geometry, not a vendor connector.
Scope excludes prose-to-pad inference, uninstantiated part intake, schema/alias
model, ERC, optimizer, provider/import, silk/font, placement/routing/manufacture
and gr467885 metadata-observability closure. Codex source review is required
before merge. No future view is ready merely because this spec exists.

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
