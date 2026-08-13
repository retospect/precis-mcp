"""The laptop fixer loop.

A git-world CI scheduler that closes the dark-factory loop: pick a
ready work item, build it with ``claude`` in an isolated worktree,
gate it, and — at higher autonomy — ship + deploy + look at prod +
fix-forward, reporting by exception.

This is the **repo-dev** lane's scheduler. It deliberately does *not*
ride precis dispatch (which is content-only); precis
is touched as both a source and a sink. As a source: proposals under
``docs/backlog/`` always, plus — behind the ``PRECIS_FIXER_GRIPE_DB``
dial — promoted (open + ``auto-fix``-tagged + diagnosed) gripes, one
normalized queue via ``intake.all_items``. As a sink: today just the
report/status surface; writing back to a gripe (status flip, timeline
comment) on build is a follow-on, not this slice. The proven ``/go``
core (``scripts/ship`` + ``scripts/deploy``) is the deploy heart; this
package is the autonomous intake + verify-and-fix wrap.

Entry point: ``python -m precis.fixer.tick`` (via ``scripts/fixer-tick``).
"""

from __future__ import annotations

from precis.fixer.intake import (
    WorkItem,
    all_items,
    gripe_items,
    parse_front_matter,
    pick_next,
    ready_items,
)

__all__ = [
    "WorkItem",
    "all_items",
    "gripe_items",
    "parse_front_matter",
    "pick_next",
    "ready_items",
]
