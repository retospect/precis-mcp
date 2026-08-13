"""bib_parse — per-paper bibliography parse + identity match
(``paper_bib_entries``, ``citation-bib-parse`` (git-only)).

Self-contained ref-pass (shaped like ``classify``/``paper_glossary`` — DB
reads + outbound LLM/Crossref calls, not a pure ``WorkerHandler``). For
each claimed paper it:

  1. **Detects** bibliography-shaped chunks by *content*, not
     ``chunk_kind`` (a chunk qualifies when most of its non-empty lines
     look like ``- [N] ...``; ``chunk_kind='references'`` chunks always
     qualify). Deliberate defense-in-depth, not a workaround: the ingest
     classifier's own retag (``utils/boilerplate.py``, gr196447/gr196690)
     catches new ingests, but pre-fix chunks are still
     ``chunk_kind='paragraph'`` corpus-wide — this detector works on that
     backlog regardless (``bib_retag`` is the mutating remediation). The
     per-line regex is ``boilerplate.MARKER_LINE_RE``, shared so the two
     detectors can't drift on what a bibliography line looks like.
  2. **Splits** each qualifying chunk into ``[marker] raw_text`` entries
     and **dedupes** chunk-overlap duplicate markers across the paper
     (first occurrence wins).
  3. **Extracts** fields (authors/journal/year/volume/first_page) via a
     regex for the ACS/Wiley ``authors, Journal YEAR, vol, page`` shape;
     lines that don't fit go to a SMALL-tier LLM in batches (cascade philosophy).
  4. **Matches** each entry to a DOI — local DOI-exact against the
     paper's own ``s2_neighbors`` rows first (free; no fuzzy/tuple
     matching — ``s2_neighbors`` has no author/journal columns to
     compare), else a Crossref bibliographic query (``safe_get``,
     backoff), with SMALL-LLM adjudication when two Crossref candidates
     are close; a matched DOI resolves ``held_ref_id`` against
     ``ref_identifiers``. A genuine no-candidates answer is negatively
     memoized (non-NULL ``match_conf`` doubles as the resolved marker);
     a Crossref *query failure* deliberately is not — ``match_conf``
     stays NULL so an outage window can't permanently poison an entry.

The pass **always stamps** ``refs.meta.bib_parse_version`` at the end,
even when it finds no bibliography — so a no-bibliography paper converges
(never re-claimed) and a version bump re-sweeps the corpus, same
discipline as ``paper_glossary``'s ``meta.glossary_version`` /
``hub_refine``'s ``last_refined_*``. Default-ON (``_SYS`` profile, see
``workers/registry.py``) — the predicate naturally drains the backlog on
normal worker cadence; ``--only bib_parse`` is just the fast-path burst.

Consumers (shipped sibling slices): the paper Sources tab reads the rows
via ``store.list_bib_entries`` (real ``[N]`` markers, unions in entries
S2 misses); ``bib_mark`` + ``taproot/resolve.py::resolve_citation`` wire
inline-marker resolution into hub-refine's verify loop. Explicitly NOT in
scope: author-year citation styles, auto-fetching a
matched-but-not-held paper.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.types.json import Jsonb

from precis.liveness import drain_sleep
from precis.utils.boilerplate import MARKER_LINE_RE as _MARKER_LINE_RE
from precis.utils.db_retry import retry_locked
from precis.workers import ref_lease

log = logging.getLogger(__name__)

#: Bumping this re-parses (and re-matches, since a fresh row starts with
#: ``match_conf IS NULL`` again) the corpus lazily — mirrors
#: ``paper_glossary.GLOSSARY_VERSION`` / ``classify.CLASSIFY_VERSION``.
BIB_PARSE_VERSION = 1

#: Key stamped onto ``refs.meta`` (paper-level convergence marker — see
#: module docstring).
_META_VERSION_KEY = "bib_parse_version"

#: Claim-time attempt-lease namespace (:mod:`precis.workers.ref_lease`,
#: gripe 202116): ``_claim``'s ``FOR UPDATE OF r SKIP LOCKED`` only holds
#: the row lock for the duration of the claim SELECT — ``run_bib_parse_pass``
#: commits that transaction immediately after, long before the multi-minute
#: parse/Crossref/LLM work that follows. A durable ``ref_tags`` lease,
#: stamped in the SAME claim transaction before it commits, is what a
#: sibling host's ``_claim`` actually respects; the row lock alone let two
#: hosts race the same still-unstamped paper (double LLM/Crossref cost, one
#: side dying on a ``paper_bib_entries`` lock timeout). Cleared on success
#: so a legitimate re-trigger (a ``BIB_PARSE_VERSION`` bump) isn't blocked
#: by a stale lease; left in place on failure so a crashed/wedged worker's
#: claim self-heals after :data:`ref_lease.ATTEMPT_COOLDOWN_MIN` instead of
#: stranding the paper forever.
_LEASE_NS = "BIBPARSE"

# ── content-based bibliography detection + entry splitting ────────────

#: A bibliography-shaped line: optional leading markdown ``-``, a
#: ``[N]`` marker, then non-empty content. Anchored to the line start —
#: a continuation line of a wrapped entry (no leading marker) doesn't
#: match, so it's correctly excluded from the "most lines match" ratio
#: and instead folded into the preceding entry by :func:`_split_entries`.
#: The marker is capped at 4 digits (``paper_bib_entries.marker`` is a
#: plain ``int``/int4, and a real bibliography never runs anywhere near
#: 9999 entries) — an OCR-garbled marker with more digits simply fails to
#: match here, so the line folds into the preceding entry's continuation
#: text instead of overflowing int4 at INSERT and failing that paper
#: every cycle without ever converging.
#:
#: Imported from :mod:`precis.utils.boilerplate` (aliased back to the
#: original private name) rather than redefined here — the ingest-time
#: classifier's citation-density check uses the identical pattern to
#: recognize this same per-entry chunk shape, and importing one shared
#: compiled regex means the two detectors can't silently drift apart.

#: A chunk "qualifies" as bibliography when at least half of its
#: non-empty lines match the marker shape (``>=``, not ``>``: a 2-line
#: chunk holding one entry plus one wrapped continuation line scores
#: exactly 0.5 and must still qualify).
_BIB_CHUNK_MIN_RATIO = 0.5

#: ``chunk_kind`` values that always qualify regardless of content ratio
#: (the ingest retag pass does sometimes get it right).
_ALWAYS_BIB_CHUNK_KINDS = frozenset({"references"})


def _chunk_is_bibliography(text: str, chunk_kind: str) -> bool:
    """True when ``text`` looks like (most of) a bibliography chunk.

    Content-based (decided — see module docstring): scans non-empty
    lines for the ``- [N] ...`` shape. A prose chunk that happens to
    mention one bracketed citation (``"... shown previously [3] ..."``)
    has that single line outvoted by ordinary prose lines and is
    correctly rejected.
    """
    if chunk_kind in _ALWAYS_BIB_CHUNK_KINDS:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = sum(1 for ln in lines if _MARKER_LINE_RE.match(ln))
    return hits / len(lines) >= _BIB_CHUNK_MIN_RATIO


def _split_entries(text: str) -> list[tuple[int, str]]:
    """Split one chunk's text into ``(marker, raw_text)`` entries.

    A wrapped continuation line (no leading ``[N]`` marker) is folded
    into the entry currently being built, space-joined. Lines before the
    first marker (a stray heading) are dropped. ``raw_text`` excludes the
    ``- [N]`` prefix itself — the marker is already its own column, and
    the matcher's Crossref bibliographic query works better on the
    citation text alone.
    """
    entries: list[tuple[int, str]] = []
    marker: int | None = None
    parts: list[str] = []
    for line in text.splitlines():
        m = _MARKER_LINE_RE.match(line)
        if m:
            if marker is not None:
                entries.append((marker, " ".join(parts).strip()))
            marker = int(m.group("marker"))
            parts = [m.group("rest").strip()]
        elif marker is not None and line.strip():
            parts.append(line.strip())
    if marker is not None:
        entries.append((marker, " ".join(parts).strip()))
    return entries


def _collect_paper_entries(
    chunk_rows: list[tuple[int, str, str]],
) -> list[tuple[int, str]]:
    """``(marker, raw_text)`` pairs for one paper's whole bibliography.

    ``chunk_rows`` is ``(ord, text, chunk_kind)`` in ``ord`` order.
    Non-qualifying chunks are skipped; a marker already seen (chunk
    overlap re-emits the tail of one chunk at the head of the next) keeps
    its *first* occurrence — later duplicates are dropped.
    """
    seen: dict[int, str] = {}
    order: list[int] = []
    for _ord, text, chunk_kind in chunk_rows:
        if not _chunk_is_bibliography(text, chunk_kind):
            continue
        for marker, raw_text in _split_entries(text):
            if marker in seen or not raw_text:
                continue
            seen[marker] = raw_text
            order.append(marker)
    return [(marker, seen[marker]) for marker in order]


# ── field extraction: regex fast path + LLM fallback ──────────────────

#: ACS/Wiley shape: ``<authors>, <Journal Abbrev> <YEAR>, <volume>,
#: <first_page>``. ``journal`` excludes commas (author names are
#: comma-separated, so the lazy ``authors`` group backtracks past each
#: author in turn until it finds a comma-free, space-joined run of
#: capitalized tokens immediately followed by ``YEAR, volume, page``).
_ACS_ENTRY_RE = re.compile(
    r"^(?P<authors>.+?),\s*"
    r"(?P<journal>[A-Z][\w.&\-]*(?:\s+[A-Z][\w.&\-]*)*)\s+"
    r"(?P<year>(?:18|19|20)\d{2})\s*,\s*"
    r"(?P<volume>\d+[A-Za-z]?)\s*,\s*"
    r"(?P<first_page>\d+)\b"
)

#: A confident regex parse.
_PARSE_CONF_REGEX = 0.9
#: An LLM-fallback parse that returned usable fields.
_PARSE_CONF_LLM = 0.55
#: Attempted (regex + LLM both) but nothing usable extracted.
_PARSE_CONF_UNPARSED = 0.0

#: How many messy lines go into one SMALL-tier LLM call.
_LLM_PARSE_BATCH_SIZE = 20

_PARSE_SYS = (
    "You are a precise bibliographic-citation-line parser. Each numbered "
    "line is one reference, usually in ACS/Wiley style (authors, journal, "
    "year, volume, first page). Extract the fields for each line, using "
    "null for anything you cannot determine. Reply with ONLY the "
    "requested JSON object, no prose."
)


def _extract_acs_fields(raw_text: str) -> dict[str, Any] | None:
    """Regex fast path for the ACS/Wiley line shape, or ``None``."""
    m = _ACS_ENTRY_RE.search(raw_text)
    if not m:
        return None
    return {
        "authors": m.group("authors").strip(" ,"),
        "journal": m.group("journal").strip(),
        "year": int(m.group("year")),
        "volume": m.group("volume"),
        "first_page": m.group("first_page"),
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON extraction — whole string first, then first ``{``..``}``."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            obj = json.loads(text[a : b + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _clean_year(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _build_llm_parse_prompt(batch: list[tuple[int, str]]) -> str:
    lines = [f"[{marker}] {raw_text}" for marker, raw_text in batch]
    return (
        "Parse each of the following bibliography lines into "
        "{authors, journal, year, volume, first_page} (use null for any "
        "field you cannot determine). Reply with JSON exactly:\n"
        '{"entries":[{"marker":<int>,"authors":<string|null>,'
        '"journal":<string|null>,"year":<int|null>,"volume":<string|null>,'
        '"first_page":<string|null>}]}\n\n' + "\n".join(lines)
    )


def _parse_llm_response(text: str) -> dict[int, dict[str, Any]]:
    data = _extract_json(text)
    out: dict[int, dict[str, Any]] = {}
    raw_entries = (data or {}).get("entries")
    entries_list: list[Any] = raw_entries if isinstance(raw_entries, list) else []
    for item in entries_list:
        if not isinstance(item, dict):
            continue
        marker_raw = item.get("marker")
        if marker_raw is None:
            continue
        try:
            marker = int(marker_raw)
        except (TypeError, ValueError):
            continue
        out[marker] = {
            "authors": _clean_str(item.get("authors")),
            "journal": _clean_str(item.get("journal")),
            "year": _clean_year(item.get("year")),
            "volume": _clean_str(item.get("volume")),
            "first_page": _clean_str(item.get("first_page")),
        }
    return out


def _parse_paper_entries(
    client: Any, entries: list[tuple[int, str]]
) -> list[dict[str, Any]]:
    """Regex-first, SMALL-LLM-fallback field extraction for one paper's
    entries. Returns one dict per entry: ``{marker, raw_text, authors,
    journal, year, volume, first_page, parse_conf}``."""
    fields: dict[int, dict[str, Any]] = {}
    fallback: list[tuple[int, str]] = []
    for marker, raw_text in entries:
        regex_fields = _extract_acs_fields(raw_text)
        if regex_fields is not None:
            fields[marker] = {**regex_fields, "parse_conf": _PARSE_CONF_REGEX}
        else:
            fallback.append((marker, raw_text))

    for i in range(0, len(fallback), _LLM_PARSE_BATCH_SIZE):
        batch = fallback[i : i + _LLM_PARSE_BATCH_SIZE]
        parsed: dict[int, dict[str, Any]] = {}
        try:
            out = client.complete(
                [
                    {"role": "system", "content": _PARSE_SYS},
                    {"role": "user", "content": _build_llm_parse_prompt(batch)},
                ]
            )
            parsed = _parse_llm_response(getattr(out, "text", "") or "")
        except Exception:
            log.warning("bib_parse: LLM parse-fallback batch failed", exc_info=True)
        for marker, raw_text in batch:
            got = parsed.get(marker)
            if got is not None and any(got.values()):
                fields[marker] = {**got, "parse_conf": _PARSE_CONF_LLM}
            else:
                fields[marker] = {
                    "authors": None,
                    "journal": None,
                    "year": None,
                    "volume": None,
                    "first_page": None,
                    "parse_conf": _PARSE_CONF_UNPARSED,
                }

    return [
        {"marker": marker, "raw_text": raw_text, **fields[marker]}
        for marker, raw_text in entries
    ]


# ── matcher: local DOI-exact, then Crossref, then LLM adjudication ────

#: A DOI embedded in the raw citation text (loose but rejects obvious
#: non-matches before any lookup).
_DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/\S+")

_CROSSREF_BASE = "https://api.crossref.org/works"
_CROSSREF_TIMEOUT_S = 20.0
_CROSSREF_RETRY_MAX_ATTEMPTS = 3
_CROSSREF_RETRY_BASE_S = 1.0

#: A second Crossref candidate within this fraction of the top candidate's
#: score is "close enough" to be ambiguous, needing LLM adjudication.
_AMBIGUOUS_SCORE_RATIO = 0.92

_MATCH_CONF_LOCAL_DOI = 1.0
_MATCH_CONF_CROSSREF = 0.8
_MATCH_CONF_CROSSREF_LLM = 0.6
#: Attempted, unmatched — the memoization marker (non-NULL, ``doi`` stays
#: NULL) so a later pass doesn't re-query Crossref for this entry.
_MATCH_CONF_UNMATCHED = 0.0

_ADJUDICATE_SYS = (
    "You are matching a bibliography citation line to the correct "
    "Crossref candidate record. Reply with ONLY the requested JSON "
    "object, no prose."
)


def _extract_doi_from_text(raw_text: str) -> str | None:
    m = _DOI_IN_TEXT_RE.search(raw_text)
    if not m:
        return None
    return m.group(0).rstrip(".,;)]")


def _local_doi_match(
    conn: Any, ref_id: int, raw_text: str
) -> tuple[str, str | None] | None:
    """DOI-exact against this paper's own ``s2_neighbors`` ``cites`` rows
    (decided policy — no fuzzy/tuple matching). Returns ``(doi, s2_id)``
    on a hit, else ``None`` (including when ``raw_text`` carries no DOI
    at all, or carries one Crossref/S2 never saw)."""
    candidate = _extract_doi_from_text(raw_text)
    if not candidate:
        return None
    from precis.identity import normalize_doi

    nd = normalize_doi(candidate)
    if not nd:
        return None
    rows = conn.execute(
        "SELECT doi, s2_id FROM s2_neighbors "
        "WHERE ref_id = %s AND direction = 'cites' AND doi IS NOT NULL",
        (ref_id,),
    ).fetchall()
    for doi_val, s2_id in rows:
        if normalize_doi(doi_val) == nd:
            return (doi_val, s2_id)
    return None


def _crossref_query(raw_text: str, *, mailto: str = "") -> list[dict[str, Any]] | None:
    """Crossref bibliographic query (``query.bibliographic=<raw_text>``),
    up to two candidates, via ``safe_get`` (never raw httpx — SSRF guard).
    Retries 429/5xx with exponential backoff.

    Returns the candidate list on success — possibly ``[]``, a genuine
    "Crossref found nothing" answer the caller is free to memoize as
    unmatched — or ``None`` on any **failure** (network error, exhausted
    retries, a non-200 status, an unparseable body). The distinction
    matters: a ``None`` must NOT be memoized as unmatched, or a Crossref
    outage during a backfill would permanently poison every entry it
    touched — the caller leaves ``match_conf`` NULL instead, so a later
    attempt retries it.
    """
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    params: dict[str, Any] = {"query.bibliographic": raw_text, "rows": 2}
    if mailto:
        params["mailto"] = mailto

    with http_client(timeout=_CROSSREF_TIMEOUT_S) as client:
        for attempt in range(_CROSSREF_RETRY_MAX_ATTEMPTS):
            try:
                resp = safe_get(client, _CROSSREF_BASE, params=params)
            except Exception as exc:
                log.warning("bib_parse: crossref request failed: %r", exc)
                if attempt + 1 < _CROSSREF_RETRY_MAX_ATTEMPTS:
                    if drain_sleep(_CROSSREF_RETRY_BASE_S * (2**attempt)):
                        return None
                    continue
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt + 1 < _CROSSREF_RETRY_MAX_ATTEMPTS:
                    if drain_sleep(_CROSSREF_RETRY_BASE_S * (2**attempt)):
                        return None
                    continue
                return None
            if resp.status_code != 200:
                log.warning("bib_parse: crossref returned status %s", resp.status_code)
                return None
            try:
                data = resp.json()
            except Exception:
                log.warning("bib_parse: crossref returned an unparseable body")
                return None
            items = ((data or {}).get("message") or {}).get("items") or []
            return items if isinstance(items, list) else None
    return None


def _candidate_summary(item: dict[str, Any]) -> str:
    titles = item.get("title") or []
    title = titles[0] if titles else ""
    journals = item.get("container-title") or []
    journal = journals[0] if journals else ""
    year = None
    parts = ((item.get("issued") or {}).get("date-parts")) or [[]]
    if parts and parts[0]:
        year = parts[0][0]
    return f'DOI {item.get("DOI")}: "{title}" -- {journal} ({year})'


def _adjudicate_candidates(
    client: Any, raw_text: str, a: dict[str, Any], b: dict[str, Any]
) -> str | None:
    """SMALL-LLM pick between two close Crossref candidates. Returns
    ``"a"``/``"b"``, or ``None`` (model said "none" / call failed /
    unparseable — the entry stays unmatched)."""
    prompt = (
        f"Citation line: {raw_text}\n\n"
        f"Candidate A: {_candidate_summary(a)}\n"
        f"Candidate B: {_candidate_summary(b)}\n\n"
        "Which candidate is this citation? Reply with ONLY JSON exactly: "
        '{"pick": "a" | "b" | "none"}'
    )
    try:
        out = client.complete(
            [
                {"role": "system", "content": _ADJUDICATE_SYS},
                {"role": "user", "content": prompt},
            ]
        )
    except Exception:
        log.warning("bib_parse: crossref adjudication call failed", exc_info=True)
        return None
    data = _extract_json(getattr(out, "text", "") or "")
    pick = (data or {}).get("pick")
    return pick if pick in ("a", "b") else None


def _resolve_crossref_candidates(
    client: Any, raw_text: str, items: list[dict[str, Any]]
) -> tuple[str | None, float]:
    """``(doi, match_conf)`` from a Crossref candidate list. A single
    confident top candidate matches outright; two close candidates go to
    LLM adjudication; no candidates (or an adjudication that resolves to
    neither) leaves the entry unmatched (memoized, not re-queried)."""
    if not items:
        return None, _MATCH_CONF_UNMATCHED
    top = items[0]
    top_doi = (top.get("DOI") or "").strip()
    if not top_doi:
        return None, _MATCH_CONF_UNMATCHED
    top_score = float(top.get("score") or 0.0)
    if len(items) < 2:
        return top_doi, _MATCH_CONF_CROSSREF
    second = items[1]
    second_score = float(second.get("score") or 0.0)
    if top_score <= 0 or second_score < top_score * _AMBIGUOUS_SCORE_RATIO:
        return top_doi, _MATCH_CONF_CROSSREF
    second_doi = (second.get("DOI") or "").strip()
    pick = _adjudicate_candidates(client, raw_text, top, second)
    if pick == "a":
        return top_doi, _MATCH_CONF_CROSSREF_LLM
    if pick == "b" and second_doi:
        return second_doi, _MATCH_CONF_CROSSREF_LLM
    return None, _MATCH_CONF_UNMATCHED


def _held_ref_for_doi(conn: Any, doi: str) -> int | None:
    """Resolve a matched ``doi`` to a **held** paper via ``ref_identifiers``
    (mirrors ``backfill/citation_lens.py::_held_ref_for_neighbor``)."""
    from precis.identity import normalize_doi

    nd = normalize_doi(doi)
    if not nd:
        return None
    row = conn.execute(
        "SELECT ref_id FROM ref_identifiers WHERE id_kind = 'doi' AND id_value = %s",
        (nd,),
    ).fetchone()
    return int(row[0]) if row else None


def _match_entry_via_local_doi(
    conn: Any, ref_id: int, raw_text: str
) -> tuple[str, str | None, int | None] | None:
    """The fast, network-free path of the matcher: local DOI-exact lookup
    plus held-ref resolution, both plain DB reads. Returns ``(doi, s2_id,
    held_ref_id)`` on a hit, else ``None`` (no local match at all)."""
    local = _local_doi_match(conn, ref_id, raw_text)
    if local is None:
        return None
    doi, s2_id = local
    held_ref_id = _held_ref_for_doi(conn, doi)
    return doi, s2_id, held_ref_id


def _write_local_doi_match(
    conn: Any,
    entry_id: int,
    doi: str,
    s2_id: str | None,
    held_ref_id: int | None,
) -> None:
    conn.execute(
        "UPDATE paper_bib_entries SET doi = %s, s2_id = %s, "
        "held_ref_id = %s, match_conf = %s WHERE id = %s",
        (doi, s2_id, held_ref_id, _MATCH_CONF_LOCAL_DOI, entry_id),
    )


def _write_crossref_match(
    conn: Any,
    entry_id: int,
    matched_doi: str | None,
    held_ref_id: int | None,
    match_conf: float,
) -> None:
    conn.execute(
        "UPDATE paper_bib_entries SET doi = %s, held_ref_id = %s, "
        "match_conf = %s WHERE id = %s",
        (matched_doi, held_ref_id, match_conf, entry_id),
    )


def run_bib_parse_match_pass(
    store: Any, ref_id: int, *, client: Any, mailto: str = ""
) -> dict[str, int]:
    """Match every still-unmatched (``match_conf IS NULL``) entry of one
    paper. Split out from :func:`run_bib_parse_pass` so it's independently
    idempotent/testable: a second call against the same paper makes zero
    Crossref calls (every entry already carries a non-NULL ``match_conf``
    from the first call).

    A successful attempt (local hit, a confident/adjudicated Crossref
    match, or a genuine Crossref zero-candidate answer) always leaves
    ``match_conf`` non-NULL — that's the memoization marker a later pass
    checks. A Crossref *query failure* (see :func:`_crossref_query`)
    leaves the row untouched (``match_conf`` stays NULL) so it's picked
    back up on a later attempt instead of being permanently mis-memoized
    as unmatched.

    No single connection is held across the whole loop (gripe: prod's
    ``lock_timeout=5s`` -- see ``precis.utils.db_retry``'s module
    docstring). The unmatched-entry read below is its own short
    transaction; each entry's Crossref query and any LLM adjudication call
    then run with NO connection checked out (mirrors
    :func:`run_bib_parse_pass`'s "NO connection" phase for the same
    reason — Postgres's ``idle_in_transaction_session_timeout`` would kill
    an idle-in-txn connection held across a slow outbound call); and each
    entry's write is its own short transaction, wrapped in
    :func:`retry_locked` so a sibling host's own short write against the
    same paper (this same loop on another entry, or the parse leg's
    delete+insert -- see :func:`run_bib_parse_pass`) can collide for an
    instant without failing this entry outright.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, raw_text FROM paper_bib_entries "
            "WHERE ref_id = %s AND match_conf IS NULL",
            (ref_id,),
        ).fetchall()
        conn.commit()

    for raw_entry_id, raw_text in rows:
        entry_id = int(raw_entry_id)

        # Local DOI-exact match: DB reads only, no network -- fine to hold
        # a connection for the length of this lookup.
        with store.pool.connection() as conn:
            local = _match_entry_via_local_doi(conn, ref_id, raw_text)
            conn.commit()

        if local is not None:
            doi, s2_id, held_ref_id = local

            def _write_local(
                entry_id: int = entry_id,
                doi: str = doi,
                s2_id: str | None = s2_id,
                held_ref_id: int | None = held_ref_id,
            ) -> None:
                with store.pool.connection() as wconn:
                    _write_local_doi_match(wconn, entry_id, doi, s2_id, held_ref_id)
                    wconn.commit()

            retry_locked(_write_local, label=f"bib_parse match entry_id={entry_id}")
            continue

        items = _crossref_query(raw_text, mailto=mailto)
        if items is None:
            log.warning(
                "bib_parse: crossref query failed for entry_id=%s -- leaving "
                "match_conf NULL for a later retry",
                entry_id,
            )
            continue

        matched_doi, match_conf = _resolve_crossref_candidates(client, raw_text, items)

        with store.pool.connection() as conn:
            held_ref_id = _held_ref_for_doi(conn, matched_doi) if matched_doi else None
            conn.commit()

        def _write_crossref(
            entry_id: int = entry_id,
            matched_doi: str | None = matched_doi,
            held_ref_id: int | None = held_ref_id,
            match_conf: float = match_conf,
        ) -> None:
            with store.pool.connection() as wconn:
                _write_crossref_match(
                    wconn, entry_id, matched_doi, held_ref_id, match_conf
                )
                wconn.commit()

        retry_locked(_write_crossref, label=f"bib_parse match entry_id={entry_id}")

    return {"attempted": len(rows)}


# ── DB: claim + chunk read + write ─────────────────────────────────────


def _claim(
    conn: Any, *, limit: int, ref_ids: list[int] | None = None
) -> list[tuple[int, str]]:
    """Papers with body content whose ``meta.bib_parse_version`` is absent
    or below :data:`BIB_PARSE_VERSION`. ``ref_ids`` optionally restricts
    the sweep (targeted backfill / tests); ``None`` sweeps the corpus.

    ``FOR UPDATE OF r SKIP LOCKED`` (mirrors ``hub_refine._claim_hubs_due_
    for_refine``) drops a row a concurrent claim already holds — but only
    for the instant both SELECTs overlap; the caller commits this
    transaction right after the SELECT, well before the multi-minute
    parse/Crossref work runs, so the row lock alone doesn't stop a sibling
    host's *later* claim from re-selecting the same still-unstamped paper.
    The additional :func:`ref_lease.exclude_clause` check is what actually
    closes that window — it excludes a ref currently under an unexpired
    claim-time attempt lease (:data:`_LEASE_NS`, stamped by the caller in
    the same transaction as this SELECT, see ``run_bib_parse_pass`` —
    gripe 202116).
    """
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        WHERE r.kind = 'paper' AND r.deleted_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND (
            NOT (r.meta ? %(mk)s)
            OR COALESCE((r.meta->>%(mk)s)::int, 0) < %(ver)s
          )
          {ref_lease.exclude_clause("r.ref_id", "attempt_ns")}
        ORDER BY r.ref_id
        LIMIT %(limit)s
        FOR UPDATE OF r SKIP LOCKED
    """
    params: dict[str, Any] = {
        "mk": _META_VERSION_KEY,
        "ver": BIB_PARSE_VERSION,
        "attempt_ns": ref_lease.attempt_ns(_LEASE_NS),
        "limit": limit,
    }
    if ref_ids:
        params["ref_ids"] = list(ref_ids)
    rows = conn.execute(sql, params).fetchall()
    return [(int(r[0]), str(r[1] or "")) for r in rows]


def _paper_chunks(conn: Any, ref_id: int) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT ord, text, chunk_kind FROM chunks "
        "WHERE ref_id = %s AND ord >= 0 AND retired_at IS NULL ORDER BY ord",
        (ref_id,),
    ).fetchall()
    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def _delete_stale_entries(conn: Any, ref_id: int) -> None:
    """Clear out-of-date rows before a version-bumped re-parse writes
    fresh ones.

    ``_write_parsed_entry``'s ``ON CONFLICT (ref_id, marker) DO NOTHING``
    only dedupes chunk-overlap duplicates *within* one parse pass at the
    *current* version — without this delete, a ``BIB_PARSE_VERSION`` bump
    would re-scan a paper (since ``_claim`` re-selects it) but never
    touch its existing rows: every INSERT would hit the same
    ``(ref_id, marker)`` conflict and no-op, so stale fields/``doi``/
    ``match_conf`` from the old version would converge forever instead of
    being refreshed.
    """
    conn.execute(
        "DELETE FROM paper_bib_entries WHERE ref_id = %s AND parse_version < %s",
        (ref_id, BIB_PARSE_VERSION),
    )


def _write_parsed_entry(conn: Any, ref_id: int, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO paper_bib_entries "
        "(ref_id, marker, raw_text, authors, journal, year, volume, "
        "first_page, parse_conf, parse_version) "
        "VALUES (%(ref_id)s, %(marker)s, %(raw_text)s, %(authors)s, "
        "%(journal)s, %(year)s, %(volume)s, %(first_page)s, "
        "%(parse_conf)s, %(parse_version)s) "
        "ON CONFLICT (ref_id, marker) DO NOTHING",
        {
            "ref_id": ref_id,
            "marker": row["marker"],
            "raw_text": row["raw_text"],
            "authors": row["authors"],
            "journal": row["journal"],
            "year": row["year"],
            "volume": row["volume"],
            "first_page": row["first_page"],
            "parse_conf": row["parse_conf"],
            "parse_version": BIB_PARSE_VERSION,
        },
    )


def _stamp_paper_version(conn: Any, ref_id: int) -> None:
    conn.execute(
        "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s",
        (Jsonb({_META_VERSION_KEY: BIB_PARSE_VERSION}), ref_id),
    )


# ── the pass ────────────────────────────────────────────────────────────


def run_bib_parse_pass(
    store: Any,
    *,
    client: Any,
    batch_size: int = 4,
    ref_ids: list[int] | None = None,
    mailto: str = "",
) -> dict[str, int]:
    """One claim -> parse -> match -> stamp cycle. Returns ``{claimed, ok,
    failed}``.

    ``ref_ids`` optionally restricts the claim to specific papers
    (targeted backfill / tests, mirroring ``paper_glossary``); ``None``
    sweeps the whole corpus. ``batch_size`` bounds *papers* per call, not
    LLM-fallback lines (see :data:`_LLM_PARSE_BATCH_SIZE`) — a single
    paper's bibliography can carry hundreds of entries.
    """
    with store.pool.connection() as conn:
        rows = _claim(conn, limit=batch_size, ref_ids=ref_ids)
        # Stamp the claim-time attempt lease for every claimed paper in the
        # SAME transaction as the claim SELECT, before it commits (see
        # ``_claim``'s docstring / gripe 202116) — that's what makes the
        # claim durable across the multi-minute processing window below,
        # not the ``FOR UPDATE`` row lock alone (which is released the
        # instant this transaction commits).
        for ref_id, _title in rows:
            ref_lease.stamp_attempt(store, ref_id, _LEASE_NS, conn=conn)
        conn.commit()
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    for ref_id, _title in rows:
        try:
            with store.pool.connection() as conn:
                chunk_rows = _paper_chunks(conn, ref_id)
            entries = _collect_paper_entries(chunk_rows)
            # The LLM parse below can take a long time (hundreds of
            # entries per paper). It must run with NO connection checked
            # out — holding one open across the call leaves it idle in an
            # open transaction, which Postgres's
            # idle_in_transaction_session_timeout then kills, failing the
            # paper outright. Read above, parse here, write in a fresh
            # connection below.
            parsed = _parse_paper_entries(client, entries)

            # The delete-stale + insert-parsed write is one short
            # transaction, wrapped in retry_locked: a sibling host's own
            # short write to this same paper (e.g. its match leg, above)
            # can collide with this one for an instant under prod's
            # lock_timeout=5s -- retry rather than fail the whole paper.
            def _write_parsed_batch(
                ref_id: int = ref_id, parsed: list[dict[str, Any]] = parsed
            ) -> None:
                with store.pool.connection() as conn:
                    _delete_stale_entries(conn, ref_id)
                    for parsed_row in parsed:
                        _write_parsed_entry(conn, ref_id, parsed_row)
                    conn.commit()

            retry_locked(_write_parsed_batch, label=f"bib_parse write ref_id={ref_id}")

            if parsed:
                run_bib_parse_match_pass(store, ref_id, client=client, mailto=mailto)

            with store.pool.connection() as conn:
                _stamp_paper_version(conn, ref_id)
                # Success — clear the attempt lease in the same transaction
                # so a legitimate re-trigger (a BIB_PARSE_VERSION bump)
                # isn't blocked by a stale lease left over from this run.
                ref_lease.clear_attempt(store, ref_id, _LEASE_NS, conn=conn)
                conn.commit()
            ok += 1
        except (LockNotAvailable, DeadlockDetected):
            # Residual lock contention that outlasted retry_locked's own
            # budget (attempts exhausted -- either error class, same as
            # what retry_locked itself retries on). Deliberately do NOT
            # clear the attempt lease here -- same spend-guard reasoning as
            # the except Exception branch below applies: this paper still
            # brakes for ref_lease.ATTEMPT_COOLDOWN_MIN rather than
            # re-billing Crossref/LLM every sweep. But this is expected
            # fleet contention, not a crash, so it's a warning, not an
            # exception traceback.
            log.warning(
                "bib_parse: ref_id=%s hit residual lock contention after "
                "retries -- braking for %d min",
                ref_id,
                ref_lease.ATTEMPT_COOLDOWN_MIN,
            )
            failed += 1
        except Exception:
            # Deliberately do NOT clear the attempt lease here — it stays
            # in place, braking this paper from immediate re-claim (by
            # this host or a sibling) for ref_lease.ATTEMPT_COOLDOWN_MIN
            # instead of re-billing Crossref/LLM every sweep. A crashed
            # worker (never reaches this except at all) still self-heals
            # once the lease's TTL expires.
            log.exception("bib_parse: failed ref_id=%s", ref_id)
            failed += 1
    return {"claimed": len(rows), "ok": ok, "failed": failed}


__all__ = [
    "BIB_PARSE_VERSION",
    "run_bib_parse_match_pass",
    "run_bib_parse_pass",
]
