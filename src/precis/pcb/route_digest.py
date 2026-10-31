"""The compact route-failure digest (gr513871).

The vector router's ``pcb_route`` job used to stamp a digest onto
``refs.meta.last_route.digest``; that job died with the router
(2026-10-25, tag ``vroute-final``). The topological route stage produces
digests again: :func:`build_digest` aggregates ``sketch_rows``'
``fail.problems`` into the same ``{failed, constraints}`` shape —
``meta.topo_run.route.digest`` on the job ref, ``meta.last_route.digest``
on the board ref (``view='route-status'`` renders it), ``route.json`` and
:class:`precis.pcb.local.LocalRoute` on the laptop path. ``digest_lines``
is the renderer both stamped vintage and new share.
"""

from __future__ import annotations

from typing import Any

#: Constraint lines a digest renders before truncating — the per-net table
#: below it carries the rest.
_MAX_CONSTRAINT_LINES = 8

#: One remediation line per topo ``fail.problems`` reason — the lever a
#: reader should reach for, never a re-statement of the reason itself.
_REASON_HINTS: dict[str, str] = {
    "unrouted": "pins walled off — adjust placement or pin assignment",
    "same-layer-crossing": "the crossed net owns the channel — another "
    "seed or a moved blocker part opens it",
    "no via site": "no keepout-clean via edge — relax the keepout or the via clearance",
}


def build_digest(
    rows: dict[str, dict[str, Any]],
    *,
    layer_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Aggregate ``sketch_rows`` output into the stored digest shape —
    ``{failed: [net...], constraints: [{reason, count, nets, target?,
    hint?}]}``, ``constraints`` worst-first. A ``fail.problems`` entry is
    one complaint; nets failing on the SAME blocker collapse into one
    constraint line, which is the whole point of the digest (the old
    per-net note repeated "crosses CC1 on F.Cu" 183 times).

    ``target`` is the blocker a crossing reason names most often — the
    net the failed nets are stuck on — rendered as ``<net> on <layer>``
    when ``layer_names`` resolves the problem's layer index. ``hint``
    carries :data:`_REASON_HINTS`'s lever for the reason."""
    failed = sorted(n for n, r in rows.items() if r.get("status") == "failed")
    by_reason: dict[str, dict[str, Any]] = {}
    blockers: dict[str, dict[str, int]] = {}
    for name in failed:
        for p in (rows[name].get("fail") or {}).get("problems") or []:
            reason = str(p.get("reason") or "failed")
            c = by_reason.setdefault(reason, {"reason": reason, "count": 0, "nets": []})
            c["count"] += 1
            if name not in c["nets"]:
                c["nets"].append(name)
            other = p.get("with")
            if other is not None:
                layer = p.get("layer")
                lname = (
                    str(layer_names[int(layer)])
                    if layer_names is not None
                    and isinstance(layer, int)
                    and not isinstance(layer, bool)
                    and 0 <= layer < len(layer_names)
                    else None
                )
                tgt = f"{other} on {lname}" if lname else str(other)
                seen = blockers.setdefault(reason, {})
                seen[tgt] = seen.get(tgt, 0) + 1
    constraints = []
    for reason, c in sorted(by_reason.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        c["nets"] = sorted(c["nets"])
        counts = blockers.get(reason)
        if counts:
            c["target"] = max(sorted(counts), key=lambda t: counts[t])
        hint = _REASON_HINTS.get(reason)
        if hint:
            c["hint"] = hint
        constraints.append(c)
    return {"failed": failed, "constraints": constraints}


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


__all__ = ["build_digest", "digest_lines"]
