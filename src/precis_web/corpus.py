"""Corpus PDF path resolution, shared across web routes.

A held paper's PDF lives at ``<corpus_root>/<letter>/<cite_key>.pdf`` where
``letter`` is the lower-cased first alnum char of the cite_key (else ``_``),
mirroring ``precis.cli.watch._move_to_corpus``. ``PRECIS_CORPUS_DIR`` may list
several roots (``:``-separated, ADR 0029) so a per-host NFS mount difference
is searched rather than fatal, and a paper may carry several ``cite_key``
aliases (author-year key + a book's bib key from ``tex-import``) — the fetcher
files the PDF under whichever it chose, which need not be the display slug, so
resolution probes every alias.

Both the paper reader (which streams the file) and the draft reader (which flags
a cited paper whose file is *missing*) need this, so it lives here rather than
private to one route.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any


def pdf_candidates(
    corpus_dirs: tuple[Path, ...], cite_keys: str | Sequence[str]
) -> list[Path]:
    """All on-disk PDF paths to try, one per (cite_key × corpus root).

    ``cite_keys`` accepts a single key or a sequence — a paper can carry more
    than one ``cite_key`` alias and the fetcher files the PDF under whichever
    it chose as the filename stem. Order is preserved and duplicates dropped so
    the returned list is a stable, de-duped probe order.
    """
    keys = [cite_keys] if isinstance(cite_keys, str) else list(cite_keys)
    out: list[Path] = []
    seen: set[Path] = set()
    for cite_key in keys:
        if not cite_key:
            continue
        letter = cite_key[0].lower() if cite_key[0].isalnum() else "_"
        for root in corpus_dirs:
            cand = root / letter / f"{cite_key}.pdf"
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def resolve_pdf(
    corpus_dirs: tuple[Path, ...], cite_keys: str | Sequence[str]
) -> Path | None:
    """First existing PDF path across the corpus roots, or ``None``.

    Tries every cite_key alias (see :func:`pdf_candidates`) so a paper whose
    file is filed under a non-display alias still resolves.
    """
    for path in pdf_candidates(corpus_dirs, cite_keys):
        if path.is_file():
            return path
    return None


def ref_pdf_keys(store: Any, ref: Any) -> list[str]:
    """De-duped cite_key probe order for ``ref``: display slug first, then
    every other ``cite_key`` alias the ref carries."""
    keys: list[str] = []
    seen: set[str] = set()
    for k in [ref.slug or "", *store.ref_cite_keys(ref.id)]:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


__all__ = ["pdf_candidates", "ref_pdf_keys", "resolve_pdf"]
