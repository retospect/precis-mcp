"""run_oa_fetch_pass — sibling worker that fetches OA PDFs.

Closes the chase ↔ stub loop. The finding-chase worker creates
stub paper refs (identifiers known, ``pdf_sha256 IS NULL``); this
worker walks the stub backlog and asks **three OA sources in
cascade** whether an open-access copy exists:

1. **Unpaywall** — aggregator of 30+ OA sources, by DOI. Free;
   TOS requires an email parameter.
2. **arXiv** — direct PDF download by arXiv ID. Free; no key.
3. **Semantic Scholar** — ``openAccessPdf`` field on the paper
   object, by DOI / arXiv / S2 id. Free; no key for low volume.

When the first source yields a PDF the cascade stops; later
sources only run when earlier ones returned ``no_oa_version`` or
``fetch_failed``. Each attempt writes its own ``ref_events`` row
(``source='fetcher:unpaywall'`` / ``'fetcher:arxiv'`` /
``'fetcher:s2'``) so the audit trail shows what was tried and
what worked.

When a PDF lands, it goes into the watch inbox →
``precis watch`` triggers ``precis_add`` → C7's
``register_aliases_and_maybe_upgrade`` promotes the stub →
the chase resumes on the next pass.

Per ADR 0018 this is a sibling worker (plain function), not a
``WorkerHandler`` subclass — same pattern as
``precis.workers.segment_toc`` and ``precis.workers.chase``.

Event vocabulary (same shape across all three sources):

- ``fetch_ok``       — PDF downloaded; payload carries url + bytes + license
- ``no_oa_version``  — source confirmed no OA copy
- ``fetch_failed``   — source returned a URL but download failed
- ``rate_limited``   — 429 / equivalent throttle
- ``api_error``      — 5xx / network / unexpected JSON
- ``invalid_identifier`` — the identifier failed format validation
- ``identifier_missing`` — the ref had no usable identifier for this source

``precis stubs`` (CLI subcommand, Step 4) reads the latest event
per stub to render the backlog.

Pre-existing relative: ``scripts/_doilist.py`` (``doilist scan
--download``) is an *operator-facing* CLI that drives Unpaywall
fetches from a curated ``dois_to_get.md`` file. Different starting
point (file-driven, not stub-driven); different output (``downloads/``
dir, no DB writes); both can coexist.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from psycopg import Connection
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


_SOURCE_UNPAYWALL = "fetcher:unpaywall"
_SOURCE_ARXIV = "fetcher:arxiv"
_SOURCE_S2 = "fetcher:s2"

# The retry-window predicate matches any ``fetcher:%`` source rather
# than one canonical provider: the window must arm after whichever
# provider actually ran. Keying it on a single provider that may be
# disabled (e.g. Unpaywall without an email) silently defeats the
# guard and turns the cascade into a per-pass spin loop.

# Unpaywall public API root. Free; requires ``email=`` per their TOS.
# https://unpaywall.org/products/api
_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"

# Default per-request timeout. Generous — Unpaywall sometimes takes
# a few seconds on cold edges; PDF downloads need their own budget.
_API_TIMEOUT_S = 30.0
_DOWNLOAD_TIMEOUT_S = 120.0

# Retry policy on rate-limit + transient API errors. Exponential
# backoff matches the pattern in ``precis.ingest.citations``.
_RETRY_MAX_ATTEMPTS = 4

# Don't re-poke Unpaywall for the same stub within this window. The
# fetcher's claim query honours it via a LEFT JOIN on ref_events.
_RETRY_WINDOW_HOURS = 24

# DOI shape validation. Loose but rejects obviously-broken inputs
# before paying for an HTTP roundtrip.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# arXiv id shape. Accepts both new-style (``2401.12345`` /
# ``2401.12345v2``) and old-style (``hep-th/9901001``). The
# bare-id regex below is permissive — arXiv normalises its URL.
_ARXIV_ID_RE = re.compile(
    r"^([a-z\-]+/)?\d{4}\.?\d{4,5}(v\d+)?$|^[a-z\-]+/\d{7}(v\d+)?$"
)


# ── Result type ────────────────────────────────────────────────────


@dataclass
class FetchOutcome:
    """Per-stub outcome the runner aggregates into BatchResult."""

    event: str  # 'fetch_ok' | 'no_oa_version' | ...
    payload: dict[str, Any]
    duration_ms: int
    cost_usd: float | None = None  # always None for Unpaywall (free)


# ── Claim query ────────────────────────────────────────────────────


@dataclass(frozen=True)
class StubRef:
    """Stub identifiers the cascade tries each source against.

    At least one of ``doi`` / ``arxiv`` / ``s2_id`` is non-None
    by construction of the claim query — a stub with no usable
    identifier is excluded.
    """

    ref_id: int
    doi: str | None
    arxiv: str | None
    s2_id: str | None
    cite_key: str | None


def claim_stubs_to_fetch(
    conn: Connection,
    *,
    limit: int,
    retry_after_hours: int = _RETRY_WINDOW_HOURS,
) -> list[StubRef]:
    """Lock and return up to ``limit`` stubs needing an OA fetch attempt.

    A stub qualifies when ``refs.pdf_sha256 IS NULL`` AND at least one
    of {DOI, arXiv, S2 id} is registered. Excludes stubs tried by *any*
    ``fetcher:%`` source within ``retry_after_hours`` — the retry window
    applies cross-source by convention (if one source had nothing N
    hours ago, the others probably won't either).

    Keying the window on the literal ``fetcher:unpaywall`` source used
    to defeat the whole guard in deployments where Unpaywall is
    disabled (no ``PRECIS_UNPAYWALL_EMAIL``): the cascade then only ran
    arXiv + S2, never wrote an ``unpaywall`` event, so the window never
    armed and every stub re-qualified on every pass — re-polling S2
    hundreds of times a day. Matching ``fetcher:%`` arms the window
    after whichever provider actually ran.

    Returns newest-stub-first (``ORDER BY ref_id DESC``) so the chase
    bottleneck shows up promptly when a chain creates many stubs.
    ``FOR UPDATE OF r SKIP LOCKED`` lets multiple fetcher workers run
    in parallel.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = conn.execute(
        """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi')      AS doi,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'arxiv')    AS arxiv,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 's2')       AS s2_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key
          FROM refs r
          LEFT JOIN LATERAL (
                SELECT 1 FROM ref_events e
                 WHERE e.ref_id = r.ref_id
                   AND e.source LIKE 'fetcher:%%'
                   AND e.ts > now() - (%s || ' hours')::INTERVAL
                 LIMIT 1
          ) recent_event ON TRUE
         WHERE r.kind = 'paper'
           AND r.pdf_sha256 IS NULL
           AND r.deleted_at IS NULL
           AND recent_event IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_identifiers ri
                  WHERE ri.ref_id = r.ref_id
                    AND ri.id_kind IN ('doi', 'arxiv', 's2')
           )
         ORDER BY r.ref_id DESC
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (str(retry_after_hours), limit),
    ).fetchall()
    return [
        StubRef(
            ref_id=int(r[0]),
            doi=r[1],
            arxiv=r[2],
            s2_id=r[3],
            cite_key=r[4],
        )
        for r in rows
    ]


# ── Per-stub logic ─────────────────────────────────────────────────


def _try_unpaywall(
    stub: StubRef,
    *,
    inbox_dir: Path,
    email: str,
) -> FetchOutcome | None:
    """Try Unpaywall for one stub.

    Returns ``None`` when there's no DOI to try (the cascade falls
    through to the next provider). Returns a :class:`FetchOutcome`
    otherwise — caller writes the corresponding event and stops the
    cascade on ``fetch_ok``.
    """
    if not stub.doi:
        return None  # no DOI → not Unpaywall's problem
    t0 = time.perf_counter()

    if not _DOI_RE.match(stub.doi):
        return FetchOutcome(
            event="invalid_identifier",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )

    try:
        data = _query_unpaywall(stub.doi, email=email)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return FetchOutcome(
                event="rate_limited",
                payload={
                    "doi": stub.doi,
                    "retry_after": exc.response.headers.get("retry-after"),
                },
                duration_ms=_ms(t0),
            )
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "status": exc.response.status_code},
            duration_ms=_ms(t0),
        )
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    oa = (data or {}).get("best_oa_location") or {}
    url = oa.get("url_for_pdf") or oa.get("url")
    if not url:
        return FetchOutcome(
            event="no_oa_version",
            payload={
                "doi": stub.doi,
                "is_oa": (data or {}).get("is_oa"),
                "oa_status": (data or {}).get("oa_status"),
            },
            duration_ms=_ms(t0),
        )

    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": url,
            "size_bytes": size_bytes,
            "license": oa.get("license"),
            "host_type": oa.get("host_type"),
            "version": oa.get("version"),
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_arxiv(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try direct arXiv PDF fetch for one stub.

    arXiv hosts every preprint as a PDF at a deterministic URL:
    ``https://arxiv.org/pdf/<id>.pdf`` (also accepts versioned ids
    like ``2401.12345v2``). No API key, no metadata round-trip,
    no rate-limit issues for occasional fetches.

    Returns ``None`` when the stub has no arXiv id (cascade falls
    through). When the id is present, attempts download and writes
    the outcome.
    """
    if not stub.arxiv:
        return None
    t0 = time.perf_counter()
    arxiv_id = stub.arxiv.strip()
    # Accept ``arxiv:`` prefixed values defensively.
    arxiv_id = arxiv_id.removeprefix("arxiv:")
    if not _ARXIV_ID_RE.match(arxiv_id):
        return FetchOutcome(
            event="invalid_identifier",
            payload={"arxiv": stub.arxiv},
            duration_ms=_ms(t0),
        )

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"arxiv": arxiv_id, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "arxiv": arxiv_id,
            "url": url,
            "size_bytes": size_bytes,
            "license": "arxiv",  # whatever arXiv licence the author chose
            "host_type": "preprint",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_s2(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try Semantic Scholar's ``openAccessPdf`` for one stub.

    Uses the existing :mod:`semanticscholar` client (already in
    the dep tree via :mod:`precis.ingest.citations`). S2 indexes
    OA PDFs across publishers + repositories with comparable
    coverage to Unpaywall — useful as a fallback when Unpaywall
    returns ``no_oa_version`` (the two sources don't agree 100%
    on what's OA).

    Identifier priority: DOI > arXiv > S2 id (any single one is
    sufficient for ``get_paper``).
    """
    paper_id_for_s2 = None
    if stub.doi:
        paper_id_for_s2 = f"doi:{stub.doi}"
    elif stub.arxiv:
        paper_id_for_s2 = f"ARXIV:{stub.arxiv.removeprefix('arxiv:')}"
    elif stub.s2_id:
        paper_id_for_s2 = stub.s2_id
    if paper_id_for_s2 is None:
        return None

    t0 = time.perf_counter()
    try:
        oa_url = _query_s2_openaccess(paper_id_for_s2)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"paper_id": paper_id_for_s2, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    if not oa_url:
        return FetchOutcome(
            event="no_oa_version",
            payload={"paper_id": paper_id_for_s2},
            duration_ms=_ms(t0),
        )

    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(oa_url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={
                "paper_id": paper_id_for_s2,
                "url": oa_url,
                "error": str(exc)[:200],
            },
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "paper_id": paper_id_for_s2,
            "url": oa_url,
            "size_bytes": size_bytes,
            "host_type": "s2_openaccess",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


# ── Runner ─────────────────────────────────────────────────────────


def run_oa_fetch_pass(
    store: Any,
    *,
    limit: int = 8,
    inbox_dir: Path | str | None = None,
    email: str | None = None,
) -> dict[str, int]:
    """Process up to ``limit`` stubs through the OA fetcher cascade.

    Per stub: tries Unpaywall → arXiv → S2 in order. The cascade
    stops at the first ``fetch_ok``; intermediate ``no_oa_version``
    / ``fetch_failed`` outcomes still produce an audit event and
    fall through to the next source. ``invalid_identifier`` /
    ``identifier_missing`` outcomes also fall through.

    Each stub is tried independently; an unhandled exception in
    one provider logs ``api_error`` for that source and moves on
    to the next. Returns the BatchResult shape: ``{claimed, ok,
    failed}``. ``ok`` counts stubs the cascade processed without
    a fatal exception (regardless of whether a PDF actually
    landed); ``failed`` counts only unhandled exceptions that
    escaped all three providers.

    ``email`` defaults to ``PRECIS_UNPAYWALL_EMAIL``. Without one,
    Unpaywall is skipped (with an ``identifier_missing``-style
    event) — arXiv and S2 still run.

    ``inbox_dir`` defaults to ``PRECIS_WATCH_INBOX`` or
    ``~/work/new_papers/_oa_fetched``.
    """
    email = email or os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()
    if inbox_dir is None:
        inbox_dir = os.environ.get("PRECIS_WATCH_INBOX") or str(
            Path.home() / "work" / "new_papers" / "_oa_fetched"
        )
    inbox_path = Path(inbox_dir)

    with store.pool.connection() as conn:
        stubs = claim_stubs_to_fetch(conn, limit=limit)

    if not stubs:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = 0
    failed = 0
    for stub in stubs:
        try:
            _run_cascade(store, stub, inbox_path, email)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "fetch_oa: ref_id=%s unhandled error: %s",
                stub.ref_id,
                exc,
                exc_info=True,
            )
            failed += 1
    return {"claimed": len(stubs), "ok": ok, "failed": failed}


def _run_cascade(store: Any, stub: StubRef, inbox_dir: Path, email: str) -> None:
    """Walk providers in order; stop at first fetch_ok.

    Records every attempted provider's outcome via append_event.
    Email-less Unpaywall is silently skipped (the email gate is
    enforced at the provider, not the cascade level, so arXiv +
    S2 still run).
    """
    providers: list[tuple[str, Any]] = []
    if email:
        providers.append(
            (
                _SOURCE_UNPAYWALL,
                lambda: _try_unpaywall(stub, inbox_dir=inbox_dir, email=email),
            )
        )
    providers.append((_SOURCE_ARXIV, lambda: _try_arxiv(stub, inbox_dir=inbox_dir)))
    providers.append((_SOURCE_S2, lambda: _try_s2(stub, inbox_dir=inbox_dir)))

    for source, runner in providers:
        try:
            outcome = runner()
        except Exception as exc:
            store.append_event(
                stub.ref_id,
                source=source,
                event="api_error",
                payload={"error": str(exc)[:200]},
            )
            continue
        if outcome is None:
            # No identifier for this source — silent skip; no event.
            continue
        store.append_event(
            stub.ref_id,
            source=source,
            event=outcome.event,
            payload=outcome.payload,
            duration_ms=outcome.duration_ms,
            cost_usd=outcome.cost_usd,
        )
        if outcome.event == "fetch_ok":
            return


# ── Internals ──────────────────────────────────────────────────────


@retry(
    wait=wait_exponential(min=1, max=30),
    stop=stop_after_attempt(_RETRY_MAX_ATTEMPTS),
    retry=retry_if_exception_type(
        (httpx.TransportError, httpx.ReadTimeout, httpx.WriteTimeout)
    ),
    reraise=True,
)
def _query_unpaywall(doi: str, *, email: str) -> dict[str, Any]:
    """Hit GET /v2/<doi>?email=… and return the parsed JSON.

    Retries on transport / timeout errors; HTTPStatusError (400/404/
    429/5xx) bubbles up to the caller for per-status handling. The
    return shape is documented at https://unpaywall.org/data-format.
    """
    url = f"{_UNPAYWALL_BASE}/{doi}"
    with httpx.Client(timeout=_API_TIMEOUT_S) as client:
        resp = client.get(url, params={"email": email})
        resp.raise_for_status()
        return resp.json()


def _download_pdf(url: str, target: Path) -> int:
    """Stream a PDF to ``target`` and return the byte count.

    Streams so a 50 MB PDF doesn't sit in memory. Atomic-ish:
    writes to ``<target>.part`` and renames on success so a crashed
    download doesn't leave a half-file the watcher would try to
    ingest.

    Validates the first 5 bytes are ``%PDF-`` before keeping the
    file — some publishers return HTML interstitial pages with 200
    OK; saving those would poison the watcher (Marker would barf).
    """
    from precis.utils.safe_fetch import safe_stream

    tmp = target.with_suffix(target.suffix + ".part")
    size = 0
    head = b""
    # follow_redirects=False — safe_stream walks the chain itself,
    # revalidating each Location. Original code set this True with
    # only is_http_url() shape validation on ``url``, so a publisher
    # redirect to 169.254.169.254 / 127.0.0.1 would be followed and
    # the magic-byte check at the end could be defeated by a server
    # that echoes %PDF- bytes.
    with httpx.Client(
        timeout=_DOWNLOAD_TIMEOUT_S,
        follow_redirects=False,
        headers=_DOWNLOAD_HEADERS,
    ) as client:
        with safe_stream(client, "GET", url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    if size == 0:
                        head = chunk[:8]
                    fh.write(chunk)
                    size += len(chunk)
    if not head.startswith(b"%PDF-"):
        tmp.unlink(missing_ok=True)
        raise ValueError(f"response is not a PDF (got {size} bytes, head={head!r})")
    tmp.rename(target)
    return size


# Polite UA — publishers (PeerJ, arXiv) sometimes 403 the default
# httpx UA. Identify ourselves and include the contact env when
# present so an annoyed sysadmin can ping us rather than blocking.
_USER_AGENT = (
    "precis-mcp/8.0 (+https://github.com/retostamm/precis-mcp; mailto:{email})"
)


def _user_agent_header(email: str | None = None) -> str:
    return _USER_AGENT.format(
        email=email or os.environ.get("PRECIS_UNPAYWALL_EMAIL", "noreply@example.com")
    )


_DOWNLOAD_HEADERS = {
    "User-Agent": _user_agent_header(),
    # Some hosts insist on an Accept header that names PDFs explicitly.
    "Accept": "application/pdf,*/*;q=0.8",
}


def _ms(t0: float) -> int:
    """Helper: monotonic millisecond delta from ``t0``."""
    return int((time.perf_counter() - t0) * 1000)


def _stub_filename(stub: StubRef) -> str:
    """Filename stem for the downloaded PDF.

    Prefer the cite_key (clean, human-readable); fall back to a
    DOI- or arXiv-derived slug; last resort, ``ref_<id>``.
    """
    if stub.cite_key:
        return _sanitise(stub.cite_key)
    if stub.doi:
        return _sanitise(stub.doi)
    if stub.arxiv:
        return _sanitise(stub.arxiv.removeprefix("arxiv:"))
    return f"ref_{stub.ref_id}"


def _sanitise(s: str) -> str:
    """Strip a string to filename-safe characters, capped at 80 chars."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s.lower())[:80]


def _query_s2_openaccess(paper_id: str) -> str | None:
    """Look up the ``openAccessPdf`` URL for a paper via Semantic Scholar.

    Uses the existing :mod:`semanticscholar` client (no API key
    needed for low volume). Returns the URL or ``None`` when S2
    has no OA PDF on file.

    Lifted up here rather than added to
    :mod:`precis.ingest.semantic_scholar` because the existing
    module's ``_normalize`` shape doesn't carry the
    ``openAccessPdf`` field — splitting the fetcher's S2 surface
    keeps the existing metadata-lookup path untouched.
    """
    import os as _os

    from semanticscholar import SemanticScholar

    api_key = _os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
    paper = sch.get_paper(paper_id, fields=["openAccessPdf"])
    if not paper:
        return None
    oa = getattr(paper, "openAccessPdf", None) or {}
    if isinstance(oa, dict):
        url = oa.get("url")
    else:
        url = getattr(oa, "url", None)
    return str(url) if url else None


__all__ = [
    "FetchOutcome",
    "StubRef",
    "claim_stubs_to_fetch",
    "run_oa_fetch_pass",
]
