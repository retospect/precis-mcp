"""Dataclasses for the Python AST index.

A `RepoIndex` is a snapshot of every indexable `.py` file under one
configured root. It contains zero or more `ModuleIndex` rows (one per
file), each of which contains zero or more `Symbol` rows (one per
indexable definition: the module itself, classes, functions, methods,
async variants thereof, and nested classes).

Line numbers are **1-indexed and inclusive on both ends** to match the
unified addressing convention (see `docs/file-kinds-unified-addressing.md
§ Line-number convention`). Python's `ast` module already returns
1-indexed `lineno` and `end_lineno` so no conversion is needed.

Qualnames are dotted paths — `precis.registry.Registry.get` for a
method, `precis.registry` for the module itself, `precis.registry.foo`
for a top-level function. The leading prefix matches the import path
the user would write, computed by walking up parents while
`__init__.py` exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SymbolKind = Literal["module", "class", "function", "method"]


@dataclass(frozen=True, slots=True)
class Symbol:
    """One indexable definition.

    `kind` distinguishes module / class / function / method so consumers
    can render and group accordingly. `parent` is the enclosing
    qualname; for a top-level function or class it is the module's
    qualname; for a method it is the class qualname; for the module
    symbol itself it is None.

    `signature` is None for the module symbol and for classes (use
    `kind` to discriminate). For functions and methods it is the
    `def name(args) -> return_annotation` line, recovered by
    `ast.unparse` from the original AST node — preserves type
    annotations and default-value expressions textually.

    `decorators` is the unparsed source of each decorator in the order
    they appear above the def (top-down), each *without* the leading
    `@`. So `@property\\n@cached_property` yields
    `('property', 'cached_property')`.
    """

    qualname: str
    kind: SymbolKind
    file: str
    start_line: int
    end_line: int
    parent: str | None
    signature: str | None
    docstring: str | None
    decorators: tuple[str, ...] = ()
    is_async: bool = False

    @property
    def name(self) -> str:
        """Last segment of the qualname (`get` for `precis.registry.Registry.get`)."""
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def line_count(self) -> int:
        """Number of lines spanned, inclusive of both endpoints."""
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class CallEdge:
    """One static call-graph edge: `caller` calls `callee` at `file:line`.

    `caller` is always a qualname for a function/method we indexed.
    `callee` is either a resolved qualname (within this repo or a
    known import) or `'ext:<name>'` for unresolved calls (stdlib,
    third-party, dynamic dispatch, calls on local variables whose
    types we don't track).

    Best-effort resolution per the spec § Indexing pipeline step 5:
    no type inference, no MRO walk, no duck typing. Just module-level
    names + imports + `self`/`cls` for class context.
    """

    caller: str
    callee: str
    file: str
    line: int


@dataclass(frozen=True, slots=True)
class ModuleIndex:
    """One indexed `.py` file.

    `qualname` is the dotted import path (e.g. `precis.registry`).
    `file` is the path relative to the repo root, using forward slashes.
    `sha256` is the hex digest of the source bytes — used by callers to
    decide whether to re-parse on reindex.

    `symbols` includes the module symbol itself (always at index 0)
    followed by every class / function / method discovered, in the
    order they appear in the source. `parse_error` is set if `ast.parse`
    failed; in that case `symbols` contains only the module-level row
    with degraded metadata (no docstring, line range = whole file).

    `imports` maps each name *bound at module scope by an import* to
    its resolved qualname (e.g. `{'NotFound': 'precis.errors.NotFound',
    'os': 'os'}`). Used by the call pass for static resolution; also
    surfaced via the `imports` view.

    `calls` contains every static call edge originating in this module:
    one entry per `ast.Call` site inside an indexed function/method.
    Calls inside nested functions, lambdas, and comprehensions inside
    nested scopes are pruned (locals are noise). See `CallEdge`.
    """

    qualname: str
    file: str
    sha256: str
    symbols: tuple[Symbol, ...]
    parse_error: str | None = None
    imports: dict[str, str] = field(default_factory=dict)
    calls: tuple[CallEdge, ...] = ()

    @property
    def module_symbol(self) -> Symbol:
        """The module-level Symbol (always present, always first)."""
        return self.symbols[0]

    def by_qualname(self, qualname: str) -> Symbol | None:
        """Look up a symbol within this module by its full qualname."""
        for s in self.symbols:
            if s.qualname == qualname:
                return s
        return None


@dataclass(frozen=True, slots=True)
class RepoIndex:
    """All `.py` files under one configured root, indexed.

    `root` is the absolute path of the repo root. `modules` is keyed
    by qualname (e.g. `precis.registry`) — same key both for the
    module symbol and for the module index.

    Use `module(qualname)` to get a module by import path, or
    `file(rel_path)` to get one by file path (e.g.
    `src/precis/registry.py`). Both are O(1).
    """

    root: Path
    modules: dict[str, ModuleIndex] = field(default_factory=dict)
    _by_file: dict[str, ModuleIndex] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, root: Path, modules: list[ModuleIndex]) -> RepoIndex:
        """Construct a `RepoIndex` from a list of pre-built modules."""
        by_qualname = {m.qualname: m for m in modules}
        by_file = {m.file: m for m in modules}
        return cls(root=root, modules=by_qualname, _by_file=by_file)

    def module(self, qualname: str) -> ModuleIndex | None:
        return self.modules.get(qualname)

    def file(self, relative_path: str) -> ModuleIndex | None:
        return self._by_file.get(relative_path)

    def symbol(self, qualname: str) -> Symbol | None:
        """Find a symbol by full qualname across every module.

        Tries the module index first (covers `module.thing`), then walks
        every parent prefix to find the enclosing module — handles
        nested classes (`pkg.mod.Outer.Inner.method`).
        """
        # Direct module-level hit.
        if (mod := self.modules.get(qualname)) is not None:
            return mod.module_symbol

        # Walk parent prefixes: 'a.b.c.D.method' -> try 'a.b.c.D',
        # 'a.b.c', 'a.b', 'a' as the module qualname.
        parts = qualname.split(".")
        for i in range(len(parts) - 1, 0, -1):
            mod_qn = ".".join(parts[:i])
            if (mod := self.modules.get(mod_qn)) is not None:
                if (sym := mod.by_qualname(qualname)) is not None:
                    return sym
                # Module exists but symbol doesn't — keep walking only
                # if we still have a chance to find a parent module
                # (e.g. for nested-package qualnames). In practice once
                # we found *the* module the symbol must live there or
                # not at all, so we can break.
                return None
        return None

    def symbols_in(self, qualname_prefix: str) -> list[Symbol]:
        """All symbols whose qualname starts with `qualname_prefix.`
        (or equals it). Useful for "list everything under this module
        / class". Order: module by module, then source order."""
        out: list[Symbol] = []
        prefix_dot = qualname_prefix + "."
        for mod in self.modules.values():
            for s in mod.symbols:
                if s.qualname == qualname_prefix or s.qualname.startswith(prefix_dot):
                    out.append(s)
        return out

    @property
    def n_modules(self) -> int:
        return len(self.modules)

    @property
    def n_symbols(self) -> int:
        return sum(len(m.symbols) for m in self.modules.values())
