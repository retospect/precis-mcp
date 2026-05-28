"""PDF metadata write-back for re-ingest correctness.

After a fresh ingest resolves a canonical DOI / title / authors, this
module patches those values into the PDF's Info dict so re-ingesting
the same file from a clean database still recovers the right
identifiers via :func:`precis.ingest.pdf_metadata._read_existing_pdf_metadata`.

Why this is safe (reversing the B4b removal):

* The v2 ``ref_identifiers`` table accepts N rows per ref. Both the
  pre-patch and post-patch ``pdf_sha256`` get stored as aliases for
  the same ``ref_id``, so a re-ingest of *either* byte sequence
  short-circuits in the fast path. The hash-drift problem that
  motivated removing write-back in B4b is neutralised by the alias
  model. See ADR 0014.

* Save uses ``incremental=True`` so the existing PDF bytes are not
  rewritten — pymupdf appends an update section. This keeps risk
  low on weird PDFs (the original content stream is byte-identical
  in the saved file).

Operator off-switch: ``PRECIS_PATCH_PDFS=0`` (or ``false`` / ``no`` /
``off``) disables write-back. Default is on.

The module is pure: no DB knowledge. The caller (``precis_add``)
extracts info from the resolved ``PaperToWrite`` and threads the
returned hashes into :class:`precis.ingest.db_writer.PaperToWrite`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PatchInfo:
    """What we want the patched PDF's Info dict to carry.

    Fields are all optional — missing values just skip that key. The
    caller (``precis_add``) assembles this from the resolved paper
    metadata after the lookup cascade picks a canonical DOI.
    """

    title: str | None = None
    authors: list[str] | None = None  # surname strings, already formatted
    doi: str | None = None
    arxiv_id: str | None = None


@dataclass(frozen=True)
class PatchOutcome:
    """Result of :func:`patch_pdf_metadata`.

    * ``pre_hash`` is always populated (sha256 of the file as it
      existed before we touched it). The caller stores this as a
      ``pdf_sha256`` alias regardless of whether a write happened.

    * ``post_hash`` and ``post_size`` are populated iff a write
      actually happened. The caller swaps these in as the canonical
      ``pdf_sha256`` / ``pdf_size_bytes`` on the ``PaperToWrite``.

    * ``skipped_reason`` is one of ``"disabled"``, ``"encrypted"``,
      ``"noop"``, ``"error"``, or ``None``. ``None`` means the patch
      ran and produced a new hash. ``"error"`` is logged at WARNING
      level; everything else is INFO.
    """

    pre_hash: str
    post_hash: str | None
    post_size: int | None
    skipped_reason: str | None


def patch_pdf_metadata(
    path: Path,
    info: PatchInfo,
    *,
    pre_hash: str | None = None,
) -> PatchOutcome:
    """Patch ``path``'s Info dict with ``info`` and return both hashes.

    Idempotent: a second call with the same target ``info`` notices
    the fields already match and returns ``skipped_reason="noop"``
    without rewriting. This means re-ingesting an already-patched
    PDF produces no hash drift and adds no new alias rows.

    Caller-supplied ``pre_hash`` avoids a re-hash if the value was
    already computed upstream (e.g. by ``_probe_pdf_sha256``). Pass
    ``None`` to recompute here.
    """
    # 1. Pre-hash (always computed, used in every return path).
    if pre_hash is None:
        pre_hash = _sha256_file(path)

    # 2. Off-switch.
    if not _patching_enabled():
        return PatchOutcome(pre_hash, None, None, "disabled")

    # Import lazily — pymupdf pulls a C extension; keep
    # `import precis.ingest.pdf_writer` cheap for callers that
    # only want the dataclasses (e.g. tests).
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError:
        log.warning("pymupdf (fitz) not available; PDF write-back disabled")
        return PatchOutcome(pre_hash, None, None, "error")

    target = _info_dict_from_patch(info)
    if not target:
        # Nothing to write — info was empty. Treat as no-op.
        return PatchOutcome(pre_hash, None, None, "noop")

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        log.warning("pdf_writer: cannot open %s: %s", path, exc)
        return PatchOutcome(pre_hash, None, None, "error")

    try:
        if doc.is_encrypted:
            return PatchOutcome(pre_hash, None, None, "encrypted")

        current = doc.metadata or {}
        if _already_matches(current, target):
            return PatchOutcome(pre_hash, None, None, "noop")

        # Merge: keep existing fields we don't intend to set
        # (Producer, Creator, CreationDate, etc.), overlay the
        # fields from ``target``.
        merged = dict(current)
        merged.update(target)
        doc.set_metadata(merged)

        try:
            # Incremental save: appends an update section instead of
            # rewriting the entire file. The original content stream
            # stays byte-identical, which is the lowest-risk write
            # mode for academic PDFs of unknown provenance.
            doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        except Exception as exc:
            log.warning("pdf_writer: save failed for %s: %s", path, exc)
            return PatchOutcome(pre_hash, None, None, "error")
    finally:
        doc.close()

    # 3. Post-hash + size (re-stat the now-modified file).
    post_hash = _sha256_file(path)
    post_size = path.stat().st_size

    if post_hash == pre_hash:
        # pymupdf's incremental save was a true no-op at the byte
        # level (rare but possible — e.g. metadata was logically
        # equal but ``_already_matches`` missed it due to a key-case
        # quirk). Treat as no-op: don't insert a duplicate alias.
        return PatchOutcome(pre_hash, None, None, "noop")

    log.info(
        "pdf_writer: patched %s (%s → %s)",
        path.name,
        pre_hash[:12],
        post_hash[:12],
    )
    return PatchOutcome(pre_hash, post_hash, post_size, None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _patching_enabled() -> bool:
    """Honour the ``PRECIS_PATCH_PDFS`` off-switch.

    Default ON; set to one of ``0`` / ``false`` / ``no`` / ``off`` /
    empty to disable. Anything else (including ``1`` / ``true`` /
    unset) keeps write-back on.
    """
    val = os.environ.get("PRECIS_PATCH_PDFS", "1").strip().lower()
    return val not in {"0", "false", "no", "off", ""}


def _sha256_file(path: Path) -> str:
    """Stream-hash a file. ~1 ms/MB on modern hardware."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _info_dict_from_patch(info: PatchInfo) -> dict[str, str]:
    """Translate :class:`PatchInfo` to pymupdf's ``set_metadata`` keys.

    pymupdf accepts the standard PDF Info dict names lowercased.
    DOI and arXiv go into ``keywords`` as a machine-readable list
    (``"doi:10.x/y, arxiv:1234.56789"``) so the existing
    ``_read_existing_pdf_metadata`` cascade picks them up via the
    ``-Keywords`` field. We don't write XMP yet — keeping the
    surface narrow to the standard Info dict avoids a whole class
    of structural-PDF edge cases.
    """
    out: dict[str, str] = {}
    if info.title:
        out["title"] = info.title
    if info.authors:
        out["author"] = ", ".join(info.authors)

    kw_parts: list[str] = []
    if info.doi:
        kw_parts.append(f"doi:{info.doi}")
        # Also stash in subject as a human-readable hint — some PDF
        # readers (Preview, Acrobat) surface Subject more prominently
        # than Keywords.
        out["subject"] = f"DOI: {info.doi}"
    if info.arxiv_id:
        kw_parts.append(f"arxiv:{info.arxiv_id}")
    if kw_parts:
        out["keywords"] = ", ".join(kw_parts)

    return out


def _already_matches(current: dict[str, Any], target: dict[str, str]) -> bool:
    """Return ``True`` if every key in ``target`` is already equal in
    ``current``.

    Comparison is case-sensitive on values (titles can legitimately
    differ on capitalization across publishers) but normalises
    whitespace. Keys are exact matches against pymupdf's lowercase
    Info-dict names.
    """
    for key, want in target.items():
        have = (current.get(key) or "").strip()
        if have != want.strip():
            return False
    return True


__all__ = [
    "PatchInfo",
    "PatchOutcome",
    "patch_pdf_metadata",
]
