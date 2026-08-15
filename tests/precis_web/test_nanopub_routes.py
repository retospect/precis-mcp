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


def test_queue_table_buckets_and_flags(client: TestClient, runtime_with_store) -> None:
    store = _store(runtime_with_store)
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(store, "A queue-table claim.", paper, chunk)
    resp = client.get("/nanopub")
    assert resp.status_code == 200
    assert f"fi{hub}" in resp.text
    assert "unminted" in resp.text


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
