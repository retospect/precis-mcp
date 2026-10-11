"""DOCDB id parsing for the ``patent`` kind.

A DOCDB id is the EPO's canonical patent publication identifier:

    <country><number><kind>[<seq>]

Examples:
    EP1234567B1     — granted European patent
    US20240012345A1 — US application publication
    WO2023123456A1  — PCT application
    EP1000000A2     — EP application (kind A2 = republished)

Storage form is **lowercased and whitespace-stripped**:

    EP1234567B1     → ep1234567b1
    EP 1234567 B1   → ep1234567b1

Dots are deliberately *not* stripped — DOIs and arXiv ids carry
semantic dots and a normaliser that silently reshapes them would
set a bad cross-kind precedent. The dotted DOCDB form
``EP.1234567.B1`` is a third-party convenience EPO doesn't actually
emit; we let it raise ``BadInput`` with a recovery hint.

The ``DocDbId`` dataclass exposes the parsed parts (``country``,
``number``, ``kind_code``, ``seq``) for the handler to use when
building the on-disk path under ``$PRECIS_PATENT_RAW_ROOT`` and the
OPS API call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from precis.errors import BadInput

# Match the canonical lowercased form. The kind code is a single
# letter optionally followed by one digit (A, A1, A2, A3, B, B1, B2,
# T, T1, U, U1, P, P1, S, S1, …). Country code is always two letters.
_DOCDB_RE = re.compile(r"^([a-z]{2})(\d+)([a-z])(\d?)$")


def looks_like_docdb(s: str) -> bool:
    """True when ``s`` matches DOCDB shape after lowercasing + dot/space strip.

    Cheap shape check — does NOT validate the country code against
    the authority list. Callers route DOCDB-shaped misses to the
    OPS-fetch + finding pipeline rather than re-querying with keyword
    variants; the authority check happens later, on the fetch path.
    """
    if not s:
        return False
    stripped = re.sub(r"[\s.]", "", s.lower())
    return _DOCDB_RE.match(stripped) is not None


# Closed list of country / authority codes that EPO OPS recognises.
# Source: WIPO ST.3 / EPO DOCDB. Kept here because OPS will reject
# anything else with a 400, and we'd rather catch typos at the agent
# boundary with a recovery hint. List is conservative — if a real
# authority is missing, the next can be added without a migration.
_COUNTRY_CODES: frozenset[str] = frozenset(
    {
        # G7 + EU + WIPO
        "ep",
        "us",
        "wo",
        "gb",
        "de",
        "fr",
        "ch",
        "at",
        "be",
        "nl",
        "lu",
        "se",
        "no",
        "dk",
        "fi",
        "is",
        "ie",
        "it",
        "es",
        "pt",
        "gr",
        "tr",
        # Eastern Europe
        "pl",
        "cz",
        "sk",
        "hu",
        "ro",
        "bg",
        "ru",
        "ua",
        "by",
        "lt",
        "lv",
        "ee",
        "si",
        "hr",
        "rs",
        "ba",
        "mk",
        "al",
        "md",
        # Asia-Pacific
        "jp",
        "cn",
        "kr",
        "tw",
        "hk",
        "in",
        "au",
        "nz",
        "sg",
        "my",
        "th",
        "vn",
        "id",
        "ph",
        "il",
        "ae",
        "sa",
        # Americas
        "ca",
        "mx",
        "br",
        "ar",
        "cl",
        "co",
        "pe",
        "ve",
        # Africa
        "za",
        "eg",
        "ma",
        "ng",
        "ke",
        # Eurasian patent office
        "ea",
    }
)


@dataclass(frozen=True, slots=True)
class DocDbId:
    """Parsed DOCDB publication identifier.

    The canonical lowercased form is reconstructable as
    ``f"{country}{number}{kind_code}{seq}"``. The on-disk path under
    ``$PRECIS_PATENT_RAW_ROOT`` is
    ``<country>/<number>/<kind_code><seq>/`` — same structure as the
    OPS URL space, navigable by hand.
    """

    country: str  # 2-letter ISO authority, lowercased ('ep', 'us', 'wo')
    number: str  # publication number digits, no padding ('1234567')
    kind_code: str  # single letter ('a', 'b', 't', 'u')
    seq: str  # optional sequence digit, possibly empty ('1', '2', '')

    @property
    def slug(self) -> str:
        """Canonical lowercased slug — what we store in ``refs.slug``."""
        return f"{self.country}{self.number}{self.kind_code}{self.seq}"

    @property
    def kind_full(self) -> str:
        """Kind code + optional sequence as a single token ('b1', 'a2', 'a')."""
        return f"{self.kind_code}{self.seq}"

    @property
    def display(self) -> str:
        """Uppercase form for human-facing output ('EP1234567B1')."""
        return self.slug.upper()

    @property
    def disk_subpath(self) -> tuple[str, str, str]:
        """The ``(country, number, kind_full)`` tuple used to derive the
        on-disk cache path under ``$PRECIS_PATENT_RAW_ROOT``."""
        return (self.country, self.number, self.kind_full)


def parse_docdb_id(raw: str) -> DocDbId:
    """Normalise a user-supplied patent id and parse it.

    Steps:
        1. Strip whitespace (leading/trailing AND internal).
        2. Lowercase.
        3. Match against the canonical regex.

    Dots are *not* stripped — see module docstring. The dotted form
    ``EP.1234567.B1`` raises ``BadInput`` with a recovery hint
    suggesting the caller drop the dots themselves.

    Raises:
        BadInput: id doesn't match ``<cc><digits><letter>[<digit>]``
            after normalisation, or the country code isn't a known
            DOCDB authority.
    """
    if not isinstance(raw, str) or not raw:
        raise BadInput(
            f"invalid patent id: {raw!r}",
            next="patent id must be a DOCDB string like 'EP1234567B1'",
        )

    # Strip ALL whitespace, including internal — that's the one
    # transformation we promise. Leaves dots, hyphens, slashes alone.
    normalised = re.sub(r"\s+", "", raw).lower()

    if not normalised:
        raise BadInput(
            f"empty patent id (after stripping whitespace): {raw!r}",
            next="patent id must be a DOCDB string like 'EP1234567B1'",
        )

    # Dotted convenience form ('ep.1234567.b1') gets a specific hint —
    # this catches the most common third-party variant explicitly so
    # the agent sees how to fix it.
    if "." in normalised:
        no_dots = normalised.replace(".", "")
        if _DOCDB_RE.match(no_dots):
            raise BadInput(
                f"dotted DOCDB form is not supported: {raw!r}",
                next=f"try {no_dots!r} (drop the dots)",
            )

    m = _DOCDB_RE.match(normalised)
    if m is None:
        raise BadInput(
            f"{raw!r} is not a DOCDB id",
            next=(
                "format: <cc><digits><letter>[<digit>] "
                "(e.g. 'EP1234567B1', 'US20240012345A1', 'WO2023123456A1')"
            ),
        )

    country, number, kind_letter, seq = m.groups()

    if country not in _COUNTRY_CODES:
        raise BadInput(
            f"unknown patent authority country code: {country!r}",
            next="see WIPO ST.3 for the country code list (e.g. 'EP', 'US', 'WO')",
        )

    return DocDbId(
        country=country,
        number=number,
        kind_code=kind_letter,
        seq=seq,
    )


__all__ = ["DocDbId", "parse_docdb_id"]
