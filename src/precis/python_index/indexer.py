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

from precis.python_index.types import (
    CallEdge,
    ModuleIndex,
    RepoIndex,
    Symbol,
    SymbolKind,
)

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

    # Pass 1 — imports. Walked first so the call pass can resolve names.
    imports = _collect_imports(tree, module_qualname=qualname)

    # Pass 2 — symbols + calls. The visitor walks function bodies looking
    # for ast.Call sites, resolving each through `local_names` (imports
    # ∪ top-level defs) plus `self`/`cls` for class context.
    visitor = _SymbolVisitor(
        module_qualname=qualname,
        file_relative=rel,
        imports=imports,
        top_level_names=_collect_top_level_names(tree, module_qualname=qualname),
    )
    visitor.visit(tree)

    return ModuleIndex(
        qualname=qualname,
        file=rel,
        sha256=sha,
        symbols=(module_sym, *visitor.symbols),
        imports=imports,
        calls=tuple(visitor.calls),
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

    def __init__(
        self,
        *,
        module_qualname: str,
        file_relative: str,
        imports: dict[str, str],
        top_level_names: dict[str, str],
    ) -> None:
        self.module_qualname = module_qualname
        self.file_relative = file_relative
        self.imports = imports
        self.top_level_names = top_level_names
        self._stack: list[str] = [module_qualname]
        # Stack of class qualnames currently in scope (for `self.foo` /
        # `cls.foo` resolution). Empty when not inside a class.
        self._class_stack: list[str] = []
        self.symbols: list[Symbol] = []
        self.calls: list[CallEdge] = []

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
        self._class_stack.append(qualname)
        for child in node.body:
            self.visit(child)
        self._class_stack.pop()
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
        # Walk the body for ast.Call sites — pruning at nested function
        # / class / lambda boundaries so calls inside locally-defined
        # helpers don't get attributed to this enclosing function.
        class_qn = self._class_stack[-1] if self._class_stack else None
        for call_node in _walk_calls_in_function_body(node.body):
            callee = _resolve_call(
                call_node,
                imports=self.imports,
                top_level_names=self.top_level_names,
                class_qualname=class_qn,
            )
            self.calls.append(
                CallEdge(
                    caller=qualname,
                    callee=callee,
                    file=self.file_relative,
                    line=call_node.lineno,
                )
            )
        # Do NOT descend further — locally-defined helpers stay invisible.


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


def _collect_imports(tree: ast.Module, *, module_qualname: str) -> dict[str, str]:
    """Collect every name bound at module scope by an import statement.

    Returns a dict mapping the bound name to its resolved qualname:

        import os                       → {'os': 'os'}
        import os.path                  → {'os': 'os'}            (only 'os' is bound)
        import os.path as p             → {'p': 'os.path'}
        from os.path import join        → {'join': 'os.path.join'}
        from os.path import join as j   → {'j': 'os.path.join'}
        from . import sibling           → {'sibling': 'pkg.sibling'}     (relative)
        from ..sibling import thing     → {'thing': 'pkg.parent.sibling.thing'}

    Only top-level imports (directly under `Module.body`) are collected.
    Conditional/inner imports inside `if`/`try`/function bodies are
    deliberately skipped — they're either uncommon or context-dependent
    and would muddy resolution.

    Relative imports (`from .x import y`) are resolved against the
    current module's qualname. If the level overruns the qualname (e.g.
    `from ..foo import` in a top-level module), the import is skipped
    silently — best-effort.
    """
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = alias.name
                else:
                    # `import a.b.c` only binds the leftmost segment.
                    bound = alias.name.split(".")[0]
                    imports[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative_base(
                level=node.level,
                module=node.module,
                current_qualname=module_qualname,
            )
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue  # star-imports: no bound names tracked
                bound = alias.asname or alias.name
                imports[bound] = f"{base}.{alias.name}" if base else alias.name
    return imports


def _resolve_relative_base(
    *, level: int, module: str | None, current_qualname: str
) -> str | None:
    """Resolve `from .X import` or `from ..X import` to an absolute qualname base.

    `level` is the number of leading dots; `module` is the part after
    the dots (may be None for `from . import x`). For non-relative
    imports (`level == 0`), returns `module` as-is.

    Returns None if `level` overruns the current qualname (degenerate
    relative import — caller skips silently).
    """
    if level == 0:
        return module
    parts = current_qualname.split(".")
    if level > len(parts):
        return None
    base = ".".join(parts[:-level])
    if module:
        return f"{base}.{module}" if base else module
    return base or None


def _collect_top_level_names(
    tree: ast.Module, *, module_qualname: str
) -> dict[str, str]:
    """Names bound at module scope by `def` / `class` / simple assignment.

    Returns a dict from bare name to the *qualname under this module*
    (not the leftmost segment). Used by call resolution so that
    in-module references like `Registry()` resolve to
    `precis.registry.Registry`.

    Imports are NOT included here (callers compose this with the
    imports dict). Augmented assignments (`x += 1`) are skipped — they
    require the name to exist already.
    """
    names: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names[node.name] = f"{module_qualname}.{node.name}"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = f"{module_qualname}.{target.id}"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] = f"{module_qualname}.{node.target.id}"
    return names


def _walk_calls_in_function_body(body: list[ast.stmt]) -> Iterable[ast.Call]:
    """Walk a function body's statements yielding every `ast.Call` node,
    pruning at nested function / class / lambda boundaries.

    Comprehensions, `if` / `for` / `while` / `try` / `with` blocks are
    NOT pruned — calls inside them belong to the enclosing function
    from the user's perspective. Walrus expressions, ternary, generator
    expressions: also not pruned.
    """
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            yield node
            # Recurse into args/kwargs — `f(g())` has two calls.
            stack.extend(ast.iter_child_nodes(node))
            continue
        if isinstance(
            node,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
        ):
            continue  # nested scope boundary
        stack.extend(ast.iter_child_nodes(node))


def _resolve_call(
    call: ast.Call,
    *,
    imports: dict[str, str],
    top_level_names: dict[str, str],
    class_qualname: str | None,
) -> str:
    """Map a call site's `func` expression to a qualname.

    Resolution order (per spec § Indexing pipeline step 5):

    1. `Name`: look in `imports`, then `top_level_names`, else `ext:<name>`.
    2. `Attribute` chain: peel off attrs to find the leftmost `Name`.
       If the leftmost is `self` / `cls` and we're inside a class →
       `<class_qualname>.<chain>`. Otherwise resolve the leftmost via
       imports / top-level, append the rest.
    3. Anything else (chained calls, subscript, lambda, etc.):
       `ext:<rightmost-attr>` if we can find one, else `ext:?`.

    Returns a qualname string. `'ext:<name>'` for unresolved.
    """
    func = call.func

    if isinstance(func, ast.Name):
        return _resolve_name(func.id, imports=imports, top_level_names=top_level_names)

    if isinstance(func, ast.Attribute):
        return _resolve_attribute(
            func,
            imports=imports,
            top_level_names=top_level_names,
            class_qualname=class_qualname,
        )

    # Chained call like `f()()`, subscript like `a[0]()`, etc. Best effort.
    return "ext:?"


def _resolve_name(
    name: str, *, imports: dict[str, str], top_level_names: dict[str, str]
) -> str:
    """Resolve a bare name."""
    if name in imports:
        return imports[name]
    if name in top_level_names:
        return top_level_names[name]
    return f"ext:{name}"


def _resolve_attribute(
    attr: ast.Attribute,
    *,
    imports: dict[str, str],
    top_level_names: dict[str, str],
    class_qualname: str | None,
) -> str:
    """Resolve a dotted attribute chain like `a.b.c.method`."""
    # Peel off attributes from the rightmost end, building `chain` in
    # left-to-right order. Stops at the first non-Attribute node.
    chain: list[str] = []
    cur: ast.AST = attr
    while isinstance(cur, ast.Attribute):
        chain.insert(0, cur.attr)
        cur = cur.value

    if not isinstance(cur, ast.Name):
        # e.g. `func()[0].method()` or `(x or y).z()`. Best effort: ext:<last>
        return f"ext:{chain[-1]}" if chain else "ext:?"

    leftmost = cur.id
    rest = ".".join(chain)

    # `self.foo(...)` / `cls.foo(...)` — class-relative.
    if leftmost in ("self", "cls") and class_qualname:
        return f"{class_qualname}.{rest}"

    if leftmost in imports:
        return f"{imports[leftmost]}.{rest}"
    if leftmost in top_level_names:
        return f"{top_level_names[leftmost]}.{rest}"
    return f"ext:{leftmost}.{rest}"


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
