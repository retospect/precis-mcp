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

Landed piece (gr595546): the weekly `llm_tier_regression` scheduler
cadence runs the packaged ~30-task gold set through every rung of every
tier's resolved chain, records each offering's pass rate in
`app_settings['llm_tier_regression:last']` (prior runs in `history`),
and the `llm-tier-regression:<tier>/<rung>` condition alert fires on a
week-over-week drop past `PRECIS_COND_TIER_REGRESSION_DROP` or a
scored offering going dark — model/wire/provider drift, including a
silent model-id swap. Implementation:
`src/precis/llm_eval/regression.py` + `src/precis/workers/tier_regression.py`;
discovery: `precis-alert-help` / `precis-health-digest-help` skills.
