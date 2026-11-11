---
status: draft
pillar: 3d-design
title: T-handle bearing — the third hexfold test piece (nanotube race, rotating inner handle)
prio: high
blocked-by: me628772
---

# T-handle bearing — third hexfold test piece

*(Transfer to graph memory is pending — spec-in-memory ruling
`me568168`, target carbon-topology spec `me628772` (this is a rotary
test piece related to the valve `me629053`), gripe `gr629125`. The
2026-11-11 transfer pass wrote nothing for this file (the builder lane
had no precis MCP) and review `td630546` found `gr629125` still open on
the banner alone. The executing lane needs the session MCP on PROD —
the writes cannot be done from a no-MCP lane. Do not delete this file
until the nodes below are written, `me628776`'s reading order carries
their handles, `me628772`'s not-transferred list drops this file, and
the references are repointed — retiring ahead of the transfer was
reverted once already (`a331c87a4`).)*

*Transfer manifest — coverage checked 2026-11-11 (read-only prod
queries): every `part-of` child of `me628772`'s five groups
(`me628774`, `me628775`, `me628776`, `me628777`, `me630478`) was
listed; no node covers the T-handle bearing or the joint-sweep first
consumer. Neighbours only: `me629053` (the valve rotary piece — the
prior revolute instance), `me630418`/`me630419` (join composes resolved
blocks over matched port pairs; JOINERS dispatch), `me628980` (the
nanoreactor spec). Missing — write each as one `section:reference`
node tagged `spec:carbon-topology` in `SPACE:repo-dev`, `part-of` →
`me628776` (the group that holds the join/block machinery), `Source:`
line naming this file + transfer date + gripe `gr629125`, house shape
of the `me630415`–`me630430` set:*

1. *Title ≈ "The T-handle bearing is the third hexfold test piece — a
   reusable bearing, not another one-off". Body: Reto, product-plan
   review 2026-09-30 — the third test piece after the box
   (`src/hexfold/spec.md` §28 roadmap step 3) and the rotary ratchet
   valve (`me628980`, group `me628987`, built pieces `me629053`): a
   nanotube with buds on opposite sides serving as races and an inner
   T-handle block that rotates. Peer-session hexa finding, same day:
   buds-on-opposite-sides is producible under the existing notation,
   and the revolute joint needed is the same two-block joint the box
   and valve already specify ("axle ⇄ liner as separate blocks",
   "rotor ⇄ shell as separate blocks") — no new joint primitive.
   Motivation: box and valve each proved the revolute pattern once but
   neither is designed as a *bearing* — a component meant for reuse
   inside a larger assembly; this piece validates that pattern and is
   the first real consumer of the se joint sweep
   (`python:main::precis_se.kinematics_drc.sweep_findings`: a revolute
   joint's declared `params.range` swept for envelope collisions,
   `joint_sweep_interference`/`joint_sweep_unchecked`, read under
   `view='drc'`). `Built:` none — draft, unbuilt; the code-first
   workspaces halt `me510763` applies. `cites` → `me629053`;
   `related-to` → `me628980`, `me630418`.*
2. *Title ≈ "T-handle bearing contract: builds and checks clean on the
   existing revolute mechanics — new joint code is itself a finding".
   Body: in scope — the `.hx` piece (bud races + inner T-handle
   block), the two-block revolute connect built the box/valve way, and
   wiring the piece as the first joint-sweep consumer (declare the
   handle's `range` on its revolute connect, read `view='drc'`). Not
   in scope — the sweep itself (it lives in
   `precis_se.kinematics_drc`; this item is its first real case, not
   its implementation), motion planning or torque budgeting (the
   DRC-vs-planner boundary the `se-3d-viewer` thread draws), and the
   incommensurate-lattice shaft-in-sleeve bearing
   (`diamondoid-pattern-language.md` open question 3 — the sp³
   diamondoid variant; this is an sp² hexfold bud-race bearing, a
   different lattice regime). Acceptance — the piece builds, `check`s
   clean under the existing notation, round-trips through `generate`
   as bound `structure` designs matching the box/valve precedent, and
   the handle rotates within its race on the existing revolute-joint
   mechanics; if new joint code turns out to be needed, that is itself
   a finding to report back on `me628772`. Blast radius `src/hexfold/`
   only — no new `precis_se` joint machinery expected. `Built:` none.*

*Then: append the two new handles to `me628776`'s "Sections in reading
order" line (after `me630430`); edit `me628772`'s "Not transferred yet"
line to drop this file — close `gr629125` only once both its files land
— repoint `docs/roadmap.md` ("`backlog/hexfold-t-handle-bearing.md`,
third piece"), `docs/backlog/threads/hexfold-toolkit.md` item 6 and
`docs/backlog/threads/se-machine-design.md` (the joint-sweep first
consumer) to the new handles (or `me628776` where the item as a whole
is meant); delete this file and regenerate `docs/backlog/INDEX.md`
(`scripts/docs-index`).*

Reto, product-plan review 2026-09-30: the third hexfold test piece after
the box (`src/hexfold/spec.md` §28 roadmap step 3) and the rotary ratchet
valve (spec `me628980`, group `me628987`). A nanotube with buds on
opposite sides serving as races, with an inner T-handle that rotates.

Peer session hexa, 2026-09-30: buds-on-opposite-sides is producible today
under the existing hexfold notation; the revolute joint needed is the same
two-block joint the box and valve test pieces already specify — `spec.md`
§28 roadmap step 3 names both "axle ⇄ liner as separate blocks with a
revolute joint" (box) and "rotor ⇄ shell as separate blocks with a revolute
joint" (valve). No new joint primitive.

## Motivation / why

Two test pieces (box, valve) have proven the revolute-joint pattern once
each, but neither is designed as a *bearing* — a component meant to be
reused as a building block inside a larger assembly. The T-handle piece is
the first one built explicitly to validate the bearing pattern and to be
the first real consumer of the se joint sweep
(`precis_se.kinematics_drc.sweep_findings`: a revolute joint's declared
`params.range` checked for envelope collisions in `view='drc'`).

## In scope

- The `.hx` piece: a nanotube with buds on opposite sides as races, an
  inner T-handle block.
- The two-block revolute joint, built the same way the box/valve pieces
  already do it — no new joint kind.
- Wiring this piece up as the first consumer of the joint sweep: declare
  the handle's `range` on its revolute connect and read `view='drc'`.

## Explicitly NOT in scope

- The joint sweep itself — it lives in `precis_se.kinematics_drc`; this
  item is its first real case, not its implementation.
- Motion planning or torque budgeting for the handle — out of scope per the
  same DRC-vs-planner boundary `se-3d-viewer`'s thread draws (a check, not
  a planner).
- The incommensurate-lattice question for shaft-in-sleeve bearings
  (`diamondoid-pattern-language.md` open question 3) — that's the sp³
  diamondoid bearing variant; this is an sp² hexfold bud-race bearing, a
  different lattice regime.

## Acceptance criteria

- The `.hx` piece builds, `check`s clean under the existing hexfold
  notation, and round-trips through `generate` as bound `structure`
  designs, matching the box/valve precedent.
- The T-handle rotates within its race with the same revolute-joint
  mechanics the box/valve pieces already validate (no new joint code
  needed — if new code turns out to be needed, that is itself a finding
  worth reporting back on spec `me628772`).

## Target + blast radius

`src/hexfold/` (the `.hx` piece definition); no new `precis_se` joint
machinery expected — reuses the box/valve revolute-joint pattern.

## Open questions / decisions log

- None yet — this item is new from the 2026-09-30 review; open questions
  will surface once building starts.

Closest existing items: the rotary ratchet valve spec `me628980` (the
same revolute-joint pattern, prior instance),
`diamondoid-pattern-language.md` (shaft-in-sleeve bearing, sp³ variant,
the incommensurate-lattice question), spec `me628772` (the retired hexfold-integration.md).
Thread: `docs/backlog/threads/hexfold-toolkit.md`.
