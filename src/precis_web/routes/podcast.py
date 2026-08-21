"""Private podcast feed routes — the "pipe audio to the phone" surface.

- ``GET /podcast/feed.xml`` — RSS 2.0 over the episodes in ``podcast_dir``.
- ``GET /podcast/audio/{name}`` — stream one episode's audio enclosure. ``name``
  is the audio filename (``news-2026-07-16.mp3``) that the feed points at; the
  bare episode id (no extension) still resolves, for URLs cached before the
  extension landed.

Content-agnostic: any producer drops an episode via
:func:`precis_web.podcast.publish_episode`; these routes just render + serve.
Meant to be reached over the Tailscale-served origin for a private feed —
set ``PRECIS_PODCAST_BASE_URL`` to that origin so enclosure URLs are absolute
and reachable from the phone. See :mod:`precis_web.podcast`.

**These two routes authenticate themselves**, which is why
:mod:`precis_web.auth` exempts the ``/podcast`` prefix from its blanket
Basic gate. A podcast app subscribes once and then fetches enclosures on
its own schedule, and support for HTTP Basic on those enclosure requests
is inconsistent across clients — Basic alone would leave the phone
silently not downloading. So a per-user feed token
(``precis users feed-token <login>``) is accepted as ``?t=`` and is
threaded into the enclosure URLs the feed emits; Basic still works for a
browser hitting the feed directly. Both paths go through
:func:`_require_listener`, so an unauthenticated request is rejected here
exactly as it would have been upstream.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from precis import audio_feed as podcast
from precis.users import feed_token_digest
from precis_web.config import WebConfig
from precis_web.deps import get_web_config

router = APIRouter(tags=["podcast"])


def _require_listener(request: Request, cfg: WebConfig) -> str | None:
    """Authorize a podcast request; return the feed token it presented.

    Returns the ``?t=`` token when that is what authenticated the caller
    (so the feed can thread it back into enclosure URLs), ``None`` when
    Basic did or when auth is off entirely. Raises 401/503 exactly like
    the middleware otherwise.
    """
    from precis_web.auth import (
        AuthError,
        authenticate,
        parse_basic_header,
        require_roster,
    )

    if not cfg.auth_required:
        return None

    store = getattr(getattr(request.app.state, "runtime", None), "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="no database connection")
    try:
        require_roster(store)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    token = request.query_params.get("t")
    if token:
        if store.get_web_user_by_feed_token(feed_token_digest(token)) is not None:
            return token
        # A wrong token is a dead end, not a fallback to Basic: a podcast
        # app retrying a rotated URL would otherwise get a challenge it
        # can't answer and churn.
        raise HTTPException(status_code=401, detail="invalid feed token")

    creds = parse_basic_header(request.headers.get("authorization"))
    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": 'Basic realm="precis"'},
        )
    try:
        authenticate(store, creds[0], creds[1])
    except AuthError as exc:
        headers = (
            {"WWW-Authenticate": 'Basic realm="precis"'} if exc.challenge else None
        )
        raise HTTPException(
            status_code=exc.status, detail=exc.detail, headers=headers
        ) from exc
    return None


def _base_url(request: Request, cfg: WebConfig) -> str:
    """Public origin for enclosure URLs — the configured base wins; else the
    request origin (fine for same-host testing, wrong behind a proxy)."""
    if cfg.podcast_base_url:
        return cfg.podcast_base_url
    return str(request.base_url).rstrip("/")


@router.get("/podcast/feed.xml")
def feed(request: Request, cfg: WebConfig = Depends(get_web_config)) -> Response:
    token = _require_listener(request, cfg)
    episodes = podcast.list_episodes(cfg.podcast_dir) if cfg.podcast_dir else []
    channel = podcast.ChannelMeta(author=cfg.owner)
    xml = podcast.build_rss(
        episodes,
        base_url=_base_url(request, cfg),
        channel=channel,
        credential=token,
    )
    # A short cache so a podcast app polling every few minutes isn't
    # re-rendering the feed each time, but new episodes still land
    # promptly. ``private`` since the response now embeds the caller's own
    # feed token — a shared cache must not hand one listener's credential
    # to the next.
    return Response(
        content=xml,
        media_type="application/rss+xml",
        headers={"Cache-Control": "private, max-age=120"},
    )


@router.get("/podcast/audio/{name}")
def audio(
    name: str, request: Request, cfg: WebConfig = Depends(get_web_config)
) -> FileResponse:
    _require_listener(request, cfg)
    if not cfg.podcast_dir:
        raise HTTPException(status_code=404, detail="no podcast configured")
    # Resolve strictly inside podcast_dir — reject traversal / escapes, the
    # same discipline the file kinds use. Match the audio filename (what the
    # feed points at now) or the bare episode id (older cached URLs).
    root = cfg.podcast_dir.resolve()
    for ep in podcast.list_episodes(root):
        if name in (ep.audio_file, ep.id):
            target = (root / ep.audio_file).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise HTTPException(status_code=404, detail="episode audio missing")
            return FileResponse(target, media_type=ep.mime, filename=ep.audio_file)
    raise HTTPException(status_code=404, detail="episode not found")
