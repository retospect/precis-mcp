"""Tests for ``precis.handlers._python_pin.RootPinner`` (gr762343).

A pinned alias's reads answer a materialized ``refs/remotes/origin/main``
tree instead of the working tree, so a builder checkout whose HEAD moves
between slice branches still reads as main.  The tests stand up a real
bare remote + clone per case — git behaviour itself is under test.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers._python_pin import (
    PIN_ROOTS_ENV,
    RootPinner,
    parse_pin_roots,
)
from precis.handlers.python import PythonHandler
from precis.python_index import RepoCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, content: str) -> None:
    file = repo / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote with one commit on main, and a clone of it."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        capture_output=True,
        check=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _write(seed, "pkg/__init__.py", "")
    _write(seed, "pkg/m.py", "def main_func():\n    return 'main'\n")
    _commit(seed, "main state")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(clone)],
        capture_output=True,
        check=True,
    )
    return remote, clone


def _pinner(tmp_path: Path, **overrides: Any) -> RootPinner:
    options: dict[str, Any] = {
        "fetch_ttl_s": 0.0,  # tests force a fetch per resolve unless said otherwise
        "fetch_timeout_s": 10.0,
        "archive_timeout_s": 30.0,
    }
    options.update(overrides)
    return RootPinner(tmp_path / "pin-cache", **options)


def _module_text(decision_index_root: Path, rel: str) -> str:
    idx = RepoCache().get(decision_index_root)
    mod = idx.file(rel)
    assert mod is not None
    return mod.source or ""


# ---------------------------------------------------------------------------
# parse_pin_roots
# ---------------------------------------------------------------------------


def test_parse_pin_roots_none_and_empty() -> None:
    assert parse_pin_roots(None) == set()
    assert parse_pin_roots("") == set()
    assert parse_pin_roots("  , ,") == set()


def test_parse_pin_roots_entries() -> None:
    assert parse_pin_roots("main, cli") == {"main", "cli"}
    assert parse_pin_roots(" main ,,cli,") == {"main", "cli"}


# ---------------------------------------------------------------------------
# RootPinner.resolve
# ---------------------------------------------------------------------------


def test_first_resolve_materializes_origin_main(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    _, clone = remote_and_clone
    pinner = _pinner(tmp_path)

    decision = pinner.resolve(clone)

    assert decision.pinned
    assert decision.sha == _git(clone, "rev-parse", "origin/main")
    assert decision.index_root != clone.resolve()
    assert decision.note is None  # caller composes the refresh note
    assert "def main_func" in _module_text(decision.index_root, "pkg/m.py")


def test_read_answers_main_after_checkout_moves_to_feature_branch(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    """gr762343: the checkout's branch must not leak into pinned reads."""
    _, clone = remote_and_clone
    _git(clone, "checkout", "-b", "slice-feature")
    _write(clone, "pkg/m.py", "def feature_func():\n    return 'feature'\n")
    _commit(clone, "feature state")

    decision = _pinner(tmp_path).resolve(clone)

    assert decision.pinned
    text = _module_text(decision.index_root, "pkg/m.py")
    assert "def main_func" in text
    assert "def feature_func" not in text


def test_resolve_advances_when_origin_main_moves(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    remote, clone = remote_and_clone
    pinner = _pinner(tmp_path, keep=1)
    first = pinner.resolve(clone)
    assert first.pinned and first.sha is not None

    # Advance main on the remote (a sibling slice landing).
    seed2 = tmp_path / "seed2"
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(seed2)],
        capture_output=True,
        check=True,
    )
    _write(seed2, "pkg/m.py", "def main_func():\n    return 'main v2'\n")
    _commit(seed2, "main v2")
    _git(seed2, "push", "origin", "main")

    second = pinner.resolve(clone)
    assert second.pinned
    assert second.sha != first.sha
    assert "main v2" in _module_text(second.index_root, "pkg/m.py")
    # keep=1 pruned the first tree.
    assert not first.index_root.exists()


def test_fetch_failure_still_answers_last_good_tree(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    """A failed fetch keeps the current pinned tree and names the failure."""
    _, clone = remote_and_clone
    pinner = _pinner(tmp_path)
    first = pinner.resolve(clone)
    assert first.pinned and first.sha is not None

    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    second = pinner.resolve(clone)
    assert second.pinned
    assert second.index_root == first.index_root
    assert second.note is not None
    assert "fetch failed or timed out" in second.note


def test_materialize_failure_after_main_moves_answers_behind(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """origin/main advanced but the new tree could not be built: the read
    answers the last good pinned tree and says it is behind."""
    remote, clone = remote_and_clone
    pinner = _pinner(tmp_path)
    first = pinner.resolve(clone)
    assert first.pinned and first.sha is not None

    seed2 = tmp_path / "seed2"
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(seed2)],
        capture_output=True,
        check=True,
    )
    _write(seed2, "pkg/m.py", "def main_func():\n    return 'main v2'\n")
    _commit(seed2, "main v2")
    _git(seed2, "push", "origin", "main")

    monkeypatch.setattr(
        pinner,
        "_extract",
        lambda root, sha, dest: "archive timed out or failed (TimeoutExpired)",
    )
    second = pinner.resolve(clone)
    assert second.pinned
    assert second.index_root == first.index_root
    assert second.note is not None
    assert "CUT SHORT" in second.note
    assert "behind" in second.note


def test_no_origin_main_answers_working_tree(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    _, clone = remote_and_clone
    _git(clone, "remote", "remove", "origin")
    decision = _pinner(tmp_path).resolve(clone)
    assert not decision.pinned
    assert decision.index_root == clone.resolve()
    assert decision.note is not None
    assert "working tree" in decision.note


def test_non_git_root_answers_working_tree(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    decision = _pinner(tmp_path).resolve(plain)
    assert not decision.pinned
    assert decision.note is not None
    assert "not a git checkout" in decision.note


def test_disabled_cache_answers_working_tree(tmp_path: Path) -> None:
    pinner = RootPinner(None)
    decision = pinner.resolve(tmp_path)
    assert not decision.pinned
    assert decision.note is not None
    assert "disabled" in decision.note


def test_fetch_is_ttl_bounded(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """Within the TTL the remote-tracking ref is used as-is — no fetch."""
    _, clone = remote_and_clone
    pinner = _pinner(tmp_path, fetch_ttl_s=3600.0)
    real_git = pinner._git
    fetches = 0

    def counting(root: Path, *args: str, timeout: float):
        nonlocal fetches
        if args and args[0] == "fetch":
            fetches += 1
        return real_git(root, *args, timeout=timeout)

    monkeypatch.setattr(pinner, "_git", counting)
    assert pinner.resolve(clone).pinned
    assert fetches == 1
    assert pinner.resolve(clone).pinned
    assert fetches == 1


def test_archive_failure_falls_back_to_working_tree(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """A broken archive step degrades loudly on the first resolve."""
    _, clone = remote_and_clone
    pinner = _pinner(tmp_path)
    monkeypatch.setattr(
        pinner,
        "_extract",
        lambda root, sha, dest: "archive timed out or failed (TimeoutExpired)",
    )
    decision = pinner.resolve(clone)
    assert not decision.pinned
    assert decision.index_root == clone.resolve()
    assert decision.note is not None
    assert "CUT SHORT" in decision.note
    assert "working tree" in decision.note


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


def _handler(clone: Path, pinner: RootPinner) -> PythonHandler:
    return PythonHandler(
        hub=Hub(),
        roots={"r": clone},
        cache=RepoCache(),  # process-local: no snapshot writes from tests
        pinner=pinner,
    )


def test_handler_read_carries_pin_fragment(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, clone = remote_and_clone
    monkeypatch.setenv(PIN_ROOTS_ENV, "r")
    handler = _handler(clone, _pinner(tmp_path))

    body = handler.get(id="r/pkg/m.py", view="source").body

    assert "pinned to origin/main@" in body
    assert "main_func" in body
    sha = _git(clone, "rev-parse", "origin/main")
    assert f"pinned to origin/main@{sha[:8]}" in body


def test_handler_read_answers_main_not_checked_out_branch(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """End-to-end gr762343: a feature branch checked out in the root does
    not leak into get/search answers."""
    _, clone = remote_and_clone
    _git(clone, "checkout", "-b", "slice-feature")
    _write(clone, "pkg/m.py", "def feature_func():\n    return 'feature'\n")
    _commit(clone, "feature state")
    monkeypatch.setenv(PIN_ROOTS_ENV, "r")
    handler = _handler(clone, _pinner(tmp_path))

    body = handler.get(id="r/pkg/m.py", view="source").body
    assert "main_func" in body
    assert "feature_func" not in body
    found = handler.search(q="feature_func").body
    assert "r::pkg.m.feature_func" not in found


def test_handler_search_carries_pin_fragment(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, clone = remote_and_clone
    monkeypatch.setenv(PIN_ROOTS_ENV, "r")
    handler = _handler(clone, _pinner(tmp_path))

    body = handler.search(q="main_func").body
    assert "pinned to origin/main@" in body
    assert "r::pkg.m.main_func" in body


def test_handler_unpinned_alias_unchanged(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    """Without the env var, reads keep following the working tree."""
    _, clone = remote_and_clone
    monkeypatch.delenv(PIN_ROOTS_ENV, raising=False)
    monkeypatch.setenv(PIN_ROOTS_ENV, "somebody-else")
    handler = _handler(clone, _pinner(tmp_path))

    body = handler.get(id="r/pkg/m.py", view="source").body
    assert "main_func" in body
    assert "pinned to origin/main" not in body


def test_provenance_drill_down_names_the_pin(
    remote_and_clone: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    _, clone = remote_and_clone
    monkeypatch.setenv(PIN_ROOTS_ENV, "r")
    handler = _handler(clone, _pinner(tmp_path))

    body = handler.get(id="r", view="provenance").body
    assert "Pinned to origin/main: pinned to origin/main@" in body
