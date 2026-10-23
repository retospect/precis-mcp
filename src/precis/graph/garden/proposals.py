"""Garden proposals — the reviews-ledger write and read side.

A proposal is a ``reviews`` row (migration 0185) with
``verdict='proposed'``, ``actor='graph-garden'`` and a JSON ``note``
payload::

    {"garden": {"v": 1, "key": "<action>:<kind>:<id>:<other>",
                "action": "merge|split|relink|prune|tag",
                "detector": "…", "title": "…",
                "evidence": {...}, "payload": {...},
                "rationale": "…"}}

``verdict='proposed'`` + the ``garden`` key make the row
distinguishable from an approved/rejected review (and from other
actors' proposed rows, e.g. per-chunk reviews). The row's
``content_sha`` pins the target's state at proposal time — an edit to
the target makes the proposal non-current without touching the row,
which is the review surface's "stale" signal.

**Dedup** is by ``key``: a candidate whose key already has a pending
(``proposed``) garden row is not re-proposed, and neither is a key whose
newest verdict is ``rejected`` while the target's sha is unchanged. An
``approved`` verdict, or an edit to the target after a rejection,
re-opens the key — the graph moved on, so the question is legitimately
open again.

**Proposer rejections are rows too** (gr506046). A candidate the
proposer turns down gets a ``rejected`` graph-garden row
(:func:`record_rejection`, ``note.garden.proposer_reject``) stamped with
the proposer's prompt version. Without it the same unchanged candidates
were re-asked every run: on prod 2026-10-22/23 one taxon pair took 16
calls and each run spent 16-20 calls on rejects it had already made. The
row blocks the key while the target is unchanged *and* the prompt
version matches — a new prompt is a new judge, so its first run
re-asks. These rows are not decisions on a proposal: the pending view
skips them and only the runner's :func:`key_block` honours them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.graph.garden.candidates import GardenCandidate

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The reviews.actor value every garden row carries — the discriminator
#: the read view filters on (and the "who" an auditor sees).
GARDEN_ACTOR = "graph-garden"

#: The payload schema version, stamped into ``reviews.version`` and
#: ``note.garden.v`` — bump when the JSON shape changes so old rows stay
#: readable.
GARDEN_VERSION = "garden.v1"


@dataclass(frozen=True, slots=True)
class GardenProposal:
    """One ``reviews`` row decoded as a garden proposal."""

    review_id: int
    target_kind: str
    target_id: int
    action: str
    key: str
    detector: str
    title: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    rationale: str
    model: str | None
    at: Any
    current: bool


def _parse_note(note: str | None) -> dict[str, Any] | None:
    """The ``note.garden`` payload of a review row, or None for a
    non-garden/unparseable note."""
    if not note:
        return None
    try:
        body = json.loads(note)
    except (TypeError, ValueError):
        return None
    g = body.get("garden")
    return g if isinstance(g, dict) else None


def key_block(
    store: Store,
    target_kind: str,
    target_id: int,
    key: str,
    *,
    prompt_version: str | None = None,
) -> str | None:
    """Why ``key`` may not be proposed now: ``'pending'``, ``'rejected'``,
    or ``None`` when the slot is open.

    reviews_for is newest-first: the FIRST row carrying this garden key
    decides. A pending (``proposed``) row blocks. A ``rejected`` row blocks
    while the target is unchanged — its ``content_sha`` still matches the
    target's sha now (gr504843: the same unchanged candidate came back
    every tick). An edit to the target, or an ``approved`` verdict,
    re-opens the slot. No actor filter: the terminal verdict belongs to
    whoever resolved it, not to graph-garden.

    A proposer rejection (:func:`record_rejection`) blocks only a caller
    naming the same ``prompt_version`` — the runner, asking whether to
    re-ask. Without one (the emit path, after the runner decided) it
    does not block."""
    for rev in store.reviews_for(target_kind, target_id):
        g = _parse_note(rev.note)
        if g is None or g.get("key") != key:
            continue
        if rev.verdict == "proposed":
            return "pending"
        if rev.verdict == "rejected" and rev.current:
            if g.get("proposer_reject") and g.get("prompt") != prompt_version:
                return None
            return "rejected"
        return None
    return None


def _key_pending(store: Store, target_kind: str, target_id: int, key: str) -> bool:
    return key_block(store, target_kind, target_id, key) is not None


def proposal_key_pending(store: Store, candidate: GardenCandidate) -> bool:
    """True when this candidate's key may not be proposed: a pending
    garden row carries it, or its newest verdict is a rejection of the
    target's current content (dedup — re-minting must not pile up
    identical proposals or re-ask a decided question)."""
    return _key_pending(
        store, candidate.target_kind, candidate.target_id, candidate.key
    )


def emit_proposal(
    store: Store,
    candidate: GardenCandidate,
    *,
    rationale: str,
    model: str | None = None,
    key: str | None = None,
    conn: Any = None,
) -> int | None:
    """Write one ``verdict='proposed'`` reviews row for ``candidate``.

    Returns the new ``review_id``, or ``None`` when an identical pending
    proposal already exists (the idempotency half of "bounded per tick,
    idempotent idem_key" — the ledger, not the job, carries the dedup so
    re-running the same window over an unchanged graph is a no-op).

    ``key`` overrides the dedup token — the runner passes the
    *pre-refinement* candidate key so a proposer changing the action
    (merge → re-axis) can't mint a second pending row for the same
    deterministic signal.
    """
    dedup_key = key or candidate.key
    if _key_pending(store, candidate.target_kind, candidate.target_id, dedup_key):
        return None
    note = json.dumps(
        {
            "garden": {
                "v": 1,
                "key": dedup_key,
                "action": candidate.action,
                "detector": candidate.detector,
                "title": candidate.title,
                "evidence": candidate.evidence,
                "payload": candidate.payload,
                "rationale": rationale,
            }
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    review = store.record_target_review(
        candidate.target_kind,
        candidate.target_id,
        actor=GARDEN_ACTOR,
        verdict="proposed",
        model=model,
        version=GARDEN_VERSION,
        note=note,
        conn=conn,
    )
    return int(review.review_id)


def record_rejection(
    store: Store,
    candidate: GardenCandidate,
    *,
    rationale: str,
    prompt_version: str,
    model: str | None = None,
    reason: str | None = None,
) -> int:
    """Write the proposer's ``rejected`` row for ``candidate`` — the memory
    that keeps the next run from re-asking (see the module docstring).
    ``reason`` is a machine-readable code when a deterministic rule, not
    the model, rejected it. Returns the new ``review_id``."""
    body: dict[str, Any] = {
        "v": 1,
        "key": candidate.key,
        "action": candidate.action,
        "detector": candidate.detector,
        "title": candidate.title,
        "evidence": candidate.evidence,
        "rationale": rationale,
        "proposer_reject": True,
        "prompt": prompt_version,
    }
    if reason:
        body["reason"] = reason
    note = json.dumps(
        {"garden": body},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    review = store.record_target_review(
        candidate.target_kind,
        candidate.target_id,
        actor=GARDEN_ACTOR,
        verdict="rejected",
        model=model,
        version=GARDEN_VERSION,
        note=note,
    )
    return int(review.review_id)


def resolve_proposal(
    store: Store,
    review_id: int,
    *,
    actor: str,
    verdict: str,
    extra: dict[str, Any] | None = None,
    conn: Any = None,
) -> Any:
    """Append the terminal (``approved``/``rejected``) verdict for a
    pending proposal — the second bar of two-bars-per-action.

    The row echoes the proposal's ``key`` so both the dedup check and the
    read view fold the pair: newest row wins, and a decided key stops
    reading as pending. Returns the new :class:`Review`, or ``None`` when
    ``review_id`` is not a pending garden row (already decided, or not
    ours). ``extra`` merges into the ``garden`` payload (an apply door
    passes e.g. ``{"applied": merge_id}``).
    """

    def _do(c: Any) -> Any:
        prop = c.execute(
            "SELECT target_kind, target_id, note FROM reviews "
            "WHERE review_id = %s AND actor = %s AND verdict = 'proposed'",
            (review_id, GARDEN_ACTOR),
        ).fetchone()
        if prop is None:
            return None
        g = _parse_note(prop[2]) or {}
        body: dict[str, Any] = {
            "garden": {"v": 1, "key": g.get("key"), "resolves": int(review_id)}
        }
        if extra:
            body["garden"].update(extra)
        return store.record_target_review(
            str(prop[0]),
            int(prop[1]),
            actor=actor,
            verdict=verdict,
            version=GARDEN_VERSION,
            note=json.dumps(body, ensure_ascii=False, sort_keys=True),
            conn=c,
        )

    if conn is not None:
        return _do(conn)
    with store.pool.connection() as c:
        return _do(c)


def pending_proposals(
    store: Store,
    *,
    action: str | None = None,
    limit: int = 200,
) -> list[GardenProposal]:
    """The read view: garden proposals still awaiting a verdict.

    A proposal stays *pending* while its row is the newest garden row for
    its key — a later ``approved``/``rejected`` row echoing the same key
    (via :func:`resolve_proposal`) retires it. The scan is not restricted
    to ``actor='graph-garden'``: the terminal verdict carries whoever
    decided it (a human, the apply door), so the fold has to read garden
    payloads regardless of actor. ``current=False`` marks proposals the
    target has drifted past (sha no longer matches).

    Proposer rejections (graph-garden's own ``rejected`` rows) are left
    out of the scan: no proposal precedes them, so they decide nothing
    here, and they would crowd the over-read window.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT review_id, target_kind, target_id, model, content_sha, "
            "       verdict, note, at, "
            "       coalesce(content_sha = precis_target_sha(target_kind, "
            "                target_id), false) AS current "
            "  FROM reviews WHERE note LIKE %s "
            "   AND NOT (actor = %s AND verdict = 'rejected' AND note LIKE %s) "
            " ORDER BY at DESC, review_id DESC LIMIT %s",
            # over-read; superseded keys fold below
            ('%"garden"%', GARDEN_ACTOR, '%"proposer_reject": true%', limit * 4),
        ).fetchall()
    decided: set[str] = set()
    out: list[GardenProposal] = []
    for rid, tkind, tid, model, _sha, verdict, note, at, current in rows:
        g = _parse_note(note)
        if g is None:
            continue
        key = str(g.get("key") or "")
        if key in decided:
            continue  # a later verdict already resolved this key
        if verdict != "proposed":
            decided.add(key)  # newest garden row for the key is terminal
            continue
        if action is not None and g.get("action") != action:
            continue
        out.append(
            GardenProposal(
                review_id=int(rid),
                target_kind=str(tkind),
                target_id=int(tid),
                action=str(g.get("action") or ""),
                key=key,
                detector=str(g.get("detector") or ""),
                title=str(g.get("title") or ""),
                payload=g.get("payload") or {},
                evidence=g.get("evidence") or {},
                rationale=str(g.get("rationale") or ""),
                model=None if model is None else str(model),
                at=at,
                current=bool(current),
            )
        )
        decided.add(key)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "GARDEN_ACTOR",
    "GARDEN_VERSION",
    "GardenProposal",
    "emit_proposal",
    "key_block",
    "pending_proposals",
    "proposal_key_pending",
    "record_rejection",
    "resolve_proposal",
]
