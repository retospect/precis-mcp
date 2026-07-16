"""The reusable audio-feed (podcast) primitive — publish + list + RSS render.

Pure (no web, no DB): drop an audio file + sidecar, list it back, render a
valid RSS 2.0 feed with absolute enclosure URLs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from precis import audio_feed


def _write_audio(path, data: bytes = b"ID3fakeaudio") -> str:
    path.write_bytes(data)
    return str(path)


def _publish(d, *, episode_id, title, when, desc="a brief", source="brief", ext=".m4a"):
    audio = _write_audio(d / f"src{ext}")
    return audio_feed.publish_episode(
        d,
        audio,
        episode_id=episode_id,
        title=title,
        description=desc,
        published_at=when,
        source=source,
    )


def test_publish_writes_audio_and_sidecar(tmp_path):
    when = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    ep = _publish(tmp_path, episode_id="e1", title="Morning brief", when=when)
    assert (tmp_path / "e1.m4a").is_file()
    assert (tmp_path / "e1.json").is_file()
    assert ep.mime == "audio/mp4"
    assert ep.bytes > 0
    assert ep.source == "brief"


def test_list_newest_first_and_skips_orphan_sidecar(tmp_path):
    _publish(
        tmp_path, episode_id="old", title="Old", when=datetime(2026, 7, 1, tzinfo=UTC)
    )
    _publish(
        tmp_path, episode_id="new", title="New", when=datetime(2026, 7, 14, tzinfo=UTC)
    )
    # An orphan sidecar (no audio) must be skipped, not crash the listing.
    (tmp_path / "ghost.json").write_text('{"id":"ghost"}', encoding="utf-8")
    eps = audio_feed.list_episodes(tmp_path)
    assert [e.id for e in eps] == ["new", "old"]  # newest first


def test_missing_dir_lists_empty(tmp_path):
    assert audio_feed.list_episodes(tmp_path / "nope") == []


def test_build_rss_shape_and_absolute_enclosures(tmp_path):
    ep = _publish(
        tmp_path,
        episode_id="e1",
        title="Brief & <friends>",  # exercises XML escaping
        when=datetime(2026, 7, 14, 8, 0, tzinfo=UTC),
        ext=".mp3",
    )
    xml = audio_feed.build_rss(
        [ep], base_url="https://host.tailnet.ts.net/", channel=audio_feed.ChannelMeta()
    )
    assert xml.startswith("<?xml")
    assert '<rss version="2.0"' in xml
    assert "<enclosure " in xml
    # Absolute enclosure URL under the configured base (trailing slash trimmed).
    # The URL carries the audio filename + extension so it shares unambiguously.
    assert 'url="https://host.tailnet.ts.net/podcast/audio/e1.mp3"' in xml
    assert f'length="{ep.bytes}"' in xml
    assert 'type="audio/mpeg"' in xml
    # The guid stays the bare id, so the extension in the URL never re-adds it.
    assert '<guid isPermaLink="false">e1</guid>' in xml
    # XML-escaped, not raw.
    assert "Brief &amp; &lt;friends&gt;" in xml
    assert "<friends>" not in xml


def test_empty_feed_is_valid(tmp_path):
    xml = audio_feed.build_rss([], base_url="https://h.ts.net")
    assert xml.startswith("<?xml")
    assert "<channel>" in xml and "<item>" not in xml
