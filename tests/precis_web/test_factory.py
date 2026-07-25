"""Route-render tests for the factory console (slice 3).

WS3 folded ``GET /factory`` into the Services sub-tab of the merged
System page (``/status?tab=services`` — see ``tests/precis_web/
test_routes.py`` for the render assertions: every registry service
listed, category headers, host selector). ``GET /factory`` now just
redirects there; what's left here is the redirect itself plus the write
endpoints (which still live at ``/factory/{prio,model,clear}``). The SQL
helpers are covered against real PG in ``tests/test_factory_helpers.py``.
"""

from __future__ import annotations


def test_factory_old_url_redirects_to_system_services_tab(client) -> None:
    resp = client.get("/factory", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/status?tab=services&host=*"


def test_factory_old_url_redirect_preserves_host(client) -> None:
    resp = client.get("/factory?host=melchior", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/status?tab=services&host=melchior"


# ── slice 4: the write endpoints (FakeStore — assert wiring + redirect) ──


def test_post_prio_redirects_to_services_tab(client) -> None:
    resp = client.post(
        "/factory/prio",
        data={"host": "melchior", "service": "classify", "prio": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services&host=melchior"


def test_post_model_redirects(client) -> None:
    resp = client.post(
        "/factory/model",
        data={"host": "*", "service": "briefing", "model": "claude-opus-4-8"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services&host=*"


def test_post_clear_redirects(client) -> None:
    resp = client.post(
        "/factory/clear",
        data={"host": "melchior", "service": "classify"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services&host=melchior"
