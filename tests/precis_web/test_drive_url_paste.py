"""Pasted URLs and ``/c/<record handle>`` resolution (gr484947).

Pasting a full UI URL (``https://<host>/c/qu459585``) into the /drive
search box used to feed the whole string to the chunk search — the
handle inside it was never seen. Now a lone-token URL whose path names a
real GET route navigates straight there; other "/"-bearing text stays a
plain search; and ``/c/<handle>`` resolves record handles (``qu``/``me``/…),
not just chunk handles.

Real-PG, like ``test_drive_identifier_redirect.py`` — the handle
resolution path needs a live store.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig


@pytest.fixture
def drive_client(runtime_with_store: Any, tmp_path: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _quest(store: Any) -> int:
    ref = store.insert_ref(kind="quest", slug=None, title="route test quest", meta={})
    return int(ref.id)


def _paper(store: Any, slug: str, doi: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"paper {slug}", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value) "
            "VALUES (%s, 'doi', %s)",
            (ref.id, doi),
        )
        conn.commit()
    return int(ref.id)


# ── pasted URLs ─────────────────────────────────────────────────────


def test_pasted_c_url_navigates(drive_client: TestClient, store: Any) -> None:
    rid = _quest(store)
    h = handle_registry.format_handle("quest", rid)
    resp = drive_client.get(
        "/drive",
        params={"q": f"https://melchior.example.ts.net/c/{h}"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/c/{h}"


def test_pasted_site_path_navigates(drive_client: TestClient, store: Any) -> None:
    """A bare ``/c/<handle>`` path (no host) resolves the same way."""
    rid = _quest(store)
    h = handle_registry.format_handle("quest", rid)
    resp = drive_client.get("/drive", params={"q": f"/c/{h}"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/c/{h}"


def test_pasted_url_with_query_and_fragment(drive_client: TestClient) -> None:
    resp = drive_client.get(
        "/drive",
        params={"q": "https://melchior.example.ts.net/drive?q=nano#top"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/drive?q=nano#top"


def test_unknown_path_is_plain_search(drive_client: TestClient) -> None:
    """A URL the app doesn't serve falls through to search — never a
    redirect to a 404."""
    resp = drive_client.get(
        "/drive",
        params={"q": "https://example.com/no/such/route"},
        follow_redirects=False,
    )
    assert resp.status_code == 200


def test_post_only_path_is_plain_search(drive_client: TestClient) -> None:
    """``/factory/llm/chain`` exists but is POST-only — a paste of it must
    not navigate (GET would 405); it stays a search."""
    resp = drive_client.get(
        "/drive",
        params={"q": "https://melchior.example.ts.net/factory/llm/chain"},
        follow_redirects=False,
    )
    assert resp.status_code == 200


def test_slash_text_is_plain_search(drive_client: TestClient) -> None:
    """Other text containing "/" is plain search; only a *leading* "/kind"
    token is a slash command."""
    for q in ("foo/bar baz", "a /b c", "not/a real/url"):
        resp = drive_client.get("/drive", params={"q": q}, follow_redirects=False)
        assert resp.status_code == 200, q


def test_bare_handle_still_resolves(drive_client: TestClient, store: Any) -> None:
    rid = _quest(store)
    h = handle_registry.format_handle("quest", rid)
    resp = drive_client.get("/drive", params={"q": h}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/refs/quest/{rid}"


def test_pasted_doi_url(drive_client: TestClient, store: Any) -> None:
    rid = _paper(store, "urlpaper2024", "10.1021/urlpaste.4c00001")
    resp = drive_client.get(
        "/drive",
        params={"q": "https://doi.org/10.1021/urlpaste.4c00001"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/papers/{rid}"


# ── /c/<handle> resolves record handles ─────────────────────────────


def test_c_record_handle_redirects(drive_client: TestClient, store: Any) -> None:
    rid = _quest(store)
    h = handle_registry.format_handle("quest", rid)
    resp = drive_client.get(f"/c/{h}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/r/quest/{rid}"


def test_c_record_handle_lands(drive_client: TestClient, store: Any) -> None:
    """The whole chain: ``/c/qu<n>`` → ``/r/quest/<n>`` → the quest page."""
    rid = _quest(store)
    h = handle_registry.format_handle("quest", rid)
    resp = drive_client.get(f"/c/{h}")
    assert resp.status_code == 200
    assert "route test quest" in resp.text


def test_c_unknown_handle_404s(drive_client: TestClient) -> None:
    resp = drive_client.get("/c/qu999999", follow_redirects=False)
    assert resp.status_code == 404
