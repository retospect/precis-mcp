---
status: idea
title: BOM consolidation nudge — circuit tolerance open question
pillar: 3d-design
prio: low
---

# BOM consolidation nudge — fewer distinct parts, one standard set

**Shipped 2026-10-12** (all three slices): `spec_roles` jsonb on
`component_categories`/`component_specs` (migration 0205), the
`## Consolidation` footer on `get(kind='component', view='bom')` and
`get(kind='pcb', view='bom')`, `kept-distinct` link/tag suppression, the
LLM merge verdict (`precis.bom_nudge.ask_merge_verdict`),
`src/precis/data/standard_parts.json`, cross-design usage counts, and a
guarded live-stock hint. See `precis-component-help` /
`precis-pcb-help`.

## Remaining open question

- Whether the tolerance of the *circuit* (as opposed to the part) can be
  known without asking a human; the LLM verdict cites design intent, and a
  line with no stated intent says so rather than asserting.
