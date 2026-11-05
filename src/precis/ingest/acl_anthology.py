"""Deterministic ACL Anthology DOI → PDF connector.

The ``10.18653/v1/`` suffix is an Anthology ID, so acquisition needs no
metadata request or local corpus. Only modern and legacy ID shapes are
accepted: a DOI must never add path segments, queries or fragments to the
PDF URL. Older ``10.3115`` DOIs need a separate metadata lookup and return
``None`` here. Downloading and host pacing belong to the fetch worker.
"""

from __future__ import annotations

import re

_DOI_PREFIX = "10.18653/v1/"
_MODERN_ID = re.compile(r"[0-9]{4}\.[a-z0-9]+(?:-[a-z0-9]+)*\.[0-9]+", re.ASCII)
_LEGACY_ID = re.compile(r"[A-Z][0-9]{2}-[0-9]{4}", re.ASCII)


def _canonical_id(anthology_id: str) -> str | None:
    if _MODERN_ID.fullmatch(anthology_id.lower()):
        return anthology_id.lower()
    if _LEGACY_ID.fullmatch(anthology_id.upper()):
        return anthology_id.upper()
    return None


def anthology_id_from_doi(doi: str) -> str | None:
    """Extract a validated ID; DOI prefix and ID matching ignore case."""
    if not doi.lower().startswith(_DOI_PREFIX):
        return None
    return _canonical_id(doi[len(_DOI_PREFIX) :])


def pdf_url(anthology_id: str) -> str:
    """Build the canonical PDF URL, refusing malformed IDs."""
    canonical = _canonical_id(anthology_id)
    if canonical is None:
        raise ValueError("invalid ACL Anthology ID")
    return f"https://aclanthology.org/{canonical}.pdf"
