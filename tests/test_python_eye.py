"""Tests for the python kind's fisheye ladder — ``get(kind='python',
id='<alias>::<qualname>', extent=<rung>)`` (gr498663).

DB-free — the python kind is in-memory. Each test stands up a tiny repo
in ``tmp_path`` and calls ``handler.eye`` directly (the dispatcher's
extent= door lands there; ``tests/test_eye_everywhere.py`` covers the
door itself).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._python_worktrees import WorktreeRegistry
from precis.handlers.python import PythonHandler


def _write(repo: Path, rel: str, content: str) -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like the gripe's acceptance: a target with a
    production caller, a callee, package siblings and direct tests."""
    _write(tmp_path, "pkg/__init__.py", '"""pkg — the sample package."""\n')
    _write(
        tmp_path,
        "pkg/runner.py",
        '''
        """The runner module."""

        def helper(x: int) -> int:
            """Small helper."""
            return x + 1


        def target(x: int) -> int:
            """Run the thing under a ceiling.

            Longer body of the docstring — context the eye shows in
            full while the rings stay at one line each.
            """
            return helper(x) * 2


        def other(x: int) -> int:
            """Unrelated."""
            return x - 1
        ''',
    )
    _write(
        tmp_path,
        "pkg/dispatch.py",
        '''
        """The job-type module."""

        from pkg.runner import target

        NAME = "sample_job"


        def _dispatch(x: int) -> int:
            """Dispatch the claimed job."""
            return target(x)
        ''',
    )
    _write(
        tmp_path,
        "pkg/mates.py",
        '''
        """The second-hop file."""

        from pkg.dispatch import _dispatch


        def scalar() -> int:
            """The file-mate the 2-hop surfaces."""

            return 0


        def via_dispatch() -> int:
            return _dispatch(1)
        ''',
    )
    _write(
        tmp_path,
        "tests/test_target.py",
        """
        from pkg.runner import target


        def test_target_direct():
            assert target(1) == 4
        """,
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_kwd_is_a_bookmark(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="kwd").body
    assert "pkg.runner.target" in body and "function" in body
    assert "Run the thing" in body
    assert "ring" not in body


def test_summary_is_sig_plus_docline(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="summary").body
    assert "def target(x: int) -> int" in body
    assert "Run the thing under a ceiling." in body
    assert "Longer body" not in body  # one line, not the whole card


def test_verbatim_is_the_focus_alone_in_full(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="verbatim").body
    assert "def target(x: int) -> int" in body
    assert "Longer body of the docstring" in body  # in full
    assert "— in pkg.runner" not in body  # no surroundings yet


def test_fisheye_shows_the_module_span(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="fisheye").body
    assert "— in pkg.runner" in body
    assert "▸ L" in body  # the focus marked in its member list
    assert "helper" in body and "other" in body
    assert "ring 1" not in body  # edges land at +1hop


def test_1hop_adds_callers_callees_package_and_tests(
    handler: PythonHandler,
) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="fisheye+1hop").body
    assert "ring 1" in body
    # caller, signature + first doc line
    assert "pkg.dispatch._dispatch" in body
    assert "Dispatch the claimed job." in body
    # callee likewise
    assert "pkg.runner.helper" in body and "Small helper." in body
    # package __init__ gloss
    assert "pkg — the sample package." in body
    # the referencing test, evidence-labelled
    assert "tests/test_target.py::test_target_direct" in body
    assert "[strong]" in body
    # module span is still there (rungs strictly contain)
    assert "— in pkg.runner" in body


def test_2hop_adds_second_hop_and_file_mates_as_names(
    handler: PythonHandler,
) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="fisheye+2hop").body
    assert "ring 2" in body and "names only" in body
    # one edge further: pkg.mates.via_dispatch calls the caller _dispatch
    assert "pkg.mates.via_dispatch" in body
    # and the rest of that file's top-level symbols ride along as names
    assert "pkg.mates.scalar" in body
    # names only: no signature text leaking through
    assert "return _dispatch(1)" not in body
    # ring 1 still rendered (containment)
    assert "ring 1" in body and "— in pkg.runner" in body


def test_2hop_excludes_focus_and_ring1(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner.target", extent="fisheye+2hop").body
    ring2 = body.split("— ring 2", 1)[1].split("\nNext:", 1)[0]
    assert "pkg.runner.target" not in ring2
    assert "pkg.dispatch._dispatch" not in ring2  # already ring 1


# ---------------------------------------------------------------------------
# Module focus
# ---------------------------------------------------------------------------


def test_module_focus_uses_import_edges(handler: PythonHandler) -> None:
    body = handler.eye(id="r::pkg.runner", extent="fisheye+1hop").body
    assert "pkg.runner" in body and "(module" in body
    assert "Importers:" in body and "pkg.dispatch" in body
    assert "target" in body  # module card lists its members


def test_file_id_focuses_the_module(handler: PythonHandler) -> None:
    body = handler.eye(id="r/pkg/runner.py", extent="fisheye").body
    assert "pkg.runner" in body and "(module" in body


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_ring_budget_shows_overflow_not_silent_cut(repo: Path) -> None:
    """A crowded ring stays under its stated cap and says so."""
    for i in range(130):
        _write(
            repo,
            f"pkg/c{i}.py",
            f"from pkg.runner import target\n\n\ndef caller{i}():\n    return target({i})\n",
        )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    body = handler.eye(id="r::pkg.runner.target", extent="fisheye+1hop").body
    assert "… +" in body and "more" in body
    ring1 = body.split("— ring 1", 1)[1]
    assert len(ring1) <= 3500 + 200  # cap + header/overflow slack


# ---------------------------------------------------------------------------
# Worktree aliases
# ---------------------------------------------------------------------------


def test_wt_alias_eyes_its_own_tree(tmp_path: Path) -> None:
    """``wt-<tree>::`` ids resolve like any alias (the read-only ones)."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / "pkg").mkdir()
    (main / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (main / "pkg" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    wt = tmp_path / "wts" / "mine"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (wt / "pkg").mkdir()
    (wt / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (wt / "pkg" / "m.py").write_text(
        'def f():\n    """Worktree f."""\n    return 2\n', encoding="utf-8"
    )
    reg_dir = main / ".git" / "worktrees" / "mine"
    reg_dir.mkdir(parents=True)
    (reg_dir / "gitdir").write_text(f"{wt}/.git\n", encoding="utf-8")
    reg = WorktreeRegistry("wt", main, gitdir_map=("/hostprefix", str(tmp_path)))
    handler = PythonHandler(hub=Hub(), roots={"main": main}, worktrees=reg)
    body = handler.eye(id="wt-mine::pkg.m.f", extent="fisheye+1hop").body
    assert "wt-mine::pkg.m.f" in body and "Worktree f." in body


# ---------------------------------------------------------------------------
# Refusals — every one carries next=
# ---------------------------------------------------------------------------


def test_unknown_rung_is_badinput(handler: PythonHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.eye(id="r::pkg.runner.target", extent="fisheye+9hop")
    assert exc.value.next is not None


def test_recall_is_unsupported(handler: PythonHandler) -> None:
    with pytest.raises(Unsupported) as exc:
        handler.eye(id="r::pkg.runner.target", extent="fisheye+1hop+recall")
    assert exc.value.next is not None


def test_q_and_ring_refused(handler: PythonHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.eye(id="r::pkg.runner.target", extent="fisheye+1hop", q="x:y")
    assert exc.value.next is not None
    with pytest.raises(BadInput) as exc:
        handler.eye(
            id="r::pkg.runner.target",
            extent="fisheye+1hop",
            ring={"kinds": "memory"},
        )
    assert exc.value.next is not None


def test_bad_ids(handler: PythonHandler) -> None:
    with pytest.raises(BadInput):
        handler.eye(id="r", extent="fisheye")  # the repo is not a node
    with pytest.raises(BadInput):
        handler.eye(id="r/pkg/runner.py~L1-5", extent="fisheye")  # a window
    with pytest.raises(NotFound) as exc:
        handler.eye(id="r::pkg.runner.nope", extent="fisheye")
    assert exc.value.next is not None
    with pytest.raises(NotFound):
        handler.eye(id="nosuch::pkg.runner.target", extent="fisheye")
