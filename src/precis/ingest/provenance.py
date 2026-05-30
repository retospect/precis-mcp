"""Provenance / retraction monitoring against Crossref.

Phase 1 of ``docs/provenance-kind-plan.md``.

Public surface:

    check_doi(doi, *, store, mailto=None) -> ProvenanceResult

Fetches Crossref ``/works/{doi}``, classifies any ``message.update-to``
entries by severity, and for each retraction or expression-of-concern
notice:

- auto-ingests the notice as a paper ref *when the parent paper is
  in the local store* (otherwise the result is informational only —
  no writes)
- writes a ``retracted-by`` / ``corrected-by`` / ``concern-raised-by``
  link from the parent paper to the notice
- sets ``refs.retraction_status`` on the parent + applies
  ``STATUS:retracted`` / ``:concern`` / ``:corrected`` tag
- touches ``refs.retraction_checked_at`` so the TTL gate in later
  phases can skip recently-checked refs

Out of scope for Phase 1 (see plan):
- batch DOI input (Phase 2)
- Retraction Watch reason codes (Phase 3)
- transitive cite-walk (Phase 4)
- fuzzy DOI resolution (Phase 5)

DOI canonicalisation matches ``store/_identifiers_ops.py``: lowercase,
URL and ``doi:`` prefixes stripped.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Literal

from precis.ingest._text_norm import best_jaccard, surname_matches
from precis.store import Store, Tag


# ---------------------------------------------------------------------------
# DOI format validation
# ---------------------------------------------------------------------------

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def validate_doi(raw: str) -> str | None:
    """Return the canonical lowercase DOI form, or ``None`` if malformed.

    Accepts ``10.x/foo``, ``doi:10.x/foo``, ``https://doi.org/10.x/foo``,
    and the ``dx.doi.org`` variant. The shape check is conservative:
    ``10.<registrant>/<suffix>`` where the registrant is 4-9 digits and
    the suffix is non-empty and non-whitespace.

    Anything that doesn't match the shape returns ``None``; callers
    surface that as ``status='malformed'`` in the report rather than
    making an HTTP call against garbage.
    """
    if not raw:
        return None
    v = raw.strip()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/)", "", v, flags=re.IGNORECASE)
    v = re.sub(r"^doi:\s*", "", v, flags=re.IGNORECASE)
    if not _DOI_RE.match(v):
        return None
    return v.lower()


# ---------------------------------------------------------------------------
# Severity / status classification
# ---------------------------------------------------------------------------

Severity = Literal["blocker", "review", "note", "info"]
RetractionStatus = Literal["retracted", "expression_of_concern", "corrected"]
LinkRelation = Literal["retracted-by", "corrected-by", "concern-raised-by"]


# Crossref's ``update_type`` vocabulary mapped to our internal severity
# and the ``refs.retraction_status`` CHECK-constraint values from
# ``0001_initial.sql:308``. Keep this aligned with the table in
# ``docs/provenance-kind-plan.md`` § "Severity classification".
_UPDATE_TYPE_MAP: dict[str, tuple[Severity, RetractionStatus | None, LinkRelation | None]] = {
    "retraction":            ("blocker", "retracted",             "retracted-by"),
    "partial_retraction":    ("blocker", "retracted",             "retracted-by"),
    "withdrawal":            ("blocker", "retracted",             "retracted-by"),
    "removal":               ("blocker", "retracted",             "retracted-by"),
    "expression_of_concern": ("review",  "expression_of_concern", "concern-raised-by"),
    "correction":            ("note",    "corrected",             "corrected-by"),
    "corrigendum":           ("note",    "corrected",             "corrected-by"),
    "erratum":               ("note",    "corrected",             "corrected-by"),
    "addendum":              ("info",    "corrected",             "corrected-by"),
    "clarification":         ("info",    "corrected",             "corrected-by"),
    # ``new_edition`` / ``new_version`` aren't notices in the
    # retraction sense — Phase 1 ignores them. Phase 5 maps them
    # onto the existing ``supersedes`` link relation.
}


def classify_update_type(
    update_type: str,
) -> tuple[Severity, RetractionStatus | None, LinkRelation | None] | None:
    """Classify a Crossref ``update_type`` string.

    Returns ``(severity, status, link_relation)``, or ``None`` if the
    type isn't in our vocabulary (e.g. ``new_version``). ``status`` is
    one of the three values accepted by the ``refs.retraction_status``
    CHECK constraint; ``link_relation`` is one of the three notice
    relations defined in migration ``0002_provenance.sql``.
    """
    key = update_type.strip().lower().replace("-", "_").replace(" ", "_")
    return _UPDATE_TYPE_MAP.get(key)


# Dominance order: a paper with both a correction and a later
# retraction is reported as retracted; one with a concern but no
# retraction is reported as concern; etc. Used when writing the
# single ``refs.retraction_status`` column. The full chronology
# stays on the ``links`` table.
_STATUS_DOMINANCE: dict[RetractionStatus, int] = {
    "retracted":             3,
    "expression_of_concern": 2,
    "corrected":             1,
}


def dominant_status(
    statuses: list[RetractionStatus],
) -> RetractionStatus | None:
    """Pick the highest-severity status from a list, or ``None`` for empty."""
    if not statuses:
        return None
    return max(statuses, key=lambda s: _STATUS_DOMINANCE[s])


# ---------------------------------------------------------------------------
# Notice slug generation
# ---------------------------------------------------------------------------

# Single letter per notice family, per the resolved plan decision.
_NOTICE_LETTER: dict[LinkRelation, str] = {
    "retracted-by":      "r",
    "concern-raised-by": "e",
    "corrected-by":      "c",
}


def make_notice_slug(parent_slug: str, relation: LinkRelation, n: int) -> str:
    """Mint a notice slug from the parent paper's slug.

    Format ``<parent>-<letter><n>`` where letter is ``r`` for
    retractions, ``e`` for expressions of concern, ``c`` for
    corrections. ``n`` is 1-based; callers pass the next available
    sequence number for that letter family on the parent.

    Lives here (not in ``ingest/crossref.py:_normalize``) so the
    primary-paper slug heuristic stays unmodified. Notices have
    pathologically thin metadata (``authors=[{name: 'Editors'}]``,
    ``title='Retraction of …'``) that mangles the normal slug rule;
    deriving from the parent's slug avoids the problem entirely.
    """
    letter = _NOTICE_LETTER[relation]
    return f"{parent_slug}-{letter}{n}"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """One ``update-to`` entry on a Crossref paper record."""

    update_type: str  # raw Crossref string ("retraction", "corrigendum", …)
    severity: Severity
    status: RetractionStatus | None  # None when type is informational only
    relation: LinkRelation | None  # link relation to use when writing
    notice_doi: str  # the notice's own DOI, canonicalised
    notice_date: datetime | None  # ``updated`` timestamp from Crossref
    notice_title: str | None  # populated when notice metadata is fetched
    notice_authors: list[dict[str, Any]] | None
    notice_year: int | None
    persisted_ref_id: int | None = None  # populated when auto-ingested
    # Phase 3: Retraction Watch reason codes joined from the local
    # cache. Empty list means either (a) no RW data for this paper-DOI,
    # or (b) RW data exists but the notice_doi didn't match any cached
    # row. The renderer treats both cases identically — just no
    # reasons surfaced.
    rw_reasons: list[str] = field(default_factory=list)
    # RW's own classification ("Retraction" / "Correction" / "Expression
    # of concern" / …). Mostly redundant with ``update_type`` from
    # Crossref but kept for cross-reference when the two disagree
    # (rare, but informative when investigating discrepancies).
    rw_notice_nature: str | None = None


@dataclass(frozen=True, slots=True)
class BibEntry:
    """Caller-supplied bibliographic metadata for one citation.

    Used by Phase 2.5 verification to check that the supplied
    metadata actually corresponds to the DOI's Crossref record.
    All fields except ``doi`` are optional — missing fields are
    skipped in the verification step rather than treated as
    mismatches.

    ``authors`` is a list of surname strings (no given names; given
    names are commonly mangled and don't reliably distinguish
    papers). Pass the first author at minimum; the second-author
    and beyond are recorded for the report but not checked in
    Phase 2.5.
    """

    doi: str
    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    journal: str | None = None  # display only, no verification
    pages: str | None = None  # display only, no verification


YearMatch = Literal["match", "off_by_one", "mismatch", "unchecked"]


@dataclass(frozen=True, slots=True)
class MetadataVerification:
    """Per-field comparison of a supplied ``BibEntry`` against Crossref.

    Each field captures both the supplied and Crossref forms, plus a
    structural outcome. Scores stay raw — no pass/fail threshold is
    hardcoded; the report-rendering model applies common-sense
    judgement (see plan §"Phase 2.5: per-field thresholds").
    """

    # Title comparison via token-set Jaccard on the normalised form.
    # Score range [0, 1]; ``None`` when title wasn't supplied.
    title_score: float | None = None
    title_supplied: str | None = None
    title_crossref: str | None = None
    title_added_tokens: list[str] = field(default_factory=list)
    title_removed_tokens: list[str] = field(default_factory=list)

    # First-author surname comparison under both normalised forms.
    # ``None`` when authors weren't supplied.
    first_author_match: bool | None = None
    first_author_supplied: str | None = None
    first_author_crossref: str | None = None

    # Year comparison with ±1 tolerance for online-first vs print.
    year_match: YearMatch = "unchecked"
    year_supplied: int | None = None
    year_crossref: int | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceResult:
    """Outcome of ``check_doi`` for a single DOI."""

    doi: str  # canonical DOI as queried (may differ from input)
    status: Literal["ok", "malformed", "unknown", "check_failed"]
    # Crossref metadata for the queried paper (when status='ok')
    paper_title: str | None = None
    paper_authors: list[dict[str, Any]] | None = None
    paper_year: int | None = None
    notices: list[Notice] = field(default_factory=list)
    # Dominant status applied to refs.retraction_status (when paper in store)
    applied_status: RetractionStatus | None = None
    # Local ref id when the paper was found and write-through happened
    paper_ref_id: int | None = None
    # Hint for the caller: was the parent paper in the local store?
    paper_in_store: bool = False
    # Phase 2.5: populated when the caller supplied a BibEntry to
    # verify against Crossref. ``None`` for the plain DOI-check path.
    verification: MetadataVerification | None = None
    # Phase 3.5: 1-based position in the original input list. ``0``
    # for single-DOI calls (where there's no ambiguity to resolve)
    # or when the result is constructed outside a batch context.
    # Populated by ``check_dois`` at result-slot assignment so the
    # index reflects *input* order, not thread-pool completion order.
    # See plan §"Phase 3.5: numbered-result rendering".
    input_index: int = 0
    # Error message when status='check_failed'
    error: str | None = None

    @property
    def overall_severity(self) -> Severity:
        """Highest-severity bucket among notices, or ``'info'`` for clean."""
        if not self.notices:
            return "info"
        order: dict[Severity, int] = {
            "blocker": 4,
            "review":  3,
            "note":    2,
            "info":    1,
        }
        return max(self.notices, key=lambda n: order[n.severity]).severity

    @property
    def has_metadata_mismatch(self) -> bool:
        """True when the verification step found a substantive disagreement.

        Used by the renderer to decide whether to include this result
        in the ⚠️ Metadata mismatch section. ``None`` verification (no
        BibEntry supplied) returns False — nothing to flag.
        """
        v = self.verification
        if v is None:
            return False
        if v.first_author_match is False:
            return True
        if v.title_score is not None and v.title_score < 0.6:
            return True
        if v.year_match == "mismatch":
            return True
        return False


# ---------------------------------------------------------------------------
# Retraction Watch cache lookup (Phase 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RWCacheRow:
    """One row from ``provenance_rw_cache`` for a single paper DOI."""

    notice_doi: str | None
    notice_nature: str
    reasons: list[str]


def _lookup_rw_cache(store: Store, paper_doi: str) -> list[_RWCacheRow]:
    """Return every cached RW row for ``paper_doi``, or ``[]`` if none.

    Multiple rows are possible: a paper with both a correction *and*
    a later retraction has one row per notice. The caller matches
    each row to the corresponding Crossref ``update-to`` Notice by
    DOI; un-matched rows are surfaced via a fallback path so RW
    reasons aren't lost when Crossref's view of the paper is thin.

    Returns ``[]`` when the cache table doesn't exist yet (e.g. on
    a deployment that hasn't run migration 0003) — the caller
    treats the absence as "no reasons available", same as an empty
    cache. This keeps the kind usable in degraded environments.
    """
    sql = (
        "SELECT notice_doi, notice_nature, reasons "
        "FROM provenance_rw_cache WHERE paper_doi = %s"
    )
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(sql, (paper_doi,)).fetchall()
    except Exception:  # noqa: BLE001 — missing table / read error
        return []
    return [
        _RWCacheRow(
            notice_doi=(r[0] or None),
            notice_nature=str(r[1] or ""),
            reasons=list(r[2] or []),
        )
        for r in rows
    ]


def _enrich_notices_with_rw(
    notices: list[Notice],
    cache_rows: list[_RWCacheRow],
) -> list[Notice]:
    """Match RW cache rows to Crossref notices and merge in the reasons.

    Match strategy:

    1. **Exact DOI match** — cache row's ``notice_doi`` equals the
       notice's ``notice_doi``. Strongest signal.
    2. **Fallback by nature** — if no DOI match and there's exactly
       one cache row of the matching nature (retraction/correction/
       EoC), attach those reasons. Common when Crossref carries a
       generic notice DOI and the RW row has a more specific one.

    Reasons-on-no-match: returned as a synthetic notice would risk
    surfacing data the user can't act on (no notice DOI to cite).
    We keep it simple in Phase 3 — un-matched RW rows are dropped.
    The sync ledger captures coverage; future phases can add a
    "RW knows about this but Crossref doesn't" callout if useful.
    """
    if not cache_rows:
        return notices

    # Index cache rows by exact notice_doi for the fast path.
    by_doi: dict[str, _RWCacheRow] = {
        r.notice_doi.lower(): r for r in cache_rows if r.notice_doi
    }

    # Build a nature → row index for the fallback. Lowercase + collapse
    # whitespace so "Expression of Concern" matches "expression_of_concern"
    # off the Crossref side.
    def _norm_nature(s: str) -> str:
        return re.sub(r"[^a-z]", "", s.lower())

    by_nature: dict[str, list[_RWCacheRow]] = {}
    for r in cache_rows:
        by_nature.setdefault(_norm_nature(r.notice_nature), []).append(r)

    nature_for_status: dict[RetractionStatus, str] = {
        "retracted":             "retraction",
        "expression_of_concern": "expressionofconcern",
        "corrected":             "correction",
    }

    out: list[Notice] = []
    for n in notices:
        match: _RWCacheRow | None = by_doi.get(n.notice_doi)
        if match is None and n.status is not None:
            candidates = by_nature.get(nature_for_status.get(n.status, ""), [])
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            out.append(n)
            continue
        out.append(
            Notice(
                update_type=n.update_type,
                severity=n.severity,
                status=n.status,
                relation=n.relation,
                notice_doi=n.notice_doi,
                notice_date=n.notice_date,
                notice_title=n.notice_title,
                notice_authors=n.notice_authors,
                notice_year=n.notice_year,
                persisted_ref_id=n.persisted_ref_id,
                rw_reasons=list(match.reasons),
                rw_notice_nature=match.notice_nature or None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Metadata verification (Phase 2.5)
# ---------------------------------------------------------------------------


def _first_author_surname(authors: list[dict[str, Any]] | None) -> str | None:
    """Pluck the first author's surname from Crossref's author list shape."""
    if not authors:
        return None
    first = authors[0]
    # ``ingest/crossref.py`` normalises into ``{"name": "Family, Given"}``;
    # the comma split gives us the surname back.
    name = (first.get("name") or "").strip()
    if not name:
        return None
    return name.split(",")[0].strip()


def _classify_year_match(supplied: int, crossref: int) -> YearMatch:
    """``match`` for ==, ``off_by_one`` for |Δ|==1, ``mismatch`` otherwise."""
    diff = abs(supplied - crossref)
    if diff == 0:
        return "match"
    if diff == 1:
        return "off_by_one"
    return "mismatch"


def verify_against_crossref(
    crossref_msg: dict[str, Any],
    bib_entry: BibEntry,
) -> MetadataVerification:
    """Produce a per-field ``MetadataVerification`` between supplied and Crossref.

    The caller (``check_doi``) decides whether to run this — it only
    fires when the kind API received a BibEntry alongside the DOI.
    Missing fields on the supplied side cause that field to be skipped
    (``unchecked`` / ``None``), not flagged. Missing fields on the
    Crossref side (rare but happens for thin records) likewise skip.

    Scoring choices:

    - **Title**: token-set Jaccard under both normalised forms
      (NFKD-strip and German-phonetic). Best of the two scores wins.
      The added/removed token lists are computed from the NFKD form
      only — phonetic-form diffs would be confusing in the report.
    - **First author**: exact match on a normalised single-token
      surname (no Jaccard — too few tokens for it to be meaningful).
    - **Year**: ±1 tolerance covers online-first vs print publication
      drift; anything beyond is a mismatch.
    - **Journal / pages**: surfaced in the report for display but not
      part of the structural comparison.
    """
    cr_title = _extract_title(crossref_msg)
    cr_authors_list = _extract_authors(crossref_msg)
    cr_year = _extract_year(crossref_msg)
    cr_first_author = _first_author_surname(cr_authors_list)

    title_score: float | None = None
    title_added: list[str] = []
    title_removed: list[str] = []
    if bib_entry.title and cr_title:
        score, supplied_tokens, crossref_tokens = best_jaccard(
            bib_entry.title, cr_title
        )
        title_score = score
        title_added = sorted(crossref_tokens - supplied_tokens)
        title_removed = sorted(supplied_tokens - crossref_tokens)

    first_author_match: bool | None = None
    supplied_first: str | None = None
    if bib_entry.authors:
        supplied_first = bib_entry.authors[0]
        if cr_first_author:
            first_author_match = surname_matches(supplied_first, cr_first_author)
        else:
            first_author_match = None  # nothing on the Crossref side to check

    year_match: YearMatch = "unchecked"
    if bib_entry.year is not None and cr_year is not None:
        year_match = _classify_year_match(bib_entry.year, cr_year)

    return MetadataVerification(
        title_score=title_score,
        title_supplied=bib_entry.title,
        title_crossref=cr_title,
        title_added_tokens=title_added,
        title_removed_tokens=title_removed,
        first_author_match=first_author_match,
        first_author_supplied=supplied_first,
        first_author_crossref=cr_first_author,
        year_match=year_match,
        year_supplied=bib_entry.year,
        year_crossref=cr_year,
    )


# ---------------------------------------------------------------------------
# Crossref fetch (deliberately tiny — habanero is already a [paper] dep)
# ---------------------------------------------------------------------------


def _fetch_crossref_message(doi: str, mailto: str | None) -> dict[str, Any] | None:
    """Return Crossref's ``message`` dict for ``doi`` or ``None`` on 404.

    Raises any other transport error so the caller can surface
    ``status='check_failed'`` with the error string. ``habanero`` is
    imported lazily because the provenance module itself should remain
    importable on a stateless build (the handler does the dep check
    at boot time).

    404 detection — habanero raises ``requests.exceptions.HTTPError``
    whose ``.response.status_code`` is ``404`` when the DOI is unknown
    to Crossref. We translate that one case into ``None`` so the caller
    can map it onto ``status='unknown'`` in the report; every other
    transport error (429, 5xx, network failure) propagates and surfaces
    as ``check_failed`` with the error string preserved.
    """
    from habanero import Crossref  # noqa: PLC0415 — lazy optional dep

    cr = Crossref(mailto=mailto) if mailto else Crossref()
    try:
        result = cr.works(ids=doi)
    except Exception as exc:  # noqa: BLE001 — duck-typed 404 detection
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None) if response is not None else None
        if status_code == 404:
            return None
        raise
    if not result or "message" not in result:
        return None
    return result["message"]


# ---------------------------------------------------------------------------
# Slug helpers (DB-backed)
# ---------------------------------------------------------------------------


def _next_notice_seq(store: Store, parent_slug: str, relation: LinkRelation) -> int:
    """Return the next 1-based sequence number for a notice family.

    Probes ``ref_identifiers`` for existing ``<parent>-<letter><n>``
    slugs and returns ``max(n) + 1`` (or ``1`` if none exist). Cheap —
    the LIKE pattern hits the ``ref_identifiers`` PK and the result
    set is small in practice (one or two notices per paper).
    """
    letter = _NOTICE_LETTER[relation]
    prefix = f"{parent_slug}-{letter}"
    pattern = f"{prefix}%"
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE id_kind = 'cite_key' AND id_value LIKE %s",
            (pattern,),
        ).fetchall()
    nums: list[int] = []
    suffix_re = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for (slug,) in rows:
        m = suffix_re.match(slug)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ---------------------------------------------------------------------------
# Crossref message parsing
# ---------------------------------------------------------------------------


def _parse_iso_date(parts: list[Any] | None) -> datetime | None:
    """Convert a Crossref ``date-parts`` list ``[[YYYY, MM, DD]]`` to a datetime."""
    if not parts:
        return None
    try:
        first = parts[0]
        y = int(first[0])
        m = int(first[1]) if len(first) > 1 else 1
        d = int(first[2]) if len(first) > 2 else 1
        return datetime(y, m, d)
    except (TypeError, ValueError, IndexError):
        return None


def _extract_year(msg: dict[str, Any]) -> int | None:
    """Best-effort year extraction from a Crossref message."""
    for key in ("issued", "published-print", "published-online", "created"):
        dp = msg.get(key, {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            try:
                return int(dp[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _extract_title(msg: dict[str, Any]) -> str | None:
    """First non-empty title string from Crossref's title list."""
    titles = msg.get("title") or []
    for t in titles:
        if t and t.strip():
            return t.strip()
    return None


def _extract_authors(msg: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Author list in the same shape ``ingest/crossref.py`` produces."""
    authors: list[dict[str, Any]] = []
    for a in msg.get("author") or []:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family or given:
            authors.append({"name": ", ".join(p for p in (family, given) if p)})
            continue
        name = (a.get("name") or "").strip()
        if name:
            authors.append({"name": name})
    return authors or None


def _parse_update_entry(
    entry: dict[str, Any],
) -> tuple[LinkRelation, Severity, RetractionStatus | None, str, datetime | None] | None:
    """Parse one ``message.update-to[i]`` entry.

    Returns ``(relation, severity, status, notice_doi, notice_date)`` or
    ``None`` when the entry's ``type`` isn't in our vocabulary or the
    DOI is malformed.
    """
    raw_type = entry.get("type")
    if not raw_type:
        return None
    cls = classify_update_type(raw_type)
    if cls is None:
        return None
    severity, status, relation = cls
    if relation is None:
        return None
    notice_doi = validate_doi(entry.get("DOI") or "")
    if not notice_doi:
        return None
    notice_date = _parse_iso_date(entry.get("updated", {}).get("date-parts"))
    return relation, severity, status, notice_doi, notice_date


# ---------------------------------------------------------------------------
# Write-through to the store
# ---------------------------------------------------------------------------


def _status_tag_value(status: RetractionStatus) -> str:
    """Map the schema-enum status to the ``STATUS:*`` tag value."""
    if status == "retracted":
        return "retracted"
    if status == "expression_of_concern":
        return "concern"
    return "corrected"


def _ingest_notice_ref(
    store: Store,
    *,
    parent_slug: str,
    relation: LinkRelation,
    notice_doi: str,
    notice_msg: dict[str, Any] | None,
    seq: int,
) -> int:
    """Auto-ingest a notice DOI as a minimal paper ref.

    Returns the new ``ref_id``. ``notice_msg`` is the Crossref
    response for the notice DOI (may be ``None`` if the notice
    itself 404s, in which case we still create the ref with a
    synthetic title so the link target exists).
    """
    notice_slug = make_notice_slug(parent_slug, relation, seq)

    title = (
        _extract_title(notice_msg) if notice_msg else None
    ) or f"Notice for {parent_slug}"

    authors = _extract_authors(notice_msg) if notice_msg else None
    year = _extract_year(notice_msg) if notice_msg else None

    meta: dict[str, Any] = {
        "doi": notice_doi,
        "is_notice": True,
        "notice_relation": relation,
        "notice_parent_slug": parent_slug,
    }
    if authors is not None:
        meta["authors"] = authors
    if year is not None:
        meta["year"] = year

    with store.pool.connection() as conn:
        with conn.transaction():
            ref = store.insert_ref(
                kind="paper",
                slug=notice_slug,
                title=title,
                provider="crossref",
                meta=meta,
                conn=conn,
            )
            store.insert_ref_identifiers(
                ref.id,
                [("doi", notice_doi, "crossref")],
                conn=conn,
            )
            # STATUS:notice — flag the ref as a notice rather than a
            # primary paper, so future search calls can suppress it
            # if asked (out of scope for Phase 1 wiring; the tag is
            # the contract).
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", "notice"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
    return ref.id


def _write_through(
    store: Store,
    *,
    paper_ref_id: int,
    paper_slug: str | None,
    notices: list[Notice],
) -> tuple[RetractionStatus | None, list[Notice]]:
    """Apply the parent-paper write-through.

    For each 🔴/🟠 notice: ingest the notice ref if not present, add the
    link. Set the dominant retraction_status, STATUS tag, and touch
    retraction_checked_at. Returns ``(applied_status, notices_with_ids)``
    where the notice list is rewritten with ``persisted_ref_id``
    populated for the ones we actually wrote.

    ``paper_slug`` is required for notice slug generation; the caller
    looks it up before invoking this. (A paper ref in the store
    always has a slug — paper is a slug-addressed kind.)
    """
    if paper_slug is None:
        # Shouldn't happen for the paper kind — every paper ref carries a
        # cite_key in ref_identifiers — but guard anyway.
        return None, notices

    applied_statuses: list[RetractionStatus] = []
    rewritten: list[Notice] = []

    for notice in notices:
        if notice.status is not None:
            applied_statuses.append(notice.status)

        if notice.severity not in ("blocker", "review"):
            # Phase 1: only ingest 🔴/🟠 notices. Plain corrections
            # (🟡) and informational (🟢) entries are reported but
            # not persisted as separate refs.
            rewritten.append(notice)
            continue

        if notice.relation is None:
            rewritten.append(notice)
            continue

        # Check if we've already ingested this notice DOI (e.g. from a
        # previous provenance check on the same paper).
        existing_id = store.find_ref_by_identifier("doi", notice.notice_doi, kind="paper")

        if existing_id is None:
            seq = _next_notice_seq(store, paper_slug, notice.relation)
            notice_msg: dict[str, Any] | None = {
                "title": [notice.notice_title] if notice.notice_title else [],
                "author": notice.notice_authors or [],
            }
            if notice.notice_year is not None:
                notice_msg["issued"] = {"date-parts": [[notice.notice_year]]}
            notice_ref_id = _ingest_notice_ref(
                store,
                parent_slug=paper_slug,
                relation=notice.relation,
                notice_doi=notice.notice_doi,
                notice_msg=notice_msg,
                seq=seq,
            )
        else:
            notice_ref_id = existing_id

        # Link parent → notice (idempotent on the unique tuple).
        store.add_link(
            src_ref_id=paper_ref_id,
            dst_ref_id=notice_ref_id,
            relation=notice.relation,
            set_by="system",
        )

        # Re-pack notice with persisted ref id so the renderer can
        # show the local slug.
        rewritten.append(
            Notice(
                update_type=notice.update_type,
                severity=notice.severity,
                status=notice.status,
                relation=notice.relation,
                notice_doi=notice.notice_doi,
                notice_date=notice.notice_date,
                notice_title=notice.notice_title,
                notice_authors=notice.notice_authors,
                notice_year=notice.notice_year,
                persisted_ref_id=notice_ref_id,
            )
        )

    applied = dominant_status(applied_statuses)

    # Earliest retraction date among status-bearing notices, for the
    # ``refs.retracted_at`` column. Phase 1 stores the earliest known
    # notice date; if none of the notices carried a date we leave it
    # NULL — the column is informational, not load-bearing.
    earliest_date: datetime | None = None
    for n in rewritten:
        if n.notice_date is None or n.status is None:
            continue
        if earliest_date is None or n.notice_date < earliest_date:
            earliest_date = n.notice_date

    # Build the retraction_reason summary string — Phase 1 is just the
    # notice DOIs joined; Phase 3 (RW reasons) replaces this with the
    # human-readable reason taxonomy.
    notice_dois = [n.notice_doi for n in rewritten if n.status is not None]
    reason = "; ".join(notice_dois) if notice_dois else None
    url = None
    if notice_dois:
        url = f"https://doi.org/{notice_dois[0]}"

    store.set_retraction_status(
        paper_ref_id,
        status=applied,
        retracted_at=earliest_date,
        reason=reason,
        url=url,
    )

    if applied is not None:
        store.add_tag(
            paper_ref_id,
            Tag.closed("STATUS", _status_tag_value(applied)),
            set_by="system",
            replace_prefix=True,
        )

    return applied, rewritten


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_doi(
    doi: str,
    *,
    store: Store | None = None,
    mailto: str | None = None,
    bib_entry: BibEntry | None = None,
) -> ProvenanceResult:
    """Run a single-DOI provenance check.

    ``store`` is optional: when ``None`` (or when the DOI isn't in the
    store), the result is informational only — no rows are written.
    When the parent paper *is* in the store, we write through:
    notice refs are created for 🔴/🟠 notices, links are attached, the
    ``refs.retraction_*`` columns are updated, and a closed-namespace
    ``STATUS:*`` tag is applied.

    ``bib_entry`` is the Phase 2.5 metadata-verification hook. When
    supplied, the function compares the caller's bibliographic claim
    against the Crossref record and populates
    ``ProvenanceResult.verification`` with per-field results. When
    ``None``, verification is skipped and the result.verification
    stays ``None`` (no extra cost).

    Failure modes:
    - DOI doesn't match the format → ``status='malformed'``
    - Crossref returns no record → ``status='unknown'``
    - Network / transport error → ``status='check_failed'`` with the
      error string in ``.error``
    """
    canonical = validate_doi(doi)
    if canonical is None:
        return ProvenanceResult(doi=doi.strip().lower(), status="malformed")

    try:
        msg = _fetch_crossref_message(canonical, mailto)
    except Exception as exc:  # noqa: BLE001 — surface any transport error uniformly
        return ProvenanceResult(doi=canonical, status="check_failed", error=str(exc))

    if msg is None:
        return ProvenanceResult(doi=canonical, status="unknown")

    paper_title = _extract_title(msg)
    paper_authors = _extract_authors(msg)
    paper_year = _extract_year(msg)

    verification: MetadataVerification | None = None
    if bib_entry is not None:
        verification = verify_against_crossref(msg, bib_entry)

    notices: list[Notice] = []
    for entry in msg.get("update-to") or []:
        parsed = _parse_update_entry(entry)
        if parsed is None:
            continue
        relation, severity, status, notice_doi, notice_date = parsed
        notices.append(
            Notice(
                update_type=str(entry.get("type") or ""),
                severity=severity,
                status=status,
                relation=relation,
                notice_doi=notice_doi,
                notice_date=notice_date,
                notice_title=entry.get("label") or None,
                notice_authors=None,
                notice_year=None,
            )
        )

    # Sort chronologically — earliest first — so the report iterates in
    # the natural reading order.
    notices.sort(key=lambda n: (n.notice_date or datetime.min))

    # Phase 3: enrich notices with Retraction Watch reasons from the
    # local cache. Cheap when there's no store (no-op) or no cache
    # row for this DOI (one read returning zero rows).
    if store is not None and notices:
        cache_rows = _lookup_rw_cache(store, canonical)
        notices = _enrich_notices_with_rw(notices, cache_rows)

    paper_in_store = False
    paper_ref_id: int | None = None
    applied: RetractionStatus | None = None
    final_notices = notices

    if store is not None:
        paper_ref_id = store.find_ref_by_identifier("doi", canonical, kind="paper")
        paper_in_store = paper_ref_id is not None
        if paper_in_store and paper_ref_id is not None:
            parent_ref = store.get_ref(kind="paper", id=paper_ref_id)
            paper_slug = parent_ref.slug if parent_ref is not None else None
            applied, final_notices = _write_through(
                store,
                paper_ref_id=paper_ref_id,
                paper_slug=paper_slug,
                notices=notices,
            )

    return ProvenanceResult(
        doi=canonical,
        status="ok",
        paper_title=paper_title,
        paper_authors=paper_authors,
        paper_year=paper_year,
        notices=final_notices,
        applied_status=applied,
        paper_ref_id=paper_ref_id,
        paper_in_store=paper_in_store,
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Batch input parsing
# ---------------------------------------------------------------------------


# Tokens that are clearly not DOIs but commonly appear in DOI-per-line
# input files: blank lines, comments, list markers. We strip these
# before passing the rest to ``validate_doi``.
_COMMENT_RE = re.compile(r"^\s*#")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")


def parse_doi_list(raw: str) -> list[str]:
    """Split a batch input string into candidate DOI tokens.

    Accepts any of:

    - ``"10.x/a,10.x/b,10.x/c"`` — comma-separated (the canonical
      ``q='...'`` form for the kind API)
    - whitespace-separated tokens
    - newline-separated tokens (the ``--refs preflight.txt`` form on
      the CLI)
    - mixed separators

    Strips:
    - empty / whitespace-only entries
    - lines starting with ``#`` (comments)
    - leading list markers (``- ``, ``* ``, ``+ ``)
    - surrounding whitespace on each token

    Does **not** validate DOI shape — that's ``validate_doi``'s job
    inside ``check_doi``. The split is deliberately permissive so the
    report can show ``status='malformed'`` for tokens that look DOI-ish
    but aren't, alongside the rest of the batch.

    Order is preserved (callers may want to render results in input
    order); duplicates are kept too — the user may have a real reason
    to check a DOI twice in one batch, and de-dup is cheap to do
    on the caller side if wanted.
    """
    tokens: list[str] = []
    for raw_line in raw.splitlines() if "\n" in raw else [raw]:
        # Drop comment lines wholesale.
        if _COMMENT_RE.match(raw_line):
            continue
        # Strip a leading bullet marker if present.
        line = _BULLET_RE.sub("", raw_line)
        # Split on comma + whitespace so both shapes work in one pass.
        for chunk in re.split(r"[,\s]+", line):
            t = chunk.strip()
            if t:
                tokens.append(t)
    return tokens


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------


# Conservative default — Crossref's polite pool tolerates higher
# concurrency but the cost of going higher is marginal for typical
# preflight batch sizes (≤300 DOIs). Tuned in the plan §"Concurrency /
# rate".
_DEFAULT_MAX_WORKERS = 8


def check_dois(
    dois: Iterable[str],
    *,
    store: Store | None = None,
    mailto: str | None = None,
    max_workers: int = _DEFAULT_MAX_WORKERS,
    bib_entries: list[BibEntry] | None = None,
) -> list[ProvenanceResult]:
    """Run ``check_doi`` over a batch of DOIs concurrently.

    Returns results in the same order as the input — order matters for
    the preflight report (the user can map each line to a bib entry).
    Concurrency comes from a thread pool, not asyncio, because the
    underlying ``habanero`` client is synchronous and switching to
    asyncio would require dragging ``httpx`` in. With the default
    ``max_workers=8`` a 250-DOI batch finishes in ~30s warm / ~90s cold
    against the Crossref polite pool.

    ``bib_entries`` is the Phase 2.5 metadata-verify hook. When
    supplied, it must have the same length as ``dois`` (positional
    pairing — entry[i] verifies dois[i]). When ``None``, no
    verification runs. Mismatched lengths raise ``ValueError`` because
    the alternative — silently dropping entries or DOIs — would mask
    a caller bug into a meaningless report.

    Failure isolation: any per-DOI exception surfaces as
    ``status='check_failed'`` on that result (already true for the
    single-DOI path), so a single transport hiccup doesn't kill the
    batch. The caller never sees an exception.
    """
    inputs = list(dois)
    if not inputs:
        return []

    if bib_entries is not None and len(bib_entries) != len(inputs):
        raise ValueError(
            f"check_dois: bib_entries length {len(bib_entries)} != dois "
            f"length {len(inputs)} — positional pairing requires equal "
            "lengths; pad with BibEntry(doi=...) for unchecked entries"
        )

    def _entry_for(i: int) -> BibEntry | None:
        return bib_entries[i] if bib_entries is not None else None

    # Single-DOI fast path — skip the thread-pool overhead. We still
    # set ``input_index=1`` so the result is interchangeable with
    # any other batch result (Phase 3.5: standardised LLM output).
    if len(inputs) == 1:
        r = check_doi(
            inputs[0],
            store=store,
            mailto=mailto,
            bib_entry=_entry_for(0),
        )
        return [replace(r, input_index=1)]

    results: list[ProvenanceResult | None] = [None] * len(inputs)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(
                check_doi,
                doi,
                store=store,
                mailto=mailto,
                bib_entry=_entry_for(i),
            ): i
            for i, doi in enumerate(inputs)
        }
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                # input_index is 1-based and reflects *input* order,
                # not thread-pool completion order — that's the whole
                # point of Phase 3.5.
                results[i] = replace(fut.result(), input_index=i + 1)
            except Exception as exc:  # noqa: BLE001 — preserve batch isolation
                # ``check_doi`` already catches transport errors internally,
                # so reaching this branch implies a bug. Surface as
                # ``check_failed`` so the batch still completes.
                results[i] = ProvenanceResult(
                    doi=inputs[i].strip().lower(),
                    status="check_failed",
                    error=f"unexpected error in worker: {exc}",
                    input_index=i + 1,
                )

    # ``results`` is fully populated by here (one entry per future).
    return [r for r in results if r is not None]


__all__ = [
    "BibEntry",
    "LinkRelation",
    "MetadataVerification",
    "Notice",
    "ProvenanceResult",
    "RetractionStatus",
    "Severity",
    "YearMatch",
    "check_doi",
    "check_dois",
    "classify_update_type",
    "dominant_status",
    "make_notice_slug",
    "parse_doi_list",
    "validate_doi",
    "verify_against_crossref",
]
