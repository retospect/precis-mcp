"""The laptop fixer loop (ADR 0048).

A git-world CI scheduler that closes the dark-factory loop: pick a
ready work item, build it with ``claude`` in an isolated worktree,
gate it, and — at higher autonomy — ship + deploy + look at prod +
fix-forward, reporting by exception.

This is the **repo-dev** lane's scheduler. It deliberately does *not*
ride precis dispatch (which is content-only, ADR 0030/0048); precis
is touched only as a source (gripes) and sink (status). The proven
``/go`` core (``scripts/ship`` + ``scripts/deploy``) is the deploy
heart; this package is the autonomous intake + verify-and-fix wrap.

Entry point: ``python -m precis.fixer.tick`` (via ``scripts/fixer-tick``).
"""

from __future__ import annotations

from precis.fixer.intake import WorkItem, parse_front_matter, pick_next, ready_proposals

__all__ = [
    "WorkItem",
    "parse_front_matter",
    "pick_next",
    "ready_proposals",
]
