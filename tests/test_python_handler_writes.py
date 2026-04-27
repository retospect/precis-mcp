"""Tests for ``PythonHandler.put`` — the four write modes and three gates.

Each test uses a tmp_path repo, performs a write, and asserts on (a)
the response body, (b) the file contents on disk, and (c) the
post-write cache state. Ruff is normally available in the dev venv;
tests that depend on ruff outcomes mock or skip if it isn't.
"""

from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers.python import PythonHandler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        '''
        """Module."""


        def helper(x: int) -> int:
            """A helper."""
            return x + 1


        class C:
            """A class."""

            def greet(self, name: str) -> str:
                return helper(len(name))

            def shout(self, name: str) -> str:
                return self.greet(name).upper()
        ''',
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(roots={"r": repo})


# Whether ruff is reachable from the test process. Some assertions
# need the real fix+format outcome and skip if ruff is absent.
_RUFF_AVAILABLE = (
    shutil.which("ruff") is not None or (Path(sys.executable).parent / "ruff").is_file()
)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_put_requires_mode(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="mode= is required"):
        handler.put(id="r/pkg/m.py", text="x = 1\n")


def test_put_rejects_unknown_mode(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="mode= is required"):
        handler.put(id="r/pkg/m.py", text="x = 1\n", mode="bogus")


def test_put_requires_id(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="put requires id="):
        handler.put(text="x = 1\n", mode="replace")


def test_put_unknown_alias_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="unknown python repo"):
        handler.put(id="bogus/x.py", text="x=1", mode="create")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_writes_new_file(handler: PythonHandler, repo: Path) -> None:
    out = handler.put(
        id="r/pkg/new.py",
        text='"""New module."""\n\n\ndef foo():\n    return 1\n',
        mode="create",
    )
    assert "ast.parse:           ok" in out.body
    assert "qualname preserved:  ok" in out.body
    new_file = repo / "pkg" / "new.py"
    assert new_file.exists()
    body = new_file.read_text()
    assert "def foo()" in body


def test_create_refuses_overwrite(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="already exists"):
        handler.put(id="r/pkg/m.py", text="x = 1\n", mode="create")


def test_create_rejects_qualname(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="not a qualname"):
        handler.put(id="r::pkg.new", text="x = 1\n", mode="create")


def test_create_rejects_selector(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="does not accept a selector"):
        handler.put(id="r/pkg/new.py~L1-L1", text="x = 1\n", mode="create")


def test_create_requires_text(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires text"):
        handler.put(id="r/pkg/new.py", mode="create")


def test_create_refuses_path_escape(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="escapes repo root"):
        handler.put(id="r/../escape.py", text="x = 1\n", mode="create")


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


def test_append_adds_at_eof(handler: PythonHandler, repo: Path) -> None:
    handler.put(
        id="r/pkg/m.py",
        text="\n\ndef appended() -> int:\n    return 0\n",
        mode="append",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "def appended()" in body
    # And it parses (gate 1 happy).
    import ast

    ast.parse(body)


def test_append_requires_text(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires text"):
        handler.put(id="r/pkg/m.py", mode="append")


def test_append_rejects_selector(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="does not accept a selector"):
        handler.put(id="r/pkg/m.py~L1-L1", text="x = 1\n", mode="append")


def test_append_rejects_qualname(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires a file path"):
        handler.put(id="r::pkg.m.helper", text="x = 1\n", mode="append")


# ---------------------------------------------------------------------------
# replace — by qualname (the spec's primary path)
# ---------------------------------------------------------------------------


def test_replace_by_qualname_swaps_function(handler: PythonHandler, repo: Path) -> None:
    handler.put(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x * 2\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x * 2" in body
    assert "return x + 1" not in body


def test_replace_by_qualname_swaps_method(handler: PythonHandler, repo: Path) -> None:
    """Methods preserve indentation discipline — caller supplies it."""
    handler.put(
        id="r::pkg.m.C.greet",
        text="    def greet(self, name: str) -> str:\n        return f'hello {name}'\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "f'hello {name}'" in body or 'f"hello {name}"' in body


def test_replace_unknown_qualname_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="symbol .* not found"):
        handler.put(
            id="r::pkg.m.NoSuchSymbol",
            text="def NoSuchSymbol(): pass\n",
            mode="replace",
        )


# ---------------------------------------------------------------------------
# replace — by line range / block selector / whole file
# ---------------------------------------------------------------------------


def test_replace_by_line_range(handler: PythonHandler, repo: Path) -> None:
    """Replace lines containing the helper function (lines 4-6)."""
    handler.put(
        id="r/pkg/m.py~L4-L6",
        text="def helper(x):\n    return x - 1\n",
        mode="replace",
        allow_rename=True,  # signature changes type annotation
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x - 1" in body


def test_replace_by_block_selector(handler: PythonHandler, repo: Path) -> None:
    handler.put(
        id="r/pkg/m.py~C.greet",
        text="    def greet(self, name: str) -> str:\n        return name.upper()\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "name.upper()" in body


def test_replace_whole_file(handler: PythonHandler, repo: Path) -> None:
    """No selector → replace the entire file. allow_rename needed because
    the symbol set differs."""
    handler.put(
        id="r/pkg/m.py",
        text='"""New."""\n\n\ndef brand_new() -> int:\n    return 42\n',
        mode="replace",
        allow_rename=True,
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "brand_new" in body
    assert "helper" not in body


def test_replace_requires_text(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires text"):
        handler.put(id="r::pkg.m.helper", mode="replace")


# ---------------------------------------------------------------------------
# Gate 1 — AST
# ---------------------------------------------------------------------------


def test_gate1_blocks_syntax_error(handler: PythonHandler, repo: Path) -> None:
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="ast.parse failed"):
        handler.put(
            id="r::pkg.m.helper",
            text="def helper(\n",  # truncated — syntax error
            mode="replace",
        )
    # File untouched.
    assert (repo / "pkg" / "m.py").read_text() == pre


# ---------------------------------------------------------------------------
# Gate 2 — qualname drop
# ---------------------------------------------------------------------------


def test_gate2_blocks_accidental_rename(handler: PythonHandler, repo: Path) -> None:
    """Replacing `helper` with a body that defines `renamed` drops a
    qualname — gate 2 must reject."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="qualname-drop"):
        handler.put(
            id="r::pkg.m.helper",
            text="def renamed(x: int) -> int:\n    return x + 1\n",
            mode="replace",
        )
    # File still has original content.
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_gate2_bypassed_with_allow_rename(handler: PythonHandler, repo: Path) -> None:
    handler.put(
        id="r::pkg.m.helper",
        text="def renamed(x: int) -> int:\n    return x + 1\n",
        mode="replace",
        allow_rename=True,
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "def renamed" in body
    assert "def helper" not in body


def test_gate2_blocks_class_method_drop(handler: PythonHandler, repo: Path) -> None:
    """Replacing class C with a body that omits `shout` should fail —
    `pkg.m.C.shout` would disappear from the file."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="qualname-drop"):
        handler.put(
            id="r::pkg.m.C",
            text='''class C:
    """A class."""

    def greet(self, name: str) -> str:
        return name
''',
            mode="replace",
        )
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_gate2_passes_when_symbol_moved_within_file(
    handler: PythonHandler, repo: Path
) -> None:
    """Moving `helper` out of class C (or anywhere in the file) is fine
    — its qualname survives at the file level. Here we just rewrite the
    function body and the gate sees `pkg.m.helper` still present."""
    handler.put(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    # moved logic\n    return x + 100\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x + 100" in body


# ---------------------------------------------------------------------------
# Gate 3 — ruff (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff binary not on PATH")
def test_gate3_runs_ruff_fix(handler: PythonHandler, repo: Path) -> None:
    """A replacement with an unused import gets ruff-fixed before disk write."""
    handler.put(
        id="r::pkg.m.helper",
        text="import json\ndef helper(x: int) -> int:\n    return x + 1\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "import json" not in body  # ruff stripped it


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff binary not on PATH")
def test_gate3_runs_ruff_format(handler: PythonHandler, repo: Path) -> None:
    """Tight syntax gets reformatted by ruff format."""
    handler.put(
        id="r::pkg.m.helper",
        text="def helper(x:int)->int:\n    return x+1\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    # Spaces around '->' and ':' get normalised by ruff format.
    assert "x: int" in body
    assert "-> int" in body


def test_gate3_missing_ruff_does_not_block(
    handler: PythonHandler, repo: Path, monkeypatch
) -> None:
    """If ruff isn't found, the write proceeds with the unfixed buffer
    and the response surfaces the skip."""
    from precis.handlers import _python_write

    monkeypatch.setattr(_python_write, "_find_ruff", lambda: None)
    out = handler.put(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x + 99\n",
        mode="replace",
    )
    assert "ruff:                skipped" in out.body
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x + 99" in body  # write still happened


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_by_qualname_removes_function(
    handler: PythonHandler, repo: Path
) -> None:
    handler.put(id="r::pkg.m.helper", mode="delete")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def helper" not in body


def test_delete_by_block_selector(handler: PythonHandler, repo: Path) -> None:
    handler.put(id="r/pkg/m.py~C.shout", mode="delete")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def shout" not in body
    assert "def greet" in body  # didn't delete the wrong method


def test_delete_by_line_range(handler: PythonHandler, repo: Path) -> None:
    """Lines 4-6 are the helper function in the dedented fixture."""
    handler.put(id="r/pkg/m.py~L4-L6", mode="delete")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def helper" not in body


def test_delete_refuses_whole_file(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="cannot delete a whole file"):
        handler.put(id="r/pkg/m.py", mode="delete")


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_cache_sees_post_write_state(handler: PythonHandler, repo: Path) -> None:
    """A get() right after a put() reflects the new content — no stale
    symbols cached. The mtime change drives RepoCache invalidation."""
    handler.put(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x + 999\n",
        mode="replace",
    )
    out = handler.get(id="r/pkg/m.py", view="source")
    assert "x + 999" in out.body


def test_create_then_get_lists_new_symbol(handler: PythonHandler, repo: Path) -> None:
    """A symbol created by put becomes addressable on the next get."""
    handler.put(
        id="r/pkg/n.py",
        text='"""new module."""\n\n\ndef created_fn() -> int:\n    return 7\n',
        mode="create",
    )
    out = handler.get(id="r::pkg.n.created_fn")
    assert "pkg.n.created_fn" in out.body


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_response_includes_change_summary(handler: PythonHandler, repo: Path) -> None:
    out = handler.put(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x + 5\n",
        mode="replace",
    )
    assert "Replaced" in out.body
    assert "Next:" in out.body
    assert "r::pkg.m.helper" in out.body


def test_kind_spec_supports_put() -> None:
    """The KindSpec advertises put + the four modes — the dispatcher
    relies on this for surface validation."""
    from precis.handlers.python import _SUPPORTED_PUT_MODES

    assert PythonHandler.spec.supports_put is True
    assert tuple(PythonHandler.spec.modes) == _SUPPORTED_PUT_MODES
