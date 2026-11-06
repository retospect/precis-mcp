"""``gripe_dispatch`` — turn fixable gripes into ``gripe-fix-direct`` chains.

The dark factory's intake (``docs/backlog/dark-factory.md``, td564019 cut
3; Reto 2026-11-04: "the codex chain should run indep of claude"): until
now a gripe reached a build lane only when someone typed
``put(kind='todo', mode='plan', args={'template': 'gripe-fix-direct',
'gripe_id': N})``. This scheduled pass mints that chain itself, bounded
by what the build lanes can take, so an agent-filed code gripe becomes a
codex build with no human and no Claude step in front.

Dark unless ``service_config`` lights ``gripe_dispatch`` for the calling
host (:func:`gripe_dispatch_enabled`, the ``gripe_triage`` gate shape:
an exact-host row beats ``*``; prio 0 or no row reads dark). Lighting it
is an enablement and rides Reto's go, not a code change.

Each fire:

* **Supply.** Open chains = live plan roots ``related-to`` a gripe whose
  STATUS is not terminal. Capacity = the summed ``cap`` of the enabled
  lanes that fit the ``code`` task class (``plans/lanes.py``). Slots =
  capacity minus open chains, at most :data:`_MAX_PER_TICK` per fire, so
  one tick never floods the lanes and a stuck chain holds its slot.
* **Candidates** (:func:`candidates`): live gripes, STATUS ``open`` or
  ``triaged``, tagged ``fix:code``, older than :data:`_MIN_AGE_MINUTES`
  (the dedup and triage passes get their turn first), with no live todo
  ``related-to`` them (a chain, hand-minted or ours, already exists), none
  of the hold tags (``fix:reto``, ``reto:ask``, ``disagreement``,
  ``flood``, ``hold:no-dispatch``, ``hold:no-autoclose``), and **filed by a
  Claude Code client
  or a human account** — BUILDER's condition (2026-11-04): the gripe
  text rides the build body verbatim and lands on main with no triage in
  front, so a reader-intake or worker-filed gripe never reaches a lane by
  this door. ``meta.filed_by`` carries the stamp
  (:mod:`precis.filing_origin`); a gripe without one is skipped. Order:
  ``PRIO`` (lower is hotter) then oldest.
* **Mint** through :func:`precis.plans.templates.mint_gripe_fix_direct`
  (unpinned: the build picks its lane at dispatch, devin first by table
  order) and one ``dispatch:`` comment on the gripe naming the root. The
  ``related-to`` link the template writes is the idempotency marker: a
  re-fire sees the gripe linked and skips it, so a doubled tick converges.

The pass bills no model call and holds no transaction across the mint
(each mint is the template's own transaction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from precis.corpus_layout import host_name
from precis.plans import lanes as _lanes
from precis.plans.templates import mint_gripe_fix_direct
from precis.store import Store
from precis.workers.runner import BatchResult
from precis.workers.service_config import ALL_HOSTS, ServiceConfigResolver

log = logging.getLogger(__name__)

SERVICE_NAME = "gripe_dispatch"
#: Chains minted per fire at most, whatever the free slots.
_MAX_PER_TICK = 2
#: A gripe younger than this waits: the dedup pass and the triage tick
#: get their turn, and a filer can still retract a mistaken report.
_MIN_AGE_MINUTES = 60
#: Task class the chain's build carries (templates leave it unset → code).
_BUILD_TASK_CLASS = "code"
#: Tags that keep a gripe out of the lane by this door. ``hold:no-autoclose``
#: marks Reto's never-close gripes (me547530): a land would write
#: ``resolved:`` on one, so they never enter a chain by this door (BUILDER
#: 2026-11-05).
HOLD_TAGS: tuple[str, ...] = (
    "fix:reto",
    "reto:ask",
    "disagreement",
    "flood",
    "hold:no-dispatch",
    "hold:no-autoclose",
)
#: STATUS values a gripe may carry to be dispatched.
_DISPATCH_STATUSES: tuple[str, ...] = ("open", "triaged")
#: Plan-root STATUS values that no longer hold a lane slot.
_TERMINAL_STATUSES: tuple[str, ...] = ("done", "won't-do")
#: ``meta.filed_by.client`` prefix of a Claude Code session.
_AGENT_CLIENT_PREFIX = "claude-code/"
#: ``meta.filed_by.actor`` prefix of a human account
#: (:mod:`precis.acting_user`).
_HUMAN_ACTOR_PREFIX = "user:"


def gripe_dispatch_enabled(
    store: Store, *, conn: Any = None, host: str | None = None
) -> bool:
    """True iff ``service_config`` lights ``gripe_dispatch`` for ``host``
    (default: this host): exact-host row over ``*``, prio 0 or no row
    reads dark. Never raises: a blip reads as dark."""
    h = host or host_name()
    try:
        if conn is None:
            return ServiceConfigResolver(store, h).enabled(
                SERVICE_NAME, default_on=False
            )
        row = conn.execute(
            "SELECT prio FROM service_config "
            "WHERE service = %s AND host IN (%s, %s) "
            "ORDER BY (host = %s) DESC LIMIT 1",
            (SERVICE_NAME, h, ALL_HOSTS, h),
        ).fetchone()
        return bool(row) and int(row[0]) > 0
    except Exception:
        log.warning("gripe_dispatch: service_config lookup failed", exc_info=True)
        return False


@dataclass(frozen=True, slots=True)
class DispatchPick:
    gripe_id: int
    title: str
    prio: int | None


def lane_capacity(
    store: Store | None, lanes: tuple[_lanes.Lane, ...] | None = None
) -> int:
    """Summed cap of the enabled lanes that fit the build's task class."""
    table = _lanes.load_lanes(store) if lanes is None else lanes
    return sum(ln.cap for ln in table if ln.enabled and ln.fits(_BUILD_TASK_CLASS))


def open_chain_count(store: Store) -> int:
    """Live plan roots ``related-to`` a gripe whose STATUS is not terminal."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT r.ref_id)
              FROM refs r
              JOIN links l ON l.src_ref_id = r.ref_id AND l.relation = 'related-to'
              JOIN refs g ON g.ref_id = l.dst_ref_id AND g.kind = 'gripe'
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND EXISTS (SELECT 1 FROM ref_tags pt JOIN tags p ON p.tag_id = pt.tag_id
                            WHERE pt.ref_id = r.ref_id AND p.namespace = 'OPEN'
                              AND p.value = 'plan:' || r.ref_id)
               AND NOT EXISTS (SELECT 1 FROM ref_tags st JOIN tags s ON s.tag_id = st.tag_id
                                WHERE st.ref_id = r.ref_id AND s.namespace = 'STATUS'
                                  AND s.value = ANY(%s))
            """,
            (list(_TERMINAL_STATUSES),),
        ).fetchone()
    return int(row[0]) if row else 0


def candidates(store: Store, limit: int) -> list[DispatchPick]:
    """The next ``limit`` dispatchable gripes, hottest PRIO then oldest."""
    if limit <= 0:
        return []
    sql = """
        SELECT r.ref_id, r.title, r.prio
          FROM refs r
          JOIN ref_tags st ON st.ref_id = r.ref_id
          JOIN tags s ON s.tag_id = st.tag_id
                     AND s.namespace = 'STATUS' AND s.value = ANY(%s)
         WHERE r.kind = 'gripe' AND r.retired_at IS NULL
           AND r.created_at < now() - %s * interval '1 minute'
           AND (r.meta->'filed_by'->>'client' LIKE %s
                OR r.meta->'filed_by'->>'actor' LIKE %s)
           AND EXISTS (SELECT 1 FROM ref_tags ft JOIN tags f ON f.tag_id = ft.tag_id
                        WHERE ft.ref_id = r.ref_id AND f.namespace IN ('OPEN', 'FLAG')
                          AND f.value = 'fix:code')
           AND NOT EXISTS (SELECT 1 FROM ref_tags ht JOIN tags h ON h.tag_id = ht.tag_id
                            WHERE ht.ref_id = r.ref_id AND h.namespace IN ('OPEN', 'FLAG')
                              AND h.value = ANY(%s))
           AND NOT EXISTS (SELECT 1 FROM links l JOIN refs t ON t.ref_id = l.src_ref_id
                            WHERE l.dst_ref_id = r.ref_id AND l.relation = 'related-to'
                              AND t.kind = 'todo' AND t.retired_at IS NULL)
         ORDER BY r.prio NULLS LAST, r.created_at, r.ref_id
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql,
            (
                list(_DISPATCH_STATUSES),
                _MIN_AGE_MINUTES,
                _AGENT_CLIENT_PREFIX + "%",
                _HUMAN_ACTOR_PREFIX + "%",
                list(HOLD_TAGS),
                limit,
            ),
        ).fetchall()
    return [DispatchPick(int(r[0]), str(r[1] or ""), r[2]) for r in rows]


def run_gripe_dispatch_pass(store: Store, *, host: str | None = None) -> BatchResult:
    """One fire: free slots → candidates → one chain each. ``claimed`` is
    the number of gripes the tick tried, ``ok`` the chains minted."""
    if not gripe_dispatch_enabled(store, host=host):
        log.info("gripe_dispatch: dark — no live service_config row; no-op")
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    capacity = lane_capacity(store)
    open_chains = open_chain_count(store)
    slots = min(_MAX_PER_TICK, max(capacity - open_chains, 0))
    picks = candidates(store, slots)
    extra: dict[str, Any] = {
        "lane_capacity": capacity,
        "open_chains": open_chains,
        "slots": slots,
        "minted": [],
    }
    ok = failed = 0
    for c in picks:
        try:
            mint = mint_gripe_fix_direct(store, gripe_id=c.gripe_id)
        except Exception as exc:
            failed += 1
            log.warning("gripe_dispatch: mint for gr%d failed: %s", c.gripe_id, exc)
            continue
        ok += 1
        extra["minted"].append({"gripe": c.gripe_id, "root": mint.root_id})
        try:
            _comment(store, c.gripe_id, mint.root_id)
        except Exception:  # pragma: no cover — the chain exists either way
            log.warning(
                "gripe_dispatch: comment on gr%d failed", c.gripe_id, exc_info=True
            )
    log.info(
        "gripe_dispatch: capacity=%d open=%d slots=%d minted=%d failed=%d",
        capacity,
        open_chains,
        slots,
        ok,
        failed,
    )
    return BatchResult(
        handler=SERVICE_NAME, claimed=len(picks), ok=ok, failed=failed, extra=extra
    )


def _comment(store: Store, gripe_id: int, root_id: int) -> None:
    from precis.store.types import ChunkInsert

    text = (
        f"dispatch: minted gripe-fix-direct td{root_id} (build picks its lane at "
        "dispatch, devin first; the land writes `resolved: <sha>` here)"
    )
    with store.tx() as conn:
        ords = conn.execute(
            "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (gripe_id,),
        ).fetchone()
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=int(ords[0]) if ords else 0,
                    text=text,
                    meta={"chunk_kind": "gripe_comment"},
                )
            ],
            conn=conn,
        )


__all__ = [
    "HOLD_TAGS",
    "SERVICE_NAME",
    "DispatchPick",
    "candidates",
    "gripe_dispatch_enabled",
    "lane_capacity",
    "open_chain_count",
    "run_gripe_dispatch_pass",
]
