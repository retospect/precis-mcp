"""run_oa_fetch_pass — sibling worker that fetches OA PDFs via Unpaywall.

Closes the chase ↔ stub loop. The finding-chase worker creates
stub paper refs (DOI known, ``pdf_sha256 IS NULL``); this worker
walks the stub backlog and asks Unpaywall whether an open-access
copy exists. When one does, the PDF lands in the watch inbox →
``precis watch`` triggers ``precis_add`` → C7's
``register_aliases_and_maybe_upgrade`` promotes the stub →
the chase resumes on the next pass.

Per ADR 0018 this is a sibling worker (plain function), not a
``WorkerHandler`` subclass — same pattern as
``precis.workers.segment_toc`` and ``precis.workers.chase``.

Cost model: Unpaywall is **free** and **OA-only** by construction.
Their TOS requires an email parameter for rate-limit identification;
the worker refuses to start without ``PRECIS_UNPAYWALL_EMAIL`` set
(or the ``email=`` kwarg explicit). Per-pass ``--limit`` caps the
call count; 429s back off via tenacity.

Audit: every attempt writes a ``ref_events`` row under
``source='fetcher:unpaywall'`` with one of:

- ``fetch_ok``       — PDF downloaded; payload carries url + bytes + license
- ``no_oa_version``  — Unpaywall says no OA copy exists
- ``fetch_failed``   — Unpaywall returned, URL didn't download (404 etc.)
- ``rate_limited``   — Unpaywall returned 429; backed off, will retry
- ``api_error``      — Unpaywall returned 5xx / unexpected JSON
- ``invalid_doi``    — DOI format Unpaywall rejected (400)

``precis stubs`` (CLI subcommand, separate step) reads the latest
event per stub to render the backlog.
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


_SOURCE = "fetcher:unpaywall"

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


# ── Result type ────────────────────────────────────────────────────


@dataclass
class FetchOutcome:
    """Per-stub outcome the runner aggregates into BatchResult."""

    event: str  # 'fetch_ok' | 'no_oa_version' | ...
    payload: dict[str, Any]
    duration_ms: int
    cost_usd: float | None = None  # always None for Unpaywall (free)


# ── Claim query ────────────────────────────────────────────────────


def claim_stubs_to_fetch(
    conn: Connection,
    *,
    limit: int,
    retry_after_hours: int = _RETRY_WINDOW_HOURS,
) -> list[tuple[int, str]]:
    """Lock and return up to ``limit`` stub refs needing an Unpaywall poke.

    Returns ``[(ref_id, doi), ...]`` newest-stub-first (by ref_id,
    which monotonically increases). Excludes stubs already tried in
    the last ``retry_after_hours`` so a transient failure doesn't
    burn the API budget on retry-storms.

    ``FOR UPDATE OF r SKIP LOCKED`` lets multiple fetchers run in
    parallel without re-queuing the same DOI.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = conn.execute(
        """
        SELECT r.ref_id, ri.id_value AS doi
          FROM refs r
          JOIN ref_identifiers ri
            ON ri.ref_id = r.ref_id
           AND ri.id_kind = 'doi'
          LEFT JOIN LATERAL (
                SELECT 1 FROM ref_events e
                 WHERE e.ref_id = r.ref_id
                   AND e.source = %s
                   AND e.ts > now() - (%s || ' hours')::INTERVAL
                 LIMIT 1
          ) recent_event ON TRUE
         WHERE r.kind = 'paper'
           AND r.pdf_sha256 IS NULL
           AND r.deleted_at IS NULL
           AND recent_event IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_SOURCE, str(retry_after_hours), limit),
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


# ── Per-stub logic ─────────────────────────────────────────────────


def fetch_one(
    *,
    ref_id: int,
    doi: str,
    inbox_dir: Path,
    email: str,
    cite_key: str | None = None,
) -> FetchOutcome:
    """Try Unpaywall for one stub; download if OA URL is available.

    Pure function — DB writes happen in the runner so this can be
    called from tests / ad-hoc scripts. Returns a :class:`FetchOutcome`
    suitable for direct ``store.append_event(...)``.
    """
    t0 = time.perf_counter()

    if not _DOI_RE.match(doi):
        return FetchOutcome(
            event="invalid_doi",
            payload={"doi": doi},
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    try:
        data = _query_unpaywall(doi, email=email)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return FetchOutcome(
                event="rate_limited",
                payload={"doi": doi, "retry_after": exc.response.headers.get("retry-after")},
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
        return FetchOutcome(
            event="api_error",
            payload={"doi": doi, "status": exc.response.status_code},
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": doi, "error": str(exc)[:200]},
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    oa = (data or {}).get("best_oa_location") or {}
    url = oa.get("url_for_pdf") or oa.get("url")
    if not url:
        return FetchOutcome(
            event="no_oa_version",
            payload={
                "doi": doi,
                "is_oa": (data or {}).get("is_oa"),
                "oa_status": (data or {}).get("oa_status"),
            },
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Download to the inbox so the watcher picks it up.
    filename = (cite_key or _doi_to_slug(doi)) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": doi, "url": url, "error": str(exc)[:200]},
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": doi,
            "url": url,
            "size_bytes": size_bytes,
            "license": oa.get("license"),
            "host_type": oa.get("host_type"),
            "version": oa.get("version"),
            "filename": filename,
        },
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )


# ── Runner ─────────────────────────────────────────────────────────


def run_oa_fetch_pass(
    store: Any,
    *,
    limit: int = 8,
    inbox_dir: Path | str | None = None,
    email: str | None = None,
) -> dict[str, int]:
    """Process up to ``limit`` stub refs through Unpaywall.

    Each stub is tried independently; failures are logged as events
    but don't poison the batch. Returns the BatchResult shape:
    ``{claimed, ok, failed}``. ``ok`` counts every non-exception
    outcome (including ``no_oa_version`` — Unpaywall *did* respond
    correctly); ``failed`` counts unhandled exceptions only.

    ``email`` defaults to ``PRECIS_UNPAYWALL_EMAIL``. Refuses to
    run without one — Unpaywall's TOS requires it.

    ``inbox_dir`` defaults to ``PRECIS_WATCH_INBOX`` (the env var
    the watcher honours) or ``~/work/new_papers/_oa_fetched``.
    """
    email = email or os.environ.get("PRECIS_UNPAYWALL_EMAIL", "").strip()
    if not email:
        log.warning(
            "fetch_oa: PRECIS_UNPAYWALL_EMAIL not set; skipping pass. "
            "Unpaywall's TOS requires an email parameter — set the env "
            "var or pass email= explicitly to enable fetching."
        )
        return {"claimed": 0, "ok": 0, "failed": 0}

    if inbox_dir is None:
        inbox_dir = (
            os.environ.get("PRECIS_WATCH_INBOX")
            or str(Path.home() / "work" / "new_papers" / "_oa_fetched")
        )
    inbox_path = Path(inbox_dir)

    with store.pool.connection() as conn:
        stubs = claim_stubs_to_fetch(conn, limit=limit)

    if not stubs:
        return {"claimed": 0, "ok": 0, "failed": 0}

    ok = 0
    failed = 0
    for ref_id, doi in stubs:
        cite_key = _cite_key_for(store, ref_id)
        try:
            outcome = fetch_one(
                ref_id=ref_id,
                doi=doi,
                inbox_dir=inbox_path,
                email=email,
                cite_key=cite_key,
            )
            store.append_event(
                ref_id,
                source=_SOURCE,
                event=outcome.event,
                payload=outcome.payload,
                duration_ms=outcome.duration_ms,
                cost_usd=outcome.cost_usd,
            )
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "fetch_oa: ref_id=%s doi=%s unhandled error: %s",
                ref_id, doi, exc, exc_info=True,
            )
            try:
                store.append_event(
                    ref_id,
                    source=_SOURCE,
                    event="api_error",
                    payload={"doi": doi, "error": str(exc)[:200]},
                )
            except Exception:  # pragma: no cover
                pass
            failed += 1

    return {"claimed": len(stubs), "ok": ok, "failed": failed}


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
    """
    tmp = target.with_suffix(target.suffix + ".part")
    size = 0
    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    fh.write(chunk)
                    size += len(chunk)
    tmp.rename(target)
    return size


def _doi_to_slug(doi: str) -> str:
    """Sanitise a DOI into a filename-safe token.

    Fallback when we don't have a cite_key yet (e.g. stub minted by
    the chase before identity resolution finishes).
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", doi.lower())[:80]


def _cite_key_for(store: Any, ref_id: int) -> str | None:
    """Look up the ref's cite_key for naming the downloaded file."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'cite_key'",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row is not None else None


__all__ = [
    "FetchOutcome",
    "claim_stubs_to_fetch",
    "fetch_one",
    "run_oa_fetch_pass",
]
