"""Fetch sidecar — the acquisition manifest the OA fetcher drops next
to each downloaded PDF so ingest folds into the *right* stub.

Problem it solves
-----------------
The OA fetcher creates a metadata-only stub (DOI/arXiv/S2 + a good
title), downloads the article, and drops the PDF into the shared NFS
inbox. A watcher on *any* of the four cluster nodes then runs Marker,
**re-derives identity from the PDF bytes**, and folds via
:func:`precis.ingest.db_writer.probe_existing`. That fold works only
when the PDF-derived identifiers intersect the stub's. In practice two
common cases break the intersection:

* Marker extracts a **truncated / malformed DOI** (a suffix dropped),
  so the DOI-match misses the stub by a character.
* Marker extracts **no ref-level identifiers at all** (metadata-poor
  scan), so there is nothing to match on.

In both cases ingest mints a *fresh* ref and the well-described stub is
left ``pdf_sha256 IS NULL`` forever — a duplicate split. The historic
mitigation matched the stub by ``cite_key == PDF filename stem``
(:func:`precis.ingest.add._reconcile_orphan_stub`), but the multi-host
inbox race timestamp-renames colliding files, so the stem stops
matching.

The sidecar carries the stub's **authoritative ``ref_id``** (plus its
identifiers, for provenance / manual recovery) in a tiny JSON file
written atomically alongside the PDF. It survives filename munging and
NFS, so ingest can fold deterministically into the stub the fetch was
*for* — keeping the stub's good metadata rather than minting a junk-
metadata twin.

File convention
---------------
For ``<inbox>/continuous83.pdf`` the sidecar is
``<inbox>/continuous83.pdf.precis-fetch.json``. The suffix is **not**
``.pdf``, so :func:`precis.cli.watch._is_pdf` never treats a sidecar as
a droppable file and the backfill ``*.pdf`` glob skips it.

Manual drops (drag-drop, rsync) carry no sidecar; ``read_sidecar``
returns ``None`` and ingest falls back to identity re-derivation exactly
as before — the sidecar is a purely additive hint.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Appended to the PDF's *full* filename (so ``foo.pdf`` →
#: ``foo.pdf.precis-fetch.json``). Chosen to end in ``.json`` — never
#: ``.pdf`` — so the watcher's ``_is_pdf`` / backfill glob ignore it.
SIDECAR_SUFFIX = ".precis-fetch.json"


#: Recognised chunk-source formats. ``"pdf"`` is the legacy default for
#: any sidecar written before the markup-first work (and for PDF-only
#: fetches); the others select the markup producer in the watcher.
SOURCE_FORMATS: frozenset[str] = frozenset(
    {"pdf", "jats", "elsevier_xml", "arxiv_html", "latex"}
)


@dataclass(frozen=True)
class FetchSidecar:
    """Decoded acquisition manifest for one downloaded file."""

    ref_id: int
    #: ``id_kind → id_value`` for the stub (doi/arxiv/s2/cite_key). Carried
    #: for provenance and manual recovery; the deterministic fold keys on
    #: ``ref_id`` alone.
    identifiers: dict[str, str]
    #: The ``fetcher:<provider>`` source that produced the download.
    source: str
    #: Which producer the trigger file feeds. ``"pdf"`` (default) → the
    #: Marker pipeline; ``"jats"``/``"elsevier_xml"``/``"arxiv_html"``/
    #: ``"latex"`` → the markup producer. Back-compat: absent in legacy
    #: PDF-only sidecars, decoded as ``"pdf"``.
    source_format: str = "pdf"
    #: Filename (basename, same dir) of the printable PDF fetched
    #: alongside a markup trigger, else ``None``. Lets the watcher attach
    #: the printable without re-deriving it from the markup.
    companion_pdf: str | None = None


def sidecar_path(pdf: Path) -> Path:
    """Return the sidecar path for a given PDF path."""
    return pdf.with_name(pdf.name + SIDECAR_SUFFIX)


def write_sidecar(
    pdf: Path,
    *,
    ref_id: int,
    identifiers: dict[str, str],
    source: str,
    source_format: str = "pdf",
    companion_pdf: str | None = None,
) -> None:
    """Atomically write the sidecar next to the trigger file ``pdf``.

    ``pdf`` is the *trigger* file the watcher will pick up — the PDF on
    the legacy path, or the markup file (``.xml`` / ``.tex`` / ``.tar.gz``)
    on the markup-first path. ``source_format`` selects the producer and
    ``companion_pdf`` names the printable fetched alongside a markup
    trigger (basename in the same directory), if any.

    Best-effort: a sidecar write failure must never fail the fetch (the
    file is already on disk and ingest degrades to identity re-derivation
    without it). Writes to a temp file in the same directory and
    ``os.replace``\\ s it into place so a watcher polling the inbox never
    reads a half-written manifest.
    """
    if source_format not in SOURCE_FORMATS:
        log.warning(
            "fetch_sidecar: unknown source_format %r for ref_id=%s — "
            "defaulting to 'pdf'",
            source_format,
            ref_id,
        )
        source_format = "pdf"
    target = sidecar_path(pdf)
    payload = {
        "ref_id": int(ref_id),
        "identifiers": {k: v for k, v in identifiers.items() if v},
        "source": source,
        "source_format": source_format,
        "companion_pdf": companion_pdf,
    }
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("fetch_sidecar: failed to write %s: %s", target.name, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_sidecar(pdf: Path) -> FetchSidecar | None:
    """Read + validate the sidecar for ``pdf``; ``None`` if absent or junk.

    A malformed / partially-written sidecar is treated as absent (logged
    at warning) so ingest never crashes on a bad manifest — it just
    falls back to identity re-derivation.
    """
    path = sidecar_path(pdf)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("fetch_sidecar: failed to read %s: %s", path.name, exc)
        return None
    try:
        data: Any = json.loads(raw)
        ref_id = int(data["ref_id"])
        identifiers = {
            str(k): str(v) for k, v in dict(data.get("identifiers", {})).items()
        }
        source = str(data.get("source", ""))
        # Back-compat: legacy PDF-only sidecars have neither key.
        source_format = str(data.get("source_format", "pdf")) or "pdf"
        if source_format not in SOURCE_FORMATS:
            log.warning(
                "fetch_sidecar: unknown source_format %r in %s — treating as 'pdf'",
                source_format,
                path.name,
            )
            source_format = "pdf"
        companion_raw = data.get("companion_pdf")
        companion_pdf = str(companion_raw) if companion_raw else None
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("fetch_sidecar: ignoring malformed %s: %s", path.name, exc)
        return None
    return FetchSidecar(
        ref_id=ref_id,
        identifiers=identifiers,
        source=source,
        source_format=source_format,
        companion_pdf=companion_pdf,
    )


def clear_sidecar(pdf: Path) -> None:
    """Best-effort remove the sidecar for ``pdf`` (idempotent)."""
    try:
        sidecar_path(pdf).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("fetch_sidecar: failed to clear sidecar for %s: %s", pdf.name, exc)


__all__ = [
    "SIDECAR_SUFFIX",
    "SOURCE_FORMATS",
    "FetchSidecar",
    "clear_sidecar",
    "read_sidecar",
    "sidecar_path",
    "write_sidecar",
]
