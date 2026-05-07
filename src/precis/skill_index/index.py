"""In-memory chunk index with cosine search and lexical fallback.

:class:`FileCorpusIndex` ties the chunker and the disk cache together:

1. On first :meth:`search`, the index walks the input ``files`` dict,
   chunks each entry, looks up the cache, and embeds anything that
   missed. Each successful embed is written back to the cache so
   subsequent boots load from disk in milliseconds.
2. The query is embedded once per call; cosine similarity is
   computed against every chunk vector in pure Python (no numpy
   dependency at this layer; ~150 chunks × 1024 dims = sub-ms).
3. Hits are returned sorted by descending score with the slug,
   heading, and a snippet so the caller can render them.

When no embedder is wired (``embedder=None``), the index reports
:meth:`is_available` as False — callers fall back to whatever
lexical strategy they had before. The chunker still runs (no embed
required) so the same path works for tests.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from precis.skill_index.cache import (
    CachedChunk,
    CacheEntry,
    EmbeddingCache,
    default_cache_dir,
)
from precis.skill_index.chunker import CHUNKER_VERSION, Chunk, chunk_by_h2

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    """One ranked chunk match.

    ``score`` is cosine similarity in ``[-1, 1]``; bge-m3 vectors
    are L2-normalized so cosine == dot product. Snippet is the
    first non-empty line of the chunk for display, trimmed.
    """

    slug: str
    chunk_idx: int
    heading: str
    score: float
    snippet: str


class FileCorpusIndex:
    """Embedded index over a fixed map of ``slug -> raw_text``.

    Shape:

    - The caller assembles ``files`` (typically by walking a data
      directory). The index has no opinion on where the text came
      from.
    - Chunking is delegated to a callable so a future kind with
      different layout (e.g. YAML-per-entry oracles in-memory) can
      reuse the cosine + cache machinery.
    - The cache is namespaced (``cache_namespace='skill'`` for
      :class:`SkillHandler`) so multiple corpora can coexist under
      the same ``$PRECIS_CACHE_DIR``.

    Lifecycle: cheap to construct; the first :meth:`search` triggers
    chunking + embedding + cache I/O. Reset by simply discarding
    and re-constructing.
    """

    def __init__(
        self,
        *,
        files: dict[str, str],
        embedder: object | None,
        cache_namespace: str = "corpus",
        cache_dir: Path | None = None,
        chunker: Callable[[str], list[Chunk]] = chunk_by_h2,
    ) -> None:
        self._files = files
        self._embedder = embedder
        self._chunker = chunker
        self._cache_namespace = cache_namespace
        self._cache_dir = cache_dir or default_cache_dir()
        self._cache: EmbeddingCache | None = None
        self._entries: dict[str, CacheEntry] | None = None

    # ── availability ───────────────────────────────────────────────

    def is_available(self) -> bool:
        """True iff a search call would route through cosine search.

        False when no embedder is wired or when the embedder lacks
        the duck-typed ``embed`` / ``embed_one`` / ``model``
        attributes the index needs. Falsey result is the caller's
        cue to use its lexical fallback.
        """
        e = self._embedder
        if e is None:
            return False
        return all(hasattr(e, attr) for attr in ("embed", "embed_one", "model"))

    # ── search ─────────────────────────────────────────────────────

    def search(self, q: str, top_k: int = 10) -> list[SearchHit]:
        """Cosine search the corpus for ``q``.

        Returns ``[]`` when the index isn't available (see
        :meth:`is_available`) or when the corpus is empty. Build
        side-effects — chunking, cache reads, embedding misses,
        cache writes — happen on the first call and amortise over
        subsequent ones.
        """
        if not q or not q.strip():
            return []
        if not self.is_available():
            return []

        self._build()
        entries = self._entries or {}
        if not entries:
            return []

        try:
            qv = self._embedder.embed_one(q)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover — embedder failure
            log.warning("skill_index: query embed failed: %s", exc)
            return []

        hits: list[SearchHit] = []
        for slug, entry in entries.items():
            for idx, chunk in enumerate(entry.chunks):
                score = _cosine(qv, chunk.embedding)
                hits.append(
                    SearchHit(
                        slug=slug,
                        chunk_idx=idx,
                        heading=chunk.heading,
                        score=score,
                        snippet=_snippet(chunk.text),
                    )
                )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    # ── build / cache ──────────────────────────────────────────────

    def _build(self) -> None:
        """Populate the in-memory entry table, embedding as needed.

        Idempotent — once ``self._entries`` is set we never rebuild
        within the lifetime of this object. The caller drops the
        index when it wants a fresh build (e.g. file reloaded).
        """
        if self._entries is not None:
            return
        if self._cache is None:
            model = getattr(self._embedder, "model", "unknown")
            self._cache = EmbeddingCache(
                cache_dir=self._cache_dir,
                namespace=self._cache_namespace,
                embedder_model=str(model),
                chunker_version=CHUNKER_VERSION,
            )

        out: dict[str, CacheEntry] = {}
        for slug, raw in self._files.items():
            entry = self._build_one(slug, raw)
            if entry is None:
                continue
            out[slug] = entry
        self._entries = out

    def _build_one(self, slug: str, raw: str) -> CacheEntry | None:
        """Build (or load from cache) a single slug's entry."""
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cache = self._cache
        assert cache is not None  # set in _build()

        cached = cache.load(slug, sha)
        if cached is not None:
            return cached

        chunks = self._chunker(raw)
        if not chunks:
            return None

        try:
            vectors = self._embedder.embed(  # type: ignore[union-attr]
                [c.text for c in chunks]
            )
        except Exception as exc:
            log.warning("skill_index: embed failed for %s: %s", slug, exc)
            return None
        if len(vectors) != len(chunks):
            log.warning(
                "skill_index: embedder returned %d vectors for %d chunks (slug=%s)",
                len(vectors),
                len(chunks),
                slug,
            )
            return None

        cached_chunks = [
            CachedChunk(heading=c.heading, text=c.text, embedding=list(v))
            for c, v in zip(chunks, vectors, strict=False)
        ]
        entry = CacheEntry(
            slug=slug,
            file_sha256=sha,
            embedder_model=cache.embedder_model,
            chunker_version=cache.chunker_version,
            chunks=cached_chunks,
        )
        cache.save(entry)
        return entry


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Returns 0.0 when either vector has zero norm rather than
    raising — keeps the search loop simple at the cost of a tiny
    bias toward ranking degenerate vectors below real ones.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _snippet(text: str, max_chars: int = 140) -> str:
    """First non-blank, non-heading line of a chunk, trimmed."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if len(line) > max_chars:
            line = line[: max_chars - 1].rstrip() + "…"
        return line
    return ""
