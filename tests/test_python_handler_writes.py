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

from precis.dispatch import Hub
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
    return PythonHandler(hub=Hub(), roots={"r": repo})


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
    handler.edit(
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
        handler.edit(id="r/pkg/m.py", mode="append")


def test_append_rejects_selector(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="does not accept a selector"):
        handler.edit(id="r/pkg/m.py~L1-L1", text="x = 1\n", mode="append")


def test_append_rejects_qualname(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires a file path"):
        handler.edit(id="r::pkg.m.helper", text="x = 1\n", mode="append")


# ---------------------------------------------------------------------------
# replace — by qualname (the spec's primary path)
# ---------------------------------------------------------------------------


def test_replace_by_qualname_swaps_function(handler: PythonHandler, repo: Path) -> None:
    handler.edit(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x * 2\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x * 2" in body
    assert "return x + 1" not in body


def test_replace_by_qualname_swaps_method(handler: PythonHandler, repo: Path) -> None:
    """Methods preserve indentation discipline — caller supplies it."""
    handler.edit(
        id="r::pkg.m.C.greet",
        text="    def greet(self, name: str) -> str:\n        return f'hello {name}'\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "f'hello {name}'" in body or 'f"hello {name}"' in body


def test_replace_unknown_qualname_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="symbol .* not found"):
        handler.edit(
            id="r::pkg.m.NoSuchSymbol",
            text="def NoSuchSymbol(): pass\n",
            mode="replace",
        )


# ---------------------------------------------------------------------------
# replace — by line range / block selector / whole file
# ---------------------------------------------------------------------------


def test_replace_by_line_range(handler: PythonHandler, repo: Path) -> None:
    """Replace lines containing the helper function (lines 4-6)."""
    handler.edit(
        id="r/pkg/m.py~L4-L6",
        text="def helper(x):\n    return x - 1\n",
        mode="replace",
        allow_rename=True,  # signature changes type annotation
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "return x - 1" in body


def test_replace_by_block_selector(handler: PythonHandler, repo: Path) -> None:
    handler.edit(
        id="r/pkg/m.py~C.greet",
        text="    def greet(self, name: str) -> str:\n        return name.upper()\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "name.upper()" in body


def test_replace_whole_file(handler: PythonHandler, repo: Path) -> None:
    """No selector → replace the entire file. allow_rename needed because
    the symbol set differs."""
    handler.edit(
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
        handler.edit(id="r::pkg.m.helper", mode="replace")


# ---------------------------------------------------------------------------
# Gate 1 — AST
# ---------------------------------------------------------------------------


def test_gate1_blocks_syntax_error(handler: PythonHandler, repo: Path) -> None:
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="ast.parse failed"):
        handler.edit(
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
        handler.edit(
            id="r::pkg.m.helper",
            text="def renamed(x: int) -> int:\n    return x + 1\n",
            mode="replace",
        )
    # File still has original content.
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_gate2_bypassed_with_allow_rename(handler: PythonHandler, repo: Path) -> None:
    handler.edit(
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
        handler.edit(
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
    handler.edit(
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
    handler.edit(
        id="r::pkg.m.helper",
        text="import json\ndef helper(x: int) -> int:\n    return x + 1\n",
        mode="replace",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "import json" not in body  # ruff stripped it


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff binary not on PATH")
def test_gate3_runs_ruff_format(handler: PythonHandler, repo: Path) -> None:
    """Tight syntax gets reformatted by ruff format."""
    handler.edit(
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
    out = handler.edit(
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
    handler.delete(id="r::pkg.m.helper")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def helper" not in body


def test_delete_by_block_selector(handler: PythonHandler, repo: Path) -> None:
    handler.delete(id="r/pkg/m.py~C.shout")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def shout" not in body
    assert "def greet" in body  # didn't delete the wrong method


def test_delete_by_line_range(handler: PythonHandler, repo: Path) -> None:
    """Lines 4-6 are the helper function in the dedented fixture."""
    handler.delete(id="r/pkg/m.py~L4-L6")
    body = (repo / "pkg" / "m.py").read_text()
    assert "def helper" not in body


def test_delete_refuses_whole_file(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="cannot delete a whole file"):
        handler.delete(id="r/pkg/m.py")


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_cache_sees_post_write_state(handler: PythonHandler, repo: Path) -> None:
    """A get() right after a put() reflects the new content — no stale
    symbols cached. The mtime change drives RepoCache invalidation."""
    handler.edit(
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
    out = handler.edit(
        id="r::pkg.m.helper",
        text="def helper(x: int) -> int:\n    return x + 5\n",
        mode="replace",
    )
    assert "Replaced" in out.body
    assert "Next:" in out.body
    assert "r::pkg.m.helper" in out.body


def test_kind_spec_supports_put() -> None:
    """The KindSpec advertises put + the supported modes — the dispatcher
    relies on this for surface validation."""
    from precis.handlers.python import _SUPPORTED_PUT_MODES

    assert PythonHandler.spec.supports_put is True
    assert tuple(PythonHandler.spec.modes) == _SUPPORTED_PUT_MODES
    # v1 modes: create + append + replace + delete + edit + insert.
    assert "edit" in _SUPPORTED_PUT_MODES
    assert "insert" in _SUPPORTED_PUT_MODES


# ---------------------------------------------------------------------------
# mode='edit' — anchored sub-region replace
# ---------------------------------------------------------------------------


def test_edit_swaps_token_in_function(handler: PythonHandler, repo: Path) -> None:
    """The motivating case: rename one call site without touching others."""
    handler.edit(
        id="r::pkg.m.helper",
        mode="find-replace",
        find="x + 1",
        text="x + 2",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "x + 2" in body
    assert "x + 1" not in body
    # Other symbols intact.
    assert "def greet" in body
    assert "def shout" in body


def test_edit_uses_anchors_to_disambiguate(handler: PythonHandler, repo: Path) -> None:
    """`name` appears multiple times across greet/shout; anchored
    edit picks one specific occurrence."""
    handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="name",
        before="len(",
        after=")",
        text="full_name",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "len(full_name)" in body
    # Other `name` occurrences unchanged.
    assert "self, name: str" in body  # both methods' signatures intact
    assert "self.greet(name)" in body  # shout's body intact


def test_edit_match_all_replaces_every_occurrence(
    handler: PythonHandler, repo: Path
) -> None:
    handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="name: str",
        text="name: bytes",
        match="all",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert body.count("name: bytes") == 2
    assert "name: str" not in body


def test_edit_unique_match_errors_when_ambiguous(
    handler: PythonHandler, repo: Path
) -> None:
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput) as excinfo:
        handler.edit(id="r/pkg/m.py", mode="find-replace", find="name", text="renamed")
    msg = str(excinfo.value)
    assert "matches" in msg
    # File untouched on validation failure.
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_edit_within_qualname_region(handler: PythonHandler, repo: Path) -> None:
    """Editing scoped to one symbol's source — the anchored search
    only sees that symbol's lines."""
    handler.edit(
        id="r::pkg.m.C.shout",
        mode="find-replace",
        find=".upper()",
        text=".lower()",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert ".lower()" in body
    # `greet` body unchanged.
    assert "return helper(len(name))" in body


def test_edit_blocks_syntax_breakage(handler: PythonHandler, repo: Path) -> None:
    """Gate 1 (ast.parse) catches an edit that produces invalid Python."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="ast.parse failed"):
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="def helper",
            text="def )(broken",
        )
    # File untouched.
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_edit_blocks_qualname_drop_via_anchored_replace(
    handler: PythonHandler, repo: Path
) -> None:
    """An anchored edit that renames a `def` should fail gate 2."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="qualname-drop"):
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="def helper(",
            text="def renamed(",
        )
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_edit_qualname_rename_with_allow_rename(
    handler: PythonHandler, repo: Path
) -> None:
    handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="def helper(",
        text="def renamed(",
        allow_rename=True,
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "def renamed(" in body
    assert "def helper(" not in body


def test_edit_not_found_carries_actionable_hint(handler: PythonHandler) -> None:
    with pytest.raises(BadInput) as excinfo:
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="nonexistent_token",
            text="x",
        )
    msg = str(excinfo.value)
    assert "not found" in msg


def test_edit_requires_find(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires find="):
        handler.edit(id="r/pkg/m.py", mode="find-replace", text="x")


def test_edit_requires_text(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires text="):
        handler.edit(id="r/pkg/m.py", mode="find-replace", find="def helper")


# ---------------------------------------------------------------------------
# mode='insert' — anchored insert
# ---------------------------------------------------------------------------


def test_insert_after_function_definition(handler: PythonHandler, repo: Path) -> None:
    """Insert a new top-level function after the existing helper."""
    handler.edit(
        id="r/pkg/m.py",
        mode="insert",
        find="    return x + 1\n",
        where="after",
        text="\n\ndef twice(x: int) -> int:\n    return x * 2\n",
    )
    body = (repo / "pkg" / "m.py").read_text()
    assert "def twice" in body
    assert "def helper" in body  # original preserved


def test_insert_requires_where(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires where="):
        handler.edit(
            id="r/pkg/m.py",
            mode="insert",
            find="def helper",
            text="x = 1\n",
        )


def test_insert_blocks_syntax_breakage(handler: PythonHandler, repo: Path) -> None:
    """An insert that breaks syntax fails gate 1, file untouched."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="ast.parse failed"):
        handler.edit(
            id="r/pkg/m.py",
            mode="insert",
            find="def helper",
            where="before",
            text="def )(broken",
        )
    assert (repo / "pkg" / "m.py").read_text() == pre


# ---------------------------------------------------------------------------
# mode='edit' / mode='insert' — dry_run
# ---------------------------------------------------------------------------


def test_edit_dry_run_does_not_write(handler: PythonHandler, repo: Path) -> None:
    """dry_run=True must NOT touch the file on disk."""
    pre = (repo / "pkg" / "m.py").read_text()
    out = handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="x + 1",
        text="x + 2",
        dry_run=True,
    )
    # File untouched.
    assert (repo / "pkg" / "m.py").read_text() == pre
    # Header advertises a dry run plus the gates.
    assert "DRY RUN" in out.body
    assert "ast.parse:" in out.body
    assert "qualname-drop:" in out.body
    assert "ruff:" in out.body


def test_edit_dry_run_diff_format(handler: PythonHandler, repo: Path) -> None:
    out = handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="x + 1",
        text="x + 2",
        dry_run="diff",
    )
    # Standard difflib unified-diff headers.
    assert "--- a/r/pkg/m.py" in out.body
    assert "+++ b/r/pkg/m.py" in out.body
    assert "-    return x + 1" in out.body
    assert "+    return x + 2" in out.body


def test_edit_dry_run_full_format(handler: PythonHandler, repo: Path) -> None:
    out = handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="x + 1",
        text="x + 2",
        dry_run="full",
    )
    assert "DRY RUN" in out.body
    # Full view shows the post-edit line marked with `> `; no diff headers.
    assert "x + 2" in out.body
    assert "> " in out.body
    assert "--- a/" not in out.body


def test_edit_dry_run_runs_gates(handler: PythonHandler, repo: Path) -> None:
    """A syntactically broken dry_run still fires gate 1 (no write)."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="ast.parse failed"):
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="def helper",
            text="def )(broken",
            dry_run=True,
        )
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_edit_dry_run_qualname_drop_blocks(handler: PythonHandler, repo: Path) -> None:
    """dry_run still enforces gate 2 (qualname-drop)."""
    pre = (repo / "pkg" / "m.py").read_text()
    with pytest.raises(BadInput, match="qualname-drop"):
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="def helper(",
            text="def renamed(",
            dry_run=True,
        )
    assert (repo / "pkg" / "m.py").read_text() == pre


def test_edit_dry_run_qualname_with_allow_rename(
    handler: PythonHandler, repo: Path
) -> None:
    """dry_run + allow_rename: gate 2 passes, file still untouched."""
    pre = (repo / "pkg" / "m.py").read_text()
    out = handler.edit(
        id="r/pkg/m.py",
        mode="find-replace",
        find="def helper(",
        text="def renamed(",
        allow_rename=True,
        dry_run=True,
    )
    assert (repo / "pkg" / "m.py").read_text() == pre
    assert "DRY RUN" in out.body
    assert "qualname-drop:" in out.body
    assert "ok" in out.body  # allow_rename → ok


def test_insert_dry_run_does_not_write(handler: PythonHandler, repo: Path) -> None:
    pre = (repo / "pkg" / "m.py").read_text()
    out = handler.edit(
        id="r/pkg/m.py",
        mode="insert",
        find="    return x + 1\n",
        where="after",
        text="\n\ndef twice(x: int) -> int:\n    return x * 2\n",
        dry_run=True,
    )
    assert (repo / "pkg" / "m.py").read_text() == pre
    assert "DRY RUN" in out.body
    # Diff shows the new function as added lines.
    assert "+def twice" in out.body or "+\n+def twice" in out.body


def test_edit_dry_run_rejects_unknown_mode(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="dry_run must be"):
        handler.edit(
            id="r/pkg/m.py",
            mode="find-replace",
            find="x + 1",
            text="x + 2",
            dry_run="brief",
        )


# ---------------------------------------------------------------------------
# Regression: critic CRITICAL-C B2 — double-root-prefix path guard
# ---------------------------------------------------------------------------


@pytest.fixture
def package_root_repo(tmp_path: Path) -> Path:
    """Repo whose root IS itself a package (has __init__.py at root).

    This matches real-world precis-style deployments
    (``PRECIS_PYTHON_ROOTS=precis=<pkg>/src/precis``) where the alias
    points inside the package, not above it. Callers who paste the
    full repo-root-relative path (``src/precis/foo.py``) produce a
    double-prefixed id like ``alias/src/precis/foo.py``.

    The root directory is named literally ``precis`` so the indexer
    produces qualnames starting with ``precis.`` — matching the
    alias and production layout.
    """
    root = tmp_path / "precis"
    root.mkdir()
    _write(root, "__init__.py", "")
    _write(
        root,
        "registry.py",
        '''
        """Top-level module inside the root package."""


        def lookup(name: str) -> str:
            return name
        ''',
    )
    return root


@pytest.fixture
def package_root_handler(package_root_repo: Path) -> PythonHandler:
    """Handler whose alias 'precis' points at a root that is a package."""
    return PythonHandler(hub=Hub(), roots={"precis": package_root_repo})


def test_put_create_rejects_doubled_root_prefix(
    package_root_handler: PythonHandler, package_root_repo: Path
) -> None:
    """Passing ``precis/src/precis/foo.py`` when root is already
    ``.../src/precis`` would silently create a phantom nested
    ``src/precis/src/precis/foo.py`` tree. Reject instead."""
    with pytest.raises(BadInput, match="repeats the root's path"):
        package_root_handler.put(
            id=f"precis/{package_root_repo.parts[-1]}/foo.py",
            mode="create",
            text="x = 1\n",
        )
    # And no phantom directory was created.
    assert not (package_root_repo / package_root_repo.parts[-1]).exists()


def test_put_create_reject_includes_suggested_unprefixed_path(
    package_root_handler: PythonHandler, package_root_repo: Path
) -> None:
    """The BadInput's ``next=`` hint must point at the address the
    caller probably meant — alias plus the *stripped* relative path,
    not the doubled prefix."""
    root_name = package_root_repo.parts[-1]
    with pytest.raises(BadInput) as exc_info:
        package_root_handler.put(
            id=f"precis/{root_name}/foo.py",
            mode="create",
            text="x = 1\n",
        )
    err = exc_info.value
    hint = str(err.next)
    # Correct form: alias + bare filename.
    assert "'precis/foo.py'" in hint
    # Must NOT recreate the doubled form.
    assert f"precis/{root_name}/foo.py" not in hint


# ---------------------------------------------------------------------------
# Regression: critic MAJOR-C F1 — qualname-drop gate false positive
# ---------------------------------------------------------------------------


def test_replace_identical_body_under_package_root_is_noop(
    package_root_handler: PythonHandler, package_root_repo: Path
) -> None:
    """When the repo root is itself a package, the handler-side module
    qualname helper used to short by one segment relative to the
    indexer's qualname (``lookup`` vs ``precis.registry.lookup``). A
    replace with identical body then tripped the qualname-drop gate
    with a phantom 'would disappear' error. Now: round-trip the same
    source and succeed."""
    unchanged = "def lookup(name: str) -> str:\n    return name\n"
    out = package_root_handler.edit(
        id="precis::precis.registry.lookup",
        mode="replace",
        text=unchanged,
    )
    assert "ast.parse" in out.body
    assert "qualname preserved" in out.body


def test_replace_identical_body_under_package_root_via_line_range(
    package_root_handler: PythonHandler, package_root_repo: Path
) -> None:
    """Same bug via the ``~L<start>-<end>`` selector form."""
    unchanged = "def lookup(name: str) -> str:\n    return name\n"
    # registry.py after dedent is:
    #   L1: """Top-level module inside the root package."""
    #   L2: (blank)
    #   L3: (blank)
    #   L4: def lookup(name: str) -> str:
    #   L5:     return name
    out = package_root_handler.edit(
        id="precis/registry.py~L4-5",
        mode="replace",
        text=unchanged,
    )
    assert "qualname preserved" in out.body
