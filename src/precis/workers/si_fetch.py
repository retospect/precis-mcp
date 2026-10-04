"""SI pass — fetch a flagged paper's supplementary information.

Trigger: ``put(kind='paper', id=<slug>, mode='fetch-si')`` only stamps
``refs.meta.si_fetch = {requested_at, by}`` (the MCP server fetches nothing).
This pass, run by the ``fetch_oa`` lane **before** its stub claim (so an
attention-queued SI fetch goes ahead of the stub backlog), claims up to
``limit`` flagged parents, discovers their SI
(:mod:`precis.ingest.si_discovery`), downloads each PDF into the watch inbox
with ``fetch_oa._download_pdf`` and a ``role='supplement'`` sidecar naming the
PARENT ``ref_id`` — the watcher then mints each file as its own linked ref
(``precis.ingest.add._ingest_supplement``).

Whatever happens, the parent records the check (``meta.si_checked_at``,
``si_found``, ``si_fetched``, ``si_misses``, ``si_skipped``) plus one
``ref_events`` row, so a paper is tried once per request: it is re-claimed only
by a newer ``si_fetch.requested_at``. Exception: a check cut short by the pass
budget (any ``deadline`` miss, or a parent claimed after the deadline, which
runs no discovery) is re-armed — ``si_fetch.requested_at`` is bumped past the
claim stamp and ``si_fetch.deadline_retries`` incremented — so the next pass
re-claims it; after ``MAX_DEADLINE_RETRIES`` (3) the misses stand as recorded.
The same re-arm (same counter and cap, which therefore covers both causes)
applies when no PDF was queued and a miss is a transient network error
(``error:ConnectTimeout`` and kin, ``download_timeout``): a host-side blip must
not burn the paper's one check. ``cloudflare_403``, HTTP 4xx and ``cap`` never
re-arm.

The event source is ``si_fetch``, deliberately NOT ``fetcher:*``: those
sources arm the stub claim's retry window / backoff (``claim_stubs_to_fetch``),
and an SI check on a still-unfetched parent must not delay its main-PDF retry.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.ingest.fetch_sidecar import sidecar_path, write_sidecar
from precis.ingest.si_discovery import (
    DiscoveryResult,
    FetchFn,
    SiCandidate,
    default_fetch,
    discover,
)
from precis.store import Store
from precis.store.si_links import utc_stamp

log = logging.getLogger(__name__)

#: ``ref_events.source`` for SI passes (see module docstring on why not ``fetcher:``).
SI_EVENT_SOURCE = "si_fetch"
#: Sidecar ``source`` stamped on a fetched SI file.
SI_SIDECAR_SOURCE = "fetcher:si"
DEFAULT_SI_CLAIM_LIMIT = 2
#: Downloaded PDFs per parent; the rest are recorded as ``cap`` misses.
MAX_SI_PDFS_PER_PARENT = 8
#: Wall-clock budget of one ``run_si_pass``; untried candidates are recorded
#: as ``deadline`` misses. Keeps the pass from monopolising the fetch lane.
SI_PASS_DEADLINE_S = 120.0
#: Re-arms (budget-cut or transient-network) per request before the misses stand.
MAX_DEADLINE_RETRIES = 3

#: Exception names (``error:<name>`` miss reasons) that mark a transient
#: network failure worth re-arming an otherwise empty check for.
_TRANSIENT_ERRORS = frozenset(
    {
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectError",
        "RemoteProtocolError",
        "PoolTimeout",
    }
)


def _is_transient(reason: str) -> bool:
    if reason == "download_timeout":
        return True
    return reason.startswith("error:") and reason[len("error:") :] in _TRANSIENT_ERRORS


@dataclass(frozen=True)
class SiParent:
    ref_id: int
    doi: str | None
    cite_key: str | None
    requested_at: str


def claim_si_parents(
    conn: Connection, *, limit: int = DEFAULT_SI_CLAIM_LIMIT
) -> list[SiParent]:
    """Claim up to ``limit`` papers whose SI was requested and not yet checked
    since that request. ``FOR UPDATE SKIP LOCKED``; the claim stamps
    ``si_checked_at`` immediately (same transaction), so a crash mid-pass or a
    second worker never re-runs the same request — a fresh request re-arms it.
    Explicit ``fetch-si`` requests (no ``si_fetch.trigger``) go before
    attention-queued ones, then oldest request first.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi'),
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'),
               r.meta->'si_fetch'->>'requested_at'
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND r.meta ? 'si_fetch'
           AND (r.meta->>'si_checked_at' IS NULL
                OR r.meta->>'si_checked_at' < r.meta->'si_fetch'->>'requested_at')
         ORDER BY (r.meta->'si_fetch'->>'trigger' IS NOT NULL),
                  r.meta->'si_fetch'->>'requested_at', r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (limit,),
    ).fetchall()
    parents = [
        SiParent(
            ref_id=int(r[0]),
            doi=str(r[1]) if r[1] else None,
            cite_key=str(r[2]) if r[2] else None,
            requested_at=str(r[3] or ""),
        )
        for r in rows
    ]
    stamp = utc_stamp()
    for p in parents:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('si_checked_at', %s::text) "
            "WHERE ref_id = %s",
            (stamp, p.ref_id),
        )
    return parents


def _download_reason(exc: BaseException) -> str:
    """Short miss reason for a failed SI download."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        if resp.status_code == 403 and "challenge" in (
            resp.headers.get("cf-mitigated", "").lower()
        ):
            return "cloudflare_403"
        return f"http_{resp.status_code}"
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError):
        return "download_timeout"
    if isinstance(exc, ValueError):
        return "not_pdf"
    return f"error:{type(exc).__name__}"


def _rearm(conn: Connection, ref_id: int) -> int | None:
    """Re-arm a budget-cut parent: bump ``si_fetch.requested_at`` to a fresh
    stamp (later than the claim's ``si_checked_at``) and count the retry.
    Returns the new ``deadline_retries``, or ``None`` when the cap is reached
    (nothing changed) or the parent carries no ``si_fetch``."""
    row = conn.execute(
        """
        UPDATE refs
           SET meta = jsonb_set(
                 meta, '{si_fetch}',
                 (meta->'si_fetch') || jsonb_build_object(
                   'requested_at', %s::text,
                   'deadline_retries',
                   COALESCE((meta->'si_fetch'->>'deadline_retries')::int, 0) + 1))
         WHERE ref_id = %s
           AND jsonb_exists(meta, 'si_fetch')
           AND COALESCE((meta->'si_fetch'->>'deadline_retries')::int, 0) < %s
        RETURNING (meta->'si_fetch'->>'deadline_retries')::int
        """,
        (utc_stamp(), ref_id, MAX_DEADLINE_RETRIES),
    ).fetchone()
    return int(row[0]) if row else None


def _fetch_one_parent(
    store: Store,
    parent: SiParent,
    inbox: Path,
    fetch: FetchFn,
    deadline: float | None = None,
) -> dict[str, int]:
    """Discover + download one parent's SI, then record the outcome."""
    from precis.workers import fetch_oa  # late: fetch_oa imports this module

    if deadline is not None and time.monotonic() >= deadline:
        # Claimed after the budget ran out: no network, just try again next pass.
        with store.pool.connection() as conn:
            late_retries = _rearm(conn, parent.ref_id)
            if late_retries is None:
                # Retry cap reached: record the miss so the parent does not
                # vanish silently (the claim stamp already blocks a re-claim).
                miss = [{"url": None, "source": "si_pass", "reason": "deadline"}]
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (
                        Jsonb(
                            {
                                "si_found": 0,
                                "si_fetched": 0,
                                "si_misses": miss,
                                "si_skipped": [],
                            }
                        ),
                        parent.ref_id,
                    ),
                )
            conn.commit()
        if late_retries is None:
            store.append_event(
                parent.ref_id,
                source=SI_EVENT_SOURCE,
                event="si_blocked",
                payload={
                    "candidates": [],
                    "queued": [],
                    "skipped": [],
                    "misses": miss,
                    "deadline_retries": MAX_DEADLINE_RETRIES,
                },
            )
        return {"found": 0, "fetched": 0}
    if parent.doi:
        found: DiscoveryResult = discover(parent.doi, fetch)
    else:
        found = DiscoveryResult(
            misses=[{"url": "", "source": "none", "reason": "no_doi"}]
        )
    cands: list[SiCandidate] = found.candidates
    misses: list[dict[str, str]] = list(found.misses)
    skipped: list[dict[str, str]] = []
    queued: list[dict[str, str]] = []
    stem = fetch_oa._sanitise(parent.cite_key or f"ref_{parent.ref_id}")
    for n, cand in enumerate(cands, start=1):
        if not cand.is_pdf:
            skipped.append(
                {"url": cand.url, "filename": cand.filename, "source": cand.source}
            )
            continue
        if len(queued) >= MAX_SI_PDFS_PER_PARENT:
            misses.append({"url": cand.url, "source": cand.source, "reason": "cap"})
            continue
        if deadline is not None and time.monotonic() >= deadline:
            misses.append(
                {"url": cand.url, "source": cand.source, "reason": "deadline"}
            )
            continue
        target = inbox / f"{stem}-si-{n:02d}.pdf"
        if target.exists():
            misses.append(
                {"url": cand.url, "source": cand.source, "reason": "already_in_inbox"}
            )
            continue
        # Staging convention (as fetch_oa's markup trigger): download into
        # ``.staging/``, write the sidecar there, then publish sidecar-first,
        # PDF-last — the watcher keys off the ``.pdf`` and so never sees one
        # without its ``role='supplement'`` sidecar.
        staging = inbox / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / target.name
        try:
            fetch_oa._download_pdf(cand.url, staged)
        except Exception as exc:
            misses.append(
                {
                    "url": cand.url,
                    "source": cand.source,
                    "reason": _download_reason(exc),
                }
            )
            continue
        write_sidecar(
            staged,
            ref_id=parent.ref_id,
            identifiers={"cite_key": parent.cite_key or "", "doi": ""},
            source=SI_SIDECAR_SOURCE,
            role="supplement",
            si={
                "source": cand.source,
                "url": cand.url,
                "component_doi": cand.component_doi or "",
            },
        )
        os.replace(sidecar_path(staged), sidecar_path(target))
        os.replace(staged, target)
        queued.append({"url": cand.url, "source": cand.source, "file": target.name})

    # ``si_checked_at`` is NOT rewritten here: the claim stamped it, and a new
    # request made during a long fetch must stay newer than it and survive.
    patch = {
        "si_found": len(cands),
        "si_fetched": len(queued),
        "si_misses": misses,
        "si_skipped": skipped,
    }
    retries: int | None = None
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb(patch), parent.ref_id),
        )
        if any(m["reason"] == "deadline" for m in misses) or (
            not queued and any(_is_transient(m["reason"]) for m in misses)
        ):
            retries = _rearm(conn, parent.ref_id)
        conn.commit()
    if queued:
        event = "si_found"
    elif any(
        m["reason"].startswith(("cloudflare", "http_403", "http_5", "error:"))
        or m["reason"] in ("download_timeout", "not_pdf")
        for m in misses
    ):
        # Something we could not see or get (challenge, 403, timeout, 5xx):
        # distinct from a clean "this paper has no SI".
        event = "si_blocked"
    else:
        event = "si_found" if cands else "si_none"
    store.append_event(
        parent.ref_id,
        source=SI_EVENT_SOURCE,
        event=event,
        payload={
            "candidates": [
                {
                    "url": c.url,
                    "source": c.source,
                    "filename": c.filename,
                    "component_doi": c.component_doi,
                }
                for c in cands
            ],
            "queued": queued,
            "skipped": skipped,
            "misses": misses,
            **({"rearmed": True, "deadline_retries": retries} if retries else {}),
        },
    )
    return {"found": len(cands), "fetched": len(queued)}


def run_si_pass(
    store: Store,
    inbox_dir: Path | str,
    *,
    limit: int = DEFAULT_SI_CLAIM_LIMIT,
    email: str = "",
    fetch: FetchFn | None = None,
    deadline_s: float = SI_PASS_DEADLINE_S,
) -> dict[str, Any]:
    """Claim flagged parents and fetch their SI. Returns
    ``{claimed, found, fetched, failed}``. Per-parent failures are logged and
    leave the claim stamp in place (no retry until a new request)."""
    with store.pool.connection() as conn:
        parents = claim_si_parents(conn, limit=limit)
        conn.commit()
    out: dict[str, Any] = {
        "claimed": len(parents),
        "found": 0,
        "fetched": 0,
        "failed": 0,
    }
    if not parents:
        return out
    fetch_fn = fetch if fetch is not None else default_fetch(email)
    inbox = Path(inbox_dir)
    deadline = time.monotonic() + deadline_s
    for parent in parents:
        try:
            res = _fetch_one_parent(store, parent, inbox, fetch_fn, deadline)
            out["found"] += res["found"]
            out["fetched"] += res["fetched"]
        except Exception as exc:
            log.warning(
                "si_fetch: ref_id=%s failed: %s", parent.ref_id, exc, exc_info=True
            )
            out["failed"] += 1
    return out


__all__ = [
    "SI_EVENT_SOURCE",
    "SiParent",
    "claim_si_parents",
    "run_si_pass",
    "utc_stamp",
]
