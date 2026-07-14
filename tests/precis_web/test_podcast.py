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
    (d / "src.m4a").write_bytes(b"ID3fakeaudio-bytes")
    return audio_feed.publish_episode(
        d,
        d / "src.m4a",
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
    assert 'url="https://host.tailnet.ts.net/podcast/audio/e1"' in r.text


def test_audio_route_streams_the_enclosure(runtime, tmp_path):
    _seed(tmp_path)
    r = _client(runtime, tmp_path).get("/podcast/audio/e1")
    assert r.status_code == 200
    assert r.content == b"ID3fakeaudio-bytes"


def test_unknown_episode_404(runtime, tmp_path):
    _seed(tmp_path)
    assert _client(runtime, tmp_path).get("/podcast/audio/nope").status_code == 404


def test_no_podcast_dir_empty_feed(runtime, tmp_path):
    cfg = WebConfig(podcast_dir=None)
    client = TestClient(create_app(runtime=runtime, web_config=cfg))
    r = client.get("/podcast/feed.xml")
    assert r.status_code == 200
    assert "<item>" not in r.text
