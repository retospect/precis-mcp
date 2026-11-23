"""Detect and repair dangling ``[fi<id>]`` cites in live draft prose
(gr690419, the data-repair half of gr265228).

A draft chunk citing ``[fi<id>]`` kept the cite when the hub was deleted
or merged away *before* the guards existed (``retire_ref`` now refuses a
cited finding, 70bca5d9; ``merge_hubs`` rewrites live cites in the same
transaction, 6276c3501), so grounded prose reverted to ungrounded while
still looking grounded — and every conversion census counts it as done.
This pass is the bounded backfill over that known cohort: find the
dangling cites, repoint each to the live successor hub, and list what has
no successor for manual regrounding.

**Detection** (:func:`plan_cite_repairs`) mirrors the merge's cite planner
(:func:`~precis.taproot.hub._plan_cite_rewrites`): live body chunks only
(``chunks.retired_at IS NULL AND ord >= 0``) of live drafts, whose text
matches the shared bracketed-``fi`` cite shape. Every ``fi`` element of
every matched handle list is classified against ``refs``:

* **live finding** — healthy, untouched (the census must not churn
  grounded prose);
* **retired with a recorded successor** — repointed via the merge's
  :func:`~precis.taproot.hub.rewrite_fi_cites` (collapsing a doubled
  winner cite), written through the draft edit primitive on one
  transaction (:func:`apply_cite_repairs`), exactly as ``merge_hubs``
  writes its cite rewrites — each move's target re-resolved and locked
  at write time so a winner retired since the plan cannot be written in
  as a fresh dangling cite, and the parent draft's liveness re-checked
  under its row lock so a draft soft-deleted since the plan is skipped,
  not rewritten;
* **retired or missing with no recorded successor** — listed as
  unresolved; the prose needs regrounding by hand, never a guessed hub.

**Successor resolution** is :func:`~precis.taproot.hub.resolve_successor_ref_id`:
the merge collapse edge first, then the plain-supersede stamp, the
retirement procedure's publish record, and the inbound ``supersedes``
tombstone edge — each verified to resolve to a live finding, hop by hop.

**Chunk handles.** The write goes through ``store.drafts.edit_text_in_tx``,
whose address is the chunk ``handle``; a live chunk with a dangling cite
but no handle cannot be repaired by this pass and is reported unresolved
rather than silently skipped (the merge's planner skips these so the
retire guard refuses loudly; here there is no later guard, so the report
is the loudness).

Idempotent: a repointed cite names a live hub, so a re-plan finds nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.store._refs_ops import CITE_LIST_SEP
from precis.store.revision_context import trust_door
from precis.taproot.hub import (
    _MERGE_CHAIN_MAX_HOPS,
    MergeCiteRewrite,
    apply_cite_rewrites,
    resolve_successor_ref_id,
    rewrite_fi_cites,
)

if TYPE_CHECKING:
    from precis.store import Store

#: One element of a bracketed handle list (``[fi12]``, ``[pa3, fi12]``) —
#: the same ``[a-z]{2}\d+`` shape :func:`~precis.store._refs_ops.fi_cite_pattern`
#: matches, so detection and the merge's rewrite agree on what a cite is.
_HANDLE = r"[a-z]{2}\d+"

#: A bracketed handle list, captured whole so every element is seen — a
#: greedy ``fi(\d+)`` inside one pattern would skip ``[fi1, fi2]``'s first
#: element (the prefix group eats it), and a dangling first element is
#: precisely the case this pass exists for.
_HANDLE_LIST_RE = re.compile(rf"\[({_HANDLE}(?:{CITE_LIST_SEP}{_HANDLE})*)\]")

#: The SQL prefilter, valid for Postgres ``~`` exactly as
#: ``fi_cite_pattern`` is (gr265228's detection query, widened from one
#: ref_id to any).
_ANY_FI_CITE_SQL = (
    rf"\[(?:{_HANDLE}{CITE_LIST_SEP})*fi[0-9]+(?:{CITE_LIST_SEP}{_HANDLE})*\]"
)


@dataclass(frozen=True)
class CiteRepair:
    """One live draft chunk whose dangling ``[fi]`` cites can all be
    repointed, and what each moves to. ``moves`` is
    ``(loser_ref_id, winner_ref_id)`` pairs, applied in order by
    :func:`~precis.taproot.hub.rewrite_fi_cites`."""

    chunk_id: int
    handle: str
    draft_ref_id: int
    old_text: str
    new_text: str
    moves: tuple[tuple[int, int], ...]

    def as_merge_rewrite(self) -> MergeCiteRewrite:
        """The shape :func:`~precis.taproot.hub.apply_cite_rewrites` writes."""
        return MergeCiteRewrite(
            chunk_id=self.chunk_id,
            handle=self.handle,
            draft_ref_id=self.draft_ref_id,
            old_text=self.old_text,
            new_text=self.new_text,
        )

    def to_row(self, *, applied: bool) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "handle": self.handle,
            "draft_ref_id": self.draft_ref_id,
            "status": "repointed",
            "moves": {f"fi{lo}": f"fi{w}" for lo, w in self.moves},
            "old_text": self.old_text,
            "new_text": self.new_text,
            "applied": applied,
        }


@dataclass(frozen=True)
class UnresolvedCite:
    """A dangling ``[fi<ref_id>]`` cite this pass cannot repoint: no
    recorded successor (reground by hand) or no chunk handle to write
    through. The cite stays exactly as it was."""

    chunk_id: int
    handle: str | None
    draft_ref_id: int
    ref_id: int
    reason: str

    def to_row(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "handle": self.handle,
            "draft_ref_id": self.draft_ref_id,
            "ref_id": self.ref_id,
            "status": self.reason,
            "applied": False,
        }


@dataclass(frozen=True)
class CiteRepairPlan:
    """The full repair proposal — the same object a dry run reports and an
    ``--apply`` run writes, so the CLI has one formatter for both."""

    rewrites: list[CiteRepair] = field(default_factory=list)
    unresolved: list[UnresolvedCite] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.rewrites and not self.unresolved


def _chunk_fi_ids(text: str) -> list[int]:
    """Every ``fi`` ref_id cited by bracketed handle lists in ``text``,
    order-preserving and deduped."""
    ids: list[int] = []
    for m in _HANDLE_LIST_RE.finditer(text):
        for el in re.split(CITE_LIST_SEP, m.group(1)):
            el = el.strip()
            if el.startswith("fi") and el[2:].isdigit():
                fid = int(el[2:])
                if fid not in ids:
                    ids.append(fid)
    return ids


def _candidate_chunks(conn: Any, draft_ref_id: int | None) -> list[Any]:
    """Live body chunks of live drafts whose text cites any ``fi`` hub —
    the detection query of gr265228, widened to any ref_id."""
    sql = (
        "SELECT c.chunk_id, c.handle, c.ref_id, c.text "
        "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
        "WHERE c.retired_at IS NULL AND c.ord >= 0 AND r.retired_at IS NULL "
        "AND r.kind = 'draft' AND c.text ~ %s"
    )
    params: list[Any] = [_ANY_FI_CITE_SQL]
    if draft_ref_id is not None:
        sql += " AND c.ref_id = %s"
        params.append(draft_ref_id)
    return conn.execute(f"{sql} ORDER BY c.ref_id, c.chunk_id", params).fetchall()


def _ref_states(conn: Any, ref_ids: list[int]) -> dict[int, tuple[bool, str | None]]:
    """``ref_id -> (retired, kind)`` for the cited ids; an absent row means
    the cite names a ref that never existed."""
    if not ref_ids:
        return {}
    rows = conn.execute(
        "SELECT ref_id, retired_at IS NOT NULL, kind FROM refs WHERE ref_id = ANY(%s)",
        (ref_ids,),
    ).fetchall()
    return {int(r[0]): (bool(r[1]), str(r[2])) for r in rows}


def plan_cite_repairs(
    store: Store, *, draft_ref_id: int | None = None
) -> CiteRepairPlan:
    """Scan live draft prose for dangling ``[fi]`` cites and plan the
    repair: repoints to recorded live successors, plus the leftovers no
    record covers. Read-only — the same plan an ``--apply`` run writes."""
    with store.tx() as conn:
        chunks = _candidate_chunks(conn, draft_ref_id)
        cited = sorted({fid for *_, text in chunks for fid in _chunk_fi_ids(text)})
        states = _ref_states(conn, cited)
        successors: dict[int, int | None] = {}
        plan = CiteRepairPlan()
        for chunk_id, handle, doc_ref_id, text in chunks:
            dangling = []
            for fid in _chunk_fi_ids(text):
                retired, kind = states.get(fid, (True, None))
                if retired or kind != "finding":
                    dangling.append(fid)
            if not dangling:
                continue
            moves: list[tuple[int, int]] = []
            for fid in dangling:
                if fid not in successors:
                    retired, kind = states.get(fid, (True, None))
                    successors[fid] = (
                        resolve_successor_ref_id(conn, fid)
                        if retired and kind == "finding"
                        else None
                    )
                winner = successors[fid]
                if winner is not None:
                    moves.append((fid, winner))
            new_text = text
            for loser, winner in moves:
                new_text = rewrite_fi_cites(new_text, loser, winner)
            written = moves and handle is not None and new_text != text
            if written:
                plan.rewrites.append(
                    CiteRepair(
                        chunk_id=int(chunk_id),
                        handle=str(handle),
                        draft_ref_id=int(doc_ref_id),
                        old_text=text,
                        new_text=new_text,
                        moves=tuple(moves),
                    )
                )
            for fid in dangling:
                if written and fid in {loser for loser, _ in moves}:
                    continue
                # A successor the records name but no handle to write it
                # through is a different job (manual) than no successor at
                # all (reground) — keep them apart in the report.
                reason = (
                    "no-chunk-handle"
                    if fid in {loser for loser, _ in moves}
                    else "no-successor"
                )
                plan.unresolved.append(
                    UnresolvedCite(
                        chunk_id=int(chunk_id),
                        handle=str(handle) if handle is not None else None,
                        draft_ref_id=int(doc_ref_id),
                        ref_id=fid,
                        reason=reason,
                    )
                )
        return plan


def _live_successor_locked(conn: Any, loser_ref_id: int) -> int | None:
    """The live-finding successor ``loser_ref_id``'s records point at,
    re-resolved and verified under a ``FOR SHARE`` row lock inside the
    write transaction — the plan named its winners on a since-closed
    read, so a winner merged or retired meanwhile moves to its own
    recorded live successor rather than writing a fresh dangling cite.

    ``loser_ref_id`` itself when the ref came back live since the plan
    (a restore healed the cite — leave it alone); ``None`` when no live
    successor is recorded. A winner that raced a retire between the
    resolve and the lock is re-resolved from its own records, under the
    same cap the collapse-chain walk uses.

    The row lock conflicts with
    ``retire_ref``'s ``FOR NO KEY UPDATE`` (taken *before* that door's
    cited-finding check), so whoever holds it first is the state the other
    sees — a concurrent retire either wins and this walk re-resolves, or
    waits and then refuses on the cite this transaction is about to
    commit. It is compatible with the ``FOR KEY SHARE`` taken by link
    repoints. The caller takes finding locks before draft locks, matching
    audited retirement, which can rewrite the same draft."""
    row = conn.execute(
        "SELECT retired_at FROM refs WHERE ref_id = %s FOR SHARE",
        (loser_ref_id,),
    ).fetchone()
    if row is not None and row[0] is None:
        return loser_ref_id
    start = loser_ref_id
    for _ in range(_MERGE_CHAIN_MAX_HOPS):
        nxt = resolve_successor_ref_id(conn, start)
        if nxt is None:
            return None
        row = conn.execute(
            "SELECT retired_at, kind FROM refs WHERE ref_id = %s FOR SHARE",
            (nxt,),
        ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return int(nxt) if row[1] == "finding" else None
        start = int(nxt)
    return None


@trust_door("computed")
def apply_cite_repairs(store: Store, plan: CiteRepairPlan) -> CiteRepairPlan:
    """Write the plan's repoints on one transaction through the draft edit
    primitive (sha bump + ``edited`` chunk event), exactly as
    ``merge_hubs`` applies its cite rewrites.

    Every move's target is revalidated inside the write transaction
    (:func:`_live_successor_locked`): the planned winner may itself have
    been merged or retired since the plan read it, and writing it then
    would only mint a new dangling cite. A loser restored live since the
    plan keeps its cite; a loser with no live successor left joins the
    unresolved list (reground by hand). A draft soft-deleted since the
    plan is skipped by :func:`~precis.taproot.hub.apply_cite_rewrites` —
    its cites stopped counting as live — and its cites join the
    unresolved list as ``draft-retired`` so the report explains why the
    planned rewrite never landed. A chunk edited since the plan
    read it fails the write rather than being overwritten. Returns the
    plan as it was actually applied — the report row, not the proposal."""
    if not plan.rewrites:
        return plan
    applied: list[CiteRepair] = []
    unresolved = list(plan.unresolved)
    with store.tx() as conn:
        # Audited retirement locks its findings before rewriting drafts.
        # Resolve and lock the findings before apply_cite_rewrites locks
        # any draft, including drafts that already cite the successor.
        for r in plan.rewrites:
            moves: list[tuple[int, int]] = []
            lost: list[int] = []
            for loser, _planned_winner in r.moves:
                successor = _live_successor_locked(conn, loser)
                if successor is None:
                    lost.append(loser)
                elif successor != loser:
                    moves.append((loser, successor))
            new_text = r.old_text
            for loser, winner in moves:
                new_text = rewrite_fi_cites(new_text, loser, winner)
            if moves and new_text != r.old_text:
                applied.append(
                    CiteRepair(
                        chunk_id=r.chunk_id,
                        handle=r.handle,
                        draft_ref_id=r.draft_ref_id,
                        old_text=r.old_text,
                        new_text=new_text,
                        moves=tuple(moves),
                    )
                )
            unresolved.extend(
                UnresolvedCite(
                    chunk_id=r.chunk_id,
                    handle=r.handle,
                    draft_ref_id=r.draft_ref_id,
                    ref_id=loser,
                    reason="no-successor",
                )
                for loser in lost
            )
        written = apply_cite_rewrites(
            store,
            conn,
            [r.as_merge_rewrite() for r in applied],
            source={"tool": "taproot-repair-cites", "gripe": "gr690419"},
        )
        if len(written) != len(applied):
            written_ids = {rw.chunk_id for rw in written}
            dropped = [r for r in applied if r.chunk_id not in written_ids]
            applied = [r for r in applied if r.chunk_id in written_ids]
            unresolved.extend(
                UnresolvedCite(
                    chunk_id=r.chunk_id,
                    handle=r.handle,
                    draft_ref_id=r.draft_ref_id,
                    ref_id=loser,
                    reason="draft-retired",
                )
                for r in dropped
                for loser, _ in r.moves
            )
    return CiteRepairPlan(rewrites=applied, unresolved=unresolved)
