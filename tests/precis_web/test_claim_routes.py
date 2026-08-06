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
    # The evidence-row paper link targets the shared 'precis-paper' window
    # (B) so clicking a source from the claim window reuses ONE paper window
    # instead of navigating the claim page away.
    assert f'href="/r/paper/{originator}" target="precis-paper"' in r.text


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


def _seed_hub_with_chunk(hub: Hub) -> tuple[int, str, str]:
    """Mint a claim hub whose corroborating edge grounds at a REAL paper
    chunk. Returns ``(hub_ref_id, chunk_handle, chunk_text)``."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)
    paper = store.insert_ref(
        kind="paper", slug="claim-grounded", title="The grounded report", year=2003
    ).id
    chunk_text = "Pd/C converts aryl halides at 25 °C when K2CO3 is present."
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (paper, chunk_text),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    chunk_handle = handle_registry.format_handle("paper", chunk_id, chunk=True)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": chunk_handle},
    )
    return claim_hub, chunk_handle, chunk_text


def test_claim_view_grounding_passage_linked_and_quoted(
    claim_client: TestClient, hub: Hub
) -> None:
    hub_ref_id, chunk_handle, chunk_text = _seed_hub_with_chunk(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Grounding passages" in r.text
    assert chunk_text in r.text
    assert f"/c/{chunk_handle}" in r.text  # the chunk is clickable


def test_claim_view_dangling_source_handle_degrades(
    claim_client: TestClient, hub: Hub
) -> None:
    # _seed_hub grounds at pc999, which has no chunks row in the test DB.
    hub_ref_id, _pub_id = _seed_hub(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    assert "Grounding passages" in r.text
    assert "/c/pc999" in r.text  # still clickable — hover degrades server-side
    assert "passage text not available" in r.text


def test_claim_preview_lists_cited_chunks(claim_client: TestClient, hub: Hub) -> None:
    hub_ref_id, chunk_handle, chunk_text = _seed_hub_with_chunk(hub)
    fi_handle = handle_registry.format_handle("finding", hub_ref_id)

    r = claim_client.get(f"/preview/claim/{fi_handle}")

    assert r.status_code == 200
    assert f"/c/{chunk_handle}" in r.text
    assert chunk_text[:80] in r.text


def _insert_chunk(store, *, ref_id: int, ord: int, text: str) -> str:
    """Insert a real paper chunk, return its universal chunk handle — the
    grounding-quote render fix (docs/proposals/smartdraft-claim-ux.md slice
    1) needs REAL chunk text, not the ``pc999``-dangling-handle shape
    `_seed_hub` uses."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord, text),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def test_claim_view_renders_table_math_and_all_three_passages(
    claim_client: TestClient, hub: Hub
) -> None:
    """A hub grounded by three distinct passages — one plain, one a
    markdown pipe table, one carrying ``$…$`` TeX — across all three
    evidence roles (originator/corroborator/contradictor). The full
    ``/claim/<head>`` render must: turn the table into a real ``<table>``
    (not one flattened pipe run — fi191167), mark the quote container
    ``tex-scope`` so client KaTeX picks up the TeX span, list all three
    passages (not just the ★ print set), and leave the claim TITLE as
    plain text (titles never get math-processed — the "Already decided"
    policy)."""
    store = hub.store
    claim_hub = mint_hub(store, _CLAIM)

    plain_paper = store.insert_ref(
        kind="paper", slug="claim-plain", title="Plain-passage paper", year=2010
    ).id
    plain_text = "Pd/C converts aryl halides at 25 °C when K2CO3 is present."
    plain_handle = _insert_chunk(store, ref_id=plain_paper, ord=0, text=plain_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=plain_paper,
        role="establishes",
        meta={"source_handle": plain_handle},
    )

    table_paper = store.insert_ref(
        kind="paper", slug="claim-table", title="Table-passage paper", year=2011
    ).id
    table_text = (
        "| Catalyst | Yield (%) |\n| --- | --- |\n| Pd/C | 92 |\n| Pd(OAc)2 | 78 |"
    )
    table_handle = _insert_chunk(store, ref_id=table_paper, ord=0, text=table_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=table_paper,
        role="corroborates",
        meta={"source_handle": table_handle},
    )
    # Seniority is DERIVED from the intra-supporter citation graph, not the
    # write-time role (`seniority.py`) — without a `cites` edge among the
    # supporters, both would fall back to "corroborator". table_paper
    # citing plain_paper makes plain_paper the derived originator.
    store.add_link(src_ref_id=table_paper, dst_ref_id=plain_paper, relation="cites")

    tex_paper = store.insert_ref(
        kind="paper", slug="claim-tex", title="TeX-passage paper", year=2012
    ).id
    tex_text = "The rate constant scales as $k = A e^{-E_a/RT}$ across the series."
    tex_handle = _insert_chunk(store, ref_id=tex_paper, ord=0, text=tex_text)
    attach_evidence(
        store,
        hub_ref_id=claim_hub,
        paper_ref_id=tex_paper,
        role="contradicts",
        meta={"source_handle": tex_handle},
    )

    fi_handle = handle_registry.format_handle("finding", claim_hub)

    r = claim_client.get(f"/claim/{fi_handle}")

    assert r.status_code == 200
    body = r.text
    assert "<table" in body
    assert 'class="tex-scope' in body
    # All three distinct grounding passages are listed, not just the ★
    # print set (originator only).
    assert f"/c/{plain_handle}" in body
    assert f"/c/{table_handle}" in body
    assert f"/c/{tex_handle}" in body
    assert plain_text in body
    assert "(originator)" in body
    assert "(corroborator)" in body
    assert "(contradictor)" in body
    # The claim title stays plain text — no math engine over it.
    title_line = next(line for line in body.splitlines() if "<h1" in line)
    assert "tex-scope" not in title_line


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
