"""Tests for `precis.handlers.python.PythonHandler`.

DB-free — the python kind is in-memory by design. Each test stands up
a tiny repo in `tmp_path`, constructs the handler, and exercises the
read paths and search.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.python import PythonHandler, _parse_id

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    file = repo / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small but realistic Python repo: package + module + class."""
    _write(tmp_path, "pkg/__init__.py", '"""pkg root."""\n')
    _write(
        tmp_path,
        "pkg/m.py",
        '''
        """Module-level docstring."""

        from os.path import join


        def helper(x: int) -> int:
            """A helper."""
            return x + 1


        class Greeter:
            """Says hi."""

            def greet(self, name: str) -> str:
                """Greet a name."""
                return helper(len(name))

            def shout(self, name: str) -> str:
                return self.greet(name).upper()
        ''',
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construct_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        PythonHandler(hub=Hub(), roots={"r": tmp_path / "no-such-dir"})


def test_construct_rejects_invalid_alias(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid python repo alias"):
        PythonHandler(hub=Hub(), roots={"bad/alias": tmp_path})


def test_construct_resolves_paths(tmp_path: Path) -> None:
    """Roots get path-resolved at construction so later equality checks work."""
    handler = PythonHandler(hub=Hub(), roots={"r": tmp_path})
    assert handler.roots["r"] == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Address parser
# ---------------------------------------------------------------------------


def test_parse_id_alias_only() -> None:
    p = _parse_id("repo")
    assert p.alias == "repo"
    assert p.file is None
    assert p.qualname is None


def test_parse_id_alias_slash_file() -> None:
    p = _parse_id("repo/pkg/m.py")
    assert p.alias == "repo"
    assert p.file == "pkg/m.py"
    assert p.qualname is None


def test_parse_id_qualname() -> None:
    p = _parse_id("repo::pkg.m.Class.method")
    assert p.alias == "repo"
    assert p.qualname == "pkg.m.Class.method"
    assert p.file is None


def test_parse_id_line_range() -> None:
    p = _parse_id("repo/m.py~L10-L20")
    assert p.start_line == 10
    assert p.end_line == 20


def test_parse_id_line_range_unprefixed_end() -> None:
    """Trailing form `~L10-20` (no second `L`) also parses."""
    p = _parse_id("repo/m.py~L10-20")
    assert p.start_line == 10
    assert p.end_line == 20


def test_parse_id_single_line() -> None:
    p = _parse_id("repo/m.py~L42")
    assert p.start_line == 42
    assert p.end_line == 42


def test_parse_id_block_selector() -> None:
    """Non-line-range selectors are stored as block_selector verbatim."""
    p = _parse_id("repo/m.py~Greeter.greet")
    assert p.block_selector == "Greeter.greet"
    assert p.start_line is None


def test_parse_id_rejects_selector_on_qualname() -> None:
    with pytest.raises(BadInput, match="not supported on symbol id"):
        _parse_id("repo::pkg.Class~method")


def test_parse_id_rejects_inverted_range() -> None:
    with pytest.raises(BadInput, match="end < start"):
        _parse_id("repo/m.py~L20-L10")


def test_parse_id_rejects_empty() -> None:
    with pytest.raises(BadInput, match="empty id"):
        _parse_id("")


# ---------------------------------------------------------------------------
# get — index / overview
# ---------------------------------------------------------------------------


def test_get_no_id_lists_repos(handler: PythonHandler) -> None:
    out = handler.get()
    assert "1 repo" in out.body
    assert "r " in out.body or "r\t" in out.body or "r " in out.body


def test_get_slash_lists_repos(handler: PythonHandler) -> None:
    """The `/` sentinel is also accepted."""
    out = handler.get(id="/")
    assert "1 repo" in out.body


def test_get_with_no_roots_configured(tmp_path: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={})
    out = handler.get()
    assert "no repos configured" in out.body


def test_get_alias_renders_overview(handler: PythonHandler) -> None:
    out = handler.get(id="r")
    assert "Python repo overview" in out.body
    assert "Modules:" in out.body
    assert "Symbols:" in out.body


def test_get_unknown_alias_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="unknown python repo"):
        handler.get(id="nonexistent")


# ---------------------------------------------------------------------------
# get — toc view
# ---------------------------------------------------------------------------


def test_get_toc_view_lists_modules(handler: PythonHandler) -> None:
    out = handler.get(id="r", view="toc")
    assert "TOC" in out.body
    assert "__init__.py" in out.body
    assert "m.py" in out.body


def test_get_toc_on_file_rejected(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="applies to a repo"):
        handler.get(id="r/pkg/m.py", view="toc")


# ---------------------------------------------------------------------------
# get — file outline
# ---------------------------------------------------------------------------


def test_get_file_outline_default(handler: PythonHandler) -> None:
    out = handler.get(id="r/pkg/m.py")
    assert "outline" in out.body
    assert "IMPORTS" in out.body
    assert "FUNCTIONS" in out.body
    assert "CLASSES" in out.body
    assert "Greeter" in out.body
    assert "def helper" in out.body


def test_get_file_outline_explicit_view(handler: PythonHandler) -> None:
    out = handler.get(id="r/pkg/m.py", view="outline")
    assert "outline" in out.body


def test_get_unknown_file_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="not found in repo"):
        handler.get(id="r/no/such/file.py")


def test_imports_render_skips_future_and_redundant_aliases(
    handler: PythonHandler,
) -> None:
    """`from __future__ import annotations` shouldn't appear; bare `from
    os.path import join` should appear without `as join`."""
    out = handler.get(id="r/pkg/m.py")
    assert "__future__" not in out.body
    assert "os.path.join" in out.body
    assert "as join" not in out.body


# ---------------------------------------------------------------------------
# get — file source
# ---------------------------------------------------------------------------


def test_get_file_source_full(handler: PythonHandler) -> None:
    out = handler.get(id="r/pkg/m.py", view="source")
    # The 'def helper' line is in there, with the full source.
    assert "def helper" in out.body
    # Header has file:line range.
    assert "r/pkg/m.py:1-" in out.body


def test_get_file_source_line_range(handler: PythonHandler) -> None:
    """Track A line range returns just those lines."""
    out = handler.get(id="r/pkg/m.py~L1-L3")
    assert "r/pkg/m.py:1-3" in out.body
    # Only the first 3 lines of the file should be in there.
    body_after_header = out.body.split("\n", 3)[-1]
    assert body_after_header.count("\n") <= 4  # header + 3 lines + trailing nl


def test_get_file_source_clamps_oversize_range(handler: PythonHandler) -> None:
    """A range past EOF clamps to the actual line count rather than crashing."""
    out = handler.get(id="r/pkg/m.py~L1-L9999")
    assert "r/pkg/m.py:1-" in out.body
    # End line in the header is finite, much less than 9999.
    header = out.body.splitlines()[0]
    end = int(header.rsplit("-", 1)[1])
    assert end < 1000


# ---------------------------------------------------------------------------
# get — symbol drill-down
# ---------------------------------------------------------------------------


def test_get_symbol_default_outline(handler: PythonHandler) -> None:
    out = handler.get(id="r::pkg.m.Greeter")
    assert "pkg.m.Greeter" in out.body
    assert "(class," in out.body
    assert "Methods:" in out.body
    assert "greet" in out.body
    assert "shout" in out.body


def test_get_symbol_shows_calls_from(handler: PythonHandler) -> None:
    """The class drill-down aggregates calls from each of its methods."""
    out = handler.get(id="r::pkg.m.Greeter")
    # `greet` calls `helper` (an in-module function).
    assert "pkg.m.helper" in out.body
    # `shout` calls `self.greet` (resolves to pkg.m.Greeter.greet).
    assert "pkg.m.Greeter.greet" in out.body


def test_get_symbol_source(handler: PythonHandler) -> None:
    out = handler.get(id="r::pkg.m.helper", view="source")
    assert "def helper" in out.body
    # Header carries the symbol's actual line range (start 6 in the dedented
    # fixture: docstring + blank + import + blank + def helper).
    assert "pkg/m.py:" in out.body


def test_get_unknown_symbol_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="symbol .* not found"):
        handler.get(id="r::pkg.m.NoSuchSymbol")


def test_get_block_selector_resolves_local_symbol(handler: PythonHandler) -> None:
    """`~Greeter.greet` on a file resolves to the symbol's drill-down view."""
    out = handler.get(id="r/pkg/m.py~Greeter.greet")
    assert "pkg.m.Greeter.greet" in out.body
    assert "method" in out.body


def test_get_block_selector_unknown_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="no symbol"):
        handler.get(id="r/pkg/m.py~Nonexistent")


# ---------------------------------------------------------------------------
# get — view validation
# ---------------------------------------------------------------------------


def test_unknown_view_raises_with_options(handler: PythonHandler) -> None:
    with pytest.raises(Unsupported) as ei:
        handler.get(id="r/pkg/m.py", view="bogus")
    assert ei.value.options is not None
    assert "outline" in ei.value.options


def test_view_source_on_alias_alone_rejected(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires a file or symbol"):
        handler.get(id="r", view="source")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_finds_symbol_by_name(handler: PythonHandler) -> None:
    out = handler.search(q="greet")
    assert "greet" in out.body.lower()
    assert "r::pkg.m.Greeter" in out.body or "r::pkg.m.Greeter.greet" in out.body


def test_search_no_match(handler: PythonHandler) -> None:
    out = handler.search(q="zzzfrobnicate-nothing")
    assert "no python symbols match" in out.body


def test_search_no_match_includes_recovery_next_section(
    handler: PythonHandler,
) -> None:
    """Regression for MCP critic round 2: the empty-search response
    used to be a bare 'no matches' line. It now carries a ``Next:``
    section pointing at ``view='entries'`` (for browsing main
    functions) and the repo listing view.
    """
    out = handler.search(q="zzzfrobnicate-nothing")
    # Headline first.
    assert "no python symbols match" in out.body
    # Structured recovery trailer must be present.
    assert "Next:" in out.body
    assert "view='entries'" in out.body
    # Without a scope= the "widen to all repos" hint must NOT show —
    # we're already searching all repos.
    assert "widen to all repos" not in out.body


def test_search_no_match_with_scope_suggests_widening(
    handler: PythonHandler,
) -> None:
    """When ``scope=`` narrowed the search, the first recovery hint
    must be 'widen to all repos' — the most common cause of a
    scoped-search miss is a typo in the scope itself."""
    # Scope to a file that doesn't contain the query.
    out = handler.search(q="zzznothing", scope="r/pkg/m.py")
    assert "no python symbols match" in out.body
    assert "Next:" in out.body
    assert "widen to all repos" in out.body
    # The widened-call itself must drop the ``scope=`` argument.
    # The *description* mentions ``drop scope=`` in prose, so we
    # isolate just the call column. After D2 (TOON-ified Next:
    # blocks), the row is ``<description>\t<call>`` so the call is
    # the right side of the tab split.
    widen_line = next(ln for ln in out.body.splitlines() if "widen to all repos" in ln)
    _, _, call_part = widen_line.partition("\t")
    assert "scope=" not in call_part


def test_search_requires_q(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="search requires"):
        handler.search()


def test_search_scope_to_repo(handler: PythonHandler) -> None:
    """Scope=alias still searches the whole repo (only one alias here)."""
    out = handler.search(q="helper", scope="r")
    assert "pkg.m.helper" in out.body


def test_search_scope_to_file(handler: PythonHandler) -> None:
    """Scope='alias/path' restricts to a single file."""
    out = handler.search(q="helper", scope="r/pkg/m.py")
    assert "pkg.m.helper" in out.body


def test_search_scope_unknown_alias_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="no python repo matches scope"):
        handler.search(q="x", scope="missing-alias")


def test_search_top_k_limits_results(repo: Path) -> None:
    """top_k caps the result count even when many symbols match."""
    # Add a noisy file so we have plenty of matches for 'def'.
    _write(
        repo,
        "pkg/many.py",
        "def a(): pass\ndef b(): pass\ndef c(): pass\ndef d(): pass\n",
    )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    out = handler.search(q="pkg", top_k=2)
    # Header reports the actual hit count (≤ 2).
    header = out.body.splitlines()[0]
    assert header.startswith("# ")
    n = int(header.split()[1])
    assert n <= 2


# ---------------------------------------------------------------------------
# Multi-repo
# ---------------------------------------------------------------------------


def test_two_roots_independent(tmp_path: Path) -> None:
    """Two aliases see only their own files."""
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _write(r1, "pkg/__init__.py", "")
    _write(r1, "pkg/a.py", "def in_r1(): pass\n")
    _write(r2, "pkg/__init__.py", "")
    _write(r2, "pkg/b.py", "def in_r2(): pass\n")

    handler = PythonHandler(hub=Hub(), roots={"r1": r1, "r2": r2})
    out1 = handler.get(id="r1", view="toc")
    out2 = handler.get(id="r2", view="toc")
    assert "a.py" in out1.body and "b.py" not in out1.body
    assert "b.py" in out2.body and "a.py" not in out2.body


def test_search_across_repos(tmp_path: Path) -> None:
    """No scope → search every configured repo."""
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    _write(r1, "pkg/__init__.py", "")
    _write(r1, "pkg/a.py", "def special_thing_one(): pass\n")
    _write(r2, "pkg/__init__.py", "")
    _write(r2, "pkg/b.py", "def special_thing_two(): pass\n")

    handler = PythonHandler(hub=Hub(), roots={"r1": r1, "r2": r2})
    out = handler.search(q="special_thing")
    assert "r1::pkg.a.special_thing_one" in out.body
    assert "r2::pkg.b.special_thing_two" in out.body
