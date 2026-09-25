---
id: precis-se-design-help
title: precis — designing in se (the abstraction-ladder walk)
summary: the workflow — set box + forces on a root block, interfaces before interiors, run the checking views at every rung, refine block-by-block under a frozen contract, accept proposed values, realize leaves, arbitrate tradeoffs via measures/notes/quest frontier
answers:
  - how do I start an se design from requirements?
  - how do I refine a design without breaking what's already checked?
  - how do I get proposed geometry/components and accept or override them?
  - how do I express and resolve tradeoffs in a design?
applies-to: get/edit/put (kind='se'); read precis-se-help first for the op grammar
status: active
tags: workflow, design
kinds: se
---

# precis-se-design-help — how to walk the ladder

The contract of the ladder: **interfaces are preserved, interiors are
replaced.** A coarse check stays valid after refinement, so check early
and re-check cheap at every rung. Op grammar, units (metres, radians —
`rot` is bare radians, not degrees!), and the half-extent envelope
warning: `precis-se-help`.

## 0 — set box, set forces

One root block: a tolerance-box envelope for the whole machine, `fixed`
supports and external loads via `set_load`, and the requirement measures
(`add_measure` with `min`/`max`, `strength: hard` only for genuine
must-holds — default `gauge`, `soft` for preferences). This design is
already checkable: run `validate` and `freedom` on the box.

## 1 — interfaces before interiors

Ports + joints + measures on the boundary are the contract every later
solver treats as boundary conditions. Declare mating ports, joint
classes, connect objectives (`force`/`torque`/`duty`/`cycles`) before
any interior exists. `view='interview'` elicits what's missing — run it
whenever unsure what to declare next.

## 2 — check at every rung

After each edit batch: `validate` (structure), `drc` (capacity, BOM
demands, interpenetration), `stability` when axial members exist,
`clearance` for named pairs, `fasten` where stack-ups join. Cheap, and
regressions surface at the rung that caused them. A clean report on an
unfilled scaffold means "nothing wrong YET" — the views say so; believe
the header, not the absence of findings.

## 3 — refine: identify subcomponents

Split a block into children (`add_block parent=…`); the parent keeps its
ports/measures as the frozen contract. Repeats: `instance_block` /
`array_block` (blocks only — N similar connects are authored
individually; generate them). Tie child dimensions to the contract with
measure `relation`s instead of copying numbers.

## 4 — proposed subcomponents: accept or override

System suggestions arrive as `origin: 'proposed'` and **never overwrite
human-set values**:

- `formfind` writes equilibrium poses onto proposed-origin blocks only
  (`move='all'` to opt everything in).
- Component selection: `precis-part-select-help` for choosing real
  parts; bind with `set_binding`, cost/availability via `view='bom'`.
- Accepting a proposal = re-declaring it with `origin: 'user'` (or just
  leaving it proposed until it matters).

## 5 — realize leaves

Every leaf gets a `set_mode` (`purchase`, `fdm/asa`, …) or a
`set_binding` to a `component`/`part`/`cad`/`structure` design, plus BOM lines
(`add_bom`) for what mechanisms demand. Dangling component slugs are
expected until the component is minted — `view='bom'` lists them.
Process DRC is unshipped: mode is intent, not yet checked.

## 6 — make tradeoffs

- Local tension: `soft` measures + connect objectives carry the numbers.
- Human-arbitrated: `add_note` kind=`question`, answered by
  kind=`decision` notes (`re=` links them) — the design carries its own
  open-questions list.
- Candidate-level: mint alternatives as separate designs, link `serves`
  → a quest; `quest` frontier Pareto-ranks against human-set rubric
  weights (mass via `bom`, compliance, member count, cost). **Weights
  are human-set; a solver never tunes its own objective.**

## Worked example (in prod)

`unicycle-mk2` is the live 20-inch printable demo: 15 blocks, flat (no
subassembly parents), wheel as one integral cylinder envelope — tire, rim,
spokes and hub abstracted into a single block. Read it with `view='tree'`,
then `view='stability'` to see what that abstraction costs you: the
stability view reports **no axial members — stability analysis does not
apply**, and warns that the loads on `saddle` and `wheel` sit outside the
analysed subgraph, so they were not checked.

That is the lesson, not a defect. The stability view models *pin-ended
axial members only* — an integral wheel has none, so there is nothing for
Maxwell/Calladine to count. Lacing the wheel instead (a hub, a rim, and
`connect` members with `class: "axial"`, `compression_capacity: 0` and a
declared `preload`/`free_length`/`rate` triple) is what makes the view
informative — and what it then reports is non-obvious: a purely radially-
laced wheel comes back **first-order mobile**, because radial spokes
transmit no crank torque. An earlier laced revision of this design is
where that showed up; it has since been retired, so re-derive it on your
own design rather than expecting to load it.

Caveat while modelling that: `rigid` connects are not in the equilibrium
matrix (gr334788), so a rim built as a ring of rigid blocks contributes
nothing — express the ring itself as axial members if you want the view to
see it.
