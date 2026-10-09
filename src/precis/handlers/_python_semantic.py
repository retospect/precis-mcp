"""Semantic (embedding) half of ``PythonHandler.search``.

Mirrors the ``md`` kind's design (``precis.md_index``): DB-free, vectors
cached by content hash in an :class:`~precis.md_index.vectors.MdVectorCache`
under its own cache dir (``python-vectors``), filled by a background thread
so neither server boot nor a request ever waits on a full embed pass.

One text per symbol — ``qualname``, signature, first docstring paragraph —
keyed by the sha256 of that text, so a re-index only embeds symbols whose
text changed (a moved file or a renamed sibling re-embeds nothing).

Degradation is the contract, never an error: no embedder, an embedder that
raises, or a still-cold cache all return "no semantic ranking" and the
caller falls back to lexical with a one-line note (see :meth:`note`).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from precis.md_index.vectors import MdVectorCache
from precis.python_index import Symbol

log = logging.getLogger(__name__)

#: Texts per embed() call; chars cap keeps a batch inside the request-path
#: embedder's deadline (md_index measured the same trap).
_BATCH_SIZE = 32
_BATCH_CHARS = 16_000
_BATCH_ATTEMPTS = 3
_FLUSH_EVERY = 5_000
#: Prune after a complete warm when more than this share of cached
#: vectors belongs to no current symbol.
_PRUNE_STALE_SHARE = 0.5
_BACKOFF_S = 2.0
#: Docstring paragraph cap — the first paragraph carries the intent.
_DOC_CHARS = 400
#: Semantic candidates handed to fusion.
TOP_K = 30
#: Cosine floor below which a "nearest" symbol is noise. Tuned 2026-10-09
#: against real bge-m3 over this repo (47,890 symbol vectors), 15 prose
#: queries with known targets + 5 off-topic/nonsense queries:
#:   targets in the top 30 (ranks 1-6, n=11): sim 0.54-0.73
#:   targets at rank >30 (n=4, never returned anyway): 0.46-0.55
#:   off-topic best hit: 0.41-0.50 (max 0.496, "qwerty asdf zxcv")
#: 0.50 drops every off-topic result and no retrievable target; 0.40 let
#: all five off-topic queries return a full page of noise.
MIN_SIM = 0.50

#: Last warm outcome, read by ``precis-status`` (``python_vector_warmup``).
_WARMUP_STATE: str | None = None


def warmup_state() -> str | None:
    """The warm pass's last recorded outcome, or ``None`` if it never ran."""
    return _WARMUP_STATE


def _record(state: str) -> None:
    global _WARMUP_STATE
    _WARMUP_STATE = state


def _is_test_file(file: str) -> bool:
    parts = file.split("/")
    base = parts[-1] if parts else file
    return bool(parts) and (
        parts[0] == "tests" or base.startswith("test_") or base.endswith("_test.py")
    )


def embeddable(sym: Symbol) -> bool:
    """Whether ``sym`` gets a vector.

    Private helpers in test files are skipped: they are the bulk of the
    volume and never the answer to a "where do we ..." question.
    """
    return not (_is_test_file(sym.file) and sym.name.startswith("_"))


def symbol_text(sym: Symbol) -> str:
    """The embedded text: qualname, signature, first docstring paragraph."""
    parts = [sym.qualname]
    if sym.signature:
        parts.append(sym.signature)
    if sym.docstring:
        para = sym.docstring.strip().split("\n\n", 1)[0]
        parts.append(" ".join(para.split())[:_DOC_CHARS])
    return "\n".join(parts)


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SemanticIndex:
    """Vector store + warm state for one ``PythonHandler``."""

    def __init__(self, embedder: Any, *, cache_dir: Path | None = None) -> None:
        self.embedder = embedder
        self._cache_dir = cache_dir
        self._cache: MdVectorCache | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._retry_at = 0.0
        self.state = "no-embedder" if embedder is None else "idle"

    # -- cache ---------------------------------------------------------

    def cache(self) -> MdVectorCache | None:
        """The vector cache, built lazily (``embedder.model`` may be HTTP)."""
        if self.embedder is None:
            return None
        if self._cache is None:
            try:
                self._cache = MdVectorCache(
                    model=self.embedder.model,
                    dim=self.embedder.dim,
                    # Each flush rewrites the whole npz (~200 MB fully warm);
                    # md's default of 200 would rewrite it ~240x per cold warm.
                    flush_every=_FLUSH_EVERY,
                    cache_dir=self._cache_dir
                    if self._cache_dir is not None
                    else _default_cache_dir(),
                )
            except Exception as exc:  # unreachable embedder: degrade
                log.warning("python semantic cache unavailable: %s", exc)
                return None
        return self._cache

    # -- warm ----------------------------------------------------------

    def start_warmup(
        self,
        symbols: Callable[[], Iterable[Symbol]],
        *,
        keep: Callable[[], Iterable[Symbol]] | None = None,
        block: bool = False,
    ) -> None:
        """Start one background warm pass (no-op when running or warm).

        ``symbols`` is called on the warm thread, so indexing the repos
        never delays the caller. ``keep`` yields extra symbols (e.g. worktree
        indexes held in memory) whose vectors survive a prune but are not
        embedded here. After a failure, re-arm at most every
        60s (a request that finds the index cold triggers it).
        """
        if self.embedder is None:
            return
        with self._lock:
            if self.state in ("warming", "ready"):
                return
            if time.monotonic() < self._retry_at:
                return
            self.state = "warming"
            _record("warming: starting")
            t = threading.Thread(
                target=self._warm,
                args=(symbols, keep),
                name="python-semantic-warmup",
                daemon=True,
            )
            self._thread = t
        t.start()
        if block:
            t.join()

    def _warm(
        self,
        symbols: Callable[[], Iterable[Symbol]],
        keep: Callable[[], Iterable[Symbol]] | None = None,
    ) -> None:
        try:
            cache = self.cache()
            if cache is None:
                raise RuntimeError("embedder unreachable")
            by_key: dict[str, str] = {}
            for sym in symbols():
                if embeddable(sym):
                    text = symbol_text(sym)
                    by_key.setdefault(text_key(text), text)
            missing = cache.missing(by_key)
            batches = _plan(missing, by_key)
            added = skipped = 0
            for i, batch in enumerate(batches):
                vecs = self._embed_batch([by_key[k] for k in batch])
                if vecs is None:
                    skipped += 1
                    continue
                for k, v in zip(batch, vecs, strict=True):
                    cache.add(k, v)
                added += len(batch)
                _record(f"warming: batch {i + 1}/{len(batches)} ({added} new)")
            if not skipped:
                # Stale vectors (edited/deleted symbols) are never looked up
                # again. Prune only when they dominate: another server sharing
                # this cache dir with different roots would re-embed its share.
                live = set(by_key)
                for sym in keep() if keep is not None else ():
                    if embeddable(sym):
                        live.add(text_key(symbol_text(sym)))
                stale = len(cache) - sum(1 for k in live if k in cache)
                if stale > len(cache) * _PRUNE_STALE_SHARE:
                    dropped = cache.retain(live)
                    log.info("python semantic cache pruned %d stale vectors", dropped)
            cache.flush()
            with self._lock:
                if skipped:
                    self.state = "idle"
                    self._retry_at = time.monotonic() + 60.0
                    _record(
                        f"warm with gaps ({added} new, {skipped} batch(es) skipped)"
                    )
                else:
                    self.state = "ready"
                    _record(f"warm ({added} new, {len(by_key)} symbols)")
        except Exception as exc:
            log.warning("python semantic warmup failed: %s", exc)
            with self._lock:
                self.state = "idle"
                self._retry_at = time.monotonic() + 60.0
            _record(f"COLD: {type(exc).__name__} — python search is lexical-only")

    def _embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        for attempt in range(1, _BATCH_ATTEMPTS + 1):
            try:
                out = self.embedder.embed(texts)
            except Exception as exc:
                if attempt >= _BATCH_ATTEMPTS:
                    log.warning("python semantic batch skipped: %s", exc)
                    return None
                time.sleep(getattr(exc, "retry_after_s", None) or _BACKOFF_S * attempt)
                continue
            if len(out) != len(texts):
                raise ValueError(
                    f"embedder returned {len(out)} vectors for {len(texts)}"
                )
            return out
        return None

    # -- query ---------------------------------------------------------

    def rank(
        self, query: str, syms: list[Symbol]
    ) -> tuple[list[tuple[float, int]], str | None]:
        """Cosine-rank ``syms`` against ``query``.

        Returns ``(hits, note)``: ``hits`` is ``[(sim, index into syms)]``
        best first (empty when semantic ranking is unavailable) and
        ``note`` a one-line reason shown to the caller, or ``None`` when
        the ranking is complete.
        """
        if self.embedder is None:
            return [], "lexical only: no embedder wired"
        cache = self.cache()
        if cache is None:
            return [], "lexical only: embedder unreachable"
        rows: list[int] = []
        vecs: list[np.ndarray] = []
        for i, sym in enumerate(syms):
            if not embeddable(sym):
                continue
            v = cache.get(text_key(symbol_text(sym)))
            if v is not None:
                rows.append(i)
                vecs.append(v)
        wanted = sum(1 for s in syms if embeddable(s))
        if not vecs:
            return [], "lexical only: semantic index warming (0% ready)"
        try:
            qv = np.asarray(self.embedder.embed_one(query), dtype=np.float32)
        except Exception as exc:
            log.warning("python semantic query embed failed: %s", exc)
            return [], "lexical only: embedder unavailable"
        qn = float(np.linalg.norm(qv))
        mat = np.stack(vecs)
        norms = np.linalg.norm(mat, axis=1)
        sims = (mat @ qv) / np.maximum(norms * max(qn, 1e-9), 1e-9)
        order = np.argsort(-sims)[:TOP_K]
        hits = [(float(sims[j]), rows[j]) for j in order if sims[j] >= MIN_SIM]
        note = None
        if len(vecs) < wanted:
            note = (
                f"semantic index warming ({100 * len(vecs) // wanted}% ready); "
                "ranking is partly lexical"
            )
        return hits, note


def _plan(keys: list[str], by_key: dict[str, str]) -> list[list[str]]:
    batches: list[list[str]] = []
    cur: list[str] = []
    chars = 0
    for k in keys:
        n = len(by_key[k])
        if cur and (len(cur) >= _BATCH_SIZE or chars + n > _BATCH_CHARS):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(k)
        chars += n
    if cur:
        batches.append(cur)
    return batches


def _default_cache_dir() -> Path:
    from precis.config import cache_root

    return cache_root("python-vectors")


def rrf(
    rank_lists: list[list[int]], weights: list[float], k: int = 60
) -> dict[int, float]:
    """Weighted reciprocal-rank fusion over lists of item ids."""
    out: dict[int, float] = {}
    for ranks, w in zip(rank_lists, weights, strict=True):
        for pos, item in enumerate(ranks):
            out[item] = out.get(item, 0.0) + w / (k + pos + 1)
    return out
