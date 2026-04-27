"""Static call-graph view for ``PythonHandler``.

`build_callgraph` walks `CallEdge` records starting from one entry
qualname, following resolved callees up to a configured depth. The
result is rendered as a box-drawn tree.

Cycle handling:

- **On-path revisit**  (a callee is an ancestor in the current branch)
  → emitted as ``… (cycle)`` and not expanded again.
- **Already-shown** (callee was expanded earlier elsewhere in the tree)
  → emitted as ``… (see above)`` and not re-expanded.
- **Depth cap reached** → ``…`` and stop.

Cross-repo: when ``cross_repo=True`` and ``other_repos`` is non-empty,
unresolved callees (those whose qualnames don't live in the entry
repo) are looked up in each other repo in order. The first hit is used
and tagged with the alias (e.g. ``[other-repo]``); a miss falls back
to ``[ext]``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from precis.python_index import CallEdge, RepoIndex

# ---------------------------------------------------------------------------
# Tree node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Node:
    """One row in the call-graph tree.

    `label` is the qualname (or ``ext:<name>``) for the callee.
    `tag` is a short bracketed annotation rendered in the right column
    (``[ext]``, ``[<repo-alias>]``, ``[cycle]``, ``[see above]``,
    ``[truncated]``, etc.). `children` is empty when truncated or
    revisited.
    """

    label: str
    tag: str = ""
    children: list[_Node] = field(default_factory=list)
    multiplicity: int = 1  # collapsed dup-call count at this level


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_callgraph(
    idx: RepoIndex,
    *,
    entry: str,
    max_depth: int = 3,
    other_repos: dict[str, RepoIndex] | None = None,
    cross_repo: bool = False,
) -> _Node:
    """Build a `_Node` tree rooted at `entry`.

    `entry` accepts either ``module:function`` (per spec) or the dotted
    form ``module.function`` directly. `max_depth` is enforced strictly
    — a depth-3 graph shows the root + 3 levels of callees.

    Raises ``ValueError`` if `entry` doesn't resolve to a known
    qualname in `idx`.
    """
    entry_qn = entry.replace(":", ".")
    if idx.symbol(entry_qn) is None:
        raise ValueError(f"entry {entry!r} not found in repo")

    # Pre-build callers→edges index for the entry repo. O(N) once,
    # avoids repeated O(N) scans during recursion. Edges are coalesced
    # by callee so we can render multiplicity.
    edges_by_caller = _index_calls(idx)

    other_indexes: list[tuple[str, RepoIndex, dict[str, list[CallEdge]]]] = []
    if cross_repo and other_repos:
        for alias, other in other_repos.items():
            other_indexes.append((alias, other, _index_calls(other)))

    visited: set[str] = set()  # any qualname we've already expanded once
    root = _Node(label=entry_qn)
    visited.add(entry_qn)
    _expand(
        node=root,
        qualname=entry_qn,
        idx=idx,
        edges_by_caller=edges_by_caller,
        depth_remaining=max_depth,
        path={entry_qn},
        visited=visited,
        other_indexes=other_indexes,
    )
    return root


def _index_calls(idx: RepoIndex) -> dict[str, list[CallEdge]]:
    """Pre-build a `caller_qualname → [CallEdge]` lookup.

    For class qualnames we also include edges from the class's methods
    (so `expand(C)` shows the union of every method's calls). This
    matches what an agent expects when drilling into a class node from
    the entry of a callgraph rooted on a class.
    """
    by_caller: dict[str, list[CallEdge]] = defaultdict(list)
    for mod in idx.modules.values():
        for edge in mod.calls:
            by_caller[edge.caller].append(edge)

    # Add class-level aggregations: every class qualname maps to the
    # union of its methods' call edges.
    for mod in idx.modules.values():
        for sym in mod.symbols:
            if sym.kind == "class":
                pref = sym.qualname + "."
                aggregated: list[CallEdge] = []
                for caller, calls in list(by_caller.items()):
                    if caller.startswith(pref):
                        aggregated.extend(calls)
                if aggregated:
                    by_caller[sym.qualname] = aggregated

    return dict(by_caller)


def _expand(
    *,
    node: _Node,
    qualname: str,
    idx: RepoIndex,
    edges_by_caller: dict[str, list[CallEdge]],
    depth_remaining: int,
    path: set[str],
    visited: set[str],
    other_indexes: list[tuple[str, RepoIndex, dict[str, list[CallEdge]]]],
) -> None:
    """Recursively populate `node.children` by following call edges."""
    if depth_remaining <= 0:
        if edges_by_caller.get(qualname):
            node.children.append(_Node(label="…", tag="truncated"))
        return

    edges = edges_by_caller.get(qualname, [])
    if not edges:
        return

    # Coalesce by callee so dup edges become multiplicity counts.
    counts: dict[str, int] = defaultdict(int)
    order: list[str] = []
    for edge in edges:
        if edge.callee not in counts:
            order.append(edge.callee)
        counts[edge.callee] += 1

    for callee in order:
        hits = counts[callee]
        if callee.startswith("ext:"):
            node.children.append(_Node(label=callee[4:], tag="ext", multiplicity=hits))
            continue

        # Same-repo callee?
        local_sym = idx.symbol(callee)
        cross_alias: str | None = None
        cross_idx: RepoIndex | None = None
        cross_edges: dict[str, list[CallEdge]] | None = None
        if local_sym is None:
            # Try cross-repo resolution.
            for alias, other_idx, other_edges in other_indexes:
                if other_idx.symbol(callee) is not None:
                    cross_alias = alias
                    cross_idx = other_idx
                    cross_edges = other_edges
                    break
            if cross_alias is None:
                # Unresolved cross-repo / external — surface as ext.
                node.children.append(_Node(label=callee, tag="ext", multiplicity=hits))
                continue

        # On-path cycle?
        if callee in path:
            node.children.append(_Node(label=callee, tag="cycle", multiplicity=hits))
            continue

        # Already shown elsewhere?
        if callee in visited:
            node.children.append(
                _Node(label=callee, tag="see above", multiplicity=hits)
            )
            continue

        child_tag = f"{cross_alias}" if cross_alias else ""
        child = _Node(label=callee, tag=child_tag, multiplicity=hits)
        node.children.append(child)
        visited.add(callee)

        # Recurse using the right index for this callee.
        sub_idx = cross_idx if cross_idx is not None else idx
        sub_edges = cross_edges if cross_edges is not None else edges_by_caller
        _expand(
            node=child,
            qualname=callee,
            idx=sub_idx,
            edges_by_caller=sub_edges,
            depth_remaining=depth_remaining - 1,
            path=path | {callee},
            visited=visited,
            other_indexes=other_indexes,
        )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_callgraph(
    root: _Node,
    *,
    alias: str,
    entry: str,
    max_depth: int,
    cross_repo: bool,
) -> str:
    """Render a built `_Node` tree as a string body."""
    header = (
        f"# Static call graph from {alias}::{entry}  "
        f"(depth={max_depth}{', cross-repo' if cross_repo else ''})\n"
    )
    lines = [header, _render_label(root)]
    _render_children(root.children, prefix="", out=lines)

    # Legend covers what tags can appear.
    if _tree_uses_tags(root, {"ext", "cycle", "see above", "truncated"}):
        lines.append("")
        lines.append("Legend:")
        lines.append("  [ext]         unresolved (stdlib / third-party / dynamic)")
        lines.append("  [cycle]       already on path to root — recursion stopped")
        lines.append("  [see above]   already expanded earlier in the tree")
        lines.append("  [truncated]   depth limit reached")

    lines.append("")
    lines.append("Next:")
    lines.append(
        f"  get(kind='python', id={alias!r}, view='callgraph', "
        f"entry={entry!r}, depth={max_depth + 2})"
    )
    return "\n".join(lines)


def _render_label(node: _Node) -> str:
    """Compose `<label>  [tag]  Nx` for one node."""
    parts: list[str] = [node.label]
    if node.tag:
        parts.append(f"[{node.tag}]")
    if node.multiplicity > 1:
        parts.append(f"{node.multiplicity}×")
    return "  ".join(parts)


def _render_children(children: list[_Node], *, prefix: str, out: list[str]) -> None:
    """Append tree-glyph-prefixed lines for `children` to `out`."""
    n = len(children)
    for i, child in enumerate(children):
        is_last = i == n - 1
        glyph = "└── " if is_last else "├── "
        out.append(f"{prefix}{glyph}{_render_label(child)}")
        next_prefix = prefix + ("    " if is_last else "│   ")
        _render_children(child.children, prefix=next_prefix, out=out)


def _tree_uses_tags(root: _Node, watched: set[str]) -> bool:
    """True iff any node in the tree has a tag in `watched`."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.tag in watched:
            return True
        stack.extend(n.children)
    return False
