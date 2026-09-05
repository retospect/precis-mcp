---
status: draft
title: margin budget tree — system-level engineering margin allocated unevenly down the contains tree
prio: medium
model: opus
---

# Margin budget tree (rides the estimate kind)

Design session 2026-09-04 (Reto + agent), imperative-plotting-hare worktree.
Reto: "we want to add an engineering margin too on a system level and it
maybe translates unevenly to each component". Best-practice grounding:
`perplexity-research:317035` — the *Hierarchical Margin and Reliability
Budget Propagation Pattern* (NASA/ESA + MSFC margin tables).

## The mechanism

A **budget tree** over `contains` links: a quantity on a parent (mass
budget, power, thermal, margin factor on load) allocated with **per-edge
weights** down the tree — uneven allocation is just non-uniform weights
(mature subsystems get thin margin, risky ones get fat). A lint checks
that children's claimed values compose within the parent's budget;
violation files a gripe (the same code-flags-issues channel as
`attached-models-layer.md`).

**Two quantities, not one** (the report's #1 named failure mode is
conflating them):

- **growth allowance** — *expected* growth, a function of design maturity
  (NASA: 25–40% at concept review shrinking to 5–10% at test readiness);
- **margin** — protection against *unexpected* growth, on top.

predicted = basic + growth_allowance; margin = requirement − predicted.
Consuming one vs the other means different things for risk, so both live
on the node, and targets are **time-phased** (margin requirements decline
toward zero as the design matures — a maturity field, not a date).

## Why the estimate kind

Margins are uncertainty-carrying quantities by nature — basic value,
allowance, and margin are exactly the value/value_low/value_high +
provenance shape the estimate kind (designed in
`quest-dossier-dialectic.md`, unshipped) carries. Blocked on that build;
do not hand-roll a parallel quantity store here.

## MTBF / reliability — parked, one warning kept

Same budget-tree shape (a failure-rate budget allocated down), BUT:
reliability does **not** aggregate by summation — series/parallel structure
and redundancy change the roll-up, so it needs a structure-aware combiner,
not the linear lint above. Backlog line only, until the linear budget tree
exists and someone actually asks for reliability.

## Sequencing

Blocked on: estimate kind (carrier) + `design-graph-relations.md`
(`contains` tree to allocate over). Then: v1 = mass budget on a cad
contains-tree, one lint, one gripe path — the smallest end-to-end slice.
