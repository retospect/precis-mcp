"""Tests for the python kind's ``view='impact'`` — bounded change impact
(slice 3 of python navigation context+impact).

Uses a real git repo in ``tmp_path`` so ``base``/``head`` revisions and
uncommitted-worktree diffs exercise the actual plumbing.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.python import PythonHandler
from tests.hintcheck import assert_hints_round_trip


def _write(repo: Path, rel: str, content: str) -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return f


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Commit v1; tests mutate it to v2."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/core.py",
        '''
        """Core module docstring."""


        def target(x: int) -> int:
            return x + 1


        def helper(x: int) -> int:
            return target(x) * 2


        def doomed() -> None:
            return None
        ''',
    )
    _write(
        tmp_path,
        "pkg/mover.py",
        """
        def relocated(x: int) -> int:
            return x * 3
        """,
    )
    _write(
        tmp_path,
        "pkg/user.py",
        """
        from pkg.core import helper, target


        def run() -> int:
            return helper(1) + target(2)
        """,
    )
    _write(
        tmp_path,
        "tests/test_core.py",
        """
        from pkg.core import target


        def test_target():
            assert target(1) == 2
        """,
    )
    _write(
        tmp_path,
        "docs.md",
        "Mentions pkg.core.target in prose.\n",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "f@e.invalid")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "v1")
    return tmp_path


def _commit_v2(repo: Path) -> None:
    """Apply a known diff: sig change, body change, delete, add, move."""
    _write(
        repo,
        "pkg/core.py",
        '''
        """Core module docstring."""


        def target(x: int, scale: int = 1) -> int:
            return (x + 1) * scale


        def helper(x: int) -> int:
            return target(x) * 3
        ''',
    )
    (repo / "pkg" / "mover.py").unlink()
    _write(
        repo,
        "pkg/newhome.py",
        """
        def relocated(x: int) -> int:
            return x * 3
        """,
    )
    _write(
        repo,
        "pkg/added.py",
        """
        def brand_new() -> int:
            return 7
        """,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v2")


def test_impact_revision_pair(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    head = _git(repo, "rev-parse", "HEAD")
    body = handler.get(id="r", view="impact", base=base, head=head).body

    assert f"{base[:12]}..{head[:12]}" in body
    # Symbol-level classification.
    assert "signature" in body and "pkg.core.target" in body
    assert "scale: int=1" in body
    assert "body" in body and "pkg.core.helper" in body
    assert "removed" in body and "pkg.core.doomed" in body
    assert "added" in body and "pkg.added.brand_new" in body
    # Cross-file move: pkg.mover.relocated -> pkg.newhome.relocated.
    assert "moved" in body and "pkg.mover.relocated" in body
    assert "pkg.newhome.relocated" in body


def test_impact_reports_callers_importers_tests_docs(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    head = _git(repo, "rev-parse", "HEAD")
    body = handler.get(id="r", view="impact", base=base, head=head).body

    assert "Callers of changed symbols" in body
    assert "r::pkg.user.run" in body
    assert "Importers of changed modules" in body
    assert "r::pkg.user" in body
    # Affected tests: the direct caller surfaces, labelled.
    assert "Affected tests:" in body
    assert "test_target" in body
    assert "[strong]" in body
    # docs.md names the changed qualname.
    assert "docs.md" in body
    # head was a revision: the fan-out caveat must be explicit.
    assert "indexed working tree" in body


def test_impact_worktree_diff_fingerprints(repo: Path) -> None:
    """head omitted -> live worktree bytes, reported with fingerprints."""
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    # Uncommitted edit — no commit at all.
    _write(
        repo,
        "pkg/core.py",
        '''
        """Core module docstring."""


        def target(x: int) -> int:
            return x + 1


        def helper(x: int) -> int:
            return target(x) * 9


        def doomed() -> None:
            return None
        ''',
    )
    _write(repo, "pkg/untracked_new.py", "def fresh() -> int:\n    return 1\n")
    body = handler.get(id="r", view="impact", base=base).body
    assert "worktree" in body
    assert "body" in body and "pkg.core.helper" in body
    assert "added" in body and "pkg.untracked_new.fresh" in body
    assert "fingerprints" in body.lower() or "sha256" in body
    assert "pkg/core.py" in body


def test_impact_file_scoped(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    head = _git(repo, "rev-parse", "HEAD")
    body = handler.get(id="r/pkg/core.py", view="impact", base=base, head=head).body
    assert "pkg.core.target" in body
    assert "pkg.added.brand_new" not in body
    # An unchanged file errors rather than emitting an empty diff.
    with pytest.raises(BadInput):
        handler.get(id="r/pkg/user.py", view="impact", base=base, head=head)


def test_impact_symbol_mode(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    body = handler.get(id="r::pkg.core.target", view="impact").body
    assert "seed" in body or "pkg.core.target" in body
    assert "r::pkg.user.run" in body  # caller fan-out
    assert "test_target" in body  # test fan-out
    with pytest.raises(BadInput):
        handler.get(id="r::pkg.core.target", view="impact", base="HEAD")
    with pytest.raises(NotFound):
        handler.get(id="r::pkg.core.nope", view="impact")


def test_impact_requires_base_for_alias(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    with pytest.raises(BadInput):
        handler.get(id="r", view="impact")
    with pytest.raises(BadInput):
        handler.get(id="r", view="impact", base="refs/nonexistent-rev")


def test_impact_bounds(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(BadInput):
        handler.get(id="r", view="impact", base=base, depth=0)
    with pytest.raises(BadInput):
        handler.get(id="r", view="impact", base=base, max_results=0)
    # depth=1 keeps pkg.user.run (1 hop from target/helper) but drops
    # deeper transitive callers — assert the bound is honoured via a
    # two-hop chain: caller_of_user -> run -> target.
    _write(
        repo,
        "pkg/top.py",
        "from pkg.user import run\n\n\ndef top() -> int:\n    return run()\n",
    )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    shallow = handler.get(id="r", view="impact", base=base, head=head, depth=1).body
    assert "r::pkg.user.run" in shallow
    assert "r::pkg.top.top" not in shallow
    deep = handler.get(id="r", view="impact", base=base, head=head, depth=2).body
    assert "r::pkg.top.top" in deep


def test_impact_hints_round_trip(repo: Path) -> None:
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    body = handler.get(id="r", view="impact", base=base).body
    assert_hints_round_trip(body, lambda verb, kw: getattr(handler, verb)(**kw).body)


def test_impact_no_source_execution(repo: Path, monkeypatch) -> None:
    """Git reads are plumbing; python source must never be executed."""
    import sys

    monkeypatch.setattr(sys, "setprofile", lambda *_a, **_k: pytest.fail("executed"))
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    head = _git(repo, "rev-parse", "HEAD")
    body = handler.get(id="r", view="impact", base=base, head=head).body
    assert "pkg.core.target" in body


def test_impact_transitive_callers_name_their_chain(repo: Path) -> None:
    """gr488913: a two-hop caller reads ``reaches X via Y``, never as a
    direct ``calls X``; an indirect test hit names its chain."""
    _write(
        repo,
        "pkg/top.py",
        "from pkg.user import run\n\n\ndef top() -> int:\n    return run()\n",
    )
    _write(
        repo,
        "tests/test_top.py",
        "from pkg.top import top\n\n\ndef test_top():\n    assert top()\n",
    )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    body = handler.get(id="r::pkg.core.target", view="impact", depth=3).body
    run_row = next(ln for ln in body.splitlines() if "r::pkg.user.run " in ln)
    assert "calls pkg.core.target" in run_row  # run calls target directly
    top_row = next(ln for ln in body.splitlines() if "r::pkg.top.top " in ln)
    assert "reaches pkg.core.target via run" in top_row
    test_row = next(
        ln for ln in body.splitlines() if "tests/test_top.py::test_top " in ln
    )
    assert "indirect call at" in test_row and "(via top -> run)" in test_row


def test_impact_line_shift_only_is_one_summary_line(repo: Path) -> None:
    """gr488914: a symbol whose lines moved but whose text did not is
    'shifted' — counted apart in the header, one summary line, no seed."""
    base = _git(repo, "rev-parse", "HEAD")
    _write(
        repo,
        "pkg/core.py",
        '''
        """Core module docstring."""

        PAD = 1


        def target(x: int) -> int:
            return x + 2


        def helper(x: int) -> int:
            return target(x) * 2


        def doomed() -> None:
            return None
        ''',
    )
    handler = PythonHandler(hub=Hub(), roots={"r": repo})
    body = handler.get(id="r", view="impact", base=base).body
    assert "1 changed symbol + 2 line-shift only" in body
    assert "~ body      r::pkg.core.target" in body
    assert "~ shifted   2 symbols moved lines only" in body
    assert "helper" in body and "doomed" in body


def test_impact_fingerprints_only_uncommitted_files(repo: Path) -> None:
    """gr488915: with base older than HEAD, committed changes are diffed
    but only files whose worktree bytes differ from HEAD get a fingerprint."""
    base = _git(repo, "rev-parse", "HEAD")
    _commit_v2(repo)
    clean = PythonHandler(hub=Hub(), roots={"r": repo})
    body = clean.get(id="r", view="impact", base=base).body
    assert "pkg.core.target" in body
    assert "Worktree fingerprints" not in body
    _write(repo, "pkg/added.py", "def brand_new() -> int:\n    return 8\n")
    dirty = PythonHandler(hub=Hub(), roots={"r": repo})
    body = dirty.get(id="r", view="impact", base=base).body
    fp = body.split("Worktree fingerprints", 1)[1].split("\n\n", 1)[0]
    assert "pkg/added.py" in fp
    assert "pkg/core.py" not in fp
