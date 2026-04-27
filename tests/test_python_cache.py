"""Tests for `precis.python_index.cache.RepoCache`.

Exercises the mtime-invalidation contract: first call indexes
everything, second call hits cache, edits trigger reparse of just the
changed file, deletes drop entries, multiple roots stay independent.

We bump mtimes explicitly via `os.utime` instead of waiting on the
filesystem clock — keeps tests fast and deterministic.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from precis.python_index import RepoCache


def _write(repo: Path, relpath: str, content: str) -> Path:
    file = repo / relpath
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


def _bump_mtime(path: Path, *, delta_ns: int = 2_000_000_000) -> None:
    """Move a file's mtime forward by `delta_ns` nanoseconds (default 2 s).

    Using `os.utime(..., ns=)` avoids depending on `time.sleep` and the
    coarseness of some filesystems' second-resolution mtimes.
    """
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


# ---------------------------------------------------------------------------


def test_initial_index_parses_everything(tmp_path: Path) -> None:
    """First `get(root)` indexes every .py file in the tree."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")
    _write(tmp_path, "pkg/b.py", "def b(): pass\n")

    cache = RepoCache()
    idx = cache.get(tmp_path)

    assert idx.n_modules == 3  # pkg, pkg.a, pkg.b
    assert {m.qualname for m in idx.modules.values()} == {"pkg", "pkg.a", "pkg.b"}


def test_cache_hit_reuses_parsed_modules(tmp_path: Path) -> None:
    """Second call with no edits returns the same `ModuleIndex` instances."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)
    second = cache.get(tmp_path)

    # Identity check — the cached _CachedFile.module is the same object.
    assert first.module("pkg.a") is second.module("pkg.a")
    assert first.module("pkg") is second.module("pkg")


def test_modified_file_triggers_reparse(tmp_path: Path) -> None:
    """Editing a file (with bumped mtime) reparses just that one file."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")
    b = _write(tmp_path, "pkg/b.py", "def b(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)

    # Edit b.py and bump its mtime.
    b.write_text("def b(): pass\ndef new(): pass\n", encoding="utf-8")
    _bump_mtime(b)

    second = cache.get(tmp_path)

    # b.py has been reparsed (different ModuleIndex instance, new symbol present).
    assert first.module("pkg.b") is not second.module("pkg.b")
    assert second.symbol("pkg.b.new") is not None
    # a.py is unchanged — same instance.
    assert first.module("pkg.a") is second.module("pkg.a")


def test_new_file_picked_up_on_next_get(tmp_path: Path) -> None:
    """A file added after the first index appears on the next `get()`."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)
    assert first.module("pkg.b") is None

    _write(tmp_path, "pkg/b.py", "def b(): pass\n")
    second = cache.get(tmp_path)
    assert second.module("pkg.b") is not None
    # And `pkg.a` came from cache untouched.
    assert first.module("pkg.a") is second.module("pkg.a")


def test_deleted_file_dropped_from_index(tmp_path: Path) -> None:
    """A file removed from disk disappears from the cache and from the
    returned RepoIndex on the next `get()`."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")
    b = _write(tmp_path, "pkg/b.py", "def b(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)
    assert first.module("pkg.b") is not None

    b.unlink()
    second = cache.get(tmp_path)
    assert second.module("pkg.b") is None
    assert second.module("pkg.a") is not None


def test_unchanged_file_with_same_mtime_is_not_reparsed(tmp_path: Path) -> None:
    """If mtime is unchanged we trust the cache even if content changed.

    This is the documented contract: agents that need a forced
    re-index call `cache.drop(root)` first. Verifying the cache
    actually skips reparse keeps us honest about the trade-off.
    """
    _write(tmp_path, "pkg/__init__.py", "")
    a = _write(tmp_path, "pkg/a.py", "def a(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)

    # Sneakily rewrite content but pin the original mtime so the cache
    # thinks nothing happened.
    original_mtime_ns = a.stat().st_mtime_ns
    a.write_text("def renamed(): pass\n", encoding="utf-8")
    os.utime(a, ns=(a.stat().st_atime_ns, original_mtime_ns))

    second = cache.get(tmp_path)
    assert first.module("pkg.a") is second.module("pkg.a")
    assert second.symbol("pkg.a.a") is not None  # stale, by design
    assert second.symbol("pkg.a.renamed") is None


def test_multiple_roots_are_independent(tmp_path: Path) -> None:
    """Two different roots tracked by one cache don't cross-contaminate."""
    root1 = tmp_path / "r1"
    root2 = tmp_path / "r2"
    _write(root1, "pkg/__init__.py", "")
    _write(root1, "pkg/a.py", "def a(): pass\n")
    _write(root2, "pkg/__init__.py", "")
    _write(root2, "pkg/b.py", "def b(): pass\n")

    cache = RepoCache()
    idx1 = cache.get(root1)
    idx2 = cache.get(root2)

    assert {m.qualname for m in idx1.modules.values()} == {"pkg", "pkg.a"}
    assert {m.qualname for m in idx2.modules.values()} == {"pkg", "pkg.b"}
    assert set(cache.known_roots()) == {root1.resolve(), root2.resolve()}


def test_drop_forces_full_reparse(tmp_path: Path) -> None:
    """`cache.drop(root)` evicts everything for that root."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "def a(): pass\n")

    cache = RepoCache()
    first = cache.get(tmp_path)
    cache.drop(tmp_path)
    second = cache.get(tmp_path)

    # New ModuleIndex instances after drop, even though content unchanged.
    assert first.module("pkg.a") is not second.module("pkg.a")


def test_get_on_nonexistent_root_raises(tmp_path: Path) -> None:
    """A bad root raises `NotADirectoryError`, not silently empty."""
    import pytest

    cache = RepoCache()
    with pytest.raises(NotADirectoryError):
        cache.get(tmp_path / "does-not-exist")
