"""Reverse lookups for `precis.handlers.python.PythonHandler`.

`view='callers'`, `view='importers'`, `view='imports'` and
`search(mode='pattern')` — all answered from the in-memory index
(`CallEdge`, `ModuleIndex.imports`, `Symbol.decorators` / `is_async`),
with no new stored field and no re-parse.

Resolution is exactly as good as the index: names bound at module scope,
imports, `self` / `cls`. No type inference, no MRO walk.
"""

from __future__ import annotations

import re

from precis.errors import BadInput
from precis.python_index import ModuleIndex, RepoIndex, Symbol

_CAP = 50


def _import_target(value: str, idx: RepoIndex) -> str:
    """Resolve an `imports` value to the in-repo module it points into.

    `from pkg.mod import Name` binds `pkg.mod.Name`; the longest prefix
    that is an indexed module wins (`pkg.mod`). Values with no in-repo
    prefix (stdlib / third party) are returned unchanged.
    """
    parts = value.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in idx.modules:
            return cand
    return value


def render_callers(alias: str, sym: Symbol, idx: RepoIndex) -> str:
    """Call sites that reference `sym`, one row per `CallEdge`.

    Exact rows come from edges resolved to `sym.qualname` (or a member
    of it, for a class). A second, labelled section lists unresolved
    edges whose final name segment matches (`handler.search(...)` where
    the receiver's type is not tracked): a lexical lead, not a resolution.
    """
    prefix = sym.qualname + "."
    exact: list[tuple[str, str, int, str]] = []
    lexical: list[tuple[str, str, int, str]] = []
    for mod in idx.modules.values():
        for e in mod.calls:
            row = (e.caller, e.file, e.line, e.callee)
            if e.callee == sym.qualname or e.callee.startswith(prefix):
                if e.caller == sym.qualname or e.caller.startswith(prefix):
                    continue  # self-call inside the symbol
                exact.append(row)
            elif (
                sym.kind in ("function", "method")
                and e.callee.rsplit(".", 1)[-1] == sym.name
                and "." in e.callee.removeprefix("ext:")
            ):
                lexical.append(row)
    exact.sort(key=lambda r: (r[1], r[2]))
    lexical.sort(key=lambda r: (r[1], r[2]))

    lines = [f"# callers of {sym.qualname}  ({len(exact)} resolved)\n"]
    if exact:
        for caller, file, line, _ in exact[:_CAP]:
            lines.append(f"  {alias}::{caller}  {file}:{line}")
        if len(exact) > _CAP:
            lines.append(f"  … ({len(exact) - _CAP} more)")
    else:
        lines.append("  (no resolved call sites)")
    if lexical:
        lines.append("")
        lines.append(
            f"Unresolved, same name `{sym.name}` "
            f"({len(lexical)}; receiver type unknown):"
        )
        for caller, file, line, callee in lexical[:_CAP]:
            lines.append(
                f"  {alias}::{caller}  {file}:{line}  {callee.removeprefix('ext:')}"
            )
        if len(lexical) > _CAP:
            lines.append(f"  … ({len(lexical) - _CAP} more)")
    lines.append("")
    lines.append("Dynamic dispatch (getattr, registries) is invisible to this view.")
    lines.append("")
    lines.append("Next:")
    lines.append(f"  get(kind='python', id='{alias}::{sym.qualname}', view='source')")
    return "\n".join(lines) + "\n"


def render_importers(alias: str, module: str, idx: RepoIndex) -> str:
    """Modules whose module-scope imports resolve into `module`."""
    rows: list[tuple[str, str, str]] = []
    for mod in idx.modules.values():
        if mod.qualname == module:
            continue
        names = sorted(
            bound
            for bound, value in mod.imports.items()
            if _import_target(value, idx) == module
        )
        if names:
            rows.append((mod.qualname, mod.file, ", ".join(names)))
    rows.sort()
    lines = [f"# importers of {module}  ({len(rows)} modules)\n"]
    if not rows:
        lines.append("  (no module imports it at module scope)")
    for qn, file, bound_names in rows[:_CAP]:
        lines.append(f"  {alias}::{qn}  {file}  [{bound_names}]")
    if len(rows) > _CAP:
        lines.append(f"  … ({len(rows) - _CAP} more)")
    lines.append("")
    lines.append("Function-local imports are not indexed.")
    lines.append("")
    lines.append("Next:")
    lines.append(f"  get(kind='python', id='{alias}::{module}', view='imports')")
    return "\n".join(lines) + "\n"


def render_imports(alias: str, mod: ModuleIndex, idx: RepoIndex) -> str:
    """What `mod` imports, split into in-repo and external targets."""
    internal: dict[str, list[str]] = {}
    external: list[str] = []
    for bound, value in sorted(mod.imports.items()):
        target = _import_target(value, idx)
        if target in idx.modules:
            if target != mod.qualname:
                internal.setdefault(target, []).append(bound)
        else:
            external.append(value)
    lines = [
        f"# imports of {mod.qualname}  "
        f"({len(internal)} in-repo modules, {len(external)} external names)\n"
    ]
    if internal:
        lines.append("In-repo:")
        for target, names in sorted(internal.items()):
            lines.append(f"  {alias}::{target}  [{', '.join(names)}]")
        lines.append("")
    if external:
        lines.append("External:")
        lines.extend(f"  {value}" for value in external)
        lines.append("")
    if not internal and not external:
        lines.append("  (no module-scope imports)")
        lines.append("")
    lines.append("Next:")
    lines.append(
        f"  get(kind='python', id='{alias}::{mod.qualname}', view='importers')"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# search(mode='pattern')
# ---------------------------------------------------------------------------


def compile_pattern(q: str) -> list[tuple[str, re.Pattern[str] | None]]:
    """Parse a `mode='pattern'` query into AND-ed predicates.

    Whitespace-separated terms: `async` (async defs only), `@regex`
    (some decorator matches), anything else is a regex searched in the
    qualname or signature. Raises `BadInput` on a bad regex.
    """
    preds: list[tuple[str, re.Pattern[str] | None]] = []
    for term in q.split():
        try:
            if term == "async":
                preds.append(("async", None))
            elif term.startswith("@") and len(term) > 1:
                preds.append(("decorator", re.compile(term[1:])))
            else:
                preds.append(("text", re.compile(term)))
        except re.error as exc:
            raise BadInput(
                f"bad regex {term!r}: {exc}",
                next="search(kind='python', mode='pattern', q='async @router\\.get')",
            ) from exc
    return preds


def matches_pattern(
    sym: Symbol, preds: list[tuple[str, re.Pattern[str] | None]]
) -> bool:
    """Whether `sym` satisfies every predicate from `compile_pattern`."""
    for kind, rx in preds:
        if kind == "async":
            if not sym.is_async:
                return False
        elif rx is None:
            return False
        elif kind == "decorator":
            if not any(rx.search(d) for d in sym.decorators):
                return False
        elif not (
            rx.search(sym.qualname) or (sym.signature and rx.search(sym.signature))
        ):
            return False
    return True
