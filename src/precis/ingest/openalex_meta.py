"""OpenAlex free-metadata enrichment (OPEN-ITEMS OA #G).

The OpenAlex *work* object is free (a configured paid ``api_key`` simply
rides along, drawing the credit) and far richer than the
CrossRef/S2 metadata we hold — 49 fields incl. citations, topics, funders,
ORCID+ROR affiliations. This slurps it into the ref, independent of (and much
cheaper than) the paid OpenAlex Content PDF pull (``workers/fetch_oa`` /
``precis fetch-openalex``).

What lands where:

* ``meta.openalex`` — a versioned block: OpenAlex id, reconstructed abstract,
  topics/concepts/keywords, funders, ``fwci`` + ``cited_by_count``, SDGs, MeSH,
  ``referenced_works`` (the raw W-ids + count — the *edge materialization*
  into ``links`` is deferred to the scholarly-graph item, roadmap #6), and the
  structured ``authorships`` (name + ORCID + institution ROR + country).
* ``refs.authors`` (byline column) — filled from OpenAlex authorships **only
  when the ref has none** (thin/empty), so a good existing byline is never
  clobbered; ORCID/ROR survive via ``to_author_dicts``.
* ``ref_identifiers`` — the ``openalex:W…`` id registered.

The fetch side is addressed by *any* scheme OpenAlex can select on —
DOI, PubMed id, MAG id, the bare ``W…`` id, or an arXiv id (resolved
through its ``10.48550/arXiv.<id>`` DataCite DOI) — via
:func:`fetch_openalex_work_by_identifier`, so :func:`enrich_ref` doubles
as the bulk-backfill door for a ref carrying any of those aliases
(gr514199).

Topics are captured in ``meta.openalex`` rather than written as ``topic:``
``ref_tags`` on purpose — the OPEN-tag namespace is mid-teardown, so
materializing controlled topic tags waits for that axis to settle.

Idempotent: ``meta_patch`` merges ``meta || {openalex: …}`` so a re-run
overwrites the block wholesale. Fixed host (no SSRF surface); the polite
``mailto`` routes us to OpenAlex's fast pool, and a configured
``PRECIS_OPENALEX_CONTENT_KEY`` rides along as ``api_key`` so the paid
credit is used (gr513771).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from precis.secrets import get_secret
from precis.store._identifiers_ops import (
    _normalise_identifier,
    detect_identifier_scheme,
)
from precis.utils.authors import to_author_dicts
from precis.utils.http import call_with_backoff, http_client, request_with_deadline

log = logging.getLogger(__name__)

_OPENALEX_WORKS = "https://api.openalex.org/works"
_TIMEOUT_S = 30.0
_UA = "precis-mcp/8.0 (+https://github.com/retostamm/precis-mcp{contact})"

#: Bump when the shape of the ``meta.openalex`` block changes so a backfill
#: sweep (``meta->openalex->>'v' != current``) can re-claim the corpus.
ENRICH_VERSION = 1


def _ua(email: str) -> str:
    """Honest UA; no contact configured -> no mailto, never a made-up one."""
    return _UA.format(contact=f"; mailto:{email}" if email else "")


def _openalex_api_key() -> str:
    """The paid OpenAlex key, ``""`` when unconfigured.

    Same vault name + ``get_secret`` resolution as the Content download leg
    (:func:`precis.workers.fetch_oa._openalex_content_key` reads
    ``PRECIS_OPENALEX_CONTENT_KEY``) — one premium key serves both endpoints,
    and metadata calls then draw the paid credit instead of the anonymous
    pool (gr513771). The param value never reaches logs:
    :func:`precis.utils.log_scrub.install_log_scrub` masks credential-named
    query params in every record, and ``call_with_backoff``'s refusal lines
    go through :func:`precis.utils.http.redact_url`.
    """
    return (get_secret("PRECIS_OPENALEX_CONTENT_KEY") or "").strip()


def _params(email: str) -> dict[str, str]:
    """Query params for a ``works`` call: polite ``mailto`` plus the paid
    ``api_key`` when the key is configured."""
    params = {"mailto": email} if email else {}
    api_key = _openalex_api_key()
    if api_key:
        params["api_key"] = api_key
    return params


def _fetch_work(selector: str, *, email: str, timeout: float) -> dict[str, Any] | None:
    """``GET /works/{selector}`` — shared fetch behind the two public doors.

    A 404 returns ``None``; other HTTP errors raise for the caller to
    record. Raises :class:`precis.utils.rate_limit.RateLimited` without
    calling out when the shared ``openalex`` limiter refuses (daily quota
    spent or rate-starved).
    """
    url = f"{_OPENALEX_WORKS}/{selector}"
    params = _params(email)

    def once() -> Any:
        with http_client(timeout=timeout, headers={"User-Agent": _ua(email)}) as client:
            # httpx timeouts are per-phase — a trickling peer defeats them
            # (gr486823), so the whole request gets an overall wall-clock
            # bound of 2x the per-phase timeout.
            resp = request_with_deadline(
                client, 2 * timeout, lambda: client.get(url, params=params)
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    # The shared ``openalex`` row gives a token per attempt; 429/5xx back
    # off (Retry-After honoured), 4xx other than 404 raise at once.
    data = call_with_backoff(once, service="openalex")
    return data if isinstance(data, dict) else None


def fetch_openalex_work(
    doi: str, *, email: str = "", timeout: float = _TIMEOUT_S
) -> dict[str, Any] | None:
    """Fetch the free OpenAlex work object for a DOI, or ``None`` if unknown.

    A 404 (DOI not in OpenAlex) returns ``None``; other HTTP errors
    raise for the caller to record. ``mailto`` is polite + faster. When
    ``PRECIS_OPENALEX_CONTENT_KEY`` resolves, the request also carries it as
    ``api_key`` so the call draws the paid credit (the work object itself is
    free).
    """
    return _fetch_work(f"doi:{doi}", email=email, timeout=timeout)


def fetch_openalex_work_by_identifier(
    identifier: str, *, email: str = "", timeout: float = _TIMEOUT_S
) -> dict[str, Any] | None:
    """Fetch the free OpenAlex work object by any recognised identifier.

    The scheme is auto-detected
    (:func:`~precis.store._identifiers_ops.detect_identifier_scheme`) and
    mapped onto a ``/works/{selector}`` form: ``doi:``, ``pmid:`` and
    ``mag:`` are native selectors and a bare ``W…`` goes through
    :func:`fetch_openalex_work_by_id`; an ``arxiv`` id resolves through
    its DataCite DOI ``10.48550/arXiv.<id>`` (minted for the whole arXiv
    backlog), which the ``doi:`` selector matches — OpenAlex has no
    ``arxiv:`` selector. Same contract as :func:`fetch_openalex_work`:
    a 404 returns ``None``; other HTTP errors raise. Raises
    :class:`ValueError` when the string detects no scheme or the scheme
    has no OpenAlex selector (``s2``, ``pdfsha256``, ``dblp``).
    """
    scheme = detect_identifier_scheme(identifier)
    if scheme is None:
        # The detector's conservative shapes miss the
        # ``https://openalex.org/W…`` URL form and the bare lowercase
        # ``w…`` (``ref_identifiers``' storage case — what a backfill
        # reads); both still resolve through
        # :func:`fetch_openalex_work_by_id`.
        low = identifier.strip().lower()
        if "openalex.org/" in low or re.match(r"^w\d{6,}$", low):
            scheme = "openalex"
    if scheme is None:
        raise ValueError(f"unrecognised identifier {identifier!r}")
    value = _normalise_identifier(scheme, identifier)
    if not value:
        raise ValueError(f"empty {scheme} identifier {identifier!r}")
    if scheme == "openalex":
        return fetch_openalex_work_by_id(value, email=email, timeout=timeout)
    if scheme == "arxiv":
        return fetch_openalex_work(
            f"10.48550/arXiv.{value}", email=email, timeout=timeout
        )
    if scheme == "doi":
        return fetch_openalex_work(value, email=email, timeout=timeout)
    if scheme == "pubmed":
        return _fetch_work(f"pmid:{value}", email=email, timeout=timeout)
    if scheme == "mag":
        return _fetch_work(f"mag:{value}", email=email, timeout=timeout)
    raise ValueError(f"no OpenAlex selector for a {scheme!r} identifier")


def fetch_openalex_work_by_id(
    openalex_id: str, *, email: str = "", timeout: float = _TIMEOUT_S
) -> dict[str, Any] | None:
    """Fetch the free OpenAlex work object by OpenAlex id, or ``None``.

    Same contract as :func:`fetch_openalex_work`, addressed by ``W…`` id
    instead of DOI (``/works/W123``). Accepts the bare id, the
    ``https://openalex.org/W…`` URL form, or a lowercase ``w…`` (the
    ``ref_identifiers`` storage form) — normalised to the API's
    uppercase-W path segment.
    """
    oid = _short_id(openalex_id).strip()
    if not oid:
        return None
    if oid[:1].lower() == "w":
        oid = "W" + oid[1:]
    return _fetch_work(oid, email=email, timeout=timeout)


def search_openalex_works(
    query: str,
    *,
    year: int | None = None,
    per_page: int = 5,
    email: str = "",
    timeout: float = _TIMEOUT_S,
) -> list[dict[str, Any]]:
    """``GET /works?search=<query>`` — the live title/bibliographic lookup.

    Returns the ``results`` list (top ``per_page`` relevance-ranked work
    objects), ``[]`` when the response carries none. ``year`` pins the
    search to ``filter=publication_year:<year>``. Same client /
    ``call_with_backoff`` / shared ``openalex`` limiter row as
    :func:`_fetch_work`: a first-attempt limiter refusal raises
    :class:`precis.utils.rate_limit.RateLimited` (the caller defers);
    HTTP errors raise after the politeness core's retries.
    """
    params = _params(email)
    params["search"] = query
    params["per-page"] = str(per_page)
    if year is not None:
        params["filter"] = f"publication_year:{year}"

    def once() -> Any:
        with http_client(timeout=timeout, headers={"User-Agent": _ua(email)}) as client:
            resp = request_with_deadline(
                client, 2 * timeout, lambda: client.get(_OPENALEX_WORKS, params=params)
            )
            resp.raise_for_status()
            return resp.json()

    data = call_with_backoff(once, service="openalex")
    results = (data or {}).get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


def fetch_citing_works(
    openalex_ids: list[str],
    *,
    since: str,
    email: str = "",
    timeout: float = _TIMEOUT_S,
    per_page: int = 200,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """``GET /works?filter=cites:W1|W2|…,from_publication_date:<since>``.

    The forward-citation poll the ``citer_watch`` worker runs: one call
    covers a whole batch of seed works (OpenAlex ORs ``|``-joined values
    inside a single filter), restricted to works published on/after
    ``since`` (``YYYY-MM-DD``). Only the fields the caller scores and
    mints stubs from are ``select``ed — ``referenced_works`` is what
    tells the caller *which* of the batched seeds each citing work
    actually cites.

    Cursor-paginates (``cursor=*`` … ``meta.next_cursor``) up to
    ``max_pages``; a truncated walk is logged, never silently dropped.
    Same client / ``call_with_backoff`` / shared ``openalex`` limiter
    row as :func:`_fetch_work`: a first-attempt limiter refusal raises
    :class:`precis.utils.rate_limit.RateLimited` (the caller defers);
    HTTP errors raise after the politeness core's retries.
    """
    ids = [_short_id(i) for i in openalex_ids]
    ids = ["W" + i[1:] if i[:1].lower() == "w" else i for i in ids if i]
    if not ids:
        return []
    params = _params(email)
    params["filter"] = f"cites:{'|'.join(ids)},from_publication_date:{since}"
    params["select"] = "id,doi,title,publication_year,referenced_works,is_retracted"
    params["per-page"] = str(per_page)
    params["cursor"] = "*"

    out: list[dict[str, Any]] = []
    for _page in range(max_pages):
        page_params = dict(params)

        def once(page_params: dict[str, str] = page_params) -> Any:
            with http_client(
                timeout=timeout, headers={"User-Agent": _ua(email)}
            ) as client:
                resp = request_with_deadline(
                    client,
                    2 * timeout,
                    lambda: client.get(_OPENALEX_WORKS, params=page_params),
                )
                resp.raise_for_status()
                return resp.json()

        data = call_with_backoff(once, service="openalex")
        results = (data or {}).get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            out.extend(w for w in results if isinstance(w, dict))
        next_cursor = (
            ((data or {}).get("meta") or {}).get("next_cursor")
            if isinstance(data, dict)
            else None
        )
        if not next_cursor or not results:
            return out
        params["cursor"] = str(next_cursor)
    log.warning(
        "fetch_citing_works: hit %d-page cap at %d works (cites:%s, since %s)",
        max_pages,
        len(out),
        "|".join(ids),
        since,
    )
    return out


def _short_id(openalex_url: str | None) -> str:
    """``https://openalex.org/W123`` → ``W123`` (bare id)."""
    if not openalex_url:
        return ""
    return str(openalex_url).rstrip("/").rsplit("/", 1)[-1]


def _reconstruct_abstract(inverted: Any) -> str:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index``.

    The index maps ``token -> [positions]``; we scatter tokens back to their
    positions and join. Returns ``""`` on anything unexpected (pure, never
    raises).
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    slots: dict[int, str] = {}
    for token, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for p in positions:
            if isinstance(p, int):
                slots[p] = token
    if not slots:
        return ""
    return " ".join(slots[i] for i in range(max(slots) + 1) if i in slots)


def _names(items: Any, key: str = "display_name") -> list[str]:
    """Pluck ``display_name`` (or ``key``) off a list of dicts, skipping blanks."""
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        name = (it.get(key) if isinstance(it, dict) else None) or ""
        name = str(name).strip()
        if name and name not in out:
            out.append(name)
    return out


def _authorships(work: dict[str, Any]) -> list[dict[str, str]]:
    """Structured author rows: name + ORCID + OpenAlex author id + first
    institution ROR + country."""
    out: list[dict[str, str]] = []
    for a in work.get("authorships") or []:
        if not isinstance(a, dict):
            continue
        author = a.get("author") or {}
        name = str(author.get("display_name") or "").strip()
        if not name:
            continue
        row: dict[str, str] = {"name": name}
        orcid = str(author.get("orcid") or "").strip()
        if orcid:
            row["orcid"] = orcid
        oa_author_id = _short_id(author.get("id"))
        if oa_author_id:
            row["openalex_author_id"] = oa_author_id
        insts = a.get("institutions") or []
        if insts and isinstance(insts[0], dict):
            aff = str(insts[0].get("display_name") or "").strip()
            ror = str(insts[0].get("ror") or "").strip()
            cc = str(insts[0].get("country_code") or "").strip()
            if aff:
                row["affiliation"] = aff
            if ror:
                row["ror"] = ror
            if cc:
                row["country"] = cc
        out.append(row)
    return out


@dataclass
class OpenAlexEnrichment:
    """Normalized enrichment ready to write onto a ref."""

    openalex_id: str
    meta: dict[str, Any]
    authorships: list[dict[str, str]] = field(default_factory=list)

    @property
    def byline_authors(self) -> list[dict[str, str]]:
        """Author byline in the canonical storage shape (name+affiliation+ror)."""
        return to_author_dicts(self.authorships)


def normalize(work: dict[str, Any]) -> OpenAlexEnrichment:
    """Turn a raw OpenAlex work object into an :class:`OpenAlexEnrichment`.

    Pure — no I/O, tolerant of missing fields. The ``meta`` dict is the block
    stored under ``meta.openalex``.
    """
    oid = _short_id(work.get("id"))
    authorships = _authorships(work)
    meta: dict[str, Any] = {"v": ENRICH_VERSION}
    if oid:
        meta["id"] = oid

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
    if abstract:
        meta["abstract"] = abstract

    primary = work.get("primary_topic") or {}
    if isinstance(primary, dict) and primary.get("display_name"):
        meta["primary_topic"] = str(primary["display_name"]).strip()
    for src_key, dst_key in (
        ("topics", "topics"),
        ("concepts", "concepts"),
        ("keywords", "keywords"),
        ("funders", "funders"),
        ("sustainable_development_goals", "sdgs"),
        ("mesh", "mesh"),
    ):
        vals = _names(
            work.get(src_key),
            key="descriptor_name" if src_key == "mesh" else "display_name",
        )
        if vals:
            meta[dst_key] = vals

    for num_key in ("fwci", "cited_by_count", "referenced_works_count"):
        v = work.get(num_key)
        if isinstance(v, (int, float)):
            meta[num_key] = v

    refs = work.get("referenced_works")
    if isinstance(refs, list) and refs:
        meta["referenced_works"] = [_short_id(r) for r in refs if r]

    if work.get("is_retracted") is not None:
        meta["is_retracted"] = bool(work["is_retracted"])
    oa = work.get("open_access") or {}
    if isinstance(oa, dict) and oa.get("oa_status"):
        meta["oa_status"] = str(oa["oa_status"])
    if authorships:
        meta["authorships"] = authorships

    return OpenAlexEnrichment(openalex_id=oid, meta=meta, authorships=authorships)


def enrich_ref(
    store: Any,
    ref_id: int,
    *,
    doi: str | None = None,
    identifier: str | None = None,
    email: str = "",
    source: str = "openalex-enrich",
) -> OpenAlexEnrichment | None:
    """Fetch + normalize + write OpenAlex metadata onto ``ref_id``.

    The work is addressed by ``identifier`` — any scheme
    :func:`fetch_openalex_work_by_identifier` resolves (DOI, arXiv id,
    PubMed id, MAG id, or an OpenAlex ``W…`` id), so a bulk backfill can
    drive it with whichever alias a ref carries (gr514199). ``doi`` is
    kept for the DOI sweeps and is equivalent to ``identifier=<doi>``;
    exactly one of the two is required. Returns the enrichment (for
    reporting) or ``None`` when OpenAlex has no record for the
    identifier. Fills the ``authors`` byline column only when the ref
    currently has none, so a good existing byline is preserved. Likewise
    promotes a reconstructed abstract to top-level ``meta['abstract']``
    (what the paper page reads) only when the ref has none yet — a good
    existing abstract is never clobbered.

    The ``openalex:W…`` alias is registered via
    :meth:`~precis.store.Store.insert_ref_identifiers` (first-write-wins,
    never raises): an id already claimed by a different ref is a
    duplicate-ref data problem for the merge path, not a reason to kill
    a backfill sweep — the earlier ``set_ref_identifier`` write raised
    ``BadInput`` on exactly that claim (gr514199).
    """
    if doi is not None and identifier is not None:
        raise ValueError("enrich_ref takes doi= or identifier=, not both")
    ident = identifier if identifier is not None else doi
    if not ident:
        raise ValueError("enrich_ref needs doi= or identifier=")
    work = fetch_openalex_work_by_identifier(ident, email=email)
    if work is None:
        return None
    enr = normalize(work)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT authors, meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    current_authors = row[0] if row else None
    current_meta = (row[1] if row else None) or {}

    # Overwrite the byline only when the ref has no authors today.
    authors: list[dict[str, str]] | None = None
    if enr.byline_authors and not current_authors:  # None or empty list
        authors = enr.byline_authors

    meta_patch: dict[str, Any] = {"openalex": enr.meta}
    if enr.meta.get("abstract") and not current_meta.get("abstract"):
        meta_patch["abstract"] = enr.meta["abstract"]

    store.update_paper_fields(
        ref_id,
        authors=authors,
        meta_patch=meta_patch,
        source=source,
    )
    if enr.openalex_id:
        store.insert_ref_identifiers(ref_id, [("openalex", enr.openalex_id, source)])
    return enr


__all__ = [
    "ENRICH_VERSION",
    "OpenAlexEnrichment",
    "enrich_ref",
    "fetch_citing_works",
    "fetch_openalex_work",
    "fetch_openalex_work_by_id",
    "fetch_openalex_work_by_identifier",
    "normalize",
    "search_openalex_works",
]
