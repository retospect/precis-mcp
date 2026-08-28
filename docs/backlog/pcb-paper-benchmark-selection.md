---
status: draft
title: Benchmark selection for the topological place+route paper's evaluation section
prio: normal
model: opus
---

# Benchmark selection for the place+route paper

Queued background project. Source handoff: `~/benchmark.txt` (user's home,
outside this repo). Paper draft lives at
`docs/papers/topological-place-route/` in the **`pcb-paper` worktree** —
not here.

## Sequencing (user's order, 2026-08-28)

1. Build the **USB-C PD Nano test board** first
   (`pcb-usb-c-pd-nano-testboard.md`) — a real board before any benchmark.
2. **Argue about the gerbers** — user review of what the exporter actually
   produces. Expect this to change things; do not pick benchmarks first.
3. *Then* pick benchmarks from the candidates below.

Do not start step 3 early. The gerber argument is likely to move what is
worth measuring.

## Candidates

**Primary — PCBWorld / PCBWorld-Bench.** arXiv:2607.05915, Song et al.,
7 Jul 2026. Open KiCad-grounded interactive routing environment driven by
DRC feedback; 679 real open-source boards in native `.kicad_pcb`, three
board datasets, two controllable synthetic generators, eight
engine-checked metrics, unified protocol. Its own headline finding:
learning-based methods still lag rule-based routers, and interactive
routing beats one-shot planning.

**Secondary — UCSD PCB-Benchmarks.** `github.com/DAC-2020-Submission-1703/PCB-Benchmarks`.
11 sets (bm1–bm11) of manually placed-and-routed real manufactured boards
with locked components, outlines, courtyard polygons. Published metrics:
through-hole via count, required routing layers, laser via layers,
bounding-box area. A per-paper artifact, **not** a community standard —
weight it accordingly.

**Explicitly NOT for router-quality claims** — these are LLM *reasoning*
benchmarks and using them for router quality would be a category error:
PCB-Bench (ICLR 2026, OpenReview Q5QLu7XTWx), OmniRouting
(arXiv:2608.04434), OmniLayout (arXiv:2607.03261).

**There is no ISPD or ICCAD contest benchmark for PCB** — those contest
series are IC-only, every year 2012–2025. State the gap explicitly in the
paper. PCBWorld and PCB-Bench both open by asserting it, so saying it
ourselves is stronger than having a reviewer say it.

## Citations to VERIFY before planning around them

Both came from search snippets, not from the primary source. Confirm
before they reach a draft:
- PCBWorld's **KDD 2026 venue attribution** — the arXiv page does not state
  it. Also confirm the repo is live and the harness actually runs.
- The ASP-DAC 2021 **DOI** for Lin, Merrill, Wu, Holtz, Cheng, "A unified
  printed circuit board routing algorithm with complicated constraints and
  differential pairs."

## Three things that will bite

1. **Via metrics will read zero** — the realizer emits only tracks, so no
   via copper is persisted and every via DRC rule is unreachable. Report
   N/A, **never** as a favourable via count. ⚠ *This is actively being
   fixed* (see the master spec's gap work); if via geometry lands before
   the evaluation runs, this caveat is obsolete and real via numbers become
   reportable. Re-check status before writing the section.
2. **Ablations on `SIDE_FLIP` and `ROTATE` are vacuous** — both are
   provably cost-neutral (no registered term reads `seg_side` or
   `inst_rot`). `PIN_SWAP`'s effect is real and measured by its own
   crossing evaluator but is invisible to `total()`. Present none of the
   three as an optimizer lever until sub-instance pad geometry lands.
3. **Budget from measured throughput**: ~880 moves/s on a 30-component /
   49-segment synthetic board across all move classes ⇒ 1e5 moves ≈
   2 min/board, and a 679-board sweep ≈ 22 h serial. **Gate on move/pass
   count, not wall time** — wall time is flaky under sibling worktree load.

## CONSIDER FOR THE PAPER — claims from the 2026-08-28 design session

Captured here because the draft lives in the `pcb-paper` worktree and
these would otherwise be lost. Each is a design position taken
deliberately, with a reason; fold in the ones that earn their space.

**Cost-curve *shape* resolves conflicts; coefficients do not.** When two
objectives compete for the same area (a bypass cap's loop inductance vs. a
silkscreen label wanting space by the same part), do not hand-tune relative
weights. Shape the surfaces: the electrical term is **steep and narrow**
(0.5 mm of via displacement measurably raises loop inductance), the label
term is **shallow and wide** (it can slide, rotate, shrink to the fab's
minimum text height, or degrade to an off-part legend). The conflict then
resolves itself — the label flows around the via because its penalty
surface is nearly flat. The asymmetry is *derived* (physics on one side, an
available degrade path on the other), not asserted. This is the honest
answer to "why these coefficients?", a question every optimization paper
gets asked and few answer well.

**Bounded vs. unbounded penalties give a free priority ordering.** A
label's worst case is finite — drop it, put the legend elsewhere. An
electrical objective has no comparable ceiling. Priority falls out of the
cost structure instead of being imposed by a rule.

**Silkscreen legibility as a placement input.** Most tools treat silk as
decoration applied after the real work. Here a connector whose label has
nowhere to go is a genuine *placement* cost, and labels can push
components. Defensible claim: a board that cannot be labelled is not a
finished board — you cannot service or wire it.

**Sketch-as-canonical extends past copper.** Text is an IR primitive
(content, anchor, rotation, height, mirror); strokes are *derived* at
export. The same argument as copper-from-sketch, and it buys four things:
O(1) label moves (bounded delta, which the SA loop requires), cheap bbox
during anneal with exact glyphs only at final DRC, re-checkability when fab
rules change, and round-trippable relabelling.

**Two-sided admissibility, stated per term.** A clear bounding box is an
**UPPER** bound (looks clear ⇒ is clear) and therefore safe to anneal
against; other estimates are LOWER bounds (looks bad ⇒ is bad). Declaring
direction per term is what makes coarse-then-exact sound rather than
hopeful.

**Staging is a move-mix schedule, not an architecture.** Placement,
routing and labelling are optimized simultaneously over one state. Staging
survives only as weighting within the temperature ramp. Justification: any
stage boundary creates decisions later stages cannot undo — a part crammed
into a corner has already lost its label space.

**Methods/limitations — the silent-defect count is worth reporting
honestly.** Nine defects in this build passed type checking, passed
review, and never crashed: an inverted hardening penalty, an SA loop
comparing costs across schedules (1-in-3000 acceptance — the optimizer was
not optimizing), a temperature decayed below move eligibility, a crossings
estimator *provably* always zero, every via invisible to clearance DRC, a
phase gate unsatisfiable on a legal board, a whole family of DRC rules
structurally unreachable, two queries missing a soft-delete filter, and a
duplicate flat track-width constant in the production write path (the
function everyone believed was responsible had *zero* production callers,
so fixing it would have left every real board fused while tests passed).
**Every one was found by a property test, a reference oracle, or a
measurement — none by reading code.** The recurring generator was a rule
implemented twice in two components that then drifted — five of the nine
share exactly that shape, which is the single most reportable finding here. This is a real
methodological finding about building optimizers with LLM assistance, and
reporting it is stronger than quietly not mentioning it — it also motivates
the O(n²) reference oracle as a *deliverable*, not scaffolding.

**State the PCB-benchmark gap ourselves** (no ISPD/ICCAD PCB contest,
2012–2025) rather than letting a reviewer state it.

## Protocol discipline (the point of §2.4)

Pre-register the protocol and the metric list **before running anything**,
and report non-completions alongside completions. The macro-placement
episode did not turn on fabrication — it turned on results stated in a form
third parties could not check. A self-defined benchmark carrying a
favourable number is the exact failure shape to avoid. Release the
instances and the harness alongside the numbers.
