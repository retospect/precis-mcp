"""gr548660: ``view='retired'`` tombstone read + ``/garden/merges`` ledger.

A merge retires the loser; its handle then *redirects* to the survivor,
so an auditor could not read what was folded without falling back to
raw SQL. Two read doors close that:

- ``get(kind=K, id='<retired handle>', view='retired')`` (also
  ``args={'retired': True}``) renders the tombstone itself — remaining
  identifiers, its own body chunks, the links the merge repointed,
  ``superseded_by`` and the merge record — and never redirects.
- ``get(kind='job', id='/garden/merges', args={'since': '30d'})`` lists
  the merge slice of the garden review ledger (proposal + terminal
  verdict folded by ``garden.key``), including the merge id and the
  undone flag.
"""

from __future__ import annotations

from precis.graph.garden.apply import apply_merge, undo_merge
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import emit_proposal, merge_ledger
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.store.revision_context import revision_context


def _mem(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _endpoint_shas(store: Store, *ref_ids: int) -> dict[str, str]:
    with store.pool.connection() as conn:
        return {
            str(r[0]): str(r[1])
            for r in conn.execute(
                "SELECT ref_id, precis_target_sha('ref', ref_id) "
                "FROM refs WHERE ref_id = ANY(%s)",
                (list(ref_ids),),
            ).fetchall()
        }


def _garden_merge(store: Store, loser: int, winner: int) -> tuple[int, str]:
    """A merge as the consumer runs it: proposal row + apply that
    resolves the row — so the ledger carries the full fixture."""
    candidate = GardenCandidate(
        # Not ``dup_pair``: apply_merge re-verifies that detector's dup
        # floor inside the transaction, and a fixture pair with no
        # embeddings fails it (``sibling_confusion`` is a real merge
        # detector without a re-check gate).
        detector="sibling_confusion",
        action="merge",
        target_kind="ref",
        target_id=loser,
        other_id=winner,
        title="dup pair: two copies of one node",
        evidence={"score": 0.97, "shared_terms": ["alpha", "beta"]},
        payload={
            "loser": loser,
            "winner": winner,
            "shas": _endpoint_shas(store, loser, winner),
        },
    )
    review_id = emit_proposal(
        store, candidate, rationale="identical body text, same neighbours"
    )
    assert review_id is not None
    record = apply_merge(
        store, loser, winner, actor="garden-consumer", review_id=review_id
    )
    return review_id, record.merge_id


def _get(rt: PrecisRuntime, **kw: object) -> tuple[str, bool]:
    return rt.dispatch_with_status("get", kw)


# ── view='retired' ────────────────────────────────────────────────────


def test_retired_view_renders_tombstone(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = _mem(store, "Retired audit loser")
    winner = _mem(store, "Surviving audit winner")
    store.chunks.insert_chunks(
        loser,
        [
            ChunkInsert(ord=0, text="First loser body chunk."),
            ChunkInsert(ord=1, text="Second loser body chunk."),
        ],
    )
    _review_id, merge_id = _garden_merge(store, loser, winner)

    body, is_error = _get(
        runtime_with_store, kind="memory", id=f"me{loser}", view="retired"
    )

    assert not is_error, body
    assert f"me{loser} — retired" in body
    assert f"superseded_by: me{winner}" in body
    assert f"merge: {merge_id}" in body
    assert "body chunks: 2" in body
    assert "First loser body chunk." in body


def test_retired_view_via_args_spelling(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = _mem(store, "Args-spelled tombstone")
    winner = _mem(store, "Args-spelled survivor")
    _garden_merge(store, loser, winner)

    body, is_error = _get(
        runtime_with_store,
        kind="memory",
        id=f"me{loser}",
        __extras__={"retired": True},
    )

    assert not is_error, body
    assert f"me{loser} — retired" in body


def test_retired_view_on_paper_handle(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = store.insert_ref(
        kind="paper", slug="auditloser2026", title="Audit loser paper"
    ).id
    winner = store.insert_ref(
        kind="paper", slug="auditwinner2026", title="Audit winner paper"
    ).id
    _garden_merge(store, loser, winner)

    body, is_error = _get(
        runtime_with_store, kind="paper", id=f"pa{loser}", view="retired"
    )

    assert not is_error, body
    assert f"pa{loser} — retired" in body
    assert "Audit loser paper" in body


def test_default_get_still_redirects_to_survivor(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = _mem(store, "Redirect-check loser")
    winner = _mem(store, "Redirect-check survivor")
    _garden_merge(store, loser, winner)

    body, is_error = _get(runtime_with_store, kind="memory", id=f"me{loser}")

    assert not is_error, body
    assert "Redirect-check survivor" in body
    assert f"me{loser} — retired" not in body


def test_retired_view_refuses_a_live_ref(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    live = _mem(store, "Still alive")

    body, is_error = _get(
        runtime_with_store, kind="memory", id=f"me{live}", view="retired"
    )

    assert is_error
    assert "is live" in body


# ── /garden/merges ledger ─────────────────────────────────────────────


def test_merges_list_renders_the_fixture_review(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = _mem(store, "Ledger loser")
    winner = _mem(store, "Ledger winner")
    _review_id, merge_id = _garden_merge(store, loser, winner)

    body, is_error = _get(
        runtime_with_store,
        kind="job",
        id="/garden/merges",
        __extras__={"since": "1d"},
    )

    assert not is_error, body
    assert merge_id in body
    assert "approved" in body
    assert f"me{loser} → me{winner}" in body
    assert "dup pair: two copies of one node" in body
    assert "identical body text, same neighbours" in body
    assert "undone" not in body


def test_merges_list_marks_an_undone_merge(
    store: Store, runtime_with_store: PrecisRuntime
) -> None:
    loser = _mem(store, "Undone-merge loser")
    winner = _mem(store, "Undone-merge winner")
    _review_id, merge_id = _garden_merge(store, loser, winner)
    with revision_context(trust_origin="human", trust_actor_type="operator"):
        undo_merge(store, merge_id)

    body, is_error = _get(
        runtime_with_store,
        kind="job",
        id="/garden/merges",
        __extras__={"since": "1d"},
    )

    assert not is_error, body
    assert merge_id in body
    assert "undone" in body


def test_merge_ledger_model_directly(store: Store) -> None:
    loser = _mem(store, "Model-check loser")
    winner = _mem(store, "Model-check winner")
    review_id, merge_id = _garden_merge(store, loser, winner)

    entries = merge_ledger(store)

    assert len(entries) == 1
    e = entries[0]
    assert e.review_id == review_id
    assert e.merge_id == merge_id
    assert e.loser_id == loser
    assert e.winner_id == winner
    assert e.verdict == "approved"
    assert e.decided_by == "garden-consumer"
    assert e.undone is False


def test_pending_merge_proposal_lists_as_proposed(store: Store) -> None:
    loser = _mem(store, "Pending-merge loser")
    winner = _mem(store, "Pending-merge winner")
    candidate = GardenCandidate(
        detector="dup_pair",
        action="merge",
        target_kind="ref",
        target_id=loser,
        other_id=winner,
        title="pending dup pair",
        payload={"loser": loser, "winner": winner},
    )
    emit_proposal(store, candidate, rationale="looks duplicated")

    entries = merge_ledger(store)

    assert len(entries) == 1
    e = entries[0]
    assert e.verdict == "proposed"
    assert e.merge_id is None
    assert e.payload["loser"] == loser
