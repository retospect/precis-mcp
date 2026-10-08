"""Function-local imports resolve calls, feed `importers`, and unresolved
same-name call sites surface as labelled leads (never silently dropped)."""

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
    _write(tmp_path, "pkg/a.py", "def f(x):\n    return x\n")
    _write(
        tmp_path,
        "pkg/local.py",
        """
        def uses_local():
            from pkg.a import f
            return f(1)


        def uses_guarded():
            try:
                from pkg.a import f as g
            except ImportError:
                g = None
            return g(2)


        def uses_module_alias():
            import pkg.a as mod
            return mod.f(3)
        """,
    )
    _write(
        tmp_path,
        "pkg/mystery.py",
        """
        def unbound():
            return f(4)  # `f` bound nowhere the indexer can see
        """,
    )
    return PythonHandler(hub=Hub(), roots={"r": tmp_path})


def test_callers_view_resolves_function_local_import(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.a.f", view="callers").body
    assert "3 resolved" in body
    assert "r::pkg.local.uses_local  pkg/local.py:" in body
    assert "r::pkg.local.uses_guarded  pkg/local.py:" in body
    assert "r::pkg.local.uses_module_alias  pkg/local.py:" in body


def test_symbol_view_called_by_includes_function_local_import(
    handler: PythonHandler,
) -> None:
    body = handler.get(id="r::pkg.a.f").body
    assert "Called by:" in body
    assert "pkg.local.uses_local" in body


def test_unresolved_bare_name_call_is_a_labelled_lead(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.a.f", view="callers").body
    assert "Unresolved, same name `f`" in body
    assert "r::pkg.mystery.unbound" in body


def test_importers_reports_function_local_importers(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.a", view="importers").body
    assert "r::pkg.local  pkg/local.py" in body
    assert "f (in uses_local)" in body
    assert "Function-local imports are not indexed" not in body
