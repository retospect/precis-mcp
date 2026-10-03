"""``/drive?q=<identifier>`` jumps straight to the item (gr462129).

An exact universal handle (``pa5``, ``fi12``), a retired handle with a live
successor, a whole DOI, or a unique DOI prefix 302s to the item page the web
UI already uses for that kind, before any chunk search. An ambiguous DOI
prefix, an unknown handle/DOI, or anything else falls through to the normal
200 search page. Real-PG, like ``test_drive_sql.py`` (the FakeStore doesn't
parse SQL, so the DOI-prefix lookup needs the live ``store``).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.tags import _ref_url


@pytest.fixture
def drive_client(runtime_with_store: Any, tmp_path: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _paper(store: Any, slug: str, doi: str | None = None) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=f"paper {slug}", meta={})
    if doi is not None:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value) "
                "VALUES (%s, 'doi', %s)",
                (ref.id, doi),
            )
            conn.commit()
    return int(ref.id)


def _handle(kind: str, ref_id: int) -> str:
    return handle_registry.format_handle(kind, ref_id)


def _assert_redirect(resp: Any, kind: str, ref_id: int) -> None:
    assert resp.status_code == 302, f"expected 302, got {resp.status_code}"
    assert resp.headers["location"] == _ref_url(kind, ref_id)


def _assert_search_page(resp: Any) -> None:
    assert resp.status_code == 200
    assert "location" not in resp.headers


def test_paper_handle_redirects(store: Any, drive_client: TestClient) -> None:
    pid = _paper(store, "alpha2024")
    resp = drive_client.get(
        "/drive", params={"q": _handle("paper", pid)}, follow_redirects=False
    )
    _assert_redirect(resp, "paper", pid)


def test_non_paper_handle_redirects(store: Any, drive_client: TestClient) -> None:
    ref = store.insert_ref(kind="finding", slug=None, title="a finding", meta={})
    resp = drive_client.get(
        "/drive", params={"q": _handle("finding", ref.id)}, follow_redirects=False
    )
    _assert_redirect(resp, "finding", ref.id)


def test_handle_with_surrounding_whitespace_redirects(
    store: Any, drive_client: TestClient
) -> None:
    pid = _paper(store, "padded2024")
    resp = drive_client.get(
        "/drive", params={"q": f"  {_handle('paper', pid)} \t"}, follow_redirects=False
    )
    _assert_redirect(resp, "paper", pid)


def test_other_query_params_do_not_block_redirect(
    store: Any, drive_client: TestClient
) -> None:
    pid = _paper(store, "facets2024")
    resp = drive_client.get(
        "/drive",
        params={
            "q": _handle("paper", pid),
            "k": "paper",
            "paper_chunks": "both",
            "state": "all",
            "folder": "*",
        },
        follow_redirects=False,
    )
    _assert_redirect(resp, "paper", pid)


def test_unknown_handle_falls_through_to_search(drive_client: TestClient) -> None:
    resp = drive_client.get(
        "/drive", params={"q": "pa99999999"}, follow_redirects=False
    )
    _assert_search_page(resp)


def test_retired_handle_redirects_to_live_successor(
    store: Any, drive_client: TestClient
) -> None:
    old = _paper(store, "dup-old2024")
    live = _paper(store, "dup-live2024")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now(), "
            "meta = meta || jsonb_build_object('superseded_by', %s::text) "
            "WHERE ref_id = %s",
            (live, old),
        )
        conn.commit()
    resp = drive_client.get(
        "/drive", params={"q": _handle("paper", old)}, follow_redirects=False
    )
    _assert_redirect(resp, "paper", live)


def test_whole_doi_redirects(store: Any, drive_client: TestClient) -> None:
    pid = _paper(store, "whole2023", doi="10.1021/acscatal.3c01963")
    resp = drive_client.get(
        "/drive", params={"q": "10.1021/acscatal.3c01963"}, follow_redirects=False
    )
    _assert_redirect(resp, "paper", pid)


def test_unique_doi_prefix_redirects(store: Any, drive_client: TestClient) -> None:
    pid = _paper(store, "prefix2023", doi="10.1021/acscatal.3c01963")
    _paper(store, "other2023", doi="10.1038/nature12345")
    resp = drive_client.get(
        "/drive", params={"q": "10.1021/acscatal.3c0196"}, follow_redirects=False
    )
    _assert_redirect(resp, "paper", pid)


def test_ambiguous_doi_prefix_is_not_a_redirect(
    store: Any, drive_client: TestClient
) -> None:
    _paper(store, "twin-a2023", doi="10.1021/acscatal.3c01963")
    _paper(store, "twin-b2023", doi="10.1021/acscatal.3c01964")
    resp = drive_client.get(
        "/drive", params={"q": "10.1021/acscatal.3c0196"}, follow_redirects=False
    )
    _assert_search_page(resp)


def test_unknown_doi_falls_through_to_search(
    store: Any, drive_client: TestClient
) -> None:
    _paper(store, "known2023", doi="10.1021/acscatal.3c01963")
    resp = drive_client.get(
        "/drive", params={"q": "10.9999/does.not.exist"}, follow_redirects=False
    )
    _assert_search_page(resp)
