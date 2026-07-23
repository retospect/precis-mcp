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

import functools
import hashlib
import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from precis.skill_index.cache import (
    CachedChunk,
    CacheEntry,
    EmbeddingCache,
    default_cache_dir,
)
from precis.skill_index.chunker import CHUNKER_VERSION, Chunk, chunk_by_h2

#: Wall-clock cap on a single embedder call (query embed or a per-slug
#: build embed) made from inside :class:`FileCorpusIndex`. ``is_ready()``
#: already gates *cold* BgeM3 loads (see ``_build``'s docstring and
#: ``embedder.BgeM3Embedder._raise_if_warming``) so once a call reaches
#: here the embedder believes itself loaded — this guard is for the
#: *other* failure mode: a hung/deadlocked encode (stuck native thread,
#: a RemoteEmbedder whose transport ignores its own timeout) that would
#: otherwise block the MCP transport indefinitely (observed: one
#: ``search(kind='skill', ...)`` call idle for the full 1800s
#: client-side timeout before being force-aborted). 30s comfortably
#: covers a real encode of a handful of skill chunks; a wedge past that
#: is failed fast into the same "semantic unavailable, lexical carries
#: on" path a cold embedder already produces, rather than dropping
#: dead silent for half an hour.
_EMBED_CALL_TIMEOUT_S = 30.0

#: Sentinel: the embedder call exceeded :data:`_EMBED_CALL_TIMEOUT_S`.
_TIMED_OUT: Any = object()


def _bounded(fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
    """Run ``fn(*args)`` with a wall-clock cap.

    Mirrors ``ingest.metadata_resolve._bounded`` (same shape, different
    subsystem — that one bounds Crossref/S2 network lookups, this one
    bounds embedder calls). Returns :data:`_TIMED_OUT` if the call
    overran; the underlying thread is abandoned, not force-killed — it
    dies on its own once/if the embedder call eventually returns.
    """
    if timeout <= 0:
        return fn(*args)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        return _TIMED_OUT
    finally:
        ex.shutdown(wait=False)


#: The index embeds body-only twins (v3) in addition to the
#: structural per-heading chunks — extra embedding surface costs a
#: few vectors per skill (corpus is ~150 chunks) and buys a
#: heading-noise-free match. Structural-only callers (the ``slug~N``
#: addresser, the TOC adapter) keep the default ``chunk_by_h2``.
_index_chunker = functools.partial(chunk_by_h2, with_body_aliases=True)

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
    #: True for a v3 body-only twin (heading-stripped embedding of a
    #: section already represented by a structural chunk). Callers
    #: counting "distinct sections matched" should skip these.
    body_only: bool = False


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
        chunker: Callable[[str], list[Chunk]] = _index_chunker,
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

    def search(self, q: str, page_size: int = 10) -> list[SearchHit]:
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

        # Build (chunk + embed + cache) on first call. Any failure here
        # — unwritable cache dir, embedder blowing up mid-corpus, a
        # cache deserialisation error — must leave semantic search
        # unavailable (return []) so the caller's lexical fallback still
        # answers, rather than escaping as a 500 (gripe #38690: skill
        # search returned internal server error for ordinary queries).
        try:
            self._build()
        except Exception as exc:
            log.warning("skill_index: build failed: %s", exc, exc_info=True)
            return []
        entries = self._entries or {}
        if not entries:
            return []

        try:
            qv = _bounded(
                self._embedder.embed_one,  # type: ignore[union-attr]
                q,
                timeout=_EMBED_CALL_TIMEOUT_S,
            )
        except Exception as exc:  # pragma: no cover — embedder failure
            log.warning("skill_index: query embed failed: %s", exc)
            return []
        if qv is _TIMED_OUT:
            log.warning(
                "skill_index: query embed exceeded %.0fs wall-clock cap — "
                "treating semantic search as unavailable this call",
                _EMBED_CALL_TIMEOUT_S,
            )
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
                        body_only=chunk.body_only,
                    )
                )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:page_size]

    # ── build / cache ──────────────────────────────────────────────

    def _build(self) -> None:
        """Populate the in-memory entry table, embedding as needed.

        Idempotent **once a successful build lands**. Until then, every
        ``search`` call retries the build — so a cold-embedder first
        call doesn't poison the index with an empty entry table for
        the lifetime of the process. Broad-pass usability finding
        R2#1: my earlier ``is_ready()`` short-circuit on
        ``BgeM3Embedder.embed*`` interacted with the previous
        unconditional ``_entries = out`` assignment by killing
        natural-language skill search forever once the first cold
        search ran during warmup.

        Strategy now: if the embedder advertises an ``is_ready()``
        method (Mock / Remote / BgeM3 — see ``precis.embedder``) and
        reports False, skip the build and leave ``_entries`` unset.
        The next call retries — by then the background warmup
        thread (``server._warm_embedder_background``) has typically
        finished. Backends with no ``is_ready()`` (third-party
        custom embedders, older test fakes) keep the historical
        unconditional behaviour.
        """
        if self._entries is not None:
            return
        is_ready = getattr(self._embedder, "is_ready", None)
        if callable(is_ready) and not is_ready():
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
            vectors = _bounded(
                self._embedder.embed,  # type: ignore[union-attr]
                [c.text for c in chunks],
                timeout=_EMBED_CALL_TIMEOUT_S,
            )
        except Exception as exc:
            log.warning("skill_index: embed failed for %s: %s", slug, exc)
            return None
        if vectors is _TIMED_OUT:
            log.warning(
                "skill_index: embed for %s exceeded %.0fs wall-clock cap",
                slug,
                _EMBED_CALL_TIMEOUT_S,
            )
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
            CachedChunk(
                heading=c.heading,
                text=c.text,
                embedding=list(v),
                body_only=c.body_only,
            )
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
