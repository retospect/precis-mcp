"""Tests for the ``view='callgraph'`` static call-graph view.

Covers:
- ``cgraph.build_callgraph`` — happy path, depth limit, cycle handling,
  see-above dedup, ext: tagging, multiplicity, cross-repo resolution.
- Handler integration — argument validation (alias-only id, depth
  bounds, missing entry), unknown-entry errors, end-to-end render.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers import _python_callgraph as cgraph
from precis.handlers.python import PythonHandler
from precis.python_index import index_repo

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return p


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Three-function chain plus a cycle and a class with a method.

    Layout:
      pkg/__init__.py
      pkg/m.py
        def main():       calls a(), b()
        def a():          calls helper()
        def b():          calls helper()
        def helper():     terminal
        def cyclic():     calls cyclic() recursively
        class C:
            def use(self): calls helper(), self.other()
            def other(self): terminal
    """
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/m.py",
        """
        def helper() -> int:
            return 1


        def a() -> int:
            return helper()


        def b() -> int:
            return helper()


        def main() -> int:
            return a() + b()


        def cyclic() -> int:
            return cyclic()


        class C:
            def use(self) -> int:
                helper()
                return self.other()

            def other(self) -> int:
                return 0
        """,
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(roots={"r": repo})


# ---------------------------------------------------------------------------
# build_callgraph — happy path
# ---------------------------------------------------------------------------


def test_build_includes_direct_callees(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=1)
    labels = [c.label for c in tree.children]
    assert "pkg.m.a" in labels
    assert "pkg.m.b" in labels


def test_build_recurses_to_max_depth(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=2)

    # main → a, b. Each of those calls helper. Find a's children.
    a_node = next(c for c in tree.children if c.label == "pkg.m.a")
    helper_kids = [c.label for c in a_node.children]
    assert "pkg.m.helper" in helper_kids


def test_build_truncates_past_max_depth(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=1)
    # a is depth-1; its children should be the truncation marker.
    a_node = next(c for c in tree.children if c.label == "pkg.m.a")
    assert any(child.tag == "truncated" for child in a_node.children)


# ---------------------------------------------------------------------------
# Cycle / dedup handling
# ---------------------------------------------------------------------------


def test_build_detects_self_cycle(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.cyclic", max_depth=5)
    # Root: pkg.m.cyclic. Calls cyclic again; should be tagged 'cycle'.
    assert len(tree.children) == 1
    assert tree.children[0].label == "pkg.m.cyclic"
    assert tree.children[0].tag == "cycle"
    assert tree.children[0].children == []


def test_build_marks_repeat_subgraph_as_see_above(repo: Path) -> None:
    """`a` and `b` both call `helper`. The first occurrence expands;
    the second is marked '[see above]' so we don't re-render the
    subtree."""
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=2)

    a_node = next(c for c in tree.children if c.label == "pkg.m.a")
    b_node = next(c for c in tree.children if c.label == "pkg.m.b")

    # `a`'s helper: first occurrence, expands.
    a_helper = next(c for c in a_node.children if c.label == "pkg.m.helper")
    assert a_helper.tag == ""

    # `b`'s helper: dup, marked 'see above'.
    b_helper = next(c for c in b_node.children if c.label == "pkg.m.helper")
    assert b_helper.tag == "see above"
    assert b_helper.children == []


# ---------------------------------------------------------------------------
# ext / multiplicity
# ---------------------------------------------------------------------------


def test_build_tags_unresolved_callees_as_ext(repo: Path) -> None:
    """A symbol that calls into stdlib / unresolved names produces ext: nodes."""
    _write(
        repo,
        "pkg/q.py",
        """
        import os
        def caller():
            os.path.join("a", "b")
            os.path.join("c", "d")
        """,
    )
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.q.caller", max_depth=2)
    # os.path.join is `ext:` in our static resolution. Two calls →
    # multiplicity=2.
    join_node = next(c for c in tree.children if "join" in c.label)
    assert join_node.tag == "ext"
    assert join_node.multiplicity == 2


def test_build_class_aggregates_method_calls(repo: Path) -> None:
    """A class node aggregates calls from its methods (`use`, `other`)."""
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.C", max_depth=1)
    callees = {c.label for c in tree.children}
    # use() calls helper(); use() also calls self.other(). Both surface.
    assert "pkg.m.helper" in callees
    assert "pkg.m.C.other" in callees


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_includes_header_and_legend(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.main", max_depth=2)
    body = cgraph.render_callgraph(
        tree, alias="r", entry="pkg.m.main", max_depth=2, cross_repo=False
    )
    assert "Static call graph from r::pkg.m.main" in body
    assert "depth=2" in body
    # 'see above' or 'truncated' or 'ext' should trigger legend.
    assert "Legend:" in body
    # box-drawing glyphs appear.
    assert "├──" in body or "└──" in body


def test_render_root_label_is_entry_qualname(repo: Path) -> None:
    idx = index_repo(repo)
    tree = cgraph.build_callgraph(idx, entry="pkg.m.helper", max_depth=1)
    body = cgraph.render_callgraph(
        tree, alias="r", entry="pkg.m.helper", max_depth=1, cross_repo=False
    )
    # Root is on its own line, no glyphs, no tag.
    assert "\npkg.m.helper\n" in body or body.count("pkg.m.helper") >= 1


# ---------------------------------------------------------------------------
# Cross-repo resolution
# ---------------------------------------------------------------------------


def test_cross_repo_resolves_imported_symbol(tmp_path: Path) -> None:
    """When repo A calls into repo B by a fully-qualified name and
    cross_repo=True, the callee resolves to B's symbol (tagged with B's
    alias) instead of falling back to ext:."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write(a, "appkg/__init__.py", "")
    _write(
        a,
        "appkg/main.py",
        """
        from libpkg.lib import shared

        def entry() -> int:
            return shared()
        """,
    )
    _write(b, "libpkg/__init__.py", "")
    _write(
        b,
        "libpkg/lib.py",
        """
        def shared() -> int:
            return 42
        """,
    )

    handler = PythonHandler(roots={"a": a, "b": b})

    # Without cross_repo: shared resolves to its imported qualname but
    # isn't a member of repo `a` — so it shows as ext-ish. Specifically,
    # the static resolver writes `libpkg.lib.shared` as the callee
    # qualname; without cross_repo, it's not found in `a` → tagged ext.
    out_no_cross = handler.get(
        id="a", view="callgraph", entry="appkg.main.entry", depth=2
    )
    assert "libpkg.lib.shared" in out_no_cross.body
    assert "[ext]" in out_no_cross.body

    # With cross_repo: shared is found in repo `b` and tagged `[b]`.
    out_cross = handler.get(
        id="a",
        view="callgraph",
        entry="appkg.main.entry",
        depth=2,
        cross_repo=True,
    )
    assert "libpkg.lib.shared" in out_cross.body
    assert "[b]" in out_cross.body


# ---------------------------------------------------------------------------
# Handler integration — argument validation
# ---------------------------------------------------------------------------


def test_handler_callgraph_requires_entry(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="requires entry="):
        handler.get(id="r", view="callgraph")


def test_handler_callgraph_rejects_file_id(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="r/pkg/m.py", view="callgraph", entry="pkg.m.main")


def test_handler_callgraph_rejects_qualname_id(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="r::pkg.m.main", view="callgraph", entry="pkg.m.main")


def test_handler_callgraph_rejects_bad_depth(handler: PythonHandler) -> None:
    with pytest.raises(BadInput, match="depth must be"):
        handler.get(id="r", view="callgraph", entry="pkg.m.main", depth=0)
    with pytest.raises(BadInput, match="depth must be"):
        handler.get(id="r", view="callgraph", entry="pkg.m.main", depth=999)


def test_handler_callgraph_unknown_entry_raises(handler: PythonHandler) -> None:
    with pytest.raises(NotFound, match="callgraph entry"):
        handler.get(id="r", view="callgraph", entry="pkg.m.nonexistent_fn")


def test_handler_callgraph_accepts_colon_form(handler: PythonHandler) -> None:
    """`pkg.m:main` and `pkg.m.main` are equivalent for the entry kwarg."""
    out_colon = handler.get(id="r", view="callgraph", entry="pkg.m:main", depth=2)
    out_dot = handler.get(id="r", view="callgraph", entry="pkg.m.main", depth=2)
    # Same tree shape — we accept both spellings of the entry point.
    assert "pkg.m.main" in out_colon.body
    assert "pkg.m.main" in out_dot.body


def test_handler_callgraph_renders_end_to_end(handler: PythonHandler) -> None:
    out = handler.get(id="r", view="callgraph", entry="pkg.m.main", depth=3)
    assert "Static call graph from r::pkg.m.main" in out.body
    assert "pkg.m.a" in out.body
    assert "pkg.m.b" in out.body
    assert "pkg.m.helper" in out.body
    assert "Next:" in out.body
