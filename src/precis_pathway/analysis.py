"""Decision analysis over a computed reaction graph — pure, precis-free.

Operates on the ``graph`` dict a run stores (networkx ``node_link_data`` with
``edges='links'``): ``nodes`` carry ``id / rel_energy / energy_std /
low_confidence``; ``links`` carry ``source / target / barrier / barrier_std /
delta_e / kind / low_confidence``.

The point is to hand the LLM the *scalars it optimises on* — the rate-limiting
barrier and the whole-path energetic span — precomputed, so it never has to
find a max or subtract across cells.
"""

from __future__ import annotations

from collections import deque
from itertools import pairwise
from typing import Any, cast


def roots(graph: dict[str, Any], results: dict[str, Any]) -> tuple[str, str]:
    """Resolve the (root, target) node ids. The substrate *label* (``NO``) may
    not be a node — autocatpath's oxidation root node is e.g. ``NO+O`` — so the root
    is the first entry of the topological ``pathway`` order, and the target is
    the ``cfg.target`` node if present else the last path entry."""
    order = results.get("pathway", [])
    nodes = {n["id"] for n in graph.get("nodes", [])}
    root = order[0] if order else str(results.get("substrate", ""))
    tgt = results.get("target")
    target = tgt if tgt in nodes else (order[-1] if order else (tgt or ""))
    return cast(str, root), cast(str, target)


def _num(x: Any, default: float = 0.0) -> float:
    """A non-finite barrier/energy is persisted as JSON null (see
    ``precis_pathway.persist._json_finite``); coerce that null back to a numeric
    default so the graph math below never does ``float(None)``/``None > float``."""
    return default if x is None else float(x)


def _reaction_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in graph.get("links", []) if e.get("kind") != "supply"]


def _is_supply(e: dict[str, Any] | None) -> bool:
    return bool(e) and e.get("kind") == "supply"


def _node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph.get("nodes", [])}


def _edge(graph: dict[str, Any], a: str, b: str) -> dict[str, Any] | None:
    """Any edge a→b — reaction *or* supply (the path traverses both)."""
    for e in graph.get("links", []):
        if e["source"] == a and e["target"] == b:
            return e
    return None


def reaction_path(graph: dict[str, Any], root: str, target: str) -> list[str]:
    """Shortest chain of states from root to target. Traverses reaction AND
    supply edges (the +O*/+H* stoichiometry bridges are zero-barrier connectors
    that the productive path must cross); the barrier accounting later skips the
    supply steps. Returns [] if unreachable."""
    adj: dict[str, list[str]] = {}
    for e in graph.get("links", []):
        adj.setdefault(e["source"], []).append(e["target"])
    q: deque[list[str]] = deque([[root]])
    seen = {root}
    while q:
        path = q.popleft()
        if path[-1] == target:
            return path
        for nxt in adj.get(path[-1], []):
            if nxt not in seen:
                seen.add(nxt)
                q.append(path + [nxt])
    return []


def _path_steps(graph: dict[str, Any], path: list[str]) -> list[dict[str, Any]]:
    """Reaction (barrier-bearing) steps along the path — supply bridges skipped."""
    out = []
    for a, b in pairwise(path):
        e = _edge(graph, a, b)
        if e is not None and not _is_supply(e):
            out.append(e)
    return out


def rate_limiting(
    graph: dict[str, Any], root: str, target: str
) -> dict[str, Any] | None:
    """The max-barrier step on the root→target path (falls back to the whole
    network if no clean path)."""
    path = reaction_path(graph, root, target)
    steps = _path_steps(graph, path) or _reaction_edges(graph)
    if not steps:
        return None
    # A null barrier (a non-finite EMT value persisted as JSON null) sorts last.
    top = max(steps, key=lambda e: _num(e.get("barrier"), float("-inf")))
    return {
        "step": f"{top['source']}→{top['target']}",
        "ea": top.get("barrier"),
        "std": top.get("barrier_std", 0.0),
        "low_confidence": bool(top.get("low_confidence", False)),
    }


def energetic_span(graph: dict[str, Any], root: str, target: str) -> float | None:
    """Whole-path apparent barrier: the biggest climb from any intermediate to
    any *later* transition state along the path (Kozuch–Shaik energetic span).
    Can exceed every single-step Eₐ when a deep well precedes a high TS."""
    path = reaction_path(graph, root, target)
    if len(path) < 2:
        return None
    nm = _node_map(graph)
    state_e = [_num(nm.get(s, {}).get("rel_energy")) for s in path]
    span = 0.0
    min_state = state_e[0]
    for i, (a, b) in enumerate(pairwise(path)):
        e = _edge(graph, a, b)
        # supply bridges carry no barrier — no extra climb at that step.
        ea = 0.0 if (e is None or _is_supply(e)) else _num(e.get("barrier"))
        ts_energy = state_e[i] + ea  # cumulative TS height
        min_state = min(min_state, state_e[i])
        span = max(span, ts_energy - min_state)
    return span


def barriers_ranked(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """All reaction steps, highest barrier first (the drill-down table)."""
    rows = [
        {
            "reaction": f"{e['source']}→{e['target']}",
            "ea": e.get("barrier"),
            "std": e.get("barrier_std", 0.0),
            "conf": "low" if e.get("low_confidence") else "ok",
        }
        for e in _reaction_edges(graph)
    ]
    return sorted(rows, key=lambda r: (r["ea"] is None, -(r["ea"] or 0.0)))


def selectivity(graph: dict[str, Any], root: str, target: str) -> list[dict[str, Any]]:
    """Competing first steps out of the root: the target-path entry vs the
    other branches leaving root. Lower entry barrier = kinetically favored."""
    path = reaction_path(graph, root, target)
    on_path = set(pairwise(path))
    out = []
    for e in _reaction_edges(graph):
        if e["source"] != root:
            continue
        out.append(
            {
                "entry_step": f"{e['source']}→{e['target']}",
                "entry_ea": e.get("barrier"),
                "on_target_path": (e["source"], e["target"]) in on_path,
            }
        )
    return sorted(out, key=lambda r: (r["entry_ea"] is None, r["entry_ea"] or 0.0))


def profile_positions(
    graph: dict[str, Any], root: str, target: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """The reaction coordinate as an interleaved [state, ‡, state, ‡, …] list —
    the columns of the compare table. State cells hold rel energy; ‡ cells hold
    the step barrier Eₐ (so a cell is directly the number you'd compare)."""
    path = reaction_path(graph, root, target)
    nm = _node_map(graph)
    cols: list[dict[str, Any]] = []
    for i, s in enumerate(path):
        node = nm.get(s, {})
        cols.append(
            {
                "pos": f"s{i}",
                "kind": "state",
                "label": s,
                "value": node.get("rel_energy"),
                "low_confidence": bool(node.get("low_confidence")),
            }
        )
        if i < len(path) - 1:
            e = _edge(graph, s, path[i + 1])
            # supply bridge → no ‡ column (no barrier); the two states sit
            # adjacent. Only reaction steps get a barrier column.
            if e is not None and not _is_supply(e):
                cols.append(
                    {
                        "pos": f"‡{i + 1}",
                        "kind": "ts",
                        "label": f"{s}→{path[i + 1]}",
                        "value": e.get("barrier"),
                        "low_confidence": bool(e.get("low_confidence")),
                    }
                )
    return path, cols


def summarize(graph: dict[str, Any], root: str, target: str) -> dict[str, Any]:
    """The scalar bundle a leaderboard row / analysis headline needs."""
    rl = rate_limiting(graph, root, target)
    span = energetic_span(graph, root, target)
    return {
        "rate_limiting": rl,
        "span": span,
        "low_confidence": bool(rl and rl["low_confidence"]),
    }
