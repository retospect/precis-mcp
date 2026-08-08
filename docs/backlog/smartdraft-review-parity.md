# Smartdraft review-parity remainder (UI-only)

The retired classic reader had a read-only per-block F/C/S/A checker-flag
strip (mirroring view='review': ✓ current / ~ stale / – unreviewed) and a
machine-authored border marker for grounded-authoring-reviewer edits; neither
is ported to /smartdraft (the chunk_review ledger + view='review' are
unchanged, so this is UI-only). Also unported: the classic reader's bulk
"expand around here into eyes" affordance — `draft_eyes.expand_around`
survives (only its route + UI went), so re-wiring it into smartdraft is a
UI-only add if the working-set bulk-expand is still wanted. Owner
`src/precis_web/`.
