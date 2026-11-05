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
An explicit undo remains rejected on the restored content; anchor-rot undo
matches across main SHAs so a repository commit cannot undo that decision.

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

from precis.errors import BadInput, NotFound
from precis.graph.garden.candidates import GardenCandidate
from precis.utils.handle_registry import parse as parse_handle

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

#: Shared with the consumer so reviewer evidence needs no consumer import.
MEMORY_APPLY_ACTIONS = frozenset({"rewrite", "supersede", "retire"})

#: Candidate re-checks shared by consumer eligibility and the memory apply door.
MEMORY_APPLY_DETECTORS = {
    "dangling_mention": "dangling_mention_candidates",
    "anchor_rot": "anchor_rot_candidates",
    "thread_shape": "thread_shape_candidates",
}


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


def proposal_ref_ids(proposal: GardenProposal) -> list[int]:
    """Named refs whose own contents support a proposal's review."""
    ids = [proposal.target_id] if proposal.target_kind == "ref" else []
    for key in ("winner", "loser", "parent"):
        val = proposal.payload.get(key)
        if isinstance(val, int):
            ids.append(val)
    if proposal.action in MEMORY_APPLY_ACTIONS:
        successors = [
            proposal.payload.get("successor"),
            proposal.payload.get("delete_successor"),
        ]
        successors.extend(
            mention.get("successor")
            for mention in proposal.evidence.get("mentions", [])
            if isinstance(mention, dict)
        )
        for successor in successors:
            if isinstance(successor, int):
                ids.append(successor)
            elif isinstance(successor, str):
                parsed = parse_handle(successor)
                if parsed is not None and not parsed[1]:
                    ids.append(parsed[2])
    elif proposal.detector == "dangling_mention":
        for mention in proposal.evidence.get("mentions", []):
            successor = parse_handle(str(mention.get("successor") or ""))
            if successor is not None and not successor[1]:
                ids.append(successor[2])
    return list(dict.fromkeys(ids))


def memory_successor_refusal(
    payload: dict[str, Any], evidence: dict[str, Any]
) -> str | None:
    """Refuse document successors until their content can be shown and pinned.

    An ``md:`` route has no ref content SHA. Treating it like a missing handle
    silently omits the evidence on which preservation depends.
    """
    successors = [payload.get("successor"), payload.get("delete_successor")]
    successors.extend(
        mention.get("successor")
        for mention in evidence.get("mentions", [])
        if isinstance(mention, dict)
    )
    if any(isinstance(value, str) and value.startswith("md:") for value in successors):
        return (
            "md: successors are unsupported: their source content cannot be "
            "shown and pinned; use a live ref successor and propose again"
        )
    return None


def _retirement_successor_pin(
    store: Store, target: int, *, conn: Any = None
) -> dict[str, Any]:
    """Read the delete door's inferred route without changing detector evidence."""
    from precis.dispatch import Hub

    if conn is None:
        with store.pool.connection() as c:
            return _retirement_successor_pin(store, target, conn=c)
    row = conn.execute(
        "SELECT meta FROM refs WHERE ref_id = %s AND kind = 'memory' "
        "AND retired_at IS NULL",
        (target,),
    ).fetchone()
    if row is None:
        return {"delete_successor_error": "the memory is no longer live"}
    try:
        handle, successor, _kind = (
            Hub(store=store)
            .sibling("memory")
            ._delete_successor(target, row[0] or {}, conn=conn)
        )
    except (BadInput, NotFound) as exc:
        return {"delete_successor_error": str(exc)}
    sha = (
        conn.execute("SELECT precis_target_sha('ref', %s)", (successor,)).fetchone()[0]
        if successor is not None
        else None
    )
    return {"delete_successor": handle, "delete_successor_sha": sha}


@dataclass(frozen=True, slots=True)
class MemoryRetirementDrift:
    """Changed evidence is retryable; an unresolved route needs repair."""

    reason: str
    why: str


def memory_retirement_drift(
    store: Store, proposal: GardenProposal
) -> MemoryRetirementDrift | None:
    """Refuse unread or changed inferred successor evidence before paid review."""
    if proposal.action != "retire" or proposal.detector not in MEMORY_APPLY_DETECTORS:
        return None
    current = _retirement_successor_pin(store, proposal.target_id)
    if error := current.get("delete_successor_error"):
        return MemoryRetirementDrift(
            "apply_refused", f"the retirement successor cannot be resolved: {error}"
        )
    # Legacy proposals without a route need no extra evidence, but cannot
    # acquire a successor invisibly. Newly emitted proposals pin None too.
    for key in ("delete_successor", "delete_successor_sha"):
        if current[key] != proposal.payload.get(key):
            return MemoryRetirementDrift(
                "stale",
                "the retirement successor or its content changed; propose again",
            )
    if proposal.payload.get("delete_successor_error"):
        return MemoryRetirementDrift(
            "stale",
            "the retirement successor was not resolved when proposed; propose again",
        )
    return None


def _undo_key(key: str) -> str:
    """Undo pins restored content across main commits and retirement routes."""
    action, *parts = key.split(":")
    if action in {"supersede", "retire", "merge"} and len(parts) >= 2:
        return ":".join(("retire", *parts[:2]))
    prefix, marker, _main_sha = key.rpartition(":anchor_rot:")
    return prefix + marker if marker else key


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
    re-opens the slot. A retryable rejection closes the old proposal but
    re-opens the key: stale supporting evidence needs a fresh review even
    when the target's content is unchanged. No actor filter: the verdict belongs to
    whoever resolved it, not to graph-garden.
    An undo rejection also matches anchor-rot keys across main SHAs and all
    retirement actions/successors, including merges losing this target, while
    restored target content is current.

    A proposer rejection (:func:`record_rejection`) blocks only a caller
    naming the same ``prompt_version`` — the runner, asking whether to
    re-ask. Without one (the emit path, after the runner decided) it
    does not block."""
    reviews = store.reviews_for(target_kind, target_id)
    # An undo rejects changing/removing the restored body. Repo movement or
    # a different retirement route cannot reopen it. Other rejections retain
    # exact-key scope.
    for rev in reviews:
        g = _parse_note(rev.note)
        if (
            g is not None
            and rev.verdict == "rejected"
            and rev.current
            and g.get("reason") == "undone"
            and _undo_key(str(g.get("key") or "")) == _undo_key(key)
        ):
            return "rejected"
    for rev in reviews:
        g = _parse_note(rev.note)
        if g is None or g.get("key") != key:
            continue
        if rev.verdict == "proposed":
            return "pending"
        if rev.verdict == "rejected" and rev.current and not g.get("retryable"):
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
    payload = dict(candidate.payload)
    if candidate.action == "retire" and candidate.detector in MEMORY_APPLY_DETECTORS:
        payload.update(_retirement_successor_pin(store, candidate.target_id, conn=conn))
    note = json.dumps(
        {
            "garden": {
                "v": 1,
                "key": dedup_key,
                "action": candidate.action,
                "detector": candidate.detector,
                "title": candidate.title,
                "evidence": candidate.evidence,
                "payload": payload,
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
    model: str | None = None,
    extra: dict[str, Any] | None = None,
    conn: Any = None,
) -> Any:
    """Append the terminal (``approved``/``rejected``) verdict for a
    pending proposal — the second bar of two-bars-per-action.

    The row echoes the proposal's ``key`` so both the dedup check and the
    read view fold the pair: newest row wins, and a decided key stops
    reading as pending. Returns the new :class:`Review`, or ``None`` when
    ``review_id`` is not a pending garden row — already decided, or not
    ours. A proposal row keeps ``verdict='proposed'`` after it is
    decided (the ledger is append-only), so "decided" means a second row
    carries ``garden.resolves=<review_id>``; the ``FOR UPDATE`` lock
    makes a concurrent resolver see that row instead of double-deciding.
    ``extra`` merges into the ``garden`` payload (an apply door
    passes e.g. ``{"applied": merge_id}``).
    """

    def _do(c: Any) -> Any:
        prop = c.execute(
            "SELECT target_kind, target_id, note FROM reviews "
            "WHERE review_id = %s AND actor = %s AND verdict = 'proposed' "
            "FOR UPDATE",
            (review_id, GARDEN_ACTOR),
        ).fetchone()
        if prop is None:
            return None
        decided = c.execute(
            "SELECT 1 FROM reviews WHERE note LIKE %s AND note ~ %s LIMIT 1",
            ('%"resolves"%', f'"resolves": {int(review_id)}[^0-9]'),
        ).fetchone()
        if decided is not None:
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
            model=model,
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


@dataclass(frozen=True, slots=True)
class MergeLedgerEntry:
    """One fold of the merge-action slice of the garden ledger
    (gr548660): the proposal row plus its terminal verdict."""

    review_id: int  # the proposal row's review_id (resolves target)
    target_kind: str
    target_id: int
    key: str
    detector: str
    title: str
    evidence: dict[str, Any]
    payload: dict[str, Any]  # the proposal's pair (loser/winner pre-verdict)
    rationale: str
    verdict: str  # 'proposed' (still pending) | 'approved' | 'rejected'
    model: str | None
    proposed_at: Any
    decided_at: Any | None
    decided_by: str | None  # actor on the terminal row
    merge_id: str | None  # garden.applied — the merge record's id
    loser_id: int | None  # garden.loser on the approved row
    winner_id: int | None  # garden.winner on the approved row
    undone: bool
    current: bool  # the proposal row's content_sha still matches


class _MergeAcc:
    """Per-key fold state for :func:`merge_ledger`."""

    __slots__ = ("proposal", "terminal")

    def __init__(self) -> None:
        self.proposal: tuple[Any, dict[str, Any]] | None = None
        self.terminal: tuple[Any, dict[str, Any]] | None = None


def merge_ledger(
    store: Store,
    *,
    since: Any | None = None,
    limit: int = 200,
) -> list[MergeLedgerEntry]:
    """The merge-action slice of the garden review ledger (gr548660).

    Same fold as :func:`pending_proposals` — newest garden row per key
    decides, and a terminal row pairs with the ``proposed`` row it
    resolves — but keeps merge keys in every state (pending, approved,
    rejected), not just pending. ``merge_id`` / ``loser_id`` /
    ``winner_id`` come from the approved row's ``garden`` extras
    (``resolve_proposal``'s ``extra`` on the apply path).

    ``undone`` asks the loser ref whether it still carries
    ``meta.garden_merge`` for this merge — ``undo_merge`` clears that
    record and writes no ledger row, so the loser's own row is the only
    place the reversal shows. A rejected terminal carrying
    ``reason='undone'``/``undone=<id>`` (the relink convention) marks it
    too. ``since`` filters on the deciding row's ``at``.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT review_id, target_kind, target_id, model, content_sha, "
            "       verdict, note, at, actor, "
            "       coalesce(content_sha = precis_target_sha(target_kind, "
            "                target_id), false) AS current "
            "  FROM reviews WHERE note LIKE %s "
            "   AND NOT (actor = %s AND verdict = 'rejected' AND note LIKE %s) "
            " ORDER BY at DESC, review_id DESC LIMIT %s",
            ('%"garden"%', GARDEN_ACTOR, '%"proposer_reject": true%', limit * 8),
        ).fetchall()

    accs: dict[str, _MergeAcc] = {}
    order: list[str] = []
    for row in rows:
        g = _parse_note(row[6])
        if g is None or g.get("proposer_reject"):
            continue
        key = str(g.get("key") or "")
        if key not in accs:
            accs[key] = _MergeAcc()
            order.append(key)
        acc = accs[key]
        if row[5] == "proposed":
            # Newest proposal wins (a re-opened key); an earlier terminal
            # already in the acc belongs to a previous decision round and
            # stays — the pair reads proposed again either way.
            if acc.proposal is None:
                acc.proposal = (row, g)
        elif acc.terminal is None and acc.proposal is None:
            acc.terminal = (row, g)

    cand: list[
        tuple[tuple[Any, dict[str, Any]], tuple[Any, dict[str, Any]] | None]
    ] = []
    for key in order:
        acc = accs[key]
        if acc.proposal is None:
            # A decided key whose proposal row fell off the over-read
            # window — without it the action is unverifiable, so skip.
            continue
        if str(acc.proposal[1].get("action") or "") != "merge":
            continue
        if since is not None:
            deciding = acc.terminal[0][7] if acc.terminal else acc.proposal[0][7]
            if deciding is not None and deciding < since:
                continue
        cand.append((acc.proposal, acc.terminal))
        if len(cand) >= limit:
            break

    # Undone detection: a merge is still applied only while the loser
    # carries its garden_merge record for this merge_id.
    losers: set[int] = set()
    for _prop, term in cand:
        if term is None or term[1].get("applied") is None:
            continue
        try:
            losers.add(int(term[1]["loser"]))
        except (KeyError, TypeError, ValueError):
            continue
    live_merges: dict[int, str] = {}
    if losers:
        with store.pool.connection() as conn:
            for rid, mid in conn.execute(
                "SELECT ref_id, meta->'garden_merge'->>'merge_id' "
                "FROM refs WHERE ref_id = ANY(%s)",
                (list(losers),),
            ).fetchall():
                if mid is not None:
                    live_merges[int(rid)] = str(mid)

    out: list[MergeLedgerEntry] = []
    for (prop_row, prop_g), term in cand:
        term_g: dict[str, Any] = term[1] if term is not None else {}
        merge_id = term_g.get("applied")
        try:
            loser = int(term_g["loser"]) if term_g.get("loser") is not None else None
        except (TypeError, ValueError):
            loser = None
        try:
            winner = int(term_g["winner"]) if term_g.get("winner") is not None else None
        except (TypeError, ValueError):
            winner = None
        undone = term_g.get("reason") == "undone" or term_g.get("undone") is not None
        if (
            merge_id is not None
            and not undone
            and loser is not None
            and live_merges.get(loser) != str(merge_id)
        ):
            undone = True
        out.append(
            MergeLedgerEntry(
                review_id=int(prop_row[0]),
                target_kind=str(prop_row[1]),
                target_id=int(prop_row[2]),
                key=str(prop_g.get("key") or ""),
                detector=str(prop_g.get("detector") or ""),
                title=str(prop_g.get("title") or ""),
                evidence=prop_g.get("evidence") or {},
                payload=prop_g.get("payload") or {},
                rationale=str(prop_g.get("rationale") or ""),
                verdict="proposed" if term is None else str(term[0][5]),
                model=None if prop_row[3] is None else str(prop_row[3]),
                proposed_at=prop_row[7],
                decided_at=None if term is None else term[0][7],
                decided_by=None if term is None else str(term[0][8]),
                merge_id=None if merge_id is None else str(merge_id),
                loser_id=loser,
                winner_id=winner,
                undone=undone,
                current=bool(prop_row[9]),
            )
        )
    return out


__all__ = [
    "GARDEN_ACTOR",
    "GARDEN_VERSION",
    "GardenProposal",
    "MergeLedgerEntry",
    "emit_proposal",
    "key_block",
    "memory_retirement_drift",
    "memory_successor_refusal",
    "merge_ledger",
    "pending_proposals",
    "proposal_key_pending",
    "record_rejection",
    "resolve_proposal",
]
