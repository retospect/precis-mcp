"""The /podcast feed + audio routes serve a private podcast over the web layer."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from precis import audio_feed
from precis_web.app import create_app
from precis_web.config import WebConfig


def _client(runtime, podcast_dir, base_url="https://host.tailnet.ts.net"):
    cfg = WebConfig(podcast_dir=podcast_dir, podcast_base_url=base_url)
    return TestClient(create_app(runtime=runtime, web_config=cfg))


def _seed(d, episode_id="e1", title="Morning brief"):
    (d / "src.mp3").write_bytes(b"ID3fakeaudio-bytes")
    return audio_feed.publish_episode(
        d,
        d / "src.mp3",
        episode_id=episode_id,
        title=title,
        description="today",
        published_at=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
        source="brief",
    )


def test_feed_renders_rss_with_absolute_enclosure(runtime, tmp_path):
    _seed(tmp_path)
    r = _client(runtime, tmp_path).get("/podcast/feed.xml")
    assert r.status_code == 200
    assert "application/rss+xml" in r.headers["content-type"]
    assert "Morning brief" in r.text
    # Enclosure URL carries the .mp3 filename so a shared link is unambiguous.
    assert 'url="https://host.tailnet.ts.net/podcast/audio/e1.mp3"' in r.text


def test_audio_route_streams_the_enclosure(runtime, tmp_path):
    _seed(tmp_path)
    client = _client(runtime, tmp_path)
    # The feed's filename URL streams the bytes with the audio content-type…
    r = client.get("/podcast/audio/e1.mp3")
    assert r.status_code == 200
    assert r.content == b"ID3fakeaudio-bytes"
    assert "audio/mpeg" in r.headers["content-type"]
    # …and the bare episode id still resolves (URLs cached before the extension).
    r2 = client.get("/podcast/audio/e1")
    assert r2.status_code == 200
    assert r2.content == b"ID3fakeaudio-bytes"


def test_unknown_episode_404(runtime, tmp_path):
    _seed(tmp_path)
    assert _client(runtime, tmp_path).get("/podcast/audio/nope").status_code == 404


def test_no_podcast_dir_empty_feed(runtime, tmp_path):
    cfg = WebConfig(podcast_dir=None)
    client = TestClient(create_app(runtime=runtime, web_config=cfg))
    r = client.get("/podcast/feed.xml")
    assert r.status_code == 200
    assert "<item>" not in r.text


def test_without_a_token_the_route_trusts_only_the_gate():
    """No ``?t=`` and no caller parked by the middleware: 401 + challenge,
    never a Basic parse of its own (gr730562). Only reachable if the
    middleware and the route ever disagree about "has a token"."""
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from precis_web.routes.podcast import _require_listener

    class _Store:
        def count_web_users(self, *, enabled_only=True):
            return 1

        def get_web_user_credentials(self, login):  # pragma: no cover
            raise AssertionError("the route must not authenticate by itself")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(runtime=SimpleNamespace(store=_Store()))
        ),
        query_params={},
        scope={},
        headers={"authorization": "Basic cmV0bzpwdw=="},
    )
    with pytest.raises(HTTPException) as err:
        _require_listener(request, WebConfig(auth_required=True))
    assert err.value.status_code == 401
    assert err.value.headers == {"WWW-Authenticate": 'Basic realm="precis"'}


def test_feed_token_path_is_unaffected_by_the_credential_class_allow_list(
    store, tmp_path, monkeypatch
):
    """``?t=`` on /podcast is a web credential, not an MCP bearer class: an
    OAuth-only deployment (``capability`` not accepted) still serves it."""
    from types import SimpleNamespace

    from precis.mcp_identity import mint_capability_token
    from precis.users import hash_password

    monkeypatch.setenv("PRECIS_MCP_CREDENTIAL_CLASSES", "oauth")
    alice = store.create_web_user(
        login="alice", abbrev="al", password=hash_password("pw-for-alice")
    )
    _, raw = mint_capability_token(store, alice.id, "phone app", ("feed",))
    _seed(tmp_path)
    cfg = WebConfig(
        podcast_dir=tmp_path,
        podcast_base_url="https://host.tailnet.ts.net",
        auth_required=True,
    )
    app = create_app(runtime=SimpleNamespace(store=store), web_config=cfg)
    assert "capability" not in app.state.mcp_credential_classes
    client = TestClient(app)
    ok = client.get(f"/podcast/feed.xml?t={raw}")
    assert ok.status_code == 200 and "Morning brief" in ok.text
    assert client.get("/podcast/feed.xml").status_code == 401
    assert client.get("/podcast/feed.xml?t=precis_cap_wrong").status_code == 401
