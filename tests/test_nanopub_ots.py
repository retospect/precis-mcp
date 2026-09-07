"""OTS batching, upgrade sweep, and the recompute audit. DB-backed;
the calendar is faked via the injectable ``submit``/``fetch_upgrade``
callables — no network anywhere."""

from __future__ import annotations

import hashlib
from typing import Any

from opentimestamps.core.notary import (
    BitcoinBlockHeaderAttestation,
    PendingAttestation,
)
from opentimestamps.core.timestamp import Timestamp

from precis.nanopub import mint, ots
from precis.nanopub.keys import generate_keypair
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper

_FAKE_CAL = "https://calendar.test"


def _fake_submit(_url: str, digest: bytes) -> Timestamp:
    ts = Timestamp(digest)
    ts.attestations.add(PendingAttestation(_FAKE_CAL))
    return ts


def _fake_upgrade(_url: str, commitment: bytes) -> Timestamp:
    ts = Timestamp(commitment)
    ts.attestations.add(BitcoinBlockHeaderAttestation(860000))
    return ts


def _fake_still_pending(_url: str, commitment: bytes) -> Timestamp:
    """A calendar round-trip that never completes — the stuck-forever
    shape: every fetch just hands back another pending attestation."""
    ts = Timestamp(commitment)
    ts.attestations.add(PendingAttestation(_FAKE_CAL))
    return ts


def _make_stuck(monkeypatch: Any) -> None:
    """Trip the stuck-pending age threshold immediately, without waiting
    real days — ``nanopub_ots_batches`` is append-only (no UPDATEing
    ``created_at`` to backdate it), so the sweep's own threshold is what
    moves instead."""
    monkeypatch.setattr(ots, "STUCK_PENDING_DAYS", -1)


def _signed_hub(store: Any, monkeypatch: Any, sentence: str) -> Any:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, sentence, paper, chunk)
    mint.approve(store, hub, payload=_payload(chunk), interactive=True)
    return mint.sign(store, hub)


def test_stamp_upgrade_and_audit_roundtrip(store: Any, monkeypatch: Any) -> None:
    row_a = _signed_hub(store, monkeypatch, "DFT finds OTS claim one holds.")
    row_b = _signed_hub(store, monkeypatch, "DFT finds OTS claim two holds.")

    batch_id = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert batch_id is not None

    for row in (row_a, row_b):
        refreshed = store.nanopub_publish_row_by_id(row.id)
        assert refreshed.state == "anchored"
        assert refreshed.batch_id == batch_id

    leaves = store.nanopub_batch_leaves(batch_id)
    assert len(leaves) == 2
    for leaf in leaves:
        art = store.nanopub_artifact(leaf.artifact_id)
        assert leaf.leaf_hash == hashlib.sha256(art.trig_bytes).hexdigest()
        # The leaf's serialized inclusion path reaches the recorded root.
        leaf_ts = ots._deserialize(leaf.path_proof, bytes.fromhex(leaf.leaf_hash))
        msgs = {msg for msg, _ in ots._walk(leaf_ts)}
        batch = next(b for b in store.nanopub_batches() if b.id == batch_id)
        assert bytes.fromhex(batch.merkle_root) in msgs

    # Nothing further waiting → no second batch.
    assert ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit) is None

    # Upgrade: pending → a new 'upgraded' proof row.
    assert store.nanopub_pending_batches() != []
    upgraded = ots.upgrade_sweep(store, fetch_upgrade=_fake_upgrade)
    assert upgraded == [batch_id]
    state, _proof = store.nanopub_latest_proof(batch_id)
    assert state == "upgraded"
    assert store.nanopub_pending_batches() == []

    assert ots.audit(store) == []


def test_stamp_skips_drifted_rows(store: Any, monkeypatch: Any) -> None:
    row = _signed_hub(
        store, monkeypatch, "DFT finds the drift-before-anchor claim holds."
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET title = 'Edited after signing.' WHERE ref_id = %s",
            (row.claim_ref_id,),
        )
    assert ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit) is None
    # Still signed, still unanchored — needs re-review, not a stale anchor.
    assert store.nanopub_publish_row_by_id(row.id).state == "signed"


def test_audit_flags_index_drift_and_alerts(store: Any) -> None:
    from tests.workers._helpers import seed_ref

    hub = seed_ref(store, title="Audit target.", kind="finding")
    prow = store.nanopub_create_publish_row(hub)
    store.nanopub_insert_artifact(
        publish_id=prow.id,
        claim_ref_id=hub,
        artifact_type="claim",
        trig_bytes=b"sub:claim rdfs:label 'x' .",  # not even TriG-with-prefixes
        trusty_uri="https://w3id.org/np/RAnotinbytes",
        aida_uri="http://purl.org/aida/Absent.",
        claim_sha="ab" * 8,
        signer="https://precis.retostamm.com/id/precis",
        key_fingerprint="ff" * 32,
        dois=["10.9/nowhere"],
    )
    findings = ots.audit(store)
    kinds = {f.kind for f in findings}
    # Either the parse fails outright or the extracts are flagged absent;
    # both are index-vs-bytes violations, and the audit alerted critical.
    assert kinds & {"parse", "trusty-uri", "aida-uri", "doi"}
    from precis.alerts import open_alert_severity

    assert (
        open_alert_severity(store, source="nanopub_ots", fingerprint="audit-mismatch")
        == "critical"
    )


def test_reopen_stuck_batch_only_touches_its_own_rows(
    store: Any, monkeypatch: Any
) -> None:
    """The stuck-pending alert names a re-stamp remedy (gr316504): without
    a dedicated reopen, ``anchored`` rows had no path back to ``signed``
    and the advertised fix was a dead end. This exercises the round trip
    the alert promises: reopen flips exactly the named batch's rows,
    leaves other anchored rows alone, and the freed rows are picked up by
    the next ``stamp_batch`` sweep."""
    row_a = _signed_hub(
        store, monkeypatch, "DFT finds the stuck-batch claim one holds."
    )
    row_b = _signed_hub(
        store, monkeypatch, "DFT finds the stuck-batch claim two holds."
    )
    stuck_batch = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert stuck_batch is not None

    # A second batch, anchored separately — must stay untouched.
    row_c = _signed_hub(store, monkeypatch, "DFT finds the other-batch claim holds.")
    other_batch = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert other_batch is not None
    assert other_batch != stuck_batch

    n = store.nanopub_reopen_stuck_batch(stuck_batch)
    assert n == 2

    for row in (row_a, row_b):
        refreshed = store.nanopub_publish_row_by_id(row.id)
        assert refreshed.state == "signed"
        assert refreshed.batch_id is None

    # The other batch's row is untouched.
    refreshed_c = store.nanopub_publish_row_by_id(row_c.id)
    assert refreshed_c.state == "anchored"
    assert refreshed_c.batch_id == other_batch

    # Re-running on an already-reopened batch is a no-op (nothing left in
    # 'anchored' bound to it).
    assert store.nanopub_reopen_stuck_batch(stuck_batch) == 0

    # The freed rows are picked back up into a fresh, later batch.
    fresh_batch = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert fresh_batch is not None
    assert fresh_batch not in (stuck_batch, other_batch)
    for row in (row_a, row_b):
        refreshed = store.nanopub_publish_row_by_id(row.id)
        assert refreshed.state == "anchored"
        assert refreshed.batch_id == fresh_batch

    # The old (stuck) batch's proof row is untouched — history, not deleted.
    old_state, _proof = store.nanopub_latest_proof(stuck_batch)
    assert old_state == "pending"


def test_upgrade_sweep_still_polls_batch_with_current_rows(
    store: Any, monkeypatch: Any
) -> None:
    """A batch with rows still bound to it is genuinely stuck work: the
    sweep must keep polling the calendar and can still raise the alert."""
    row = _signed_hub(store, monkeypatch, "DFT finds the still-bound claim holds.")
    batch_id = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert batch_id is not None
    _make_stuck(monkeypatch)

    calls: list[bytes] = []

    def _spy(url: str, commitment: bytes) -> Timestamp:
        calls.append(commitment)
        return _fake_still_pending(url, commitment)

    assert ots.upgrade_sweep(store, fetch_upgrade=_spy) == []
    assert len(calls) == 1  # the calendar was actually polled

    from precis.alerts import open_alert_severity

    assert store.nanopub_publish_row_by_id(row.id).batch_id == batch_id
    assert (
        open_alert_severity(
            store, source="nanopub_ots", fingerprint=f"stuck-pending:{batch_id}"
        )
        == "warn"
    )


def test_upgrade_sweep_skips_and_resolves_superseded_batch(
    store: Any, monkeypatch: Any
) -> None:
    """gr316504 residual: a re-stamp frees a stuck batch's rows, but the
    batch row itself is append-only history — its latest proof stays
    'pending' forever (the calendar lost that commitment). Once no
    ``nanopub_publish`` row references it any more, the sweep must stop
    polling it and resolve its stuck-pending alert instead of
    re-raising it every pass."""
    row_a = _signed_hub(store, monkeypatch, "DFT finds the ghost claim one holds.")
    row_b = _signed_hub(store, monkeypatch, "DFT finds the ghost claim two holds.")
    batch_id = ots.stamp_batch(store, calendar_url=_FAKE_CAL, submit=_fake_submit)
    assert batch_id is not None
    _make_stuck(monkeypatch)

    calls: list[bytes] = []

    def _spy(url: str, commitment: bytes) -> Timestamp:
        calls.append(commitment)
        return _fake_still_pending(url, commitment)

    fingerprint = f"stuck-pending:{batch_id}"
    from precis.alerts import open_alert_severity

    # First pass, rows still bound: raises the stuck-pending alert.
    assert ots.upgrade_sweep(store, fetch_upgrade=_spy) == []
    assert len(calls) == 1
    assert (
        open_alert_severity(store, source="nanopub_ots", fingerprint=fingerprint)
        == "warn"
    )

    # Operator/auto re-stamp: rows freed, batch left as history.
    assert store.nanopub_reopen_stuck_batch(batch_id) == 2
    for row in (row_a, row_b):
        refreshed = store.nanopub_publish_row_by_id(row.id)
        assert refreshed.state == "signed"
        assert refreshed.batch_id is None

    calls.clear()
    assert ots.upgrade_sweep(store, fetch_upgrade=_spy) == []
    assert calls == []  # no calendar poll attempted for the ghost batch
    assert (
        open_alert_severity(store, source="nanopub_ots", fingerprint=fingerprint)
        is None
    )

    # The old (superseded) batch's proof row is untouched — history, not
    # deleted or "fixed" — only the alert lifecycle changed.
    old_state, _proof = store.nanopub_latest_proof(batch_id)
    assert old_state == "pending"

    # A subsequent sweep does not flap the resolved alert back open.
    calls.clear()
    assert ots.upgrade_sweep(store, fetch_upgrade=_spy) == []
    assert calls == []
    assert (
        open_alert_severity(store, source="nanopub_ots", fingerprint=fingerprint)
        is None
    )


def test_sweep_pass_runs_audit_even_when_dark(store: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("PRECIS_OTS_ENABLED", raising=False)
    row = _signed_hub(store, monkeypatch, "DFT finds the dark-mode hub claim holds.")
    from precis.workers.ots_sweep import run_ots_sweep_pass

    result = run_ots_sweep_pass(store)
    # No stamping happened (dark), so the row stays signed…
    assert store.nanopub_publish_row_by_id(row.id).state == "signed"
    assert result.claimed == 0
    assert result.handler == "ots_sweep"
