"""Reviewed apply doors — called explicitly or by the proposal consumer.

This module exists so an *accepted* proposal can be applied cleanly and
undone — detectors and proposers never call it. Structural actions:
merges repoint every link off the loser onto the winner, fold the
loser's name into the winner's aliases, and retire the loser with
``meta.superseded_by`` set; relinks (gr507641) write the structural edge
an approved proposal names — child → parent, ``specialises`` or another
of the gated structural relations. Split/prune/tag proposals are for a
human to carry out by hand today; the apply surface is deliberately
narrower than the proposal space.

Memory rewrites, supersedes and retirements re-run their detector under
the target lock. The consumer enables these only with
``PRECIS_GARDEN_MEMORY_APPLY`` (default off); human-origin and currently
considered targets are held. Rewrites use the pinned ``base_sha`` and
anchored memory edit door in one ``Store.atomic`` transaction with the
approval, reversal record and ``kg-review`` trust act. The record includes
body bytes because the old chunk is deleted, and outgoing link rows because
mention sync can otherwise erase provenance/route edges. Undo restores
that snapshot and refuses to overwrite intervening work. Supersede and
retire both use the existing memory delete door; supersede also leaves
the tombstone redirect and a winner-to-loser ``supersedes`` edge.
Both snapshot incident links, successor twins and endpoint pins under
``meta.garden_retirement`` before deleting. Their undo restores the node
and edges atomically, refusing changed content at rewritten link ends and
changed liveness elsewhere. Chunk pins omit read statistics and derived data.
Exact snapshots preserve deduplicated twins and chunk anchors during reversal;
strict trust restores legacy origin/confidence with an audited v1 import stamp.

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
primary link) refuses — the merge itself never moves them. The sanctioned
move is the canonical_unit-safe re-point
(``store.repoint_measurand_measures``, migration 0234's
``precis.allow_measurand_repoint`` door): run it first, then this.
Memory rewrite and undo likewise refuse links anchoring measures: deleting
and reinserting an edge would otherwise clear its measure FK permanently.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound
from precis.store._links_ops import (
    GARDEN_RELINK_META,
    MACHINE_EDGE_STRUCTURAL,
    _reground_link_chunk,
    relink_apply_door,
)
from precis.store.revision_context import (
    TrustAct,
    apply_revision_context,
    revision_context,
    trust_door,
)

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
    ``apply_refused``, or a paper-gate reason); ``detail`` carries the
    gate's finding when it refused."""

    def __init__(
        self,
        reason: str,
        why: str,
        detail: dict[str, Any] | None = None,
        *,
        next: str | None = None,
    ) -> None:
        super().__init__(f"{reason}: {why}", next=next)
        self.reason = reason
        self.why = why
        self.detail = detail


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
    model: str | None = None,
    identity_gate: bool = False,
    verdict_extra: dict[str, Any] | None = None,
) -> MergeRecord:
    """Apply an accepted merge: loser folds into winner, links repoint.

    Caller-facing contract (the AC4 door): raises :class:`BadInput` /
    :class:`NotFound` on a missing, retired, cross-kind, already-merged or
    measures-carrying pair — the whole thing runs in one transaction so
    nothing is half-applied. ``actor`` lands in the ``approved`` review
    row written when ``review_id`` names the proposal being applied;
    that row always carries ``garden.outcome='applied'``, the
    ``loser``/``winner`` and the ``merge_id`` (gr512124), plus
    ``verdict_extra`` (the caller's why/evidence) and ``model`` in the
    model column.

    ``identity_gate`` (the machine consumer sets it): a paper pair must
    clear :func:`~precis.graph.garden.paper_gate.paper_identity` inside
    the transaction, else :class:`MergeRefused` with the gate's reason
    (gr512125). A human applying by hand is not gated.

    A paper loser's body and PDF move to a winner that lacks them
    (gr548654, dedup's ``_move_body_to_survivor`` → ``move_body``) inside
    the same transaction — a failure rolls the whole merge back. The move
    is named in the review row (``body_moved_from`` / ``body_moved``) and
    in ``meta.garden_merge.body_moved``; a winner that already has a body
    keeps it. ``undo_merge`` does not move the body back: the restored
    loser comes back as a stub and the body stays on the winner.
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
            _check_reviewed_merge(store, conn, review_id, loser_id, winner_id)

        if identity_gate and loser_kind == "paper":
            from precis.graph.garden.paper_gate import paper_identity

            ident = paper_identity(store, loser_id, winner_id, conn=conn)
            if not ident.passes:
                raise MergeRefused(str(ident.reason), ident.why, ident.as_meta())
            verdict_extra = {**(verdict_extra or {}), "identity": ident.as_meta()}

        # A body-less / pdf-less winner takes the loser's body and PDF
        # (gr548654): without it the retire strands the only text on a
        # tombstone. Decided here so the review row names the move before
        # it runs; both bodies present is left to the identity gate.
        body_moves: list[str] = []
        if loser_kind == "paper":
            from precis.ingest.dedup import pending_body_move

            body_moves = pending_body_move(
                conn, survivor_ref_id=winner_id, duplicate_ref_id=loser_id
            )
            if body_moves:
                verdict_extra = {
                    **(verdict_extra or {}),
                    "body_moved_from": loser_id,
                    "body_moved": body_moves,
                }

        if review_id is not None:
            from precis.graph.garden.proposals import resolve_proposal

            resolved = resolve_proposal(
                store,
                review_id,
                actor=actor,
                verdict="approved",
                model=model,
                extra={
                    **(verdict_extra or {}),
                    "outcome": "applied",
                    "loser": loser_id,
                    "winner": winner_id,
                    "applied": merge_id,
                },
                conn=conn,
            )
            if resolved is None:
                raise NotFound(
                    f"no pending garden proposal review_id={review_id} "
                    "(already decided, or not a garden row)"
                )

        # Body first: its chunk-anchored links re-point onto the copies
        # the move lands on the winner, so the repoint below finds
        # them already on the winner instead of de-grounding them
        # against a chunkless winner.
        if body_moves:
            from precis.ingest.dedup import _move_body_to_survivor

            _move_body_to_survivor(
                store,
                survivor_ref_id=winner_id,
                duplicate_ref_id=loser_id,
                source=actor,
                conn=conn,
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
            # A chunk on the repointed (loser) endpoint can't follow
            # verbatim — the copy would read <winner>~<loser's chunk>
            # (gr520025). Re-ground on the winner's identical-text
            # chunk, de-ground to ref-level when the winner has no text
            # twin. The unchanged endpoint keeps its chunk even when
            # the loser owns it — crossed damage stays verbatim.
            new_src_chunk = _reground_link_chunk(
                conn,
                src_chunk,
                endpoint_ref_id=int(src_ref),
                old_ref_id=loser_id,
                new_ref_id=winner_id,
            )
            new_dst_chunk = _reground_link_chunk(
                conn,
                dst_chunk,
                endpoint_ref_id=int(dst_ref),
                old_ref_id=loser_id,
                new_ref_id=winner_id,
            )
            if new_src == new_dst and new_src_chunk == new_dst_chunk:
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
                    "sc": new_src_chunk,
                    "d": new_dst,
                    "dc": new_dst_chunk,
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
        # Merged into the live meta, not written over the early read: the
        # body move strips the loser's body-owned keys in between.
        loser_stamp = {
            "superseded_by": winner_id,
            META_MERGE: {
                "merge_id": merge_id,
                "winner": winner_id,
                "at": record.at,
                "aliases_added": record.aliases_added,
                "links": record.links,
                **({"body_moved": body_moves} if body_moves else {}),
            },
        }
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (Jsonb(loser_stamp), loser_id),
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


#: Relations an approved structural proposal may write (the 2a classing).
_STRUCTURAL_RELS = (
    "specialises",
    "generalises",
    "part-of",
    "contains",
    "represents",
    "represented-by",
    "realized-by",
    "realizes",
    "made-of",
    "used-in",
    "qualifies",
    "qualified-by",
    "instance-of",
    "has-instance",
)


@trust_door("generated", actor_type="agent")
def apply_structural(
    store: Store,
    proposal_review_id: int,
    *,
    actor: str,
    model: str | None = None,
) -> dict[str, Any]:
    """Apply an accepted *structural* garden proposal (slice 2b, ruling
    Q7): Reto's OR an Opus review approval writes the proposed edge.
    Origin stays ``generated`` — the approval, not the writer, carries
    the review identity; the door runs model-free so the machine-edge
    gate does not refuse it (the proposal's ``review_id`` is the
    admission, checked here — never a caller-set flag).

    One transaction: validate the pending proposal, write the terminal
    ``approved`` review of it, write the edge, record the consider act.
    Only the owner's approval additionally mints the edge's ``defined``
    grounding (rule 3); an Opus-applied edge carries a machine review
    and stays not-``defined``.

    The proposal payload carries ``relation`` plus endpoints: ``src`` /
    ``dst`` ids, or the relink convention (``parent`` → child=target_id
    specialises parent; ``child`` → src=child, dst=target_id).
    """
    from precis.graph.garden.proposals import (
        GARDEN_ACTOR,
        _key_pending,
        _parse_note,
        resolve_proposal,
    )
    from precis.store._grounding_ops import _owner_actors

    is_owner = model is None and actor in _owner_actors()
    if not is_owner and not (model or "").strip():
        raise BadInput(
            "structural-apply-not-reviewed: applying a structural proposal "
            "takes the owner's approval (actor=<owner>, no model) or a "
            "model review (model=<id>) — a bare actor string is neither",
            next="call apply_structural(..., actor=<owner>) or pass the "
            "reviewing model id",
        )
    with store.atomic() as conn:
        prop = conn.execute(
            "SELECT review_id, target_kind, target_id, note, content_sha "
            "  FROM reviews WHERE review_id = %s AND actor = %s "
            "   AND verdict = 'proposed'",
            (proposal_review_id, GARDEN_ACTOR),
        ).fetchone()
        if prop is None:
            raise NotFound(
                f"no pending garden proposal review_id={proposal_review_id} "
                "(already decided, or not a garden row)"
            )
        _pid, target_kind, target_id, note, content_sha = prop
        if target_kind != "ref":
            raise BadInput(
                f"structural proposals apply to refs; proposal "
                f"{proposal_review_id} targets {target_kind} {target_id}"
            )
        g = _parse_note(str(note)) or {}
        key = str(g.get("key") or "")
        if not _key_pending(store, str(target_kind), int(target_id), key):
            raise BadInput(
                f"structural-proposal-decided: proposal {proposal_review_id} "
                "is no longer the pending row for its key",
                next="check `precis garden proposals` for the current row",
            )
        payload = g.get("payload") or {}
        relation = str(payload.get("relation") or "")
        if relation not in _STRUCTURAL_RELS:
            raise BadInput(
                f"structural-payload: proposal {proposal_review_id} names "
                f"relation {relation!r} — only structural relations apply "
                f"through this door",
                options=list(_STRUCTURAL_RELS),
            )
        if payload.get("parent") is not None:
            src_id, dst_id = int(target_id), int(payload["parent"])
        elif payload.get("child") is not None:
            src_id, dst_id = int(payload["child"]), int(target_id)
        elif payload.get("src") is not None and payload.get("dst") is not None:
            src_id, dst_id = int(payload["src"]), int(payload["dst"])
        else:
            raise BadInput(
                f"structural-payload: proposal {proposal_review_id} has no "
                "src/dst (or parent/child) endpoints to write",
                next="the detector must put relation+endpoints in "
                "payload; carry it out by hand otherwise",
            )
        # staleness: the proposal pinned the target's content sha at
        # proposal time; a drifted target is a different question.
        sha_row = conn.execute(
            "SELECT precis_target_sha('ref', %s)", (int(target_id),)
        ).fetchone()
        now_sha = None if sha_row is None else sha_row[0]
        if content_sha != now_sha:
            raise BadInput(
                f"structural-proposal-stale: proposal {proposal_review_id} "
                "pinned a sha the target has drifted past",
                next="let the gardener re-propose on the current content, "
                "or carry the edge out by hand",
            )
        # terminal approval, recorded BEFORE the edge write so the ledger
        # sha matches what was actually reviewed.
        resolved = resolve_proposal(
            store,
            proposal_review_id,
            actor=actor,
            verdict="approved",
            model=model,
            extra={"applied": "structural"},
            conn=conn,
        )
        if resolved is None:
            raise NotFound(
                f"proposal {proposal_review_id} is not pending (raced a decision)"
            )
        # cycle guard for the taxon DAG spellings.
        if relation in ("specialises", "generalises"):
            child, parent = (
                (src_id, dst_id) if relation == "specialises" else (dst_id, src_id)
            )
            if store.taxon_would_cycle(child, parent):
                raise BadInput(
                    f"structural-cycle: {relation} {src_id}->{dst_id} would "
                    "close a taxon cycle",
                    next="reject the proposal or reparent elsewhere",
                )
        meta = {
            "proposal": proposal_review_id,
            "review_id": int(resolved.review_id),
            "detector": g.get("detector"),
            "why": g.get("title") or g.get("rationale"),
        }
        if model:
            meta["model"] = model
        # origin stays generated with NO model identity — the approval is
        # the admission (checked above); a model-bearing outer context
        # must not route this write into the machine-edge gate.
        with revision_context(
            "garden: apply structural proposal",
            actor=actor,
            model="",
            trust_origin="generated",
            trust_actor_type="agent",
        ):
            link = store.add_link(
                src_ref_id=src_id,
                dst_ref_id=dst_id,
                relation=relation,
                set_by="agent",
                meta={k: v for k, v in meta.items() if v is not None},
                conn=conn,
            )
        # the recorded act: the approval considered this edge.
        sha = store.trust_state("link", int(link.id), conn=conn)
        assert sha is not None and sha.sha is not None
        store.record_trust_act(
            TrustAct(
                action="consider",
                target_kind="link",
                target_id=int(link.id),
                expected_sha=sha.sha,
                reason=f"structural proposal {proposal_review_id} approved",
            ),
            actor=actor,
            actor_type="human" if is_owner else "agent",
            model=model,
        )
        defined = None
        if is_owner:
            defined = store.record_grounding(
                "link",
                int(link.id),
                grounding="defined",
                review_id=int(resolved.review_id),
                actor=actor,
                actor_type="human",
                reason=f"owner-approved structural edge "
                f"(proposal {proposal_review_id})",
                conn=conn,
            )
    return {
        "link_id": int(link.id),
        "src_ref_id": src_id,
        "dst_ref_id": dst_id,
        "relation": relation,
        "review_id": int(resolved.review_id),
        "defined_grounding_id": None if defined is None else int(defined.grounding_id),
    }


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
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
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
            model=model,
            extra={
                **(verdict_extra or {}),
                "outcome": "applied",
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


# ── reviewed memory actions ──────────────────────────────────────────────


@dataclass(slots=True)
class RewriteRecord:
    """Undo data on ``refs.meta.garden_rewrite``.

    Body chunks are deleted by the memory edit door, so the old chunk id
    alone cannot replay the bytes. Keep its text and outgoing links too.
    Incoming chunk links also need preservation across the chunk cascade.
    """

    rewrite_id: str
    proposal: int
    target: int
    prior_sha: str
    prior_body_chunk_id: int
    prior_body: str
    prior_title: str
    prior_body_ord: int
    links: list[dict[str, Any]]
    prior_header: dict[str, Any] | None = None
    applied_sha: str = ""
    applied_body_chunk_id: int = 0
    applied_links: list[dict[str, Any]] = field(default_factory=list)
    previous: dict[str, Any] | None = None


def memory_human_hold(store: Store, ref_id: int) -> bool:
    """Human origin or a currently considered stamp forbids machine edits.

    Unlike the detector's age window, a considered stamp never ages out of
    this protection. Semantic writes already demote it through trust guards.
    """
    state = store.trust_state("ref", ref_id)
    return state is not None and (
        state.origin == "human" or state.confidence == "considered"
    )


def _memory_successor(payload: dict[str, Any]) -> int | None:
    from precis.utils.handle_registry import parse

    value = payload.get("successor")
    if isinstance(value, int):
        return value
    parsed = parse(str(value or ""))
    return parsed[2] if parsed and parsed[:2] == ("memory", False) else None


def _memory_dependency_ids(conn: Any, target: int, garden: dict[str, Any]) -> set[int]:
    """Collect named evidence refs and routes before locking and rechecking.

    A tombstone restore or successor edit must wait for the rewrite just as
    an endpoint edit waits for a merge. Thread evidence also reads ancestor
    routes and terminal work refs; lock those refs and their part-of edges.
    """
    from precis.utils.handle_registry import parse

    evidence = garden.get("evidence") or {}
    payload = garden.get("payload") or {}
    ids = {target}
    handles = [
        payload.get("successor"),
        payload.get("delete_successor"),
        *evidence.get("terminal_work", {}),
    ]
    for mention in evidence.get("mentions", []):
        handles.extend([mention.get("retired_ref"), mention.get("successor")])
    if garden.get("action") == "compact":
        for values in (evidence.get("related") or {}).values():
            if isinstance(values, list):
                ids.update(int(v) for v in values if isinstance(v, int))
        for sha_key in payload.get("shas") or {}:
            if str(sha_key).isdigit():
                ids.add(int(sha_key))
        plan = payload.get("plan") or {}
        for edit in plan.get("edits") or []:
            if isinstance(edit, dict):
                handles.append(edit.get("ref"))
        shell = plan.get("shell")
        if isinstance(shell, dict):
            handles.append(shell.get("successor"))
        for put in plan.get("puts") or []:
            if not isinstance(put, dict):
                continue
            handles.extend(put.get("qualifies") or [])
            for link in put.get("links") or []:
                if isinstance(link, dict):
                    handles.append(link.get("dst"))
    for entry in evidence.get("memories", []):
        if isinstance(entry, dict) and isinstance(entry.get("ref_id"), int):
            ids.add(int(entry["ref_id"]))
    if isinstance(evidence.get("gripe"), int):
        ids.add(int(evidence["gripe"]))
    if garden.get("action") == "split":
        handles.append(payload.get("hub"))
        for child in payload.get("children") or []:
            if not isinstance(child, dict):
                continue
            handles.extend(
                spec.get("target")
                for spec in child.get("links") or []
                if isinstance(spec, dict)
            )
            handles.extend(child.get("qualifies") or [])
    for value in handles:
        if isinstance(value, int):
            ids.add(value)
        elif isinstance(value, str) and (parsed := parse(value)) and not parsed[1]:
            ids.add(parsed[2])
    if garden.get("detector") == "thread_shape":
        rows = conn.execute(
            "WITH RECURSIVE parents(id) AS (SELECT %s::bigint UNION "
            "SELECT l.dst_ref_id FROM links l JOIN parents p ON p.id = l.src_ref_id "
            "WHERE l.relation = 'part-of') SELECT id FROM parents",
            (target,),
        ).fetchall()
        ids.update(int(row[0]) for row in rows)
    # A tombstone can redirect through intermediate refs. Match the detector's
    # bounded chain rather than only locking the final live successor.
    frontier = set(ids)
    for _ in range(16):
        rows = conn.execute(
            "SELECT meta->>'superseded_by' FROM refs WHERE ref_id = ANY(%s)",
            (sorted(frontier),),
        ).fetchall()
        following = set()
        for (raw,) in rows:
            if not raw:
                continue
            value = str(raw)
            if value.isascii() and value.isdigit() and len(value) <= 19:
                rid = int(value)
            else:
                parsed = parse(value)
                if parsed is None or parsed[1]:
                    continue
                rid = parsed[2]
            if 0 < rid <= 9223372036854775807 and rid not in ids:
                following.add(rid)
        if not following:
            break
        ids.update(following)
        frontier = following
    return ids


def _checked_memory_proposal(
    store: Store, conn: Any, review_id: int, action: str
) -> tuple[int, dict[str, Any], tuple[Any, ...]]:
    """Lock, pin and re-run the same detector on exactly this memory.

    ``Store.atomic`` pins detector/handler checkouts to the apply transaction.
    Unknown detectors fail closed until they have an apply-time predicate.
    Evidence changes (including successor or main SHA drift) need a new review.
    """
    from precis.graph.garden import candidates
    from precis.graph.garden.proposals import (
        GARDEN_ACTOR,
        MEMORY_APPLY_DETECTORS,
        key_block,
        memory_successor_refusal,
    )

    prop = conn.execute(
        "SELECT target_kind, target_id, note, content_sha FROM reviews "
        "WHERE review_id = %s AND actor = %s AND verdict = 'proposed' FOR UPDATE",
        (review_id, GARDEN_ACTOR),
    ).fetchone()
    if prop is None:
        raise NotFound(f"no pending garden proposal review_id={review_id}")
    garden = (json.loads(prop[2] or "{}") or {}).get("garden") or {}
    if prop[0] != "ref" or garden.get("action") != action or not garden.get("key"):
        raise BadInput(f"review {review_id} is not a memory {action} proposal")
    target = int(prop[1])
    payload = garden.get("payload") or {}
    refusal = memory_successor_refusal(payload, garden.get("evidence") or {})
    if refusal:
        raise MergeRefused("apply_refused", refusal)
    successor = _memory_successor(payload) if action == "supersede" else None
    if action == "supersede" and (successor is None or successor == target):
        raise BadInput("supersede requires a distinct live memory successor")
    ids = sorted(_memory_dependency_ids(conn, target, garden))
    rows = conn.execute(
        "SELECT ref_id, kind, retired_at FROM refs WHERE ref_id = ANY(%s) "
        "ORDER BY ref_id FOR UPDATE",
        (ids,),
    ).fetchall()
    live = {int(r[0]) for r in rows if r[1] == "memory" and r[2] is None}
    if target not in live or (successor is not None and successor not in live):
        raise MergeRefused("target_gone", "the memory or its successor is not live")
    conn.execute(
        "SELECT link_id FROM links WHERE src_ref_id = ANY(%s) "
        "AND relation IN ('part-of', 'supersedes') ORDER BY link_id FOR UPDATE",
        (ids,),
    ).fetchall()
    # Read SHA after the lock, not in the snapshot that waited for the lock.
    sha = conn.execute("SELECT precis_target_sha('ref', %s)", (target,)).fetchone()[0]
    if not sha or prop[3] != sha:
        raise MergeRefused("stale", "the proposal no longer matches the target")
    if key_block(store, "ref", target, garden["key"]) == "rejected":
        raise MergeRefused(
            "apply_refused",
            "the proposal is rejected on this unchanged target content",
            next=f"get(kind='memory', id={target})",
        )
    if payload.get("base_sha") != sha:
        raise MergeRefused("apply_refused", "base_sha does not match the pinned target")
    if memory_human_hold(store, target):
        raise MergeRefused("human", "human-origin or considered memory needs a human")
    decided = conn.execute(
        "SELECT 1 FROM reviews WHERE note LIKE %s AND note ~ %s LIMIT 1",
        ('%"resolves"%', f'"resolves": {review_id}[^0-9]'),
    ).fetchone()
    newer = conn.execute(
        "SELECT note FROM reviews WHERE review_id > %s "
        "AND target_kind = 'ref' AND target_id = %s AND note LIKE %s",
        (review_id, target, '%"garden"%'),
    ).fetchall()
    replaced = False
    for (note,) in newer:
        try:
            replaced |= (json.loads(note).get("garden") or {}).get("key") == garden[
                "key"
            ]
        except (TypeError, ValueError, AttributeError):
            continue
    if decided is not None or replaced:
        raise MergeRefused("stale", "the proposal has already been decided or replaced")
    detector = str(garden.get("detector") or "")
    recheck = MEMORY_APPLY_DETECTORS.get(detector)
    if recheck is None:
        raise MergeRefused("apply_refused", f"no memory re-check for {detector!r}")
    detect = getattr(candidates, recheck)
    recheck_cursors: dict[str, int] = {f"{detector}:memory": target - 1}
    if detector == "griped" and isinstance(payload.get("gripe"), int):
        # Targeted mode: same gripe, same memory — several open complaints may
        # name this memory, so the pinned story selects the candidate.
        recheck_cursors["griped:gripe"] = int(payload["gripe"])
    fresh = detect(store, scan_cap=1, limit=1, cursors=recheck_cursors)
    if (
        not fresh
        or fresh[0].target_id != target
        or fresh[0].evidence != garden.get("evidence")
    ):
        raise MergeRefused("stale", "the detector predicate or its evidence changed")
    if detector != "griped" and fresh[0].action != action:
        raise MergeRefused(
            "apply_refused",
            "the proposal action differs from the fresh detector action",
        )
    for key in ("shas", "successor", "main_sha"):
        if detector == "griped" and key == "successor":
            continue  # the evaluator names it; membership checked below
        if fresh[0].payload.get(key) != payload.get(key):
            raise MergeRefused(
                "apply_refused",
                f"the proposal's {key} differs from fresh detector evidence",
            )
    if detector == "griped" and action == "supersede":
        named = {
            int(e["ref_id"])
            for e in (garden.get("evidence") or {}).get("memories", [])
            if isinstance(e, dict) and isinstance(e.get("ref_id"), int)
        }
        if successor not in named:
            raise MergeRefused(
                "apply_refused",
                "the supersede successor is not another memory the gripe names",
            )
    if action == "rewrite":
        _check_rewrite_scope(payload, fresh[0].payload)
    elif action == "split":
        _check_split_scope(payload, fresh[0].evidence)
    bodies = conn.execute(
        "SELECT chunk_id, text, ord FROM chunks WHERE ref_id = %s "
        "AND chunk_kind = 'memory_body' AND retired_at IS NULL AND ord >= 0 "
        "ORDER BY ord, chunk_id",
        (target,),
    ).fetchall()
    if len(bodies) != 1:
        raise MergeRefused("apply_refused", "memory apply requires one live body chunk")
    return target, garden, bodies[0]


def _check_rewrite_scope(payload: dict[str, Any], fresh: dict[str, Any]) -> None:
    """Models supply replacement text only; fresh detector spans own scope.

    The current rewrite detectors justify no link removal, so a removal list
    cannot erase provenance even when it invents a plausible reason.
    """
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops:
        raise BadInput("rewrite requires nonempty anchored ops")
    fresh_ops = fresh.get("ops", [])
    for op in ops:
        if not isinstance(op, dict) or not any(
            op.get("find") == allowed.get("find")
            and op.get("span") == allowed.get("span")
            for allowed in fresh_ops
        ):
            raise BadInput(
                "rewrite scope rule: every find and span must match a fresh detector op; "
                "the model may supply only replacement text"
            )
    removals = payload.get("remove_links", [])
    if not isinstance(removals, list):
        raise BadInput("remove_links must be an empty list")
    if removals:
        raise BadInput(
            "remove_links scope rule: memory rewrite detectors do not support removals"
        )


def _rewrite_ops(body: str, raw_ops: Any) -> list[dict[str, Any]]:
    """Translate detector spans (Python string offsets) to anchored ops.

    Right-to-left order keeps repeated handles at their pinned positions.
    Unresolved replacement text is a refusal, never an instruction to delete.
    """
    from precis.utils.edit_resolve import EditOp, find_candidates

    if not isinstance(raw_ops, list) or not raw_ops:
        raise BadInput("rewrite requires nonempty anchored ops")
    located: list[tuple[int, int, str, str]] = []
    for op in raw_ops:
        if not isinstance(op, dict):
            raise BadInput("rewrite ops must be objects")
        find, text = op.get("find"), op.get("text")
        if not isinstance(find, str) or not find or not isinstance(text, str):
            raise BadInput("rewrite requires resolved find/text strings")
        matches = find_candidates(body, EditOp(op="edit", find=find, text=text))
        span = op.get("span")
        if span is None:
            if len(matches) != 1:
                raise BadInput("rewrite anchor must be unique or carry its pinned span")
            start, end = matches[0].start, matches[0].end
        elif (
            isinstance(span, list)
            and len(span) == 2
            and all(isinstance(i, int) for i in span)
        ):
            start, end = span
        else:
            raise BadInput("rewrite span must be [start, end]")
        if not 0 <= start < end <= len(body) or body[start:end] != find:
            raise BadInput("the pinned rewrite span does not match the body")
        located.append((start, end, find, text))
    located.sort(reverse=True)
    if any(left[1] > right[0] for right, left in pairwise(located)):
        raise BadInput("rewrite spans overlap")
    return [
        {
            "find": find,
            "text": text,
            "match": "nth",
            "nth": 1
            + sum(
                match.start < start
                for match in find_candidates(
                    body, EditOp(op="edit", find=find, text=text)
                )
            ),
        }
        for start, _end, find, text in located
    ]


def _memory_links(conn: Any, target: int, body_chunk: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT to_jsonb(l) FROM links l WHERE src_ref_id = %s "
        "OR dst_chunk_id = %s OR dst_chunk_id IN "
        "(SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = -1) "
        "ORDER BY link_id FOR UPDATE",
        (target, body_chunk, target),
    ).fetchall()
    return [dict(row[0]) for row in rows]


def _guard_memory_link_measures(conn: Any, links: list[dict[str, Any]]) -> None:
    """A delete/reinsert cannot restore ON DELETE SET NULL measure anchors."""
    if conn.execute(
        "SELECT 1 FROM measures WHERE primary_link_id = ANY(%s) LIMIT 1",
        ([link["link_id"] for link in links],),
    ).fetchone():
        raise MergeRefused(
            "apply_refused",
            "memory links anchor measures; resolve them before rewriting",
            next="get(kind='skill', id='precis-measure-help')",
        )


def _memory_header(conn: Any, target: int) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT to_jsonb(c) FROM chunks c WHERE ref_id = %s AND ord = -1",
        (target,),
    ).fetchall()
    if len(rows) > 1:
        raise MergeRefused("apply_refused", "memory header mapping is ambiguous")
    return dict(rows[0][0]) if rows else None


def _guard_legacy_memory_links(links: list[dict[str, Any]]) -> None:
    """A version-zero edge can survive in place, but strict INSERT stamps v1."""
    if any(
        link["trust_write_version"] == 0
        and (
            link["src_chunk_id"] is not None
            or link["dst_chunk_id"] is not None
            or link["relation"] == "mentions"
        )
        for link in links
    ):
        raise MergeRefused(
            "apply_refused", "legacy memory links must be stamped before replacement"
        )


def _restore_memory_link(
    store: Store, conn: Any, link: dict[str, Any], *, actor: str
) -> bool:
    """Replay an existing edge, including its id and trust, through trust acts.

    Like merge undo, this restores a prior edge rather than admitting a new
    machine relation. Trust columns cannot be assigned by SQL: reconstruct
    the recorded origin/confidence through the existing import/consider doors.
    Return whether strict mode required a legacy v0 row to be stamped v1;
    its recorded unknown origin and confidence remain unchanged.
    """
    existing = conn.execute(
        "SELECT to_jsonb(l) FROM links l WHERE link_id = %s", (link["link_id"],)
    ).fetchone()
    if existing is not None and existing[0] == link:
        return False
    legacy = link["trust_write_version"] == 0
    preserve_v0 = (
        legacy and not conn.execute("SELECT precis_trust_strict()").fetchone()[0]
    )
    chunk_ids = {
        link[key] for key in ("src_chunk_id", "dst_chunk_id") if link[key] is not None
    }
    if chunk_ids:
        present = conn.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id = ANY(%s)",
            (list(chunk_ids),),
        ).fetchall()
        if chunk_ids != {row[0] for row in present}:
            raise MergeRefused(
                "apply_refused",
                "a memory link's original chunk cannot be restored",
                next="get(kind='skill', id='precis-gardener-memory-help')",
            )
    conn.execute(
        "DELETE FROM links WHERE link_id = %(link_id)s OR "
        "(src_ref_id = %(src_ref_id)s AND src_chunk_id IS NOT DISTINCT FROM "
        "%(src_chunk_id)s AND dst_ref_id = %(dst_ref_id)s AND dst_chunk_id "
        "IS NOT DISTINCT FROM %(dst_chunk_id)s AND relation = %(relation)s)",
        link,
    )
    origin = str(link["trust_origin"])
    # The import act must bind to INSERT: strict mode refuses an unstamped
    # unknown-origin row before a subsequent targeted act can restore it.
    import_act = (
        TrustAct(
            action="legacy-import",
            target_kind="link",
            create_match=str(link["relation"]),
            origin="unknown",
            confidence="unknown",
            reason="garden rewrite restores the recorded link origin",
        )
        if origin == "unknown" and not preserve_v0
        else None
    )
    write_origin = "generated" if origin == "unknown" else origin
    with revision_context(
        trust_origin="" if preserve_v0 else write_origin,
        trust_act=import_act,
        actor=actor,
        trust_actor_type="agent",
    ):
        apply_revision_context(conn, clear=True)
        if import_act is not None:
            conn.execute("SELECT set_config('precis.trust_act_bound', '', true)")
        conn.execute(
            "INSERT INTO links (link_id, src_ref_id, src_chunk_id, dst_ref_id, "
            "dst_chunk_id, relation, set_by, meta, created_at) VALUES "
            "(%(link_id)s, %(src_ref_id)s, %(src_chunk_id)s, %(dst_ref_id)s, "
            "%(dst_chunk_id)s, %(relation)s, %(set_by)s, %(meta)s, %(created_at)s)",
            {**link, "meta": Jsonb(link["meta"])},
        )
        if import_act is not None:
            conn.execute("SELECT set_config('precis.trust_act_bound', '', true)")
    apply_revision_context(conn, clear=True)
    if preserve_v0:
        return False
    lid = int(link["link_id"])
    state = store.trust_state("link", lid, conn=conn)
    assert state is not None
    if state.version == 0:
        store.record_trust_act(
            TrustAct(
                action="legacy-import",
                target_kind="link",
                target_id=lid,
                expected_sha=state.sha,
                origin=origin,
                confidence="unknown",
                reason="garden rewrite restores the recorded link origin",
            ),
            actor=actor,
            actor_type="agent",
        )
    confidence = str(link["trust_confidence"])
    state = store.trust_state("link", lid, conn=conn)
    assert state is not None
    if state.confidence != confidence:
        store.record_trust_act(
            TrustAct(
                action="consider" if confidence == "considered" else "demote",
                target_kind="link",
                target_id=lid,
                expected_sha=state.sha,
                confidence=None if confidence == "considered" else confidence,
                reason="garden rewrite restores the recorded link confidence",
            ),
            actor=actor,
            actor_type="agent",
        )
    apply_revision_context(conn, clear=True)
    return legacy


def _resolve_memory_apply(
    store: Store,
    conn: Any,
    review_id: int,
    *,
    actor: str,
    model: str | None,
    garden: dict[str, Any],
    applied: str,
    verdict_extra: dict[str, Any] | None,
) -> None:
    from precis.graph.garden.proposals import resolve_proposal

    resolved = resolve_proposal(
        store,
        review_id,
        actor=actor,
        verdict="approved",
        model=model,
        extra={
            **(verdict_extra or {}),
            "action": garden["action"],
            "detector": garden["detector"],
            "evidence": garden["evidence"],
            "outcome": "applied",
            "applied": applied,
        },
        conn=conn,
    )
    if resolved is None:
        raise MergeRefused("stale", "the proposal has already been decided")


def _garden_writer(model: str | None) -> dict[str, str]:
    """The ``revisions.writer`` stamp of a gardener memory write; ``model`` is
    the reviewer that approved it, kept off ``model=`` (see the machine-edge
    gate note in :func:`apply_structural`)."""
    stamp = {"source": "garden"}
    if model:
        stamp.update(model=model, model_source="caller")
    return stamp


def _review_model_token(model: str | None) -> str:
    """Keep safe model names readable; reserve hex- for reversible UTF-8 encoding."""
    name = model or "unknown"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) and not name.startswith(
        "hex-"
    ):
        return name
    return "hex-" + name.encode("utf-8").hex()


@trust_door("generated")
def apply_rewrite(
    store: Store,
    review_id: int,
    *,
    actor: str,
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
    applied: str | None = None,
) -> RewriteRecord:
    """Apply a reviewed anchored memory rewrite and its trust stamp atomically.

    All outgoing edges survive mention synchronization. A shorter
    body dropping a handle must not silently erase provenance or recall routes.

    ``applied`` overrides the ledger's ``garden.applied`` marker — the
    consumer's no-model duplicate-successor path records ``"mention"``
    (gr564554); the reversal record id then lands under ``rewrite_id``
    so the ledger still names the undo handle.
    """
    from precis.dispatch import Hub
    from precis.handlers.memory import _derive_title
    from precis.store._chunks_ops import header_card_text
    from precis.utils.edit_resolve import EditOp, apply_edit

    with store.atomic() as conn:
        target, garden, body = _checked_memory_proposal(
            store, conn, review_id, "rewrite"
        )
        payload = garden["payload"]
        ops = _rewrite_ops(str(body[1]), payload.get("ops"))
        links = _memory_links(conn, target, int(body[0]))
        _guard_memory_link_measures(conn, links)
        header = _memory_header(conn, target)
        _guard_legacy_memory_links(links)
        ref = store.get_ref(kind="memory", id=target)
        assert ref is not None
        if header is not None and ref.title == _derive_title(str(body[1])):
            rewritten = str(body[1])
            for op in ops:
                rewritten = apply_edit(rewritten, EditOp(op="edit", **op)).new_buffer
            if not header_card_text(_derive_title(rewritten), ref.meta.get("hook")):
                raise MergeRefused(
                    "apply_refused", "the rewrite would remove the header anchor"
                )
        rec = RewriteRecord(
            rewrite_id=f"garden-rewrite:{target}:{review_id}",
            proposal=review_id,
            target=target,
            prior_sha=payload["base_sha"],
            prior_body_chunk_id=int(body[0]),
            prior_body=str(body[1]),
            prior_title=ref.title,
            prior_body_ord=int(body[2]),
            links=links,
            prior_header=header,
            previous=(ref.meta or {}).get("garden_rewrite"),
        )
        applied_id = applied or rec.rewrite_id
        extra = verdict_extra
        if applied_id != rec.rewrite_id:
            extra = {**(verdict_extra or {}), "rewrite_id": rec.rewrite_id}
        _resolve_memory_apply(
            store,
            conn,
            review_id,
            actor=actor,
            model=model,
            garden=garden,
            applied=applied_id,
            verdict_extra=extra,
        )
        reason = f"garden:{garden['detector']}:{review_id}"
        with revision_context(
            reason, actor=actor, model="", writer=_garden_writer(model)
        ):
            # The full base_sha was checked under the ref lock above. The
            # handler's anchors and expected_body guard run on this same conn.
            Hub(store=store).sibling("memory").edit(id=target, ops=ops, reason=reason)
        new_body = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
            (target,),
        ).fetchone()
        if new_body is None:
            raise MergeRefused("apply_refused", "the rewritten memory body is missing")
        rec.applied_body_chunk_id = int(new_body[0])
        chunk_map = {rec.prior_body_chunk_id: rec.applied_body_chunk_id}
        if header is not None:
            new_header = _memory_header(conn, target)
            if new_header is None:
                raise MergeRefused("apply_refused", "the rewritten header is missing")
            chunk_map[header["chunk_id"]] = new_header["chunk_id"]
        for link in links:
            restored = dict(link)
            for key in ("src_chunk_id", "dst_chunk_id"):
                restored[key] = chunk_map.get(restored[key], restored[key])
            _restore_memory_link(store, conn, restored, actor=actor)
        rec.applied_links = _memory_links(conn, target, rec.applied_body_chunk_id)
        state = store.trust_state("ref", target, conn=conn)
        assert state is not None and state.sha is not None
        rec.applied_sha = state.sha
        store.stamp_ref_meta(target, {"garden_rewrite": asdict(rec)}, conn=conn)
        store.record_trust_act(
            TrustAct(
                action="consider",
                target_kind="ref",
                target_id=target,
                expected_sha=state.sha,
                reason=(
                    f"kg-review:{_review_model_token(model)}:"
                    f"{datetime.now(UTC):%Y-%m-%d}:fixed"
                ),
            ),
            actor=actor,
            actor_type="agent",
            model=model,
        )
    return rec


@trust_door("generated")
def undo_rewrite(store: Store, rewrite_id: str, *, actor: str) -> dict[str, int]:
    """Restore the snapshot and reject its key so the next tick cannot replay it."""
    from precis.dispatch import Hub
    from precis.graph.garden.proposals import GARDEN_VERSION

    with store.atomic() as conn:
        row = conn.execute(
            "SELECT ref_id, meta->'garden_rewrite', title FROM refs "
            "WHERE meta->'garden_rewrite'->>'rewrite_id' = %s "
            "AND retired_at IS NULL FOR UPDATE",
            (rewrite_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no rewrite record for rewrite_id={rewrite_id!r}")
        target, rec = int(row[0]), row[1]
        state = store.trust_state("ref", target, conn=conn)
        links = _memory_links(conn, target, rec["applied_body_chunk_id"])
        _guard_memory_link_measures(conn, links)
        if (
            state is None
            or state.sha != rec["applied_sha"]
            or links != rec["applied_links"]
        ):
            raise MergeRefused("stale", "memory or links changed after the rewrite")
        # Keep identical edges, including legacy v0 links that strict mode
        # cannot reinsert with the same trust version. Chunk replacements
        # below only cascade edges whose anchors changed during apply.
        prior_links = {link["link_id"]: link for link in rec["links"]}
        deleted = store.delete_links(
            [l["link_id"] for l in links if prior_links.get(l["link_id"]) != l],
            conn=conn,
        )
        reason = f"garden:undo:{rewrite_id}"
        with revision_context(reason, actor=actor):
            apply_revision_context(conn, clear=True)
            store.chunks.replace_body_chunk(
                target,
                rec["prior_body"],
                chunk_kind="memory_body",
                source=actor,
                reason=reason,
                conn=conn,
            )
            # Reinsert the saved identity too: chunk-addressed links in the
            # snapshot must point at the original chunk, not a missing FK.
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
                (target,),
            )
            conn.execute(
                "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, %s, 'memory_body', %s)",
                (
                    rec["prior_body_chunk_id"],
                    target,
                    rec["prior_body_ord"],
                    rec["prior_body"],
                ),
            )
            if row[2] != rec["prior_title"]:
                store.chunks.set_ref_title(
                    target, rec["prior_title"], source=actor, conn=conn
                )
                header = rec.get("prior_header")
                if header is None:
                    store.chunks.sync_header_card(target, conn=conn)
                else:
                    # Replay the saved identity, just as for the body. A
                    # newly synthesized card would strand the original links.
                    conn.execute(
                        "DELETE FROM chunks WHERE ref_id = %s AND ord = -1",
                        (target,),
                    )
                    conn.execute(
                        "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, "
                        "text, meta, set_by, created_at) VALUES "
                        "(%(chunk_id)s, %(ref_id)s, %(ord)s, %(chunk_kind)s, "
                        "%(text)s, %(meta)s, %(set_by)s, %(created_at)s)",
                        {**header, "meta": Jsonb(header["meta"])},
                    )
            for link in rec["links"]:
                _restore_memory_link(store, conn, link, actor=actor)
            # Replay exact bytes rather than running them through a new
            # secret-mask pass, then recompute the same derived audit tags as
            # the memory edit door against the restored body and routes.
            handler = Hub(store=store).sibling("memory")
            handler._apply_attribution_tag(
                target,
                handler._attribution_misses(rec["prior_body"], reject=False),
                conn=conn,
            )
            handler._apply_shape_tags(
                target, handler._shape_after_write(target, None, conn=conn), conn=conn
            )
            if rec.get("previous") is None:
                conn.execute(
                    "UPDATE refs SET meta = meta - 'garden_rewrite' WHERE ref_id = %s",
                    (target,),
                )
            else:
                store.stamp_ref_meta(
                    target, {"garden_rewrite": rec["previous"]}, conn=conn
                )
        proposal = conn.execute(
            "SELECT note FROM reviews WHERE review_id = %s",
            (rec["proposal"],),
        ).fetchone()
        if proposal is not None:
            garden = (json.loads(proposal[0] or "{}") or {}).get("garden") or {}
            if garden.get("key"):
                store.record_target_review(
                    "ref",
                    target,
                    actor=actor,
                    verdict="rejected",
                    version=GARDEN_VERSION,
                    note=json.dumps(
                        {
                            "garden": {
                                "v": 1,
                                "key": garden["key"],
                                "resolves": rec["proposal"],
                                "undone": rewrite_id,
                                "reason": "undone",
                            }
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    conn=conn,
                )
    return {"links_restored": len(rec["links"]), "links_deleted": deleted}


def _retirement_links(conn: Any, scope: list[int]) -> list[dict[str, Any]]:
    """Include both ends and existing successor twins before deduplication."""
    return [
        dict(row[0])
        for row in conn.execute(
            "SELECT to_jsonb(l) FROM links l WHERE src_ref_id = ANY(%s) "
            "OR dst_ref_id = ANY(%s) ORDER BY link_id FOR UPDATE",
            (scope, scope),
        ).fetchall()
    ]


def _retirement_ends(
    conn: Any, ids: list[int], *, content_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    """Pin liveness everywhere, content only where undo rewrites a link."""
    conn.execute(
        "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) ORDER BY ref_id FOR UPDATE",
        (ids,),
    ).fetchall()
    return [
        dict(row[0])
        for row in conn.execute(
            "SELECT jsonb_build_object('id', ref_id, 'kind', kind, "
            "'retired_at', retired_at, 'sha', CASE WHEN ref_id = ANY(%s) "
            "THEN precis_target_sha('ref', ref_id) END) "
            "FROM refs WHERE ref_id = ANY(%s) ORDER BY ref_id",
            (ids if content_ids is None else content_ids, ids),
        ).fetchall()
    ]


def _retirement_chunks(conn: Any, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted(
        {
            link[key]
            for link in links
            for key in ("src_chunk_id", "dst_chunk_id")
            if link[key] is not None
        }
    )
    return [
        dict(row[0])
        for row in conn.execute(
            "SELECT jsonb_build_object('chunk_id', chunk_id, 'ref_id', ref_id, "
            "'ord', ord, 'text', text, 'content_sha', content_sha, "
            "'retired_at', retired_at) FROM chunks WHERE chunk_id = ANY(%s) "
            "ORDER BY chunk_id FOR UPDATE",
            (ids,),
        ).fetchall()
    ]


def _retirement_snapshot(
    store: Store,
    conn: Any,
    target: int,
    garden: dict[str, Any],
    review_id: int,
    *,
    successor: int | None,
) -> dict[str, Any]:
    from precis.dispatch import Hub
    from precis.graph.garden.proposals import memory_successor_refusal

    ref = store.get_ref(kind="memory", id=target)
    assert ref is not None
    reason = f"garden:{garden['detector']}:{review_id}"
    handle, resolved, _kind = (
        Hub(store=store)
        .sibling("memory")
        ._delete_successor(
            target,
            ref.meta or {},
            conn=conn,
            superseded_by=f"me{successor}" if successor is not None else None,
            reason=reason,
        )
    )
    refusal = memory_successor_refusal({"successor": handle}, {})
    if refusal:
        raise MergeRefused("apply_refused", refusal)
    scope = sorted({target, *([resolved] if resolved is not None else [])})
    ends = _retirement_ends(conn, scope)
    if garden["action"] == "retire":
        payload = garden["payload"]
        sha = next((end["sha"] for end in ends if end["id"] == resolved), None)
        if (
            payload.get("delete_successor") != handle
            or payload.get("delete_successor_sha") != sha
            or payload.get("delete_successor_error")
        ):
            raise MergeRefused(
                "stale", "the retirement successor or its content changed"
            )
    if resolved is not None:
        _check_retirement_cycle(conn, target, resolved)
    links = _retirement_links(conn, scope)
    affected = [l for l in links if target in (l["src_ref_id"], l["dst_ref_id"])]
    _guard_memory_link_measures(conn, affected)
    endpoints = sorted(
        {*scope, *(l[k] for l in links for k in ("src_ref_id", "dst_ref_id"))}
    )
    _retirement_ends(conn, endpoints)
    return {
        "retirement_id": f"garden-{garden['action']}:{target}:{review_id}",
        "proposal": review_id,
        "target": target,
        "successor": resolved,
        "successor_handle": handle,
        "scope": scope,
        "endpoints": endpoints,
        "prior_meta": ref.meta or {},
        "links": links,
        "chunks": _retirement_chunks(conn, links),
    }


def _check_retirement_cycle(conn: Any, target: int, successor: int) -> None:
    # Both endpoints migrate. Direct target/successor edges collapse away;
    # every other migrated edge must leave containment acyclic.
    cycle = conn.execute(
        "WITH RECURSIVE paths(id, goal) AS ("
        "SELECT dst_ref_id, %(successor)s::bigint FROM links "
        "WHERE relation = 'part-of' AND src_ref_id = %(target)s "
        "AND dst_ref_id != %(successor)s "
        "UNION SELECT %(successor)s::bigint, src_ref_id FROM links "
        "WHERE relation = 'part-of' AND dst_ref_id = %(target)s "
        "AND src_ref_id != %(successor)s "
        "UNION SELECT l.dst_ref_id, p.goal FROM links l "
        "JOIN paths p ON p.id = l.src_ref_id WHERE l.relation = 'part-of') "
        "SELECT 1 FROM paths WHERE id = goal LIMIT 1",
        {"target": target, "successor": successor},
    ).fetchone()
    if cycle is not None:
        raise MergeRefused(
            "apply_refused",
            "migrating the memory's part-of links would close a cycle",
        )


def _save_retirement(store: Store, conn: Any, rec: dict[str, Any]) -> None:
    rec["applied_links"] = _retirement_links(conn, rec["scope"])
    prior = {link["link_id"]: link for link in rec["links"]}
    applied = {link["link_id"]: link for link in rec["applied_links"]}
    rewritten = [link for lid, link in prior.items() if applied.get(lid) != link]
    changed = rewritten + [
        link for lid, link in applied.items() if prior.get(lid) != link
    ]
    rec["content_endpoints"] = sorted(
        {*rec["scope"], *(l[k] for l in changed for k in ("src_ref_id", "dst_ref_id"))}
    )
    # Undo never touches unchanged successor links or their endpoints.
    rec["endpoints"] = rec["content_endpoints"]
    chunk_ids = {l[k] for l in rewritten for k in ("src_chunk_id", "dst_chunk_id")}
    rec["chunks"] = [c for c in rec["chunks"] if c["chunk_id"] in chunk_ids]
    rec["applied_ends"] = _retirement_ends(
        conn, rec["endpoints"], content_ids=rec["content_endpoints"]
    )
    rec["applied_meta"] = conn.execute(
        "SELECT meta - 'garden_retirement' FROM refs WHERE ref_id = %s",
        (rec["target"],),
    ).fetchone()[0]
    store.stamp_ref_meta(rec["target"], {"garden_retirement": rec}, conn=conn)


@trust_door("generated")
def undo_retirement(store: Store, retirement_id: str, *, actor: str) -> dict[str, int]:
    """Restore a retired node and its exact edges, refusing intervening edits.

    Both ends are saved because migration can collapse links onto existing
    successor twins. Only changed rows are removed; unchanged trust survives.
    The snapshot and terminal undo decision share the restoration transaction.
    ``links_restamped`` counts legacy edges strict trust required to move from
    version 0 to 1, preserving their original origin and confidence.
    """
    from precis.graph.garden.proposals import GARDEN_VERSION

    with store.atomic() as conn:
        row = conn.execute(
            "SELECT ref_id, meta->'garden_retirement', meta - 'garden_retirement' "
            "FROM refs WHERE meta->'garden_retirement'->>'retirement_id' = %s "
            "AND retired_at IS NOT NULL FOR UPDATE",
            (retirement_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no retirement record for {retirement_id!r}")
        target, rec = int(row[0]), row[1]
        ends = _retirement_ends(
            conn, rec["endpoints"], content_ids=rec["content_endpoints"]
        )
        links = _retirement_links(conn, rec["scope"])
        prior = {l["link_id"]: l for l in rec["links"]}
        applied = {l["link_id"]: l for l in rec["applied_links"]}
        rewritten = [l for lid, l in prior.items() if applied.get(lid) != l]
        removed = [l for lid, l in applied.items() if prior.get(lid) != l]
        touched = {l["link_id"] for l in rewritten + removed}
        current = {l["link_id"]: l for l in links}
        edge_keys = (
            "src_ref_id",
            "src_chunk_id",
            "dst_ref_id",
            "dst_chunk_id",
            "relation",
        )
        restored_edges = {tuple(l[k] for k in edge_keys) for l in rewritten}
        if (
            ends != rec["applied_ends"]
            or any(current.get(lid) != applied.get(lid) for lid in touched)
            or any(
                l["link_id"] not in touched
                and tuple(l[k] for k in edge_keys) in restored_edges
                for l in links
            )
            or row[2] != rec["applied_meta"]
            or _retirement_chunks(conn, rewritten) != rec["chunks"]
        ):
            raise BadInput(
                "retired memory, successor or link end changed; undo is unsafe",
                next=f"inspect me{target} and its garden_retirement snapshot; "
                "reconcile changed endpoints before retrying garden undo",
            )
        _guard_memory_link_measures(conn, removed)
        deleted = store.delete_links(
            [l["link_id"] for l in removed],
            conn=conn,
        )
        restamped = []
        with revision_context(f"garden:undo:{retirement_id}", actor=actor):
            apply_revision_context(conn, clear=True)
            store.restore_ref(target, conn=conn)
            conn.execute(
                "UPDATE refs SET meta = %s WHERE ref_id = %s",
                (Jsonb(rec["prior_meta"]), target),
            )
            for link in rewritten:
                if _restore_memory_link(store, conn, link, actor=actor):
                    restamped.append(link["link_id"])
        proposal = conn.execute(
            "SELECT note FROM reviews WHERE review_id = %s", (rec["proposal"],)
        ).fetchone()
        assert proposal is not None
        garden = (json.loads(proposal[0]) or {}).get("garden") or {}
        store.record_target_review(
            "ref",
            target,
            actor=actor,
            verdict="rejected",
            version=GARDEN_VERSION,
            note=json.dumps(
                {
                    "garden": {
                        "v": 1,
                        "key": garden["key"],
                        "resolves": rec["proposal"],
                        "undone": retirement_id,
                        "reason": "undone",
                        "restamped_links": restamped,
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            conn=conn,
        )
    return {
        "links_restored": len(rewritten),
        "links_deleted": deleted,
        "links_restamped": len(restamped),
    }


@trust_door("generated")
def apply_supersede(
    store: Store,
    review_id: int,
    *,
    actor: str,
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Record the successor and retire through the existing memory delete door.

    The compact-first gate (gr561025) applies here too: a supersede is a
    retirement, so a pending or unasked-and-triggered compaction on the
    node completes first — only an applied compaction counts as
    preservation.
    """
    from precis.dispatch import Hub

    with store.atomic() as conn:
        target, garden, _body = _checked_memory_proposal(
            store, conn, review_id, "supersede"
        )
        _compact_first(store, conn, target)
        successor = _memory_successor(garden["payload"])
        assert successor is not None
        rec = _retirement_snapshot(
            store, conn, target, garden, review_id, successor=successor
        )
        _resolve_memory_apply(
            store,
            conn,
            review_id,
            actor=actor,
            model=model,
            garden=garden,
            applied=rec["retirement_id"],
            verdict_extra=verdict_extra,
        )
        reason = f"garden:{garden['detector']}:{review_id}"
        with revision_context(reason, actor=actor, writer=_garden_writer(model)):
            apply_revision_context(conn, clear=True)
            Hub(store=store).sibling("memory").delete(
                id=target, reason=reason, superseded_by=f"me{successor}"
            )
            # Delete migrates existing edges. Write the tombstone trail after
            # that migration so it remains winner → loser, not a self-loop.
            store.add_link(
                src_ref_id=successor,
                dst_ref_id=target,
                relation="supersedes",
                set_by="agent",
                meta={"garden_supersede": {"proposal": review_id}},
                conn=conn,
            )
            _save_retirement(store, conn, rec)
    return {"target": target, "successor": successor}


@trust_door("generated")
def apply_retire(
    store: Store,
    review_id: int,
    *,
    actor: str,
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Apply a reviewed retirement through the memory delete door, atomically.

    The compact-first gate (gr561025): a pending or undecided compaction
    on the node runs before this retirement may apply.
    """
    from precis.dispatch import Hub

    with store.atomic() as conn:
        target, garden, _body = _checked_memory_proposal(
            store, conn, review_id, "retire"
        )
        # Stale successor evidence must close retryably before checking
        # compaction; both checks still precede every retirement write.
        rec = _retirement_snapshot(
            store, conn, target, garden, review_id, successor=None
        )
        _compact_first(store, conn, target)
        _resolve_memory_apply(
            store,
            conn,
            review_id,
            actor=actor,
            model=model,
            garden=garden,
            applied=rec["retirement_id"],
            verdict_extra=verdict_extra,
        )
        reason = f"garden:{garden['detector']}:{review_id}"
        with revision_context(reason, actor=actor, writer=_garden_writer(model)):
            Hub(store=store).sibling("memory").delete(
                id=target, reason=reason, superseded_by=rec["successor_handle"]
            )
            _save_retirement(store, conn, rec)
    return {"target": target}


# ── compaction (gr561025) ───────────────────────────────────────────────────
#
# No node retires and no sentence drops before a per-sentence plan: the
# compact detector's triggers (expired, cold, history-dense, or a pending
# retire proposal) file one ``compact`` proposal; the evaluator supplies
# ``payload.plan`` — anchored edits into the pinned related neighbourhood,
# atomic puts, and the shell's fate. ``apply_compact`` runs it as one unit
# under one ``meta.garden_compact`` record; ``undo_compact`` restores every
# touched body, retires the minted children and un-retires the shell.

COMPACT_KEEP_MAX_LINES = 3
COMPACT_MAX_EXPIRES_DAYS = 3650


def compact_plan_shape(plan: Any) -> bool:
    """The consumer's shallow reviewable check on ``payload.plan``."""
    if not isinstance(plan, dict):
        return False
    shell = plan.get("shell")
    return (
        isinstance(plan.get("edits") or [], list)
        and isinstance(plan.get("puts") or [], list)
        and isinstance(plan.get("sentences"), list)
        and isinstance(shell, dict)
        and shell.get("fate") in {"retire", "keep"}
    )


def _plan_ref(value: Any) -> int | None:
    """An int ref id or a ``meN``-style record handle, decoded."""
    from precis.utils.handle_registry import parse

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        parsed = parse(value)
        if parsed is not None and not parsed[1]:
            return int(parsed[2])
    return None


def _checked_compact_plan(
    store: Store, conn: Any, target: int, garden: dict[str, Any]
) -> dict[str, Any]:
    """Validate the reviewed plan against the pinned neighbourhood, under lock.

    Edits may only land on the detector's related set (hub parents and
    siblings, qualifies targets, recall neighbours) — like the rewrite
    scope rule, the model supplies text inside evidence, never new scope.
    """
    evidence = garden.get("evidence") or {}
    payload = garden.get("payload") or {}
    plan = payload.get("plan")
    if not compact_plan_shape(plan):
        raise BadInput("compact requires a well-formed payload.plan")
    assert isinstance(plan, dict)
    related = evidence.get("related") or {}
    editable = {
        int(v)
        for values in related.values()
        for v in (values or [])
        if isinstance(v, int)
    }
    live = {
        int(r[0])
        for r in conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND kind = 'memory' "
            "AND retired_at IS NULL",
            (sorted(editable),),
        ).fetchall()
    }
    if live != editable:
        raise MergeRefused("stale", "a related memory the plan edits is not live")
    edits: list[dict[str, Any]] = []
    for i, edit in enumerate(plan.get("edits") or []):
        if not isinstance(edit, dict):
            raise BadInput(f"plan.edits[{i}] must be an object")
        ref = _plan_ref(edit.get("ref"))
        if ref is None or ref == target or ref not in editable:
            raise BadInput(
                f"plan.edits[{i}].ref must name a related live memory from "
                "the detector's neighbourhood; the shell's own body is not "
                "an edit target"
            )
        if memory_human_hold(store, ref):
            raise MergeRefused(
                "human",
                f"plan.edits[{i}] touches human-origin or considered me{ref}",
            )
        ops = edit.get("ops")
        if not isinstance(ops, list) or not ops:
            raise BadInput(f"plan.edits[{i}] requires nonempty anchored ops")
        for j, op in enumerate(ops):
            if (
                not isinstance(op, dict)
                or not isinstance(op.get("find"), str)
                or not op["find"]
                or not isinstance(op.get("text"), str)
            ):
                raise BadInput(
                    f"plan.edits[{i}].ops[{j}] requires resolved find/text strings"
                )
            span = op.get("span")
            if span is not None and not (
                isinstance(span, list)
                and len(span) == 2
                and all(isinstance(x, int) for x in span)
            ):
                raise BadInput(f"plan.edits[{i}].ops[{j}].span must be [start, end]")
        edits.append(
            {
                "ref": ref,
                "ops": ops,
                "reason": str(edit.get("reason") or ""),
            }
        )
    puts: list[dict[str, Any]] = []
    for i, put in enumerate(plan.get("puts") or []):
        if not isinstance(put, dict):
            raise BadInput(f"plan.puts[{i}] must be an object")
        if not isinstance(put.get("title"), str) or not put["title"].strip():
            raise BadInput(f"plan.puts[{i}].title must state the claim")
        if not isinstance(put.get("body"), str) or not put["body"].strip():
            raise BadInput(f"plan.puts[{i}].body must carry the claim text")
        section = put.get("section")
        if section is not None and (
            not isinstance(section, str) or not section.startswith("section:")
        ):
            raise BadInput(f"plan.puts[{i}].section must be a section: tag")
        qualifies = []
        for j, value in enumerate(put.get("qualifies") or []):
            ref = _plan_ref(value)
            if ref is None:
                raise BadInput(f"plan.puts[{i}].qualifies[{j}] must name a live ref")
            qualifies.append(ref)
        links = []
        for j, link in enumerate(put.get("links") or []):
            if not isinstance(link, dict) or not isinstance(link.get("relation"), str):
                raise BadInput(
                    f"plan.puts[{i}].links[{j}] needs a relation and a live dst ref"
                )
            ref = _plan_ref(link.get("dst"))
            if ref is None:
                raise BadInput(f"plan.puts[{i}].links[{j}].dst must name a live ref")
            links.append({"dst": ref, "relation": str(link["relation"])})
        puts.append(
            {
                "title": put["title"].strip(),
                "body": put["body"],
                "section": section,
                "qualifies": qualifies,
                "links": links,
            }
        )
    shell = dict(plan["shell"])
    if shell["fate"] == "retire":
        successor = shell.get("successor")
        if isinstance(successor, dict):
            idx = successor.get("put")
            if not isinstance(idx, int) or not 0 <= idx < len(puts):
                raise BadInput('shell.successor {"put": i} must index a plan put')
        elif successor is not None:
            ref = _plan_ref(successor)
            parents = {
                int(v) for v in (related.get("parents") or []) if isinstance(v, int)
            }
            if ref is None or ref not in parents:
                raise BadInput(
                    "a compact retirement's superseded_by must be the shell's "
                    "hub parent, a minted child ({'put': i}) or null"
                )
            shell["successor"] = ref
    else:
        summary = shell.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise BadInput("a kept shell requires a nonempty summary")
        if sum(1 for line in summary.splitlines() if line.strip()) > (
            COMPACT_KEEP_MAX_LINES
        ):
            raise BadInput(
                f"a kept shell's summary is at most {COMPACT_KEEP_MAX_LINES} "
                "non-empty lines"
            )
        days = shell.get("expires_days")
        if (
            not isinstance(days, int)
            or isinstance(days, bool)
            or not (0 < days <= COMPACT_MAX_EXPIRES_DAYS)
        ):
            raise BadInput(
                f"a kept shell re-arms expires: expires_days 1..{COMPACT_MAX_EXPIRES_DAYS}"
            )
    _checked_compact_sentences(plan, evidence, shell, edits, puts, editable)
    return {"edits": edits, "puts": puts, "shell": shell}


#: The only dispositions that may drop a sentence — every other sentence is
#: kept on a kept shell, moved into a related node, or minted atomic.
COMPACT_DROP_REASONS = frozenset({"history", "restatement"})


def _checked_compact_sentences(
    plan: dict[str, Any],
    evidence: dict[str, Any],
    shell: dict[str, Any],
    edits: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    editable: set[int],
) -> None:
    """Per-sentence accounting: every detector sentence gets a disposition.

    No sentence silently drops with a replacement summary (gr561025): the
    plan must name one disposition per ``evidence.sentences`` entry, in
    order — ``keep`` (only on a kept shell), ``move`` onto a related node
    that a plan edit actually touches, ``mint`` into a named put, or
    ``drop`` with its allowed reason. A sentence-capped body cannot be
    accounted for at all: it splits or shrinks first.
    """
    if evidence.get("sentences_capped"):
        raise BadInput(
            "compact evidence is sentence-capped; split or shorten the body "
            "before a per-sentence plan can account for it"
        )
    sentences = evidence.get("sentences") or []
    dispositions = plan.get("sentences")
    if not isinstance(dispositions, list) or len(dispositions) != len(sentences):
        raise BadInput(
            "compact requires one plan.sentences disposition per "
            "evidence.sentences entry, in order"
        )
    edited = {int(edit["ref"]) for edit in edits}
    for i, entry in enumerate(dispositions):
        if not isinstance(entry, dict):
            raise BadInput(f"plan.sentences[{i}] must be an object")
        span = entry.get("span")
        expected = (
            (sentences[i] or {}).get("span") if isinstance(sentences[i], dict) else None
        )
        if span is not None and span != expected:
            raise BadInput(
                f"plan.sentences[{i}].span must echo evidence.sentences[{i}].span"
            )
        disp = entry.get("disposition")
        if disp == "keep":
            if shell["fate"] != "keep":
                raise BadInput(
                    f"plan.sentences[{i}] keeps a sentence on a shell that retires"
                )
        elif disp == "move":
            ref = _plan_ref(entry.get("ref"))
            if ref is None or ref not in editable:
                raise BadInput(
                    f"plan.sentences[{i}].ref must name a related live memory"
                )
            if ref not in edited:
                raise BadInput(
                    f"plan.sentences[{i}] moves to me{ref} but no plan edit lands there"
                )
        elif disp == "mint":
            idx = entry.get("put")
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or not 0 <= idx < len(puts)
            ):
                raise BadInput(f"plan.sentences[{i}].put must index a plan put")
        elif disp == "drop":
            if entry.get("reason") not in COMPACT_DROP_REASONS:
                raise BadInput(
                    f"plan.sentences[{i}] drops need reason in "
                    f"{sorted(COMPACT_DROP_REASONS)} — a still-true claim moves, "
                    "it does not drop"
                )
        else:
            raise BadInput(
                f"plan.sentences[{i}].disposition must be keep|move|mint|drop"
            )


def _compact_body_row(conn: Any, ref_id: int) -> tuple[Any, ...]:
    rows = conn.execute(
        "SELECT chunk_id, text, ord FROM chunks WHERE ref_id = %s "
        "AND chunk_kind = 'memory_body' AND retired_at IS NULL AND ord >= 0 "
        "ORDER BY ord, chunk_id",
        (ref_id,),
    ).fetchall()
    if len(rows) != 1:
        raise MergeRefused("apply_refused", "compact edits require one live body chunk")
    return rows[0]


def _compact_edit_node(
    store: Store,
    conn: Any,
    handler: Any,
    ref_id: int,
    raw_ops: Any,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Snapshot one memory body, apply anchored ops, restore its routes.

    The same reversal shape ``undo_rewrite`` records: prior bytes, chunk
    identity, title, header and every link, plus the applied twins.
    """
    body = _compact_body_row(conn, ref_id)
    ops = _rewrite_ops(str(body[1]), raw_ops)
    links = _memory_links(conn, ref_id, int(body[0]))
    _guard_memory_link_measures(conn, links)
    header = _memory_header(conn, ref_id)
    _guard_legacy_memory_links(links)
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    rec: dict[str, Any] = {
        "ref": ref_id,
        "prior_body": str(body[1]),
        "prior_body_chunk_id": int(body[0]),
        "prior_body_ord": int(body[2]),
        "prior_title": ref.title,
        "prior_header": header,
        "links": links,
    }
    handler.edit(id=ref_id, ops=ops, reason=reason)
    new_body = conn.execute(
        "SELECT chunk_id FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
        (ref_id,),
    ).fetchone()
    if new_body is None:
        raise MergeRefused("apply_refused", "the rewritten memory body is missing")
    rec["applied_body_chunk_id"] = int(new_body[0])
    chunk_map = {rec["prior_body_chunk_id"]: rec["applied_body_chunk_id"]}
    if header is not None:
        new_header = _memory_header(conn, ref_id)
        if new_header is None:
            raise MergeRefused("apply_refused", "the rewritten header is missing")
        chunk_map[header["chunk_id"]] = new_header["chunk_id"]
    for link in links:
        restored = dict(link)
        for key in ("src_chunk_id", "dst_chunk_id"):
            restored[key] = chunk_map.get(restored[key], restored[key])
        _restore_memory_link(store, conn, restored, actor=actor)
    rec["applied_links"] = _memory_links(conn, ref_id, rec["applied_body_chunk_id"])
    state = store.trust_state("ref", ref_id, conn=conn)
    assert state is not None and state.sha is not None
    rec["applied_sha"] = state.sha
    return rec


def _apply_compact_put(
    store: Store,
    conn: Any,
    handler: Any,
    put: dict[str, Any],
    *,
    parents: list[int],
    review_id: int,
    actor: str,
) -> dict[str, Any]:
    """Mint one atomic child and hang it on the shell's hub.

    A direct insert rather than the handler ``put`` door: the ack carries
    no ``ref_id``, and minting on this transaction keeps the whole plan —
    edits, puts and the shell's fate — one undoable unit.
    """
    from precis.store.types import ChunkInsert, Tag

    ref = store.insert_ref(
        kind="memory",
        slug=None,
        title=put["title"],
        meta={"garden_compact": {"proposal": review_id}},
        conn=conn,
    )
    ref_id = int(ref.id)
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=0, text=put["body"], meta={"chunk_kind": "memory_body"})],
        conn=conn,
    )
    if put.get("section"):
        store.add_tag(ref_id, Tag.open(str(put["section"])), set_by="agent", conn=conn)
    marker = {"garden_compact": {"proposal": review_id}}
    for dst, relation in (
        [(p, "part-of") for p in parents]
        + [(q, "qualifies") for q in put["qualifies"]]
        + [(l["dst"], l["relation"]) for l in put["links"]]
    ):
        store.add_link(
            src_ref_id=ref_id,
            dst_ref_id=dst,
            relation=relation,
            set_by="agent",
            meta=marker,
            conn=conn,
        )
    # applied_sha/applied_links are stamped only after the shell's fate
    # runs: its delete can migrate edges onto a minted successor.
    return {"ref_id": ref_id}


def _compact_shell_retire(
    store: Store,
    conn: Any,
    handler: Any,
    target: int,
    garden: dict[str, Any],
    review_id: int,
    shell: dict[str, Any],
    puts: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """Retire the shell through the delete door; capture the same snapshot."""
    successor = shell.get("successor")
    if isinstance(successor, dict):
        successor = puts[int(successor["put"])]["ref_id"]
    rec = _retirement_snapshot(
        store, conn, target, garden, review_id, successor=successor
    )
    handler.delete(id=target, reason=reason, superseded_by=rec["successor_handle"])
    if successor is not None:
        store.add_link(
            src_ref_id=successor,
            dst_ref_id=target,
            relation="supersedes",
            set_by="agent",
            meta={"garden_compact": {"proposal": review_id}},
            conn=conn,
        )
    rec["applied_links"] = _retirement_links(conn, rec["scope"])
    prior = {link["link_id"]: link for link in rec["links"]}
    applied = {link["link_id"]: link for link in rec["applied_links"]}
    rewritten = [link for lid, link in prior.items() if applied.get(lid) != link]
    changed = rewritten + [
        link for lid, link in applied.items() if prior.get(lid) != link
    ]
    rec["content_endpoints"] = sorted(
        {*rec["scope"], *(l[k] for l in changed for k in ("src_ref_id", "dst_ref_id"))}
    )
    rec["endpoints"] = rec["content_endpoints"]
    chunk_ids = {l[k] for l in rewritten for k in ("src_chunk_id", "dst_chunk_id")}
    rec["chunks"] = [c for c in rec["chunks"] if c["chunk_id"] in chunk_ids]
    rec["applied_ends"] = _retirement_ends(
        conn, rec["endpoints"], content_ids=rec["content_endpoints"]
    )
    rec["applied_meta"] = conn.execute(
        "SELECT meta - 'garden_compact' FROM refs WHERE ref_id = %s",
        (target,),
    ).fetchone()[0]
    return {"fate": "retire", "retirement": rec}


def _compact_shell_keep(
    store: Store,
    conn: Any,
    handler: Any,
    target: int,
    shell: dict[str, Any],
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Reduce the shell to its live-state summary and re-arm its expiry."""
    ref = store.get_ref(kind="memory", id=target)
    assert ref is not None
    rec = _compact_edit_node(
        store,
        conn,
        handler,
        target,
        [{"find": _compact_body_row(conn, target)[1], "text": shell["summary"]}],
        actor=actor,
        reason=reason,
    )
    expires = (
        datetime.now(UTC) + timedelta(days=int(shell["expires_days"]))
    ).isoformat()
    store.stamp_ref_meta(target, {"expires": expires}, conn=conn)
    rec["prior_expires"] = (ref.meta or {}).get("expires")
    rec["expires"] = expires
    return {"fate": "keep", "edit": rec}


@trust_door("generated")
def apply_compact(
    store: Store,
    review_id: int,
    *,
    actor: str,
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a reviewed per-sentence compaction plan as one undoable unit.

    Edits apply on the pinned related nodes, atomic children mint under
    the shell's hub, and the shell retires through the delete door (with
    ``superseded_by`` → the hub or the main child) or keeps a ≤3-line
    summary with re-armed expiry — one ``meta.garden_compact`` record
    reverses the whole plan via ``precis garden undo <compact_id>``.
    """
    from precis.dispatch import Hub

    with store.atomic() as conn:
        target, garden, _body = _checked_memory_proposal(
            store, conn, review_id, "compact"
        )
        plan = _checked_compact_plan(store, conn, target, garden)
        parents = [
            int(v)
            for v in ((garden.get("evidence") or {}).get("related") or {}).get(
                "parents"
            )
            or []
            if isinstance(v, int)
        ]
        rec: dict[str, Any] = {
            "compact_id": f"garden-compact:{target}:{review_id}",
            "proposal": review_id,
            "target": target,
            "detector": garden["detector"],
            "edits": [],
            "puts": [],
            "shell": {},
        }
        _resolve_memory_apply(
            store,
            conn,
            review_id,
            actor=actor,
            model=model,
            garden=garden,
            applied=rec["compact_id"],
            verdict_extra=verdict_extra,
        )
        reason = f"garden:{garden['detector']}:{review_id}"
        handler = Hub(store=store).sibling("memory")
        with revision_context(reason, actor=actor, writer=_garden_writer(model)):
            for edit in plan["edits"]:
                rec["edits"].append(
                    _compact_edit_node(
                        store,
                        conn,
                        handler,
                        edit["ref"],
                        edit["ops"],
                        actor=actor,
                        reason=reason,
                    )
                )
            for put in plan["puts"]:
                rec["puts"].append(
                    _apply_compact_put(
                        store,
                        conn,
                        handler,
                        put,
                        parents=parents,
                        review_id=review_id,
                        actor=actor,
                    )
                )
            if plan["shell"]["fate"] == "retire":
                rec["shell"] = _compact_shell_retire(
                    store,
                    conn,
                    handler,
                    target,
                    garden,
                    review_id,
                    plan["shell"],
                    rec["puts"],
                    reason,
                )
            else:
                rec["shell"] = _compact_shell_keep(
                    store,
                    conn,
                    handler,
                    target,
                    plan["shell"],
                    actor=actor,
                    reason=reason,
                )
            # Post-mutation twins: the shell's delete migrates edges onto a
            # minted successor, so the child's applied state is only final
            # after the shell's fate has run.
            for put_rec in rec["puts"]:
                child = int(put_rec["ref_id"])
                sha_row = conn.execute(
                    "SELECT precis_target_sha('ref', %s)", (child,)
                ).fetchone()
                assert sha_row is not None
                put_rec["applied_sha"] = sha_row[0]
                put_rec["applied_links"] = _retirement_links(conn, [child])
        store.stamp_ref_meta(target, {"garden_compact": rec}, conn=conn)
    return rec


def _pending_compact_proposal(conn: Any, target: int) -> bool:
    """An undecided compact proposal on this memory — compact runs first."""
    row = conn.execute(
        "SELECT 1 FROM reviews p WHERE p.target_kind = 'ref' AND p.target_id = %s "
        "AND p.verdict = 'proposed' AND p.note LIKE %s AND p.note LIKE %s "
        "AND NOT EXISTS (SELECT 1 FROM reviews d WHERE d.note LIKE %s "
        "AND d.note ~ ('\"resolves\": ' || p.review_id || '[^0-9]')) LIMIT 1",
        (target, '%"garden"%', '%"action": "compact"%', '%"resolves"%'),
    ).fetchone()
    return row is not None


def _compact_first(store: Store, conn: Any, target: int) -> None:
    """The compact-first gate both retirement doors share (gr561025).

    A retirement may not apply while a compact proposal is pending on the
    node, or while the compact detector still fires on a node whose last
    completed compaction does not cover its current content. Only an
    applied ``garden_compact`` record is completed preservation — a
    rejected plan decided a question, it preserved nothing.
    """
    from precis.graph.garden import candidates

    if _pending_compact_proposal(conn, target):
        raise MergeRefused(
            "apply_refused",
            "a pending compaction decides this memory before it retires",
            next=f"precis garden proposals --action compact (me{target})",
        )
    fresh = candidates.compact_candidates(
        store, scan_cap=1, limit=1, cursors={"compact:memory": target - 1}
    )
    if not fresh or fresh[0].target_id != target:
        return
    row = conn.execute(
        "SELECT meta->'garden_compact'->'shell'->'edit'->>'applied_sha', "
        "precis_target_sha('ref', %s) FROM refs WHERE ref_id = %s",
        (target, target),
    ).fetchone()
    if row is not None and row[0] is not None and row[0] == row[1]:
        return  # a completed compaction already preserved this content
    raise MergeRefused(
        "compact_first",
        "this memory owes a per-sentence compaction plan before retiring",
        next="run the memory garden pass to file the compact proposal",
    )


def _restore_compact_body(
    store: Store, conn: Any, rec: dict[str, Any], *, actor: str, reason: str
) -> dict[str, int]:
    """Restore one edited-memory snapshot; drift refuses the whole undo."""
    target = int(rec["ref"])
    links = _memory_links(conn, target, int(rec["applied_body_chunk_id"]))
    _guard_memory_link_measures(conn, links)
    state = store.trust_state("ref", target, conn=conn)
    if (
        state is None
        or state.sha != rec["applied_sha"]
        or links != rec["applied_links"]
    ):
        raise BadInput(
            "a compacted memory or its links changed; undo is unsafe",
            next=f"inspect me{target} and its garden_compact snapshot; "
            "reconcile changed endpoints before retrying garden undo",
        )
    prior_links = {link["link_id"]: link for link in rec["links"]}
    deleted = store.delete_links(
        [l["link_id"] for l in links if prior_links.get(l["link_id"]) != l],
        conn=conn,
    )
    store.chunks.replace_body_chunk(
        target,
        rec["prior_body"],
        chunk_kind="memory_body",
        source=actor,
        reason=reason,
        conn=conn,
    )
    conn.execute(
        "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
        (target,),
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, text) "
        "VALUES (%s, %s, %s, 'memory_body', %s)",
        (
            rec["prior_body_chunk_id"],
            target,
            rec["prior_body_ord"],
            rec["prior_body"],
        ),
    )
    title = conn.execute(
        "SELECT title FROM refs WHERE ref_id = %s", (target,)
    ).fetchone()[0]
    if title != rec["prior_title"]:
        store.chunks.set_ref_title(target, rec["prior_title"], source=actor, conn=conn)
        header = rec.get("prior_header")
        if header is None:
            store.chunks.sync_header_card(target, conn=conn)
        else:
            conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord = -1", (target,))
            conn.execute(
                "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, "
                "text, meta, set_by, created_at) VALUES "
                "(%(chunk_id)s, %(ref_id)s, %(ord)s, %(chunk_kind)s, "
                "%(text)s, %(meta)s, %(set_by)s, %(created_at)s)",
                {**header, "meta": Jsonb(header["meta"])},
            )
    for link in rec["links"]:
        _restore_memory_link(store, conn, link, actor=actor)
    return {"restored": len(rec["links"]), "deleted": deleted}


def _restore_compact_retirement(
    store: Store, conn: Any, rec: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    """Un-retire the compact shell, the same drift contract as undo_retirement."""
    target = int(rec["target"])
    ends = _retirement_ends(
        conn, rec["endpoints"], content_ids=rec["content_endpoints"]
    )
    links = _retirement_links(conn, rec["scope"])
    prior = {l["link_id"]: l for l in rec["links"]}
    applied = {l["link_id"]: l for l in rec["applied_links"]}
    rewritten = [l for lid, l in prior.items() if applied.get(lid) != l]
    removed = [l for lid, l in applied.items() if prior.get(lid) != l]
    touched = {l["link_id"] for l in rewritten + removed}
    current = {l["link_id"]: l for l in links}
    edge_keys = (
        "src_ref_id",
        "src_chunk_id",
        "dst_ref_id",
        "dst_chunk_id",
        "relation",
    )
    restored_edges = {tuple(l[k] for k in edge_keys) for l in rewritten}
    meta = conn.execute(
        "SELECT meta - 'garden_compact' FROM refs WHERE ref_id = %s", (target,)
    ).fetchone()[0]
    if (
        ends != rec["applied_ends"]
        or any(current.get(lid) != applied.get(lid) for lid in touched)
        or any(
            l["link_id"] not in touched
            and tuple(l[k] for k in edge_keys) in restored_edges
            for l in links
        )
        or meta != rec["applied_meta"]
        or _retirement_chunks(conn, rewritten) != rec["chunks"]
    ):
        raise BadInput(
            "retired memory, successor or link end changed; undo is unsafe",
            next=f"inspect me{target} and its garden_compact snapshot; "
            "reconcile changed endpoints before retrying garden undo",
        )
    _guard_memory_link_measures(conn, removed)
    deleted = store.delete_links([l["link_id"] for l in removed], conn=conn)
    restamped: list[int] = []
    store.restore_ref(target, conn=conn)
    conn.execute(
        "UPDATE refs SET meta = %s WHERE ref_id = %s",
        (Jsonb(rec["prior_meta"]), target),
    )
    for link in rewritten:
        if _restore_memory_link(store, conn, link, actor=actor):
            restamped.append(link["link_id"])
    return {
        "restored": len(rewritten),
        "deleted": deleted,
        "restamped": restamped,
    }


@trust_door("generated")
def undo_compact(store: Store, compact_id: str, *, actor: str) -> dict[str, int]:
    """Reverse one applied compaction as a unit.

    Every edited related body and the kept or retired shell restore
    byte-for-byte; each minted child retires; the restored key rejects so
    the next tick cannot replay the plan.
    """
    from precis.dispatch import Hub
    from precis.graph.garden.proposals import GARDEN_VERSION

    with store.atomic() as conn:
        row = conn.execute(
            "SELECT ref_id, meta->'garden_compact' FROM refs "
            "WHERE meta->'garden_compact'->>'compact_id' = %s FOR UPDATE",
            (compact_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no compact record for compact_id={compact_id!r}")
        target, rec = int(row[0]), row[1]
        for put in rec["puts"]:
            child = int(put["ref_id"])
            crow = conn.execute(
                "SELECT retired_at IS NULL, precis_target_sha('ref', ref_id) "
                "FROM refs WHERE ref_id = %s FOR UPDATE",
                (child,),
            ).fetchone()
            if (
                crow is None
                or not crow[0]
                or crow[1] != put["applied_sha"]
                or _retirement_links(conn, [child]) != put["applied_links"]
            ):
                raise BadInput(
                    "a minted compact child changed or retired; undo is unsafe",
                    next=f"inspect me{child} and the garden_compact snapshot on "
                    f"me{target}; reconcile before retrying garden undo",
                )
        reason = f"garden:undo:{compact_id}"
        counts = {
            "edits_restored": 0,
            "puts_retired": 0,
            "links_deleted": 0,
            "links_restamped": 0,
        }
        with revision_context(reason, actor=actor):
            apply_revision_context(conn, clear=True)
            # Dependent mutations reverse in apply's reverse order: the
            # shell's retirement pins its endpoints' post-apply shas —
            # including any node the plan edited and retired into — so it
            # un-retires first, edits restore second, and the minted
            # children retire last (their edges may be the successor
            # twins the retirement undo expects to find unchanged).
            shell = rec["shell"]
            if shell.get("fate") == "retire":
                ret = _restore_compact_retirement(
                    store, conn, shell["retirement"], actor=actor
                )
                counts["links_deleted"] += ret["deleted"]
                counts["links_restamped"] += len(ret["restamped"])
            else:
                out = _restore_compact_body(
                    store, conn, shell["edit"], actor=actor, reason=reason
                )
                counts["edits_restored"] += 1
                counts["links_deleted"] += out["deleted"]
                prior_expires = shell["edit"].get("prior_expires")
                if prior_expires is None:
                    conn.execute(
                        "UPDATE refs SET meta = meta - 'expires' WHERE ref_id = %s",
                        (target,),
                    )
                else:
                    store.stamp_ref_meta(target, {"expires": prior_expires}, conn=conn)
            for edit in rec["edits"]:
                out = _restore_compact_body(
                    store, conn, edit, actor=actor, reason=reason
                )
                counts["edits_restored"] += 1
                counts["links_deleted"] += out["deleted"]
            handler = Hub(store=store).sibling("memory")
            for put in rec["puts"]:
                store.delete_links(
                    [l["link_id"] for l in put["applied_links"]], conn=conn
                )
                handler.delete(
                    id=int(put["ref_id"]),
                    reason=reason,
                )
                counts["puts_retired"] += 1
            if shell.get("fate") != "retire":
                conn.execute(
                    "UPDATE refs SET meta = meta - 'garden_compact' WHERE ref_id = %s",
                    (target,),
                )
        proposal = conn.execute(
            "SELECT note FROM reviews WHERE review_id = %s", (rec["proposal"],)
        ).fetchone()
        if proposal is not None:
            garden = (json.loads(proposal[0] or "{}") or {}).get("garden") or {}
            if garden.get("key"):
                store.record_target_review(
                    "ref",
                    target,
                    actor=actor,
                    verdict="rejected",
                    version=GARDEN_VERSION,
                    note=json.dumps(
                        {
                            "garden": {
                                "v": 1,
                                "key": garden["key"],
                                "resolves": rec["proposal"],
                                "undone": compact_id,
                                "reason": "undone",
                            }
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    conn=conn,
                )
    return counts


# ── reviewed memory split (gr561019) ─────────────────────────────────────

#: The section types a split child may carry — a trap is gotchas, a fact
#: reference, a ruling rulings; the evaluator picks per child.
SPLIT_CHILD_SECTIONS = frozenset(
    {"gotchas", "reference", "rulings", "runbooks", "workflow"}
)
SPLIT_MAX_CHILDREN = 12
SPLIT_TITLE_MAX = 80
SPLIT_KEEP_MAX_LINES = 3


def _check_split_scope(payload: dict[str, Any], fresh: dict[str, Any]) -> None:
    """The evaluator proposes children; fresh detector spans own the scope.

    Every child span must be one of the fresh detector's sentence spans and
    join exactly one child — the model supplies titles, sections, links and
    the keep summary, never new body text or expanded coverage.
    """
    children = payload.get("children")
    if (
        not isinstance(children, list)
        or not 2 <= len(children) <= SPLIT_MAX_CHILDREN
        or not all(isinstance(c, dict) for c in children)
    ):
        raise BadInput(
            f"split requires 2..{SPLIT_MAX_CHILDREN} children "
            "(payload_overrides.children)"
        )
    allowed = {
        tuple(s["span"])
        for s in (fresh.get("sentences") or [])
        if isinstance(s, dict) and isinstance(s.get("span"), list)
    }
    if not allowed:
        raise BadInput("split requires the detector's sentence-span evidence")
    used: set[tuple[int, int]] = set()
    for child in children:
        title = child.get("title")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > SPLIT_TITLE_MAX
        ):
            raise BadInput(
                f"split child title must be a nonempty ≤{SPLIT_TITLE_MAX}-char "
                "claim (the sūtra)"
            )
        if child.get("section") not in SPLIT_CHILD_SECTIONS:
            raise BadInput(
                f"split child section must be one of {sorted(SPLIT_CHILD_SECTIONS)}"
            )
        spans = child.get("spans")
        if not isinstance(spans, list) or not spans:
            raise BadInput("split child needs ≥1 detector sentence span")
        for span in spans:
            key = tuple(span) if isinstance(span, list) and len(span) == 2 else None
            if key is None or key not in allowed:
                raise BadInput(
                    "split scope rule: every span must match a fresh detector "
                    "sentence span"
                )
            if key in used:
                raise BadInput("a sentence span joins exactly one child")
            used.add(key)
        for extra in ("links", "qualifies"):
            if child.get(extra) is not None and not isinstance(child[extra], list):
                raise BadInput(f"split child {extra} must be a list")
    fate = payload.get("parent_fate") or "retire"
    if fate not in ("retire", "keep"):
        raise BadInput("parent_fate must be retire or keep")
    if fate == "keep":
        keep = payload.get("keep_body")
        if (
            not isinstance(keep, str)
            or not keep.strip()
            or sum(1 for line in keep.splitlines() if line.strip())
            > SPLIT_KEEP_MAX_LINES
        ):
            raise BadInput(f"keep_body must be a ≤{SPLIT_KEEP_MAX_LINES}-line summary")
    hub = payload.get("hub")
    if hub is not None and not isinstance(hub, int):
        from precis.utils.handle_registry import parse

        parsed = parse(str(hub))
        if parsed is None or parsed[:2] != ("memory", False):
            raise BadInput("split hub must be a memory id or me<N> handle")


@dataclass(slots=True)
class SplitRecord:
    """Undo data on the parent's ``meta.garden_split`` (fate ``keep``)."""

    split_id: str
    proposal: int
    target: int
    parent_fate: str
    children: list[dict[str, Any]] = field(default_factory=list)
    hub: int | None = None
    prior_sha: str = ""
    prior_body_chunk_id: int = 0
    prior_body: str = ""
    prior_title: str = ""
    prior_body_ord: int = 0
    prior_header: dict[str, Any] | None = None
    links: list[dict[str, Any]] = field(default_factory=list)
    applied_sha: str = ""
    applied_body_chunk_id: int = 0
    applied_links: list[dict[str, Any]] = field(default_factory=list)


def _is_index(conn: Any, ref_id: int) -> bool:
    """Whether ``ref_id`` carries the ``section:index`` hub tag."""
    return (
        conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' "
            "AND t.value = 'section:index'",
            (ref_id,),
        ).fetchone()
        is not None
    )


def _index_ancestor(conn: Any, target: int) -> int | None:
    """The nearest live ``section:index`` ancestor along the ``part-of``
    chain (``contains`` is the same membership stored inverted), or None.
    Intermediate parents must be live; the chain is depth-capped like
    :func:`precis.utils.memory_hubs.hubs_of`."""
    row = conn.execute(
        "WITH RECURSIVE edges(child, parent) AS ("
        "  SELECT src_ref_id, dst_ref_id FROM links WHERE relation = 'part-of' "
        "  UNION SELECT dst_ref_id, src_ref_id FROM links "
        "  WHERE relation = 'contains'), "
        "up(ref_id, depth) AS ("
        "  SELECT e.parent, 1 FROM edges e JOIN refs r ON r.ref_id = e.parent "
        "  WHERE e.child = %(t)s AND r.retired_at IS NULL "
        "  UNION SELECT e.parent, u.depth + 1 FROM up u "
        "  JOIN edges e ON e.child = u.ref_id "
        "  JOIN refs r ON r.ref_id = e.parent AND r.retired_at IS NULL "
        "  WHERE u.depth < 8) "
        "SELECT u.ref_id FROM up u JOIN refs r ON r.ref_id = u.ref_id "
        "WHERE r.kind = 'memory' AND EXISTS ("
        "  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "  WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
        "  AND t.value = 'section:index') "
        "ORDER BY u.depth, u.ref_id LIMIT 1",
        {"t": target},
    ).fetchone()
    return int(row[0]) if row is not None else None


def _split_hub(
    payload: dict[str, Any], target: int, fate: str, *, conn: Any
) -> int | None:
    """Resolve the children's part-of parent (the hub, never the parent).

    An explicit ``payload.hub`` must be a live memory. Without one the
    children hang on the target's ``section:index`` ancestry — a kept hub
    stays its own hub, anything else resolves to the nearest live index
    ancestor. The split parent is never a substitute hub: hanging
    children on it would violate the one-hub-parent shape.
    """
    from precis.utils.handle_registry import parse

    raw = payload.get("hub")
    if raw is None:
        if fate == "keep" and _is_index(conn, target):
            return target
        rid = _index_ancestor(conn, target)
        if rid is None:
            raise MergeRefused(
                "apply_refused",
                "the split names no hub and no live section:index ancestor "
                "reaches the target; name a hub",
            )
        return rid
    rid = raw if isinstance(raw, int) else (parse(str(raw)) or (None, None, -1))[2]
    if not isinstance(rid, int) or rid <= 0:
        raise BadInput("split hub must be a memory id or me<N> handle")
    if rid == target:
        raise BadInput("split hub cannot be the split target")
    row = conn.execute(
        "SELECT kind, retired_at FROM refs WHERE ref_id = %s", (rid,)
    ).fetchone()
    if row is None or str(row[0]) != "memory" or row[1] is not None:
        raise MergeRefused("target_gone", "the split hub is not a live memory")
    return rid


def _split_link_target(store: Store, raw: Any) -> tuple[int, int | None]:
    from precis.utils.link_target import _parse_link_target

    target = _parse_link_target(
        f"me{raw}" if isinstance(raw, int) else str(raw), store=store
    )
    return int(target.ref_id), target.pos


def _mint_split_child(
    store: Store,
    conn: Any,
    handler: Any,
    rec: SplitRecord,
    child: dict[str, Any],
    body: str,
    space_tags: list[str],
    *,
    actor: str,
    model: str | None,
) -> int:
    """Mint one atomic child through the memory create door, then its edges."""
    from precis.utils.relations import validate_relation

    text = "\n".join(body[s:e] for s, e in child["spans"])
    _resp, cid = handler._create_with_id(
        text=text,
        tags=[f"section:{child['section']}", *space_tags],
        link=None,
        put_meta={"title": str(child["title"]).strip()},
        set_by="agent",
        system_meta={
            "garden_split_child": {"split_id": rec.split_id, "source": rec.target}
        },
        default_tags=False,
    )
    if rec.hub is not None:
        store.add_link(
            src_ref_id=cid,
            dst_ref_id=rec.hub,
            relation="part-of",
            set_by="agent",
            conn=conn,
        )
    for spec in child.get("links") or []:
        dst, pos = _split_link_target(store, spec.get("target"))
        store.add_link(
            src_ref_id=cid,
            dst_ref_id=dst,
            dst_pos=pos,
            relation=validate_relation(spec.get("relation"), store=store),
            set_by="agent",
            conn=conn,
        )
    for raw in child.get("qualifies") or []:
        dst, pos = _split_link_target(store, raw)
        store.add_link(
            src_ref_id=cid,
            dst_ref_id=dst,
            dst_pos=pos,
            relation="qualifies",
            set_by="agent",
            conn=conn,
        )
    state = store.trust_state("ref", cid, conn=conn)
    assert state is not None and state.sha is not None
    store.record_trust_act(
        TrustAct(
            action="consider",
            target_kind="ref",
            target_id=cid,
            expected_sha=state.sha,
            reason=(
                f"kg-review:{_review_model_token(model)}:"
                f"{datetime.now(UTC):%Y-%m-%d}:ok"
            ),
        ),
        actor=actor,
        actor_type="agent",
        model=model,
    )
    rec.children.append(
        {"id": cid, "title": child["title"], "spans": child["spans"], "body": text}
    )
    return cid


def _split_child_pins(
    store: Store, conn: Any, child_ids: list[int]
) -> list[dict[str, Any]]:
    """Undo pins for the minted children: each child's trust-subject sha
    and every link row touching it — exactly what undo may delete."""
    pins = []
    for cid in child_ids:
        state = store.trust_state("ref", cid, conn=conn)
        assert state is not None and state.sha is not None
        pins.append(
            {"id": cid, "sha": state.sha, "links": _retirement_links(conn, [cid])}
        )
    return pins


def _retire_split_children(store: Store, conn: Any, pins: list[dict[str, Any]]) -> int:
    """Undo helper: retire the minted children and drop their edges.

    Every child must still match its apply-time pin — trust-subject sha
    and the full link set. An edited, retired or relinked child carries
    intervening work a blind delete would erase, so undo refuses."""
    for pin in pins:
        cid = int(pin["id"])
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id = %s FOR UPDATE", (cid,)
        ).fetchone()
        if row is None:
            raise MergeRefused("target_gone", f"split child me{cid} is gone")
        state = store.trust_state("ref", cid, conn=conn)
        links = _retirement_links(conn, [cid])
        if (
            row[0] is not None
            or not isinstance(pin.get("sha"), str)
            or not isinstance(pin.get("links"), list)
            or state is None
            or state.sha != pin["sha"]
            or links != pin["links"]
        ):
            raise MergeRefused(
                "stale",
                f"split child me{cid} changed after the split; undo is unsafe",
                next=f"get(kind='memory', id='me{cid}')",
            )
        _guard_memory_link_measures(conn, links)
    for pin in pins:
        cid = int(pin["id"])
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
            (cid, cid),
        )
        store.retire_ref(cid, conn=conn)
    return len(pins)


@trust_door("generated")
def apply_split(
    store: Store,
    review_id: int,
    *,
    actor: str,
    model: str | None = None,
    verdict_extra: dict[str, Any] | None = None,
) -> SplitRecord:
    """Apply a reviewed grab-bag split: mint atomic children, then retire or
    shrink the parent — all one transaction (gr561019).

    ``retire`` (default): the parent's links migrate to the hub through the
    memory delete door, its tombstone carries ``meta.split_into`` plus the
    ``garden_retirement`` snapshot, and ``undo`` restores the node exactly.
    ``keep``: the parent body shrinks to the reviewed ≤3-line summary and the
    ``meta.garden_split`` record holds prior bytes and links for undo.
    """
    from precis.dispatch import Hub

    with store.atomic() as conn:
        target, garden, body = _checked_memory_proposal(store, conn, review_id, "split")
        payload = garden["payload"]
        children = payload["children"]
        fate = payload.get("parent_fate") or "retire"
        body_text = str(body[1])
        links = _memory_links(conn, target, int(body[0]))
        _guard_memory_link_measures(conn, links)
        header = _memory_header(conn, target)
        _guard_legacy_memory_links(links)
        ref = store.get_ref(kind="memory", id=target)
        assert ref is not None
        rec = SplitRecord(
            split_id=f"garden-split:{target}:{review_id}",
            proposal=review_id,
            target=target,
            parent_fate=fate,
            prior_sha=payload["base_sha"],
            prior_body_chunk_id=int(body[0]),
            prior_body=body_text,
            prior_title=ref.title,
            prior_body_ord=int(body[2]),
            prior_header=header,
            links=links,
        )
        rec.hub = _split_hub(payload, target, fate, conn=conn)
        _resolve_memory_apply(
            store,
            conn,
            review_id,
            actor=actor,
            model=model,
            garden=garden,
            applied=rec.split_id,
            verdict_extra=verdict_extra,
        )
        handler = Hub(store=store).sibling("memory")
        space_tags = [
            t.value
            for t in store.tags_for(target)
            if t.namespace == "open" and t.value.startswith("space:")
        ]
        reason = f"garden:{garden['detector']}:{review_id}"
        with revision_context(
            reason, actor=actor, model="", writer=_garden_writer(model)
        ):
            for child in children:
                _mint_split_child(
                    store,
                    conn,
                    handler,
                    rec,
                    child,
                    body_text,
                    space_tags,
                    actor=actor,
                    model=model,
                )
            child_ids = [c["id"] for c in rec.children]
            if fate == "retire":
                assert rec.hub is not None
                ret = _retirement_snapshot(
                    store, conn, target, garden, review_id, successor=rec.hub
                )
                ret["children"] = child_ids
                ret["parent_fate"] = "retire"
                store.stamp_ref_meta(target, {"split_into": child_ids}, conn=conn)
                handler._delete(id=target, reason=reason, superseded_by=f"me{rec.hub}")
                _save_retirement(store, conn, ret)
                # Written after the delete migrates links, so the provenance
                # edge keeps pointing at the tombstone, not the hub.
                for cid in child_ids:
                    store.add_link(
                        src_ref_id=cid,
                        dst_ref_id=target,
                        relation="derived-from",
                        set_by="agent",
                        conn=conn,
                    )
                # The pins include the derived-from edges, so they stamp
                # after _save_retirement; the merge rewrites the key whole.
                ret["child_pins"] = _split_child_pins(store, conn, child_ids)
                store.stamp_ref_meta(target, {"garden_retirement": ret}, conn=conn)
                return rec
            for cid in child_ids:
                store.add_link(
                    src_ref_id=cid,
                    dst_ref_id=target,
                    relation="derived-from",
                    set_by="agent",
                    conn=conn,
                )
            for child, pin in zip(
                rec.children,
                _split_child_pins(store, conn, child_ids),
                strict=True,
            ):
                child.update(pin)
            ops = _rewrite_ops(
                body_text,
                [
                    {
                        "find": body_text,
                        "text": str(payload["keep_body"]).strip(),
                        "span": [0, len(body_text)],
                    }
                ],
            )
            handler.edit(id=target, ops=ops, reason=reason)
            new_body = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'memory_body'",
                (target,),
            ).fetchone()
            if new_body is None:
                raise MergeRefused(
                    "apply_refused", "the shrunken memory body is missing"
                )
            rec.applied_body_chunk_id = int(new_body[0])
            rec.applied_links = _memory_links(conn, target, rec.applied_body_chunk_id)
            state = store.trust_state("ref", target, conn=conn)
            assert state is not None and state.sha is not None
            rec.applied_sha = state.sha
            store.stamp_ref_meta(target, {"garden_split": asdict(rec)}, conn=conn)
            store.record_trust_act(
                TrustAct(
                    action="consider",
                    target_kind="ref",
                    target_id=target,
                    expected_sha=state.sha,
                    reason=(
                        f"kg-review:{_review_model_token(model)}:"
                        f"{datetime.now(UTC):%Y-%m-%d}:fixed"
                    ),
                ),
                actor=actor,
                actor_type="agent",
                model=model,
            )
    return rec


@trust_door("generated")
def undo_split(store: Store, split_id: str, *, actor: str) -> dict[str, int]:
    """Reverse an applied split: retire the minted children, restore the parent.

    A ``retire`` fate delegates the node/link restoration to
    :func:`undo_retirement` on the embedded snapshot; a ``keep`` fate replays
    the saved body chunk and link set like :func:`undo_rewrite`. Both write
    the terminal ``undone`` rejection on the proposal key. The children are
    pinned at apply time (content sha plus every link row) and undo refuses
    when one changed, retired or relinked since — an intervening edit is
    never deleted.
    """
    from precis.graph.garden.proposals import GARDEN_VERSION

    with store.atomic() as conn:
        row = conn.execute(
            "SELECT ref_id, meta->'garden_split', title, retired_at FROM refs "
            "WHERE meta->'garden_split'->>'split_id' = %s FOR UPDATE",
            (split_id,),
        ).fetchone()
        if row is not None:
            target, rec, title = int(row[0]), dict(row[1]), str(row[2])
            if rec["parent_fate"] == "retire":
                raise BadInput(
                    "garden_split record on a retired parent is inconsistent",
                    next=f"precis garden undo {split_id}",
                )
        else:
            tomb = conn.execute(
                "SELECT ref_id, meta->'garden_retirement' FROM refs "
                "WHERE meta->'garden_retirement'->>'retirement_id' = %s "
                "FOR UPDATE",
                (split_id,),
            ).fetchone()
            if tomb is None:
                raise NotFound(f"no split record for split_id={split_id!r}")
            if tomb[1].get("parent_fate") != "retire":
                raise NotFound(f"no split record for split_id={split_id!r}")
            child_pins = tomb[1].get("child_pins")
            if not isinstance(child_pins, list):
                raise MergeRefused(
                    "stale",
                    "the split record carries no child pins; "
                    "undo cannot verify the children are untouched",
                    next=f"inspect me{int(tomb[0])}'s garden_retirement record",
                )
            deleted_children = _retire_split_children(store, conn, child_pins)
            counts = undo_retirement(store, split_id, actor=actor)
            return {
                "children_deleted": deleted_children,
                "links_restored": counts["links_restored"],
                "links_deleted": counts["links_deleted"],
                "links_restamped": counts["links_restamped"],
            }
        # fate == "keep": replay the saved body/links like undo_rewrite.
        state = store.trust_state("ref", target, conn=conn)
        links = _memory_links(conn, target, rec["applied_body_chunk_id"])
        _guard_memory_link_measures(conn, links)
        if (
            state is None
            or state.sha != rec["applied_sha"]
            or links != rec["applied_links"]
        ):
            raise MergeRefused("stale", "memory or links changed after the split")
        deleted_children = _retire_split_children(store, conn, rec["children"])
        prior_links = {link["link_id"]: link for link in rec["links"]}
        deleted = store.delete_links(
            [l["link_id"] for l in links if prior_links.get(l["link_id"]) != l],
            conn=conn,
        )
        reason = f"garden:undo:{split_id}"
        with revision_context(reason, actor=actor):
            apply_revision_context(conn, clear=True)
            store.chunks.replace_body_chunk(
                target,
                rec["prior_body"],
                chunk_kind="memory_body",
                source=actor,
                reason=reason,
                conn=conn,
            )
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
                (target,),
            )
            conn.execute(
                "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, %s, 'memory_body', %s)",
                (
                    rec["prior_body_chunk_id"],
                    target,
                    rec["prior_body_ord"],
                    rec["prior_body"],
                ),
            )
            if title != rec["prior_title"]:
                store.chunks.set_ref_title(
                    target, rec["prior_title"], source=actor, conn=conn
                )
                header = rec.get("prior_header")
                if header is None:
                    store.chunks.sync_header_card(target, conn=conn)
                else:
                    conn.execute(
                        "DELETE FROM chunks WHERE ref_id = %s AND ord = -1",
                        (target,),
                    )
                    conn.execute(
                        "INSERT INTO chunks (chunk_id, ref_id, ord, chunk_kind, "
                        "text, meta, set_by, created_at) VALUES "
                        "(%(chunk_id)s, %(ref_id)s, %(ord)s, %(chunk_kind)s, "
                        "%(text)s, %(meta)s, %(set_by)s, %(created_at)s)",
                        {**header, "meta": Jsonb(header["meta"])},
                    )
            for link in rec["links"]:
                _restore_memory_link(store, conn, link, actor=actor)
            conn.execute(
                "UPDATE refs SET meta = meta - 'garden_split' WHERE ref_id = %s",
                (target,),
            )
        proposal = conn.execute(
            "SELECT note FROM reviews WHERE review_id = %s", (rec["proposal"],)
        ).fetchone()
        if proposal is not None:
            garden = (json.loads(proposal[0] or "{}") or {}).get("garden") or {}
            if garden.get("key"):
                store.record_target_review(
                    "ref",
                    target,
                    actor=actor,
                    verdict="rejected",
                    version=GARDEN_VERSION,
                    note=json.dumps(
                        {
                            "garden": {
                                "v": 1,
                                "key": garden["key"],
                                "resolves": rec["proposal"],
                                "undone": split_id,
                                "reason": "undone",
                            }
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    conn=conn,
                )
    return {
        "children_deleted": deleted_children,
        "links_restored": len(rec["links"]),
        "links_deleted": deleted,
    }


__all__ = [
    "META_MERGE",
    "META_PRIOR_REF",
    "MergeRecord",
    "MergeRefused",
    "RelinkRecord",
    "RewriteRecord",
    "SplitRecord",
    "apply_compact",
    "apply_merge",
    "apply_relink",
    "apply_retire",
    "apply_rewrite",
    "apply_split",
    "apply_structural",
    "apply_supersede",
    "compact_plan_shape",
    "undo_compact",
    "undo_merge",
    "undo_relink",
    "undo_retirement",
    "undo_rewrite",
    "undo_split",
]
