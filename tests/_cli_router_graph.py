"""Static call graph from each ``precis`` CLI command to the model router.

Support module for ``tests/test_cli_binds_store.py``. Stdlib ``ast`` over
``src/precis``: every module-level function and method is a node; an edge is
a name the body references that resolves, through the module's imports or its
own definitions, to another node. References to a class reach its methods
(``DispatchClient(...).complete()`` is a constructor, then a method). Calls on
values the resolver cannot type (``obj.method()``) are not followed, so the
graph under-approximates; the router sinks are matched by name, which the
callers import directly.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

#: The model router: resolving a model, a backend or a chain, or dispatching,
#: reads ``live_config`` overrides through ``budget.meter.active_store()``.
ROUTER_SINKS = frozenset(
    {
        "precis.utils.llm.router.route",
        "precis.utils.llm.router.resolve_model",
        "precis.utils.llm.router.resolve_backend",
        "precis.utils.llm.router.resolve_chain",
        "precis.utils.llm.router.resolve_selection",
        "precis.utils.llm.router.DispatchClient",
    }
)

#: Binding the process store for the router: the budget meter is the channel
#: ``live_config`` reads (``precis.budget`` re-exports ``meter.bind_store``).
BIND_SINKS = frozenset(
    {
        "precis.budget.bind_store",
        "precis.budget.meter.bind_store",
    }
)


@dataclass
class _Module:
    name: str
    tree: ast.Module
    #: local alias -> fully qualified target (module or module.attr)
    imports: dict[str, str] = field(default_factory=dict)
    #: qualnames defined here: "f", "Cls", "Cls.m"
    defs: dict[str, ast.AST] = field(default_factory=dict)


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(mod: str, is_pkg: bool, level: int, target: str | None) -> str:
    if level == 0:
        return target or ""
    base = mod.split(".")
    if not is_pkg:
        base = base[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, target] if target else base)


def _collect_imports(m: _Module, is_pkg: bool) -> None:
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    m.imports[a.asname] = a.name
                else:
                    m.imports[a.name.split(".")[0]] = a.name.split(".")[0]
                    m.imports[a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(m.name, is_pkg, node.level, node.module)
            for a in node.names:
                m.imports[a.asname or a.name] = f"{base}.{a.name}"


def _collect_defs(m: _Module) -> None:
    for node in m.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m.defs[node.name] = node
        elif isinstance(node, ast.ClassDef):
            m.defs[node.name] = node
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m.defs[f"{node.name}.{sub.name}"] = sub


@cache
def modules() -> dict[str, _Module]:
    out: dict[str, _Module] = {}
    for path in sorted((SRC / "precis").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        m = _Module(_module_name(path), tree)
        _collect_imports(m, path.name == "__init__.py")
        _collect_defs(m)
        out[m.name] = m
    return out


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _canonical(fq: str) -> str:
    """Follow package re-exports (``precis.budget.bind_store`` is
    ``precis.budget.meter.bind_store``) to the defining module."""
    mods = modules()
    for _ in range(8):
        mod, _, attr = fq.rpartition(".")
        m = mods.get(mod)
        if m is None or not attr or attr in m.defs:
            return fq
        nxt = m.imports.get(attr)
        if nxt is None or nxt == fq:
            return fq
        fq = nxt
    return fq


def _resolve(m: _Module, owner: str | None, dotted: str) -> str | None:
    """Fully qualified node name for a dotted reference inside ``m``."""
    head, _, rest = dotted.partition(".")
    if head == "self" and owner and rest:
        return f"{m.name}.{owner}.{rest.split('.')[0]}"
    if head in m.defs and not rest:
        return f"{m.name}.{head}"
    if head in m.defs and rest and f"{head}.{rest}" in m.defs:
        return f"{m.name}.{head}.{rest}"
    target = m.imports.get(head)
    if target is None:
        return None
    return _canonical(f"{target}.{rest}" if rest else target)


@cache
def _references(fq: str) -> frozenset[str]:
    """Fully qualified names the body of node ``fq`` references."""
    m, qual = _split(fq)
    if m is None or qual is None:
        return frozenset()
    node = m.defs[qual]
    owner = (
        qual.split(".")[0] if "." in qual or isinstance(node, ast.ClassDef) else None
    )
    if isinstance(node, ast.ClassDef):
        return frozenset(
            f"{m.name}.{q}" for q in m.defs if q.startswith(f"{node.name}.")
        )
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Name, ast.Attribute)) and isinstance(
            getattr(sub, "ctx", None), ast.Load
        ):
            d = _dotted(sub)
            if d:
                r = _resolve(m, owner, d)
                if r:
                    out.add(r)
    return frozenset(out)


def _split(fq: str) -> tuple[_Module | None, str | None]:
    mods = modules()
    parts = fq.split(".")
    for i in range(len(parts) - 1, 0, -1):
        m = mods.get(".".join(parts[:i]))
        if m is not None:
            qual = ".".join(parts[i:])
            return (m, qual) if qual in m.defs else (m, None)
    return None, None


def reaches(start: Iterable[str], sinks: frozenset[str]) -> list[str] | None:
    """A path from any of ``start`` to a sink, or None."""
    prev: dict[str, str | None] = {s: None for s in start}
    queue = list(prev)
    while queue:
        cur = queue.pop(0)
        if cur in sinks:
            path = [cur]
            while (p := prev[path[-1]]) is not None:
                path.append(p)
            return path[::-1]
        for nxt in sorted(_references(cur)):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    return None


def _header_exprs(stmt: ast.stmt) -> Iterator[ast.AST]:
    """A statement's own expressions, not those of statements nested in it."""
    for _name, value in ast.iter_fields(stmt):
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, (ast.expr, ast.withitem, ast.keyword, ast.arguments)):
                yield item


def statements(fq: str) -> Iterator[tuple[int, set[str]]]:
    """Every statement in function ``fq`` (nested ones too), in source order,
    as ``(line, names its own expressions reference)``."""
    m, qual = _split(fq)
    assert m is not None and qual is not None, fq
    node = m.defs[qual]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), fq
    owner = qual.split(".")[0] if "." in qual else None
    stmts = [s for s in ast.walk(node) if isinstance(s, ast.stmt) and s is not node]
    stmts.sort(key=lambda s: (s.lineno, s.col_offset))
    for stmt in stmts:
        refs: set[str] = set()
        for expr in _header_exprs(stmt):
            for sub in ast.walk(expr):
                if isinstance(sub, (ast.Name, ast.Attribute)) and isinstance(
                    getattr(sub, "ctx", None), ast.Load
                ):
                    d = _dotted(sub)
                    if d and (r := _resolve(m, owner, d)):
                        refs.add(r)
        yield stmt.lineno, refs


def is_function(fq: str) -> bool:
    m, qual = _split(fq)
    return (
        m is not None
        and qual is not None
        and isinstance(m.defs[qual], (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def unbound_router_call(fq: str, depth: int = 6) -> str | None:
    """None when function ``fq`` binds the store before anything it runs
    reaches the router; else the first offending reference, as
    ``"<line>: <path to the router>"``. A callee that reaches the router is
    accepted when it binds first itself (``build_runtime`` does)."""
    return _unbound(fq, depth)


@cache
def _unbound(fq: str, depth: int) -> str | None:
    bound = False
    for line, refs in statements(fq):
        if refs & BIND_SINKS:
            bound = True
            continue
        for ref in sorted(refs):
            path = reaches([ref], ROUTER_SINKS)
            if path is None:
                if not bound and reaches([ref], BIND_SINKS):
                    bound = True  # a binding helper (bind_process_store)
                continue
            if bound:
                continue
            if depth > 0 and is_function(ref) and _unbound(ref, depth - 1) is None:
                bound = True
                continue
            return f"{line}: {' -> '.join(path)}"
    return None
