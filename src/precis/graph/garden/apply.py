"""The apply door — explicit, human-invoked, never called by a pass.

This module exists so an *accepted* proposal can be applied cleanly and
undone — the gardener itself never calls it. Two shapes are supported:
merges repoint every link off the loser onto the winner, fold the
loser's name into the winner's aliases, and retire the loser with
``meta.superseded_by`` set; relinks (gr507641) write the structural edge
an approved proposal names — child → parent, ``specialises`` or another
of the gated structural relations. Split/prune/tag proposals are for a
human to carry out by hand today; the apply surface is deliberately
narrower than the proposal space.

Reversibility is the contract. Every repointed edge is recorded —
original endpoints, chunks, relation, meta — on the loser's
``meta.garden_merge`` blob (survives retirement), and each repointed row
carries ``meta.garden_merge`` + ``meta.prior_ref_id`` so SQL alone can
tell merge-made edges from pre-existing ones. ``undo_merge`` replays
that record: delete the merge-made copies, restore the originals
(``ON CONFLICT DO NOTHING`` — an edge that re-materialised since is left
alone), un-retire the loser, drop the alias the merge added. A relink
mints exactly one edge, so its record lives on the edge itself
(``links.meta.garden_relink``) and ``undo_relink`` is a plain delete.

A relink writes through ``store.add_link`` — the same admission every
edge takes. The machine-edge gate admits exactly this door, keyed on
the approved review row the apply writes first in the same transaction
(``store._links_ops._reviewed_structural_edge``); every other machine
structural write stays refused. Before resolving, the proposal is
re-verified like a merge: still current on the target, both endpoints
live, the ``payload.shas`` pins (written by the orphan detector for new
proposals) hold where present, and the edge does not already exist.

The measures guard mirrors ``store.migrate_links``: a loser carrying
live ``measures`` rows (subject, measurand, or a quantifies-anchored
primary link) refuses — re-pointing measures by merge is not built.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store._links_ops import (
    GARDEN_RELINK_META,
    MACHINE_EDGE_STRUCTURAL,
    relink_apply_door,
)
from precis.store.revision_context import trust_door

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Relation

log = logging.getLogger(__name__)

#: ``links.meta`` key stamps every merge-repointed edge + the loser's
#: reversal record lives under the same name on ``refs.meta``. Neither
#: key is in ``precis_link_covered_meta``, so the stamps add no
#: covered-revision noise.
META_MERGE = "garden_merge"
META_PRIOR_REF = "prior_ref_id"


class MergeRefused(BadInput):
    """An approved merge refused by the deterministic re-check inside
    the apply transaction. ``reason`` is the machine-readable code the
    caller records on the rejected proposal (``stale``, ``target_gone``,
    ``apply_refused``)."""

    def __init__(self, reason: str, why: str) -> None:
        super().__init__(f"{reason}: {why}")
        self.reason = reason


def _check_reviewed_merge(
    store: Store, conn: Any, review_id: int, loser_id: int, winner_id: int
) -> None:
    """Re-verify the proposal being applied, inside the apply
    transaction while the endpoint locks are held (the reviewer's
    finding: the verdict judged pinned state, so a merge may not write
    over drift). Refusal raises :class:`MergeRefused`; a ``review_id``
    naming no garden proposal returns — ``resolve_proposal`` owns the
    not-a-proposal ``NotFound``.

    Checks: the proposal row is still ``current`` on the target; both
    endpoints' ``payload.shas`` pins still hold (a proposal without
    them cannot be verified and refuses ``stale``); a ``dup_pair``
    merge still clears :func:`~.candidates.dup_pair_qualifies` live."""
    row = conn.execute(
        "SELECT note, "
        "  content_sha = precis_target_sha(target_kind, target_id) "
        "FROM reviews WHERE review_id = %s",
        (review_id,),
    ).fetchone()
    if row is None:
        return
    try:
        g = (json.loads(row[0] or "") or {}).get("garden") or {}
    except (TypeError, ValueError):
        g = {}
    if not g.get("key"):
        return  # not a garden proposal — resolve_proposal will refuse it
    if not row[1]:
        raise MergeRefused("stale", "the target changed since the proposal")
    payload = g.get("payload") or {}
    shas = payload.get("shas") or {}
    if str(loser_id) not in shas or str(winner_id) not in shas:
        raise MergeRefused(
            "stale", "the proposal carries no endpoint sha pins to verify"
        )
    live = conn.execute(
        "SELECT ref_id, precis_target_sha('ref', ref_id) "
        "FROM refs WHERE ref_id = ANY(%s)",
        ([loser_id, winner_id],),
    ).fetchall()
    now = {int(r[0]): str(r[1]) for r in live if r[1] is not None}
    if winner_id not in now:
        raise MergeRefused("target_gone", "the winner no longer exists")
    if shas[str(winner_id)] != now[winner_id]:
        raise MergeRefused("stale", "the winner changed since the proposal")
    if loser_id not in now or shas[str(loser_id)] != now[loser_id]:
        raise MergeRefused("stale", "the loser changed since the proposal")
    if str(g.get("detector") or "") == "dup_pair":
        from precis.graph.garden.candidates import dup_pair_qualifies

        fail = dup_pair_qualifies(store, loser_id, winner_id)
        if fail is not None:
            raise MergeRefused(
                "apply_refused",
                f"the pair no longer meets the dup floor: {fail}",
            )


@dataclass(slots=True)
class MergeRecord:
    """The reversal record stored on the loser at ``meta.garden_merge``."""

    merge_id: str
    loser: int
    winner: int
    at: str
    aliases_added: list[str] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)


@trust_door("generated")
def apply_merge(
    store: Store,
    loser_id: int,
    winner_id: int,
    *,
    actor: str,
    review_id: int | None = None,
    merge_id: str | None = None,
) -> MergeRecord:
    """Apply an accepted merge: loser folds into winner, links repoint.

    Caller-facing contract (the AC4 door): raises :class:`BadInput` /
    :class:`NotFound` on a missing, retired, cross-kind, already-merged or
    measures-carrying pair — the whole thing runs in one transaction so
    nothing is half-applied. ``actor`` lands in the ``approved`` review
    row written when ``review_id`` names the proposal being applied.
    """
    if loser_id == winner_id:
        raise BadInput("merge needs two distinct refs")
    if merge_id is None:
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        merge_id = f"garden-merge:{loser_id}:{winner_id}:{stamp}"
    with store.tx() as conn:
        loser = conn.execute(
            "SELECT ref_id, kind, title, meta, retired_at FROM refs "
            "WHERE ref_id = %s FOR UPDATE",
            (loser_id,),
        ).fetchone()
        winner = conn.execute(
            "SELECT ref_id, kind, title, meta, retired_at FROM refs "
            "WHERE ref_id = %s FOR UPDATE",
            (winner_id,),
        ).fetchone()
        if loser is None:
            raise NotFound(f"loser ref {loser_id} does not exist")
        if winner is None:
            raise NotFound(f"winner ref {winner_id} does not exist")
        loser_kind, loser_title, loser_meta = (
            str(loser[1]),
            str(loser[2]),
            loser[3] or {},
        )
        if loser[4] is not None:
            raise BadInput(f"loser ref {loser_id} is already retired")
        if winner[4] is not None:
            raise BadInput(f"winner ref {winner_id} is retired")
        if loser_kind != str(winner[1]):
            raise BadInput(
                f"merge needs same-kind refs (loser is {loser_kind}, "
                f"winner is {winner[1]})"
            )
        if loser_meta.get("superseded_by") is not None:
            raise BadInput(
                f"ref {loser_id} is already superseded by {loser_meta['superseded_by']}"
            )

        # measures guard — same refusal as store.migrate_links.
        n_measures = conn.execute(
            "SELECT count(*) FROM measures "
            "WHERE subject_ref_id = %(r)s OR measurand_ref_id = %(r)s "
            "   OR primary_link_id IN (SELECT link_id FROM links "
            "        WHERE src_ref_id = %(r)s OR dst_ref_id = %(r)s)",
            {"r": loser_id},
        ).fetchone()
        if n_measures and int(n_measures[0]):
            raise BadInput(
                f"ref {loser_id} carries measure row(s); re-pointing "
                "measures on merge is not built — resolve them first"
            )

        # Optional proposal-marking: approve the garden row being applied.
        # The deterministic re-check runs first, in this transaction, so
        # a verdict written over since-changed state cannot apply. Then
        # resolve BEFORE the mutation so the ledger sha matches the state
        # the judge actually reviewed. resolve_proposal echoes the
        # proposal's key so the read view folds the pair.
        if review_id is not None:
            from precis.graph.garden.proposals import resolve_proposal

            _check_reviewed_merge(store, conn, review_id, loser_id, winner_id)

            resolved = resolve_proposal(
                store,
                review_id,
                actor=actor,
                verdict="approved",
                extra={"applied": merge_id},
                conn=conn,
            )
            if resolved is None:
                raise NotFound(
                    f"no pending garden proposal review_id={review_id} "
                    "(already decided, or not a garden row)"
                )

        # Snapshot every link touching the loser, then repoint each onto
        # the winner (insert-substituted-copy; dedup via the unique key).
        # Exempt from the machine-edge write rule (graph-gardener-value
        # slice 1): repointing an existing edge onto the merge winner is
        # not a new machine edge — the rule lives in ``store.add_link``'s
        # admission, which this raw INSERT deliberately bypasses.
        link_rows = conn.execute(
            "SELECT link_id, src_ref_id, src_chunk_id, dst_ref_id, "
            "       dst_chunk_id, relation, set_by, meta FROM links "
            "WHERE src_ref_id = %(r)s OR dst_ref_id = %(r)s ORDER BY link_id",
            {"r": loser_id},
        ).fetchall()
        record = MergeRecord(
            merge_id=merge_id,
            loser=loser_id,
            winner=winner_id,
            at=f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
        )
        loser_name = str(loser_meta.get("name") or loser_title)
        if loser_name:
            record.aliases_added.append(loser_name)
        for (
            link_id,
            src_ref,
            src_chunk,
            dst_ref,
            dst_chunk,
            relation,
            set_by,
            lmeta,
        ) in link_rows:
            orig: dict[str, Any] = {
                "link_id": int(link_id),
                "src_ref_id": int(src_ref),
                "src_chunk_id": int(src_chunk) if src_chunk is not None else None,
                "dst_ref_id": int(dst_ref),
                "dst_chunk_id": int(dst_chunk) if dst_chunk is not None else None,
                "relation": str(relation),
                "set_by": str(set_by),
                "meta": lmeta or {},
            }
            new_src = winner_id if int(src_ref) == loser_id else int(src_ref)
            new_dst = winner_id if int(dst_ref) == loser_id else int(dst_ref)
            if new_src == new_dst and src_chunk == dst_chunk:
                orig["disposition"] = "dropped_selfloop"
                record.links.append(orig)
                continue
            new_meta = dict(lmeta or {})
            new_meta[META_MERGE] = merge_id
            new_meta[META_PRIOR_REF] = loser_id
            # A relink stamp belongs to the edge the door wrote, not to
            # its repointed copy — drop it or undo_relink would delete
            # the merged row too (gr507641).
            new_meta.pop(GARDEN_RELINK_META, None)
            cur = conn.execute(
                "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, "
                "  dst_chunk_id, relation, set_by, meta) "
                "VALUES (%(s)s, %(sc)s, %(d)s, %(dc)s, %(rel)s, %(by)s, %(m)s) "
                "ON CONFLICT (src_ref_id, src_chunk_id, dst_ref_id, "
                "  dst_chunk_id, relation) DO NOTHING RETURNING link_id",
                {
                    "s": new_src,
                    "sc": src_chunk,
                    "d": new_dst,
                    "dc": dst_chunk,
                    "rel": relation,
                    "by": set_by,
                    "m": Jsonb(new_meta),
                },
            )
            row = cur.fetchone()
            if row is None:
                orig["disposition"] = "deduped"
            else:
                orig["disposition"] = "repointed"
                orig["created_link_id"] = int(row[0])
            record.links.append(orig)

        if record.links:
            conn.execute(
                "DELETE FROM links WHERE src_ref_id = %(r)s OR dst_ref_id = %(r)s",
                {"r": loser_id},
            )

        # Fold the loser's name into the winner's aliases; stamp the loser
        # superseded + the reversal record; retire it.
        if record.aliases_added:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{aliases}', "
                "  COALESCE(meta->'aliases', '[]'::jsonb) || %s::jsonb) "
                "WHERE ref_id = %s",
                (json.dumps(record.aliases_added), winner_id),
            )
        loser_meta = dict(loser_meta)
        loser_meta["superseded_by"] = winner_id
        loser_meta[META_MERGE] = {
            "merge_id": merge_id,
            "winner": winner_id,
            "at": record.at,
            "aliases_added": record.aliases_added,
            "links": record.links,
        }
        conn.execute(
            "UPDATE refs SET meta = %s WHERE ref_id = %s",
            (Jsonb(loser_meta), loser_id),
        )
        store.retire_ref(loser_id, conn=conn)
    log.info(
        "garden: merged ref %d into %d (%s) — %d edge(s) repointed",
        loser_id,
        winner_id,
        merge_id,
        sum(1 for lk in record.links if lk["disposition"] == "repointed"),
    )
    return record


@trust_door("generated")
def undo_merge(store: Store, merge_id: str) -> dict[str, int]:
    """Reverse an applied merge. Returns counts ``{links_restored,
    links_deleted, aliases_removed}``.

    Looks the loser up by its ``meta.garden_merge.merge_id`` record — the
    merge's own row carries everything the replay needs.
    """
    with store.tx() as conn:
        row = conn.execute(
            "SELECT ref_id, meta FROM refs "
            "WHERE meta->'garden_merge'->>'merge_id' = %s",
            (merge_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no merge record for merge_id={merge_id!r}")
        loser_id = int(row[0])
        rec = (row[1] or {}).get(META_MERGE) or {}
        winner_id = int(rec["winner"])

        deleted = restored = 0
        for orig in rec.get("links") or []:
            created = orig.get("created_link_id")
            if orig.get("disposition") == "repointed" and created is not None:
                cur = conn.execute(
                    "DELETE FROM links WHERE link_id = %s", (int(created),)
                )
                deleted += cur.rowcount or 0
            # Restore the original edge (idempotent — a re-created twin
            # just dedups in). Same machine-edge exemption as apply: this
            # restores a pre-existing edge, it is not a new machine edge.
            conn.execute(
                "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, "
                "  dst_chunk_id, relation, set_by, meta) "
                "VALUES (%(s)s, %(sc)s, %(d)s, %(dc)s, %(rel)s, %(by)s, %(m)s) "
                "ON CONFLICT (src_ref_id, src_chunk_id, dst_ref_id, "
                "  dst_chunk_id, relation) DO NOTHING",
                {
                    "s": orig["src_ref_id"],
                    "sc": orig["src_chunk_id"],
                    "d": orig["dst_ref_id"],
                    "dc": orig["dst_chunk_id"],
                    "rel": orig["relation"],
                    "by": orig["set_by"],
                    "m": Jsonb(orig["meta"]),
                },
            )
            restored += 1

        # Strip the aliases the merge added from the winner.
        added = rec.get("aliases_added") or []
        if added:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{aliases}', "
                "  COALESCE((SELECT jsonb_agg(a) FROM jsonb_array_elements("
                "    meta->'aliases') a WHERE NOT (a #>> '{}') = ANY(%(rm)s)),"
                "    '[]'::jsonb)) "
                "WHERE ref_id = %(w)s",
                {"rm": list(added), "w": winner_id},
            )
        # Un-retire + clear the merge bookkeeping off the loser.
        conn.execute(
            "UPDATE refs SET meta = meta - %s - 'superseded_by' WHERE ref_id = %s",
            (META_MERGE, loser_id),
        )
        store.restore_ref(loser_id, conn=conn)
    log.info(
        "garden: undid merge %s (loser %d, winner %d): %d link(s) restored, %d deleted",
        merge_id,
        loser_id,
        winner_id,
        restored,
        deleted,
    )
    return {
        "links_restored": restored,
        "links_deleted": deleted,
        "aliases_removed": len(added),
    }


# ── relinks ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RelinkRecord:
    """What an applied relink wrote — the edge's own
    ``links.meta.garden_relink`` is the reversal record, so undo needs
    nothing beyond ``relink_id``."""

    relink_id: str
    proposal: int
    review: int  # the approved reviews row the edge is keyed on
    child: int
    parent: int
    relation: str
    at: str
    link_id: int | None = None


def _relink_edge(
    garden: dict[str, Any], target_kind: str, target_id: int, review_id: int
) -> tuple[int, int, str]:
    """The ``(child, parent, relation)`` a relink proposal's payload
    names — the child is the proposal's target ref (``payload.child``
    is an explicit restatement of it)."""
    if target_kind != "ref":
        raise BadInput(
            f"relink proposal {review_id} targets {target_kind}:{target_id} "
            "— a relink attaches ref → ref"
        )
    payload = garden.get("payload") or {}
    child = payload.get("child", target_id)
    if not isinstance(child, int) or child != target_id:
        raise BadInput(
            f"relink proposal {review_id} names child {child!r} — the "
            f"proposal's target is ref {target_id}"
        )
    parent = payload.get("parent")
    if not isinstance(parent, int):
        raise BadInput(
            f"relink proposal {review_id} names no parent ref — nothing "
            "to apply (the judge saw a candidate-parent list, not a pick)"
        )
    if child == parent:
        raise BadInput(f"relink proposal {review_id} is a self-loop on {child}")
    relation = str(payload.get("relation") or "specialises")
    if relation not in MACHINE_EDGE_STRUCTURAL:
        raise BadInput(
            f"relink proposal {review_id} names relation {relation!r} — "
            "this door writes structural edges only "
            f"({', '.join(sorted(MACHINE_EDGE_STRUCTURAL))})"
        )
    return child, parent, relation


@trust_door("generated")
def apply_relink(
    store: Store,
    review_id: int,
    *,
    actor: str,
    relink_id: str | None = None,
) -> RelinkRecord:
    """Apply a reviewed relink proposal: write the edge it names.

    The proposal must be a pending garden ``relink`` row; the approved
    review row this writes (``resolve_proposal``) lands in the same
    transaction before the edge, and the edge goes through
    ``store.add_link`` carrying ``meta.garden_relink.review`` — the key
    the machine-edge gate admits on. A bare machine write naming a
    missing, unapproved or mismatched review stays refused.

    Re-checks inside the transaction (``FOR UPDATE`` on both refs):
    the proposal is still current on the target, both endpoints are
    live, every ``payload.shas`` pin present still holds, and the edge
    does not already exist. Refusals raise :class:`MergeRefused` (the
    reason codes the consumer records: ``stale``, ``target_gone``,
    ``apply_refused``); a malformed proposal raises :class:`BadInput`,
    a missing/already-decided one :class:`NotFound`.
    """
    from precis.graph.garden.proposals import GARDEN_ACTOR, resolve_proposal

    with store.tx() as conn:
        row = conn.execute(
            "SELECT target_kind, target_id, note, "
            "  content_sha = precis_target_sha(target_kind, target_id) "
            "FROM reviews WHERE review_id = %s AND actor = %s "
            "  AND verdict = 'proposed'",
            (review_id, GARDEN_ACTOR),
        ).fetchone()
        if row is None:
            raise NotFound(f"no pending garden proposal review_id={review_id}")
        target_kind, target_id = str(row[0]), int(row[1])
        try:
            garden = (json.loads(row[2] or "") or {}).get("garden") or {}
        except (TypeError, ValueError):
            garden = {}
        if garden.get("action") != "relink":
            raise BadInput(
                f"review {review_id} is a {garden.get('action')!r} proposal "
                "— the relink door handles relinks"
            )
        child, parent, relation = _relink_edge(
            garden, target_kind, target_id, review_id
        )
        if not row[3]:
            raise MergeRefused("stale", "the target changed since the proposal")
        # Both endpoints, locked in id order (same discipline as the
        # merge door's FOR UPDATE pair): live, and matching whatever
        # sha pins the proposal carries. Proposals filed before endpoint
        # pinning hold only the target's content_sha — checked above.
        now = {
            int(r[0]): (bool(r[1]), None if r[2] is None else str(r[2]))
            for r in conn.execute(
                "SELECT ref_id, retired_at IS NULL, "
                "  precis_target_sha('ref', ref_id) "
                "FROM refs WHERE ref_id = ANY(%s) "
                "ORDER BY ref_id FOR UPDATE",
                ([child, parent],),
            ).fetchall()
        }
        for rid, label in ((child, "child"), (parent, "parent")):
            state = now.get(rid)
            if state is None:
                raise MergeRefused(
                    "target_gone", f"the {label} ref {rid} no longer exists"
                )
            if not state[0]:
                raise MergeRefused("target_gone", f"the {label} ref {rid} is retired")
        shas = (garden.get("payload") or {}).get("shas")
        if isinstance(shas, dict):
            for rid, label in ((child, "child"), (parent, "parent")):
                pin = shas.get(str(rid))
                if pin is not None and str(pin) != now[rid][1]:
                    raise MergeRefused(
                        "stale",
                        f"the {label} ref {rid} changed since the proposal",
                    )
        dup = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %s "
            "  AND src_chunk_id IS NULL AND dst_ref_id = %s "
            "  AND dst_chunk_id IS NULL AND relation = %s",
            (child, parent, relation),
        ).fetchone()
        if dup is not None:
            raise MergeRefused(
                "apply_refused",
                f"a {relation!r} edge {child}→{parent} already exists "
                f"(link {int(dup[0])})",
            )
        # The add-mode link doors do not run the acyclic constraint
        # (``check_relation_constraints`` lives at the verb doors), so
        # the door re-checks here: a ``child → parent`` edge closes a
        # cycle when ``parent`` already reaches ``child`` along the
        # relation.
        if parent in store.ancestors(relation, child, conn=conn):
            raise MergeRefused(
                "cycle",
                f"a {relation!r} edge {child}→{parent} would close a cycle",
            )
        # The orphan detector's bar, recomputed like the merge door's
        # dup floor: the child must still reach no start node, the
        # parent must still be one, and the pair must sit past
        # RELINK_MIN_DISTANCE (no card vector = nothing to measure).
        if str(garden.get("detector") or "") == "orphan":
            from precis.graph.garden.candidates import (
                RELINK_MIN_DISTANCE,
                _card_vectors,
                _cosine,
                _start_node_ids,
            )

            if child not in store.taxon_unrooted():
                raise MergeRefused(
                    "stale", f"the child {child} reaches a start node now"
                )
            if parent not in _start_node_ids(store):
                raise MergeRefused(
                    "stale", f"the parent {parent} is no longer a start node"
                )
            vecs = _card_vectors(store, [child, parent])
            if child in vecs and parent in vecs:
                dist = _cosine(
                    np.asarray(vecs[child], dtype=float),
                    np.asarray(vecs[parent], dtype=float),
                )
                if dist <= RELINK_MIN_DISTANCE:
                    raise MergeRefused(
                        "stale",
                        f"child {child} and parent {parent} now sit "
                        f"{dist:.3f} apart, inside the relink floor "
                        f"({RELINK_MIN_DISTANCE}) — retrieval already "
                        "surfaces it",
                    )
        if relink_id is None:
            stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
            relink_id = f"garden-relink:{child}:{parent}:{stamp}"
        # Resolve BEFORE the edge write so the ledger sha matches the
        # state the apply verified — and so the approved review row is
        # visible when the link admission gate runs in this transaction.
        resolved = resolve_proposal(
            store,
            review_id,
            actor=actor,
            verdict="approved",
            extra={
                "applied": relink_id,
                "link": {
                    "src_ref_id": child,
                    "dst_ref_id": parent,
                    "relation": relation,
                },
            },
            conn=conn,
        )
        if resolved is None:
            raise NotFound(
                f"no pending garden proposal review_id={review_id} "
                "(already decided, or not a garden row)"
            )
        record = RelinkRecord(
            relink_id=relink_id,
            proposal=int(review_id),
            review=int(resolved.review_id),
            child=child,
            parent=parent,
            relation=relation,
            at=f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}",
        )
        with relink_apply_door():
            link = store.add_link(
                src_ref_id=child,
                dst_ref_id=parent,
                relation=cast("Relation", relation),
                # links.set_by FKs to actors — the coarse slug vocabulary,
                # not the free-text reviews.actor that recorded the verdict.
                set_by="agent",
                meta={
                    GARDEN_RELINK_META: {
                        "relink_id": relink_id,
                        "proposal": int(review_id),
                        "review": record.review,
                        "at": record.at,
                    }
                },
                conn=conn,
            )
        record.link_id = int(link.id)
    log.info(
        "garden: relinked ref %d → %d (%s, %s)",
        child,
        parent,
        relation,
        relink_id,
    )
    return record


@trust_door("generated")
def undo_relink(store: Store, relink_id: str, *, actor: str) -> dict[str, int]:
    """Reverse an applied relink — delete the edge it wrote. Returns
    ``{links_deleted}``.

    Looks the edge up by its ``meta.garden_relink.relink_id`` record: a
    relink mints one new edge, so undo is a plain delete (unlike merge
    there is nothing to restore). The delete is made sticky: a
    ``rejected`` garden row carrying ``reason='undone'`` and the
    ``relink_id`` is written on the proposal's target+key, so the
    detector does not re-propose the edge and the gate refuses a
    replayed stamp.
    """
    from precis.graph.garden.proposals import GARDEN_VERSION

    with store.tx() as conn:
        rows = conn.execute(
            "SELECT link_id, meta->'garden_relink' FROM links "
            "WHERE meta->'garden_relink'->>'relink_id' = %s",
            (relink_id,),
        ).fetchall()
        if not rows:
            raise NotFound(f"no relink record for relink_id={relink_id!r}")
        proposal_id = (rows[0][1] or {}).get("proposal")
        pid = proposal_id if isinstance(proposal_id, int) else 0
        prop = (
            conn.execute(
                "SELECT target_kind, target_id, note FROM reviews WHERE review_id = %s",
                (pid,),
            ).fetchone()
            if pid
            else None
        )
        deleted = store.delete_links([int(r[0]) for r in rows], conn=conn)
        if prop is not None:
            try:
                g = (json.loads(prop[2] or "") or {}).get("garden") or {}
            except (TypeError, ValueError):
                g = {}
            if g.get("key"):
                store.record_target_review(
                    str(prop[0]),
                    int(prop[1]),
                    actor=actor,
                    verdict="rejected",
                    version=GARDEN_VERSION,
                    note=json.dumps(
                        {
                            "garden": {
                                "v": 1,
                                "key": g["key"],
                                "resolves": pid,
                                "undone": relink_id,
                                "reason": "undone",
                            }
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    conn=conn,
                )
    log.info("garden: undid relink %s: %d link(s) deleted", relink_id, deleted)
    return {"links_deleted": deleted}


__all__ = [
    "META_MERGE",
    "META_PRIOR_REF",
    "MergeRecord",
    "MergeRefused",
    "RelinkRecord",
    "apply_merge",
    "apply_relink",
    "undo_merge",
    "undo_relink",
]
