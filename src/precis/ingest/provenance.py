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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal

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
) -> ProvenanceResult:
    """Run a single-DOI provenance check.

    ``store`` is optional: when ``None`` (or when the DOI isn't in the
    store), the result is informational only — no rows are written.
    When the parent paper *is* in the store, we write through:
    notice refs are created for 🔴/🟠 notices, links are attached, the
    ``refs.retraction_*`` columns are updated, and a closed-namespace
    ``STATUS:*`` tag is applied.

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
) -> list[ProvenanceResult]:
    """Run ``check_doi`` over a batch of DOIs concurrently.

    Returns results in the same order as the input — order matters for
    the preflight report (the user can map each line to a bib entry).
    Concurrency comes from a thread pool, not asyncio, because the
    underlying ``habanero`` client is synchronous and switching to
    asyncio would require dragging ``httpx`` in. With the default
    ``max_workers=8`` a 250-DOI batch finishes in ~30s warm / ~90s cold
    against the Crossref polite pool.

    Failure isolation: any per-DOI exception surfaces as
    ``status='check_failed'`` on that result (already true for the
    single-DOI path), so a single transport hiccup doesn't kill the
    batch. The caller never sees an exception.
    """
    inputs = list(dois)
    if not inputs:
        return []

    # Single-DOI fast path — skip the thread-pool overhead.
    if len(inputs) == 1:
        return [check_doi(inputs[0], store=store, mailto=mailto)]

    results: list[ProvenanceResult | None] = [None] * len(inputs)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(check_doi, doi, store=store, mailto=mailto): i
            for i, doi in enumerate(inputs)
        }
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # noqa: BLE001 — preserve batch isolation
                # ``check_doi`` already catches transport errors internally,
                # so reaching this branch implies a bug. Surface as
                # ``check_failed`` so the batch still completes.
                results[i] = ProvenanceResult(
                    doi=inputs[i].strip().lower(),
                    status="check_failed",
                    error=f"unexpected error in worker: {exc}",
                )

    # ``results`` is fully populated by here (one entry per future).
    return [r for r in results if r is not None]


__all__ = [
    "LinkRelation",
    "Notice",
    "ProvenanceResult",
    "RetractionStatus",
    "Severity",
    "check_doi",
    "check_dois",
    "classify_update_type",
    "dominant_status",
    "make_notice_slug",
    "parse_doi_list",
    "validate_doi",
]
