"""Audio feed (podcast) — reusable "pipe audio to the phone" surface.

Content-agnostic on purpose: *any* producer (a future knowledge brief, the
news briefing, a "read me this paper" command) publishes an **episode** — an
audio file + a JSON sidecar of metadata — into a served directory
(``PRECIS_PODCAST_DIR``). The web layer (``precis_web.routes.podcast``)
generates a valid RSS 2.0 feed over it and serves the enclosures; subscribe
once in a podcast app (Overcast / Pocket Casts / Apple Podcasts) — over the
Tailscale-served surface for a private feed — and new episodes auto-download
to the phone.

Filesystem-backed (no DB table, no migration): `<id>.<ext>` audio next to
`<id>.json` sidecar. This module is pure (no FastAPI, no store) so it unit-
tests cleanly and the publish primitive is callable from a worker, a CLI, or
a TTS pass.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

#: Enclosure MIME by audio extension. Podcast apps key playback off this.
_MIME_BY_EXT: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
}


def mime_for(path: str | Path) -> str:
    """Enclosure MIME for an audio file; defaults to ``audio/mpeg``."""
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "audio/mpeg")


@dataclass(frozen=True, slots=True)
class Episode:
    """One published episode, reconstructed from its sidecar + audio file."""

    id: str
    title: str
    description: str
    audio_file: str  # basename, relative to the podcast dir
    published_at: datetime
    bytes: int
    mime: str
    duration_seconds: int | None = None
    source: str | None = None  # which producer made it (e.g. "brief", "news")


@dataclass(frozen=True, slots=True)
class ChannelMeta:
    """Feed-level metadata (the podcast show, not an episode)."""

    title: str = "precis"
    description: str = "precis audio feed"
    author: str = "precis"
    language: str = "en"


def publish_episode(
    podcast_dir: str | Path,
    audio_path: str | Path,
    *,
    episode_id: str,
    title: str,
    description: str,
    published_at: datetime,
    duration_seconds: int | None = None,
    source: str | None = None,
) -> Episode:
    """Copy ``audio_path`` into ``podcast_dir`` and write its sidecar.

    ``episode_id`` is the stable guid + filename stem — caller owns it (a
    timestamp slug, a content hash, whatever) so this stays deterministic and
    testable. Returns the resulting :class:`Episode`.
    """
    d = Path(podcast_dir)
    d.mkdir(parents=True, exist_ok=True)
    src = Path(audio_path)
    ext = src.suffix.lower() or ".mp3"
    audio_name = f"{episode_id}{ext}"
    dst = d / audio_name
    shutil.copyfile(src, dst)
    ep = Episode(
        id=episode_id,
        title=title,
        description=description,
        audio_file=audio_name,
        published_at=published_at,
        bytes=dst.stat().st_size,
        mime=mime_for(dst),
        duration_seconds=duration_seconds,
        source=source,
    )
    (d / f"{episode_id}.json").write_text(
        json.dumps(
            {
                "id": ep.id,
                "title": ep.title,
                "description": ep.description,
                "audio_file": ep.audio_file,
                "published_at": ep.published_at.isoformat(),
                "bytes": ep.bytes,
                "mime": ep.mime,
                "duration_seconds": ep.duration_seconds,
                "source": ep.source,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ep


def _load_episode(sidecar: Path) -> Episode | None:
    try:
        raw: dict[str, Any] = json.loads(sidecar.read_text(encoding="utf-8"))
        return Episode(
            id=str(raw["id"]),
            title=str(raw["title"]),
            description=str(raw.get("description", "")),
            audio_file=str(raw["audio_file"]),
            published_at=datetime.fromisoformat(str(raw["published_at"])),
            bytes=int(raw.get("bytes", 0)),
            mime=str(raw.get("mime", "audio/mpeg")),
            duration_seconds=(
                int(raw["duration_seconds"])
                if raw.get("duration_seconds") is not None
                else None
            ),
            source=raw.get("source"),
        )
    except (KeyError, ValueError, OSError):
        return None


def list_episodes(podcast_dir: str | Path) -> list[Episode]:
    """All published episodes, newest first. Skips malformed / orphan sidecars."""
    d = Path(podcast_dir)
    if not d.is_dir():
        return []
    eps: list[Episode] = []
    for sidecar in d.glob("*.json"):
        ep = _load_episode(sidecar)
        if ep is not None and (d / ep.audio_file).is_file():
            eps.append(ep)
    eps.sort(key=lambda e: e.published_at, reverse=True)
    return eps


def build_rss(
    episodes: list[Episode],
    *,
    base_url: str,
    channel: ChannelMeta | None = None,
    audio_path_prefix: str = "/podcast/audio",
) -> str:
    """Render RSS 2.0 (with iTunes tags) for the episodes.

    ``base_url`` is the public origin (e.g. ``https://host.tailnet.ts.net``);
    enclosure URLs are ``<base_url><audio_path_prefix>/<audio_file>`` — the
    filename carries its extension (``…/news-2026-07-16.mp3``) so a shared link
    or a saved file is unambiguously an mp3 to browsers + podcast apps. Apps
    need *absolute* URLs, so the base must be the reachable origin, not
    loopback — see ``PRECIS_PODCAST_BASE_URL``. The ``<guid>`` stays the bare
    episode id, so extension/URL changes never re-add already-seen episodes.
    """
    ch = channel or ChannelMeta()
    base = base_url.rstrip("/")
    feed_url = f"{base}/podcast/feed.xml"
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(ch.title)}</title>",
        f"<link>{escape(base)}</link>",
        f"<description>{escape(ch.description)}</description>",
        f"<language>{escape(ch.language)}</language>",
        f"<itunes:author>{escape(ch.author)}</itunes:author>",
        f'<atom:link href={quoteattr(feed_url)} rel="self" '
        'type="application/rss+xml"/>',
    ]
    for ep in episodes:
        enclosure_url = f"{base}{audio_path_prefix}/{ep.audio_file}"
        lines.append("<item>")
        lines.append(f"<title>{escape(ep.title)}</title>")
        lines.append(f"<description>{escape(ep.description)}</description>")
        lines.append(f'<guid isPermaLink="false">{escape(ep.id)}</guid>')
        lines.append(f"<pubDate>{format_datetime(ep.published_at)}</pubDate>")
        lines.append(
            f"<enclosure url={quoteattr(enclosure_url)} "
            f'length="{ep.bytes}" type={quoteattr(ep.mime)}/>'
        )
        if ep.duration_seconds is not None:
            lines.append(f"<itunes:duration>{ep.duration_seconds}</itunes:duration>")
        lines.append("</item>")
    lines.append("</channel>")
    lines.append("</rss>")
    return "\n".join(lines)
