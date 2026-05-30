"""Retraction Watch CSV parser.

The dataset Crossref distributes (see
gitlab.com/crossref/retraction-watch-data) is a 20-column CSV with
``;``-separated multi-value fields. This module parses it into a
shape the sync job can upsert into ``provenance_rw_cache``.

Stdlib-only — no pandas dep for this. The whole file is ~40 MB; we
stream-parse and yield rows so the sync job never holds the full
dataset in memory.

Schema reference (from the README, May 2026):

    Record ID, Title, Subject, Institution, Journal, Publisher,
    Country, Author, URLS, ArticleType, RetractionDate,
    RetractionDOI, RetractionPubMedID, OriginalPaperDate,
    OriginalPaperDOI, OriginalPaperPubMedID, RetractionNature,
    Reason, Paywalled, Notes

We tolerate column-name variants (case, whitespace, underscores)
because Crossref hasn't promised header stability across releases.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

log = logging.getLogger(__name__)


# Map normalised header keys → our canonical field names. Normalisation
# lowercases and strips non-alphanumeric, so ``"Original Paper DOI"``,
# ``"OriginalPaperDOI"``, ``"original_paper_doi"`` all resolve to the
# same key. Tolerance against header drift; logged when an unknown
# header shows up.
_HEADER_MAP: dict[str, str] = {
    "recordid":                "record_id",
    "title":                   "title",
    "subject":                 "subject",
    "institution":             "institution",
    "journal":                 "journal",
    "publisher":               "publisher",
    "country":                 "country",
    "author":                  "author",
    "urls":                    "urls",
    "articletype":             "article_type",
    "retractiondate":          "retraction_date",
    "retractiondoi":           "retraction_doi",
    "retractionpubmedid":      "retraction_pubmed_id",
    "originalpaperdate":       "original_paper_date",
    "originalpaperdoi":        "original_paper_doi",
    "originalpaperpubmedid":   "original_paper_pubmed_id",
    "retractionnature":        "retraction_nature",
    "reason":                  "reason",
    "paywalled":               "paywalled",
    "notes":                   "notes",
}


def _normalise_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


# RW dates appear in MM/DD/YYYY (US-style) per the dataset's history.
# We try ISO first (in case the format shifts) then fall back to a
# small set of regional patterns. Returns ``None`` for unparseable
# strings rather than raising — the parser shouldn't kill the whole
# sync because one row has a typo'd date.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",       # ISO
    "%m/%d/%Y",       # RW canonical (US)
    "%d/%m/%Y",       # DD/MM/YYYY fallback
    "%m/%d/%y",       # 2-digit year (legacy rows)
    "%Y/%m/%d",       # alt ISO-ish
)


def _parse_date(raw: str) -> date | None:
    """Parse a RW date string, or return None when unparseable."""
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# Reasons in the CSV are ``;``-separated, each typically prefixed with
# ``+`` (e.g. ``+Falsification/Fabrication of Data``). Some legacy
# rows use commas inside reasons; we keep the raw form and just split
# on ``;``.
def _split_multivalue(raw: str) -> list[str]:
    """Split ``;``-separated multi-value field, dropping empties."""
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.split(";") if chunk.strip()]


# DOIs in the CSV may include ``https://doi.org/`` prefixes or
# whitespace. Canonicalise to lowercase, no-prefix form — same rule
# as the rest of the codebase (see store/_identifiers_ops.py).
def _canonicalise_doi(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    return s.lower()


@dataclass(frozen=True, slots=True)
class RWRow:
    """One row from the RW CSV in our canonical shape.

    Mirrors the columns we materialise into ``provenance_rw_cache``
    plus the raw row dict. ``record_id`` is the RW dataset's own
    primary key, which we re-use as the cache PK so re-syncs are
    idempotent.
    """

    record_id: int
    paper_doi: str
    notice_doi: str  # empty string when the dataset has no notice DOI
    notice_nature: str
    reasons: list[str]
    retraction_date: date | None
    paper_title: str | None
    journal: str | None
    raw: dict[str, str]


def parse_rw_rows(lines: Iterable[str]) -> Iterator[RWRow]:
    """Stream-parse RW CSV lines, yielding ``RWRow`` objects.

    Rows missing the ``record_id`` or ``original_paper_doi`` field
    are skipped with a warning — they can't participate in the join
    so they're dead weight in the cache.

    Tolerates header variants via the normalisation in
    ``_HEADER_MAP``. Logs and drops malformed rows; never raises
    on a single bad row.
    """
    reader = csv.reader(lines)
    try:
        header_row = next(reader)
    except StopIteration:
        log.warning("RW CSV: empty input — no header row")
        return

    # Build column index map. Unknown headers are kept in case the
    # caller wants them in ``.raw``; known headers map to canonical
    # field names.
    col_to_field: dict[int, str] = {}
    unknown_headers: list[tuple[int, str]] = []
    for i, h in enumerate(header_row):
        norm = _normalise_header(h)
        canonical = _HEADER_MAP.get(norm)
        if canonical:
            col_to_field[i] = canonical
        else:
            unknown_headers.append((i, h))
    if unknown_headers:
        log.warning(
            "RW CSV: %d unknown header(s) — %s",
            len(unknown_headers),
            ", ".join(f"{h!r}" for _, h in unknown_headers[:5]),
        )

    skipped = 0
    seen = 0
    for row in reader:
        if not row or all(not c.strip() for c in row):
            continue
        # Build a dict of canonical fields + carry the raw row for
        # ``provenance_rw_cache.raw`` JSONB storage.
        fields: dict[str, str] = {}
        raw: dict[str, str] = {}
        for i, cell in enumerate(row):
            if i in col_to_field:
                fields[col_to_field[i]] = cell
            if i < len(header_row):
                raw[header_row[i]] = cell

        record_id_raw = fields.get("record_id", "").strip()
        paper_doi_raw = fields.get("original_paper_doi", "")
        if not record_id_raw or not paper_doi_raw.strip():
            skipped += 1
            continue
        try:
            record_id = int(record_id_raw)
        except ValueError:
            skipped += 1
            continue

        yield RWRow(
            record_id=record_id,
            paper_doi=_canonicalise_doi(paper_doi_raw),
            notice_doi=_canonicalise_doi(fields.get("retraction_doi", "")),
            notice_nature=(fields.get("retraction_nature", "") or "").strip(),
            reasons=_split_multivalue(fields.get("reason", "")),
            retraction_date=_parse_date(fields.get("retraction_date", "")),
            paper_title=(fields.get("title") or None) or None,
            journal=(fields.get("journal") or None) or None,
            raw=raw,
        )
        seen += 1

    if skipped:
        log.info("RW CSV: parsed %d rows; skipped %d (missing record_id or DOI)", seen, skipped)


__all__ = ["RWRow", "parse_rw_rows"]
