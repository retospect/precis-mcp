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
