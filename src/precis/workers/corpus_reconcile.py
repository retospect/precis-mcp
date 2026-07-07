"""Corpus-presence reconcile — maintain the per-host ``pdf_locations`` ledger.

Every node (system profile) stats the held-paper PDFs under *its own*
``PRECIS_CORPUS_DIR`` roots and records a verdict per ``(pdf_sha256, host)``:
the path where it found the file, or ``''`` when it looked and the file was
absent. The draft reader then reads that ledger (``Store.pdf_missing``) instead
of re-stat-ing at request time, so "held but missing" (the red ▲) becomes a
corpus-wide fact — independent of which mounts the *web* process happens to
have (ADR 0029).

Pass shape (mirrors ``sweeper`` — a SQL/FS ref-pass, no chunk claim):

* **Self-throttling.** ``pdfs_due_for_host`` returns only shas with no verdict
  for this host or one older than the refresh window
  (``PRECIS_CORPUS_RECONCILE_REFRESH_HOURS``, default 6), stalest first. When
  every held PDF is fresh the pass claims 0 and the worker idles — no busy
  re-stat loop. Recording an *absent* verdict (path ``''``) is what keeps a
  genuinely-missing PDF out of the due set until its next refresh.
* **Idempotent + node-local.** Each node owns its own ``host`` rows; a
  differently-mounted node simply records where *it* sees the file. No cross-
  node coordination, no lock.
* **Bounded.** Up to ``limit`` stats per pass; the whole corpus refreshes over
  many cycles.

Resolution prefers the ingest-recorded ``pdfs.storage_path`` (authoritative,
Step 1) and falls back to the cite_key convention across every configured root
— the same order as the web resolver, but expressed against
``precis.corpus_layout`` so the worker never imports the web package.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from precis.corpus_layout import corpus_pdf_dest, rebase_onto_local
from precis.store import Store
from precis.store._pdf_ops import DuePdf
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


def _refresh_hours() -> float:
    """How stale a verdict may get before this node re-checks it.

    ``PRECIS_CORPUS_RECONCILE_REFRESH_HOURS`` (default 6.0, floor 0.1).
    Deliberately far below the ledger TTL (default 7 days) so a live node
    keeps its rows comfortably fresh.
    """
    raw = os.environ.get("PRECIS_CORPUS_RECONCILE_REFRESH_HOURS")
    if not raw:
        return 6.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 6.0


def _resolve_local(corpus_dirs: tuple[Path, ...], due: DuePdf) -> Path | None:
    """Where this node holds ``due``'s PDF, or ``None`` if absent locally.

    Prefers the authoritative ``storage_path`` (an absolute path recorded at
    ingest), rebasing it onto this node's own NAS mount prefix when the raw
    path was written by a differently-mounted host, then probes the cite_key
    convention across every root.
    """
    if due.storage_path:
        p = Path(due.storage_path)
        if p.is_file():
            return p
        rebased = rebase_onto_local(due.storage_path, corpus_dirs)
        if rebased is not None:
            return rebased
    for cite_key in due.cite_keys:
        for root in corpus_dirs:
            cand = corpus_pdf_dest(cite_key, root)
            if cand.is_file():
                return cand
    return None


def run_corpus_reconcile_pass(
    store: Store,
    corpus_dirs: tuple[Path, ...],
    host: str,
    *,
    limit: int = 50,
) -> BatchResult:
    """Refresh up to ``limit`` due verdicts for ``host``.

    Counters: ``claimed`` = due shas checked this pass; ``ok`` = found on
    disk; ``failed`` = recorded absent (a normal verdict, **not** a pass
    error — surfaced as a counter so the absent count is visible in the
    worker log rollup).
    """
    if not corpus_dirs:
        return BatchResult(handler="corpus_reconcile", claimed=0, ok=0, failed=0)
    due = store.pdfs_due_for_host(host, refresh_hours=_refresh_hours(), limit=limit)
    if not due:
        return BatchResult(handler="corpus_reconcile", claimed=0, ok=0, failed=0)
    n_found = 0
    n_absent = 0
    for d in due:
        found = _resolve_local(corpus_dirs, d)
        store.record_pdf_location(d.pdf_sha256, host, str(found) if found else "")
        if found:
            n_found += 1
        else:
            n_absent += 1
    if n_absent:
        log.info(
            "corpus_reconcile: %s checked %d held PDF(s) — %d present, %d absent",
            host,
            len(due),
            n_found,
            n_absent,
        )
    return BatchResult(
        handler="corpus_reconcile",
        claimed=len(due),
        ok=n_found,
        failed=n_absent,
    )


__all__ = ["run_corpus_reconcile_pass"]
