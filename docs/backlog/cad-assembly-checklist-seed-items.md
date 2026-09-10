---
status: draft
title: "cad/3D-print assembly checklist — curated seed items (companion to checklist-kind.md, slice 4)"
prio: high
---

# cad assembly checklist — seed items

Companion to `checklist-kind.md` slice 4 (the cad instance). Theme
(Reto, 2026-09-10): parts are modeled (screws etc., component assembly
tree) — but **assemblability is only checkable if the assembly TOOLS
are modeled too**. Live failure prompting this: a part where the
screwdriver is too fat to reach the screw; it could as easily have
been too long. Umbrella question: *can the parts fit, can the tools
be reached and operated?*

**Prerequisite (engine-side, the DRC-analogue for this domain):** an
owned-tool library — the screwdrivers/drivers we actually have, as
`component` entries with envelope geometry (shaft diameter, shaft
length, tip type/size, handle diameter, swept volume when turning) —
plus a cad reachability probe that sweeps the tool envelope along the
fastening axis. Until that probe exists, `fastener-tool-clearance` is
a judgment item whose verdict must say when it could not really be
checked (the not-run ≠ clean rule); once the probe ships it becomes a
tool item.

## Tool bridges

| item | phase | decidability | prevents |
|---|---|---|---|
| cad-validate-clean | design | tool | The model ships with kernel-level defects (invalid solids, interferences) the existing validate/clearance probes would have caught. |

## Design (model-time judgment)

| item | phase | decidability | prevents |
|---|---|---|---|
| fastener-tool-clearance | design | judgment | The driving tool cannot physically operate the fastener: shaft too fat for the access bore, tool+fastener+hand longer than the headroom above it, or handle swing colliding with the part — the screw is fine, the hole is fine, and the assembly is impossible with the tools we own. Check against the owned-tool library, not an idealized driver. |
| single-direction-fastening | design | judgment | Fasteners entering from multiple directions force flipping the assembly mid-build, so already-placed parts fall out of alignment and every flip needs re-fixturing. Preferred: all fasteners drive from ONE surface; each exception argued in the item's thread. |
| assembly-sequence-marked | design | judgment | An order-dependent assembly (part B's screw unreachable once part A is mounted) is discovered with printed parts in hand; and without a marked disassembly order, service means breakage. The sequence is recorded AND marked on the parts (embossed step numbers at each fastener). |
| fastening-effort-budget | design | judgment | Nobody summed the turns: thread engagement × pitch × fastener count. Catches both absurd total assembly effort and over-long engagement in printed plastic (stripping/cracking a boss that only needed 3 turns of bite). |
| alignment-hands-count | design | judgment | Some assembly step silently requires holding three or more pieces in simultaneous alignment — a two-handed human cannot do it. Every step's held-piece count is stated; steps over 2 get self-jigging features (keys, bosses, snap retention that holds part N while N+1 arrives). |
| disassembly-path-exists | design | judgment | The assembly can be put together but not taken apart (captive part, glue-only step, fastener whose tool access exists only before a later part is mounted) — repair or battery/wear-part replacement means destroying the print. |

Print-phase items (overhangs, supports, orientation, tolerances-vs-
process) arrive with `cad-printability-probe` — they are that item's
scope, not duplicated here.
