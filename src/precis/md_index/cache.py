"""In-memory, mtime-invalidated `MdRepoIndex` cache.

Mirrors `precis.python_index.cache.RepoCache` exactly, substituting
markdown blocks for AST symbols: parsing a markdown file into blocks
is cheap and derived purely from what's on disk, so there is
deliberately no Postgres persistence here — just a per-process cache
keyed by file mtime, rebuilt on demand.

Call pattern::

    cache = MdRepoCache()
    idx = cache.get(Path('/abs/path/to/docs'))   # full index, first hit
    # ... files edited on disk ...
    idx = cache.get(Path('/abs/path/to/docs'))   # only changed files reparsed

One `MdRepoCache` instance manages any number of roots; roots are
independent. `get()` is internally locked (`threading.RLock`, held
across its full body): safe for the background warm thread
(`precis.server._warm_md_index_background`) to call `get()` while
request threads concurrently call it via `MdHandler.get`/`search`.
Per-file reparse is cheap, so a coarse whole-method lock is fine.

Staleness detection is per-file, identical contract to `RepoCache`:

- New files (in tree, not in cache)             -> parse, add.
- Deleted files (in cache, not in tree)         -> drop from cache.
- Modified files (mtime_ns differs from cache)  -> reparse, replace.
- Unchanged files                               -> reused verbatim.

`mtime_ns` rather than `mtime` avoids sub-second false-negatives; see
`RepoCache`'s docstring for the full rationale, which applies
unchanged here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from precis.md_index.indexer import _walk_md_files, index_file
from precis.md_index.types import MdFileEntry, MdRepoIndex

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _CachedFile:
    """One entry in the per-root cache: parsed file + the mtime we saw."""

    entry: MdFileEntry
    mtime_ns: int


class MdRepoCache:
    """Mtime-invalidated cache of `MdRepoIndex` per root directory.

    `get(root)` is the only public method. First call parses every
    `.md`/`.markdown` file under `root`; subsequent calls re-stat the
    tree and reparse only the files whose `mtime_ns` changed (or
    appeared).
    """

    def __init__(self) -> None:
        # root_abs_path -> { file_relative_path -> _CachedFile }
        self._cache: dict[Path, dict[str, _CachedFile]] = {}
        self._lock = threading.RLock()

    def get(self, root: Path) -> MdRepoIndex:
        """Return an `MdRepoIndex` for `root`, refreshing stale files."""
        with self._lock:
            root = root.resolve()
            if not root.is_dir():
                raise NotADirectoryError(f"not a directory: {root}")

            files_cache = self._cache.setdefault(root, {})

            # Snapshot the current tree.
            current: dict[str, Path] = {}
            for path in _walk_md_files(root):
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

                entry = index_file(path, file_relative=rel)
                files_cache[rel] = _CachedFile(entry=entry, mtime_ns=mtime_ns)
                reparsed += 1

            if reparsed:
                log.info(
                    "reparsed %d of %d md files under %s",
                    reparsed,
                    len(current),
                    root,
                )

            return MdRepoIndex.build(
                root=root,
                files=[cf.entry for cf in files_cache.values()],
            )

    def drop(self, root: Path) -> None:
        """Forget everything we know about `root`. The next `get()` will
        do a full reparse. Useful for tests and for agent-initiated
        reindex commands."""
        self._cache.pop(root.resolve(), None)

    def known_roots(self) -> list[Path]:
        """All roots currently held in the cache, in insertion order."""
        return list(self._cache)
