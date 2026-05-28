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

        if _has_signature(doc):
            # Digitally-signed PDF — incremental save *usually*
            # preserves the signature byte range, but "usually" isn't
            # "always". Strict readers re-validate the trailer and
            # warn on any append. Skip rather than risk it. See
            # ADR 0014.
            return PatchOutcome(pre_hash, None, None, "signed")

        current = doc.metadata or {}
        current_xmp = doc.get_xml_metadata() or ""
        target_xmp = _build_xmp_packet(info)

        needs_info = not _already_matches(current, target)
        needs_xmp = target_xmp is not None and not _xmp_already_carries(
            current_xmp, info
        )

        if not needs_info and not needs_xmp:
            return PatchOutcome(pre_hash, None, None, "noop")

        if needs_info:
            # Merge: keep existing fields we don't intend to set
            # (Producer, Creator, CreationDate, etc.), overlay the
            # fields from ``target``.
            merged = dict(current)
            merged.update(target)
            doc.set_metadata(merged)

        if needs_xmp and target_xmp:
            # XMP is the publisher-canonical home for dc:identifier
            # (DOI). Writing here means an exiftool-driven re-ingest
            # finds the DOI via -Identifier even if the Keywords
            # field is stripped downstream.
            doc.set_xml_metadata(target_xmp)

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


def _has_signature(doc: Any) -> bool:
    """True iff the PDF contains at least one digital signature widget.

    Iterates pages only when ``is_form_pdf`` is true, so unsigned PDFs
    (the overwhelming majority of academic corpora) pay no cost
    beyond the AcroForm catalog lookup. Bails on the first signature
    found.

    Note: this catches the standard PDF digital-signature feature
    (``/FT /Sig`` widgets). PDFs signed with external tooling that
    doesn't register a widget (rare) slip through. If write-back
    breaks such a PDF, the failure surfaces at the reader, not here.
    """
    if not doc.is_form_pdf:
        return False
    for page in doc:
        for widget in page.widgets() or ():
            if widget.field_type_string == "Signature":
                return True
    return False


# XMP packet template — RDF/XML wrapper for the dc + prism namespaces
# we populate. ``{inner}`` is replaced with one or more <dc:title> /
# <dc:creator> / <dc:identifier> / <prism:doi> elements.
_XMP_TEMPLATE = (
    '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '    <rdf:Description rdf:about=""\n'
    '        xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
    '        xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">\n'
    "{inner}\n"
    "    </rdf:Description>\n"
    "  </rdf:RDF>\n"
    "</x:xmpmeta>\n"
    '<?xpacket end="w"?>'
)


def _build_xmp_packet(info: PatchInfo) -> str | None:
    """Build a minimal RDF/XMP packet for the given metadata.

    Returns ``None`` if every field is missing — caller treats that
    as "nothing to write to XMP".

    DOI is emitted twice: once as ``dc:identifier`` prefixed with
    ``doi:`` (Dublin Core canonical) and once as ``prism:doi``
    (PRISM scientific-publishing canonical). Both fields are read
    by exiftool's ``-Identifier`` / ``-DOI`` flags and by most
    reference managers.
    """
    if not (info.title or info.authors or info.doi or info.arxiv_id):
        return None

    parts: list[str] = []
    if info.title:
        parts.append(
            "        <dc:title>\n"
            "          <rdf:Alt>\n"
            f'            <rdf:li xml:lang="x-default">{_xml_escape(info.title)}</rdf:li>\n'
            "          </rdf:Alt>\n"
            "        </dc:title>"
        )
    if info.authors:
        author_lis = "\n".join(
            f"            <rdf:li>{_xml_escape(a)}</rdf:li>" for a in info.authors
        )
        parts.append(
            "        <dc:creator>\n"
            "          <rdf:Seq>\n"
            f"{author_lis}\n"
            "          </rdf:Seq>\n"
            "        </dc:creator>"
        )
    if info.doi:
        parts.append(
            f"        <dc:identifier>doi:{_xml_escape(info.doi)}</dc:identifier>"
        )
        parts.append(f"        <prism:doi>{_xml_escape(info.doi)}</prism:doi>")
    if info.arxiv_id:
        parts.append(
            "        <prism:url>"
            f"https://arxiv.org/abs/{_xml_escape(info.arxiv_id)}"
            "</prism:url>"
        )

    return _XMP_TEMPLATE.format(inner="\n".join(parts))


def _xmp_already_carries(current_xmp: str, info: PatchInfo) -> bool:
    """Heuristic: does ``current_xmp`` already declare every field we'd
    write?

    We don't fully parse the RDF — substring checks on the
    canonical forms (escaped title text, escaped author names,
    ``<dc:identifier>doi:...</dc:identifier>``, the arXiv URL) catch
    our own prior writes (the common idempotency case). Publisher-
    set XMP that uses different element forms (e.g. ``prism:doi``
    only, no ``dc:identifier``) returns False and triggers a
    re-write — harmless, because the final-hash check at the bottom
    of :func:`patch_pdf_metadata` recognises the bytes-equivalent
    no-op and skips the alias insertion on the second pass.

    Returns True iff ``info`` is empty (nothing to write) or every
    populated field is already substring-present in the XMP.
    """
    if not (info.title or info.authors or info.doi or info.arxiv_id):
        return True
    if info.title and _xml_escape(info.title) not in current_xmp:
        return False
    if info.authors:
        for a in info.authors:
            if _xml_escape(a) not in current_xmp:
                return False
    if info.doi:
        needle = f"<dc:identifier>doi:{info.doi}</dc:identifier>"
        if needle not in current_xmp:
            return False
    if info.arxiv_id and f"https://arxiv.org/abs/{info.arxiv_id}" not in current_xmp:
        return False
    return True


def _xml_escape(s: str) -> str:
    """Minimal XML escape for text-node content. Handles the four
    characters that change meaning inside ``<rdf:li>`` etc.
    """
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = [
    "PatchInfo",
    "PatchOutcome",
    "patch_pdf_metadata",
]
