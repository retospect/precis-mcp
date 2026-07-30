"""``/claim/<head>`` full-page view + ``/preview/claim/<head>`` hover fragment —
both cite-head forms (``fi<id>`` and ``<pub_id>``), the inflight/print-set
rendering, and the non-hub-finding "missing" fallback."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig

_CLAIM = CanonicalClaim(
    sentence="Pd/C catalyzes Suzuki coupling at room temperature with a mild base.",
    scope={"material": "Pd/C", "method": "Suzuki coupling", "regime": "RT"},
)


def _hub_pub_id(store, hub_ref_id: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'pub_id'",
            (hub_ref_id,),
        ).fetchone()
    assert row is not None, f"no pub_id minted for hub ref_id={hub_ref_id}"
    return str(row[0])


@pytest.fixture
def claim_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_hub(hub: Hub) -> tuple[int, str]:
    """Mint a claim hub with a derived (cited) originator. Returns
    ``(hub_ref_id, pub_id)``."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig", title="The original report", year=2001
    ).id
    follower = store.insert_ref(
        kind="paper", slug="claim-follower", title="Follows the original", year=2005
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": "pc999"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    store.add_link(src_ref_id=follower, dst_ref_id=originator, relation="cites")
    return claim_hub, _hub_pub_id(store, claim_hub)


def test_claim_view_by_fi_handle(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text
    assert "★" in r.text


def test_claim_view_originator_handle_and_star(
    claim_client: TestClient, hub: Hub
) -> None:
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    originator = store.insert_ref(
        kind="paper", slug="claim-orig2", title="The original report v2", year=2002
    ).id
    follower = store.insert_ref(
        kind="paper", slug="claim-follower2", title="Follows v2", year=2006
    ).id
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=originator,
        role="corroborates",
        meta={"source_handle": "pc998"},
    )
    attach_evidence(
        store, hub_ref_id=claim_hub, paper_ref_id=follower, role="corroborates"
    )
    store.add_link(src_ref_id=follower, dst_ref_id=originator, relation="cites")

    fi_handle = handle_registry.format_handle("finding", claim_hub)
    originator_handle = handle_registry.format_handle("paper", originator)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text
    assert originator_handle in r.text
    assert "★" in r.text


def test_claim_view_by_pub_id(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, pub_id = _seed_hub(hub)

    r = claim_client.get(f"/claim/{pub_id}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text


def test_claim_preview_fragment(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert _CLAIM.sentence in r.text


def test_claim_view_non_hub_finding_shows_missing(
    claim_client: TestClient, hub: Hub
) -> None:
    store = hub.store
    finding = store.insert_ref(
        kind="finding", slug=None, title="An ordinary finding", meta={}
    ).id
    fi_handle = handle_registry.format_handle("finding", finding)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "No claim hub" in r.text
