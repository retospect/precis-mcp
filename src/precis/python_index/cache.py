"""Root-scoped incremental AST cache with explicitly stat-checked freshness.

Unchanged size/mtime/ctime/device/inode reuses the indexed byte snapshot; it
does not prove current content equality. Changed files get at most two parse
attempts, with before/after identity checks. Failed/unstable files are omitted
instead of attaching a successful fresh stamp to an old module. Tree walks
are not atomic snapshots; partial walks retain earlier entries with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from precis.python_index.indexer import (
    _qualname_for_file,
    _walk_python_files,
    index_module,
)
from precis.python_index.types import IndexObservation, ModuleIndex, RepoIndex


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino


@dataclass(slots=True)
class _CachedFile:
    module: ModuleIndex
    signature: tuple[int, int, int, int, int]


class RepoCache:
    """Independent per-root caches; stat checks reuse ASTs without byte scans."""

    def __init__(self) -> None:
        self._cache: dict[Path, dict[str, _CachedFile]] = {}

    def get(self, root: Path) -> RepoIndex:
        root = root.resolve()
        if not root.is_dir():
            self.drop(root)
            raise NotADirectoryError(f"not a directory: {root}")
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        root_identity = _signature(root)[3:]
        files_cache = self._cache.setdefault(root, {})
        issues: list[str] = []
        current: dict[str, Path] = {}
        try:
            for path in _walk_python_files(root):
                current[path.relative_to(root).as_posix()] = path
        except OSError as exc:
            issues.append(
                f"partial walk: {type(exc).__name__}; retained entries unverified"
            )
        if not issues:
            for rel in files_cache.keys() - current.keys():
                del files_cache[rel]

        reparsed = reused = 0
        for rel, path in current.items():
            try:
                before = _signature(path)
                cached = files_cache.get(rel)
                if cached is not None and cached.signature == before:
                    reused += 1
                    continue
                qualname = _qualname_for_file(path)
                for _attempt in range(2):
                    before = _signature(path)
                    module = index_module(path, qualname=qualname, file_relative=rel)
                    after = _signature(path)
                    if before == after:
                        files_cache[rel] = _CachedFile(module, after)
                        reparsed += 1
                        break
                else:
                    files_cache.pop(rel, None)
                    issues.append(f"{rel}: unstable read; omitted")
            except (OSError, UnicodeError, ValueError) as exc:
                files_cache.pop(rel, None)
                issues.append(f"{rel}: {type(exc).__name__}; omitted")

        try:
            if not root.is_dir() or _signature(root)[3:] != root_identity:
                raise NotADirectoryError(f"root disappeared or changed: {root}")
        except OSError:
            self.drop(root)
            raise
        idx = RepoIndex.build(root, [cf.module for cf in files_cache.values()])
        return replace(
            idx,
            observation=IndexObservation(
                started,
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                reparsed,
                reused,
                tuple(issues),
            ),
        )

    def drop(self, root: Path) -> None:
        """Forget a root; the next read reparses its files."""
        self._cache.pop(root.resolve(), None)

    def held_modules(self, root: Path) -> list[ModuleIndex]:
        """Modules currently cached for ``root``; no stat, no reparse."""
        files = self._cache.get(root.resolve(), {})
        return [cf.module for cf in list(files.values())]

    def known_roots(self) -> list[Path]:
        return list(self._cache)
