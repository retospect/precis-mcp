"""Lambda bodies credit calls to the enclosing scope; nested functions are
indexed as `outer.inner` symbols with their own call edges and local imports."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.python import PythonHandler


def _write(repo: Path, rel: str, content: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def handler(tmp_path: Path) -> PythonHandler:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path, "pkg/a.py", "def f(x):\n    return x\n\n\ndef g(x):\n    return x\n"
    )
    _write(
        tmp_path,
        "pkg/lam.py",
        """
        from pkg.a import f

        MODULE_HOOK = lambda: f(0)


        def retry(fn):
            return fn()


        def in_lambda():
            from pkg.a import g
            return retry(lambda: g(1))


        class K:
            def m(self):
                return retry(lambda: f(2))
        """,
    )
    _write(
        tmp_path,
        "pkg/nest.py",
        """
        from pkg.a import f


        def outer():
            def inner():
                import pkg.a as mod
                return mod.g(3)
            return inner() + f(4)


        class C:
            def meth(self):
                def helper():
                    return f(5)
                return helper()


        def twice():
            if True:
                def step():
                    return 1
            else:
                def step():
                    return 2
            return step()
        """,
    )
    return PythonHandler(hub=Hub(), roots={"r": tmp_path})


def test_lambda_call_credited_to_enclosing_function(handler: PythonHandler) -> None:
    # `g` is bound by a function-local import, used inside the lambda.
    body = handler.get(id="r::pkg.a.g", view="callers").body
    assert "r::pkg.lam.in_lambda  pkg/lam.py:" in body
    body = handler.get(id="r::pkg.a.f", view="callers").body
    assert "r::pkg.lam.K.m  pkg/lam.py:" in body


def test_module_scope_lambda_credited_to_module(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.a.f", view="callers").body
    assert "r::pkg.lam  pkg/lam.py:3" in body


def test_nested_function_call_edge_and_local_import(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.a.g", view="callers").body
    assert "r::pkg.nest.outer.inner  pkg/nest.py:" in body
    body = handler.get(id="r::pkg.a.f", view="callers").body
    assert "r::pkg.nest.outer  pkg/nest.py:" in body
    assert "r::pkg.nest.C.meth.helper  pkg/nest.py:" in body
    imp = handler.get(id="r::pkg.a", view="importers").body
    assert "mod (in outer.inner)" in imp


def test_nested_function_addressable_by_qualname(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.nest.outer.inner").body
    assert "# pkg.nest.outer.inner  (function" in body
    assert "pkg.a.g" in body  # its own call edge


def test_repeated_nested_name_disambiguated(handler: PythonHandler) -> None:
    assert "# pkg.nest.twice.step  " in handler.get(id="r::pkg.nest.twice.step").body
    assert (
        "# pkg.nest.twice.step#2  " in handler.get(id="r::pkg.nest.twice.step#2").body
    )


def test_outline_omits_nested_defs(handler: PythonHandler) -> None:
    body = handler.get(id="r/pkg/nest.py").body
    assert "def outer" in body
    assert "def inner" not in body
    assert "def helper" not in body
