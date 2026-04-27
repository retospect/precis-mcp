"""In-memory, mtime-invalidated `RepoIndex` cache.

AST parsing is cheap (~1 ms/file), idempotent, and derived from source
that already lives on disk. So the python kind deliberately does NOT
persist its outline to Postgres — instead it keeps a per-process cache
keyed by file mtime and rebuilds stale entries on demand.

Call pattern::

    cache = RepoCache()
    idx = cache.get(Path('/abs/path/to/repo'))   # full index, first hit
    # ... files edited on disk ...
    idx = cache.get(Path('/abs/path/to/repo'))   # only changed files reparsed

One `RepoCache` instance manages any number of roots; roots are
independent. Not thread-safe — precis runs request-serial today.

Staleness detection is per-file:

- New files (in tree, not in cache)             → parse, add.
- Deleted files (in cache, not in tree)         → drop from cache.
- Modified files (mtime_ns differs from cache)  → reparse, replace.
- Unchanged files                               → reused verbatim.

`mtime_ns` rather than `mtime` avoids sub-second false-negatives on
filesystems with good resolution; on filesystems that only expose
whole-second mtime (some network mounts) it still works but may miss
edits within the same second. Good enough for dev-loop use; an agent
reading stale code for one request is an acceptable failure mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from precis.python_index.indexer import (
    _qualname_for_file,
    _walk_python_files,
    index_module,
)
from precis.python_index.types import ModuleIndex, RepoIndex

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _CachedFile:
    """One entry in the per-root cache: parsed module + the mtime we saw."""

    module: ModuleIndex
    mtime_ns: int


class RepoCache:
    """Mtime-invalidated cache of `RepoIndex` per root directory.

    `get(root)` is the only public method. First call parses every
    `.py` file under `root`; subsequent calls re-stat the tree and
    reparse only the files whose `mtime_ns` changed (or appeared).
    """

    def __init__(self) -> None:
        # root_abs_path -> { file_relative_path -> _CachedFile }
        self._cache: dict[Path, dict[str, _CachedFile]] = {}

    def get(self, root: Path) -> RepoIndex:
        """Return a `RepoIndex` for `root`, refreshing stale files."""
        root = root.resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")

        files_cache = self._cache.setdefault(root, {})

        # Snapshot the current tree.
        current: dict[str, Path] = {}
        for path in _walk_python_files(root):
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover — _walk yields under root
                continue
            current[rel] = path

        # Evict deleted files.
        for rel in list(files_cache):
            if rel not in current:
                del files_cache[rel]

        # Add / update.
        reparsed = 0
        for rel, path in current.items():
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError as e:
                # Race: file vanished between walk and stat. Drop.
                log.debug("stat failed for %s: %s", path, e)
                files_cache.pop(rel, None)
                continue

            cached = files_cache.get(rel)
            if cached is not None and cached.mtime_ns == mtime_ns:
                continue

            try:
                qualname = _qualname_for_file(path)
            except ValueError as e:
                log.warning("skipping %s: %s", path, e)
                continue

            module = index_module(path, qualname=qualname, file_relative=rel)
            files_cache[rel] = _CachedFile(module=module, mtime_ns=mtime_ns)
            reparsed += 1

        if reparsed:
            log.info(
                "reparsed %d of %d files under %s",
                reparsed,
                len(current),
                root,
            )

        return RepoIndex.build(
            root=root,
            modules=[cf.module for cf in files_cache.values()],
        )

    def drop(self, root: Path) -> None:
        """Forget everything we know about `root`. The next `get()` will
        do a full reparse. Useful for tests and for agent-initiated
        reindex commands."""
        self._cache.pop(root.resolve(), None)

    def known_roots(self) -> list[Path]:
        """All roots currently held in the cache, in insertion order."""
        return list(self._cache)
