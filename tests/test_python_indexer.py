"""Unit tests for `precis.python_index.indexer`.

Pure logic — no DB, no network, no postgres. Each test writes a tiny
Python repo to a tmp_path and indexes it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.python_index import (
    CallEdge,
    RepoIndex,
    Symbol,
    index_repo,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(repo: Path, relpath: str, content: str) -> Path:
    """Write `content` (after dedent) to `repo / relpath`, creating parents."""
    file = repo / relpath
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


def _by_qualname(idx: RepoIndex, qn: str) -> Symbol:
    """Look up a symbol or fail the test with a useful message."""
    sym = idx.symbol(qn)
    assert sym is not None, (
        f"qualname not found: {qn!r}\n"
        f"available: {sorted(s.qualname for m in idx.modules.values() for s in m.symbols)}"
    )
    return sym


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


def test_simple_module(tmp_path: Path) -> None:
    """Top-level function and class with one method round-trip cleanly."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/mod.py",
        '''
        """Module docstring."""

        def hello(name: str) -> str:
            """Say hi."""
            return f"hi {name}"


        class Greeter:
            """A greeter."""

            def greet(self, name: str) -> str:
                return hello(name)
        ''',
    )

    idx = index_repo(tmp_path)
    assert idx.n_modules == 2  # pkg + pkg.mod
    mod = idx.module("pkg.mod")
    assert mod is not None
    assert mod.parse_error is None

    # Module-level symbol
    msym = mod.module_symbol
    assert msym.kind == "module"
    assert msym.docstring == "Module docstring."
    assert msym.parent is None

    # Function
    fn = _by_qualname(idx, "pkg.mod.hello")
    assert fn.kind == "function"
    assert fn.parent == "pkg.mod"
    assert fn.signature == "def hello(name: str) -> str"
    assert fn.docstring == "Say hi."

    # Class
    cls = _by_qualname(idx, "pkg.mod.Greeter")
    assert cls.kind == "class"
    assert cls.parent == "pkg.mod"
    assert cls.signature is None
    assert cls.docstring == "A greeter."

    # Method
    m = _by_qualname(idx, "pkg.mod.Greeter.greet")
    assert m.kind == "method"
    assert m.parent == "pkg.mod.Greeter"
    assert m.signature == "def greet(self, name: str) -> str"


# ---------------------------------------------------------------------------
# Qualname resolution
# ---------------------------------------------------------------------------


def test_qualnames_handle_init_and_nested_packages(tmp_path: Path) -> None:
    """`__init__.py` files become the package qualname, not `pkg.__init__`."""
    _write(tmp_path, "alpha/__init__.py", '"""alpha package."""\n')
    _write(tmp_path, "alpha/beta/__init__.py", "")
    _write(tmp_path, "alpha/beta/gamma.py", "VALUE = 1\n")
    _write(tmp_path, "loose.py", "x = 1\n")  # no __init__ beside it

    idx = index_repo(tmp_path)

    # Package files → qualname is the package name.
    assert idx.module("alpha") is not None
    assert idx.module("alpha.beta") is not None
    assert idx.module("alpha.beta.gamma") is not None

    # Loose file → qualname is just the stem.
    assert idx.module("loose") is not None

    # No spurious 'alpha.__init__'.
    assert idx.module("alpha.__init__") is None
    assert idx.module("alpha.beta.__init__") is None


def test_nested_classes_have_correct_parent_chain(tmp_path: Path) -> None:
    """`Outer.Inner.method` parent chain walks through every enclosing class."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        class Outer:
            class Inner:
                def deep(self) -> int:
                    return 1
        """,
    )

    idx = index_repo(tmp_path)
    outer = _by_qualname(idx, "pkg.m.Outer")
    inner = _by_qualname(idx, "pkg.m.Outer.Inner")
    deep = _by_qualname(idx, "pkg.m.Outer.Inner.deep")

    assert outer.kind == "class" and outer.parent == "pkg.m"
    assert inner.kind == "class" and inner.parent == "pkg.m.Outer"
    assert deep.kind == "method" and deep.parent == "pkg.m.Outer.Inner"


# ---------------------------------------------------------------------------
# AST features
# ---------------------------------------------------------------------------


def test_async_functions_are_marked_async(tmp_path: Path) -> None:
    """`async def` flips `is_async` and emits an `async def` signature."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        async def fetch(url: str) -> bytes:
            return b""

        class Client:
            async def get(self, url: str) -> bytes:
                return b""
        """,
    )
    idx = index_repo(tmp_path)
    fn = _by_qualname(idx, "pkg.m.fetch")
    assert fn.is_async is True
    assert fn.signature == "async def fetch(url: str) -> bytes"

    method = _by_qualname(idx, "pkg.m.Client.get")
    assert method.is_async is True
    assert method.kind == "method"
    assert method.signature == "async def get(self, url: str) -> bytes"


def test_decorators_captured_in_source_order(tmp_path: Path) -> None:
    """Decorators appear without leading `@`, in the order written."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        from functools import cached_property

        class C:
            @property
            @cached_property
            def x(self) -> int:
                return 1

        @staticmethod
        def free() -> None:
            pass
        """,
    )
    idx = index_repo(tmp_path)
    x = _by_qualname(idx, "pkg.m.C.x")
    assert x.decorators == ("property", "cached_property")

    free = _by_qualname(idx, "pkg.m.free")
    assert free.decorators == ("staticmethod",)


def test_docstrings_extracted_at_three_levels(tmp_path: Path) -> None:
    """Module / class / function docstrings all surface."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        '''
        """Module."""

        def f() -> None:
            """Function."""

        class C:
            """Class."""

            def m(self) -> None:
                """Method."""
        ''',
    )
    idx = index_repo(tmp_path)
    assert idx.module("pkg.m").module_symbol.docstring == "Module."  # type: ignore[union-attr]
    assert _by_qualname(idx, "pkg.m.f").docstring == "Function."
    assert _by_qualname(idx, "pkg.m.C").docstring == "Class."
    assert _by_qualname(idx, "pkg.m.C.m").docstring == "Method."


def test_signature_preserves_annotations_and_defaults(tmp_path: Path) -> None:
    """Type annotations and default expressions round-trip textually."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def f(
            x: int,
            y: str = "z",
            *args: int,
            kw: dict[str, list[int]] | None = None,
            **kwargs: int,
        ) -> tuple[int, str] | None:
            pass
        """,
    )
    idx = index_repo(tmp_path)
    fn = _by_qualname(idx, "pkg.m.f")
    sig = fn.signature
    assert sig is not None
    # `ast.unparse` does not put spaces around `=` for parameter defaults
    # (its convention; matches `repr(ast.unparse(...))`). We surface
    # whatever it produces so signatures are stable across edits.
    assert "x: int" in sig
    assert "y: str='z'" in sig
    assert "*args: int" in sig
    assert "kw: dict[str, list[int]] | None=None" in sig
    assert "**kwargs: int" in sig
    assert "-> tuple[int, str] | None" in sig


def test_function_locals_are_not_indexed(tmp_path: Path) -> None:
    """Locally-defined helpers inside a function body are noise; we skip
    them so the index stays focused on the API surface."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def outer() -> int:
            def helper() -> int:
                return 1
            return helper()
        """,
    )
    idx = index_repo(tmp_path)
    assert idx.symbol("pkg.m.outer") is not None
    assert idx.symbol("pkg.m.outer.helper") is None


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_syntax_error_graceful_degrade(tmp_path: Path) -> None:
    """A file with a syntax error is recorded with `parse_error` set
    and a single module-level symbol so it is still addressable."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/broken.py",
        "def oops(\n",  # truncated def — syntax error
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.broken")
    assert mod is not None
    assert mod.parse_error is not None
    assert "SyntaxError" in mod.parse_error
    # Still has the module-level symbol so the file can be addressed.
    assert len(mod.symbols) == 1
    assert mod.symbols[0].kind == "module"


def test_walk_skips_cruft_dirs(tmp_path: Path) -> None:
    """`.venv`, `__pycache__`, `node_modules`, dotfile dirs are skipped."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/real.py", "x = 1\n")
    _write(tmp_path, ".venv/lib/site-packages/fake.py", "x = 1\n")
    _write(tmp_path, "__pycache__/cached.py", "x = 1\n")
    _write(tmp_path, "node_modules/somepkg/index.py", "x = 1\n")
    _write(tmp_path, ".git/hooks/post-commit.py", "x = 1\n")  # dotted dir

    idx = index_repo(tmp_path)
    qualnames = set(idx.modules)
    assert qualnames == {"pkg", "pkg.real"}


def test_repo_index_lookups(tmp_path: Path) -> None:
    """`module()`, `file()`, `symbol()`, `symbols_in()` all work."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def a() -> None: pass
        def b() -> None: pass

        class C:
            def x(self) -> None: pass
        """,
    )
    idx = index_repo(tmp_path)

    assert idx.module("pkg.m") is not None
    assert idx.file("pkg/m.py") is not None
    assert idx.symbol("pkg.m.C.x") is not None
    assert idx.symbol("pkg.m.does_not_exist") is None

    # symbols_in: everything under pkg.m.C
    under_class = {s.qualname for s in idx.symbols_in("pkg.m.C")}
    assert under_class == {"pkg.m.C", "pkg.m.C.x"}

    # symbols_in on the module includes the module symbol + all defs
    under_module = {s.qualname for s in idx.symbols_in("pkg.m")}
    assert "pkg.m" in under_module
    assert "pkg.m.a" in under_module
    assert "pkg.m.C.x" in under_module


# ---------------------------------------------------------------------------
# Self-test against the real repo
# ---------------------------------------------------------------------------


def test_index_precis_itself_smoke() -> None:
    """End-to-end: index `src/precis` and check shape of well-known symbols.

    Locks in that the indexer survives real-world code (TYPE_CHECKING
    imports, async, `__init__.py` packages, decorators, etc.) without
    crashing and that a few stable qualnames resolve correctly.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "precis"
    if not src.is_dir():
        pytest.skip(f"can't find src/precis at {src}")

    idx = index_repo(src)

    # Sanity bounds on the cluster's smallest pip package. The exact
    # numbers will drift; the bounds are loose enough to absorb typical
    # churn.
    assert idx.n_modules >= 30
    assert idx.n_symbols >= 200

    # Stable canary symbols — pick ones unlikely to churn.
    reg = idx.symbol("precis.dispatch.Hub")
    assert reg is not None and reg.kind == "class"

    boot_fn = idx.symbol("precis.dispatch.boot")
    assert boot_fn is not None and boot_fn.kind == "function"
    assert boot_fn.signature is not None
    assert "Hub" in boot_fn.signature

    md = idx.module("precis.handlers.markdown")
    assert md is not None and md.parse_error is None


# ---------------------------------------------------------------------------
# Imports + call pass
# ---------------------------------------------------------------------------


def _calls_from(mod_index, caller: str) -> list[CallEdge]:
    """Return every CallEdge whose caller matches `caller`."""
    return [c for c in mod_index.calls if c.caller == caller]


def test_imports_collected_for_basic_forms(tmp_path: Path) -> None:
    """`import x`, `import x as y`, `from a.b import c [as d]` all bind."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        import os
        import os.path as p
        from collections import OrderedDict
        from collections import deque as dq
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    assert mod.imports == {
        "os": "os",
        "p": "os.path",
        "OrderedDict": "collections.OrderedDict",
        "dq": "collections.deque",
    }


def test_relative_imports_resolve_against_module_qualname(tmp_path: Path) -> None:
    """`from .x import y` and `from ..x import y` resolve absolutely."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/sub/__init__.py", "")
    _write(
        tmp_path,
        "pkg/sub/m.py",
        """
        from . import sibling
        from .types import Symbol
        from ..top import Helper
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.sub.m")
    assert mod is not None
    assert mod.imports == {
        "sibling": "pkg.sub.sibling",
        "Symbol": "pkg.sub.types.Symbol",
        "Helper": "pkg.top.Helper",
    }


def test_conditional_imports_inside_function_bodies_are_ignored(tmp_path: Path) -> None:
    """Imports inside `def`, `if`, `try` are intentionally NOT tracked.

    Their context-dependence would muddy resolution; we'd rather have a
    clean ext:<name> than a wrong qualname.
    """
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        import os  # tracked

        def f():
            from collections import deque  # not tracked
            return deque()

        try:
            import json  # not tracked
        except ImportError:
            json = None
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    assert mod.imports == {"os": "os"}


def test_call_to_imported_function(tmp_path: Path) -> None:
    """`from x import y; def f(): y()` → callee = 'x.y'."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        from os.path import join

        def build_path(a, b):
            return join(a, b)
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.build_path")
    assert len(calls) == 1
    assert calls[0].callee == "os.path.join"


def test_call_to_in_module_function(tmp_path: Path) -> None:
    """A call from one top-level def to another resolves to the callee's qualname."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def helper(x):
            return x + 1

        def caller(y):
            return helper(y)
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.caller")
    assert len(calls) == 1
    assert calls[0].callee == "pkg.m.helper"


def test_call_to_attribute_chain_via_imported_module(tmp_path: Path) -> None:
    """`import os.path; os.path.join()` → 'os.path.join' (not 'os.path.join.path')."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        import os

        def f(a, b):
            return os.path.join(a, b)
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.f")
    assert len(calls) == 1
    assert calls[0].callee == "os.path.join"


def test_self_method_call_resolves_to_class(tmp_path: Path) -> None:
    """`class C: def m(self): self.other()` → callee = 'pkg.m.C.other'."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        class C:
            def m(self):
                self.other()
                self.helper.foo()

            def other(self): pass
            def helper(self): pass
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.C.m")
    callees = {c.callee for c in calls}
    assert "pkg.m.C.other" in callees
    assert "pkg.m.C.helper.foo" in callees


def test_classmethod_cls_resolves_to_class(tmp_path: Path) -> None:
    """`@classmethod def m(cls): cls.factory()` resolves like self."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        class C:
            @classmethod
            def make(cls):
                return cls.factory()

            @classmethod
            def factory(cls): pass
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.C.make")
    assert len(calls) == 1
    assert calls[0].callee == "pkg.m.C.factory"


def test_unresolved_call_is_marked_ext(tmp_path: Path) -> None:
    """Calls on unknown names get `ext:<name>` so they're filterable."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def caller(thing):
            mystery()             # ext:mystery
            thing.method()        # ext:thing.method (local var, type unknown)
            return thing
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    callees = {c.callee for c in _calls_from(mod, "pkg.m.caller")}
    assert "ext:mystery" in callees
    assert "ext:thing.method" in callees


def test_calls_in_nested_function_pruned(tmp_path: Path) -> None:
    """Calls inside a locally-defined helper do NOT get attributed to
    the enclosing function — locals are noise."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        from os.path import join

        def outer():
            def inner():
                join("a", "b")  # should NOT show as outer's call
            inner()             # this DOES show as outer's call
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    callees = [c.callee for c in _calls_from(mod, "pkg.m.outer")]
    # `inner()` resolves to ext:inner (it's a local def we don't track).
    assert "ext:inner" in callees
    # `os.path.join` came from inside the nested function — must not appear.
    assert "os.path.join" not in callees
    # And the nested `inner` itself must not have been emitted as a symbol.
    assert idx.symbol("pkg.m.outer.inner") is None


def test_calls_inside_comprehensions_belong_to_outer(tmp_path: Path) -> None:
    """Comprehensions / for / if blocks are NOT pruned — calls inside
    them belong to the enclosing function."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        from os.path import basename

        def f(paths):
            return [basename(p) for p in paths if basename(p)]
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.f")
    # Two `basename` calls (one in the result, one in the filter).
    callees = [c.callee for c in calls]
    assert callees.count("os.path.basename") == 2


def test_nested_call_args_recurse(tmp_path: Path) -> None:
    """`f(g(h()))` produces three call edges from the same caller."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        from a import f, g, h

        def caller():
            f(g(h()))
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    callees = sorted(c.callee for c in _calls_from(mod, "pkg.m.caller"))
    assert callees == ["a.f", "a.g", "a.h"]


def test_class_constructor_call_resolves_to_class(tmp_path: Path) -> None:
    """`Foo()` resolves to `Foo` (the class qualname) — agents read it
    as a constructor."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        class Widget:
            pass

        def make():
            return Widget()
        """,
    )
    idx = index_repo(tmp_path)
    mod = idx.module("pkg.m")
    assert mod is not None
    calls = _calls_from(mod, "pkg.m.make")
    assert len(calls) == 1
    assert calls[0].callee == "pkg.m.Widget"
