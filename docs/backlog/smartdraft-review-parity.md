---
status: ready
pillar: memory-graph
---

# Smartdraft review-parity remainder (UI-only)

Smartdraft shows the read-only per-block F/C/S/A checker strip from its
existing review matrix (✓ current / ~ stale / – unreviewed), including
section-scoped S/A checks in both full-page and lazy rendering.

Still pending: the machine-authored border marker for
grounded-authoring-reviewer edits (the chunk_review ledger + view='review'
are unchanged, so this is UI-only), and the classic reader's bulk
"expand around here into eyes" affordance — `draft_eyes.expand_around`
survives (only its route + UI went), so re-wiring it into smartdraft is a
UI-only add if the working-set bulk-expand is still wanted. Owner
`src/precis_web/`.
