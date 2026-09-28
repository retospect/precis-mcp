---
status: draft
title: A footprint pad may name any layer string, and an unrecognised one goes invisible to DRC rather than rejected
prio: normal
---

# A footprint pad may name any layer string

## Motivation / why

A local footprint's pad carries its copper layer as a free string. Nothing
validates it against the board's stackup. `drc.py` buckets pads into
per-layer trees keyed on that string, so a pad naming a layer no other pad
names lands in a bucket of one: it is compared against nothing, and every
clearance check it should have failed passes. The pad is still flashed into
the gerbers. Same failure shape as gr451276 (a pad the router could not see
is one it draws through) and as the two pour bugs fixed 2026-09-28 — a
fixture a pass cannot see is one it never checks.

**Unreachable until recently.** Only `F.Cu`/`B.Cu` were ever written, and
`padplace._effective_layer` maps mount side onto exactly that pair. What
changed: `put(args={'op':'stackup'})` (shipped 2026-09-28) makes inner-layer
names authorable design data, so a hand-authored footprint naming `In1.Cu`
— or `In1`, or `in1.cu` — is now a plausible thing for an LLM to write,
and the near-miss spelling is the dangerous case.

Filed rather than fixed because it is latent: no board in the repo or in
prod carries such a pad today. It wants a decision, not a scramble.

## In scope

1. **Validate a pad's layer name where the footprint is stored**, against
   the board's stackup names — the same posture `ir.validate_stackup`
   takes for a stackup entry (unknown key REJECTED, never ignored, because
   a silent fallback is what lets an instruction be accepted, stored and
   never honoured).
2. **Or**, if validation at author time is the wrong seam (a footprint is
   reusable across boards with different stackups, which is a real
   argument), make DRC LOUD about a pad whose layer matches no stackup
   layer, instead of bucketing it alone. A `DrcFinding` with the pad
   named, not a silent pass.

Pick one. Doing both is fine; doing neither is the current state.

## Explicitly NOT in scope

- Changing how mount side resolves to a layer
  (`padplace._effective_layer`). That function is correct and is the one
  implementation — see gr341516.
- Promoting footprint pads to IR pins.
- The three NAME comparisons the engine already documents as load-bearing
  (`padplace.opposite_layer`, `generators.py`'s F.Cu escape refusal, the
  Gerber file map). `ir.OUTER_LAYER_NAMES` already pins those.

## Acceptance criteria

- A footprint pad naming a layer absent from the board's stackup produces
  an error naming the pad and the offending string — at author time, or as
  a `DrcFinding`, per the decision above.
- A test asserts the CURRENT silent behaviour is gone by constructing the
  bad pad directly, not by hoping a fixture happens to have one.
- No existing board changes its DRC output. (Nothing today names a layer
  outside F.Cu/B.Cu, so a passing suite is evidence, not proof — say so.)

## Target + blast radius

`src/precis/pcb/drc.py` (per-layer pad trees), the footprint store path in
`src/precis/store/_pcb_ops.py`, possibly `src/precis/pcb/padplace.py`.
Read-only for every board that does not have such a pad, which is all of
them.

## Open questions / decisions log

- **OPEN — author-time or DRC-time?** A footprint is reusable across boards
  whose stackups differ, which argues for DRC-time. But author-time is
  where the mistake is made and the only place the message can be
  actionable. Leaning: DRC-time loudness is the floor (it protects every
  board), author-time validation is the nicety.
