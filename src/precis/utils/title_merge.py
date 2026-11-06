"""Title-match merge gate shared by ingest dedup and the store (gr264748).

Lives in ``utils`` because ``Store.upsert_stub_paper`` needs it and the store
may not import ``precis.ingest``. ``content_tokens`` is the shared title
tokenizer (``ingest.verify_metadata`` re-exports it).
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from precis.identity import _entry_is_collective, _entry_name_text, _entry_surname
from precis.utils.title_views import title_plain

# Common English stopwords — excluded from word-overlap scoring so it
# focuses on distinctive content words.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "with",
        "and",
        "or",
        "but",
        "not",
        "from",
        "into",
        "onto",
        "upon",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "its",
        "it",
        "via",
        "using",
        "based",
        "new",
        "novel",
        "study",
        "studies",
        "review",
    }
)


def content_tokens(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens (≥4 chars), stopwords dropped.

    The shared tokenizer for :func:`title_overlap` (gr353804) — deliberately
    a plainer cut than :func:`_word_overlap_score`'s substring matching:
    that function tolerates pluralisation/prefix variants on purpose (a
    *real* paper's title vs its own PDF text), while ``title_overlap``
    exists to catch the opposite case — a registry hit with NO genuine
    relationship to the PDF at all — where exact-token intersection is
    the cleaner signal (no risk of a short substring like "the" tucked
    inside an unrelated word inflating the count; ``\\w`` already excludes
    that here since stopwords are filtered post-tokenization, not via
    substring).
    """
    return frozenset(
        w
        for w in re.findall(r"[a-z0-9]{4,}", title_plain(text).lower())
        if w not in _STOPWORDS
    )


def _author_list(authors: Any) -> list[Any]:
    """``authors`` as a list: a list, a JSON-encoded list, else ``[]``."""
    if isinstance(authors, list):
        return authors
    if isinstance(authors, str) and authors:
        try:
            parsed = json.loads(authors)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _surname(name: str) -> str:
    """Surname of one name string: ``"Last, First"`` or ``"First Last"``."""
    name = name.strip()
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


#: Minimum distinctive title tokens an automatic title merge needs: a short
#: title ("Defects in Carbon Nanotubes") is shared by unrelated papers.
_MIN_TITLE_TOKENS = 5


def _first_family(authors: Any) -> str:
    """Comparable family name of the first author ('' when none usable).

    Accent-folded (NFKD), lowercased, punctuation dropped, and only the last
    token kept, so ``van der Berg`` matches ``Berg`` and ``Müller`` matches
    ``Muller``.
    """
    items = _author_list(authors)
    if not items:
        return ""
    first = items[0]
    if isinstance(first, dict):
        fam = str(first.get("family") or "").strip()
        if not fam:
            fam = _surname(str(first.get("name") or ""))
    else:
        fam = _surname(str(first))
    fam = unicodedata.normalize("NFKD", fam)
    fam = "".join(ch for ch in fam if not unicodedata.combining(ch)).lower()
    toks = re.sub(r"[^a-z0-9\s]", " ", fam).split()
    return toks[-1] if toks else ""


#: "Names" that carry no person identity — a placeholder byline slot
#: (``Anonymous``, ``et al``, ``unknown``) is *no evidence*, not a person
#: the identity gate may mismatch on (gr574291: such rows refused
#: same-paper stub reconciles as ``author-mismatch``).
_NONAME_TOKENS = frozenset(
    {
        "anonymous",
        "anon",
        "unknown",
        "various",
        "et",
        "al",
        "author",
        "authors",
        "contributor",
        "contributors",
        "none",
        "null",
    }
)

#: A trailing ``et al`` / ``and others`` on a display name — dropped
#: before tokenising so ``"Zhang et al"`` compares as ``Zhang``.
_ET_AL_TAIL_RE = re.compile(r"\s+(?:et\s+al|and\s+others)\.?\s*$", re.IGNORECASE)


def _name_tokens(name: str) -> frozenset[str]:
    """Word tokens of one person's name, folded for comparison.

    NFKD-folded and lowercased; hyphens and apostrophes fold *away* so
    ``Chen-Yu`` → ``chenyu`` — the two sides' bylines come from different
    tiers and differ on exactly that axis (gr574291: ``chenyu zhang``
    vs ``Chen-Yu Zhang`` read as a mismatch).
    """
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch)).lower()
    name = re.sub(r"[-'’]", "", name)
    return frozenset(re.findall(r"[a-z0-9]+", name))


def _entry_label(entry: Any) -> str:
    """First-author-comparable display text for one byline entry.

    Whatever name fields the entry carries joined (``given`` +
    ``family``, or a bare ``name``); a semicolon-packed byline is cut to
    its first segment and a trailing ``et al`` dropped — some registries
    deliver both shapes and the *first* name is the one compared.
    """
    text = _entry_name_text(entry).split(";", 1)[0].strip()
    return _ET_AL_TAIL_RE.sub("", text)


def _first_person(authors: Any) -> tuple[str, str]:
    """``(label, surname)`` of the byline's first *person* author.

    Entries carrying no person identity are skipped — empty slots,
    placeholders (``Anonymous``/``et al`` — see :data:`_NONAME_TOKENS`),
    collectives/orgs (:func:`precis.identity._entry_is_collective`) — so
    a consortium-led or placeholder byline is *no evidence*, never a
    name to mismatch on (gr574291). ``("", "")`` when none exists.
    """
    for entry in _author_list(authors):
        label = _entry_label(entry)
        if not label:
            continue
        if _name_tokens(label) <= _NONAME_TOKENS:
            continue
        if _entry_is_collective(entry):
            continue
        return label, _entry_surname(entry)
    return "", ""


def _name_tokens_agree(a: frozenset[str], b: frozenset[str]) -> bool:
    """Token sets equal up to initials: one side's leftover tokens are all
    single characters heading leftover tokens on the other side — so
    ``Z. Chenyu`` ~ ``Chenyu Zhang`` (the family-initial case a plain
    surname compare can't see) and ``M. Alvarez`` ~ ``Maria Alvarez``."""

    def _covered(short: frozenset[str], long: frozenset[str]) -> bool:
        return all(len(t) == 1 and any(o.startswith(t) for o in long) for t in short)

    extra_a, extra_b = a - b, b - a
    return _covered(extra_a, extra_b) or _covered(extra_b, extra_a)


def first_author_agreement(stub_authors: Any, held_authors: Any) -> bool | None:
    """Whether the two bylines' first person authors are the same person.

    ``None`` when either side carries no usable first-author name —
    absent, placeholder or collective bylines are *no evidence*, never a
    mismatch (gr574291). Otherwise compares names as people, not strings:

    - surnames agree under :func:`precis.identity._entry_surname`
      (particles, trailing initials, honorifics all folded), or
    - the whole name's normalised token set agrees — casefolded,
      accent/hyphen-folded and order-insensitive, so ``"Zhang Chenyu"``
      ~ ``"Chen-Yu Zhang"`` ~ ``"Zhang, Chen-Yu"`` — with single-char
      leftovers read as initials (``C. Zhang`` ~ ``Chenyu Zhang``), or
    - one side's entire evidence is a bare surname token
      (``{"family": "Zhang"}``) contained in the other's name.
    """
    a_label, a_fam = _first_person(stub_authors)
    b_label, b_fam = _first_person(held_authors)
    a_toks, b_toks = _name_tokens(a_label), _name_tokens(b_label)
    if not a_toks or not b_toks:
        return None
    if a_fam and a_fam == b_fam:
        return True
    if a_toks == b_toks or _name_tokens_agree(a_toks, b_toks):
        return True
    return (a_toks == {a_fam} and a_fam in b_toks) or (
        b_toks == {b_fam} and b_fam in a_toks
    )


#: Minimum CONTENT-token count (:func:`content_tokens` — stopwords and
#: <4-char words excluded) of a normalised title for the identical-title
#: waiver: ``The Editorial`` or ``In Memoriam`` is too generic to
#: collapse on even when byte-identical; ``The rise of graphene`` has
#: two content tokens and qualifies.
_MIN_IDENTICAL_TITLE_TOKENS = 2


def _title_identity_key(title: str | None) -> str:
    """The identical-title key: plain view, lowercased, every non-word
    run folded to one space. Unicode letters keep their identity
    (``α`` ≠ ``γ`` — an ``α-Fe2O3`` paper is not the ``γ-Fe2O3`` one).
    ``""`` when nothing usable remains — two empty keys are NOT
    identical."""
    return re.sub(r"[^\w]+", " ", title_plain(title or "").lower()).strip()


def title_merge_refusal(
    stub_title: str | None,
    stub_year: int | None,
    stub_authors: Any,
    held_title: str | None,
    held_year: int | None,
    held_authors: Any,
    *,
    sim: float | None = None,
    unknown_year_min_sim: float | None = None,
) -> str | None:
    """Why a high-similarity title match must NOT auto-merge (``None`` = ok).

    A title match alone is a weak signal (gr264748: namesakes, short generic
    titles). An automatic merge needs all of: >=5 distinctive title tokens
    (:func:`content_tokens`) on both sides, a
    known year on BOTH sides within +/-1 (preprint vs published; a NULL year
    never passes), and agreeing first-author family names whenever both sides
    carry authors. Shared by ``ingest.dedup.reconcile_by_title_similarity`` and
    ``Store.upsert_stub_paper``.

    The token-count leg can be waived when the two titles are identical
    after normalisation (gr515509): ``"The rise of graphene" == "The Rise
    of Graphene"`` needs no >=5 tokens — the identical string IS the
    corroboration a fuzzy match was approximating. The waiver is
    deliberately narrow — it applies only when the normalised title has
    >=2 content tokens AND both sides carry a known year AND both carry a
    first-author family name that matches. Without that bar a title-only
    acquire with no supplied authors of ``Editorial`` or
    ``Reply`` would collapse onto whatever held paper shares the title —
    the hole the Opus review of gr515509 caught. Only the token count is
    waived; the year and author legs still apply afterwards.

    ``unknown_year_min_sim`` (with ``sim``) relaxes only the unknown-year
    rule: a missing year on either side passes when ``sim`` reaches it. The
    mint-time guard uses this so a year-less title-only re-acquire of a held
    paper stays idempotent; the batch reconcile leaves it unset, so a
    year-less match goes to review. A known year mismatch always refuses.
    """
    key = _title_identity_key(stub_title)
    identical = key != "" and key == _title_identity_key(held_title)
    a, b = _first_family(stub_authors), _first_family(held_authors)
    waive_tokens = (
        identical
        and len(content_tokens(key)) >= _MIN_IDENTICAL_TITLE_TOKENS
        and stub_year is not None
        and held_year is not None
        and a != ""
        and a == b
    )
    if not waive_tokens and (
        len(content_tokens(stub_title or "")) < _MIN_TITLE_TOKENS
        or len(content_tokens(held_title or "")) < _MIN_TITLE_TOKENS
    ):
        return "short-title"
    if stub_year is None or held_year is None:
        if not (
            unknown_year_min_sim is not None
            and sim is not None
            and sim >= unknown_year_min_sim
        ):
            return "year-unknown"
    elif abs(int(stub_year) - int(held_year)) > 1:
        return "year-mismatch"
    if a and b and a != b:
        return "author-mismatch"
    return None
