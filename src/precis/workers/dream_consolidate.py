"""The daily dream-consolidation sweep — retire stale dreams behind one
``supersedes`` summary per interest cluster.

Fired from the ``dream_consolidate`` scheduler cadence (host-agnostic,
daily). Measured need (2026-10): 11,462 live dream memories, of which only
396 were recalled in the last 30 days — the corpus of speculative hunches
grew far past its recall value. The sweep retires what demonstrably went
unread while keeping a findable stub of what it removed.

Candidate rule, applied per live ``DREAM:``-tagged ``memory`` ref:

* **unrecalled** — ``last_recalled_at`` is NULL or older than the horizon
  (nobody read it), AND
* **untouched** — ``updated_at`` and ``last_viewed_at`` are NULL or older
  than the horizon (nobody edited or browsed it).

Horizons: :data:`DREAM_RETIRE_DAYS` (60) for ordinary dreams,
:data:`DREAM_THREAD_RETIRE_DAYS` (90) for thread-tagged ones — a
thread is a standing pointer, slower to go stale.

Consolidation: candidates are grouped into *interest clusters* by the
embedding of their ``memory_body`` chunk (greedy single-linkage at
:data:`CLUSTER_MAX_DISTANCE` cosine; dreams without an embedding share one
"unclustered" bucket so an embedder outage never strands them). Each
cluster gets ONE summary memory — the cluster's member titles rendered
mechanically (no LLM; the full bodies stay recoverable on the retired
nodes) — tagged ``DREAM:consolidated`` + ``SPACE:research``, with a
``supersedes`` edge and ``meta.superseded_by`` onto every member, links
migrated onto the summary, and the members soft-retired. Soft-delete only
(``Store.retire_ref``), recoverable at the SQL layer — same posture as
``memory_thread_sweep``.

The sweep is **dark by default** — the cadence fires everywhere, but the
pass does nothing unless a ``service_config`` row lights
``dream_consolidate`` (``prio > 0``) on some host
(:func:`dream_consolidate_enabled`, the ``gripe_triage`` gate shape).
Once lit it still only *reports* until a second switch lands: the
``dream_consolidate.dry_run`` setting (``precis settings``, default on)
keeps the pass in dry-run so the first lit fires show the retire plan in
``worker_logs`` before any write is armed — two deliberate operator
steps before 9k+ memories can retire.

**Not to be lit (Reto 2026-10-24).** Dreams cost nothing to keep; their
purpose is to build and improve useful linkages in the knowledge graph,
which discovery work depends on. Wholesale retirement by recall age works
against that. Any future dream work is slow review, refactor and compaction
(merging near-duplicates, deleting clearly useless ones), not this sweep.
Do not add the ``service_config`` row or disarm ``dry_run`` without a new
ruling from Reto.

``dry_run=True`` plans only; each candidate retire runs in its own
transaction so one poisoned row can't strand the rest, and the pass is
idempotent: retired members leave the candidate query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis import settings as psettings
from precis.store.types import ChunkInsert, Tag
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The ``service_config`` service name an operator lights
#: (``prio > 0``) to arm the sweep.
SERVICE_NAME = "dream_consolidate"
#: The ``precis.settings`` key keeping a lit pass report-only until an
#: operator explicitly disarms it. Default on: lighting the service can
#: never write on its own.
DRY_RUN_KEY = "dream_consolidate.dry_run"

#: Days without recall or touch before an ordinary dream retires.
DREAM_RETIRE_DAYS = 60
#: Same, for thread-tagged dream memories (standing pointers).
DREAM_THREAD_RETIRE_DAYS = 90
#: Open-tag literals marking a dream thread memory: ``dream-thread``
#: (current — gr504795 moved it off the gripe ``thread:<slug>`` axis)
#: plus the legacy bare ``thread:`` pre-rename memories still carry.
THREAD_TAGS = ("dream-thread", "thread:")
#: Cosine distance for the single-linkage interest clustering.
CLUSTER_MAX_DISTANCE = 0.35
#: Cap on members folded into one summary (bounds a mega-cluster's
#: edge fan-out; leftovers stay for the next fire).
MAX_CLUSTER_MEMBERS = 25
#: Cap on candidates per fire — the first run faces ~10k stale dreams;
#: oldest-first means the pass converges over days rather than running
#: one giant sweep inside the cadence deadline.
MAX_CANDIDATES_PER_FIRE = 2000

_THREADED_CANDIDATES = """
SELECT r.ref_id, r.title, r.updated_at, r.last_viewed_at, r.last_recalled_at,
       EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN'
                  AND t.value = ANY(%(thread)s)) AS is_thread,
       (SELECT ce.vector::text FROM chunks c
          JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
           AND ce.embedder = %(emb)s AND ce.status = 'ok'
         WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'memory_body'
           AND c.retired_at IS NULL
         ORDER BY c.ord LIMIT 1) AS vec
  FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'DREAM')
 ORDER BY r.ref_id
 LIMIT %(lim)s
"""


@dataclass(frozen=True)
class DreamCandidate:
    ref_id: int
    title: str
    is_thread: bool
    vec: list[float] | None
    horizon_days: int


@dataclass
class DreamCluster:
    members: list[DreamCandidate] = field(default_factory=list)


def _parse_vec(text: str | None) -> list[float] | None:
    if not text:
        return None
    s = text.strip().strip("[]")
    try:
        return [float(x) for x in s.split(",") if x.strip()]
    except ValueError:
        return None


def _default_embedder(store) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
    return str(row[0]) if row else None


def _cosine_dist(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na**0.5 * nb**0.5)


def dream_consolidate_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``dream_consolidate``
    anywhere. Never raises: a missing table or a connection blip reads as
    dark — an unreadable gate must stop a retiring pass, not arm it."""

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
        log.warning("dream_consolidate: service_config lookup failed", exc_info=True)
        return False


def _resolve_dry_run(store: Store, dry_run: bool | None) -> bool:
    """``dry_run=`` wins when given; else the ``dream_consolidate.dry_run``
    setting (default on — an unset or unparsable value stays report-only)."""
    if dry_run is not None:
        return dry_run
    return psettings.get_bool(DRY_RUN_KEY, store=store) is not False


def plan_dream_consolidate(store, *, now: datetime | None = None) -> list[DreamCluster]:
    """The clusters due for consolidation this fire. Pure read."""
    emb = _default_embedder(store)
    if emb is None:
        log.warning("dream_consolidate: no embedder registered; no-op")
        return []
    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(
            _THREADED_CANDIDATES,
            {"thread": list(THREAD_TAGS), "emb": emb, "lim": MAX_CANDIDATES_PER_FIRE},
        ).fetchall()
    stale: list[DreamCandidate] = []
    for ref_id, title, updated, viewed, recalled, is_thread, vec in rows:
        horizon = DREAM_THREAD_RETIRE_DAYS if is_thread else DREAM_RETIRE_DAYS
        cutoff = now - timedelta(days=horizon)
        signals = [t for t in (updated, viewed, recalled) if t is not None]
        if signals and max(signals) >= cutoff:
            continue  # recently recalled/viewed/edited — leave it
        stale.append(
            DreamCandidate(
                ref_id=int(ref_id),
                title=str(title or ""),
                is_thread=bool(is_thread),
                vec=_parse_vec(vec),
                horizon_days=horizon,
            )
        )
    embedded = [c for c in stale if c.vec is not None]
    unembedded = [c for c in stale if c.vec is None]
    clusters: list[DreamCluster] = []
    for cand in embedded:
        placed = False
        for cl in clusters:
            if any(
                m.vec is not None
                and _cosine_dist(cand.vec or [], m.vec) <= CLUSTER_MAX_DISTANCE
                for m in cl.members
            ):
                cl.members.append(cand)
                placed = True
                break
        if not placed:
            clusters.append(DreamCluster(members=[cand]))
    if unembedded:
        clusters.append(DreamCluster(members=unembedded))
    return [c for c in clusters if c.members]


def _consolidate_cluster(store, cluster: DreamCluster) -> int:
    """Mint the cluster summary and retire its members. Returns summary id."""
    members = cluster.members[:MAX_CLUSTER_MEMBERS]
    lines = [
        f"Dream consolidation — {len(members)} dream memories retired under "
        "this summary (full bodies remain on the retired nodes; each "
        "``supersedes`` edge names one).",
        "",
    ]
    for m in members:
        kind = "thread " if m.is_thread else ""
        lines.append(f"- me{m.ref_id} ({kind}{m.horizon_days}d horizon): {m.title}")
    body = "\n".join(lines)
    title = (members[0].title or "dream consolidation")[:72]
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory",
            slug=None,
            title=title,
            meta={
                "dream_consolidated": [m.ref_id for m in members],
                "auto_refresh_days": 90,
            },
            auto_refresh_days=90,
            conn=conn,
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
            conn=conn,
        )
        for tag in (
            Tag.parse_strict("DREAM:consolidated", kind="memory"),
            Tag.parse_strict("SPACE:research", kind="memory"),
        ):
            store.add_tag(ref.id, tag, set_by="system", replace_prefix=True, conn=conn)
        for m in members:
            store.migrate_links(m.ref_id, ref.id, conn=conn)
            store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=m.ref_id,
                relation="supersedes",
                set_by="system",
                conn=conn,
            )
            store.stamp_ref_meta(m.ref_id, {"superseded_by": ref.id}, conn=conn)
            store.retire_ref(m.ref_id, conn=conn)
        store.chunks.sync_header_card(ref.id, conn=conn)
    return int(ref.id)


def run_dream_consolidate_pass(
    store, *, dry_run: bool | None = None, now: datetime | None = None
) -> BatchResult:
    """One consolidation tick.

    Dark unless :func:`dream_consolidate_enabled` — the ``service_config``
    row is the on/off flag, off by default. When lit, ``dry_run=``
    overrides the ``dream_consolidate.dry_run`` setting, which defaults on:
    a lit-but-not-disarmed pass plans and reports, writing nothing.

    ``claimed`` = clusters due this fire; ``ok`` = members actually retired
    (0 in dry-run); ``failed`` = clusters whose consolidation raised."""
    if not dream_consolidate_enabled(store):
        log.info(
            "dream_consolidate: dark — no live service_config row with prio > 0; no-op"
        )
        return BatchResult(handler="dream_consolidate", claimed=0, ok=0, failed=0)
    dry_run = _resolve_dry_run(store, dry_run)
    clusters = plan_dream_consolidate(store, now=now)
    retired = failed = 0
    for cl in clusters:
        n = min(len(cl.members), MAX_CLUSTER_MEMBERS)
        if dry_run:
            log.info(
                "dream_consolidate: would retire %d dreams under one summary "
                "(cluster of %d)",
                n,
                len(cl.members),
            )
            continue
        try:
            sid = _consolidate_cluster(store, cl)
            retired += n
            log.info(
                "dream_consolidate: retired %d dreams → summary me%d",
                n,
                sid,
            )
        except Exception:
            log.exception("dream_consolidate: cluster consolidation failed")
            failed += 1
    return BatchResult(
        handler="dream_consolidate",
        claimed=len(clusters),
        ok=retired,
        failed=failed,
    )


__all__ = [
    "CLUSTER_MAX_DISTANCE",
    "DREAM_RETIRE_DAYS",
    "DREAM_THREAD_RETIRE_DAYS",
    "DRY_RUN_KEY",
    "SERVICE_NAME",
    "THREAD_TAGS",
    "DreamCandidate",
    "DreamCluster",
    "dream_consolidate_enabled",
    "plan_dream_consolidate",
    "run_dream_consolidate_pass",
]
