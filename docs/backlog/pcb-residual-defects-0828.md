---
status: draft
title: Residual pcb defects found 2026-08-28 (lifecycle walkthrough + SVG inspection)
prio: high
model: opus
---

# Residuals from the 2026-08-28 session

Found by *walking the lifecycle* of a footprint and of a via, and by
rendering an actual SVG and looking at it — not by reading code. Each is
the same family this build keeps producing: a correct-looking component
that is unreachable, or two components implementing one rule.

Being fixed separately (agent dispatched 2026-08-28): the `via_count`
cost/realize divergence — `_via_count` reads `ir.n_vias`, `add_via` has
zero callers, `realize._vias_for_track` emits the real ones. Not repeated
here.

## 1. `SIDE_FLIP` is not board side — and the IR cannot express board side

`seg_side` is documented as *"which side of an **obstacle** a connection
takes"* — a rubber-band routing concept. It has nothing to do with
populating the bottom of the board.

Meanwhile `PcbIR` has `inst_x`, `inst_y`, `inst_rot`, `inst_fixed_*`,
`inst_extended_part` — and **no per-instance board side at all**. The DB
has it (`pcb_instances.layer`), the store returns it, `padplace` reads it
and mirrors correctly for the gerbers. The optimizer's IR drops it.

Consequences:
- A bottom-side part exports correctly and is **invisible to placement**.
- The second-side assembly step-function cost (user, 2026-08-28: *"if you
  put anything on the other side, it's the cost, even just 1, so it's not
  per part"*) has nowhere to attach.
- **The trap**: whoever implements that cost will find `SIDE_FLIP` /
  `seg_side`, assume it is the hook, and wire an assembly penalty onto a
  routing variable. It would look right and be entirely wrong.

Fix: add `inst_layer` to the IR; rename or clearly re-document `seg_side`
so the collision cannot mislead. Note the DB column is `layer`, not
`side` — match it, do not introduce a third spelling.

## 2. Crossings are answered geometrically, not topologically

`cost._crossings` uses a **geometric sweep-line** over straight-line
segments at L3 (with a coarse fallback below). But the L2 combinatorial
embedding — which `ir.py`'s docstring insists is stored **explicitly** and
*"never, ever derived from L3 coordinates"* — is the actual topological
answer to "what does this wire cross".

So we store the authoritative representation and then consult the derived
one. Two representations of one fact; the usual outcome follows.

Not necessarily a bug today (the geometric count is real), but it means
the topological invariant the IR exists to protect is not what any cost
term reads. Decide deliberately: either the crossings term reads L2, or
the docstring's claim about why L2 is stored explicitly needs weakening.

## 3. SVG: drills are invisible

`svg.DEFAULT_INCLUDE` is `{outline, copper, pours, pads, vias, silk}` —
**no `drills`**. The model carries a `drills` list and `render_board`
ignores it. Verified by rendering: two solder-on nuts with 3.2 mm holes
render as solid discs.

On a through-hole board every hole silently vanishes from what we call a
publication-quality figure.

## 4. SVG: every figure is y-mirrored

`svg.py` documents the choice: *"Coordinate convention — deliberately NOT
flipped"*, model mm coordinates straight into SVG's y-down space, because
it keeps arc sweep-flag arithmetic and text placement trivially correct.

Verified by rendering: a part at y=5 draws at the **top** of the image.
That is a **mirror, not a rotation** — a reader cannot fix it by turning
the page, and silkscreen text will render upside-down once text lands.

The stated reason is real, but the cost is paid by every consumer of every
figure rather than once inside `_arc_flags`. Revisit before any figure
reaches the paper.

## 5. A layer change on a current-annotated net changes required width

Deliberately out of scope for the `via_count` fix; filed here so it is not
lost. Measured via `rules.ipc2221_track_width_mm`: 10 A needs 7.19 mm on
an outer layer and **18.72 mm on an inner one** (2.6×, because internal
copper is sandwiched in dielectric and cannot shed heat).

So moving a segment of a high-current net to an inner layer does not just
add a via — it triples the width that net requires, and nothing in the
cost function knows. Once `via_count` is honest, this is the next term:
the layer decision on an annotated net must be priced by the width it
implies.

## 6. `part_footprints.model_3d` has no producer and no consumer

The column exists in `0047_pcb_kind.sql` and the baseline schema, and
appears **nowhere else in the repo**. Meanwhile EasyEDA's 3D model
reference lives in the `SVGNODE` primitive, which `parse_component` skips.

Give it a producer when the `SVGNODE` lift lands (see
`pcb-component-model.md` §Features, `Body.model_ref`), or drop the column.
A schema field with neither end wired is indistinguishable from one that
works.

## 7. Ingest skips primitive classes silently

`parse_component` handles `PAD` and `TRACK`; `HOLE`, `TEXT`, `SVGNODE`,
`SOLIDREGION`, `CIRCLE`, `ARC` are skipped with a comment and no signal.

`HOLE` is NPTH — mounting holes. `TEXT` is the refdes anchor the label
constraint needs. `SVGNODE` is the 3D reference above. Dropping `HOLE`
silently is the entire reason the solder-on nut appeared to need special
modelling: its defining feature was discarded at ingest and nobody could
see it.

**Fail loudly on an unparsed primitive class** (or record it as an
explicit gap on the row). Skipping silently is how "tested but
unreachable" gets built.

## 9. TWO live cost functions — and measures are priced only by the demoted one

*Fable review, 2026-08-28. The largest defect in the build, and it is the
same generator as the other eight, at the largest possible grain.*

Verified by grep, not by reading prose:

- `measures` appears **0 times** in `cost.py` and **0 times** in
  `optimize.py`. Every hit is the English word "measured/measurement" in a
  docstring. It appears **25 times in `place.py`**.
- `place.autoplace` is the Slice-4 annealer with hardcoded
  `W_CROSS=100, W_LEN=1, W_MEASURE=10`. It **reads measures** and **has a
  wirelength term**.
- `cost.py`'s docstring says "ONE cost function" and "**No wirelength
  term** … Do not add one back."

So the two engines disagree on both the objective *and* what inputs
exist. Which runs depends on the verb: `op='place'` enqueues
`workers/job_types/pcb_place.py` → `optimize.py`+`cost.py` (**ignores
measures**); `_place_and_store` → `place.autoplace` (`handlers/pcb.py:873`,
**honours measures**) is reached only from the **Freerouting round-trip**,
the path we ourselves demoted.

Net effect: **every measure the agent states is invisible to the engine we
call the system.** `view='measures'` still evaluates and displays them, so
they look wired. `precis-measures-help` documents the `place.py` weights
as if they were the objective. This is "tested but unreachable" applied to
the entire intent-expression feature.

Fix direction: pick one engine. If `optimize.py` is the system, measures
must become registered cost terms reading `operands`, and `place.py`'s
objective is deleted or quarantined behind a loud deprecation — **not left
as a second opinion.** Do not design more cost terms before this is
settled; you would be extending the wrong one 50% of the time.

## 10. Nothing reads the L2 embedding — anywhere

Stronger than §2 above, which undersold it as "decide deliberately whether
the crossings term reads L2". Verified: `rotation_darts`, `rotation_index`
and `validate_embedding` have **zero production readers** — only `ir.py`
itself and tests. `ir.py`'s docstring calls the explicit embedding "the
invariant this module exists to protect"; the crossings answer is computed
from L3 geometry in `cost.py` and maintained geometrically in
`optimize.py`.

So it is the tenth member of the dead-plumbing family this document was
written to catalogue (`add_via`, `model_3d`, `SIDE_FLIP` at ~15% of the
move budget, drills unrendered, measures unpriced) — and the biggest one,
missed *while writing the list*.

**The one test that catches all of them.** `cost.py` already has the right
pattern: the registry-driven move-reachability property ("every registered
term must take two distinct values under random moves"). Generalize it —
every IR field, schema column and move kind must have a mechanically
asserted **producer and consumer**. That single test subsumes §§1, 3, 6, 9,
10 as a class. The docs kept proposing more model; the defect generator
was unwatched plumbing.

## 11. Margin aggregation: don't build `max + ε·mean` — it cannot be tested

This session proposed replacing the exact `max` over margin penalties with
`max + ε·mean`, plus a property test asserting it "orders like max". Fable
killed both halves; recording the kill so nobody rebuilds it.

- **The property test is unsatisfiable.** "For any two states with
  different maxima, `max + ε·mean` orders like `max`" is false for every
  ε > 0 — choose maxima differing by δ < ε·(mean difference) and the order
  flips. Only a lexicographic comparison satisfies it exactly, and SA needs
  a scalar energy for `exp(−Δ/T)`. Any honest generator finds the
  counterexample, so the test would end up quietly rigged.
- **Exact `max` already violates `optimize.py`'s own doctrine** — "every
  cost term must decompose into local contributions with an efficient
  delta". `max` does not decompose; when the argmax improves, the engine
  must rescan or heap the whole penalty list per move.
- **The standard answer is to schedule the smoothing, and the dial
  exists.** Log-sum-exp `risk = τ·ln Σ exp(pᵢ/τ)` with τ annealed down
  alongside the existing `schedule`: `max ≤ LSE ≤ max + τ·ln n`, gradient
  everywhere, exact max recovered as τ→0, O(1) delta via a running sum.
- **Stronger still — drop `max` and just sum the hardened penalties.**
  `hardened_penalty` is already superlinear with a schedule-sharpened
  barrier; summing convex per-constraint penalties under escalating
  hardness *is* the penalty method (and is what PathFinder-style negotiated
  congestion does for routing). At `schedule=1`, `hardened(0.99) ≈ 4.6` vs
  500 nets at 5% ≈ 1.25 — convexity makes the worst constraint govern at
  the hard end by itself, and early-schedule mean-sensitivity is not a bug,
  it is **the gradient the plateau lacks**. Then `Family` stops controlling
  aggregation entirely and only controls normalization (USD vs
  fraction-of-budget) — one whole mechanism deleted.

That last point follows from a principle already written down here:
*"convexity IS the schedule — one mechanism, not two."* `max` was the
second mechanism all along.

## 8. The JLC scope probe could not fail — fixed in the doc, verify the claim

The probe in `pcb-guided-place-route.md` had two bugs, both live-fixed
2026-08-28: `c.available()` on a property (raised `TypeError` before
probing anything), and — worse — `component_info` returns `None` both
when credentials are absent *and* when the part is not found, so with no
credentials it never reached the network and the probe printed
`RESULT: success`.

A diagnostic whose success path is reachable without doing the thing it
diagnoses. **Measured: credentials do not resolve on melchior** under a
bare ssh + `/opt/mcps/venv/bin/python` — but that is not proof the vault
is empty, because a bare `python -c` never calls `secrets.bind_store` and
has no DSN, so the DB vault was never consulted. A human with the web
service's `PRECIS_DATABASE_URL` must run the corrected probe.
