---
status: draft
title: Semantic feature model — intent canonical, mask/paste/silk films derived
prio: high
model: opus
---

# Intent canonical, films derived

The next application of this build's recurring principle (sketch canonical
→ copper derived; text canonical → strokes derived): **functional intent is
canonical, and the mask/paste/silk films are projections of it.**

## The precise claim (not "layers are just output")

**Copper layers are physically real.** A 4-layer board has four copper
planes; z-order drives coupling, return paths and impedance. A conductor
must name its layer.

**Mask, paste and silk are not real in that sense.** They exist only to
express intent *about* copper. They should never be primary storage.

So: *layers are real for conductors; films are derived from intent.*

## Why boolean decoding is lossy, not merely inelegant

The natural decode rule — copper ∧ ¬mask ∧ ¬hole ⇒ SMT pad — conflates
functionally distinct things:

| Intent | copper | mask opening | paste | hole |
|---|---|---|---|---|
| SMT pad | ✓ | ✓ | ✓ | — |
| **Thermal pad** | ✓ | ✓ (one) | **windowpaned grid** | — |
| **Test point** | ✓ | ✓ | **none** | — |
| **Tented via** | ✓ | ✗ | — | ✓ |
| Covered trace | ✓ | ✗ | — | — |
| **Via-in-pad** | ✓ | ✓ | ✓ | ✓ plugged+plated |

A thermal pad's paste windowpane **cannot be derived** from
copper-and-mask-opening; get it wrong and the part floats on molten solder.
A test point is layer-identical to an SMT pad minus one film. A tented via
decodes as a covered trace. The stack cannot represent these distinctions,
so an intent-first model is *strictly more expressive*, not just tidier.

## Sketch of the model

Features, each knowing how to project itself into films:

- `SolderablePad{shape, pos, side, paste: full|windowpane|none}`
- `Conductor{layer, geometry}` — layer is real, keep it
- `Keepout{extent, layers, excludes: conductor|component|both}`
- `Hole{dia, plated, plugged}`
- `Marking{content, side}` → silk
- `Pour{layer, net}`

Projection to the gerber model is a pure function; the existing
`pcb/gerber.py` writer stays as the projection target, unchanged.

## What it buys

**Better DRC rules, not just faster ones.** "Are these two *solderable
pads* too close" is a solder-bridging rule with a different threshold than
trace-to-trace clearance. "Does a conductor enter a keepout" becomes a
direct query rather than a layer-mask intersection. Fewer rules, each more
precise.

**Interrogable footprints.** "Does this footprint have 8 solderable pads,
and where?" becomes answerable — exactly what `view='pinout'`, connector
intake and escape routing need and cannot currently get.

**Speed, honestly:** fewer objects, and clearance queries filterable by
semantic pair type (pad↔pad, conductor↔keepout) instead of all-pairs across
every film; optimizer deltas touch features rather than re-deriving four
layers. Real, but secondary to expressiveness — do not sell it as the main
benefit.

## The honest cost

EasyEDA footprint data arrives **layer-shaped**, so ingest must *lift*
layers into features — the very inference this avoids. That is acceptable
**only** because it happens once, at the boundary, with ambiguities
recorded rather than silently resolved. Confining the inference is the
goal; eliminating it is not possible.

Watch for: a lift that guesses wrong writes a permanently wrong footprint.
Record confidence and the evidence used, and make unresolved cases loud.

## Lower late, and never lift back

Features are the **IR**; films are the **target**. Everything internal —
optimizer, DRC, rendering, escape routing — works on features. Lowering to
mask/paste/silk happens **once, at export**, as the last step.

**Lowering is one-way and terminal.** Nothing inside the system may read
films back. Code that consumes the gerber model and infers intent is a
*decompiler*, and boolean layer-decoding is exactly that: reconstructing
something you had and discarded. The classic compiler error is emitting
machine code and then trying to optimize the machine code.

**Enforceable invariant** (same shape as the dead-export test): the gerber
model has exactly **one producer** (the lowering pass) and **one consumer**
(`pcb/gerber.py`'s writer). Anything else touching it is a bug and a test
should say so.

This also explains an earlier oddity: `realize.to_gerber_model` with zero
callers was a lowering step with no terminus — correct structure, missing
end. The fix was never to give it more callers *inside* the system.

## Blast radius

Additive, not a rewrite: define features → lift at ingest → project to the
existing gerber model. Sits *above* today's representation.

## Timing

Decide before pad handling calcifies. Instance pad placement is being
written now (`pcb-fab-output-unwired.md`); if features are the target, that
transform should eventually emit features rather than layer shapes. The
transform mathematics is identical either way, so the in-flight work is not
wasted — but do not accrete more layer-shaped pad logic on top of it.

> **Consider for the paper.** "Manufacturing output formats make poor
> internal models" generalizes past PCB: the film stack is a
> photoplotting artifact from the 1970s that most tools still use as their
> data model, forcing semantics to be recovered by boolean decode. See
> `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.
