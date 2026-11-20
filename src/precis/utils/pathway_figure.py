"""Stored-network selection shared by pathway text and computed figures.

Selection never solves kinetics. Supply bridges participate in route finding
before display pruning, so hiding them cannot change the productive route.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

from precis.utils.reaction_graph import reaction_path

OPTIONS = frozenset(
    {
        "route_only",
        "top_n_barriers",
        "hide_supply",
        "label_threshold_eV",
        "U",
        "size",
        "kinetics",
    }
)


def figure_options(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate bounded rendering options; omit absent keys to retain defaults."""
    out = {k: v for k, v in raw.items() if k in OPTIONS}
    for k in ("route_only", "hide_supply", "kinetics"):
        if k in out and not isinstance(out[k], bool):
            raise ValueError(f"{k} must be boolean")
    if "top_n_barriers" in out:
        n = out["top_n_barriers"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000:
            raise ValueError("top_n_barriers must be an integer in [1, 1000]")
    for k in ("U", "label_threshold_eV"):
        if k in out:
            v = out[k]
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
            ):
                raise ValueError(f"{k} must be finite")
            if k == "label_threshold_eV" and v < 0:
                raise ValueError("label_threshold_eV must be nonnegative")
    if "size" in out:
        size = out["size"]
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or not 1 <= v <= 24
                for v in size
            )
        ):
            raise ValueError("size must be [width, height] in inches, each in [1, 24]")
        out["size"] = list(size)
    return out


def selected_graph(
    graph: dict[str, Any], root: str, target: str, **options: Any
) -> dict[str, Any]:
    """Copy a graph with route/barrier/supply pruning and optional CHE shift."""
    options = figure_options(options)
    links = list(graph.get("links") or [])
    if options.get("route_only"):
        path = reaction_path(graph, root, target)
        pairs = set(pairwise(path))
        links = [e for e in links if (e.get("source"), e.get("target")) in pairs]
    if options.get("hide_supply"):
        links = [e for e in links if e.get("kind") != "supply"]
    if n := options.get("top_n_barriers"):
        ranked = sorted(
            (e for e in links if isinstance(e.get("barrier"), (int, float))),
            key=lambda e: e["barrier"],
            reverse=True,
        )[:n]
        links = [e for e in links if e in ranked]
    ids = {e[k] for e in links for k in ("source", "target")}
    prune = any(options.get(k) for k in ("route_only", "hide_supply", "top_n_barriers"))
    nodes = [
        dict(n) for n in graph.get("nodes") or [] if not prune or n.get("id") in ids
    ]
    if options.get("U") is not None:
        for node in nodes:
            if node.get("n_H") is not None and node.get("rel_energy") is not None:
                node["rel_energy"] += node["n_H"] * options["U"]
    return {**graph, "nodes": nodes, "links": links}
