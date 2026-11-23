"""The gr690419 dangling-``[fi]`` cite repair (`src/precis/taproot/repair_cites.py`).

DB-backed (real ``refs``/``chunks``/``links`` via the ``store`` fixture); no
LLM. The guards added in gr265228 (``retire_ref`` refuses a cited finding,
``merge_hubs`` rewrites live cites) make the historical defect unreachable
through the live doors, so each test builds the dangling shape the way prod
acquired it: retire the hub first, cite it afterwards (the draft lint's
tombstone hint is advisory, so the cite lands).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import (
    apply_cite_rewrites,
    merge_hubs,
    mint_hub,
    rewrite_fi_cites,
)
from precis.taproot.repair_cites import (
    apply_cite_repairs,
    plan_cite_repairs,
)
from tests.test_finding_retire_published import _publish


def _claim(n: int) -> CanonicalClaim:
    """A distinct claim per hub (distinct sentence -> distinct pub_id)."""
    return CanonicalClaim(
        sentence=f"Compound {n} catalyzes the model reaction at room temperature.",
        scope={"material": f"compound-{n}", "method": "model reaction", "regime": "RT"},
    )


_DRAFT_N = 0


def _draft_with(store: Any, text: str) -> Any:
    global _DRAFT_N
    _DRAFT_N += 1
    ref, _title = store.drafts.create_draft(
        name=f"citedraft-{_DRAFT_N}", title="Cite draft"
    )
    return store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=text,
        at={"last": True},
    )[0]


def _chunk_text(store: Any, chunk_id: int) -> str:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()[0]


def test_rewrite_fi_cites_preserves_unrelated_duplicate_groups() -> None:
    text = "[fi2, fi2] [fi2] [fi2]. [fi2][fi1] [fi2]. [pa3; fi1 and fi2]."
    assert rewrite_fi_cites(text, 1, 2) == (
        "[fi2, fi2] [fi2] [fi2]. [fi2]. [pa3; fi2]."
    )


def test_repair_repoints_merge_loser_cite(store: Any) -> None:
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    repair = plan.rewrites[0]
    assert repair.chunk_id == chunk.chunk_id
    assert repair.moves == ((loser, winner),)
    assert repair.new_text == f"Claim [fi{winner}] stands."
    assert plan.unresolved == []
    # A dry plan writes nothing.
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{loser}] stands."

    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{winner}] stands."
    with store.pool.connection() as conn:
        n_events = conn.execute(
            "SELECT count(*) FROM chunk_events WHERE event_kind = 'edited' "
            "AND source->>'tool' = 'taproot-repair-cites' AND chunk_id = %s",
            (chunk.chunk_id,),
        ).fetchone()[0]
    assert n_events == 1

    # Idempotent: the repointed cite names a live hub, so a re-plan is empty.
    assert plan_cite_repairs(store).is_empty


def test_repair_follows_chained_merges(store: Any) -> None:
    first = mint_hub(store, _claim(1))
    middle = mint_hub(store, _claim(2))
    last = mint_hub(store, _claim(3))
    merge_hubs(store, loser_ref_id=first, winner_ref_id=middle)
    merge_hubs(store, loser_ref_id=middle, winner_ref_id=last)
    chunk = _draft_with(store, f"Claim [fi{first}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].moves == ((first, last),)
    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{last}] stands."


def test_repair_resolves_supersede_stamp(store: Any) -> None:
    old = mint_hub(store, _claim(1))
    successor = mint_hub(store, _claim(2))
    handler = FindingHandler(hub=Hub(store=store))
    handler.delete(id=old, superseded_by=f"fi{successor}", reason="same claim")
    chunk = _draft_with(store, f"Claim [fi{old}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].moves == ((old, successor),)
    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{successor}] stands."


def test_repair_lists_no_successor_for_regrounding(store: Any) -> None:
    orphan = mint_hub(store, _claim(1))
    handler = FindingHandler(hub=Hub(store=store))
    handler.delete(id=orphan, reason="retracted, no replacement")
    chunk = _draft_with(store, f"Claim [fi{orphan}] stands.")

    plan = plan_cite_repairs(store)
    assert plan.rewrites == []
    assert len(plan.unresolved) == 1
    u = plan.unresolved[0]
    assert (u.chunk_id, u.ref_id, u.reason) == (chunk.chunk_id, orphan, "no-successor")

    apply_cite_repairs(store, plan)  # no rewrites: a no-op write
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{orphan}] stands."


def test_repair_partially_repoints_mixed_cite_list(store: Any) -> None:
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    orphan = mint_hub(store, _claim(3))
    handler = FindingHandler(hub=Hub(store=store))
    handler.delete(id=orphan, reason="retracted, no replacement")
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claims [fi{loser}, fi{orphan}] disagree.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].moves == ((loser, winner),)
    assert plan.rewrites[0].new_text == f"Claims [fi{winner}, fi{orphan}] disagree."
    assert [u.ref_id for u in plan.unresolved] == [orphan]

    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == (
        f"Claims [fi{winner}, fi{orphan}] disagree."
    )


def test_repair_ignores_live_and_uncited_chunks(store: Any) -> None:
    live = mint_hub(store, _claim(1))
    _draft_with(store, f"Claim [fi{live}] stands, grounded.")
    _draft_with(store, "No cites here at all.")
    assert plan_cite_repairs(store).is_empty


def test_repair_treats_never_existing_ref_as_unresolved(store: Any) -> None:
    mint_hub(store, _claim(1))  # so the corpus is non-empty
    _draft_with(store, "Claim [fi999999999] stands.")
    plan = plan_cite_repairs(store)
    assert plan.rewrites == []
    assert [u.ref_id for u in plan.unresolved] == [999999999]


def test_repair_reports_chunk_without_handle(store: Any) -> None:
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET handle = NULL WHERE chunk_id = %s", (chunk.chunk_id,)
        )

    plan = plan_cite_repairs(store)
    assert plan.rewrites == []
    assert len(plan.unresolved) == 1
    u = plan.unresolved[0]
    assert (u.chunk_id, u.handle, u.ref_id, u.reason) == (
        chunk.chunk_id,
        None,
        loser,
        "no-chunk-handle",
    )


def test_repair_follows_collapse_then_stamp_chain(store: Any) -> None:
    """Mixed chain, collapse edge into a supersede stamp: A merged into B
    (A keeps its collapse record), then B retired with ``superseded_by``
    while the A->B edge survived — e.g. stamped by hand or a door that
    does not migrate links, where the audited delete would have moved it.
    Resolving one recorded hop at a time is what lets the walk reach B's
    stamp; the all-or-nothing ``_merge_survivor`` returned A here."""
    first = mint_hub(store, _claim(1))
    middle = mint_hub(store, _claim(2))
    last = mint_hub(store, _claim(3))
    merge_hubs(store, loser_ref_id=first, winner_ref_id=middle)
    store.stamp_ref_meta(middle, {"superseded_by": f"fi{last}"})
    store.retire_ref(middle)
    chunk = _draft_with(store, f"Claim [fi{first}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].moves == ((first, last),)
    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{last}] stands."


def test_repair_follows_stamp_then_collapse_chain(store: Any) -> None:
    """The mirror image: A retired with a ``superseded_by`` stamp to B
    (the direct stamp leaves A linkless, so the stamp is its record),
    then B merged into C — the stamp hop lands on a collapse record."""
    first = mint_hub(store, _claim(1))
    middle = mint_hub(store, _claim(2))
    last = mint_hub(store, _claim(3))
    store.stamp_ref_meta(first, {"superseded_by": f"fi{middle}"})
    store.retire_ref(first)
    merge_hubs(store, loser_ref_id=middle, winner_ref_id=last)
    chunk = _draft_with(store, f"Claim [fi{first}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    assert plan.rewrites[0].moves == ((first, last),)
    apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{last}] stands."


def test_apply_repoints_to_new_successor_when_planned_winner_merged(
    store: Any,
) -> None:
    """The plan ran on a closed read: if the planned winner was itself
    merged before the write, apply must not write the stale (now
    dangling) ``fi<winner>`` — it re-resolves under row locks."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    final = mint_hub(store, _claim(3))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    assert plan.rewrites[0].moves == ((loser, winner),)
    merge_hubs(store, loser_ref_id=winner, winner_ref_id=final)

    applied = apply_cite_repairs(store, plan)
    assert len(applied.rewrites) == 1
    assert applied.rewrites[0].moves == ((loser, final),)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{final}] stands."
    assert plan_cite_repairs(store).is_empty


def test_apply_reports_unresolved_when_planned_winner_died(store: Any) -> None:
    """A winner retired with no successor between plan and apply leaves
    the loser unresolvable: no write, and the report names it."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    assert plan.rewrites[0].moves == ((loser, winner),)
    handler = FindingHandler(hub=Hub(store=store))
    handler.delete(id=winner, reason="retracted, no replacement")

    applied = apply_cite_repairs(store, plan)
    assert applied.rewrites == []
    assert [u.ref_id for u in applied.unresolved] == [loser]
    assert applied.unresolved[0].reason == "no-successor"
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{loser}] stands."


def test_apply_keeps_cite_when_loser_restored(store: Any) -> None:
    """A loser restored live between plan and apply healed the cite —
    apply leaves the text alone rather than repointing to the stale
    successor record."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    assert plan.rewrites[0].moves == ((loser, winner),)
    store.restore_ref(loser)

    applied = apply_cite_repairs(store, plan)
    assert applied.is_empty
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{loser}] stands."
    assert plan_cite_repairs(store).is_empty


def test_apply_refuses_when_chunk_changed_since_plan(store: Any) -> None:
    """The chunk update is conditional on the text the plan read
    (``base_sha`` under the row lock): a chunk edited between plan and
    apply fails the write rather than being overwritten."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    store.drafts.edit_text(chunk.handle, f"Claim [fi{loser}] revised.")

    with pytest.raises(BadInput):
        apply_cite_repairs(store, plan)
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{loser}] revised."


def test_apply_skips_rewrite_when_draft_retired_since_plan(store: Any) -> None:
    """A draft soft-deleted (``refs.retired_at``) between plan and apply
    leaves the chunk's own ``retired_at`` and text untouched — the edit
    primitive's checks can't see it — so apply locks the parent draft row
    and drops the rewrite rather than writing into dead prose. Its cites
    stop counting as live, so nothing is lost; the row is reported
    ``draft-retired`` to explain why the planned rewrite never landed."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1
    store.retire_ref(chunk.ref_id)  # ref only — chunks stay live

    applied = apply_cite_repairs(store, plan)
    assert applied.rewrites == []
    assert [(u.chunk_id, u.ref_id, u.reason) for u in applied.unresolved] == [
        (chunk.chunk_id, loser, "draft-retired")
    ]
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{loser}] stands."

    # A re-plan no longer sees the chunk: its cites are not live cites.
    assert plan_cite_repairs(store).is_empty


def test_apply_locks_successor_before_published_retirement_rewrites_draft(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause audited retirement with its finding locked, before it rewrites
    the shared draft. Repair must wait without holding the draft lock."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    successor = mint_hub(store, _claim(3))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    _publish(store, winner, "reviewed")
    chunk = _draft_with(store, f"Claim [fi{loser}][fi{winner}] stands.")

    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 1

    errors: list[BaseException] = []

    def _repair() -> None:
        try:
            apply_cite_repairs(store, plan)
        except BaseException as exc:
            errors.append(exc)

    handler = FindingHandler(hub=Hub(store=store))
    retire_apply = handler._retire_apply
    worker = threading.Thread(target=_repair, daemon=True)

    def _race_repair(retire_plan: Any, *, conn: Any) -> list[str]:
        worker.start()
        _wait_for_lock_waiter(store)
        return retire_apply(retire_plan, conn=conn)

    monkeypatch.setattr(handler, "_retire_apply", _race_repair)
    try:
        handler.delete(
            id=winner,
            superseded_by=f"fi{successor}",
            reason="same claim",
            retire_published=True,
        )
    finally:
        if worker.ident is not None:
            worker.join(timeout=15)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], BadInput)
    assert "changed since you read it" in errors[0].cause
    assert (
        _chunk_text(store, chunk.chunk_id)
        == f"Claim [fi{loser}][fi{successor}] stands."
    )
    apply_cite_repairs(store, plan_cite_repairs(store))
    assert _chunk_text(store, chunk.chunk_id) == f"Claim [fi{successor}] stands."


def test_apply_cite_rewrites_returns_only_written_rewrites(store: Any) -> None:
    """The write receipt counts what was written: a draft retired after the
    plan is skipped, and :func:`apply_cite_rewrites` returns the subset it
    wrote, not the list it was handed."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    live = _draft_with(store, f"Live [fi{loser}] cite.")
    dead = _draft_with(store, f"Dead [fi{loser}] cite.")
    plan = plan_cite_repairs(store)
    assert len(plan.rewrites) == 2
    store.retire_ref(dead.ref_id)  # ref only: its chunk stays live

    with store.tx() as conn:
        written = apply_cite_rewrites(
            store,
            conn,
            [r.as_merge_rewrite() for r in plan.rewrites],
            source={"tool": "test-receipt"},
        )
    assert [rw.chunk_id for rw in written] == [live.chunk_id]
    assert _chunk_text(store, live.chunk_id) == f"Live [fi{winner}] cite."
    assert _chunk_text(store, dead.chunk_id) == f"Dead [fi{loser}] cite."


def _wait_for_lock_waiter(store: Any, timeout: float = 15.0) -> None:
    """Block until another backend of this database is waiting on a lock —
    i.e. the worker reached its blocking acquire — so the caller commits
    only once the transaction ordering under test is staged."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store.pool.connection() as probe:
            n = probe.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' "
                "AND datname = current_database() "
                "AND pid <> pg_backend_pid() "
                "AND query ILIKE '%refs%'",
            ).fetchone()[0]
        if n:
            return
        time.sleep(0.05)
    raise TimeoutError("no lock waiter appeared — the retire race was never staged")


def test_retire_ref_waits_out_repair_lock_then_sees_cite(store: Any) -> None:
    """The two-transaction sequence ``retire_ref``'s shared lock closes:
    the kind probe locks the ref row BEFORE the cited-finding check, so a
    repair write holding that lock serializes the check after its commit
    — an earlier, unlocked check would pass on the pre-write snapshot,
    block at the UPDATE, and retire the just-cited hub anyway (gr690419
    review)."""
    loser = mint_hub(store, _claim(1))
    winner = mint_hub(store, _claim(2))
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner)
    chunk = _draft_with(store, f"Claim [fi{loser}] stands.")

    errors: list[BaseException] = []

    def _retire() -> None:
        try:
            store.retire_ref(winner)
        except BaseException as exc:
            errors.append(exc)

    with store.pool.connection() as conn:
        # Stand in for the repair's write transaction: hold the winner's
        # row lock (``_live_successor_locked``'s FOR SHARE) while
        # rewriting the chunk's cite onto it.
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR SHARE", (winner,))
        worker = threading.Thread(target=_retire, daemon=True)
        worker.start()
        _wait_for_lock_waiter(store)
        conn.execute(
            "UPDATE chunks SET text = %s WHERE chunk_id = %s",
            (f"Claim [fi{winner}] stands.", chunk.chunk_id),
        )
    worker.join(timeout=15)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], BadInput)
    with store.pool.connection() as conn:
        still_live = conn.execute(
            "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (winner,)
        ).fetchone()[0]
    assert still_live
