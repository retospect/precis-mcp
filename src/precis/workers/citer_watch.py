"""citer_watch — OpenAlex cited-by watch over the papers we actually use
(gr513769).

The second citation-forward acquisition leg, next to ``watch_poll`` (S2,
salience-rotation, hourly). Where watch_poll walks one most-due seed per
pass, this pass asks OpenAlex once a day "what *new* works cite the held
papers we keep reaching for" — the freshness side of the citation graph,
which ``watch_poll``'s per-seed rotation covers only slowly.

One pass (fired from the daily ``citer_watch`` scheduler cadence — the
lease is the fleet-singleton throttle, same as ``health_digest`` /
``dream_consolidate``):

1. **Seeds**: held papers (``kind='paper'``, ``pdf_sha256 IS NOT NULL``)
   carrying an OpenAlex id (``meta.openalex.id``, what
   :mod:`precis.ingest.openalex_meta.enrich_ref` writes), weighted by
   *use* — ``sum(chunks.accesses)`` over body chunks, the same heat
   signal ``bump_salience`` maintains. A seed must pass
   :data:`SEED_MIN_ACCESSES` uses and have been seen within
   :data:`SEED_IDLE_DAYS`; the heaviest :data:`SEED_LIMIT` make the cut.
2. **Poll**: ``GET /works?filter=cites:W1|W2|…,from_publication_date:
   <last poll>`` in batches of :data:`OA_CITES_BATCH` via
   :func:`precis.ingest.openalex_meta.fetch_citing_works` — one call per
   batch through the shared ``openalex`` limiter row, polite ``mailto``
   / paid ``api_key`` riding along automatically. ``last poll`` is the
   ``citer_watch:last_poll`` ``app_state`` row (first run looks back
   :data:`FIRST_LOOKBACK_DAYS` so the initial fire isn't a firehose);
   it advances only when every batch succeeded, so a failed batch's
   slice of the window is re-polled rather than lost.
3. **Score**: each citing work's ``referenced_works`` is intersected
   with the seed set — ``(distinct seeds cited, Σ seed weight)``,
   "how many held papers it cites and their weights" per the gripe.
   Retracted works are dropped.
4. **Queue**: the top scorers mint metadata-only stubs
   (:meth:`Store.upsert_stub_paper`, ``pdf_sha256 IS NULL`` — the same
   ingest-queue door ``watch_poll`` uses, so ``fetch_oa`` auto-claims
   them) until :data:`DAILY_INGEST_CAP` *new* stubs are minted for the
   UTC day. A collapse hit on an already-held/already-discovered paper
   is a no-op and doesn't spend cap. New stubs are tagged
   ``source:openalex`` + ``discovered-via:citer-watch``.
5. **Digest**: one ``kind='memory'`` note via
   :meth:`~precis.handlers.memory.MemoryHandler.write_system` (the
   reviewer-digest pattern, ``review.py``'s ``_write_digest``) listing
   the poll's counts and the top-scoring works — the "3 new papers
   cite your MCB sources" morning-brief line, minus the LLM. Written
   only when the poll found anything; a quiet day writes nothing.

**Ships dark.** The cadence fires daily and the lease advances, but the
pass no-ops until a ``service_config`` row lights ``citer_watch``
(``prio > 0``) — same gate shape as ``dream_consolidate``. Standing
enable is an operator decision: ``precis service prio '*' citer_watch
1`` (or the web Status → Services tab). A one-off manual run is
``precis worker --only citer_watch --once`` (``enabled=True`` — the
``--only`` operator escape hatch, same as every other pass). All HTTP
goes through the existing OpenAlex client — fixed host, shared limiter,
``call_with_backoff`` — so there is no SSRF surface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.store.types import Tag
from precis.utils.rate_limit import RateLimited

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The ``service_config`` service name an operator lights (``prio > 0``)
#: to arm the poll — the prod flag Reto flips; the cadence itself is
#: inert while dark.
SERVICE_NAME = "citer_watch"

#: Spend cap: each *new* stub minted queues a fetch_oa acquisition (an
#: Unpaywall call, possibly a PDF download + Marker parse), so the stub
#: queue is already minting far faster than fetch drains it (the
#: gripe's "~8k stubs, 7:1 mint:fetch"). Cap bounds how much new ingest
#: one day's poll can spend; only freshly-minted stubs count — a
#: collapse hit on a paper we already hold/discover is free.
DAILY_INGEST_CAP = 20

#: Seed gate: a held paper must have been *used* at least this many
#: times (``sum(chunks.accesses)`` — external access bumps only,
#: background actors don't self-heat) to earn a poll slot.
SEED_MIN_ACCESSES = 2
#: …and been seen within this window — a heavily-used-but-dormant paper
#: stops worth polling for freshness.
SEED_IDLE_DAYS = 365
#: Top-N seeds per poll ("a few hundred to a thousand" per the gripe —
#: 200 keeps the poll to ~4 API calls).
SEED_LIMIT = 200
#: OpenAlex OR-group size per ``cites:`` filter — kept comfortably under
#: the API's filter-value ceiling; 200 seeds → 4 calls.
OA_CITES_BATCH = 50
#: First run without a ``citer_watch:last_poll`` row looks back this
#: far, not to the dawn of time.
FIRST_LOOKBACK_DAYS = 30
#: Top-scoring lines carried into the digest note.
MAX_DIGEST_LINES = 10

_LAST_POLL_KEY = "citer_watch:last_poll"
_MINTED_PREFIX = "citer_watch:minted:"
_DIGEST_TAG = "digest:citer-watch"

#: Injectable for tests — ``(openalex_ids, since) -> [work dicts]``.
CitingWorksFetcher = Callable[[list[str], str], list[dict[str, Any]]]


@dataclass(frozen=True)
class _Seed:
    """One poll seed: a held, used paper and its use weight."""

    ref_id: int
    title: str
    openalex_id: str
    weight: int


@dataclass
class _Citer:
    """One new citing work, scored against the seed set."""

    openalex_id: str
    doi: str | None
    title: str
    year: int | None
    cited_seed_ids: set[str] = field(default_factory=set)
    weight_sum: int = 0


def citer_watch_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``citer_watch`` anywhere.
    Never raises: a missing table or a connection blip reads as dark —
    an unreadable gate must stop a minting pass, not arm it. Mirrors
    ``dream_consolidate_enabled``'s gate shape."""

    def _q(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (SERVICE_NAME,),
        ).fetchone()
        return row is not None

    try:
        if conn is not None:
            return _q(conn)
        with store.pool.connection() as c:
            return _q(c)
    except Exception:
        log.warning("citer_watch: service_config lookup failed", exc_info=True)
        return False


def _seeds(
    store: Store,
    *,
    min_accesses: int,
    idle_days: int,
    limit: int,
    now: datetime,
) -> list[_Seed]:
    """Held, used papers with an OpenAlex id, heaviest use first.

    ``weight`` = total ``chunks.accesses`` over the paper's body chunks
    (``ord >= 0``, the append-only convention) — "access count" per the
    gripe. ``max(last_seen)`` is the recency half of the gate.
    """
    cutoff = now - timedelta(days=idle_days)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   r.meta->'openalex'->>'id' AS oa_id,
                   COALESCE(SUM(c.accesses), 0) AS weight,
                   MAX(c.last_seen) AS seen
              FROM refs r
              JOIN chunks c
                ON c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
             WHERE r.kind = 'paper' AND r.retired_at IS NULL
               AND r.pdf_sha256 IS NOT NULL
               AND r.meta->'openalex'->>'id' IS NOT NULL
             GROUP BY r.ref_id
            HAVING COALESCE(SUM(c.accesses), 0) >= %s
               AND MAX(c.last_seen) >= %s
             ORDER BY weight DESC, r.ref_id
             LIMIT %s
            """,
            (min_accesses, cutoff, limit),
        ).fetchall()
    return [
        _Seed(
            ref_id=int(r[0]),
            title=str(r[1] or ""),
            openalex_id=str(r[2]),
            weight=int(r[3]),
        )
        for r in rows
    ]


def _minted_today(store: Store, day: date) -> int:
    """New stubs already minted on ``day`` (UTC) — the cap's spent side."""
    try:
        return max(0, int(store.get_setting(f"{_MINTED_PREFIX}{day}") or 0))
    except (TypeError, ValueError):
        return 0


def _score(works: list[dict[str, Any]], seeds_by_oa: dict[str, _Seed]) -> list[_Citer]:
    """Score each citing work: ``(distinct seeds cited, Σ seed weight)``.

    Deduped on OpenAlex id (a work can surface in more than one seed
    batch). A work whose ``referenced_works`` intersects no seed is
    dropped — OpenAlex OR-matched it to the batch, but if it carries no
    usable reference list we can't tell which seed it cites, and a
    ``referenced_works_count``-only row still minted a stub we can't
    attribute; the intersection is also what makes the digest honest.
    Retracted works never queue.
    """
    out: dict[str, _Citer] = {}
    for w in works:
        if w.get("is_retracted"):
            continue
        oid = str(w.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        if oid[:1].lower() == "w":
            oid = "W" + oid[1:]
        if not oid:
            continue
        citer = out.get(oid)
        if citer is None:
            raw_doi = w.get("doi") or ""
            doi = str(raw_doi).removeprefix("https://doi.org/") or None
            year = w.get("publication_year")
            citer = _Citer(
                openalex_id=oid,
                doi=doi,
                title=str(w.get("title") or "").strip(),
                year=int(year) if isinstance(year, int) else None,
            )
            out[oid] = citer
        for ref_w in w.get("referenced_works") or []:
            short = str(ref_w).rstrip("/").rsplit("/", 1)[-1].upper()
            seed = seeds_by_oa.get(short)
            if seed is not None:
                citer.cited_seed_ids.add(short)
    ranked = [c for c in out.values() if c.cited_seed_ids]
    for c in ranked:
        c.weight_sum = sum(seeds_by_oa[s].weight for s in c.cited_seed_ids)
    ranked.sort(
        key=lambda c: (len(c.cited_seed_ids), c.weight_sum, c.openalex_id),
        reverse=True,
    )
    return ranked


def _mint_stub(store: Store, citer: _Citer) -> bool:
    """Find-or-mint a stub for one citing work; True if newly minted."""
    ids = [("openalex", citer.openalex_id)]
    if citer.doi:
        ids.append(("doi", citer.doi))
    ref_id, created = store.upsert_stub_paper(
        identifiers=ids,
        title=citer.title or None,
        year=citer.year,
        set_by="system",
    )
    if created:
        for value in ("source:openalex", "discovered-via:citer-watch"):
            store.add_tag(ref_id, Tag.open(value), set_by="system")
    return created


def _write_digest(
    store: Store,
    *,
    today: str,
    since: str,
    seeds: int,
    found: int,
    minted: int,
    cap: int,
    top: list[_Citer],
) -> int:
    """Persist the poll as one ``kind='memory'`` note; return its ref id.

    The ``memory_body`` chunk makes the digest searchable; the open tags
    (``digest:citer-watch`` + per-day ``citer-watch:<date>``) are how a
    morning brief or an operator search finds it back.
    """
    from precis.dispatch import Hub

    lines = [
        f"citer-watch {today}: polled {seeds} used-and-held seeds "
        f"(OpenAlex, since {since}); {found} new citing works, "
        f"{minted} stubs queued (cap {cap}).",
    ]
    if top:
        lines.append("Top scoring:")
        for c in top[:MAX_DIGEST_LINES]:
            n = len(c.cited_seed_ids)
            lines.append(
                f"- {c.title or c.openalex_id} ({c.openalex_id}"
                f"{', ' + c.doi if c.doi else ''}) — cites {n} held"
            )
    handler = Hub(store=store).sibling("memory")
    return handler.write_system(
        text="\n".join(lines),
        title=f"citer-watch {today}",
        tags=[_DIGEST_TAG, f"citer-watch:{today}"],
        meta={
            "citer_watch_date": today,
            "citer_watch_since": since,
            "citer_watch_seeds": seeds,
            "citer_watch_found": found,
            "citer_watch_minted": minted,
        },
    )


def _default_fetch(email: str) -> CitingWorksFetcher:
    """The real OpenAlex fetcher behind the pass's injectable seam."""

    def _fetch(ids: list[str], since: str) -> list[dict[str, Any]]:
        from precis.ingest.openalex_meta import fetch_citing_works

        return fetch_citing_works(ids, since=since, email=email)

    return _fetch


def run_citer_watch_pass(
    store: Store,
    *,
    enabled: bool | None = None,
    fetch_works: CitingWorksFetcher | None = None,
    now: datetime | None = None,
    seed_min_accesses: int = SEED_MIN_ACCESSES,
    seed_idle_days: int = SEED_IDLE_DAYS,
    seed_limit: int = SEED_LIMIT,
    daily_cap: int = DAILY_INGEST_CAP,
    email: str | None = None,
) -> dict[str, int]:
    """One cited-by poll. Returns the BatchResult shape plus detail:
    ``claimed`` = seed papers polled, ``ok`` = new stubs minted,
    ``failed`` = seed batches whose OpenAlex fetch raised; ``found`` =
    new citing works seen, ``digest`` = the digest ref id (0 = none).

    ``enabled=None`` resolves the ``service_config`` gate (dark until a
    row lights it); the ``--only`` path passes ``True`` (the operator
    escape hatch). ``fetch_works`` is the recorded-fixture seam — the
    default calls OpenAlex through
    :func:`precis.ingest.openalex_meta.fetch_citing_works`, never raw
    HTTP. The daily cap is a spend cap: it bounds *new* stubs minted per
    UTC day regardless of how many fires the day sees.
    """
    from precis import settings as psettings

    now = now or datetime.now(UTC)
    today = now.date()
    if enabled is None:
        enabled = citer_watch_enabled(store)
    if not enabled:
        log.info(
            "citer_watch: dark — no live service_config row lights %s; no-op",
            SERVICE_NAME,
        )
        return {"claimed": 0, "ok": 0, "failed": 0, "found": 0, "digest": 0}

    mail = email
    if mail is None:
        mail = (psettings.get_str("contact.polite_email") or "").strip()
    fetch = fetch_works or _default_fetch(mail)

    last = store.get_setting(_LAST_POLL_KEY)
    if last:
        since = last
    else:
        since = (today - timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()

    seeds = _seeds(
        store,
        min_accesses=seed_min_accesses,
        idle_days=seed_idle_days,
        limit=seed_limit,
        now=now,
    )
    if not seeds:
        # Nothing used enough to seed — the window stays parked so a
        # paper that heats up later still sweeps the backlog since the
        # last successful poll.
        log.info("citer_watch: no seeds passed the use gate; no-op")
        return {"claimed": 0, "ok": 0, "failed": 0, "found": 0, "digest": 0}

    seeds_by_oa = {s.openalex_id.upper(): s for s in seeds}
    works: list[dict[str, Any]] = []
    claimed = 0
    failed = 0
    deferred = False
    for i in range(0, len(seeds), OA_CITES_BATCH):
        batch = seeds[i : i + OA_CITES_BATCH]
        try:
            works.extend(fetch([s.openalex_id for s in batch], since))
            claimed += len(batch)
        except RateLimited:
            # Shared openalex limiter refused (daily quota spent or
            # rate-starved): every later batch would be refused too, so
            # stop — the unpollled slice is re-claimed next fire because
            # last_poll only advances on a fully-clean run.
            log.info(
                "citer_watch: shared limiter refused at batch %d; deferring",
                i // OA_CITES_BATCH,
            )
            deferred = True
            break
        except Exception:
            log.exception(
                "citer_watch: citing-works fetch failed for batch %d",
                i // OA_CITES_BATCH,
            )
            failed += 1

    ranked = _score(works, seeds_by_oa)
    minted = 0
    spent = _minted_today(store, today)
    cap_left = max(0, daily_cap - spent)
    for citer in ranked:
        if cap_left <= 0:
            break
        try:
            if _mint_stub(store, citer):
                minted += 1
                cap_left -= 1
        except Exception:
            log.exception("citer_watch: stub mint failed for %s", citer.openalex_id)
            failed += 1
    if minted:
        store.set_setting(f"{_MINTED_PREFIX}{today}", str(spent + minted))

    digest = 0
    if ranked:
        try:
            digest = _write_digest(
                store,
                today=today.isoformat(),
                since=since,
                seeds=claimed,
                found=len(ranked),
                minted=minted,
                cap=daily_cap,
                top=ranked,
            )
        except Exception:
            log.exception("citer_watch: digest write failed")

    if not failed and not deferred:
        # The whole window polled clean — park the next fire at today.
        store.set_setting(_LAST_POLL_KEY, today.isoformat())

    log.info(
        "citer_watch: seeds=%d found=%d minted=%d (cap %d, spent %d) "
        "failed=%d digest=%d since=%s",
        claimed,
        len(ranked),
        minted,
        daily_cap,
        spent + minted,
        failed,
        digest,
        since,
    )
    return {
        "claimed": claimed,
        "ok": minted,
        "failed": failed,
        "found": len(ranked),
        "digest": digest,
    }


__all__ = [
    "DAILY_INGEST_CAP",
    "OA_CITES_BATCH",
    "SEED_IDLE_DAYS",
    "SEED_LIMIT",
    "SEED_MIN_ACCESSES",
    "SERVICE_NAME",
    "citer_watch_enabled",
    "run_citer_watch_pass",
]
