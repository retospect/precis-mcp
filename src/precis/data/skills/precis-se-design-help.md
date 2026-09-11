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
---

# precis-se-design-help — how to walk the ladder

The contract of the ladder: **interfaces are preserved, interiors are
replaced.** A coarse check stays valid after refinement, so check early
and re-check cheap at every rung. Op grammar, units (metres!), and the
half-extent envelope warning: `precis-se-help`.

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
`set_binding` to a `component`/`part`/`cad`/`nm` design, plus BOM lines
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

`unicycle-printed-v1`: 29 blocks, 12 tension-only `axial` spokes at
600 N verified as a self-stress state; the stability view correctly
reports it **first-order mobile** — radially-laced wheels transmit no
crank torque, which is the kind of non-obvious truth the checking views
exist to surface. Read it with `view='tree'` then `view='stability'`.
