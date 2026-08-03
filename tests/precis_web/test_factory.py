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

from precis_web.routes import factory as factory_routes


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


# ── §B-2 reserve mode (gr162694 #4) — the ONE door, never a raw UPSERT ──


def test_post_reserve_calls_the_one_door_helper(client, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def _fake_set_reserve(store, host, *, hours=4.0, actor=None):
        calls.append({"host": host, "hours": hours, "actor": actor})
        from datetime import UTC, datetime

        return datetime.now(UTC)

    monkeypatch.setattr(factory_routes, "set_reserve", _fake_set_reserve)
    resp = client.post(
        "/factory/reserve",
        data={"host": "melchior", "hours": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services&host=melchior"
    assert calls == [{"host": "melchior", "hours": 2.0, "actor": "web"}]


def test_post_reserve_refuses_bad_hours_without_500(client, monkeypatch) -> None:
    """The route never pre-clamps — a bad ``hours`` is ``set_reserve``'s own
    ``ValueError`` (mirroring the real helper's ``(0, 168]`` guard), caught
    and logged rather than crashing the request."""
    calls: list[float] = []

    def _fake_set_reserve(store, host, *, hours=4.0, actor=None):
        calls.append(hours)
        if not 0 < hours <= 168:
            raise ValueError(f"hours must be in (0, 168], got {hours}")
        return None

    monkeypatch.setattr(factory_routes, "set_reserve", _fake_set_reserve)
    resp = client.post(
        "/factory/reserve",
        data={"host": "melchior", "hours": "999"},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # never 500s
    assert calls == [999.0]  # reached the one door, which refused it


def test_post_release_calls_the_one_door_helper(client, monkeypatch) -> None:
    calls: list[str] = []

    def _fake_clear_reserve(store, host):
        calls.append(host)
        return True

    monkeypatch.setattr(factory_routes, "clear_reserve", _fake_clear_reserve)
    resp = client.post(
        "/factory/release", data={"host": "melchior"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/status?tab=services&host=melchior"
    assert calls == ["melchior"]
