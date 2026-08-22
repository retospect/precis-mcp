"""Persistent, content-addressed vector cache for `md` kind semantic search.

One npz + json manifest pair per (embedder model id, dim) under the
existing per-host cache root (`precis.config.cache_root`) — NOT per
indexed root, because entries are content-addressed by
`MdBlockEntry.sha256`: worktrees and roots that share prose (this repo
checked out twice, a doc copied verbatim into another root) dedupe for
free, and editing one file only ever adds the sha256s that changed.

Layout, both files named `<safe-model>-<dim>`:

    <cache_dir>/<safe-model>-<dim>.npz    2D float32 array "vectors";
                                           row i is the embedding for
                                           manifest["shas"][i]
    <cache_dir>/<safe-model>-<dim>.json   {"schema": 1, "model": ...,
                                           "dim": ..., "shas": [...]}

Boot (`_load`, called from `__init__`): missing pair -> cold start,
not an error. Present pair -> parse the manifest, validate schema /
model / dim / row-count against the npz, and load hits into memory.
Any failure at any step (missing file, unreadable JSON, shape
mismatch) logs a WARNING and leaves the cache empty — the caller's
next `embed_missing` just re-embeds everything, i.e. "rebuild" is not
a distinct code path, it falls out of the normal miss path.

Write-back (`flush`): atomic temp+rename for the npz, then the same
for the manifest — a concurrent reader either sees the old pair
(npz not yet replaced) or the fully new pair, never a manifest
that names more rows than the array holds. Entries are immutable
(content-addressed), so two session servers sharing a cache directory
can race a flush and lose the loser's new entries — the losing
process's new vectors just never made it to disk, which is a cold
cache-miss on next boot, not corruption: neither writer's array/
manifest pair is ever a mismatched read, and there is nothing to
reconcile. `add()` auto-flushes every `flush_every` new vectors;
callers (the handler's background warm pass / server shutdown hook)
call `flush()` explicitly at exit to persist the tail.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

from precis.config import cache_root
from precis.md_index.types import MdBlockEntry

log = logging.getLogger(__name__)

#: Bumped when the on-disk pair's shape changes incompatibly.
SCHEMA_VERSION = 1

#: Default cache-root subdirectory name (mirrors `patent-raw`,
#: `edgar-raw`'s convention in `precis.config`).
_CACHE_NAME = "md-vectors"

# Allow letters, digits, dot/underscore/hyphen; collapse anything else
# (e.g. the `/` in `BAAI/bge-m3`) to `-`. Filesystem-safe without
# obscuring which model produced the vectors.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(s: str) -> str:
    return _SAFE_NAME_RE.sub("-", s).strip("-") or "unknown"


class MdEmbedder(Protocol):
    """Duck-type for the embedder dependency this module needs.

    Matches `precis.embedder.Embedder` (`BgeM3Embedder`, `MockEmbedder`,
    `RemoteEmbedder`) structurally, without importing that module —
    keeps `md_index` embedder-implementation-agnostic and trivial to
    stub in tests (see `tests/test_md_vectors.py`).
    """

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class MdVectorCache:
    """In-memory vector store for md blocks, keyed by `sha256`, backed
    by an on-disk npz + json manifest pair for one (model, dim) pair.

    `cache_dir` defaults to `precis.config.cache_root('md-vectors')`;
    pass an explicit path in tests to avoid touching the real XDG
    cache. Internally locked (`threading.RLock`, held across each
    method's full body): safe for the background warm thread
    (`precis.server._warm_md_index_background`) to call `embed_missing`/
    `flush` while request threads concurrently call `get`/`__contains__`
    via `MdHandler.get`/`search`. The one deliberate exception is
    `embed_missing`'s call into the embedder, which happens *outside*
    the lock — embedding can take minutes and must never block a
    concurrent search.
    """

    def __init__(
        self,
        *,
        model: str,
        dim: int,
        cache_dir: Path | None = None,
        flush_every: int = 200,
    ) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.model = model
        self.dim = dim
        self.cache_dir = cache_dir if cache_dir is not None else cache_root(_CACHE_NAME)
        self.flush_every = flush_every

        self._lock = threading.RLock()
        self._shas: list[str] = []
        self._index: dict[str, int] = {}
        self._vectors: np.ndarray = np.zeros((0, dim), dtype=np.float32)
        self._dirty_since_flush = 0

        self._load()

    # -- paths ------------------------------------------------------------

    @property
    def _stem(self) -> str:
        return f"{_safe_name(self.model)}-{self.dim}"

    @property
    def npz_path(self) -> Path:
        return self.cache_dir / f"{self._stem}.npz"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / f"{self._stem}.json"

    # -- boot ---------------------------------------------------------------

    def _load(self) -> None:
        """Populate from disk if a valid pair exists; else stay empty.

        Never raises — every failure mode (missing file, corrupt JSON,
        shape mismatch) degrades to "start empty" with a WARNING, per
        the module docstring's rebuild contract. Called from
        `__init__`, before any other thread can hold a reference to
        `self` — locks anyway, for symmetry with every other method
        that touches `_shas`/`_index`/`_vectors`.
        """
        with self._lock:
            npz_exists = self.npz_path.is_file()
            manifest_exists = self.manifest_path.is_file()
            if not npz_exists and not manifest_exists:
                return  # cold start: no pair yet, not an error
            if npz_exists != manifest_exists:
                # A partial pair (one file present, the other not) means
                # a prior flush or manual edit was interrupted or
                # incomplete — an inconsistent, not merely absent,
                # state. Warn.
                log.warning(
                    "md vector cache %s has an incomplete pair (npz=%s, manifest=%s);"
                    " starting empty",
                    self._stem,
                    npz_exists,
                    manifest_exists,
                )
                return

            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if manifest.get("schema") != SCHEMA_VERSION:
                    raise ValueError(f"schema mismatch: {manifest.get('schema')!r}")
                if manifest.get("model") != self.model:
                    raise ValueError(f"model mismatch: {manifest.get('model')!r}")
                if manifest.get("dim") != self.dim:
                    raise ValueError(f"dim mismatch: {manifest.get('dim')!r}")
                shas = manifest["shas"]
                if not isinstance(shas, list):
                    raise ValueError("manifest 'shas' is not a list")

                with np.load(self.npz_path) as npz:
                    vectors = np.asarray(npz["vectors"], dtype=np.float32)
                if vectors.ndim != 2 or vectors.shape[1] != self.dim:
                    raise ValueError(f"vector array shape mismatch: {vectors.shape!r}")
                if vectors.shape[0] != len(shas):
                    raise ValueError(
                        f"row count {vectors.shape[0]} != manifest entries {len(shas)}"
                    )
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                log.warning(
                    "md vector cache %s corrupt or unreadable (%s); starting empty",
                    self._stem,
                    exc,
                )
                return

            self._shas = list(shas)
            self._vectors = vectors
            self._index = {sha: i for i, sha in enumerate(self._shas)}

    # -- lookup ---------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._shas)

    def __contains__(self, sha256: str) -> bool:
        with self._lock:
            return sha256 in self._index

    def get(self, sha256: str) -> np.ndarray | None:
        """The cached vector for `sha256`, or `None` if not cached."""
        with self._lock:
            i = self._index.get(sha256)
            if i is None:
                return None
            return self._vectors[i]

    def missing(self, shas: Iterable[str]) -> list[str]:
        """`shas` not yet cached, de-duplicated, first-seen order."""
        with self._lock:
            seen: set[str] = set()
            out: list[str] = []
            for sha in shas:
                if sha in self._index or sha in seen:
                    continue
                seen.add(sha)
                out.append(sha)
            return out

    # -- write ---------------------------------------------------------------

    def add(self, sha256: str, vector: Sequence[float]) -> None:
        """Add one vector under `sha256`. Idempotent: entries are
        content-addressed and immutable, so a repeat `add()` for an
        already-cached hash is a silent no-op — whichever vector
        arrived first wins, which is fine because same sha256 implies
        same source text implies (for a deterministic embedder) the
        same vector anyway.

        Auto-flushes once `flush_every` new vectors have accumulated.
        """
        with self._lock:
            if sha256 in self._index:
                return
            arr = np.asarray(vector, dtype=np.float32)
            if arr.shape != (self.dim,):
                raise ValueError(f"vector shape {arr.shape} != expected ({self.dim},)")

            self._index[sha256] = len(self._shas)
            self._shas.append(sha256)
            self._vectors = np.vstack([self._vectors, arr[np.newaxis, :]])
            self._dirty_since_flush += 1
            if self._dirty_since_flush >= self.flush_every:
                self.flush()

    def embed_missing(
        self, blocks: Iterable[MdBlockEntry], embedder: MdEmbedder
    ) -> int:
        """Embed and cache every block in `blocks` not already cached.

        Batches every miss into one `embedder.embed()` call. Returns
        the count of newly added vectors (0 touches the embedder not
        at all — safe to call on a fully-warm cache every request).

        The lock is deliberately released for the `embedder.embed()`
        call itself: computing the miss list and writing the results
        back both happen under `self._lock`, but embedding can take
        minutes (cold model load, a large batch) and must never hold
        the lock across it — a concurrent request thread's `get`/
        `search` would stall for the same duration.
        """
        with self._lock:
            by_sha: dict[str, MdBlockEntry] = {}
            for b in blocks:
                if b.sha256 not in self._index:
                    by_sha.setdefault(b.sha256, b)
            if not by_sha:
                return 0
            shas = list(by_sha)
            texts = [by_sha[sha].text for sha in shas]

        vectors = embedder.embed(texts)  # outside the lock — see docstring
        if len(vectors) != len(shas):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(shas)} texts"
            )

        with self._lock:
            for sha, vec in zip(shas, vectors, strict=True):
                self.add(sha, vec)  # RLock: safe to re-enter; auto-flushes
        return len(shas)

    def flush(self) -> None:
        """Atomically persist the in-memory vectors to disk.

        No-op if nothing has changed since the last flush. Best-
        effort: any IO error is logged at WARNING and swallowed — the
        cache is a perf optimization, never load-bearing for
        correctness (a lost flush just means a cold re-embed next
        boot), matching `skill_index.cache.EmbeddingCache.save`.
        """
        with self._lock:
            if self._dirty_since_flush == 0:
                return
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)

                npz_tmp = self.npz_path.with_name(self.npz_path.name + ".tmp")
                with npz_tmp.open("wb") as f:
                    np.savez(f, vectors=self._vectors)
                npz_tmp.replace(self.npz_path)

                manifest = {
                    "schema": SCHEMA_VERSION,
                    "model": self.model,
                    "dim": self.dim,
                    "shas": self._shas,
                }
                manifest_tmp = self.manifest_path.with_name(
                    self.manifest_path.name + ".tmp"
                )
                manifest_tmp.write_text(json.dumps(manifest), encoding="utf-8")
                manifest_tmp.replace(self.manifest_path)

                self._dirty_since_flush = 0
            except OSError as exc:
                log.warning(
                    "md vector cache flush to %s failed: %s", self.cache_dir, exc
                )
