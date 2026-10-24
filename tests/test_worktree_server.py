"""Identity of a per-worktree session MCP server (``precis.worktree_server``).

Everything hangs off ``PRECIS_MCP_WORKTREE``: unset means the shared server
and every write/banner/ledger path is byte-identical to before; set means
the banner names the tree and sha, created refs carry ``meta.worktree`` and
ledger session ids start ``wt-<tree>-``. The sha reaches the container
through ``PRECIS_PYTHON_GITDIR_MAP`` (a worktree's ``.git`` is a host-path
pointer), so that translation is pinned here too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from precis import worktree_server
from precis.install_watchdog import checkout_fingerprint, resolve_git_dir


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def test_unset_means_not_a_worktree_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(worktree_server.ENV, raising=False)
    assert worktree_server.worktree_name() is None
    assert worktree_server.banner("abc") == ""
    meta = {"k": 1}
    assert worktree_server.stamp_meta(meta) is meta
    assert worktree_server.stamp_meta(None) is None
    assert worktree_server.session_prefix() == ""


def test_set_names_the_tree_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(worktree_server.ENV, "dl-d1")
    assert worktree_server.worktree_name() == "dl-d1"
    line = worktree_server.banner("0123456789abcdef0123")
    assert line.startswith("worktree server dl-d1 @ 0123456789ab:")
    assert "runs no workers" in line and line.endswith("\n")
    assert worktree_server.banner(None).startswith("worktree server dl-d1 @ unknown:")
    assert worktree_server.stamp_meta({"k": 1}) == {"k": 1, "worktree": "dl-d1"}
    assert worktree_server.stamp_meta(None) == {"worktree": "dl-d1"}
    assert worktree_server.stamp_meta({"worktree": "other"}) == {"worktree": "other"}
    assert worktree_server.session_prefix() == "wt-dl-d1-"


@pytest.mark.parametrize("bad", ["", "  ", "../x", "a b", "-lead", "x" * 70])
def test_malformed_name_reads_as_unset(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(worktree_server.ENV, bad)
    assert worktree_server.worktree_name() is None
    assert worktree_server.banner("abc") == ""


def test_instructions_open_with_the_worktree_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis import server

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "f").write_text("1\n", encoding="utf-8")
    _git(main, "add", "f")
    _git(main, "commit", "-q", "-m", "init")
    sha = _git(main, "rev-parse", "HEAD")
    monkeypatch.delenv(worktree_server.ENV, raising=False)
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", str(main))
    assert server._worktree_banner() == ""
    monkeypatch.setenv(worktree_server.ENV, "t1")
    assert server._worktree_banner().startswith(f"worktree server t1 @ {sha[:12]}:")


def test_worktree_pointer_is_translated_through_the_gitdir_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inside the container the worktree's ``.git`` names a host path; the
    map turns it into the ``/main`` mount so the sha (and the checkout
    watchdog) work for a worktree ``/src``."""
    host_main = tmp_path / "host" / "precis-mcp"
    host_main.mkdir(parents=True)
    _git(host_main, "init", "-q", "-b", "main")
    _git(host_main, "config", "user.email", "t@example.com")
    _git(host_main, "config", "user.name", "t")
    (host_main / "f").write_text("1\n", encoding="utf-8")
    _git(host_main, "add", "f")
    _git(host_main, "commit", "-q", "-m", "init")
    wt = tmp_path / "host" / "precis-mcp" / ".claude" / "worktrees" / "t1"
    _git(host_main, "worktree", "add", "-q", "-b", "wt-t1", str(wt))
    (wt / "g").write_text("2\n", encoding="utf-8")
    _git(wt, "add", "g")
    _git(wt, "commit", "-q", "-m", "wt commit")
    wt_sha = _git(wt, "rev-parse", "HEAD")
    assert wt_sha != _git(host_main, "rev-parse", "HEAD")

    # Simulate the container: the main checkout "moves" to /main (a copy at
    # another path), the worktree's .git pointer still names the host path.
    ctr_main = tmp_path / "ctr" / "main"
    ctr_main.parent.mkdir()
    subprocess.run(["cp", "-R", str(host_main), str(ctr_main)], check=True)
    src = tmp_path / "ctr" / "src"
    src.mkdir()
    (src / ".git").write_text(
        f"gitdir: {host_main}/.git/worktrees/t1\n", encoding="utf-8"
    )
    # Pointer unresolvable → None, as before.
    pointer_dir = host_main / ".git" / "worktrees" / "t1"
    moved = tmp_path / "moved-away"
    pointer_dir.rename(moved)
    monkeypatch.delenv("PRECIS_PYTHON_GITDIR_MAP", raising=False)
    assert resolve_git_dir(src) is None
    assert checkout_fingerprint(src) is None
    # With the map, the pointer lands in the container copy.
    monkeypatch.setenv("PRECIS_PYTHON_GITDIR_MAP", f"{host_main}:{ctr_main}")
    assert resolve_git_dir(src) == ctr_main / ".git" / "worktrees" / "t1"
    assert checkout_fingerprint(src) == wt_sha
    moved.rename(pointer_dir)
