"""The /nanopub review-and-sign surface: queue table, per-hub page,
interactive doors (approve/sign/signoff), and exact-bytes TriG serving.
Real DB-backed store (the routes run real SQL through the nanopub
mixin + overview/preflight modules)."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.nanopub.keys import generate_keypair
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_nanopub_gates_mint import _payload, _seed_hub, _seed_paper


@pytest.fixture
def client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _store(runtime: Any) -> Any:
    return runtime.store


def test_index_is_the_three_pane_tree(client: TestClient, runtime_with_store) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A queue-table claim.", paper, chunk)
    resp = client.get("/nanopub")
    assert resp.status_code == 200
    assert f"fi{hub}" in resp.text
    assert "unminted" in resp.text
    # Hub rows load the review pane; the two iframes are the panes.
    assert f'data-src="/nanopub/fi{hub}?embed=1"' in resp.text
    assert 'name="np-review"' in resp.text
    assert 'name="np-paper"' in resp.text
    # The old tree URL redirects home.
    tree = client.get("/nanopub/tree", follow_redirects=False)
    assert tree.status_code == 307 and tree.headers["location"] == "/nanopub"


def test_embed_mode_hides_the_site_chrome(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "An embedded claim.", paper, chunk)
    full = client.get(f"/nanopub/fi{hub}")
    framed = client.get(f"/nanopub/fi{hub}?embed=1")
    assert "<header" in full.text
    assert "<header" not in framed.text
    assert "An embedded claim." in framed.text


def test_hub_page_shows_state_and_action(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A reviewable claim.", paper, chunk)
    resp = client.get(f"/nanopub/fi{hub}")
    assert resp.status_code == 200
    assert "A reviewable claim." in resp.text
    assert "Approve" in resp.text  # unminted → approve action
    # Non-hub → friendly 404, not a 500.
    other = _seed_paper(store)[0]
    assert client.get(f"/nanopub/fi{other}").status_code == 404


def test_approve_prefill_suggests_quote_and_unique_snip(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A prefilled claim.", paper, chunk)
    resp = client.get(f"/nanopub/fi{hub}")
    assert resp.status_code == 200
    # The candidate quote is the first citation-marker-free sentence of
    # the grounding chunk ("Tensorial analysis." is too short), and the
    # snip is a validated-unique token window — not empty placeholders.
    assert "This anisotropy can reach a 400:1 ratio" in resp.text
    assert "this anisotropy can reach a 400 1 ratio" in resp.text


def test_prefill_covers_derived_from_lineage_anchor(
    client: TestClient, runtime_with_store
) -> None:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    store = _store(runtime_with_store)
    paper, _chunk, _sha = _seed_paper(store)
    hub = mint_hub(
        store, CanonicalClaim(sentence="A lineage-grounded claim.", scope={})
    )
    # No inbound evidence edge — the ONLY grounding is the outbound
    # lineage pin (dst_pos resolves to the chunk), fi19981's shape.
    store.add_link(src_ref_id=hub, dst_ref_id=paper, relation="derived-from", dst_pos=0)
    resp = client.get(f"/nanopub/fi{hub}")
    assert resp.status_code == 200
    assert "This anisotropy can reach a 400:1 ratio" in resp.text


def test_prefill_skips_heading_residue_and_ranks_by_claim(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(
        store,
        chunk_text=(
            "**1. Introduction**\n\nThe debate over such matters remains deeply "
            "contentious among practitioners. The anisotropy ratio doubles "
            "roughly every two years in layered crystals."
        ),
    )
    hub = _seed_hub(store, "Anisotropy ratio doubles every two years.", paper, chunk)
    resp = client.get(f"/nanopub/fi{hub}")
    assert resp.status_code == 200
    # The heading fragment is disqualified and the claim-relevant sentence
    # beats the earlier meta-discourse one.
    assert "The anisotropy ratio doubles roughly every two years" in resp.text
    assert "Introduction**" not in resp.text


def test_approve_sign_and_serve_trig(
    client: TestClient, runtime_with_store, monkeypatch: Any
) -> None:
    import json

    store = _store(runtime_with_store)
    priv, _pub = generate_keypair(2048)
    monkeypatch.setenv("NANOPUB_BOT_PRIVATE_KEY", priv)
    paper, chunk, sha = _seed_paper(store)
    hub = _seed_hub(store, "A web-signed claim.", paper, chunk)

    resp = client.post(
        f"/nanopub/fi{hub}/approve",
        data={
            "title": "A web-signed claim.",
            "payload": json.dumps(_payload(chunk, sha)),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert store.nanopub_publish_row(hub).state == "reviewed"

    resp = client.post(f"/nanopub/fi{hub}/sign", follow_redirects=False)
    assert resp.status_code == 303
    row = store.nanopub_publish_row(hub)
    assert row.state == "signed" and row.trusty_uri

    code = row.trusty_uri.rsplit("/", 1)[-1]
    trig = client.get(f"/np/{code}")
    assert trig.status_code == 200
    assert trig.headers["content-type"].startswith("application/trig")
    artifact = store.nanopub_artifact(row.artifact_id)
    assert trig.content == artifact.trig_bytes  # the exact frozen bytes
    assert client.get("/np/RAnope").status_code == 404
    # LIKE-wildcard probe: a bare '%' must 404, never match "any artifact".
    assert client.get("/np/%25").status_code == 404


def test_approve_gate_failure_is_a_400_not_a_500(
    client: TestClient, runtime_with_store
) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A gate-failing claim.", paper, chunk)
    resp = client.post(
        f"/nanopub/fi{hub}/approve",
        data={"title": "", "payload": '{"passages": []}'},
        follow_redirects=False,
    )
    assert resp.status_code == 400  # no-source-no-atom gate fires


def test_signoff_door_from_the_web(client: TestClient, runtime_with_store) -> None:
    from precis.nanopub.preflight import withheld_edges

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A signoff claim.", paper, chunk)
    edges = withheld_edges(store, hub)
    assert len(edges) == 1
    # Note required — refused loudly.
    resp = client.post(
        f"/nanopub/fi{hub}/signoff/{edges[0].link_id}",
        data={"note": " "},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    resp = client.post(
        f"/nanopub/fi{hub}/signoff/{edges[0].link_id}",
        data={"note": "read it, checks out"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert withheld_edges(store, hub) == []


def test_tree_nests_conjunct_atom_under_compound(
    client: TestClient, runtime_with_store
) -> None:
    from precis.nanopub.overview import hub_tree
    from precis.taproot.hub import link_claims

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    compound = _seed_hub(store, "A compound tree claim.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    atom = _seed_hub(store, "An atomic tree claim.", paper2, chunk2)
    assert link_claims(
        store, from_hub_ref_id=atom, to_hub_ref_id=compound, relation="conjunct-of"
    )

    roots = hub_tree(store)
    root_ids = {n.row.ref_id for n in roots}
    assert compound in root_ids and atom not in root_ids  # atom is nested
    node = next(n for n in roots if n.row.ref_id == compound)
    assert [c.row.ref_id for c in node.children] == [atom]
    assert node.children[0].relation == "conjunct-of"
    # Evidence papers hang as leaves on both nodes.
    assert {e.relation for e in node.evidence} == {"corroborates"}
    assert node.children[0].evidence

    resp = client.get("/nanopub")
    assert resp.status_code == 200
    assert f"fi{compound}" in resp.text and f"fi{atom}" in resp.text
    assert "A compound tree claim." in resp.text
    # Evidence leaves target the paper pane via the paper reader, not the
    # kindless /refs/<id> shape (which 400s).
    assert f'data-src="/papers/{paper}?embed=1"' in resp.text
    assert f"/refs/{paper}" not in resp.text
    # Both hubs load the review pane.
    assert f'data-src="/nanopub/fi{compound}?embed=1"' in resp.text
    assert f'data-src="/nanopub/fi{atom}?embed=1"' in resp.text


def test_tree_cycle_is_cut_not_recursed(client: TestClient, runtime_with_store) -> None:
    from precis.taproot.hub import link_claims

    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    a = _seed_hub(store, "Cycle claim A.", paper, chunk)
    paper2, chunk2, _sha2 = _seed_paper(store)
    b = _seed_hub(store, "Cycle claim B.", paper2, chunk2)
    assert link_claims(store, from_hub_ref_id=a, to_hub_ref_id=b, relation="refines")
    assert link_claims(store, from_hub_ref_id=b, to_hub_ref_id=a, relation="refines")

    resp = client.get("/nanopub")  # must terminate, not recurse forever
    assert resp.status_code == 200
    assert f"fi{a}" in resp.text and f"fi{b}" in resp.text
