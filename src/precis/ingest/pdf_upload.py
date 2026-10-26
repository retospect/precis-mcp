"""Staged PDF uploads — the ``PRECIS_UPLOAD_STAGING`` holding pen
(gr517423, ``paper-backup-pdf-transfer.md`` slice 6).

The signed-upload route (:mod:`precis_web.routes.files`) streams an
agent's local PDF to ``$PRECIS_UPLOAD_STAGING``;
``put(kind='paper', args={'pdf_upload': '<sha>'})`` then queues a
``paper_pull`` job that runs the same ``check_pdf`` gate + sidecar +
inbox drop as a fetched URL. The bytes never cross the MCP boundary
either way.

The staging dir is deliberately OUTSIDE every watched tree: it began
as ``<PRECIS_WATCH_INBOX>/.uploads`` until the still-deployed
acatome-extract watcher — code outside this repo, its exclusion list
unpatchable from here — turned out to scan that same inbox; a staged
``<ref_id>-<sha>.pdf`` sitting up to 24 h under it would fold as an
unbound paper and move away before its ``pdf_upload`` attach ran
(feat-a2 MF1). Deploy points the var at a SIBLING of the inbox on the
same NAS share so the attach's ``os.replace`` stays a same-filesystem
rename, and :func:`uploads_dir` treats a value that resolves inside
``PRECIS_WATCH_INBOX`` as unset — staging never falls back into the
inbox. The legacy ``.uploads`` name stays in the watcher's
``_MANAGED_DIRS`` as a second guard.

The staged file IS the record — no ``pdfs`` row is written at upload
time. The ``pdfs`` row belongs to the ingest fold that runs when the
attach's inbox drop lands (with the real ``page_count`` and
``storage_path``); an early row only ever carried a dead staging
path and ``page_count 0`` the fold could never rewrite, and it made
the sweep's file deletion a silent pdfs-row delete — a paper could
lose its PDF when a same-sha re-upload was abandoned and swept.

Three artifact kinds live in the staging dir:

* ``<nonce>.part`` — an upload in flight (or interrupted);
* ``<ref_id>-<sha>.pdf`` — a completed upload awaiting attach, the
  ``ref_id`` in the name binding the staging to the stub its token
  named (an attach for another ref finds nothing);
* ``<nonce>.done`` — the single-use marker: the upload token's nonce
  (``exp.sig``) is tombstoned here — before the body streams — so a
  replayed token is refused (``409``) even after its ``.part`` was
  renamed away.

``sweep_stale_uploads`` — run from the daily ``paper_reconcile`` pass —
removes every artifact older than :data:`STALE_AFTER_S`. Files only,
never ``pdfs`` rows: the row a staged sha eventually owns was written by
ingest and points at the corpus — deleting it on the strength of a stale
staging name would orphan a live paper's PDF.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from precis.errors import Unsupported

#: Env var naming the staging dir for signed uploads. Deploy defaults
#: it to a sibling of the watch inbox on the same share; the value must
#: resolve OUTSIDE ``PRECIS_WATCH_INBOX`` (see module docstring).
UPLOAD_STAGING_ENV = "PRECIS_UPLOAD_STAGING"
#: The superseded staging location — ``<inbox>/.uploads``. Kept in the
#: watcher's ``_MANAGED_DIRS`` as a second guard so a stray in-inbox
#: staging is never ingested.
UPLOADS_DIRNAME = ".uploads"
#: Upload size cap — the ``paper_pull`` download cap, mirrored.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: How long a staged upload / nonce marker is trusted before the sweep
#: drops it — a day to attach an upload is generous; the sha can always
#: be re-uploaded under a fresh token.
STALE_AFTER_S = 24 * 3600
#: ``fetch_sidecar`` ``source`` for an upload-origin drop.
UPLOAD_SIDECAR_SOURCE = "agent_upload"


def uploads_dir() -> Path | None:
    """The ``PRECIS_UPLOAD_STAGING`` dir, or ``None`` when unset — and
    also when the value resolves INSIDE ``PRECIS_WATCH_INBOX`` (staging
    under a watched tree is the misconfig this var exists to remove, so
    it reads as unconfigured rather than silently recreating the
    ``.uploads`` hazard)."""
    staging = os.environ.get(UPLOAD_STAGING_ENV, "").strip()
    if not staging:
        return None
    path = Path(staging)
    inbox = os.environ.get("PRECIS_WATCH_INBOX", "").strip()
    if inbox:
        try:
            path.resolve().relative_to(Path(inbox).resolve())
        except ValueError:
            pass
        else:
            return None
    return path


def require_uploads_dir() -> Path:
    """``uploads_dir()`` or a refusal — staging is a deploy-time
    contract (``deploy/roles/precis_web``), never an inbox fallback."""
    directory = uploads_dir()
    if directory is None:
        raise Unsupported(
            f"PDF upload staging is not configured on this node "
            f"({UPLOAD_STAGING_ENV} unset, or pointing inside the watch "
            "inbox)",
            next=f"set {UPLOAD_STAGING_ENV} to a staging directory "
            "outside every watched tree (deploy default: a sibling of "
            "the watch inbox on the same share)",
        )
    return directory


def nonce(exp: int, sig: str) -> str:
    """Single-use token marker name — ``<exp>.<sig>`` (the token's own
    expiry and signature tail; unique per minted token)."""
    return f"{exp}.{sig}"


def staged_name(ref_id: int, sha: str) -> str:
    """The staged filename ``<ref_id>-<sha>.pdf`` — the ref prefix binds
    the upload to the stub its token named, so an attach for a
    different ref can never adopt it."""
    return f"{ref_id}-{sha.strip().lower()}.pdf"


def staged_upload_path(sha: str, *, ref_id: int) -> str | None:
    """The staging path of an uploaded-but-unattached PDF bound to
    ``ref_id``, else ``None`` — the file's existence IS the record (no
    ``pdfs`` row is consulted), so a swept or never-uploaded sha and a
    sha staged for a different ref both answer ``None``."""
    directory = uploads_dir()
    if directory is None:
        return None
    path = directory / staged_name(ref_id, sha)
    return str(path) if path.is_file() else None


def sweep_stale_uploads(*, now: float | None = None) -> int:
    """Delete staging artifacts older than :data:`STALE_AFTER_S`.

    Covers every shape — in-flight ``.part`` shards, nonce ``.done``
    tombstones and completed ``<ref_id>-<sha>.pdf`` stagings — and
    touches files only, never ``pdfs`` rows (a swept staging's sha may
    already be a live paper's PDF via an earlier attach — the row is
    ingest's record, not the staging's). Returns the number of files
    removed.
    """
    directory = uploads_dir()
    if directory is None or not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - STALE_AFTER_S
    removed = 0
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        entry.unlink(missing_ok=True)
        removed += 1
    return removed


__all__ = [
    "MAX_UPLOAD_BYTES",
    "STALE_AFTER_S",
    "UPLOADS_DIRNAME",
    "UPLOAD_SIDECAR_SOURCE",
    "UPLOAD_STAGING_ENV",
    "nonce",
    "require_uploads_dir",
    "staged_name",
    "staged_upload_path",
    "sweep_stale_uploads",
    "uploads_dir",
]
