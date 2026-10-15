"""The apply door — explicit, human-invoked, never called by a pass.

This module exists so an *accepted* proposal can be applied cleanly and
undone — the gardener itself never calls it. Only merges are supported:
repointing every link off the loser onto the winner, folding the loser's
name into the winner's aliases, retiring the loser with
``meta.superseded_by`` set. Split/relink/prune/tag proposals are for a
human to carry out by hand today; the apply surface is deliberately
narrower than the proposal space.

Reversibility is the contract. Every repointed edge is recorded —
original endpoints, chunks, relation, meta — on the loser's
``meta.garden_merge`` blob (survives retirement), and each repointed row
carries ``meta.garden_merge`` + ``meta.prior_ref_id`` so SQL alone can
tell merge-made edges from pre-existing ones. ``undo_merge`` replays
that record: delete the merge-made copies, restore the originals
(``ON CONFLICT DO NOTHING`` — an edge that re-materialised since is left
alone), un-retire the loser, drop the alias the merge added.

The measures guard mirrors ``store.migrate_links``: a loser carrying
live ``measures`` rows (subject, measurand, or a quantifies-anchored
primary link) refuses — re-pointing measures by merge is not built.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store.revision_context import trust_door

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: ``links.meta`` key stamps every merge-repointed edge + the loser's
#: reversal record lives under the same name on ``refs.meta``. Neither
#: key is in ``precis_link_covered_meta``, so the stamps add no
#: covered-revision noise.
META_MERGE = "garden_merge"
META_PRIOR_REF = "prior_ref_id"


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
        # Recorded BEFORE the mutation so the ledger sha matches the state
        # the judge actually reviewed. resolve_proposal echoes the
        # proposal's key so the read view folds the pair.
        if review_id is not None:
            from precis.graph.garden.proposals import resolve_proposal

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
            # just dedups in).
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


__all__ = ["META_MERGE", "META_PRIOR_REF", "MergeRecord", "apply_merge", "undo_merge"]
