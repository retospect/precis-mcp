"""Tests for `precis.md_index.cache.MdRepoCache`.

Exercises the mtime-invalidation contract: first call indexes
everything, second call hits cache, edits trigger reparse of just the
changed file, deletes drop entries, multiple roots stay independent.
Mirrors `tests/test_python_cache.py`.

We bump mtimes explicitly via `os.utime` instead of waiting on the
filesystem clock — keeps tests fast and deterministic.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from precis.md_index import MdRepoCache


def _write(repo: Path, relpath: str, content: str) -> Path:
    file = repo / relpath
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return file


def _bump_mtime(path: Path, *, delta_ns: int = 2_000_000_000) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


# ---------------------------------------------------------------------------


def test_initial_index_parses_everything(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")
    _write(tmp_path, "sub/b.md", "# B\n\nWorld.\n")

    cache = MdRepoCache()
    idx = cache.get(tmp_path)

    assert idx.n_files == 2
    assert set(idx.files) == {"a.md", "sub/b.md"}


def test_cache_hit_reuses_parsed_entries(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)
    second = cache.get(tmp_path)

    assert first.file("a.md") is second.file("a.md")


def test_modified_file_triggers_reparse(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.md", "# A\n\nOriginal.\n")
    _write(tmp_path, "b.md", "# B\n\nUnchanged.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)

    a.write_text("# A\n\nEdited.\n\nExtra paragraph.\n", encoding="utf-8")
    _bump_mtime(a)

    second = cache.get(tmp_path)

    assert first.file("a.md") is not second.file("a.md")
    second_a = second.file("a.md")
    assert second_a is not None
    assert second_a.n_blocks == 3
    # b.md is unchanged -> same instance.
    assert first.file("b.md") is second.file("b.md")


def test_new_file_picked_up_on_next_get(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)
    assert first.file("b.md") is None

    _write(tmp_path, "b.md", "# B\n\nWorld.\n")
    second = cache.get(tmp_path)
    assert second.file("b.md") is not None
    assert first.file("a.md") is second.file("a.md")


def test_deleted_file_dropped_from_index(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")
    b = _write(tmp_path, "b.md", "# B\n\nWorld.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)
    assert first.file("b.md") is not None

    b.unlink()
    second = cache.get(tmp_path)
    assert second.file("b.md") is None
    assert second.file("a.md") is not None


def test_unchanged_file_with_same_mtime_is_not_reparsed(tmp_path: Path) -> None:
    """Documented contract: mtime is the only staleness signal — content
    changes with a pinned mtime are trusted-stale until a caller does
    `cache.drop(root)`."""
    a = _write(tmp_path, "a.md", "# A\n\nOriginal.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)

    original_mtime_ns = a.stat().st_mtime_ns
    a.write_text("# A\n\nRewritten.\n\nExtra.\n", encoding="utf-8")
    os.utime(a, ns=(a.stat().st_atime_ns, original_mtime_ns))

    second = cache.get(tmp_path)
    assert first.file("a.md") is second.file("a.md")
    second_a = second.file("a.md")
    assert second_a is not None
    assert second_a.n_blocks == 2  # stale, by design


def test_multiple_roots_are_independent(tmp_path: Path) -> None:
    root1 = tmp_path / "r1"
    root2 = tmp_path / "r2"
    _write(root1, "a.md", "# A\n\nOne.\n")
    _write(root2, "b.md", "# B\n\nTwo.\n")

    cache = MdRepoCache()
    idx1 = cache.get(root1)
    idx2 = cache.get(root2)

    assert set(idx1.files) == {"a.md"}
    assert set(idx2.files) == {"b.md"}
    assert set(cache.known_roots()) == {root1.resolve(), root2.resolve()}


def test_drop_forces_full_reparse(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "# A\n\nHello.\n")

    cache = MdRepoCache()
    first = cache.get(tmp_path)
    cache.drop(tmp_path)
    second = cache.get(tmp_path)

    assert first.file("a.md") is not second.file("a.md")


def test_get_on_nonexistent_root_raises(tmp_path: Path) -> None:
    cache = MdRepoCache()
    with pytest.raises(NotADirectoryError):
        cache.get(tmp_path / "does-not-exist")
