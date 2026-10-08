"""Callgraph Next-hint shape, module-entry handling, call-index cache."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _python_callgraph as cgraph
from precis.handlers.python import PythonHandler
from precis.python_index import index_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "m.py").write_text(
        textwrap.dedent(
            """
            def helper() -> int:
                return 1


            def main() -> int:
                return helper()


            class C:
                def use(self) -> int:
                    helper()
                    return self.other()

                def other(self) -> int:
                    return 0
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )
    return tmp_path


def test_render_next_hint_uses_args_dict(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=1)
    body = cgraph.render_callgraph(
        tree, alias="r", entry="pkg.m.main", max_depth=1, cross_repo=False
    )
    assert "args={'entry': 'pkg.m.main', 'depth': 3}" in body
    assert "entry='pkg.m.main'," not in body


def test_handler_error_hints_use_args_dict(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    with pytest.raises(BadInput) as ei:
        handler.get(id="r", view="callgraph")
    assert "args={'entry'" in str(ei.value.next)
    with pytest.raises(BadInput) as ei2:
        handler.get(id="r", view="callgraph", entry="pkg.m.main", depth=99)
    assert "args={'entry': 'pkg.m.main', 'depth': 3}" in str(ei2.value.next)


def test_module_entry_lists_top_level_callables(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m", max_depth=2)
    assert tree.children == []
    for qn in ("pkg.m.main", "pkg.m.helper", "pkg.m.C"):
        assert qn in tree.note
    body = cgraph.render_callgraph(
        tree, alias="r", entry="pkg.m", max_depth=2, cross_repo=False
    )
    assert "no module-level calls" in body
    assert "pkg.m.main" in body


def test_module_entry_via_handler(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    out = handler.get(id="r", view="callgraph", entry="pkg.m", depth=2)
    assert "Top-level callables" in out.body
    assert "pkg.m.main" in out.body


def test_function_entry_has_no_module_note(repo: Path) -> None:
    idx = index_repo(repo)
    assert cgraph.build_callgraph(idx, entry="pkg.m.main").note == ""


def test_call_index_cached_per_repo_index(repo: Path) -> None:
    idx = index_repo(repo)
    assert cgraph._index_calls(idx) is cgraph._index_calls(idx)
    assert cgraph._index_calls(index_repo(repo)) is not cgraph._index_calls(idx)


def test_class_aggregation_matches_methods(repo: Path) -> None:
    edges = cgraph._index_calls(index_repo(repo))
    assert edges["pkg.m.C"]
    assert {e.callee for e in edges["pkg.m.C"]} == {
        e.callee for m in ("use", "other") for e in edges.get(f"pkg.m.C.{m}", [])
    }
