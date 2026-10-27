"""Identifier derivation for precis-mcp v2.

Pure functions only — no DB, no I/O, no model loads. Everything that
mints a ``paper_id``, ``pub_id``, ``cite_key``, ``pdf_sha256``,
``content_hash``, or ``node_id`` lives here.

Read first:

- ``identity`` (git-only) — algorithm choices, edge cases, test plan
- ``docs/decisions/0002-pub-id-and-toon.md`` — pub_id formula (locked)
- ``docs/decisions/0006-tri-identifier-scheme.md`` — cite_key (locked)
- ``docs/decisions/0008-drop-slug-identifier-normalisation.md`` —
  identifiers normalised into ``ref_identifiers``; ``slug`` retired
- ``storage-v2`` (git-only) §"Identity & naming"

Stability promise: the algorithms in this module are pinned. ``pub_id``
values appear in LaTeX cites, MCP responses, and external references;
changing the formula would silently invalidate every existing handle.
Add a new function or a new ADR + module rather than mutating these.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

# Surname truncation cap (matches ``precis.utils.slug``). Long enough
# to keep practically every academic surname intact, short enough to
# bound cite_key length.
_SURNAME_MAX = 30

# 27th paper by the same first author in the same year ⇒ overflow.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _cite_suffix(n: int) -> str:
    """The ``n``-th collision suffix (1-indexed), bijective base-26.

    ``a``, ``b``, … ``z`` (n=1..26), then ``aa``, ``ab``, … ``az``,
    ``ba`` … (n=27..), then ``aaa`` … — a spreadsheet-column roll, so the
    suffix space is unbounded and a busy surname+year never overflows.
    Single letters keep their old values (n≤26 → same as the legacy
    ``_LETTERS`` scan), so existing cite_keys are unchanged.
    """
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = _LETTERS[r] + s
    return s


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CiteKeyOverflow(ValueError):
    """Defensive guard for :func:`make_cite_key`.

    The suffix space is unbounded (``a``…``z``, ``aa``…``zz``, ``aaa``…),
    so this is unreachable for a finite ``taken`` set — kept only as a
    belt-and-suspenders backstop.

    Attributes:
        base: The ``<surname><yy>`` prefix that overflowed.
        taken: The set of taken keys passed in.
    """

    def __init__(self, base: str, taken: set[str]) -> None:
        super().__init__(
            f"cite_key {base!r}: no free suffix found within {len(taken)} tries "
            "(should be unreachable — suffix space is unbounded a…z, aa…zz)"
        )
        self.base = base
        self.taken = taken


# ---------------------------------------------------------------------------
# Private helpers (string folding + first-author extraction)
#
# The first-author logic mirrors ``precis.utils.slug._first_author`` but
# is deliberately duplicated here. Identity is foundational; pinning the
# algorithm in this module insulates ``cite_key`` values from any future
# refinement to slug minting.
# ---------------------------------------------------------------------------


# Letters that are NOT NFKD-decomposable but have well-established ASCII
# replacements in citation keys / slugs. NFKD alone would silently drop
# these, producing folds like ``nrskov`` (Nørskov) rather than ``norskov``.
# Canonical home for this table — was duplicated in
# ``precis.ingest.literature`` before consolidation.
_ASCII_FALLBACKS = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ß": "ss",
        "ł": "l",
        "Ł": "L",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
    }
)


def _ascii_fold(text: str, *, fallbacks: bool = False) -> str:
    """NFKD-decompose, drop combining marks, drop non-ASCII bytes.

    ``fallbacks=True`` first maps the Scandinavian / Eastern-European
    letters NFKD does not decompose (``ø``, ``æ``, ``ß``, ``ł`` etc.) via
    :data:`_ASCII_FALLBACKS`, so e.g. "Nørskov" folds to ``norskov``
    instead of ``nrskov``. Default is ``False`` and MUST stay that way
    for :func:`_surname_from_string` (feeds :func:`make_cite_key`, a
    pinned persistent identifier — see module stability promise) and for
    ``precis.utils.slug``'s callers (persistent slugs): changing their
    fold would make future mints diverge from already-stored identifiers
    for the same input. Only new callers should opt into
    ``fallbacks=True``.
    """
    if fallbacks:
        text = text.translate(_ASCII_FALLBACKS)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _key_letters(text: str) -> str:
    """Fold a surname string to cite_key letters (``[:_SURNAME_MAX]``)."""
    folded = _ascii_fold(text.lower())
    # "a.clark" → "clark"; "a.b.clark" → "clark". Detect a leading run
    # of one-letter dotted segments (initials) and drop them.
    if "." in folded:
        segments = folded.split(".")
        if (
            len(segments) >= 2
            and segments[-1]
            and all(len(s) == 1 and s.isalpha() for s in segments[:-1])
        ):
            folded = segments[-1]
    return re.sub(r"[^a-z]", "", folded)[:_SURNAME_MAX]


#: A whitespace-delimited token that is initials, not a surname —
#: ``"A"``, ``"A."``, ``"A.V."``, ``"A.V"``. The "Surname I" byline form
#: (``"Mula A"``) trails them; the last token is never the surname
#: (gr521515 dry-run: ``a26avk``). A bare multi-letter token never
#: matches — ``"Sa"``/``"Li"``/``"Wu"`` are surnames, not initials.
_INITIALS_TOKEN_RE = re.compile(r"(?:[A-Za-z]\.)*[A-Za-z]\.?")

#: PubMed-style unpunctuated initials: a run of 1-3 ASCII capitals
#: (``"AV"``, ``"JK"``, ``"ABC"``). Counts as initials only after a
#: head token carrying a lowercase letter — an all-caps head can't be
#: told surname-from-initials, so ``"SMITH AB"`` keeps ``ab`` while
#: ``"Mula AV"`` peels to ``mula``.
_CAPS_INITIALS_TOKEN_RE = re.compile(r"[A-Z]{1,3}")

#: Generational suffixes that trail a surname, compared case-folded on
#: the token with any trailing dot stripped (``"Jr"``, ``"Jr."``,
#: ``"Sr"``, ``"II"``, ``"III"``, ``"IV"``).
_GENERATIONAL_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

#: Nobiliary particles — the lowercase connective tokens that belong to
#: the surname they precede ("de Sa", "van der Berg", "von Neumann").
#: A *lowercase* token always joins; capitalized members of the smaller
#: :data:`_CAPITALIZED_NAME_PARTICLES` set join too (see there).
_SURNAME_PARTICLES = frozenset(
    {
        "de",
        "di",
        "da",
        "del",
        "della",
        "dei",
        "degli",
        "des",
        "du",
        "dos",
        "das",
        "le",
        "la",
        "les",
        "van",
        "von",
        "der",
        "den",
        "ten",
        "ter",
        "op",
        "het",
        "al",
        "el",
        "bin",
        "ibn",
        "ben",
        "af",
        "zu",
        "zum",
        "zur",
    }
)

#: Particles and name prefixes that join the surname even capitalized,
#: but only when another name token follows — the free-string form then
#: matches :func:`_family_key`, which folds a parsed ``family`` field
#: whole (``"Maria De La Cruz"`` → ``delacruz``, ``"Van Der Berg"`` →
#: ``vanderberg``). ``mac``/``mc`` are surname *prefixes* (a spaced
#: ``"Mac Donald"`` folds like ``MacDonald``), not nobiliary particles,
#: but the fold is the same. A capitalized particle that is *not*
#: followed by another name token is the surname itself, and a lone
#: capitalized particle before the surname is likelier a given name
#: (``"Le Nguyen"`` → ``lenguyen`` — a tolerated false positive).
_CAPITALIZED_NAME_PARTICLES = frozenset(
    {
        "van",
        "von",
        "der",
        "den",
        "de",
        "la",
        "le",
        "du",
        "da",
        "di",
        "del",
        "della",
        "dos",
        "das",
        "ter",
        "ten",
        "bin",
        "ben",
        "ibn",
        "al",
        "mac",
        "mc",
    }
)


def _surname_from_string(name: str) -> str:
    """Extract the surname from a free-text name string.

    Handles the shapes we see in the wild:

    - ``"Smith, John"``  → ``smith``  (comma form: take the head)
    - ``"John Smith"``   → ``smith``  (Western order: take last word)
    - ``"Smith"``        → ``smith``  (mononym)
    - ``"A.Clark"`` / ``"A.B.Clark"``  → ``clark``  (glued initials)
    - ``"Mula A"`` / ``"Mula A.V."`` / ``"Mula AV"`` → ``mula``
      (surname + trailing initials — gr521515 dry-run: ``a26avk``;
      the bare ``"AV"`` is PubMed's unpunctuated form)
    - ``"Smith Jr"`` / ``"Smith III"`` → ``smith``  (generational
      suffixes peel like initials)
    - ``"John de Sa"`` / ``"Jan van der Berg"`` → ``desa`` /
      ``vanderberg``  (lowercase nobiliary particles join the surname —
      gr521515 dry-run: ``sa98``)
    - ``"Maria De La Cruz"`` / ``"Van Der Berg"`` / ``"Mac Donald"`` →
      ``delacruz`` / ``vanderberg`` / ``macdonald``  (capitalized
      particles join when another name token follows — the parsed
      ``family`` field folds the same text whole)
    """
    first = (name or "").strip()
    if not first:
        return ""
    if "," in first:
        return _key_letters(first.split(",", 1)[0])
    parts = first.split()
    # Peel trailing non-surname tokens: dotted/single initials
    # ("A.", "A.V."), bare caps initials after a mixed-case head
    # ("Mula AV"), and generational suffixes ("Jr", "III").
    while len(parts) > 1:
        tail = parts[-1]
        if (
            _INITIALS_TOKEN_RE.fullmatch(tail)
            or tail.rstrip(".").lower() in _GENERATIONAL_SUFFIXES
            or (
                _CAPS_INITIALS_TOKEN_RE.fullmatch(tail)
                and any(ch.islower() for ch in parts[-2])
            )
        ):
            parts.pop()
        else:
            break
    surname = parts[-1] if parts else ""
    # Join the run of particles immediately before it: a lowercase
    # particle always joins; a capitalized member of
    # _CAPITALIZED_NAME_PARTICLES joins when another name token follows
    # it — true for every token inside this scan, so the run grows left
    # until a non-particle head.
    i = len(parts) - 2
    particles: list[str] = []
    while i >= 0:
        letters = re.sub(r"[^a-z]", "", parts[i].lower())
        if letters in _CAPITALIZED_NAME_PARTICLES or (
            parts[i].islower() and letters in _SURNAME_PARTICLES
        ):
            particles.insert(0, parts[i])
            i -= 1
        else:
            break
    if particles:
        surname = " ".join([*particles, surname])
    return _key_letters(surname)


def _family_key(family: str) -> str:
    """A parsed ``family`` field → cite_key letters, used WHOLE.

    The field is already the surname — no last-token guess: compound
    surnames (``"Fidler Mis"`` → ``fidlermis``) and particle surnames
    (``"de Sa"`` → ``desa``) keep every part; spaces simply drop in the
    letter fold (gr521515 dry-run: ``mis26``, ``sa98``). A pathological
    ``"Family, Given"`` blob still takes the head.
    """
    s = str(family)
    if "," in s:
        s = s.split(",", 1)[0]
    return _key_letters(s)


#: Collective/corporate byline tokens — a ``refs.authors`` entry
#: carrying one is a group or an organisation, not a person
#: ("Parkinson Study Group", "Neuralynx, Inc.", "LHCb Collaboration").
#: Matched whole-word, case-insensitively, across the entry's whole
#: name text (the token can land in ``given`` — ``{"given": "Inc.",
#: "family": "Neuralynx"}``). The mint walks past a collective to the
#: next person author; with none it falls back to ``anon``, staying
#: junk-shaped and sweep-visible (gr521515 dry-run: ``group00``,
#: ``neuralynx12``).
_COLLECTIVE_TOKENS = frozenset(
    {
        "group",
        "consortium",
        "committee",
        "study",
        "collaboration",
        "society",
        "team",
        "network",
        "project",
        "investigators",
        "investigator",
        "trialists",
        "collective",
        "alliance",
        "initiative",
        "working",
        "taskforce",
        "association",
        "foundation",
        "institute",
        "institutes",
        "university",
        "academy",
        "council",
        "federation",
        "commission",
        "agency",
        "bureau",
        "ministry",
        "authority",
        "administration",
        "department",
        "dept",
        "division",
        "laboratory",
        "laboratories",
        "center",
        "centre",
        "inc",
        "ltd",
        "llc",
        "corp",
        "corporation",
        "company",
        "gmbh",
        "pty",
        "plc",
        "bv",
        "pharma",
        "pharmaceuticals",
        "technologies",
        "labs",
        "systems",
        "holdings",
        "enterprises",
        "industries",
    }
)


def _is_collective_name(text: str) -> bool:
    """True when a byline entry's name text is a collective or an
    organisation rather than a person — a collective token, or digits
    (no person-name shape)."""
    lowered = text.lower()
    if any(ch.isdigit() for ch in lowered):
        return True
    return any(tok in _COLLECTIVE_TOKENS for tok in re.findall(r"[a-z]+", lowered))


def _entry_is_collective(entry: Any) -> bool:
    """True when one ``refs.authors`` element is a collective/org."""
    if isinstance(entry, Mapping):
        text = " ".join(
            str(entry.get(k) or "") for k in ("given", "family", "last", "name")
        )
        return _is_collective_name(text)
    return _is_collective_name(str(entry or ""))


def _entry_surname(entry: Any) -> str:
    """Best surname guess for one byline entry, in any stored shape."""
    if isinstance(entry, Mapping):
        family = entry.get("family") or entry.get("last")
        if family:
            return _family_key(str(family))
        name = entry.get("name")
        return _surname_from_string(str(name)) if name else ""
    if isinstance(entry, str):
        return _surname_from_string(entry)
    return ""


#: Literal title stamped on a paper minted with no title of its own — a
#: DOI/identifier-only acquire (``Store.acquire_paper_stub``). It is a
#: *sentinel*, not a value: every pass that learns a real title from an
#: external record (Crossref, Semantic Scholar) must overwrite it rather
#: than treat the row as already-titled. Kept here so the writer and the
#: fillers agree on one spelling.
PLACEHOLDER_TITLE = "(no title)"


def is_placeholder_title(title: str | None) -> bool:
    """True when ``title`` carries no information — blank, or the
    :data:`PLACEHOLDER_TITLE` sentinel a title-less acquire stamps.

    The check a metadata-filling pass uses to decide "may I write the
    title I just fetched?", so that a real title is never clobbered and
    a sentinel never survives.

        >>> is_placeholder_title(None), is_placeholder_title("  ")
        (True, True)
        >>> is_placeholder_title("(no title)")
        True
        >>> is_placeholder_title("Attention Is All You Need")
        False
    """
    stripped = (title or "").strip()
    return not stripped or stripped == PLACEHOLDER_TITLE


def _first_author_surname(authors: Any) -> str:
    """Return the first author's surname, lowercased ASCII letters only.

    Accepts:

    - ``list[str]``: ``["Smith, John", "Doe, Jane"]``
    - ``list[dict]``: ``[{"family": "Smith", "given": "John"}, …]``
      (CrossRef / Semantic Scholar shape)
    - any iterable yielding the above
    - ``None`` or empty input → ``""``

    Free-text bylines (a single string with comma-separated authors)
    are not handled here — split before calling.
    """
    if not authors:
        return ""
    try:
        first = next(iter(authors))
    except TypeError:
        return ""
    return _entry_surname(first)


def _first_person_surname(authors: Any) -> str:
    """Surname of the first *person* author — collectives skipped.

    A corporate/collective byline entry ("Parkinson Study Group",
    ``{"given": "Inc.", "family": "Neuralynx"}``) never supplies the
    surname — the mint walks to the next person author instead
    (gr521515 dry-run: ``group00``, ``neuralynx12``). Returns ``""``
    when no person author exists, which is what lets the caller fall
    back to the junk-detectable ``anon`` placeholder rather than
    minting a corporate token as a surname.
    """
    if not authors:
        return ""
    try:
        entries = iter(authors)
    except TypeError:
        return ""
    for entry in entries:
        if _entry_is_collective(entry):
            continue
        surname = _entry_surname(entry)
        if surname:
            return surname
    return ""


# ---------------------------------------------------------------------------
# DOI / arXiv normalisation
# ---------------------------------------------------------------------------


_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)


def normalize_doi(s: str | None) -> str | None:
    """Strip URL / ``doi:`` prefixes and lowercase. Empty / None → None.

    DOIs are case-insensitive per the DOI Handbook §2.4. We canonicalise
    to lowercase so ``(id_kind='doi', id_value=…)`` PK collisions aren't
    invented by publisher case inconsistency.
    """
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    lower = raw.lower()
    for prefix in _DOI_PREFIXES:
        if lower.startswith(prefix):
            lower = lower[len(prefix) :]
            break
    lower = lower.lstrip("/")
    return lower or None


_ARXIV_PREFIXES = (
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
    "arxiv.org/abs/",
    "arxiv.org/pdf/",
    "arxiv:",
)
_ARXIV_VERSION_RE = re.compile(r"v\d+$")
# New-style arxiv IDs are ``NNNN.NNNNN`` (YYMM.number, 4 or 5
# digit number). Old-style: ``<archive>(.<subclass>)?/<7-digit>``
# (e.g. ``cs.LG/0501001``, ``math-ph/0001001``). The validator
# accepts either shape and rejects anything else so callers like
# ``fetch_paper_by_arxiv`` don't hit Semantic Scholar with junk.
_ARXIV_ID_RE = re.compile(
    r"^("
    r"\d{4}\.\d{4,5}"  # new-style
    r"|"
    r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}"  # old-style
    r")$"
)


def normalize_arxiv(s: str | None) -> str | None:
    """Strip URL / ``arXiv:`` prefixes and version suffix. Empty → None.

    Version stripping makes the resulting id stable across preprint
    revisions: ``2301.12345v3`` and ``2301.12345v1`` both → ``2301.12345``.

    Old-style ids (``cs.LG/0501001``) preserve their archive prefix
    case; new-style ids (``2301.12345``) are pure digits.
    """
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    # Lowercase only the prefix-matching portion, not the id itself
    # (old-style ``cs.LG`` is case-significant in some catalogues).
    head = raw.lower()
    for prefix in _ARXIV_PREFIXES:
        if head.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.lstrip("/")
    # Drop fragment / query (#abstract, ?context=…)
    for sep in ("#", "?"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    # Strip a trailing pdf extension that occasionally leaks in.
    if raw.lower().endswith(".pdf"):
        raw = raw[:-4]
    raw = _ARXIV_VERSION_RE.sub("", raw)
    if not raw:
        return None
    # Format gate: anything that doesn't look like a valid arxiv id
    # gets rejected here so downstream lookups (Semantic Scholar,
    # OAI-PMH, …) don't waste a network round-trip on a typo.
    if not _ARXIV_ID_RE.match(raw):
        return None
    return raw


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------


def make_pdf_sha256(pdf_bytes: bytes) -> str:
    """Hex SHA-256 of the raw PDF file bytes. 64 chars."""
    return hashlib.sha256(pdf_bytes).hexdigest()


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text_for_hash(text: str | None) -> str:
    """Canonical form used by :func:`make_content_hash`.

    Steps: NFKD-fold, lowercase, collapse runs of whitespace into a
    single ASCII space, strip leading/trailing whitespace.

    Idempotent: ``f(f(x)) == f(x)``.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = folded.lower()
    folded = _WHITESPACE_RE.sub(" ", folded).strip()
    return folded


def make_content_hash(text: str | None) -> str:
    """Hex SHA-256 of :func:`normalize_text_for_hash`. 64 chars.

    Dedup key for "same paper, different bytes" — re-OCR and re-typeset
    versions of the same content fold to the same hash. Companion to
    :func:`make_pdf_sha256` which catches byte-equality.
    """
    canonical = normalize_text_for_hash(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Primary identifiers
# ---------------------------------------------------------------------------


def make_paper_id(
    *,
    arxiv: str | None = None,
    doi: str | None = None,
    pdf_sha256: str | None = None,
) -> str:
    """Build the synthetic ``paper_id`` for a ref.

    Priority: ``arxiv`` > ``doi`` > ``sha256``. Inputs are normalised
    first (caller may pass URL or prefix forms); the chosen source
    decides which "kind:" prefix prefixes the result.

    Returns one of:

    - ``"arxiv:<id>"``   (e.g. ``"arxiv:2301.12345"``)
    - ``"doi:<id>"``     (e.g. ``"doi:10.1234/foo"``)
    - ``"sha256:<hex>"`` (e.g. ``"sha256:abc123…"``)

    Raises:
        ValueError: when all three kwargs are None / empty, i.e. the
            ref has no source identifier whatsoever.
    """
    a = normalize_arxiv(arxiv)
    if a:
        return f"arxiv:{a}"
    d = normalize_doi(doi)
    if d:
        return f"doi:{d}"
    s = (pdf_sha256 or "").strip().lower() or None
    if s:
        return f"sha256:{s}"
    raise ValueError("make_paper_id requires at least one of arxiv / doi / pdf_sha256")


def make_finding_paper_id(
    body_text: str,
    scope: Mapping[str, Any] | None,
    initial_cite_pub_id: str,
) -> str:
    """Build the synthetic ``paper_id`` for a finding ref.

    Findings have no external ID (no DOI, no arXiv, no PDF). Their
    identity is derived from the empirical content the agent created:
    the claim text plus its setup envelope plus the initial citation
    that anchored the chase. Same inputs → same ``paper_id`` → same
    ``pub_id`` (via :func:`make_pub_id`), which makes the skill-side
    "search before create" rule self-correcting: two agents recording
    the same finding under the same setup collide on insert at the
    UNIQUE constraint on ``ref_identifiers (id_kind='pub_id')``
    rather than spawning duplicate chases.

    Returns ``f"finding:{hex}"`` where ``hex`` is the SHA-256 of a
    canonical key derived from the three inputs. The ``finding:``
    prefix slots into the same name space as ``arxiv:``, ``doi:``,
    ``sha256:`` so :func:`make_pub_id` does not need to know what
    kind of ref it's minting for.

    Canonicalisation:

    - ``body_text``: :func:`normalize_text_for_hash` (NFKD-fold +
      lowercase + whitespace collapse). Idempotent under cosmetic
      re-edits ("2.4 kV" vs "2.4 kV" with extra spaces).
    - ``scope``: ``json.dumps`` with ``sort_keys=True`` and
      no-whitespace separators. ``None`` and ``{}`` collapse to the
      same canonical ``"{}"``.
    - ``initial_cite_pub_id``: stripped, lowercased.

    Args:
        body_text: The claim text the agent supplied
            (`finding_body` chunk). Required non-empty.
        scope: Optional structured slice of the setup envelope, e.g.
            ``{"electrode": "Cu", "ambient": "N2"}``. Empty / None
            allowed (some findings carry only prose context).
        initial_cite_pub_id: ``pub_id`` of the ref the agent cited
            as the starting frontier (the ``cited_in`` argument on
            ``put(kind='finding', …)``). Required non-empty — a
            finding without an initial cite is not a finding.

    Raises:
        ValueError: when ``body_text`` or ``initial_cite_pub_id`` is
            empty / whitespace-only.

    Example:
        >>> pid = make_finding_paper_id(
        ...     "2.4 kV held for 30 s on Si/SiO2 MOSCAPs",
        ...     {"electrode": "Cu", "ambient": "N2"},
        ...     "ab12c3",
        ... )
        >>> pid.startswith("finding:")
        True
        >>> len(pid) == len("finding:") + 64
        True
    """
    if not body_text or not body_text.strip():
        raise ValueError("make_finding_paper_id requires a non-empty body_text")
    if not initial_cite_pub_id or not initial_cite_pub_id.strip():
        raise ValueError(
            "make_finding_paper_id requires a non-empty initial_cite_pub_id"
        )
    body_canonical = normalize_text_for_hash(body_text)
    # Sort keys so {"a":1,"b":2} and {"b":2,"a":1} hash identically; no
    # whitespace so cosmetic JSON formatting changes don't churn the id.
    # dict(scope) coerces Mapping/None into a plain dict json can serialise.
    scope_canonical = json.dumps(
        dict(scope) if scope else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    cite_canonical = initial_cite_pub_id.strip().lower()
    key = f"finding|body={body_canonical}|scope={scope_canonical}|cite={cite_canonical}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"finding:{digest}"


def make_taproot_hub_paper_id(
    sentence: str,
    scope: Mapping[str, Any] | None,
) -> str:
    """Build the synthetic ``paper_id`` for a taproot claim-hub finding.

    A taproot hub (:func:`precis.taproot.hub.mint_hub`) is minted by the
    canonicalizer, not by an agent's ``put(kind='finding', cited_in=…)`` —
    there is no citing occasion to anchor it, so it has no
    ``initial_cite_pub_id`` and can't reuse :func:`make_finding_paper_id`
    (which requires one). Instead the hub's identity is derived purely
    from the canonicalized claim it represents: the normalized sentence
    plus its scope. Same claim → same ``paper_id`` → same
    :func:`make_pub_id`, mirroring the finding-side dedup design (same
    inputs collide at the ``ref_identifiers (id_kind='pub_id')`` UNIQUE
    constraint rather than minting a duplicate hub) — belt-and-suspenders
    under the canonicalizer, whose own job is to prevent two hubs for the
    same claim in the first place.

    Returns ``f"taproot-hub:{hex}"`` where ``hex`` is the SHA-256 of a
    canonical key built the same way as :func:`make_finding_paper_id`
    (:func:`normalize_text_for_hash` on the sentence, sorted-keys
    whitespace-free JSON on the scope) minus the citation anchor. The
    ``taproot-hub:`` prefix keeps this out of the ``arxiv:`` / ``doi:`` /
    ``sha256:`` / ``finding:`` namespaces already used by
    :func:`make_paper_id` / :func:`make_finding_paper_id`.

    Args:
        sentence: The canonicalized claim sentence
            (``CanonicalClaim.sentence``). Required non-empty.
        scope: The claim's light scope dict (``CanonicalClaim.scope``).
            Empty / None allowed.

    Raises:
        ValueError: when ``sentence`` is empty / whitespace-only.
    """
    if not sentence or not sentence.strip():
        raise ValueError("make_taproot_hub_paper_id requires a non-empty sentence")
    sentence_canonical = normalize_text_for_hash(sentence)
    scope_canonical = json.dumps(
        dict(scope) if scope else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    key = f"taproot-hub|sentence={sentence_canonical}|scope={scope_canonical}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"taproot-hub:{digest}"


def make_pub_id(paper_id: str) -> str:
    """6-character base32 lowercase handle derived from ``paper_id``.

    Formula (locked):

    .. code-block:: python

        digest = sha256(paper_id.encode("utf-8"))
        pub_id = base32(digest)[:6].lower()

    Charset is ``[a-z2-7]``. Pinned at first ingest; deterministic.

    Raises:
        ValueError: when ``paper_id`` is empty.
    """
    if not paper_id:
        raise ValueError("make_pub_id requires a non-empty paper_id")
    digest = hashlib.sha256(paper_id.encode("utf-8")).digest()
    return base64.b32encode(digest)[:6].decode("ascii").lower()


def make_cite_key(
    authors: Iterable[Any] | None,
    year: int | None,
    *,
    taken: Iterable[str] = (),
) -> str:
    """Mint a LaTeX-friendly citation handle.

    Format: ``<surname><yy>[<letter>]`` — first-author surname, 2-digit
    year, optional one-letter collision suffix.

    Examples (with ``taken`` empty unless noted):

    .. code-block:: pycon

        >>> make_cite_key([{"family": "Miller"}], 2023)
        'miller23'
        >>> make_cite_key(["Miller, John"], 2023, taken={"miller23"})
        'miller23a'
        >>> make_cite_key(["Miller"], 2023, taken={"miller23", "miller23a"})
        'miller23b'
        >>> make_cite_key([], 2023)
        'anon23'
        >>> make_cite_key(["Miller"], None)
        'miller00'

    Args:
        authors: Iterable of strings or ``{"family", "given"}`` dicts.
            Only the first element matters.
        year: Integer year; ``None`` falls back to ``"00"``.
        taken: Set / iterable of cite_keys already in the corpus that
            share the prefix. Pass the result of
            ``SELECT id_value FROM ref_identifiers WHERE
            id_kind='cite_key' AND id_value LIKE :prefix || '%'``.

    Raises:
        CiteKeyOverflow: defensive only — the suffix space rolls
            ``a``…``z``, ``aa``…``zz``, ``aaa``… so a real (finite)
            corpus never overflows.
    """
    surname = _first_person_surname(authors) or "anon"
    yy = f"{year % 100:02d}" if year is not None else "00"
    base = f"{surname}{yy}"
    taken_set = set(taken)
    if base not in taken_set:
        return base
    # Roll a, b, … z, aa, ab, … (bijective base-26). Bounded by the taken
    # count: among the first N+1 distinct candidates at least one is free,
    # so this always terminates without CiteKeyOverflow for a real corpus.
    for n in range(1, len(taken_set) + 2):
        candidate = base + _cite_suffix(n)
        if candidate not in taken_set:
            return candidate
    raise CiteKeyOverflow(base, taken_set)  # unreachable for a finite taken set


#: Junk-mint cite_key shapes (gr521515): keys minted from empty or garbage
#: metadata at ingest — ``anon`` when the byline was unknown
#: (:func:`make_cite_key`'s empty-author placeholder), ``user``/``the``
#: when a junk ``/Info`` author string survived old parsing as the
#: "surname". After the word the mint is digits (the 2-digit year) then
#: the collision suffix — letters only — so ``[a-z]*`` covers the
#: ``a``…``z``/``aa``… roll too. A real surname can't collide: it is
#: ``[a-z]+`` by construction, and ``anon``/``user``/``the`` aren't
#: surnames the metadata repair paths produce.
_JUNK_CITE_KEY_RE = re.compile(r"(?:anon|user|the)\d+[a-z]*")


def is_junk_cite_key(key: str | None) -> bool:
    """True when ``key`` is a junk-mint cite_key shape (gr521515).

    >>> [is_junk_cite_key(k) for k in
    ...  ("anon13a", "user88", "the24z", "anon00", "murty88", "theo88", None)]
    [True, True, True, True, False, False, False]
    """
    return (
        key is not None and _JUNK_CITE_KEY_RE.fullmatch(key.strip().lower()) is not None
    )


def make_node_id(paper_id: str, page: int | None, block_index: int) -> str:
    """8-character opaque handle for a chunk-like node within a paper.

    Stable across DB rebuilds: the same ``(paper_id, page, block_index)``
    triple always produces the same ``node_id``. Useful as a chunk
    handle that survives re-ingest renumbering of ``BIGSERIAL chunk_id``.

    Algorithm:

    .. code-block:: python

        key = f"{paper_id}:p{page}:b{block_index}"
        node_id = base32(sha256(key))[:8].lower()

    ``page = None`` is encoded literally as ``pNone`` so non-paginated
    refs (notes, code symbols) still get a stable id space.
    """
    if not paper_id:
        raise ValueError("make_node_id requires a non-empty paper_id")
    key = f"{paper_id}:p{page}:b{block_index}".encode()
    digest = hashlib.sha256(key).digest()
    return base64.b32encode(digest)[:8].decode("ascii").lower()


__all__ = [
    "PLACEHOLDER_TITLE",
    "CiteKeyOverflow",
    "is_junk_cite_key",
    "is_placeholder_title",
    "make_cite_key",
    "make_content_hash",
    "make_finding_paper_id",
    "make_node_id",
    "make_paper_id",
    "make_pdf_sha256",
    "make_pub_id",
    "make_taproot_hub_paper_id",
    "normalize_arxiv",
    "normalize_doi",
    "normalize_text_for_hash",
]
