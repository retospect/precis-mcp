"""``fetch_google_patents`` — fall-back full-text fetcher via patents.google.com.

The OPS sweep (:mod:`precis.jobs.patent_fulltext_sweep`) handles EP
publications well, but US and CN applications routinely 404 on OPS's
description / claims endpoints for the first several months after
publication. Eventually the sweep gives up and stamps
``fulltext-unavailable`` — leaving us with biblio+abstract but no
searchable body. patents.google.com aggregates description+claims for
basically every jurisdiction and serves them in stable structured HTML,
so we use it as a one-shot fall-back: try once, mark the patent, move
on.

Claim shape (``_claim_patents_for_gp``):

* ``kind='patent'`` AND ``deleted_at IS NULL``
* Tagged ``awaiting-fulltext`` OR ``fulltext-unavailable``
  (OPS either still missing OR gave up)
* Not tagged ``gp-attempted`` (one try per patent — manual ``--force``
  CLI clears the tag to retry)

Per-patent outcome:

* **fetched** — at least one block inserted; ``gp-fetched`` tag added;
  if the previously-set ``awaiting-fulltext`` / ``fulltext-unavailable``
  tags now have a fulltext source, those tags drop.
* **not-found** — HTTP 404 on patents.google.com; ``gp-attempted`` +
  ``gp-not-found`` open tags added so the dashboard surfaces the
  terminal miss.
* **parse-error** — page loaded but no abstract/description/claims
  section matched; ``gp-attempted`` + ``gp-parse-error`` tags added so
  a human can inspect.

Every outcome stamps ``meta.gp_attempted_at`` (ISO) and
``meta.gp_status``, so the CLI report can show what happened without
re-walking the tag table.
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta as _timedelta
from typing import TYPE_CHECKING, Any

from precis.store import Tag
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


_GP_BASE_URL: str = "https://patents.google.com/patent"
_USER_AGENT: str = "precis-mcp/1.0 (+patents.google.com fallback)"

#: Per-pass cap. The worker idle-sleeps when nothing is due, so a low
#: cap keeps the wall-clock per cycle bounded without leaving work
#: stranded — overflow re-surfaces next cycle.
DEFAULT_GP_LIMIT: int = 10

#: Open-tag values. ``gp-attempted`` is the once-and-done marker; the
#: status tags split the dashboard view.
GP_ATTEMPTED_TAG: str = "gp-attempted"
GP_FETCHED_TAG: str = "gp-fetched"
GP_NOT_FOUND_TAG: str = "gp-not-found"
GP_PARSE_ERROR_TAG: str = "gp-parse-error"
GP_HTTP_GAVE_UP_TAG: str = "gp-http-gave-up"

#: Exponential backoff schedule for HTTP errors against
#: patents.google.com. Minutes per attempt; after the last entry,
#: the patent is marked gp-attempted + gp-http-gave-up and drops out
#: of the retry pool. Index 0 is the *first* retry delay (after the
#: first http-error), so a never-seen patent starts with no
#: ``gp_retry_count`` and is immediately eligible.
_RETRY_DELAY_MINUTES: tuple[int, ...] = (5, 15, 60, 360, 1440)

#: Awaiting / unavailable tag values — duplicated from
#: :mod:`precis.handlers._patent_ingest` so this module can run without
#: importing the ingest pipeline. Drift is caught by a smoke test that
#: imports both modules and asserts equality.
_AWAITING_TAG: str = "awaiting-fulltext"
_UNAVAILABLE_TAG: str = "fulltext-unavailable"


# ─── HTML parsing ──────────────────────────────────────────────────────────

# Patents.google.com HTML is rendered server-side as a single document
# with three ``<section itemprop="...">`` blocks per language. We slice
# each section out by attribute then re-extract textual children.

_SECTION_RE = re.compile(
    r'<section\b[^>]*?\bitemprop="(?P<ip>abstract|description|claims)"'
    r"[^>]*>(?P<body>.*?)</section>",
    re.DOTALL | re.IGNORECASE,
)

# Inside ``<section itemprop="description">``:
#   <heading id="h-0001">SUMMARY OF THE INVENTION</heading>
#   <div class="description-paragraph" id="p-0001" num="0001">
#       Body paragraph text...
#   </div>
_DESC_PARA_RE = re.compile(
    r'<div\b[^>]*\bclass="(?:[^"]*\s)?description-paragraph(?:\s[^"]*)?"'
    r"[^>]*>(.*?)</div>",
    re.DOTALL,
)
_HEADING_RE = re.compile(
    r"<heading\b[^>]*>(.*?)</heading>",
    re.DOTALL,
)

# Inside ``<section itemprop="claims">``:
#   <claim id="CLM-00001" num="00001">
#       <claim-text>1. A method for ...</claim-text>
#       <claim-text>wherein ...</claim-text>
#   </claim>
_CLAIM_RE = re.compile(
    r"<claim\b[^>]*>(.*?)</claim>",
    re.DOTALL,
)

# Generic tag-strip after slicing.
_TAG_RE = re.compile(r"<[^>]+>")

# Whitespace collapse — runs of any whitespace → single space.
_WS_RUN_RE = re.compile(r"\s+")
# For section-dump fallback only: split on blank-line runs to preserve
# paragraph structure before per-block collapse.
_BLANK_RUN_RE = re.compile(r"\n\s*\n+")


def _block_text(raw: str) -> str:
    """Strip tags, decode entities, collapse ALL whitespace to single spaces.

    Used for individual <div class="description-paragraph"> and <claim>
    matches — each one is semantically a single paragraph; the HTML
    layout newlines inside aren't part of the content.
    """
    text = _TAG_RE.sub(" ", raw)
    text = _html.unescape(text)
    return _WS_RUN_RE.sub(" ", text).strip()


def _section_dump(raw: str) -> list[str]:
    """Split a whole-section blob into per-paragraph strings.

    Fallback path when the structured per-paragraph selectors don't
    match. We honour blank-line runs as paragraph breaks; otherwise
    each line becomes its own block. Per-block whitespace runs collapse
    to single spaces via :func:`_block_text`.
    """
    text = _TAG_RE.sub(" ", raw)
    text = _html.unescape(text)
    # Honour blank-line paragraph breaks but be tolerant: a "paragraph"
    # may have internal layout newlines that should fold.
    paragraphs = _BLANK_RUN_RE.split(text)
    out: list[str] = []
    for p in paragraphs:
        collapsed = _WS_RUN_RE.sub(" ", p).strip()
        if collapsed:
            out.append(collapsed)
    return out


@dataclass
class ParsedGpPatent:
    """Outcome of parsing one patents.google.com HTML page."""

    abstract: str | None
    description_paragraphs: list[str]
    claim_texts: list[str]

    @property
    def is_empty(self) -> bool:
        return (
            self.abstract is None
            and not self.description_paragraphs
            and not self.claim_texts
        )


def parse_google_patent_html(html: str) -> ParsedGpPatent:
    """Parse patents.google.com HTML into abstract + description + claims.

    Tolerant to absent sections — any missing component returns ``None``
    or ``[]``. The caller decides whether the result counts as a
    successful fetch (current rule: at least one of description/claims
    populated, OR an abstract longer than the OPS-supplied one).
    """
    abstract: str | None = None
    desc_blocks: list[str] = []
    claim_blocks: list[str] = []

    for m in _SECTION_RE.finditer(html):
        ip = m.group("ip").lower()
        body = m.group("body")

        if ip == "abstract":
            cleaned = _block_text(body)
            # Strip the leading "Abstract" heading the section H2
            # introduces. The headings appear as plain text after tag-
            # strip, so a one-shot prefix-trim is enough.
            if cleaned.lower().startswith("abstract"):
                cleaned = cleaned[len("abstract") :].lstrip(" \t:-—")
            if cleaned and (abstract is None or len(cleaned) > len(abstract)):
                # Sections may repeat per-language; keep the longest.
                abstract = cleaned

        elif ip == "description":
            # Splice headings + paragraphs in source order so the
            # rendered body reads top-to-bottom. We walk the body once
            # and emit each match's text in order.
            for cm in re.finditer(
                rf"({_DESC_PARA_RE.pattern})|({_HEADING_RE.pattern})",
                body,
                re.DOTALL,
            ):
                groups = cm.groups()
                # Combined alternation captures: groups[0] = entire desc
                # div, groups[1] = its inner content; groups[2] = entire
                # heading element, groups[3] = its inner content.
                if cm.group(1) is not None:
                    text = _block_text(groups[1] or "")
                    if text:
                        desc_blocks.append(text)
                else:
                    text = _block_text(groups[3] or "")
                    if text:
                        # Render headings prefixed with `# ` so the
                        # chunker's section-detection (and the
                        # rendered view) can spot them.
                        desc_blocks.append(f"# {text}")

            # Fallback: no structured paragraphs — fall back to a
            # whole-section text dump split on blank lines.
            if not desc_blocks:
                desc_blocks = _section_dump(body)

        elif ip == "claims":
            for cm in _CLAIM_RE.finditer(body):
                text = _block_text(cm.group(1))
                if text:
                    claim_blocks.append(text)
            # Fallback for older / oddly-shaped pages: whole-section text.
            if not claim_blocks:
                claim_blocks = _section_dump(body)

    return ParsedGpPatent(
        abstract=abstract,
        description_paragraphs=desc_blocks,
        claim_texts=claim_blocks,
    )


# ─── Claim query ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PatentCandidate:
    """One patent due for a Google Patents fetch attempt."""

    ref_id: int
    cite_key: str  # DOCDB slug — used to build the URL
    status_tag: str  # awaiting-fulltext | fulltext-unavailable


def _claim_patents_for_gp(
    store: Store, *, limit: int, force: bool = False
) -> list[_PatentCandidate]:
    """Return up to ``limit`` patents needing a Google Patents try.

    Match criteria:
    - ``kind='patent'`` AND ``deleted_at IS NULL``
    - Carries either the awaiting-fulltext or fulltext-unavailable open tag
    - Does NOT carry the gp-attempted open tag (unless ``force=True``)

    Ordered oldest-pub-date-first so we attack the long tail of stuck
    publications before the recent ones whose OPS retry may still land.
    ``FOR UPDATE OF r SKIP LOCKED`` lets multiple workers run in parallel.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    gp_filter = "" if force else (
        "AND NOT EXISTS ("
        "  SELECT 1 FROM ref_tags rt2 "
        "  JOIN tags t2 ON t2.tag_id = rt2.tag_id "
        "  WHERE rt2.ref_id = r.ref_id "
        "    AND t2.namespace = 'OPEN' "
        "    AND t2.value = %s"
        ") "
        # Skip patents in HTTP-error backoff — gp_retry_at is in the
        # future. NULL means no prior failure, so the predicate stays
        # tolerant.
        "AND (r.meta->>'gp_retry_at' IS NULL "
        "     OR (r.meta->>'gp_retry_at')::timestamptz <= now()) "
    )
    sql = f"""
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
               t.value AS status_tag
        FROM   refs r
        JOIN   ref_tags rt ON rt.ref_id = r.ref_id
        JOIN   tags t       ON t.tag_id  = rt.tag_id
        WHERE  r.kind = 'patent'
          AND  r.deleted_at IS NULL
          AND  t.namespace = 'OPEN'
          AND  t.value IN (%s, %s)
          {gp_filter}
        ORDER  BY (r.meta->>'publication_date') ASC NULLS FIRST,
                  r.ref_id ASC
        LIMIT  %s
        FOR UPDATE OF r SKIP LOCKED
    """
    params: list[Any] = [_AWAITING_TAG, _UNAVAILABLE_TAG]
    if not force:
        params.append(GP_ATTEMPTED_TAG)
    params.append(limit)

    with store.pool.connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    return [
        _PatentCandidate(
            ref_id=int(r[0]),
            cite_key=str(r[1] or ""),
            status_tag=str(r[2] or ""),
        )
        for r in rows
        if r[1]  # skip rows whose cite_key disappeared — can't build URL
    ]


# ─── Per-patent processing ─────────────────────────────────────────────────


@dataclass(slots=True)
class GpFetchOutcome:
    """One patent's outcome from a Google Patents fetch."""

    slug: str
    status: str  # 'fetched' | 'not-found' | 'parse-error' | 'http-error' | 'skipped'
    blocks_added: int = 0
    error: str | None = None
    bytes_fetched: int = 0


def _build_url(cite_key: str) -> str:
    """Return the patents.google.com URL for a DOCDB cite_key.

    ``cite_key`` is the lower-case DOCDB slug we store internally
    (e.g. ``us20210123456a1``). patents.google.com is case-insensitive
    on the path component but we upper-case for stability with their
    canonical URLs.
    """
    return f"{_GP_BASE_URL}/{cite_key.upper()}/en"


def _fetch_one(slug: str) -> tuple[str, str | None, int]:
    """Fetch the HTML for ``slug``. Returns (status, html_or_None, byte_count).

    status is one of: 'ok' / 'not-found' / 'http-error'. We don't surface
    the raw HTTP code — callers want the outcome bucket.
    """
    import httpx

    from precis.utils.safe_fetch import safe_get

    url = _build_url(slug)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
    try:
        with httpx.Client(
            timeout=30.0, headers=headers, follow_redirects=False
        ) as client:
            resp = safe_get(client, url)
    except httpx.HTTPError as exc:
        return "http-error", f"transport: {exc}", 0

    if resp.status_code == 404:
        return "not-found", None, len(resp.content)
    if resp.status_code != 200:
        return "http-error", f"HTTP {resp.status_code}", len(resp.content)

    return "ok", resp.text, len(resp.content)


def _record_attempt(
    store: Store,
    *,
    ref_id: int,
    status: str,
    blocks_added: int,
    now: datetime,
    error: str | None = None,
) -> None:
    """Stamp ``gp-attempted`` + status tag + ``meta.gp_*`` fields."""
    meta_patch: dict[str, Any] = {
        "gp_attempted_at": now.isoformat(),
        "gp_status": status,
        "gp_blocks_added": blocks_added,
    }
    if error is not None:
        meta_patch["gp_error"] = error
    store.update_ref(ref_id=ref_id, meta_patch=meta_patch)

    _ensure_tag(store, ref_id=ref_id, value=GP_ATTEMPTED_TAG)
    status_tag = {
        "fetched": GP_FETCHED_TAG,
        "not-found": GP_NOT_FOUND_TAG,
        "parse-error": GP_PARSE_ERROR_TAG,
    }.get(status)
    if status_tag is not None:
        _ensure_tag(store, ref_id=ref_id, value=status_tag)


def _ensure_tag(store: Store, *, ref_id: int, value: str) -> None:
    """Best-effort add of an OPEN tag; idempotent on duplicate."""
    try:
        store.add_tag(ref_id, Tag.open(value), set_by="system")
    except Exception:
        log.warning("fetch_google_patents: failed to apply tag %s to %s", value, ref_id)


def _drop_obsolete_status_tags(store: Store, *, ref_id: int) -> None:
    """Once Google Patents fills in the body, the awaiting/unavailable
    tags no longer reflect reality — the patent has full text again,
    just from a different source."""
    for value in (_AWAITING_TAG, _UNAVAILABLE_TAG):
        try:
            store.remove_tag(ref_id, Tag.open(value))
        except Exception:
            # Best-effort — a stuck tag doesn't block search of the
            # newly-ingested blocks.
            log.debug(
                "fetch_google_patents: tag %s not present on %s (ok)", value, ref_id
            )


def _fetch_and_ingest(
    store: Store,
    candidate: _PatentCandidate,
    *,
    now: datetime,
    dry_run: bool,
) -> GpFetchOutcome:
    """Run the full fetch + parse + insert pipeline for one patent."""
    outcome = GpFetchOutcome(slug=candidate.cite_key, status="skipped")

    if dry_run:
        log.info(
            "fetch_google_patents[%s]: dry-run - would fetch (%s)",
            candidate.cite_key,
            candidate.status_tag,
        )
        return outcome

    status, html_or_err, byte_count = _fetch_one(candidate.cite_key)
    outcome.bytes_fetched = byte_count

    if status == "not-found":
        outcome.status = "not-found"
        _record_attempt(
            store,
            ref_id=candidate.ref_id,
            status="not-found",
            blocks_added=0,
            now=now,
        )
        log.info("fetch_google_patents[%s]: 404 on patents.google.com", candidate.cite_key)
        return outcome

    if status == "http-error":
        outcome.status = "http-error"
        outcome.error = html_or_err
        # Exponential backoff: bump gp_retry_count, stamp the next
        # gp_retry_at. After exhausting the schedule we give up and
        # mark gp-attempted + gp-http-gave-up so the patent drops out
        # of the retry pool.
        ref = store.get_ref(kind="patent", id=candidate.cite_key)
        prior_count = 0
        if ref is not None:
            prior_count = int((ref.meta or {}).get("gp_retry_count", 0) or 0)
        next_count = prior_count + 1

        if next_count > len(_RETRY_DELAY_MINUTES):
            # Out of retries — terminal.
            _record_attempt(
                store,
                ref_id=candidate.ref_id,
                status="http-error",
                blocks_added=0,
                now=now,
                error=f"gave up after {prior_count} retries: {html_or_err}",
            )
            _ensure_tag(store, ref_id=candidate.ref_id, value=GP_HTTP_GAVE_UP_TAG)
            log.warning(
                "fetch_google_patents[%s]: http-error - gave up after %d retries: %s",
                candidate.cite_key,
                prior_count,
                html_or_err,
            )
            outcome.status = "http-gave-up"
            return outcome

        delay = _RETRY_DELAY_MINUTES[next_count - 1]
        next_at = now + _timedelta(minutes=delay)
        store.update_ref(
            ref_id=candidate.ref_id,
            meta_patch={
                "gp_retry_at": next_at.isoformat(),
                "gp_retry_count": next_count,
                "gp_last_error": html_or_err,
            },
        )
        log.warning(
            "fetch_google_patents[%s]: http-error #%d: %s (retry in %dm)",
            candidate.cite_key,
            next_count,
            html_or_err,
            delay,
        )
        return outcome

    assert html_or_err is not None
    parsed = parse_google_patent_html(html_or_err)

    if parsed.is_empty:
        outcome.status = "parse-error"
        outcome.error = "no section matched"
        _record_attempt(
            store,
            ref_id=candidate.ref_id,
            status="parse-error",
            blocks_added=0,
            now=now,
            error=outcome.error,
        )
        log.warning(
            "fetch_google_patents[%s]: parse-error - no sections matched",
            candidate.cite_key,
        )
        return outcome

    # Build BlockInsert list. Description blocks land first (chunk_kind=
    # 'patent_section'), then claims (chunk_kind='patent_claim'). The
    # abstract goes into meta rather than as a chunk — the existing
    # OPS biblio meta column already houses it and the patent renderer
    # reads from there.
    ref = store.get_ref(kind="patent", id=candidate.cite_key)
    if ref is None:
        outcome.status = "parse-error"
        outcome.error = "ref disappeared mid-fetch"
        log.warning(
            "fetch_google_patents[%s]: ref vanished between claim and ingest",
            candidate.cite_key,
        )
        return outcome

    offset = store.count_blocks(ref.id)
    inserts: list[BlockInsert] = []

    for i, text in enumerate(parsed.description_paragraphs):
        inserts.append(
            BlockInsert(
                pos=offset + i,
                text=text,
                meta={"chunk_kind": "patent_section", "source": "patents.google.com"},
            )
        )
    desc_count = len(parsed.description_paragraphs)
    for j, text in enumerate(parsed.claim_texts):
        inserts.append(
            BlockInsert(
                pos=offset + desc_count + j,
                text=text,
                meta={"chunk_kind": "patent_claim", "source": "patents.google.com"},
            )
        )

    if inserts:
        store.insert_blocks(ref.id, inserts)

    # Update meta with the abstract (only if longer than what OPS gave
    # us) + the has_* flags.
    existing_meta = ref.meta or {}
    meta_patch: dict[str, Any] = {
        "gp_attempted_at": now.isoformat(),
        "gp_status": "fetched",
        "gp_blocks_added": len(inserts),
        "gp_source_url": _build_url(candidate.cite_key),
    }
    if parsed.description_paragraphs:
        meta_patch["has_description"] = True
    if parsed.claim_texts:
        meta_patch["has_claims"] = True
    existing_abstract = (existing_meta.get("abstract") or "").strip()
    if parsed.abstract and len(parsed.abstract) > len(existing_abstract):
        meta_patch["abstract"] = parsed.abstract
    # Success clears any prior retry bookkeeping so the row's meta
    # doesn't carry a ghost backoff timestamp forever.
    if "gp_retry_at" in existing_meta:
        meta_patch["gp_retry_at"] = None
    if "gp_retry_count" in existing_meta:
        meta_patch["gp_retry_count"] = None
    store.update_ref(ref_id=ref.id, meta_patch=meta_patch)

    # Now flip tags: gp-attempted + gp-fetched on; awaiting/unavailable off.
    _ensure_tag(store, ref_id=ref.id, value=GP_ATTEMPTED_TAG)
    _ensure_tag(store, ref_id=ref.id, value=GP_FETCHED_TAG)
    _drop_obsolete_status_tags(store, ref_id=ref.id)

    outcome.status = "fetched"
    outcome.blocks_added = len(inserts)
    log.info(
        "fetch_google_patents[%s]: fetched (+%d blocks)",
        candidate.cite_key,
        len(inserts),
    )
    return outcome


# ─── Public entry points ───────────────────────────────────────────────────


def run_gp_fetch_pass(
    store: Store,
    *,
    limit: int = DEFAULT_GP_LIMIT,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one pass over patents needing a Google Patents fallback fetch.

    Returns a count dict matching the ``BatchResult`` shape expected by
    the worker runner: ``{'claimed', 'ok', 'failed'}``. The 'ok' bucket
    counts both ``fetched`` *and* ``not-found`` outcomes — both are
    terminal states from the worker's perspective; only transient
    HTTP errors (which re-surface next pass) count as 'failed'.

    Gated by env: when ``PRECIS_GP_FETCH`` is unset or ``"0"``, the
    pass exits immediately with claimed=0. This mirrors the other
    opt-in passes (chase LLM, dream agent, …) so a default-on system
    profile doesn't burn the patents.google.com goodwill budget on
    every cluster node.
    """
    if not _is_enabled():
        return {"claimed": 0, "ok": 0, "failed": 0}

    if now is None:
        now = datetime.now(UTC)

    candidates = _claim_patents_for_gp(store, limit=limit, force=force)
    if not candidates:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = 0
    failed = 0
    for c in candidates:
        try:
            outcome = _fetch_and_ingest(store, c, now=now, dry_run=dry_run)
        except Exception as exc:
            log.exception(
                "fetch_google_patents[%s]: unhandled error", c.cite_key
            )
            failed += 1
            _record_attempt(
                store,
                ref_id=c.ref_id,
                status="parse-error",
                blocks_added=0,
                now=now,
                error=str(exc)[:200],
            )
            continue
        if outcome.status in ("fetched", "not-found", "skipped"):
            ok += 1
        else:
            failed += 1

    return {"claimed": len(candidates), "ok": ok, "failed": failed}


def _is_enabled() -> bool:
    """Env gate. Default off."""
    return os.environ.get("PRECIS_GP_FETCH", "0").lower() in ("1", "true", "yes")


__all__ = [
    "DEFAULT_GP_LIMIT",
    "GP_ATTEMPTED_TAG",
    "GP_FETCHED_TAG",
    "GP_NOT_FOUND_TAG",
    "GP_PARSE_ERROR_TAG",
    "GpFetchOutcome",
    "ParsedGpPatent",
    "parse_google_patent_html",
    "run_gp_fetch_pass",
]
