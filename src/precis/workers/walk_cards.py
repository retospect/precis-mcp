"""walk_cards — the /eye walk → asked-queue bridge (gr743232).

Contract me743214 (Reto's auto-queue ruling, 2026-11-20): concepts the
owner *met* while walking ``/eye`` should become cards without anyone
typing a "what is X?" — the walk is the ask. One pass, fired from the
daily ``walk_cards`` scheduler cadence (the lease is the fleet-singleton
throttle, same as ``health_digest``/``ots_sweep``):

1. **Walk** — every ``eye_visits`` row still inside the 90-day retention
   window, grouped by its (enabled) ``web_users`` owner.
2. **Focus** — each recorded handle resolves to its ref: a record handle
   is the ref itself, a chunk handle its owning ref, selectors
   (``pa5~3``, ``pa5#c12``) strip to the base first, ``se``/``structure``
   slug focus forms resolve through ``ref_identifiers`` exactly as
   ``routes/eye.py::_handle_target`` does, and dead handles (NotFound
   walks are recorded too) drop out. The ref set is then filtered to
   what the owner may actually see — a ref carrying a live
   ``user_secret:<other>`` tag is not a node they met (the same fence
   the read path applies, keyed on their abbrev).
3. **Candidates** — existing ``concept`` refs the owner focused directly,
   plus every concept one graph hop from a focused ref (any link
   direction, any relation — the fisheye the owner scrolled past *is*
   the meeting). Owner-bound visibility only — the concept must be
   shared or theirs — and it must carry ``meta.name``, both because the
   queue matches by name and because that pair of constraints makes the
   queue's own lookup a guaranteed self-match: the pass can never mint a
   new term (the contract's hard rule). Skipped: mastered concepts,
   concepts with a live card for this owner, concepts the forge already
   declined (``card_forge_skip``), and concepts with a live walk
   submission or an explicit walk-card drop for this owner within
   retention. The submission check makes re-reading the same 90-day
   log idempotent. The candidate also passes the same
   secret-tag fence as the focused refs — another login's secret
   concept can't ride a walk into this queue.
4. **Queue** — :func:`precis.reading.asked.queue_asked_term` with the
   owner's login and ``source="walk"``: the same ``twin:asked`` tag, the
   same ``meta.asked_submissions`` undo ledger (each entry carrying
   ``source="walk"`` and the ask's UTC ``at``) the what-is capture and
   the "Card this" chip write. Nothing else is stamped; Undo of a walk
   submission (``unqueue_asked_term``) works like any other door. If the
   queue ever reports ``created=True`` — which the candidate filter is
   built to make impossible — or resolves the name onto a different ref
   (a same-named node shadowing the met one), the pass withdraws that
   submission on the spot: the no-mint rule holds under future schema
   drift and a walk submission only lands on a met node.
5. **Mint hand-off** — nothing here authors a card. The ``twin:asked``
   tag sorts the concept to the front of the forge's cardless scan
   (asked-first ordering) and the existing daily mint cap still decides
   how many cards a day the forge writes; a walk-queued concept simply
   waits its turn like any ask. A card the forge mints for a concept
   driven by a live walk submission gets ``meta.source="walk"`` and
   ``meta.walk_date`` (the submission's UTC day) — see
   ``reading/flashcards.py``'s ``_LATEST_WALK`` lateral.

Deliberately **lit** — no ``service_config`` gate: the ruling asks for a
standing product behaviour, and the pass is bounded (one SELECT per
owner plus one queue write per new candidate), deterministic, spends no
LLM and touches no external service. ``batch_size`` is unused — the walk
log's own retention window is the bound.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from precis.eye_visits import RETENTION_DAYS
from precis.reading.asked import (
    DROPPED_WALK_SQL,
    LIVE_CARD_SQL,
    queue_asked_term,
    unqueue_asked_term,
)
from precis.utils import handle_registry

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: The ``meta.asked_submissions`` source a walk queue files — also the
#: ``meta.source`` the forge stamps on the cards it mints from them.
WALK_SOURCE = "walk"

#: ``<handle>~<sel>`` / ``<handle>#<sel>`` focus forms — strip to the base
#: handle, the same split ``routes/eye.py::_handle_target`` applies.
_SELECTOR_RE = re.compile(r"[~#]")

#: The owner-visible predicate ``secret_fence`` builds, inlined for a
#: worker that has no ambient viewer: a ref is visible to this login iff
#: it carries no live ``user_secret:`` tag, or carries the one naming the
#: login's abbrev (the handle ``viewing_as_authenticated_user`` binds).
#: ``{ref}`` is the ref_id expression; ``%(handle)s`` the bound abbrev —
#: parameterised, so no regex gate is needed.
_VISIBLE_SQL = """
    (NOT EXISTS (SELECT 1 FROM ref_tags srt
                 JOIN tags st ON st.tag_id = srt.tag_id
                 WHERE srt.ref_id = {ref} AND st.namespace = 'OPEN'
                   AND starts_with(st.value, 'user_secret:')
                   AND (srt.expires_at IS NULL OR srt.expires_at > now()))
     OR EXISTS (SELECT 1 FROM ref_tags srt
                JOIN tags st ON st.tag_id = srt.tag_id
                WHERE srt.ref_id = {ref} AND st.namespace = 'OPEN'
                  AND st.value = 'user_secret:' || %(handle)s
                  AND (srt.expires_at IS NULL OR srt.expires_at > now())))
"""

#: One owner's queueable concepts: focused directly (``ref_id`` among the
#: walked refs) or exactly one link away from any walked ref, still
#: unmastered, still without a live card for this login, never declined
#: by the forge, and not already walk-queued. The owner/shared and
#: ``meta.name IS NOT NULL`` conjuncts make ``queue_asked_term``'s own
#: ``_named_term`` lookup a guaranteed self-match — ``created`` can never
#: come back True (the contract's "never mints new terms").
_CANDIDATES_SQL = """
    SELECT DISTINCT c.ref_id, c.meta->>'name'
    FROM refs c
    WHERE c.kind = 'concept' AND c.retired_at IS NULL
      AND (c.owner_login IS NULL OR c.owner_login = %(owner)s)
      AND c.meta->>'name' IS NOT NULL
      AND COALESCE(c.meta->>'state', 'candidate') <> 'mastered'
      AND NOT (c.meta ? 'card_forge_skip')
      AND (c.ref_id = ANY(%(focused)s)
           OR EXISTS (
               SELECT 1 FROM links l
               WHERE (l.src_ref_id = ANY(%(focused)s) AND l.dst_ref_id = c.ref_id)
                  OR (l.dst_ref_id = ANY(%(focused)s) AND l.src_ref_id = c.ref_id)))
      AND NOT EXISTS ({live_card})
      AND NOT EXISTS ({dropped_walk})
      AND NOT EXISTS (
          SELECT 1 FROM jsonb_each(c.meta->'asked_submissions') s
          WHERE s.value->>'source' = %(walk_source)s
            AND s.value->>'owner' = %(owner)s
            AND (s.value->>'at')::timestamptz >= %(cutoff)s
            AND s.value->>'forge_skipped' IS NULL)
      AND {visible}
    ORDER BY c.ref_id
"""

#: ``se``/``structure`` slug bases of ``~``/``#`` focus forms resolve to
#: their refs through ``ref_identifiers`` — the same lookup
#: ``_handle_target`` runs via ``get_ref``. (``sk:`` handles and bare
#: queries never reach a walk row's ref set at all.)
_SLUG_REFS_SQL = """
    SELECT r.ref_id FROM ref_identifiers ri
    JOIN refs r ON r.ref_id = ri.ref_id
    WHERE ri.id_kind IN ('cite_key', 'cite_key_alias')
      AND ri.id_value = ANY(%(slugs)s)
      AND r.kind IN ('se', 'structure')
      AND r.retired_at IS NULL
"""


def _walk_owners(
    conn: Connection, now: datetime
) -> dict[int, tuple[str, str, list[str]]]:
    """Each enabled login's (login, abbrev, retained walk handles)."""
    rows = conn.execute(
        "SELECT v.user_id, w.login, w.abbrev, v.handle "
        "FROM eye_visits v JOIN web_users w ON w.id = v.user_id "
        "WHERE w.disabled_at IS NULL "
        "AND v.visited_at >= %s::timestamptz - make_interval(days => %s) "
        "ORDER BY v.visit_id",
        (now, RETENTION_DAYS),
    ).fetchall()
    owners: dict[int, tuple[str, str, list[str]]] = {}
    for user_id, login, abbrev, handle in rows:
        owners.setdefault(int(user_id), (str(login), str(abbrev), []))[2].append(
            str(handle)
        )
    return owners


def _focused_ref_ids(
    conn: Connection, handles: list[str], *, owner_handle: str
) -> set[int]:
    """The refs this owner focused, filtered to what they may see:
    record handles are ref ids directly, chunk handles map to their
    owning ref, selector forms resolve their ``se``/``structure`` slug
    base; unresolvable handles drop."""
    record_pks: set[int] = set()
    chunk_pks: set[int] = set()
    slug_bases: set[str] = set()
    for handle in handles:
        base = _SELECTOR_RE.split(handle, maxsplit=1)[0]
        parsed = handle_registry.parse(handle_registry.normalize(base))
        if parsed is None:
            # ``sk:`` resolves to a skill in the route, never a ref.
            if base != handle and not base.startswith("sk:"):
                slug_bases.add(base)
            continue
        _kind, is_chunk, pk = parsed
        (chunk_pks if is_chunk else record_pks).add(pk)
    ref_ids = set(record_pks)
    if chunk_pks:
        rows = conn.execute(
            "SELECT ref_id FROM chunks WHERE chunk_id = ANY(%s)",
            (list(chunk_pks),),
        ).fetchall()
        ref_ids.update(int(row[0]) for row in rows)
    if slug_bases:
        rows = conn.execute(_SLUG_REFS_SQL, {"slugs": sorted(slug_bases)}).fetchall()
        ref_ids.update(int(row[0]) for row in rows)
    if not ref_ids:
        return set()
    rows = conn.execute(
        "SELECT r.ref_id FROM refs r WHERE r.ref_id = ANY(%(ids)s) AND "
        + _VISIBLE_SQL.format(ref="r.ref_id"),
        {"ids": sorted(ref_ids), "handle": owner_handle},
    ).fetchall()
    return {int(row[0]) for row in rows}


def _candidates(
    conn: Connection,
    ref_ids: set[int],
    *,
    owner_login: str,
    owner_handle: str,
    cutoff: datetime,
) -> list[tuple[int, str]]:
    """Queueable concepts for one owner — see :data:`_CANDIDATES_SQL`."""
    live_card = LIVE_CARD_SQL.format(concept="c.ref_id", owner="%(owner)s::text")
    dropped_walk = DROPPED_WALK_SQL.format(
        concept="c.ref_id", owner="%(owner)s", cutoff="%(cutoff)s"
    )
    visible = _VISIBLE_SQL.format(ref="c.ref_id")
    rows = conn.execute(
        _CANDIDATES_SQL.format(
            live_card=live_card, dropped_walk=dropped_walk, visible=visible
        ),
        {
            "owner": owner_login,
            "handle": owner_handle,
            "focused": sorted(ref_ids),
            "walk_source": WALK_SOURCE,
            "cutoff": cutoff,
        },
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def run_walk_cards_pass(store: Store, *, now: datetime | None = None) -> dict[str, int]:
    """One daily pass over every owner's walk log (gr743232).

    Returns ``{claimed, ok, failed}`` plus pass detail (``owners``):
    ``claimed`` counts candidate concepts, ``ok`` the ones queued,
    ``failed`` per-candidate queue errors — a bad candidate never
    sinks the rest of the pass."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    result = {"claimed": 0, "ok": 0, "failed": 0, "owners": 0}
    with store.pool.connection() as conn:
        owners = _walk_owners(conn, now)
        result["owners"] = len(owners)
        for login, abbrev, handles in owners.values():
            focused = _focused_ref_ids(conn, handles, owner_handle=abbrev)
            if not focused:
                continue
            candidates = _candidates(
                conn, focused, owner_login=login, owner_handle=abbrev, cutoff=cutoff
            )
            result["claimed"] += len(candidates)
            for concept_id, name in candidates:
                try:
                    out = queue_asked_term(
                        store, name, owner_login=login, source=WALK_SOURCE
                    )
                except Exception:
                    result["failed"] += 1
                    log.warning(
                        "walk_cards: queue_asked_term failed for cn%s (%s)",
                        concept_id,
                        login,
                        exc_info=True,
                    )
                    continue
                landed = int(out.get("ref_id") or 0)
                if out.get("created") or landed != concept_id:
                    # `created` is supposed to be unreachable under the
                    # candidate filter, and a name collision landing on
                    # another node is not a met concept — either way the
                    # no-mint / met-node rules win: withdraw the
                    # submission through the same undo door any queue
                    # mint honours.
                    log.warning(
                        "walk_cards: queue of cn%s landed on cn%s "
                        "(created=%s) — withdrawing the submission",
                        concept_id,
                        landed,
                        bool(out.get("created")),
                    )
                    try:
                        unqueue_asked_term(
                            store,
                            landed,
                            submission=str(out["submission"]),
                            owner_login=login,
                        )
                    except Exception:
                        log.warning(
                            "walk_cards: withdraw of misrouted submission "
                            "on cn%s failed (%s)",
                            landed,
                            login,
                            exc_info=True,
                        )
                    result["failed"] += 1
                    continue
                result["ok"] += 1
    log.info(
        "walk_cards: owners=%d candidates=%d queued=%d failed=%d",
        result["owners"],
        result["claimed"],
        result["ok"],
        result["failed"],
    )
    return result


__all__ = ["WALK_SOURCE", "run_walk_cards_pass"]
