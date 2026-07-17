"""Route-render tests for the factory console (slice 3).

The web ``client`` fixture is FakeStore-backed (every SQL returns empty),
so this exercises the static structure: the page renders, lists every
registry service grouped by category, and shows the host-strip empty
state — all without a DB. The SQL helpers are covered against real PG in
``tests/test_factory_helpers.py``.
"""

from __future__ import annotations

from precis.workers.registry import SERVICES


def test_factory_index_renders_and_lists_services(client) -> None:
    resp = client.get("/factory")
    assert resp.status_code == 200
    body = resp.text
    assert "Factory" in body
    # A sampling of services from different categories must surface.
    for name in ("embed", "chunk_keywords", "classify", "structural", "llama_swap"):
        assert name in body, name
    # Category headers render.
    for cat in ("discovery", "review", "serving"):
        assert cat in body


def test_factory_index_empty_host_strip(client) -> None:
    """With no heartbeats (FakeStore), the host strip shows its empty state."""
    resp = client.get("/factory")
    assert resp.status_code == 200
    assert "No host heartbeats" in resp.text


def test_factory_lists_every_registry_service(client) -> None:
    """Every ServiceSpec name appears — the console is a total view."""
    body = client.get("/factory").text
    for spec in SERVICES:
        assert spec.name in body, spec.name


def test_factory_host_selector_scopes_page(client) -> None:
    """?host= is echoed into the page (the edit scope)."""
    resp = client.get("/factory?host=melchior")
    assert resp.status_code == 200
    assert "melchior" in resp.text


# ── slice 4: the write endpoints (FakeStore — assert wiring + redirect) ──


def test_post_prio_redirects_to_selected_host(client) -> None:
    resp = client.post(
        "/factory/prio",
        data={"host": "melchior", "service": "classify", "prio": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/factory?host=melchior"


def test_post_model_redirects(client) -> None:
    resp = client.post(
        "/factory/model",
        data={"host": "*", "service": "briefing", "model": "claude-opus-4-8"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/factory?host=*"


def test_post_clear_redirects(client) -> None:
    resp = client.post(
        "/factory/clear",
        data={"host": "melchior", "service": "classify"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
