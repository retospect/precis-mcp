"""Private podcast feed routes — the "pipe audio to the phone" surface.

- ``GET /podcast/feed.xml`` — RSS 2.0 over the episodes in ``podcast_dir``.
- ``GET /podcast/audio/{episode_id}`` — stream one episode's audio enclosure.

Content-agnostic: any producer drops an episode via
:func:`precis_web.podcast.publish_episode`; these routes just render + serve.
Meant to be reached over the Tailscale-served origin for a private feed —
set ``PRECIS_PODCAST_BASE_URL`` to that origin so enclosure URLs are absolute
and reachable from the phone. See :mod:`precis_web.podcast`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from precis import audio_feed as podcast
from precis_web.config import WebConfig
from precis_web.deps import get_web_config

router = APIRouter(tags=["podcast"])


def _base_url(request: Request, cfg: WebConfig) -> str:
    """Public origin for enclosure URLs — the configured base wins; else the
    request origin (fine for same-host testing, wrong behind a proxy)."""
    if cfg.podcast_base_url:
        return cfg.podcast_base_url
    return str(request.base_url).rstrip("/")


@router.get("/podcast/feed.xml")
def feed(request: Request, cfg: WebConfig = Depends(get_web_config)) -> Response:
    episodes = podcast.list_episodes(cfg.podcast_dir) if cfg.podcast_dir else []
    channel = podcast.ChannelMeta(author=cfg.owner)
    xml = podcast.build_rss(episodes, base_url=_base_url(request, cfg), channel=channel)
    # A short cache so a podcast app polling every few minutes isn't
    # re-rendering the feed each time, but new episodes still land promptly.
    return Response(
        content=xml,
        media_type="application/rss+xml",
        headers={"Cache-Control": "public, max-age=120"},
    )


@router.get("/podcast/audio/{episode_id}")
def audio(episode_id: str, cfg: WebConfig = Depends(get_web_config)) -> FileResponse:
    if not cfg.podcast_dir:
        raise HTTPException(status_code=404, detail="no podcast configured")
    # Resolve strictly inside podcast_dir — reject traversal / escapes, the
    # same discipline the file kinds use.
    root = cfg.podcast_dir.resolve()
    for ep in podcast.list_episodes(root):
        if ep.id == episode_id:
            target = (root / ep.audio_file).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise HTTPException(status_code=404, detail="episode audio missing")
            return FileResponse(target, media_type=ep.mime, filename=ep.audio_file)
    raise HTTPException(status_code=404, detail="episode not found")
