"""Runtime call-graph capture for ``view='runtrace'``.

Three responsibilities:

1. **Harness**: spawn ``_python_runtrace_runner.py`` in a subprocess
   with a configured timeout, forward argv/env, read back the JSON
   trace.
2. **Tree builder**: turn a flat call/return event stream into a
   ``TraceNode`` tree with multiplicities and per-node total time.
3. **Renderer**: format the tree (matching the static callgraph's
   visual style) and append a ``Static-only`` diff against the
   static call set rooted at the same entry.

This module is **gated** by the python handler — it should only ever
run after ``PRECIS_PYTHON_ALLOW_EXEC=1`` is verified. The gate check
itself lives in `python.py` so the error path stays close to the
agent-facing surface.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from precis.python_index import RepoIndex

log = logging.getLogger(__name__)


_RUNNER_SCRIPT = Path(__file__).parent / "_python_runtrace_runner.py"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One profiler event."""

    event: str  # 'call' / 'return' / 'c_call' / 'c_return'
    qn: str
    t: float  # seconds since trace start


@dataclass(frozen=True, slots=True)
class TraceResult:
    """Outcome of one runtrace run.

    ``ok=False`` covers timeouts, runner failures, and entry import
    errors. Even when ``ok=False`` we may still have partial events
    and a non-empty ``error`` describing what went wrong.
    """

    ok: bool
    events: tuple[TraceEvent, ...]
    truncated: bool
    exit_code: int | None
    elapsed_s: float
    error: str | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class TraceNode:
    """One row in the runtime tree.

    `multiplicity` collapses consecutive sibling calls to the same
    qualname (so a loop calling `helper()` 23 times becomes one node
    with `multiplicity=23` rather than 23 leaf rows).
    """

    qualname: str
    multiplicity: int = 1
    total_ns: float = 0.0
    is_c: bool = False
    children: list[TraceNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def run_trace(
    *,
    entry: str,
    argv: list[str] | None = None,
    cwd: Path | None = None,
    timeout: int = 10,
    env: dict[str, str] | None = None,
    syspath: list[Path] | None = None,
    max_events: int = 10_000,
) -> TraceResult:
    """Spawn the runner subprocess and return the captured trace.

    Args:
        entry: ``pkg.mod:func`` or ``pkg.mod.func``.
        argv: Forwarded to the entry as ``sys.argv[1:]``.
        cwd: Working directory for the subprocess (default: current).
        timeout: Hard kill-after seconds.
        env: Override / extend ``os.environ`` for the subprocess.
        syspath: Extra ``sys.path`` entries the runner prepends before
            importing the entry. Use to make a configured python-kind
            root importable.
        max_events: Truncate the event stream past this many events to
            bound JSON size.

    Returns a `TraceResult`. Never raises.
    """
    argv = list(argv or [])

    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    # Temp file for the output JSON. Use a context-managed dir so we
    # clean up even on subprocess crashes.
    with tempfile.TemporaryDirectory(prefix="precis-runtrace-") as tmpdir:
        out_path = Path(tmpdir) / "trace.json"

        cmd: list[str] = [
            sys.executable,
            str(_RUNNER_SCRIPT),
            "--entry",
            entry,
            "--output",
            str(out_path),
            "--max-events",
            str(max_events),
        ]
        if syspath:
            cmd.extend(["--syspath", os.pathsep.join(str(p) for p in syspath)])
        cmd.append("--")
        cmd.extend(argv)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd else None,
                env=proc_env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            return TraceResult(
                ok=False,
                events=tuple(),
                truncated=False,
                exit_code=None,
                elapsed_s=float(timeout),
                error=f"timeout after {timeout}s",
                stdout=(e.stdout or b"").decode("utf-8", "replace")
                if isinstance(e.stdout, bytes)
                else (e.stdout or ""),
                stderr=(e.stderr or b"").decode("utf-8", "replace")
                if isinstance(e.stderr, bytes)
                else (e.stderr or ""),
            )
        except OSError as e:
            return TraceResult(
                ok=False,
                events=tuple(),
                truncated=False,
                exit_code=None,
                elapsed_s=0.0,
                error=f"runner spawn failed: {e}",
            )

        # Read whatever the runner wrote (it writes even on import errors).
        events: list[TraceEvent] = []
        truncated = False
        exception: str | None = None
        elapsed_s = 0.0
        if out_path.is_file():
            try:
                payload = json.loads(out_path.read_text(encoding="utf-8"))
                events = [TraceEvent(**e) for e in payload.get("events", [])]
                truncated = bool(payload.get("truncated", False))
                exception = payload.get("exception")
                elapsed_s = float(payload.get("elapsed_s", 0.0))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                log.warning("malformed runtrace output: %s", e)

        ok = result.returncode == 0 and exception is None
        error: str | None = None
        if not ok:
            if exception is not None:
                error = exception
            elif result.returncode != 0:
                error = f"runner exit code {result.returncode}"

        return TraceResult(
            ok=ok,
            events=tuple(events),
            truncated=truncated,
            exit_code=result.returncode,
            elapsed_s=elapsed_s,
            error=error,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def build_tree(events: tuple[TraceEvent, ...]) -> TraceNode | None:
    """Turn a flat call/return event stream into a `TraceNode` tree.

    Coalesces consecutive sibling calls to the same qualname into a
    single node with bumped `multiplicity`. Per-node `total_ns` sums
    every span (call→return).

    Returns None if the event stream is empty or the first event isn't
    a call. Tolerates unbalanced events (extra returns, etc.) by
    skipping them — partial traces from timeouts still render.
    """
    if not events:
        return None

    root: TraceNode | None = None
    stack: list[tuple[TraceNode, float]] = []  # (node, call_time)

    for ev in events:
        if ev.event in ("call", "c_call"):
            is_c = ev.event == "c_call"
            if not stack:
                if root is not None:
                    # Already finished the root; ignore stragglers.
                    continue
                node = TraceNode(qualname=ev.qn, is_c=is_c)
                root = node
                stack.append((node, ev.t))
                continue

            parent_node, _ = stack[-1]
            # Coalesce consecutive same-qualname siblings.
            if (
                parent_node.children
                and parent_node.children[-1].qualname == ev.qn
                and parent_node.children[-1].is_c == is_c
            ):
                last = parent_node.children[-1]
                last.multiplicity += 1
                stack.append((last, ev.t))
            else:
                node = TraceNode(qualname=ev.qn, is_c=is_c)
                parent_node.children.append(node)
                stack.append((node, ev.t))

        elif ev.event in ("return", "c_return"):
            if not stack:
                continue
            top_node, call_t = stack.pop()
            # Tolerate unbalanced returns (e.g. the qualname doesn't
            # match the top of stack — possible with C builtins that
            # don't emit c_return on exception).
            if top_node.qualname != ev.qn:
                # Best-effort — still record elapsed.
                pass
            top_node.total_ns += (ev.t - call_t) * 1e9

    return root


def collect_runtime_qualnames(root: TraceNode | None) -> set[str]:
    """Return every qualname touched in a runtime tree.

    Used to compute the `Static-only` diff against the static
    callgraph's reachable set.
    """
    if root is None:
        return set()
    out: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        out.add(node.qualname)
        stack.extend(node.children)
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_runtrace(
    *,
    alias: str,
    entry: str,
    argv: list[str],
    result: TraceResult,
    tree: TraceNode | None,
    static_only: list[str] | None = None,
) -> str:
    """Format a `TraceResult` for the agent.

    Mirrors the static `callgraph` view's box-drawn tree style so the
    two are visually comparable. Adds total-time and multiplicity
    annotations. Appends a ``Static-only`` section listing qualnames
    the static graph reached but the runtime didn't.
    """
    n_calls = sum(1 for e in result.events if e.event in ("call", "c_call"))
    argv_repr = " ".join(argv) if argv else ""
    elapsed_ms = result.elapsed_s * 1000.0

    flags: list[str] = []
    if result.truncated:
        flags.append("truncated")
    if not result.ok:
        flags.append(f"failed: {result.error or 'unknown'}")
    flags_str = ", " + ", ".join(flags) if flags else ""

    header = (
        f"# Runtime trace of {alias}::{entry}"
        + (f" {argv_repr}" if argv_repr else "")
        + f"  ({n_calls} calls, {elapsed_ms:.1f}ms{flags_str})"
    )
    lines = [header, ""]

    if tree is None:
        lines.append("(no events captured)")
    else:
        lines.append(_render_node(tree))
        _render_children(tree.children, prefix="", out=lines)

    if static_only:
        lines.append("")
        lines.append("Static-only (not exercised this run):")
        # Collapse to ~6 per line, alphabetised.
        sorted_static = sorted(set(static_only))
        per_line = 4
        for i in range(0, len(sorted_static), per_line):
            chunk = ", ".join(sorted_static[i : i + per_line])
            lines.append(f"  {chunk}")

    if result.stderr.strip() and not result.ok:
        lines.append("")
        lines.append("Stderr (last 5 lines):")
        for ln in result.stderr.strip().splitlines()[-5:]:
            lines.append(f"  {ln}")

    lines.append("")
    lines.append("Next:")
    lines.append(
        f"  get(kind='python', id={alias!r}, view='callgraph', "
        f"args={{'entry': {entry!r}}})"
    )
    if not result.ok:
        lines.append("  # gate check: export PRECIS_PYTHON_ALLOW_EXEC=1 if missing")

    return "\n".join(lines)


def _render_node(node: TraceNode) -> str:
    """Format one row's content (without tree glyphs)."""
    parts = [node.qualname]
    if node.multiplicity > 1:
        parts.append(f"{node.multiplicity}×")
    ms = node.total_ns / 1e6
    if ms >= 0.05:
        parts.append(f"{ms:.1f}ms")
    if node.is_c:
        parts.append("[ext]")
    return "  ".join(parts)


def _render_children(children: list[TraceNode], *, prefix: str, out: list[str]) -> None:
    n = len(children)
    for i, child in enumerate(children):
        is_last = i == n - 1
        glyph = "└── " if is_last else "├── "
        out.append(f"{prefix}{glyph}{_render_node(child)}")
        next_prefix = prefix + ("    " if is_last else "│   ")
        _render_children(child.children, prefix=next_prefix, out=out)


# ---------------------------------------------------------------------------
# Static-vs-runtime diff
# ---------------------------------------------------------------------------


def static_only_qualnames(
    *,
    idx: RepoIndex,
    entry_qualname: str,
    runtime_qualnames: set[str],
    max_results: int = 30,
) -> list[str]:
    """Return qualnames the static graph would walk from `entry_qualname`
    but the runtime trace didn't touch.

    Walks the same `caller→callees` index the static callgraph view
    uses (transitively, breadth-first, capped). Filters the result
    against `runtime_qualnames` so the agent sees only the *new*
    static-only edges.
    """
    # Pre-build caller→callees lookup. Same shape as
    # _python_callgraph._index_calls but inlined here to avoid a
    # cross-module import that pulls cgraph rendering into the path.
    callees_by_caller: dict[str, list[str]] = defaultdict(list)
    for mod in idx.modules.values():
        for edge in mod.calls:
            callees_by_caller[edge.caller].append(edge.callee)

    visited: set[str] = {entry_qualname}
    queue: list[str] = [entry_qualname]
    static_set: set[str] = set()
    while queue and len(static_set) < max_results * 4:
        cur = queue.pop(0)
        for callee in callees_by_caller.get(cur, []):
            # Skip ext: edges and unresolved cross-repo names — they're
            # noise in the diff (the runtime saw them under a different
            # qualname or as C calls).
            if callee.startswith("ext:"):
                continue
            if callee in visited:
                continue
            visited.add(callee)
            static_set.add(callee)
            queue.append(callee)

    only = sorted(static_set - runtime_qualnames)
    return only[:max_results]
