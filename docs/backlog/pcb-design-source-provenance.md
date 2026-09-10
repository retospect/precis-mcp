---
status: draft
title: "pcb: structured source provenance — pin facts and design decisions cite datasheet/app-note chunks"
prio: high
---

# pcb design-source provenance

Design session 2026-09-10 (Reto + agent), zippy-booping-sky worktree,
during the checklist-kind build. Two questions, one mechanism:
"do we link to the chunk in the datasheet that supports why we believe
this pin has that feature?" and "do we link reference-design documents
from netlist components or layout things? This can be rich with source
material — as opposed to the human tools."

Current state (verified against `0047_pcb_kind.sql`): `pcb_pins` has
`tags text[]` (intended vocabulary `input|output|bidir|passive|power|
nc|analog|clock|data|gnd|3v3…`) and `description` ("datasheet
function/notes") — but nothing enforces the vocabulary, no ERC
consumes it, and no structured link ties a pin fact or any design
decision to its source. All pcb subobject rows (`pcb_components`,
`pcb_nets`, `pcb_netconns`, `pcb_measures`, features) have free-text
`note` + `meta jsonb` only.

The pattern to copy exists in-repo: `component_spec_values` carries
`source_ref_id` + `source_chunk` + `method` + `as_of` per value
(`store/_component_ops.py::ComponentValueRow`).

## Proposal

1. **Pin-fact provenance**: per-pin source anchor (datasheet ref +
   chunk) for the tags/description — the evidence a
   `strap-boot-pins-vs-datasheet` checklist verdict cites, and the
   chunk link the annotated schematic view (checklist-kind slice 5)
   renders. Tighten the pin-tag vocabulary at the same time (enforced
   enum-ish set), since sourced tags are what future encoded ERC
   consumes. Overlaps `pcb-component-model.md` roles/capabilities —
   coordinate, don't duplicate.
2. **Design-decision sources**: registered meta convention
   `sources: [{ref, chunk, note}]` on any pcb subobject row —
   "this decoupling network from the ESP32-C3 hardware design guide
   §2.1", "this crystal-island measure set from the vendor app note".
   Rendered in TOC/instance/net views.
3. **Ingest**: app notes / reference designs enter as `datasheet`
   kind with `subtype` (`appnote` / `refdesign`) — reader, chunks,
   and `link` already work there; nothing new to build.
4. **Adversarial validation (Reto, 2026-09-10: "the pin has a ref
   and the checklist checker validates adversarially")**: the stored
   ref is a CLAIM, not evidence. The checklist checker re-reads the
   cited chunk cold and tries to falsify: pass = the chunk actually
   supports the pin fact, with the decisive line quoted into the
   verdict's evidence; dangling/missing ref = not-checkable (never a
   silent pass); chunk contradicting the tag = fail with the
   discrepancy in the item's argument thread. Checker independence:
   not the agent that authored the tags (the grounding-audit posture
   from the claim layer — same pattern, same reason). Encode this in
   `precis-tapeout-help`.
5. **Reverse query**: "which decisions in this design came from
   document X" — enables re-review when a vendor revises an app note.
   (Later: staleness propagation from a re-ingested source into
   checklist verdicts that cited it — note as future, don't build.)

Timing: **now is cheap** — no live designs exist (Reto 2026-09-10:
"we have no designs, we can change"), so schema/convention changes
carry zero migration burden.
