---
name: ship-gate-red
description: Read the failure above the cross mark when the ship gate goes red
metadata:
  type: feedback
---

When `scripts/ship` exits red, the cause is printed above the final cross mark, not in the ship script itself. Scroll up to the first failing pytest id and fix that test.

## Related

- [Gate slots](gate-slots.md)
- [Flaky rerun](flaky-rerun.md)
- [[migration-numbering]]
