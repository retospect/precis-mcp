"""Canonical handling of ``refs.authors`` entries.

The ``refs.authors`` JSONB column holds author dicts in more than one
shape, because different writers built it differently over time:

* the **canonical shape**, CSL-style: ``{"given": "Bryan R.", "family":
  "Goldsmith"}`` — middle names/initials live inside ``given``, there is
  no third field. Optional ``orcid`` / ``affiliation`` / ``ror`` keys
  may ride along.
* ``{"name": "..."}`` remains a legal, explicit fallback for names that
  can't be reliably split (mononyms, some CJK, an ambiguous flat
  string) — this was the *only* shape every writer produced before
  :func:`normalize_authors` existed, so it's also what most legacy rows
  still hold.
* a few call sites pass bare strings, or a semicolon-packed byline
  string.

Readers must tolerate all of the above; indexing ``a["family"]``
directly is the bug this module exists to prevent — it silently blanks
the ``{"name"}`` shape (and a ``{"name"}``-only reader blanks the
``{"family", "given"}`` shape). Funnel every *read* through
:func:`author_names` / :func:`author_display` (default
``order="natural"`` — "Given Family" is the display convention
everywhere; "Family, Given" is *derived* only where a convention
demands it — sorting, BibTeX), and every ingest/edit *write* through
:func:`normalize_authors`, the single choke point every author writer
routes through: structured ``{given, family}`` input passes through
(junk-guarded); a flat string/`` {"name"}`` splits into
``{given, family}`` only when unambiguous (exactly one comma); anything
still ambiguous, or recognizably junk (see :func:`is_junk_author_name`
— an email, a bare section heading, an over-long non-name string),
stays ``{"name"}`` or is dropped outright.

``to_name_dicts`` predates :func:`normalize_authors` and is kept for
the paths that still want the old squash-everything-to-``{"name"}``
behaviour (metadata re-resolution / backfill enrichment, out of scope
for the structured-shape rollout) — new write paths should reach for
:func:`normalize_authors` instead.

Authored artifacts (``kind='draft'``) additionally carry a per-author
**affiliation** — an institution string plus an optional ROR id
(https://ror.org, the canonical de-duplicated organisation identifier).
That richer shape is ``{"name", "affiliation", "ror"}``;
:func:`to_author_dicts` is the write-path normaliser that *preserves*
those two keys (``to_name_dicts`` intentionally drops them), and
:func:`build_byline` turns the list into a rendered byline —
authors with superscript marks + the deduped affiliation list — shared
verbatim by the LaTeX / docx exporters and the web reader.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "author_display",
    "author_names",
    "build_byline",
    "is_junk_author_name",
    "normalize_authors",
    "to_author_dicts",
    "to_name_dicts",
]

# A lone all-caps token ("REFERENCES", "OECD") — used together with the
# stopword list below; a single all-caps *word* is virtually never a
# personal name (initials-only stamps are caught separately upstream by
# ``pdf_sidecar.is_garbage_author``).
_ALL_CAPS_TOKEN_RE = re.compile(r"^[A-Z]{2,}$")

# A dotted initial jammed against the next capital ("A.K." / "A.K") — the
# dominant Semantic Scholar byline style. Hyphenated initials ("A.-K.")
# don't match (next char is "-"), and multi-letter abbreviations ("St.",
# "PhD.") don't either (the char before the dot must itself be the
# single capital).
_JAMMED_INITIAL_RE = re.compile(r"\b([A-Z]\.)(?=[A-Z])")

# Section-heading / front-matter words that leak into an author field
# from a mis-parsed PDF or markup byline. Matched case-insensitively
# against the whole (punctuation-stripped) entry, not a substring.
_JUNK_STOPWORDS = frozenset(
    {
        "references",
        "bibliography",
        "abstract",
        "introduction",
        "conclusion",
        "conclusions",
        "acknowledgments",
        "acknowledgements",
        "keywords",
        "appendix",
        "contents",
        "methods",
        "methodology",
        "results",
        "discussion",
        "supplementary",
    }
)


def is_junk_author_name(name: str) -> bool:
    """True when *name* is an obvious non-name, not a real author.

    Catches the junk that leaks through raw-metadata scraping — an
    embedded PDF ``/Author`` field, or a mis-parsed markup byline: an
    email address, a lone all-caps token ("REFERENCES"), an over-long
    string (six-plus words — a mis-split affiliation or sentence, not a
    name), or a bare section-heading word ("Abstract", "Introduction").
    Conservative — a genuine short or single-word name ("Aristotle")
    never trips this. Used by :func:`normalize_authors` (every ingest
    write path) and by :func:`precis.ingest.lookup._sanitize_authors`
    (the PDF ``/Author`` path specifically). Pure — never raises.
    """
    s = name.strip()
    if not s:
        return True
    if "@" in s:
        return True
    words = s.split()
    if len(words) == 1 and _ALL_CAPS_TOKEN_RE.match(s):
        return True
    if len(words) > 6:
        return True
    if s.strip(".,:;").lower() in _JUNK_STOPWORDS:
        return True
    return False


def _tidy_initials(s: str) -> str:
    """Deterministic spacing repair on a name string.

    Unjams runs of dotted initials ("A.K. Geim" → "A. K. Geim",
    "J.R.R. Tolkien" → "J. R. R. Tolkien") and collapses doubled
    whitespace. Purely typographic — never adds, drops, or reorders name
    parts, so it can't lose information. Known trade-off: a dotted
    corporate acronym in an author slot ("U.S. Geological Survey" →
    "U. S. Geological Survey") gets the same spacing; accepted, since
    person initials outnumber dotted corporate authors overwhelmingly
    and the change is cosmetic. Pure — never raises.
    """
    return " ".join(_JAMMED_INITIAL_RE.sub(r"\1 ", s).split())


def author_display(entry: Any, *, order: str = "natural") -> str:
    """One author's display name, tolerant of every stored shape.

    ``order='natural'`` → ``"Given Family"`` (inline reading order);
    ``order='sortable'`` → ``"Family, Given"`` (citation / bib order).
    The order only affects ``{"family", "given"}`` entries — a bare
    ``{"name"}`` or string is returned as-is (we can't reliably split
    it). Returns ``""`` for empty / garbage so callers can filter.
    Pure — never raises.
    """
    if isinstance(entry, dict):
        family = (entry.get("family") or "").strip()
        given = (entry.get("given") or "").strip()
        if family and given:
            return f"{family}, {given}" if order == "sortable" else f"{given} {family}"
        if family:
            return family
        if given:
            return given
        return (entry.get("name") or "").strip()
    return str(entry or "").strip()


def author_names(raw: Any, *, order: str = "natural") -> list[str]:
    """Display names from a ``refs.authors`` value (or a packed byline).

    Accepts a list of dicts / strings (mixed shapes fine), a
    semicolon-packed string, or ``None`` / garbage. Empty entries are
    dropped. Pure — never raises.
    """
    if isinstance(raw, list):
        return [n for n in (author_display(a, order=order) for a in raw) if n]
    if isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(";") if a.strip()]
    return []


def to_name_dicts(raw: Any) -> list[dict[str, str]]:
    """Canonical storage shape — ``[{"name": "Family, Given"}, ...]``.

    Use on every write path so the column converges on one shape. Names
    are rendered sortable (``Family, Given``) to match the dominant
    Crossref ingest convention. Affiliation / ROR (if present) are
    *dropped* — use :func:`to_author_dicts` on the draft-authoring path
    where those must survive.
    """
    return [{"name": n} for n in author_names(raw, order="sortable")]


def normalize_authors(raw: Any) -> list[dict[str, Any]]:
    """The write-side choke point — every ingest/edit author writer
    routes through this, not an ad hoc ``{"name": ...}`` wrap.

    Converges heterogeneous input onto the canonical shape:
    ``{"given": ..., "family": ...}`` when the split is known (either
    side may be absent — a family-only entry stays ``{"family"}``),
    ``{"name": ...}`` as an explicit fallback when it isn't. Optional
    ``orcid`` / ``affiliation`` / ``ror`` keys on a dict entry ride
    along untouched.

    * Already-structured ``{"given"/"family", ...}`` entries pass
      through (junk-guarded on the rendered display name).
    * Every ``given`` / ``name`` string gets the :func:`_tidy_initials`
      spacing repair ("A.K. Geim" → "A. K. Geim") — typographic only,
      never reorders or drops name parts. ``family`` is left untouched
      (initial runs don't occur there; not touching it minimises
      mangling risk).
    * ``{"name": ...}`` dicts and bare strings are junk-guarded, then
      split into ``{"given", "family"}`` ONLY when unambiguous — a
      single comma ("Family, Given"). A natural "Given Family" string
      (no comma) is ambiguous by design (a middle name / multi-word
      surname can't be told apart without a real parser) and stays
      ``{"name": ...}`` — no heuristic reordering at write time.
    * Junk entries (see :func:`is_junk_author_name`) are dropped
      outright, regardless of shape.

    Accepts a list of dicts/strings, or a semicolon-packed string.
    Order is preserved. Pure — never raises.
    """
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(";") if p.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for a in raw:
        entry = _normalize_one_author(a)
        if entry is not None:
            out.append(entry)
    return out


def _normalize_one_author(a: Any) -> dict[str, Any] | None:
    """Normalize a single raw author entry — see :func:`normalize_authors`."""
    if isinstance(a, dict):
        family = (a.get("family") or "").strip()
        given = _tidy_initials((a.get("given") or "").strip())
        if family or given:
            display = f"{given} {family}".strip()
            if is_junk_author_name(display):
                return None
            entry: dict[str, Any] = {}
            if given:
                entry["given"] = given
            if family:
                entry["family"] = family
            _carry_optional_author_keys(a, entry)
            return entry
        name = _tidy_initials((a.get("name") or "").strip())
        if not name or is_junk_author_name(name):
            return None
        entry = _split_author_name(name)
        _carry_optional_author_keys(a, entry)
        return entry
    name = _tidy_initials(str(a or "").strip())
    if not name or is_junk_author_name(name):
        return None
    return _split_author_name(name)


def _split_author_name(name: str) -> dict[str, Any]:
    """Best-effort ``{"given", "family"}`` split — single-comma only."""
    if name.count(",") == 1:
        family, given = (p.strip() for p in name.split(","))
        if family and given:
            return {"given": given, "family": family}
    return {"name": name}


def _carry_optional_author_keys(src: dict[str, Any], dst: dict[str, Any]) -> None:
    """Copy non-blank ``orcid`` / ``affiliation`` / ``ror`` onto *dst*."""
    for key in ("orcid", "affiliation", "ror"):
        val = src.get(key)
        if isinstance(val, str):
            val = val.strip()
        if val:
            dst[key] = val


def to_author_dicts(raw: Any) -> list[dict[str, str]]:
    """Canonical draft-author storage shape, preserving affiliation + ROR.

    Like :func:`to_name_dicts` (sortable ``{"name"}``) but carries the
    optional ``affiliation`` (institution string) and ``ror`` (an
    https://ror.org id) through to storage. Accepts the same tolerant
    inputs as the readers — a list of dicts (``{"name"}`` /
    ``{"family", "given"}``, either with ``affiliation`` / ``ror``) or
    bare strings, or a semicolon-packed string (names only). Entries
    with no resolvable name are dropped. Pure — never raises.
    """
    if isinstance(raw, str):
        return [{"name": n} for n in author_names(raw, order="sortable")]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for a in raw:
        name = author_display(a, order="sortable")
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if isinstance(a, dict):
            aff = (a.get("affiliation") or "").strip()
            ror = (a.get("ror") or "").strip()
            if aff:
                entry["affiliation"] = aff
            if ror:
                entry["ror"] = ror
        out.append(entry)
    return out


def build_byline(raw: Any) -> dict[str, Any]:
    """Structured byline for rendering — the shared "notation of
    associations" consumed by both exporters and the web reader.

    Returns ``{"authors": [...], "affiliations": [...], "multi": bool}``:

    * ``authors`` — ordered ``{"name": "Given Family", "marks": [int...],
      "sup": "1,2"}``. ``marks`` indexes into ``affiliations``; ``sup`` is
      the pre-rendered comma-joined superscript, blank when there is only
      one distinct affiliation (a single shared institution reads better
      listed once, unnumbered).
    * ``affiliations`` — ordered ``{"index": int, "org": str, "ror": str}``,
      **deduped by ROR id** (falling back to the lower-cased org string),
      numbered 1.. in order of first appearance.
    * ``multi`` — whether more than one distinct affiliation exists (i.e.
      whether the superscript marks are meaningful).

    When no author carries an affiliation, ``affiliations`` is empty and
    every ``sup`` is blank — the byline degrades to a plain name list.
    Pure — never raises.
    """
    if isinstance(raw, list):
        items: list[Any] = raw
    elif isinstance(raw, str) and raw.strip():
        items = [a.strip() for a in raw.split(";") if a.strip()]
    else:
        items = []

    affiliations: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    authors: list[dict[str, Any]] = []
    for a in items:
        name = author_display(a, order="natural")
        if not name:
            continue
        aff = ror = ""
        if isinstance(a, dict):
            aff = (a.get("affiliation") or "").strip()
            ror = (a.get("ror") or "").strip()
        marks: list[int] = []
        if aff or ror:
            key = ror.lower() if ror else aff.lower()
            idx = by_key.get(key)
            if idx is None:
                idx = len(affiliations) + 1
                by_key[key] = idx
                affiliations.append({"index": idx, "org": aff, "ror": ror})
            marks = [idx]
        authors.append({"name": name, "marks": marks, "sup": ""})

    multi = len(affiliations) > 1
    if multi:
        for author in authors:
            author["sup"] = ",".join(str(m) for m in author["marks"])
    return {"authors": authors, "affiliations": affiliations, "multi": multi}
