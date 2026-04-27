"""Entry-point discovery for ``view='entries'``.

Surfaces the runnable hooks of a Python repo so an agent can find
``main`` functions to root callgraph traces on:

1. **Console scripts** declared in ``pyproject.toml``:
   ``[project.scripts]`` and ``[project.entry-points.<group>]``.
2. ``if __name__ == "__main__":`` guards in module bodies.

Pure read-only — never invokes anything. Pyproject parsing uses the
stdlib ``tomllib`` (3.11+). If no ``pyproject.toml`` is reachable
from the configured root (walking up at most a few levels), the
console-scripts section is omitted; ``__main__`` guards still work.
"""

from __future__ import annotations

import ast
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — we pin >=3.11
    import tomli as tomllib  # type: ignore[no-redef]

from precis.python_index import RepoIndex

log = logging.getLogger(__name__)


# Walk this many parent directories from the configured root looking
# for `pyproject.toml`. Covers `src/<pkg>/`-layout repos where the
# alias points at the package directory but the project metadata
# lives one or two levels up.
_PYPROJECT_SEARCH_DEPTH = 4


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsoleScript:
    """One ``[project.scripts]`` (or ``[project.entry-points.<group>]``) entry.

    ``entry`` is the raw ``module:attr`` form straight from
    pyproject. ``file`` and ``line`` are populated when the indexer
    found the symbol; otherwise None (e.g. entry points into installed
    third-party packages).
    """

    name: str
    entry: str
    group: str  # 'scripts' or 'entry-points.<group>'
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class MainGuard:
    """One ``if __name__ == '__main__':`` site."""

    file: str
    line: int
    body_summary: str  # one-line description of what runs


@dataclass(frozen=True, slots=True)
class EntriesReport:
    pyproject_path: Path | None
    console_scripts: tuple[ConsoleScript, ...] = field(default_factory=tuple)
    main_guards: tuple[MainGuard, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def find_entries(idx: RepoIndex) -> EntriesReport:
    """Build an `EntriesReport` for one repo.

    Pyproject discovery walks up from ``idx.root`` looking for
    ``pyproject.toml`` (so a `src/<pkg>/`-layout root still finds
    its project file). ``__main__`` guards are detected by re-parsing
    each indexed module.
    """
    pyproject_path = _find_pyproject(idx.root)
    scripts: list[ConsoleScript] = []
    if pyproject_path is not None:
        try:
            scripts = list(_load_console_scripts(pyproject_path, idx))
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("failed reading %s: %s", pyproject_path, e)

    guards: list[MainGuard] = []
    for mod in idx.modules.values():
        if mod.parse_error is not None:
            continue
        try:
            text = (idx.root / mod.file).read_text(encoding="utf-8")
        except OSError:
            continue
        for line, summary in _find_main_guards(text):
            guards.append(MainGuard(file=mod.file, line=line, body_summary=summary))

    return EntriesReport(
        pyproject_path=pyproject_path,
        console_scripts=tuple(scripts),
        main_guards=tuple(guards),
    )


def _find_pyproject(start: Path) -> Path | None:
    """Walk up at most `_PYPROJECT_SEARCH_DEPTH` levels looking for
    `pyproject.toml`. Returns the first match or None."""
    cur = start.resolve()
    for _ in range(_PYPROJECT_SEARCH_DEPTH):
        candidate = cur / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None


def _load_console_scripts(pyproject: Path, idx: RepoIndex):
    """Yield every `ConsoleScript` declared in `pyproject`.

    Pulls from ``[project.scripts]`` (group 'scripts') and from each
    ``[project.entry-points.<group>]`` table. For each entry, attempts
    to resolve `module:attr` to a known symbol in `idx` so the report
    can show file:line.
    """
    raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = raw.get("project") or {}

    # [project.scripts]
    for name, entry in (project.get("scripts") or {}).items():
        yield _resolve_script(name=name, entry=entry, group="scripts", idx=idx)

    # [project.entry-points.*]
    eps = project.get("entry-points") or {}
    for group, entries in eps.items():
        for name, entry in (entries or {}).items():
            yield _resolve_script(
                name=name,
                entry=entry,
                group=f"entry-points.{group}",
                idx=idx,
            )


def _resolve_script(
    *, name: str, entry: str, group: str, idx: RepoIndex
) -> ConsoleScript:
    """Look up `entry` (`module.path:func`) in `idx` for file:line."""
    qualname = entry.replace(":", ".")
    sym = idx.symbol(qualname)
    return ConsoleScript(
        name=name,
        entry=entry,
        group=group,
        file=sym.file if sym else None,
        line=sym.start_line if sym else None,
    )


# ---------------------------------------------------------------------------
# __main__ guard detection
# ---------------------------------------------------------------------------


def _find_main_guards(text: str) -> list[tuple[int, str]]:
    """Return ``[(line, body_summary), ...]`` for every ``__main__`` guard.

    Recognises:
        if __name__ == "__main__":
        if "__main__" == __name__:

    Symmetric form too. AST-based for accuracy (catches indented
    placement, regex would mis-fire on inline comments).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    out: list[tuple[int, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        if not _is_main_guard(node.test):
            continue
        out.append((node.lineno, _summarise_body(node.body)))
    return out


def _is_main_guard(test: ast.expr) -> bool:
    """True iff `test` is ``__name__ == "__main__"`` (or symmetric)."""
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left = test.left
    right = test.comparators[0]
    return (_is_name_dunder(left) and _is_main_constant(right)) or (
        _is_main_constant(left) and _is_name_dunder(right)
    )


def _is_name_dunder(n: ast.expr) -> bool:
    return isinstance(n, ast.Name) and n.id == "__name__"


def _is_main_constant(n: ast.expr) -> bool:
    return isinstance(n, ast.Constant) and n.value == "__main__"


def _summarise_body(body: list[ast.stmt]) -> str:
    """One-line description of the body. Picks the first statement and
    unparses it; truncates long lines."""
    if not body:
        return "pass"
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call):
        text = ast.unparse(first.value)
    else:
        text = ast.unparse(first).splitlines()[0]
    text = " ".join(text.split())
    if len(text) > 64:
        text = text[:63] + "…"
    return f"runs `{text}`"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_entries(alias: str, report: EntriesReport) -> str:
    """Format an `EntriesReport` for the agent."""
    lines = [f"# {alias} — entry points\n"]

    if report.console_scripts:
        lines.append("  Console scripts:")
        # Group by group= so the per-group label appears once.
        for group_name in _ordered_groups(report.console_scripts):
            entries = [s for s in report.console_scripts if s.group == group_name]
            if group_name != "scripts":
                lines.append(f"    [{group_name}]")
            for s in entries:
                file_col = f"file: {s.file}:{s.line}" if s.file else "file: —"
                lines.append(f"    {s.name:<24}entry: {s.entry:<32}{file_col}")
        lines.append("")
    elif report.pyproject_path is None:
        lines.append("  Console scripts: (no pyproject.toml found)")
        lines.append("")
    else:
        lines.append("  Console scripts: (none declared)")
        lines.append("")

    if report.main_guards:
        lines.append("  __main__ guards:")
        for g in report.main_guards:
            lines.append(f"    {g.file}:{g.line:<5} {g.body_summary}")
        lines.append("")
    else:
        lines.append("  __main__ guards: none")
        lines.append("")

    lines.append("Next:")
    if report.console_scripts:
        first = report.console_scripts[0]
        lines.append(
            f"  get(kind='python', id={alias!r}, view='callgraph', "
            f"entry={first.entry!r})"
        )
    elif report.main_guards:
        first = report.main_guards[0]
        # Trim the file name to a module guess for the hint.
        lines.append(f"  get(kind='python', id='{alias}/{first.file}~L{first.line}')")
    return "\n".join(lines)


def _ordered_groups(scripts) -> list[str]:
    """Return group names in the order they first appear."""
    seen: list[str] = []
    for s in scripts:
        if s.group not in seen:
            seen.append(s.group)
    return seen
