---
status: ready
---
# Reviewed R14 mechanical assembly

Coordinator authorizes assembly from exact R13 base
`650232a82e911fcca4a18d9fd761bd3b303d38e1` in an isolated worktree.
Apply only the reviewed Hex S1 three-commit snapshot series, private Hex k3
spec/feature/fix series, PCB snapshot/escape/cache-fix series, knowledge
advisory plus malformed-pin correction (excluding its merge), and pure
unwired catalysis retry selector. Preserve source rationale and final
snapshot/index validation fixes; ambiguous behavior conflicts stop assembly.

Set project version 8.35.13 with frozen lock; preserve Catpath 0.22 at
973491d4 and the dependency graph. Root PASS receipts identify each accepted
source pin in shared fleet-state. Paper, radicals, Melu and fleet tooling
remain excluded; scientific, non-dogfood, service and engine holds remain.

Validation here is whitespace, whole-repository read-only Ruff lint/format
and syntax parsing only. No tests, typecheck, image or Docker/service actions
while R13 gates. Publish only the non-force integration branch, verify exact
remote SHA, and hand off source-to-integrated mapping. Full integrated gate,
spec retirement, freeze, main publication and deployment stay with root.
