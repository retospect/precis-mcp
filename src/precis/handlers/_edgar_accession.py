"""Accession-number parsing for the ``edgar`` kind.

An SEC accession number is EDGAR's canonical filing identifier:

    <10-digit filer CIK, zero-padded>-<2-digit year>-<6-digit sequence>

Examples::

    0000320193-23-000106   — Apple Inc. 10-K, FY2023
    0001045810-24-000029   — NVIDIA 10-K, FY2024

Storage form is the **canonical dashed accession** (digits + dashes
only, so there is no case to normalise):

    0000320193-23-000106   → 0000320193-23-000106
    000032019323000106     → 0000320193-23-000106  (dashes re-inserted)

Two forms travel together because the SEC uses both:

* the **dashed** form appears in the submissions API and is what we
  store in ``refs.slug``;
* the **dashless** form (``000032019323000106``) is what the filing
  archive URL path uses
  (``/Archives/edgar/data/<cik>/<accession-dashless>/``).

This mirrors ``_patent_slug.py``: an ``Accession`` dataclass exposing
the parsed parts + both string forms + the on-disk / archive subpath,
plus ``parse_accession`` and ``looks_like_accession``. Anything that
doesn't match raises ``BadInput`` with a recovery hint — same
discipline as ``parse_docdb_id``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from precis.errors import BadInput

# Canonical dashed accession: 10 digits, dash, 2 digits, dash, 6 digits.
_ACCESSION_DASHED_RE = re.compile(r"^(\d{10})-(\d{2})-(\d{6})$")
# Dashless variant carried by the archive URL space: exactly 18 digits.
_ACCESSION_DASHLESS_RE = re.compile(r"^(\d{10})(\d{2})(\d{6})$")


def looks_like_accession(s: str) -> bool:
    """True when ``s`` matches accession shape (dashed or dashless).

    Cheap shape check after whitespace strip — does NOT verify the
    filer CIK exists. Callers route accession-shaped misses to the
    fetch-as-ingest path rather than re-running a keyword search,
    mirroring ``looks_like_docdb``.
    """
    if not s:
        return False
    stripped = s.strip()
    return bool(
        _ACCESSION_DASHED_RE.match(stripped) or _ACCESSION_DASHLESS_RE.match(stripped)
    )


@dataclass(frozen=True, slots=True)
class Accession:
    """Parsed SEC accession number.

    ``cik`` is the filer CIK with leading zeros stripped (the form the
    SEC archive URL path uses, e.g. ``320193``); the zero-padded
    10-digit lead is reconstructed for the canonical string forms.
    """

    cik: str  # filer CIK, leading zeros stripped ('320193')
    year2: str  # 2-digit filing year ('23')
    seq: str  # 6-digit sequence within (cik, year) ('000106')

    @property
    def cik_padded(self) -> str:
        """Zero-padded 10-digit CIK — the accession's leading segment."""
        return self.cik.zfill(10)

    @property
    def cik_int(self) -> int:
        """CIK as an integer — handy for ``ciks=`` query params."""
        return int(self.cik)

    @property
    def dashed(self) -> str:
        """Canonical dashed accession — what we store in ``refs.slug``."""
        return f"{self.cik_padded}-{self.year2}-{self.seq}"

    @property
    def dashless(self) -> str:
        """Dashless accession — the SEC archive URL path segment."""
        return f"{self.cik_padded}{self.year2}{self.seq}"

    @property
    def slug(self) -> str:
        """Alias for :attr:`dashed` — the ``refs.slug`` value."""
        return self.dashed

    @property
    def archive_subpath(self) -> str:
        """``<cik>/<accession-dashless>`` — the archive URL suffix.

        Full archive base is
        ``https://www.sec.gov/Archives/edgar/data/<cik>/<dashless>/``.
        The CIK segment is the leading-zero-stripped form, matching
        both the SEC URL and the on-disk raw cache layout.
        """
        return f"{self.cik}/{self.dashless}"

    @property
    def disk_subpath(self) -> tuple[str, str]:
        """The ``(cik, dashless)`` tuple used to derive the on-disk path
        under ``$PRECIS_EDGAR_RAW_ROOT``."""
        return (self.cik, self.dashless)


def parse_accession(raw: str) -> Accession:
    """Normalise a user-supplied accession number and parse it.

    Steps:
        1. Strip leading/trailing whitespace.
        2. Match against the dashed regex; failing that, the dashless
           regex (and re-insert dashes).

    Raises:
        BadInput: ``raw`` doesn't match either accession shape after
            stripping whitespace.
    """
    if not isinstance(raw, str) or not raw:
        raise BadInput(
            f"invalid accession number: {raw!r}",
            next="accession must look like '0000320193-23-000106'",
        )

    stripped = raw.strip()

    m = _ACCESSION_DASHED_RE.match(stripped)
    if m is None:
        m = _ACCESSION_DASHLESS_RE.match(stripped)
    if m is None:
        raise BadInput(
            f"{raw!r} is not an SEC accession number",
            next=(
                "format: <10-digit-cik>-<2-digit-year>-<6-digit-seq> "
                "(e.g. '0000320193-23-000106'); the dashless "
                "'000032019323000106' is also accepted"
            ),
        )

    cik_padded, year2, seq = m.groups()
    # Strip the CIK's leading zeros for the canonical stored form; the
    # padded lead is reconstructed via ``cik_padded``. int() drops the
    # zeros without stringly-typed lstrip edge cases (all-zero CIK).
    cik = str(int(cik_padded))

    return Accession(cik=cik, year2=year2, seq=seq)


__all__ = ["Accession", "looks_like_accession", "parse_accession"]
