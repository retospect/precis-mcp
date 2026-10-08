"""Track-A line reads name the enclosing symbol."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.python import PythonHandler


@pytest.fixture
def handler(tmp_path: Path) -> PythonHandler:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text('"""pkg."""\n', encoding="utf-8")
    (tmp_path / "pkg" / "m.py").write_text(
        textwrap.dedent(
            """\
            import os


            def boot():
                x = 1
                return x


            class Box:
                def put(self):
                    y = 2
                    return y
            """
        ),
        encoding="utf-8",
    )
    return PythonHandler(hub=Hub(), roots={"r": tmp_path})


def test_single_line_names_function(handler: PythonHandler) -> None:
    body = handler.get(id="r/pkg/m.py~L5").body
    assert "x = 1" in body
    assert "boot (lines 4-6)  r::pkg.m.boot" in body
    assert "get(kind='python', id='r::pkg.m.boot')" in body


def test_method_is_innermost(handler: PythonHandler) -> None:
    body = handler.get(id="r/pkg/m.py~L11").body
    assert "r::pkg.m.Box.put" in body
    assert "r::pkg.m.Box\n" not in body


def test_range_spanning_two_symbols_lists_both(handler: PythonHandler) -> None:
    body = handler.get(id="r/pkg/m.py~L5-L11").body
    assert "r::pkg.m.boot" in body and "r::pkg.m.Box.put" in body


def test_module_level_line_has_no_note(handler: PythonHandler) -> None:
    assert "Enclosing" not in handler.get(id="r/pkg/m.py~L1").body
