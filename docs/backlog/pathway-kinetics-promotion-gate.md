---
status: draft
title: Barriers only where they decide selectivity, on a wide promotion window with an audit sample
pillar: 3d-design
prio: high
blocked-by: pathway-selectivity-u-ph-window
---

# Barriers only where they decide selectivity, on a wide promotion window with an audit sample

## Motivation / why

Reto, 2026-10-02: kinetics for the lot, or only the promising ones? Only
the promising ones — the tier ladder (screening → neb → verify) already
promotes that way — but with two changes. First, screening is relax-only:
its fork margins are thermodynamic, and barriers can flip their sign, so
a strict top-N cut on screening scores drops winners silently. Second, a
pathway needs barriers only at the steps that decide the objective, not
on every edge (pw455722 has 15 reaction edges; about 6 decide it).

## In scope

1. **Decisive-step NEB.** At neb tier, compute barriers only for: every
   competing edge at each branch point on the target route, and the
   steps that set the span at `U_sel`. Other edges stay thermodynamic and
   are labelled so.
2. **Wide promotion window.** screening → neb promotes every candidate
   whose worst thermodynamic fork margin at `U_sel` is above a quest-set
   floor (default −0.3 eV), capped by `meta.fidelity_promote_neb`, best
   first — instead of the top-N by span.
3. **Audit sample.** Each promotion round also sends a small random
   sample (default 2) of below-floor candidates to neb, tagged `audit`.
   The fraction of audits that would have passed the kinetic gate is the
   screen's miss rate, recorded on the quest.

## Explicitly NOT in scope

- NEB itself as a pipeline step — `neb-barriers-in-the-catpath-pipeline.md`
  (chemistry Horizon 1) owns that; this item decides where it runs.
- The verify tier's seed count (already ≥3).
- Rubric weights.

## Acceptance criteria

- A neb-tier run of pw455722's config computes barriers on the
  branch-point and span-setting edges only; the others show no Ea and a
  "thermodynamic only" marker.
- `P_side` and the kinetic fork margin are non-null on a verify run
  built this way.
- A promotion round on a fixture quest promotes by the floor, adds the
  audit sample, and the quest records the miss rate after the round.

## Target + blast radius

catpath pipeline step selection (which edges get NEB); precis quest
promotion (`src/precis/quest/`, `fidelity_promote_neb`), the
redispatch path (`quest-redispatch-tier.md` touches the same code —
sequence behind it or coordinate).

## Open questions / decisions log

- Default floor −0.3 eV is a guess at the thermodynamic-vs-kinetic
  disagreement size; the audit miss rate is what should tune it.
