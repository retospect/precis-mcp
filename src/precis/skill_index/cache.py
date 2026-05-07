"""Disk cache for per-slug chunk embeddings.

One JSON file per slug under

    <cache_dir>/<namespace>/<embedder_safe>/v<chunker_version>/<slug>.json

Format:
    {
        "schema": 1,
        "slug": "...",
        "file_sha256": "...",
        "embedder_model": "...",
        "chunker_version": 1,
        "chunks": [
            {"heading": "...", "text": "...", "embedding": [...]},
            ...
        ]
    }

Cache hits require an exact match on ``file_sha256``,
``embedder_model``, and ``chunker_version`` — any mismatch invalidates
the entry, which falls through to a re-embed by the caller.

Why JSON not numpy ``.npz``: keeps the cache self-describing, lets
``numpy`` stay an optional dep (it travels with sentence-transformers
in the ``[paper]`` extra; a build without that extra has no embedder
anyway, so this layer is moot then), and 1024-float chunks at ~25 KB
per file × ~150 chunks = ~4 MB total — disk noise.

Default cache root: ``$PRECIS_CACHE_DIR`` if set, else
``~/.cache/precis``. Override via the explicit ``cache_dir=``
constructor arg on :class:`FileCorpusIndex`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Bumped when the on-disk file format changes. Distinct from
#: ``CHUNKER_VERSION`` because a layout-only schema bump shouldn't
#: force re-embedding when the chunks themselves are unchanged.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CachedChunk:
    """One chunk's worth of cached state (chunk text + its vector).

    The cache stores the chunk text alongside the vector so the
    in-memory index can rebuild itself from disk without re-reading
    the source file when nothing is stale.
    """

    heading: str
    text: str
    embedding: list[float]


@dataclass
class CacheEntry:
    """One slug's full cache record."""

    slug: str
    file_sha256: str
    embedder_model: str
    chunker_version: int
    chunks: list[CachedChunk]


def default_cache_dir() -> Path:
    """Pick the user's cache root for precis."""
    env = os.environ.get("PRECIS_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "precis"


# Allow letters, digits, and hyphens; collapse anything else to ``-``.
# Keeps embedder model names like ``BAAI/bge-m3`` filesystem-safe
# without obscuring which model produced the vectors.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(s: str) -> str:
    return _SAFE_NAME_RE.sub("-", s).strip("-") or "unknown"


class EmbeddingCache:
    """Read/write per-slug cache files.

    Stateless apart from the path layout. Concurrent boots writing
    the same file race harmlessly because the content is determined
    by the inputs — last writer wins, and either copy is correct.

    Read errors (corrupt JSON, schema mismatch, unreadable file)
    are swallowed with a debug-level log; the caller treats it as a
    cache miss and re-embeds.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        namespace: str,
        embedder_model: str,
        chunker_version: int,
    ) -> None:
        self.cache_dir = cache_dir
        self.namespace = namespace
        self.embedder_model = embedder_model
        self.chunker_version = chunker_version

    @property
    def root(self) -> Path:
        return (
            self.cache_dir
            / self.namespace
            / _safe_name(self.embedder_model)
            / f"v{self.chunker_version}"
        )

    def path_for(self, slug: str) -> Path:
        return self.root / f"{_safe_name(slug)}.json"

    def load(self, slug: str, file_sha256: str) -> CacheEntry | None:
        """Return the cached entry if it matches every keyed field."""
        path = self.path_for(slug)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("skill cache read failed for %s: %s", slug, exc)
            return None
        if data.get("schema") != SCHEMA_VERSION:
            return None
        if data.get("slug") != slug:
            return None
        if data.get("file_sha256") != file_sha256:
            return None
        if data.get("embedder_model") != self.embedder_model:
            return None
        if data.get("chunker_version") != self.chunker_version:
            return None
        chunks_raw = data.get("chunks") or []
        try:
            chunks = [
                CachedChunk(
                    heading=str(c["heading"]),
                    text=str(c["text"]),
                    embedding=[float(x) for x in c["embedding"]],
                )
                for c in chunks_raw
            ]
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("skill cache shape mismatch for %s: %s", slug, exc)
            return None
        return CacheEntry(
            slug=slug,
            file_sha256=file_sha256,
            embedder_model=self.embedder_model,
            chunker_version=self.chunker_version,
            chunks=chunks,
        )

    def save(self, entry: CacheEntry) -> None:
        """Write ``entry`` atomically to disk.

        Best-effort: any IO error is logged at debug and swallowed
        because the cache is a perf optimization — the index still
        works without it, just at boot-time cost.
        """
        path = self.path_for(entry.slug)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": SCHEMA_VERSION,
                "slug": entry.slug,
                "file_sha256": entry.file_sha256,
                "embedder_model": entry.embedder_model,
                "chunker_version": entry.chunker_version,
                "chunks": [asdict(c) for c in entry.chunks],
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("skill cache write failed for %s: %s", entry.slug, exc)
