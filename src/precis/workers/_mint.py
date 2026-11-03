"""``mint_background_job`` — the one insert door for system-minted jobs.

Every ``kind='job'`` row carries ``meta.origin``, the scheduling class
job-scheduling-fairness slice 1 stamps (``docs/backlog/
job-scheduling-fairness.md`` D1): ``human`` for a logged-in web write,
``agent`` for an MCP put — a chat agent's put is agent, not human
(ruled 2026-10-23) — ``recurring`` for a job the dispatch worker mints
under a todo (``meta.dispatched_from_todo``) and ``background`` for a
system minter's standing, idem-keyed work. ``JobHandler.put`` stamps
the first two from the write's producer; this module is the door for
the other two so a new minter can't forget the stamp —
``tests/test_job_origin_contract.py`` refuses a direct
``insert_ref(kind='job')`` anywhere else in ``src/``.

The class is *derived*, never caller-supplied: ``dispatched_from_todo``
in the meta marks a recurring row, everything else through this door is
background. ``origin=`` exists for exactly one caller — a bounded retry
re-minting under its failed parent's class
(``job_types/graph_garden._on_failed``) — not for picking a lane.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref

#: The scheduling classes a job row's ``meta.origin`` may carry, in
#: claim-rank order (D2: human first, background last). The put handler
#: stamps the first two; this module derives the last two.
JOB_ORIGINS: tuple[str, ...] = ("human", "agent", "recurring", "background")

#: Terminal job statuses for the idem-key reuse check — a queued /
#: running / parked job with the key blocks a re-mint; a terminal one
#: doesn't. Same tuple ``handlers.job`` keeps for ``_lookup_idem``.
JOB_TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")


def idem_lock_key(idem: str) -> int:
    """Hash an idem string to a 64-bit signed integer for advisory lock.

    ``pg_advisory_xact_lock`` takes a ``bigint``. We hash via BLAKE2b
    truncated to 8 bytes and reinterpret as a signed int so the
    value fits Postgres's ``bigint`` range. Stable across processes
    so two workers racing the same idem key serialize correctly.
    The hash is not security-sensitive: collisions just cause two
    unrelated puts to share a lock briefly.
    """
    digest = hashlib.blake2b(idem.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def active_job_id(conn: Any, idem_key: str) -> int | None:
    """The newest live job carrying ``meta.idem_key`` in a non-terminal
    STATUS, else ``None``. Runs on the caller's ``conn`` — inside the
    ``pg_advisory_xact_lock`` window when serialized minting matters."""
    row = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'idem_key' = %s
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS'
                    AND t.value = ANY(%s)
               )
         ORDER BY r.ref_id DESC
         LIMIT 1
        """,
        (idem_key, list(JOB_TERMINAL_STATUSES)),
    ).fetchone()
    return int(row[0]) if row else None


def latest_job_for_idem(
    conn: Any, idem_key: str
) -> tuple[int, str | None, str | None, str | None] | None:
    """The newest job carrying ``meta.idem_key`` — any STATUS, terminal or
    live — as ``(ref_id, status, finished_at, error)``, else ``None``.

    Complements :func:`active_job_id` for a caller that also needs the
    terminal record: the retry gate that refuses a re-mint while the last
    attempt's failure is still fresh (``paper_ingest``'s DOI window,
    gr551840). ``status``/``finished_at``/``error`` are ``None``-safe
    mirrors of the row's tags and meta. ``finished_at`` falls back to the
    row's ``updated_at``/``created_at`` stamp (naive UTC text — the
    caller's parser treats naive as UTC): every terminal write through
    ``add_tag`` stamps ``meta.finished_at``, but a STATUS written by
    another path or a hand-edit can leave it absent, and a terminal job
    with no readable stamp still ends the window on its row timestamp."""
    row = conn.execute(
        """
        SELECT r.ref_id,
               (SELECT t.value
                  FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = r.ref_id
                   AND t.namespace = 'STATUS'
                 LIMIT 1),
               COALESCE(r.meta->>'finished_at',
                        (r.updated_at AT TIME ZONE 'UTC')::text,
                        (r.created_at AT TIME ZONE 'UTC')::text),
               r.meta->>'error'
          FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'idem_key' = %s
         ORDER BY r.ref_id DESC
         LIMIT 1
        """,
        (idem_key,),
    ).fetchone()
    if row is None:
        return None
    return (
        int(row[0]),
        str(row[1]) if row[1] is not None else None,
        str(row[2]) if row[2] is not None else None,
        str(row[3]) if row[3] is not None else None,
    )


def meta_marked_mints_24h(
    conn: Any, *, job_type: str, meta_key: str, meta_value: str
) -> int:
    """``kind='job'`` rows minted in the rolling 24 h carrying
    ``meta.<meta_key> = <meta_value>`` — the count a mint-side daily cap
    checks (``paper_ingest``'s get-origin cap on ``discovered_via='get'``,
    gr551840 review; the ``struct_neb_quest_mints_24h`` shape). ``conn``
    runs the count inside the caller's mint-lock transaction so the check
    and the insert serialize."""
    row = conn.execute(
        "SELECT count(*) FROM refs "
        "WHERE kind = 'job' AND retired_at IS NULL "
        "AND meta->>'job_type' = %s AND meta ->> %s = %s "
        "AND created_at > now() - interval '24 hours'",
        (job_type, meta_key, meta_value),
    ).fetchone()
    return int(row[0]) if row else 0


def mint_job_once(
    store: Store,
    *,
    conn: Any,
    job_type: str,
    executor: str,
    title: str,
    params: dict[str, Any] | None = None,
    parent_id: int | None = None,
    idem_key: str,
    prio: int | None = None,
    status: str = "queued",
    origin: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Mint one job under ``idem_key``, or reuse the active one —
    ``(job_ref_id, reused)``.

    Serializes on a transaction-scoped advisory lock keyed on the idem
    string, then re-checks for an active job before minting — the same
    race-safe shape ``JobHandler.put`` uses, so two concurrent mints
    with the same key can never both insert. Callers wrap this in
    ``store.tx()``.
    """
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (idem_lock_key(idem_key),))
    existing = active_job_id(conn, idem_key)
    if existing is not None:
        return existing, True
    ref = mint_background_job(
        store,
        conn=conn,
        job_type=job_type,
        executor=executor,
        title=title,
        params=params,
        parent_id=parent_id,
        idem_key=idem_key,
        prio=prio,
        status=status,
        origin=origin,
        meta_extra=meta_extra,
    )
    return int(ref.id), False


def mint_background_job(
    store: Store,
    *,
    conn: Any,
    job_type: str,
    executor: str,
    title: str,
    params: dict[str, Any] | None = None,
    idem_key: str | None = None,
    prio: int | None = None,
    parent_id: int | None = None,
    status: str = "queued",
    origin: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> Ref:
    """Insert one ``kind='job'`` row with ``meta.origin`` and a STATUS tag.

    Wraps ``store.insert_ref`` + ``store.add_tag(STATUS:<status>)`` on
    the caller's ``conn`` so the mint joins the caller's transaction.
    ``origin`` defaults to ``recurring`` when ``meta_extra`` carries
    ``dispatched_from_todo`` (a todo-dispatched row), else
    ``background``; pass it only to carry a parent's class onto a
    retry. ``meta_extra`` merges site-specific keys (``requires``,
    ``wake_when``, ``retry_of``) into ``meta`` — an ``origin`` smuggled
    in there is overwritten by the derived one.
    """
    meta: dict[str, Any] = {
        "job_type": job_type,
        "executor": executor,
        "params": params or {},
        **(meta_extra or {}),
    }
    if idem_key is not None:
        meta["idem_key"] = idem_key
    if origin is None:
        origin = (
            "recurring"
            if meta.get("dispatched_from_todo") is not None
            else "background"
        )
    elif origin not in JOB_ORIGINS:
        raise ValueError(f"origin must be one of {JOB_ORIGINS}, got {origin!r}")
    meta["origin"] = origin
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=title,
        meta=meta,
        parent_id=parent_id,
        prio=prio,
        conn=conn,
    )
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", status),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    return ref


__all__ = [
    "JOB_ORIGINS",
    "JOB_TERMINAL_STATUSES",
    "active_job_id",
    "idem_lock_key",
    "latest_job_for_idem",
    "meta_marked_mints_24h",
    "mint_background_job",
    "mint_job_once",
]
