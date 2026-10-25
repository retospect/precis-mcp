"""The compact route-failure digest (gr513871).

A route run reports failures two ways today: the per-net ``pcb_routes``
rows (``fail.problems`` + the ``note`` sentence) and the job summary's
unrouted lines. Both name ONE problem at a time, so a board that fails
8 nets on the same blocked corridor reads as 8 repetitions of the same
crossing — "crosses CC1 on F.Cu" 183 times. This module aggregates the
already-built ``problems`` into the run-level view: which binding
constraints failed the run, which nets each one blocks, and a fix hint
where the reason maps to a knob (``search_budget`` → the realizer's
``max_expansions``).

The digest is plain JSON — the ``pcb_route`` job stamps it onto
``refs.meta.last_route.digest`` (the run's DB structure),
``view='route-status'`` renders it ahead of the per-net table, and the
offline :mod:`precis.pcb.local` result carries the same object.
"""

from __future__ import annotations

import collections
from typing import Any

#: Reason → the knob or next move a reader should try. Only constraints
#: where naming the parameter genuinely helps get a hint — "no path"
#: already says all there is to say.
_REASON_HINTS: dict[str, str] = {
    "search_budget": (
        "a corridor exists but the search ran out of expansion budget — "
        "raise the router's max_expansions"
    ),
    "congestion": (
        "a gap is over capacity — widen the corridor or give the net "
        "another layer (net_class layers / a pour)"
    ),
    "width": "the required trace width fits nowhere on the allowed layers",
}

#: Constraint lines a digest renders before truncating — the per-net table
#: below it carries the rest.
_MAX_CONSTRAINT_LINES = 8


def dedupe_details(details: list[str]) -> list[str]:
    """Collapse repeated detail strings to ``"text ×N"``, first-seen order
    preserved. The per-net note's job is the DISTINCT blockers — the same
    crossing on 183 segments was the output the gripe quotes."""
    counts = collections.Counter(details)
    return [f"{d} ×{n}" if n > 1 else d for d, n in counts.items()]


def _problem_target(p: dict[str, Any], layer_names: list[str]) -> str | None:
    """The binding object a problem names: the OTHER net+layer a crossing
    collides with. None for constraint kinds that bind to the board, not a
    peer (search_budget, width, disconnected)."""
    if p.get("with") is None:
        # LocalRouting's crossing problems carry only the rendered
        # message ("crosses A, B"); it is still the target text.
        if p.get("reason") == "same-layer-crossing" and p.get("message"):
            return str(p["message"])
        return None
    layer = p.get("layer")
    if isinstance(layer, int) and 0 <= layer < len(layer_names):
        lname = layer_names[layer]
    elif layer not in (None, -1):
        lname = str(layer)
    else:
        lname = ""
    return str(p["with"]) + (f" on {lname}" if lname else "")


def conflict_digest(
    rows: dict[str, dict[str, Any]], layer_names: list[str] | None = None
) -> dict[str, Any]:
    """Aggregate every failed net's ``fail.problems`` into run-level
    constraints — ``{failed: [net, ...], constraints: [...]}`` sorted by
    hit count. Each constraint is ``{reason, target, count, nets, hint?}``:
    ``count`` is how many problem entries hit it and ``nets`` the distinct
    failed nets it blocks."""
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    failed: list[str] = []
    names = list(layer_names or [])
    for net, row in rows.items():
        if row.get("status") != "failed":
            continue
        failed.append(str(net))
        for p in (row.get("fail") or {}).get("problems") or []:
            reason = str(p.get("reason") or p.get("kind") or "unknown")
            key = (reason, _problem_target(p, names))
            g = groups.setdefault(
                key, {"reason": reason, "target": key[1], "count": 0, "nets": set()}
            )
            g["count"] += 1
            g["nets"].add(str(net))
    constraints = []
    for g in groups.values():
        entry: dict[str, Any] = {
            "reason": g["reason"],
            "target": g["target"],
            "count": g["count"],
            "nets": sorted(g["nets"]),
        }
        hint = _REASON_HINTS.get(g["reason"])
        if hint is not None:
            entry["hint"] = hint
        constraints.append(entry)
    constraints.sort(key=lambda c: (-c["count"], c["reason"], str(c["target"])))
    return {"failed": sorted(failed), "constraints": constraints}


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


__all__ = ["conflict_digest", "dedupe_details", "digest_lines"]
