"""AST-based indexer.

`index_repo(root)` walks a directory, parses every `.py` file with
`ast`, and builds a `RepoIndex`. `index_module(...)` parses one file
and is a useful unit-test seam.

Qualname resolution: for each `.py` file, walk up parents while
`__init__.py` exists. The first ancestor *without* an `__init__.py` is
the package boundary; everything below it becomes the dotted qualname.

    src/precis/registry.py        with src/precis/__init__.py    →  precis.registry
    src/precis/handlers/md.py     with __init__.py at every step →  precis.handlers.md
    src/precis/handlers/__init__.py                              →  precis.handlers
    setup.py                      no __init__.py beside it       →  setup
    tests/test_foo.py             with tests/__init__.py         →  tests.test_foo

The walk skips a fixed set of cruft directories (`.git`, `.venv`,
`__pycache__`, `node_modules`, etc.) and any directory whose name
starts with `.`. Real `.gitignore` parsing is a future hook (would
require `pathspec`); the conservative skip list is enough for v1.
"""

from __future__ import annotations

import ast
import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

from precis.python_index.types import ModuleIndex, RepoIndex, Symbol, SymbolKind

log = logging.getLogger(__name__)


# Directories that are never code under management. Matched by name only.
# `.gitignore`-aware walking is a future enhancement (would pull in
# `pathspec`); the conservative skip set covers the 99% case.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".env",
        "env",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "site-packages",
        ".eggs",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def index_repo(root: Path) -> RepoIndex:
    """Build a `RepoIndex` for every indexable `.py` file under `root`.

    Parse errors are captured on each `ModuleIndex.parse_error` and do
    not abort the walk; the module ends up with a single module-level
    Symbol whose docstring is None and whose line range covers the
    whole file.
    """
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    modules: list[ModuleIndex] = []
    for py_file in _walk_python_files(root):
        try:
            qualname = _qualname_for_file(py_file)
        except ValueError as e:
            log.warning("skipping %s: %s", py_file, e)
            continue

        try:
            file_relative = py_file.relative_to(root).as_posix()
        except ValueError:
            continue

        modules.append(
            index_module(py_file, qualname=qualname, file_relative=file_relative)
        )

    return RepoIndex.build(root=root, modules=modules)


def index_module(
    path: Path,
    *,
    qualname: str,
    file_relative: str | None = None,
) -> ModuleIndex:
    """Parse one `.py` file and return its `ModuleIndex`.

    `qualname` is the dotted import path the module is reachable as
    (e.g. `precis.registry`). `file_relative` is what's recorded in
    each Symbol's `file` field — defaults to the file's name if not
    given (useful for ad-hoc tests against a single file).
    """
    source = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
    rel = file_relative if file_relative is not None else path.name

    total_lines = source.count("\n") + (0 if source.endswith("\n") else 1)
    total_lines = max(total_lines, 1)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        # Graceful degrade: keep the module addressable, no inner symbols.
        module_sym = Symbol(
            qualname=qualname,
            kind="module",
            file=rel,
            start_line=1,
            end_line=total_lines,
            parent=None,
            signature=None,
            docstring=None,
        )
        return ModuleIndex(
            qualname=qualname,
            file=rel,
            sha256=sha,
            symbols=(module_sym,),
            parse_error=f"{type(e).__name__}: {e}",
        )

    module_sym = Symbol(
        qualname=qualname,
        kind="module",
        file=rel,
        start_line=1,
        end_line=total_lines,
        parent=None,
        signature=None,
        docstring=ast.get_docstring(tree),
    )

    visitor = _SymbolVisitor(
        module_qualname=qualname,
        file_relative=rel,
    )
    visitor.visit(tree)

    return ModuleIndex(
        qualname=qualname,
        file=rel,
        sha256=sha,
        symbols=(module_sym, *visitor.symbols),
    )


# ---------------------------------------------------------------------------
# AST visitor — collects classes / functions / methods.
# ---------------------------------------------------------------------------


class _SymbolVisitor(ast.NodeVisitor):
    """Collect indexable symbols below the module level.

    Maintains a stack of enclosing qualnames so that a method's parent
    is the class qualname and a nested class's parent is its outer
    class. The module qualname is the bottom of the stack.

    Top-level functions and classes are emitted with `parent =
    module_qualname`; methods inside a class with `parent =
    class_qualname`. We only descend into class bodies (for methods +
    nested classes); function bodies are not walked, so locally-defined
    helpers are intentionally invisible — they are noise at index
    granularity.
    """

    def __init__(self, *, module_qualname: str, file_relative: str) -> None:
        self.module_qualname = module_qualname
        self.file_relative = file_relative
        self._stack: list[str] = [module_qualname]
        self.symbols: list[Symbol] = []

    # ── classes ──────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = self._stack[-1]
        qualname = f"{parent}.{node.name}"
        self.symbols.append(
            Symbol(
                qualname=qualname,
                kind="class",
                file=self.file_relative,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parent=parent,
                signature=None,
                docstring=ast.get_docstring(node),
                decorators=tuple(_unparse(d) for d in node.decorator_list),
            )
        )
        # Descend so methods + nested classes get their parent right.
        self._stack.append(qualname)
        for child in node.body:
            self.visit(child)
        self._stack.pop()

    # ── functions / methods (sync + async) ───────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node, is_async=True)

    def _record_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        parent = self._stack[-1]
        qualname = f"{parent}.{node.name}"
        # Method iff the immediate parent is a class — i.e. the stack has
        # a class qualname on top (anything other than the module
        # qualname). The module qualname is always at index 0 of the
        # stack, so depth > 1 means we are inside a class.
        kind: SymbolKind = "method" if len(self._stack) > 1 else "function"

        self.symbols.append(
            Symbol(
                qualname=qualname,
                kind=kind,
                file=self.file_relative,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parent=parent,
                signature=_signature(node, is_async=is_async),
                docstring=ast.get_docstring(node),
                decorators=tuple(_unparse(d) for d in node.decorator_list),
                is_async=is_async,
            )
        )
        # We do NOT descend into function bodies — locally-defined
        # helpers are noise at index granularity.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool) -> str:
    """Reconstruct `def name(args) -> Return` from an AST node.

    Uses `ast.unparse` on the args + return annotation. Keeps type
    annotations and default expressions textually exact. The trailing
    colon and body are NOT included — this is the *signature*, not the
    full def line.
    """
    args = _unparse(node.args)
    keyword = "async def " if is_async else "def "
    sig = f"{keyword}{node.name}({args})"
    if node.returns is not None:
        sig += f" -> {_unparse(node.returns)}"
    return sig


def _unparse(node: ast.AST) -> str:
    """`ast.unparse` with newline + leading-whitespace squash.

    Multi-line annotations (rare, but possible with very long generic
    parameter lists) get collapsed to one line so the rendered
    signature is grep-able and fits in TOC rows.
    """
    text = ast.unparse(node)
    # Collapse any internal whitespace runs to single spaces. Cheap and
    # idempotent for the kinds of annotations real code carries.
    return " ".join(text.split())


def _walk_python_files(root: Path) -> Iterable[Path]:
    """Yield every `.py` file under `root`, depth-first, sorted within
    each directory for stable output order. Skips `_SKIP_DIRS` and any
    directory whose name starts with `.`.
    """
    if not root.is_dir():
        return
    entries = sorted(root.iterdir(), key=lambda p: p.name)
    for entry in entries:
        if entry.is_dir():
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            yield from _walk_python_files(entry)
        elif entry.is_file() and entry.suffix == ".py":
            yield entry


def _qualname_for_file(path: Path) -> str:
    """Compute the dotted import qualname for a `.py` file.

    Walk up parents while each one contains an `__init__.py`. Once we
    hit a directory without `__init__.py`, that's the package
    boundary; everything below it (in package order) becomes the dotted
    qualname.

    For `__init__.py` files, the qualname is the *package*'s name (no
    `.__init__` suffix).
    """
    parts: list[str]
    if path.name == "__init__.py":
        # Package itself. Walk up and skip the file name.
        parts = []
        ancestor = path.parent
    else:
        # Module file. Stem is the leaf segment.
        parts = [path.stem]
        ancestor = path.parent

    # Walk up while __init__.py exists at this level.
    while (ancestor / "__init__.py").is_file():
        parts.insert(0, ancestor.name)
        ancestor = ancestor.parent

    if not parts:
        # `__init__.py` at a non-package root — e.g. mistakenly placed.
        # Fall back to the parent dir name as a single-segment qualname.
        return ancestor.name

    return ".".join(parts)
