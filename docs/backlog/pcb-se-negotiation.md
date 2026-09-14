# se↔pcb outline negotiation — grow the box when place/route fails (idea)

Reto, 2026-09-14 (glowing-zooming-glade design session): folding breeds
two-way constraints — the placer and router see via/fold keep-out zones
that come from mechanical intent, and mechanical space depends on what
placement needs. Wanted: author the board outline in se and *link* it
to the pcb design (or author in pcb and mirror to se); when routing or
placement fails, pcb should be able to ask for more area and se should
"make the box bigger". Can it be done? Yes — with owned-data message
passing, not two-way sync; nearly all machinery is shipped.

**The flows, each with one owner:**

- **Intent, se→pcb**: se authors the outline as a proposed envelope
  (flat pattern; `origin=proposed` — an envelope is a *budget*, se's
  whole design language is suggestive-hardening) and, for flex, fold
  lines as joint positions between segments. Exported to pcb as its
  authored `outline` feature + fold/keep-out features. Crossing: the
  **same seam module** as `mechanical_profile`, opposite direction
  (m→mm). This refines the map's enclave ruling: one seam *module*
  owns both directions of the pcb crossing — still exactly one place
  in the tree where the 1e-3/1e3 pair appears.
- **Realization, pcb→se**: `mechanical_profile` as already specced in
  `pcb-se-binding.md` (segments, prisms, holes, connector ports).
- **Failure, pcb→se, as findings not writes**: on place/route failure
  the pcb side emits a *computed demand* — "infeasible at this
  outline; shortfall ≈ X mm² / escape capacity exceeded on edge Y"
  (escape.py's capacity math and the optimizer's objectives can ground
  the number). pcb owns the feasibility verdict; it never writes se
  state.
- **Growth is an ordinary se op**: the driving agent (later a
  negotiation job) enlarges the proposed envelope via `set_envelope` —
  and se DRC immediately re-checks the grown box against its
  neighbours, which is exactly the check se exists to run. "Can the
  box get bigger" is answered by the surrounding design: the freedom
  vocabulary and clearance findings say whether the slack exists, and
  if it doesn't, the conflict surfaces to the human instead of being
  silently absorbed.
- **The loop**: propose outline → link (the shipped `link` verb —
  designs are in the ref graph) → pcb place/route attempt → demand
  finding → grow envelope → re-export → retry. Converges, or
  terminates with a genuine spatial conflict made visible.

**What stays banned** (per `pcb-se-binding.md`): two-way *sync*. Every
datum keeps one writer — outline intent: se; placement/routing/
feasibility: pcb; world pose + installed fold state: se. Negotiation
is messages over owned data, never two writers of one field.

**New pieces needed** (beyond `pcb-se-binding.md` +
`pcb-flexboard.md`): the se→pcb outline/fold export in the seam
module; the placer/router area-demand finding; agent-driven loop first,
a negotiation job type only if the manual loop proves the shape.

test: dogfood loop — a deliberately undersized se outline linked to a
board whose escape capacity fails; the demand finding names the
shortfall; one `set_envelope` growth; re-run places and routes clean;
se DRC confirms the grown box still clears its neighbours.
