"""Publish-time gates (preflight, trust allowlist, withheld edges) and
the registry POST module — all local: the "registry" is an injected
recorder, no network is ever touched."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.nanopub import mint, registry
from precis.nanopub.keys import generate_keypair
from precis.nanopub.preflight import publish_preflight, signoff_edge, withheld_edges
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper


def _signed_hub(store: Any, monkeypatch: Any, sentence: str) -> tuple[int, Any]:
    """Seed → approve → sign (bot key from env). Returns (hub, row)."""
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, sentence, paper, chunk)
    # The seeded evidence edge is unverified — verify it so the withheld
    # gate stays quiet in flows that aren't about it.
    with store.pool.connection() as conn:
        conn.execute(
            'UPDATE links SET meta = meta || \'{"support": "yes"}\'::jsonb '
            "WHERE dst_ref_id = %s AND relation = 'corroborates'",
            (hub,),
        )
    mint.approve(store, hub, payload=_payload(chunk, sha), interactive=True)
    row = mint.sign(store, hub, llm_models=[])
    return hub, row


def _anchor(store: Any, row: Any) -> None:
    artifact = store.nanopub_artifact(row.artifact_id)
    batch_id = store.nanopub_create_batch(
        merkle_root="ab" * 32,
        construction="test",
        calendar_url="none",
        leaves=[(artifact.id, 0, artifact.byte_sha256, b"leaf")],
        pending_proof=b"pending",
    )
    assert store.nanopub_set_batch(row.id, batch_id)


def _allow_signer(store: Any, row: Any, *, attesting: bool = True) -> None:
    artifact = store.nanopub_artifact(row.artifact_id)
    store.nanopub_allowlist_add(
        identity_uri=artifact.signer,
        key_fingerprint=artifact.key_fingerprint,
        attesting=attesting,
        note="test pin",
    )


def _checks(issues: list[Any]) -> set[str]:
    return {i.check for i in issues if i.blocking}


# ── preflight ───────────────────────────────────────────────────────────


def test_unminted_hub_has_no_publish_row(store: Any) -> None:
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "Never approved.", paper, chunk)
    assert _checks(publish_preflight(store, hub)) == {"state"}


def test_withheld_edge_blocks_and_signoff_clears(store: Any, monkeypatch: Any) -> None:
    hub, row = _signed_hub(
        store, monkeypatch, "DFT finds the withheld-edge claim holds."
    )
    _anchor(store, _refresh(store, hub))
    _allow_signer(store, row)
    # A second, unverified evidence edge arrives.
    paper2, chunk2, _sha2 = _seed_paper(store)
    from precis.taproot.hub import attach_evidence

    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper2,
        role="corroborates",
        check_retraction=False,
    )
    edges = withheld_edges(store, hub)
    assert len(edges) == 1 and edges[0].paper_ref_id == paper2
    assert "withheld-edge" in _checks(publish_preflight(store, hub))

    # No mute button: the sign-off is an interactive human act with a note.
    with pytest.raises(PermissionError):
        signoff_edge(store, edges[0].link_id, by="reto", note="checked")
    with pytest.raises(BadInput):
        signoff_edge(store, edges[0].link_id, by="reto", note="  ", interactive=True)
    assert signoff_edge(
        store, edges[0].link_id, by="reto", note="read the passage", interactive=True
    )
    assert withheld_edges(store, hub) == []
    assert "withheld-edge" not in _checks(publish_preflight(store, hub))


def test_trust_gate_requires_attesting_allowlist_entry(
    store: Any, monkeypatch: Any
) -> None:
    hub, _row = _signed_hub(
        store, monkeypatch, "DFT finds the trust-gated claim holds."
    )
    row = _refresh(store, hub)
    _anchor(store, row)
    # No allowlist entry at all → blocked.
    assert "trust" in _checks(publish_preflight(store, hub))
    # Non-attesting (bot) entry → still blocked: a bot signature alone
    # publishes nothing.
    _allow_signer(store, row, attesting=False)
    issues = publish_preflight(store, hub)
    assert any("non-attesting" in i.message for i in issues if i.check == "trust")
    # Attesting entry (upsert flips the same pair) → clear.
    _allow_signer(store, row, attesting=True)
    assert "trust" not in _checks(publish_preflight(store, hub))


def test_hanging_claim_is_unpublishable(store: Any, monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "DFT finds the hanging claim holds.", paper, chunk)
    mint.approve(
        store,
        hub,
        payload={"passages": [], "fields": {}, "hanging": True},
        interactive=True,
    )
    assert "hanging" in _checks(publish_preflight(store, hub))


def test_compound_dependency_must_be_published(store: Any, monkeypatch: Any) -> None:
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, sha = _seed_paper(store)
    atom = _seed_hub(store, "DFT finds the atom half holds.", paper, chunk)
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import link_claims, mint_hub

    compound = mint_hub(
        store, CanonicalClaim(sentence="DFT finds the compound whole holds.", scope={})
    )
    link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )
    with store.pool.connection() as conn:
        conn.execute(
            'UPDATE links SET meta = meta || \'{"support": "yes"}\'::jsonb '
            "WHERE dst_ref_id = %s",
            (atom,),
        )
    mint.approve(store, atom, payload=_payload(chunk, sha), interactive=True)
    mint.sign(store, atom, llm_models=[])
    mint.approve(store, compound, payload={"passages": []}, interactive=True)
    mint.sign(store, compound, llm_models=[])
    _anchor(store, _refresh(store, compound))
    _allow_signer(store, _refresh(store, compound))
    checks = _checks(publish_preflight(store, compound))
    assert "dependency-unpublished" in checks


# ── registry POST ───────────────────────────────────────────────────────


def _publishable_hub(store: Any, monkeypatch: Any, sentence: str) -> int:
    hub, _row = _signed_hub(store, monkeypatch, sentence)
    row = _refresh(store, hub)
    _anchor(store, row)
    _allow_signer(store, row)
    return hub


def _refresh(store: Any, hub: int) -> Any:
    row = store.nanopub_publish_row(hub)
    assert row is not None
    return row


def test_publish_needs_the_interactive_door(store: Any, monkeypatch: Any) -> None:
    hub = _publishable_hub(
        store, monkeypatch, "DFT finds the interactive-door claim holds."
    )
    with pytest.raises(PermissionError):
        registry.publish(store, hub, live=True)


def test_publish_dry_run_posts_nothing(store: Any, monkeypatch: Any) -> None:
    hub = _publishable_hub(store, monkeypatch, "DFT finds the dry-run claim holds.")
    posted: list[tuple[str, bytes]] = []
    result = registry.publish(
        store, hub, interactive=True, post=lambda u, b: posted.append((u, b))
    )
    assert not result.live and posted == []
    assert result.byte_count > 0 and result.trusty_uri.startswith(
        "https://w3id.org/np/"
    )
    assert _refresh(store, hub).state == "anchored"  # unchanged


def test_publish_live_posts_exact_bytes_and_flips_state(
    store: Any, monkeypatch: Any
) -> None:
    hub = _publishable_hub(
        store, monkeypatch, "DFT finds the live-publish claim holds."
    )
    row = _refresh(store, hub)
    artifact = store.nanopub_artifact(row.artifact_id)
    posted: list[tuple[str, bytes]] = []
    result = registry.publish(
        store, hub, live=True, interactive=True, post=lambda u, b: posted.append((u, b))
    )
    assert result.live and len(posted) == 1
    url, body = posted[0]
    assert url == registry.DEFAULT_REGISTRY_URL
    assert body == artifact.trig_bytes  # the exact stored bytes, no re-serialization
    refreshed = _refresh(store, hub)
    assert refreshed.state == "published"
    assert refreshed.published_at is not None
    assert refreshed.registry_url == registry.DEFAULT_REGISTRY_URL


def test_published_row_state_note_is_nonblocking(store: Any, monkeypatch: Any) -> None:
    hub = _publishable_hub(
        store, monkeypatch, "DFT finds the published-note claim holds."
    )
    registry.publish(store, hub, live=True, interactive=True, post=lambda u, b: None)
    state_issues = [i for i in publish_preflight(store, hub) if i.check == "state"]
    assert state_issues and not any(i.blocking for i in state_issues)
    assert "already published" in state_issues[0].message


def test_publish_blocked_on_preflight(store: Any, monkeypatch: Any) -> None:
    hub, _row = _signed_hub(
        store, monkeypatch, "DFT finds the blocked-publish claim holds."
    )
    # signed, not anchored, and no allowlist entry.
    with pytest.raises(registry.PublishBlocked) as exc:
        registry.publish(store, hub, live=True, interactive=True)
    assert {i.check for i in exc.value.issues} >= {"state", "trust"}
