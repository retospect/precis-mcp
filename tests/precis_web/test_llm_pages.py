"""``/llm`` human pages — the chain/op routing baseline (gr484946).

``/llm/chain`` and ``/llm/op`` only ever existed as ``POST /factory/llm/*``
editor endpoints, so the guessed GETs 404'd on prod. These pin the
read-only GET pages: the section renders on both store fakes, and every
``/llm…`` link the nav chrome serves resolves (never 404).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig

_LLM_HREF = re.compile(r'href="(/llm[^"]*)"')


@pytest.fixture
def llm_client(runtime_with_store: Any, tmp_path: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_llm_pages_render_fake(client: TestClient) -> None:
    """Every /llm page renders on the FakeStore (degraded ctx paths)."""
    for path in ("/llm", "/llm/", "/llm/chain", "/llm/op"):
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)


def test_llm_pages_render_real(llm_client: TestClient) -> None:
    """Same against real PG — exercises ``_llm_op_stats``'s SQL and the
    live ``resolve_chain`` walk (no rows needed; defaults render)."""
    for path in ("/llm/chain", "/llm/op"):
        resp = llm_client.get(path)
        assert resp.status_code == 200, (path, resp.status_code)


def test_llm_chain_shows_rungs(llm_client: TestClient) -> None:
    """The chain page is the rung/placement/slot baseline the gripe asked
    for: one card per capability tier, rung slots numbered in order."""
    resp = llm_client.get("/llm/chain")
    assert resp.status_code == 200
    for tier in ("frontier", "big", "medium", "small"):
        assert tier in resp.text


def test_llm_nav_links_never_404(client: TestClient) -> None:
    """Walk every ``/llm`` href the chrome + section pages emit."""
    seen: set[str] = set()
    queue = ["/drive"]  # any page carries the Ops dropdown
    while queue:
        resp = client.get(queue.pop())
        assert resp.status_code == 200
        for href in _LLM_HREF.findall(resp.text):
            path = href.split("#", 1)[0]
            if path in seen:
                continue
            seen.add(path)
            queue.append(path)
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
    # Sanity: the section really is linked — Ops dropdown + cross-links.
    assert "/llm" in seen
    assert "/llm/chain" in seen
    assert "/llm/op" in seen
    assert "/llm/rules" in seen


def test_llm_rules_page_renders(client: TestClient) -> None:
    """The keyword-rules section renders (empty table) on the FakeStore —
    the snapshot fails open to the (empty) code defaults."""
    resp = client.get("/llm/rules")
    assert resp.status_code == 200
    assert "Keyword rules" in resp.text


def test_keyword_route_preview_no_match(client: TestClient) -> None:
    """The preview endpoint degrades to ``matched: false``, never a 4xx."""
    resp = client.get("/api/llm/keyword-route", params={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"matched": False}


def test_keyword_route_preview_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seeded rule fires in the preview and carries its resolved plan."""
    from precis.utils.llm import keyword_route

    rule = keyword_route.KeywordRule(
        rule_id="3",
        pattern="opus",
        is_regex=False,
        target={"model": "claude-opus-4-8"},
        compiled=keyword_route._compile("opus", False, "3"),
    )
    monkeypatch.setattr(keyword_route, "_refresh_snapshot", lambda: [rule])
    try:
        resp = client.get("/api/llm/keyword-route", params={"text": "ask opus"})
    finally:
        keyword_route.reset_cache()
    assert resp.status_code == 200
    row = resp.json()
    assert row["matched"] is True
    assert row["rule_id"] == "3"
    assert row["target"] == {"model": "claude-opus-4-8"}
    # The plan either names the model or the reason it can't run here —
    # both are informative preview output, never an error.
    assert "model" in row or "fallback" in row


def test_llm_rule_posts_never_500(client: TestClient) -> None:
    """The write endpoints degrade to a redirect when the store can't
    take the write (FakeStore has no pool)."""
    resp = client.post(
        "/llm/rules/add",
        data={
            "pattern": "opus",
            "target_kind": "model",
            "target_value": "claude-opus-4-8",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.post(
        "/llm/rules/toggle",
        data={"rule_id": "1", "enabled": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp = client.post(
        "/llm/rules/move",
        data={"rule_id": "1", "direction": "up"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
