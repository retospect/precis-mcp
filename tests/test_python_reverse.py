"""Reverse lookups on the python kind: callers / importers / imports / pattern."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.python import PythonHandler


def _write(repo: Path, rel: str, content: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def handler(tmp_path: Path) -> PythonHandler:
    _write(tmp_path, "pkg/__init__.py", '"""pkg."""\n')
    _write(
        tmp_path,
        "pkg/core.py",
        """
        import functools


        def helper(x: int) -> int:
            return x + 1


        class Box:
            def put(self, v):
                return helper(v)

            def twice(self, v):
                return self.put(self.put(v))

            @property
            def size(self) -> int:
                return 1

            @functools.cache
            async def fetch(self, url: str) -> str:
                return url


        async def serve() -> None:
            pass
        """,
    )
    _write(
        tmp_path,
        "pkg/user.py",
        """
        import os
        from pkg.core import Box, helper
        from pkg import core


        def run():
            b = Box()
            return helper(1) + b.put(2)
        """,
    )
    _write(tmp_path, "pkg/lonely.py", "X = 1\n")
    return PythonHandler(hub=Hub(), roots={"r": tmp_path})


def test_callers_lists_call_sites_with_anchor_and_line(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.helper", view="callers").body
    assert "r::pkg.core.Box.put  pkg/core.py:" in body
    assert "r::pkg.user.run  pkg/user.py:" in body
    assert "2 resolved" in body


def test_callers_excludes_self_calls(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.Box.put", view="callers").body
    # twice() calls self.put; put itself is not listed as its own caller.
    assert "r::pkg.core.Box.twice" in body
    assert "r::pkg.core.Box.put  " not in body


def test_callers_lexical_section_for_untyped_receiver(handler: PythonHandler) -> None:
    # `b.put(2)` in run(): receiver type unknown -> unresolved same-name lead.
    body = handler.get(id="r::pkg.core.Box.put", view="callers").body
    assert "Unresolved, same name `put`" in body
    assert "r::pkg.user.run" in body


def test_callers_none(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core.serve", view="callers").body
    assert "(no resolved call sites)" in body


def test_callers_unknown_symbol(handler: PythonHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id="r::pkg.core.nope", view="callers")


def test_reverse_views_require_qualname_id(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.get(id="r", view="importers")


def test_importers_lists_importing_modules(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.core", view="importers").body
    assert "r::pkg.user  pkg/user.py" in body
    assert "1 modules" in body
    # from-import names and the submodule import are both reported.
    assert "Box" in body and "helper" in body and "core" in body


def test_importers_none(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.lonely", view="importers").body
    assert "0 modules" in body


def test_importers_rejects_non_module(handler: PythonHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id="r::pkg.core.Box", view="importers")


def test_imports_splits_in_repo_and_external(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.user", view="imports").body
    assert "In-repo:" in body and "r::pkg.core" in body
    assert "External:" in body and "os" in body


def test_imports_empty_module(handler: PythonHandler) -> None:
    body = handler.get(id="r::pkg.lonely", view="imports").body
    assert "(no module-scope imports)" in body


def test_pattern_async(handler: PythonHandler) -> None:
    body = handler.search(q="async", mode="pattern").body
    assert "r::pkg.core.serve" in body and "r::pkg.core.Box.fetch" in body
    assert "r::pkg.core.helper" not in body


def test_pattern_decorator_and_conjunction(handler: PythonHandler) -> None:
    assert "r::pkg.core.Box.size" in handler.search(q="@property", mode="pattern").body
    both = handler.search(q="async @functools\\.cache", mode="pattern").body
    assert "r::pkg.core.Box.fetch" in both and "r::pkg.core.serve" not in both


def test_pattern_regex_on_signature(handler: PythonHandler) -> None:
    body = handler.search(q="url:\\s*str", mode="pattern").body
    assert "r::pkg.core.Box.fetch" in body


def test_pattern_bad_regex_and_bad_mode(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.search(q="(", mode="pattern")
    with pytest.raises(BadInput):
        handler.search(q="x", mode="bogus")


def test_callers_on_own_repo_finds_resolved_edges() -> None:
    """Smoke on the real tree: a well-called helper has resolved callers."""
    root = Path(__file__).resolve().parent.parent
    h = PythonHandler(hub=Hub(), roots={"precis": root})
    body = h.get(
        id="precis::precis.python_index.indexer.index_repo", view="callers"
    ).body
    assert body.startswith("# callers of precis.python_index.indexer.index_repo")
    body = h.get(id="precis::precis.handlers.python", view="importers").body
    assert "importers of precis.handlers.python" in body
