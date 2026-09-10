"""Nanopub publish-row + proof-store ops (migration 0128). DB-backed.

Pins the two DB-enforced invariants: at most one non-terminal publish
row per hub (partial unique index) and append-only proof-store tables
(BEFORE UPDATE OR DELETE triggers raise, owner included)."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import RaiseException, UniqueViolation

from tests.workers._helpers import seed_ref


def _artifact(store: Any, publish_id: int, ref_id: int, n: int = 1) -> int:
    return store.nanopub_insert_artifact(
        publish_id=publish_id,
        claim_ref_id=ref_id,
        artifact_type="claim",
        trig_bytes=f"sub:claim rdfs:label 'artifact {n}' .".encode(),
        trusty_uri=f"https://w3id.org/np/RAtest{publish_id}x{n}",
        aida_uri="http://purl.org/aida/Test.",
        claim_sha="ab" * 8,
        signer="https://precis.retostamm.com/id/precis",
        key_fingerprint="ff" * 32,
        dois=["10.1/x"],
    )


def test_publish_row_lifecycle_and_cardinality(store: Any) -> None:
    hub = seed_ref(store, title="A claim.", kind="finding")
    row = store.nanopub_create_publish_row(hub)
    assert row.state == "candidate"
    assert store.nanopub_publish_row(hub).id == row.id

    # Second live row for the same hub: the partial unique index refuses.
    with pytest.raises(UniqueViolation):
        store.nanopub_create_publish_row(hub)

    assert store.nanopub_approve(
        row.id,
        approved_title="A claim.",
        claim_sha="cd" * 8,
        aida_uri="http://purl.org/aida/A%20claim.",
        grounding={"passages": []},
    )
    refreshed = store.nanopub_publish_row(hub)
    assert refreshed.state == "reviewed"
    assert refreshed.approved_title == "A claim."

    # CAS: approving again from 'reviewed' is a no-op returning False.
    assert not store.nanopub_approve(
        row.id,
        approved_title="Другой.",
        claim_sha="ee" * 8,
        aida_uri="x",
        grounding={},
    )

    art = _artifact(store, row.id, hub)
    assert store.nanopub_record_signed(
        row.id,
        trusty_uri=f"https://w3id.org/np/RAtest{row.id}x1",
        artifact_id=art,
        dependency_codes={},
    )
    assert store.nanopub_publish_row(hub).state == "signed"

    # Reopen discards the frozen fields but never the artifact row.
    assert store.nanopub_reopen(row.id)
    reopened = store.nanopub_publish_row(hub)
    assert reopened.state == "candidate"
    assert reopened.approved_title is None and reopened.artifact_id is None
    assert store.nanopub_artifact(art) is not None

    # A terminal row frees the slot for a new live one.
    assert store.nanopub_transition(row.id, to_state="rejected", expect=("candidate",))
    row2 = store.nanopub_create_publish_row(hub)
    assert row2.id != row.id


def test_artifacts_are_append_only(store: Any) -> None:
    hub = seed_ref(store, title="B claim.", kind="finding")
    row = store.nanopub_create_publish_row(hub)
    art = _artifact(store, row.id, hub)

    with pytest.raises(RaiseException):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE nanopub_artifacts SET aida_uri = 'tampered' WHERE id = %s",
                (art,),
            )
    with pytest.raises(RaiseException):
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM nanopub_artifacts WHERE id = %s", (art,))

    stored = store.nanopub_artifact(art)
    assert stored is not None
    import hashlib

    assert stored.byte_sha256 == hashlib.sha256(stored.trig_bytes).hexdigest()


def test_batch_family_is_append_only_and_upgrade_inserts(store: Any) -> None:
    hub = seed_ref(store, title="C claim.", kind="finding")
    row = store.nanopub_create_publish_row(hub)
    art = _artifact(store, row.id, hub)

    batch = store.nanopub_create_batch(
        merkle_root="ab" * 32,
        construction="test construction rule",
        calendar_url="https://calendar.test",
        leaves=[(art, 0, "cd" * 32, b"leafproof")],
        pending_proof=b"rootproof-pending",
    )
    # nanopub_pending_batches() only counts a batch a current publish row
    # still points at (a re-stamped-away batch is superseded, not
    # pending work — see test_nanopub_ots.py); wire the binding directly
    # since this test's row is deliberately kept at 'candidate' below,
    # short of the 'signed' state nanopub_set_batch's CAS requires.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE nanopub_publish SET batch_id = %s WHERE id = %s",
            (batch, row.id),
        )
    assert [b.id for b in store.nanopub_pending_batches()] == [batch]
    state, proof = store.nanopub_latest_proof(batch)
    assert state == "pending" and proof == b"rootproof-pending"

    # The upgrade is an INSERT; the pending row remains as history.
    store.nanopub_add_proof(batch, state="upgraded", ots_proof=b"rootproof-upgraded")
    assert store.nanopub_pending_batches() == []
    state, proof = store.nanopub_latest_proof(batch)
    assert state == "upgraded" and proof == b"rootproof-upgraded"

    for sql in (
        "UPDATE nanopub_ots_batches SET merkle_root = 'x' WHERE id = %s",
        "DELETE FROM nanopub_ots_leaves WHERE batch_id = %s",
        "UPDATE nanopub_ots_proofs SET state = 'pending' WHERE batch_id = %s",
    ):
        with pytest.raises(RaiseException):
            with store.pool.connection() as conn:
                conn.execute(sql, (batch,))

    leaves = store.nanopub_batch_leaves(batch)
    assert len(leaves) == 1 and leaves[0].artifact_id == art

    # Anchored publish-row flip is CAS'd on 'signed'.
    assert not store.nanopub_set_batch(row.id, batch)  # still candidate


def test_publish_states_bulk_prefers_live_row_over_terminal(store: Any) -> None:
    """One query, many hubs: live row wins over any terminal history; a
    hub with only terminal rows reports the latest one; a hub with no
    rows is absent; empty input short-circuits."""
    # hub_a: rejected once, then a fresh live candidate — live wins.
    hub_a = seed_ref(store, title="Bulk A.", kind="finding")
    row_a1 = store.nanopub_create_publish_row(hub_a)
    store.nanopub_transition(row_a1.id, to_state="reviewed", expect=("candidate",))
    store.nanopub_transition(row_a1.id, to_state="rejected", expect=("reviewed",))
    row_a2 = store.nanopub_create_publish_row(hub_a)
    # hub_b: terminal only.
    hub_b = seed_ref(store, title="Bulk B.", kind="finding")
    row_b = store.nanopub_create_publish_row(hub_b)
    store.nanopub_transition(row_b.id, to_state="reviewed", expect=("candidate",))
    store.nanopub_transition(row_b.id, to_state="rejected", expect=("reviewed",))
    # hub_c: no publish row.
    hub_c = seed_ref(store, title="Bulk C.", kind="finding")

    states = store.nanopub_publish_states_bulk([hub_a, hub_b, hub_c])

    assert states[hub_a] == (
        "candidate",
        store.nanopub_publish_row_by_id(row_a2.id).updated_at,
    )
    assert states[hub_b][0] == "rejected"
    assert hub_c not in states
    assert store.nanopub_publish_states_bulk([]) == {}


# ── gr279770 sweep half: candidate discard + regate read ──────────────


def test_discard_candidate_deletes_the_row_and_frees_the_slot(store: Any) -> None:
    """A discard is a CAS delete, not a state flip — no state in
    ``nanopub.state.STATES`` fits "staged, no longer qualifies" that isn't
    ``candidate`` itself or a human's terminal ``rejected``. Deleting frees
    the partial-unique-index slot exactly like a terminal transition does,
    so the hub restages through the ordinary path with no unblock step."""
    hub = seed_ref(store, title="Discard me.", kind="finding")
    row = store.nanopub_create_publish_row(hub)

    assert store.nanopub_discard_candidate(row.id)

    assert store.nanopub_publish_row(hub) is None
    row2 = store.nanopub_create_publish_row(hub)
    assert row2.id != row.id


def test_discard_candidate_is_cas_scoped_to_candidate(store: Any) -> None:
    """A row that already left ``candidate`` (e.g. a reviewer approved it
    between the sweep's read and this write) is left alone — the caller
    loses the race harmlessly."""
    hub = seed_ref(store, title="Already reviewed.", kind="finding")
    row = store.nanopub_create_publish_row(hub)
    store.nanopub_transition(row.id, to_state="reviewed", expect=("candidate",))

    assert not store.nanopub_discard_candidate(row.id)
    assert store.nanopub_publish_row(hub).state == "reviewed"


def test_discard_candidate_unknown_row_is_false(store: Any) -> None:
    assert not store.nanopub_discard_candidate(-1)


def test_candidate_regate_rows_reads_disputed_and_canonical(store: Any) -> None:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    clean = mint_hub(store, CanonicalClaim(sentence="A clean claim.", scope={}))
    clean_row = store.nanopub_create_publish_row(clean, artifact_type="claim")
    disputed = mint_hub(store, CanonicalClaim(sentence="A disputed claim.", scope={}))
    disputed_row = store.nanopub_create_publish_row(disputed, artifact_type="claim")
    other = seed_ref(store, title="opposing paper", kind="paper")
    store.add_link(
        src_ref_id=other, dst_ref_id=disputed, relation="contradicts", set_by="agent"
    )
    # Not staged: a reviewed row must not show up in the candidate regate set.
    reviewed = mint_hub(store, CanonicalClaim(sentence="A reviewed claim.", scope={}))
    reviewed_row = store.nanopub_create_publish_row(reviewed, artifact_type="claim")
    store.nanopub_transition(
        reviewed_row.id, to_state="reviewed", expect=("candidate",)
    )

    rows = {r.publish_id: r for r in store.nanopub_candidate_regate_rows()}

    assert clean_row.id in rows
    assert rows[clean_row.id].ref_id == clean
    assert rows[clean_row.id].disputed is False
    assert rows[clean_row.id].canonical is True
    assert rows[disputed_row.id].disputed is True
    assert reviewed_row.id not in rows
