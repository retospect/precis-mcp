"""The ``/refs/youtube/<id>`` detail page renders a watch link + thumbnail.

A YouTube ref's transcript body isn't enough — the detail page should
offer a working click-through to the video and show its thumbnail (a
"screenshot"). The scraped meta lives in ``cache_state.meta``, so the
route pulls the cache row; a missing scrape falls back to YouTube's
deterministic per-video thumbnail URL.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_youtube_detail_has_watch_link_and_thumbnail(client: TestClient) -> None:
    resp = client.get("/refs/youtube/52100")
    assert resp.status_code == 200
    html = resp.text
    watch_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # Clickable watch link (button + thumbnail anchor both point at it).
    assert watch_url in html
    assert "Watch on YouTube" in html
    # The thumbnail image ("screenshot").
    assert "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in html
    # Channel + duration surfaced from the scraped cache meta.
    assert "Rick Astley" in html
    assert "3m33s" in html  # 213s -> 3m33s


def test_youtube_thumbnail_falls_back_without_scraped_meta(
    client: TestClient, runtime
) -> None:
    """No cache meta → still a watch link + a deterministic thumbnail."""
    runtime.store.cache_meta_by_slug.clear()
    resp = client.get("/refs/youtube/52100")
    assert resp.status_code == 200
    html = resp.text
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in html
    # Deterministic i.ytimg.com fallback derived from the slug.
    assert "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg" in html
