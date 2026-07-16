"""Quest gaps + health — the striving exposes its own exploration queue.

Slice 3 of the quest layer (docs/proposals/quest-layer.md). Where slice 2
*reweighted* existing work down the ``serves`` DAG, slice 3 *surfaces what is
missing or thin* — a quest's structure makes its own holes legible, and those
holes **are** the exploration queue. Two read-time, mechanical computations over
the nodes that ``serves`` a quest, no minting:

* **Gaps** — a striving with little support, a served ``concept`` stuck at low
  mastery, a quest with no literature grounding, an un-answered ``hypothesis``
  logbook entry. Each is a concrete, computable "here is where to look next".
* **Health** — *momentum* (are deeds + knowledge flowing in? recent logbook +
  recent server activity, open todos moving, no ``child-failed`` bubble) and an
  *alignment floor* (cosine proximity between the quest's card vector and each
  server's — the free mechanical "is this still in the quest's service?" score;
  the dream re-review that refines the ambiguous middle is a later rung).

Everything degrades to empty: a quest with no servers yields ``[]`` gaps and a
``quiet`` momentum; servers whose ``card_combined`` has not embedded yet are
simply skipped by the alignment floor. So the handler can call this on every
``view='tree'`` read safely — like reweighting, it is a **no-op until quests +
servers exist**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import sqrt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Ref, Store

# ── tunables ──────────────────────────────────────────────────────────

#: Fewer than this many direct servers (any kind, incl. sub-quests) → the
#: quest is "thin" — we care about it but almost nothing is in its service.
THIN_SUPPORT_MIN = 2

#: A ``concept`` server below this mastery is a gap: the quest needs it
#: understood and it is not yet (``refs.meta['mastery']`` ∈ [0, 1]).
LOW_MASTERY = 0.5

#: Trailing window for the momentum signals (recent logbook + server activity).
MOMENTUM_WINDOW_DAYS = 14

#: Cosine-similarity floor for the alignment check: a server whose card vector
#: is *less* similar than this to the quest's card is flagged as drifting. This
#: is ``1 - SEMANTIC_DISTANCE_FLOOR`` (0.65, the corpus semantic-relevance
#: cutoff in ``store._mappers``) — beyond the distance at which two cards are
#: deemed "about the same thing", so a mechanical first-pass "off-aim?" flag.
ALIGN_SIM_FLOOR = 0.35

#: Bound the alignment floor's per-read embedding fetches — a quest with a huge
#: server fan-out still reads cheaply; the rest render without an align flag.
_ALIGN_MAX_SERVERS = 40

_LOG_KIND = "quest_log"


# ── result types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gap:
    """One thin spot in a quest's support — a place to look next."""

    kind: str  # thin-support | no-literature | low-mastery | open-hypothesis
    detail: str  # human-readable one-liner
    handle: str | None = None  # the node it concerns, if any


@dataclass(frozen=True)
class Momentum:
    """Mechanical "is anything flowing in?" read (no completion axis)."""

    label: str  # active | warming | stalled | quiet
    recent_entries: int  # logbook entries in the trailing window
    recent_server_events: int  # ref_events on servers in the window
    open_todo_servers: int  # todo servers still open (work in flight)
    blocked_todo_servers: int  # todo servers carrying a child-failed bubble


@dataclass(frozen=True)
class AlignmentFlag:
    """A server whose card vector has drifted from the quest's."""

    handle: str
    title: str
    cosine: float


@dataclass(frozen=True)
class Health:
    momentum: Momentum
    alignment_flags: list[AlignmentFlag] = field(default_factory=list)
    alignment_checked: int = 0  # servers with an embedding actually compared


# ── server fetch (shared) ─────────────────────────────────────────────


def _live_servers(store: Store, quest_id: int) -> list[Ref]:
    """Live refs that ``serves`` this quest (one hop, inbound)."""
    links = store.links_for(quest_id, direction="in", relation="serves")
    ids = [ln.src_ref_id for ln in links]
    if not ids:
        return []
    refs = store.fetch_refs_by_ids(set(ids))
    out: list[Ref] = []
    seen: set[int] = set()
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        r = refs.get(i)
        if r is not None and r.deleted_at is None:
            out.append(r)
    return out


def _handle(store_kind: str, ref_id: int) -> str:
    from precis.utils import handle_registry

    return handle_registry.try_format(store_kind, ref_id) or f"{store_kind}:{ref_id}"


# ── gaps ──────────────────────────────────────────────────────────────


def quest_gaps(
    store: Store, quest_id: int, *, servers: list[Ref] | None = None
) -> list[Gap]:
    """The exploration queue for one quest — what is thin or unanswered.

    Pass ``servers`` (the already-fetched live ``serves`` refs) to avoid a
    redundant query when the caller has them (the tree render does).
    """
    live = _live_servers(store, quest_id) if servers is None else servers
    gaps: list[Gap] = []

    # 1. Thin support — the striving with (almost) nothing in its service.
    if len(live) < THIN_SUPPORT_MIN:
        n = len(live)
        gaps.append(
            Gap(
                kind="thin-support",
                detail=(
                    f"only {n} server{'' if n == 1 else 's'} in this quest's "
                    "service — link projects / concepts / papers that serve it"
                ),
            )
        )

    # 2. No literature — work under way with no paper grounding at all. Only
    #    meaningful once *something* serves it (else thin-support covers it).
    if live and not any(r.kind == "paper" for r in live):
        gaps.append(
            Gap(
                kind="no-literature",
                detail="no paper serves this quest — nothing grounds it in the literature",
            )
        )

    # 3. Low-mastery served concepts — the quest needs these understood.
    for r in live:
        if r.kind != "concept":
            continue
        mastery = float((r.meta or {}).get("mastery", 0.0) or 0.0)
        if mastery < LOW_MASTERY:
            title = (r.title or "").splitlines()[0] if r.title else ""
            gaps.append(
                Gap(
                    kind="low-mastery",
                    detail=f"served concept at mastery {mastery:.2f} — {title[:60]}",
                    handle=_handle("concept", r.id),
                )
            )

    # 4. Un-answered hypotheses — a hypothesis logbook entry with no later
    #    result / dead-end. An open question the loop never closed.
    for text in _open_hypotheses(store, quest_id):
        gaps.append(
            Gap(
                kind="open-hypothesis",
                detail=f"un-answered hypothesis — {text.splitlines()[0][:70]}",
            )
        )

    # 5. Graduated candidates — a strong in-silico result that has crossed the
    #    quest's ceiling and now needs a real-world experiment (slice 4e). The
    #    loop can't close this; it's a call to a human / lab.
    for r in live:
        if r.kind != "structure":
            continue
        if any(str(t) == "needs-experiment" for t in store.tags_for(r.id)):
            title = (r.title or "").splitlines()[0] if r.title else ""
            gaps.append(
                Gap(
                    kind="needs-experiment",
                    detail=(
                        "graduated candidate needs a real-world experiment — "
                        f"{title[:60]}"
                    ),
                    handle=_handle("structure", r.id),
                )
            )

    return gaps


def _open_hypotheses(store: Store, quest_id: int) -> list[str]:
    """Hypothesis logbook entries with no later result / dead-end entry."""
    blocks = [
        b for b in store.list_blocks_for_ref(quest_id) if b.chunk_kind == _LOG_KIND
    ]
    if not blocks:
        return []

    def etype(b: object) -> str:
        return str((getattr(b, "meta", None) or {}).get("entry_type", "note"))

    # Positions (append order) of the entries that *answer* a hypothesis.
    answered_after = [
        i for i, b in enumerate(blocks) if etype(b) in ("result", "dead-end")
    ]
    last_answer = max(answered_after) if answered_after else -1
    # A hypothesis is open when no answer follows it. (Conservative: any
    # result/dead-end after it counts as closing it — cheap and good enough for
    # a surfacing signal, not a proof.)
    return [
        b.text
        for i, b in enumerate(blocks)
        if etype(b) == "hypothesis" and i > last_answer
    ]


# ── momentum ──────────────────────────────────────────────────────────


def quest_momentum(
    store: Store, quest_id: int, *, servers: list[Ref] | None = None
) -> Momentum:
    """Are deeds + knowledge flowing in? A mechanical read, no % done."""
    live = _live_servers(store, quest_id) if servers is None else servers
    since = datetime.now(UTC) - timedelta(days=MOMENTUM_WINDOW_DAYS)

    # Recent logbook entries on the quest itself.
    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    recent_entries = sum(
        1
        for b in store.list_blocks_for_ref(quest_id)
        if b.chunk_kind == _LOG_KIND
        and b.created_at is not None
        and _aware(b.created_at) >= since
    )

    # Recent activity on the servers (ref_events in the window).
    server_ids = [r.id for r in live]
    recent_server_events = 0
    if server_ids:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM ref_events "
                f"WHERE ref_id = ANY(%s) AND ts > now() - interval '{MOMENTUM_WINDOW_DAYS} days'",
                (server_ids,),
            ).fetchone()
        recent_server_events = int(row[0]) if row else 0

    # Todo servers: how many still open, how many carry a child-failed bubble.
    todo_ids = [r.id for r in live if r.kind == "todo"]
    open_todo = blocked_todo = 0
    if todo_ids:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT rt.ref_id, t.namespace, t.value FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = ANY(%s)",
                (todo_ids,),
            ).fetchall()
        done: set[int] = set()
        blocked: set[int] = set()
        for rid, ns, val in rows:
            if ns == "STATUS" and val == "done":
                done.add(int(rid))
            if ns == "OPEN" and str(val).startswith("child-failed:"):
                blocked.add(int(rid))
        open_todo = sum(1 for i in todo_ids if i not in done)
        blocked_todo = len(blocked)

    active_signal = recent_entries + recent_server_events
    if not live and recent_entries == 0:
        label = "quiet"
    elif active_signal == 0:
        label = "stalled"
    elif active_signal < 3:
        label = "warming"
    else:
        label = "active"

    return Momentum(
        label=label,
        recent_entries=recent_entries,
        recent_server_events=recent_server_events,
        open_todo_servers=open_todo,
        blocked_todo_servers=blocked_todo,
    )


# ── alignment floor ───────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0 if either is empty)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sqrt(sum(x * x for x in a))
    nb = sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def quest_alignment(
    store: Store, quest_id: int, *, servers: list[Ref] | None = None
) -> tuple[list[AlignmentFlag], int]:
    """Mechanical cosine floor: which servers have drifted off the quest's aim.

    Compares the quest's ``card_combined`` embedding to each server's. Servers
    whose card has not embedded yet (or the quest's) are skipped — this is a
    best-effort floor, not a gate. Returns ``(flags, n_checked)``.
    """
    live = _live_servers(store, quest_id) if servers is None else servers
    if not live:
        return [], 0
    qblock = store.get_block(quest_id, pos=-1, with_embedding=True)
    qvec = getattr(qblock, "embedding", None) if qblock is not None else None
    if not qvec:
        return [], 0

    flags: list[AlignmentFlag] = []
    checked = 0
    for r in live[:_ALIGN_MAX_SERVERS]:
        sb = store.get_block(r.id, pos=-1, with_embedding=True)
        svec = getattr(sb, "embedding", None) if sb is not None else None
        if not svec:
            continue
        checked += 1
        sim = _cosine(qvec, svec)
        if sim < ALIGN_SIM_FLOOR:
            title = (r.title or "").splitlines()[0] if r.title else ""
            flags.append(
                AlignmentFlag(
                    handle=_handle(r.kind, r.id), title=title[:60], cosine=sim
                )
            )
    flags.sort(key=lambda f: f.cosine)
    return flags, checked


# ── the combined health read ──────────────────────────────────────────


def quest_health(
    store: Store, quest_id: int, *, servers: list[Ref] | None = None
) -> Health:
    """Momentum + alignment floor, computed on read."""
    live = _live_servers(store, quest_id) if servers is None else servers
    momentum = quest_momentum(store, quest_id, servers=live)
    flags, checked = quest_alignment(store, quest_id, servers=live)
    return Health(momentum=momentum, alignment_flags=flags, alignment_checked=checked)


__all__ = [
    "ALIGN_SIM_FLOOR",
    "LOW_MASTERY",
    "MOMENTUM_WINDOW_DAYS",
    "THIN_SUPPORT_MIN",
    "AlignmentFlag",
    "Gap",
    "Health",
    "Momentum",
    "quest_alignment",
    "quest_gaps",
    "quest_health",
    "quest_momentum",
]
