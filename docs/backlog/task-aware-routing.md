---
status: draft
title: Task-aware routing over owned capacity — one ledger and one decision for every LLM call and builder session
pillar: platform
prio: high
spec_root: me568172
plan_todo: td568194
---

# Task-aware routing over owned capacity

Stub (Reto 2026-11-05: specs live in the memory graph). One allowance ledger
and one deterministic decision function serve both the completion router and
the builder-lane picker; every call names its task kind; pools pace to their
reset; suitability is learned from validated outcomes.

Spec root `me568172` (`get(kind='memory', id='me568172', extent='fisheye+1hop')`);
design sections under `me568174`, Reto's 2026-11-05 rulings under `me568175`.
Work: plan todo `td568194` under the dark-factory root `td540836`, slices
S0–S9 `td568203`–`td568212` (`get(kind='todo', id=568194, view='tree')`).
This file is deleted once gr568171 teaches `ready`, docs-index and the
gripe-fold convention to read the graph.
