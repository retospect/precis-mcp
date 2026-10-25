"""The compact route-failure digest (gr513871) — stored-data renderer.

Routing is retired (the vector router was deleted 2026-10-25, tag
``vroute-final``), so nothing produces a NEW digest: the ``pcb_route``
job used to stamp one onto ``refs.meta.last_route.digest``. This module
keeps only the read half — ``view='route-status'`` renders a stored
digest ahead of the per-net table for boards routed before the cut.
"""

from __future__ import annotations

from typing import Any

#: Constraint lines a digest renders before truncating — the per-net table
#: below it carries the rest.
_MAX_CONSTRAINT_LINES = 8


def _capped(names: list[str], keep: int) -> str:
    shown = ", ".join(names[:keep])
    rest = len(names) - keep
    return f"{shown} +{rest} more" if rest > 0 else shown


def digest_lines(digest: dict[str, Any] | None) -> list[str]:
    """The rendered block — a header naming the failed nets, then one line
    per binding constraint, worst first. Empty when nothing failed."""
    if not digest:
        return []
    failed = [str(n) for n in digest.get("failed") or []]
    constraints = list(digest.get("constraints") or [])
    if not failed:
        return []
    lines = [f"route digest — {len(failed)} failed net(s): {_capped(failed, 8)}"]
    for c in constraints[:_MAX_CONSTRAINT_LINES]:
        line = f"- {c['reason']} ×{c['count']}"
        if c.get("target"):
            line += f": {c['target']}"
        line += f" (nets: {_capped([str(n) for n in c['nets']], 4)})"
        if c.get("hint"):
            line += f" — {c['hint']}"
        lines.append(line)
    if len(constraints) > _MAX_CONSTRAINT_LINES:
        lines.append(
            f"… and {len(constraints) - _MAX_CONSTRAINT_LINES} more constraint(s)"
        )
    return lines


__all__ = ["digest_lines"]
