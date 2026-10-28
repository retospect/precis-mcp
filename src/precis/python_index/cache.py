"""Root-scoped incremental AST cache with explicitly stat-checked freshness.

Unchanged size/mtime/ctime/device/inode reuses the indexed byte snapshot; it
does not prove current content equality. Changed files get at most two parse
attempts, with before/after identity checks. Failed/unstable files are omitted
instead of attaching a successful fresh stamp to an old module. Tree walks
are not atomic snapshots; partial walks retain earlier entries with a warning.

Optional on-disk snapshot (``snapshot_dir``): the per-root cache is pickled
to one file per root whenever a ``get`` reparsed or dropped files, and a cold
in-memory cache is seeded from it. Seeded entries are candidates only — the
per-file stat check still revalidates every signature, so a stale snapshot
degrades to the usual reparse, never to stale content. What it skips is the
full AST re-parse a fresh server process would otherwise pay on its first
read (~17 s for a ~4000-file worktree corpus, gr525769). The payload is
gzip-pickle (level 1 — ~3x smaller for negligible CPU; sources dominate):
the snapshot dir is the same trust domain as the venv and must stay
user-owned.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import pickle
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from precis.python_index.indexer import (
    _qualname_for_file,
    _walk_python_files,
    index_module,
)
from precis.python_index.types import IndexObservation, ModuleIndex, RepoIndex

log = logging.getLogger(__name__)

#: Bumped when the snapshot payload shape changes; mismatches are a miss.
_SNAPSHOT_SCHEMA = 1


def _snapshot_path(snapshot_dir: Path, root: Path) -> Path:
    """One file per root: basename for humans, path hash for uniqueness."""
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return snapshot_dir / f"{root.name}-{key}.pkl.gz"


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino


@dataclass(slots=True)
class _CachedFile:
    module: ModuleIndex
    signature: tuple[int, int, int, int, int]


class RepoCache:
    """Independent per-root caches; stat checks reuse ASTs without byte scans.

    Builds for the same root are serialized on a per-root lock
    (gr525769): a background warm and a synchronous read, or two
    concurrent first reads, share one build — the second caller waits
    on the lock and gets the finished index instead of reparsing in
    parallel. Different roots still build concurrently.

    ``snapshot_dir`` enables the on-disk snapshot (module docstring); None
    keeps the cache process-local (the default, and what tests inject).
    """

    def __init__(self, snapshot_dir: Path | None = None) -> None:
        self._cache: dict[Path, dict[str, _CachedFile]] = {}
        self._locks: dict[Path, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._snapshot_dir = snapshot_dir

    def _lock_for(self, root: Path) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(root, threading.Lock())

    def get(self, root: Path) -> RepoIndex:
        root = root.resolve()
        with self._lock_for(root):
            return self._get(root)

    def _get(self, root: Path) -> RepoIndex:
        if not root.is_dir():
            self.drop(root)
            raise NotADirectoryError(f"not a directory: {root}")
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        root_identity = _signature(root)[3:]
        files_cache = self._cache.setdefault(root, {})
        if not files_cache:
            self._load_snapshot(root, files_cache)
        issues: list[str] = []
        current: dict[str, Path] = {}
        try:
            for path in _walk_python_files(root):
                current[path.relative_to(root).as_posix()] = path
        except OSError as exc:
            issues.append(
                f"partial walk: {type(exc).__name__}; retained entries unverified"
            )
        removed = 0
        if not issues:
            for rel in files_cache.keys() - current.keys():
                del files_cache[rel]
                removed += 1

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
        if reparsed or removed:
            self._save_snapshot(root)
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

    def warm(self, root: Path, *, wanted: Callable[[], bool]) -> RepoIndex | None:
        """Speculative build under the per-root lock; ``None`` when skipped.

        A warm queues behind real builds on the same lock: by the time it
        runs, the root may already be cached (a real query finished it —
        skip, the work is done) or dropped + delisted while it waited
        (skip — rebuilding would re-cache a root no eviction path
        reaches). ``wanted`` runs under the lock so the check cannot
        race the drop.
        """
        root = root.resolve()
        with self._lock_for(root):
            if root in self._cache or not wanted():
                return None
            return self._get(root)

    def drop(self, root: Path) -> None:
        """Forget a root; the next read reparses its files.

        The on-disk snapshot (when enabled) is kept: the next ``get``
        re-seeds from it and stat-validates, which is exactly the fast
        re-warm path wanted after an LRU/idle eviction.
        """
        self._cache.pop(root.resolve(), None)

    def _load_snapshot(self, root: Path, into: dict[str, _CachedFile]) -> None:
        """Seed a cold per-root cache from the on-disk snapshot, if any.

        Any read or shape problem is a cache miss — the caller falls
        through to the usual full parse.
        """
        if self._snapshot_dir is None:
            return
        path = _snapshot_path(self._snapshot_dir, root)
        try:
            with gzip.open(path, "rb") as fh:
                payload = pickle.load(fh)
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != _SNAPSHOT_SCHEMA
                or payload.get("root") != str(root)
                or not isinstance(payload.get("files"), dict)
            ):
                return
            for rel, (signature, module) in payload["files"].items():
                if isinstance(module, ModuleIndex):
                    into[rel] = _CachedFile(module, tuple(signature))
        except (
            OSError,
            EOFError,
            ValueError,
            TypeError,
            AttributeError,
            ImportError,
            pickle.UnpicklingError,
        ) as exc:
            log.debug("python corpus snapshot load failed for %s: %s", root, exc)

    def _save_snapshot(self, root: Path) -> None:
        """Persist the per-root cache so the next process skips the reparse.

        Best-effort — a cache that cannot be written only costs the next
        cold start its speedup. Atomic via a pid-unique tmp + replace, so
        concurrent servers can race harmlessly (last writer wins).
        """
        if self._snapshot_dir is None:
            return
        files_cache = self._cache.get(root)
        if not files_cache:
            return
        path = _snapshot_path(self._snapshot_dir, root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": _SNAPSHOT_SCHEMA,
                "root": str(root),
                "files": {
                    rel: (cf.signature, cf.module) for rel, cf in files_cache.items()
                },
            }
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(gzip.compress(pickle.dumps(payload), compresslevel=1))
            tmp.replace(path)
        except (OSError, pickle.PicklingError, RecursionError) as exc:
            log.debug("python corpus snapshot write failed for %s: %s", root, exc)

    def held_modules(self, root: Path) -> list[ModuleIndex]:
        """Modules currently cached for ``root``; no stat, no reparse."""
        files = self._cache.get(root.resolve(), {})
        return [cf.module for cf in list(files.values())]

    def known_roots(self) -> list[Path]:
        return list(self._cache)
