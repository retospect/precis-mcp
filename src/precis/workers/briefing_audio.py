"""briefing_audio — narrate the morning news briefing onto the podcast feed.

The **first automatic producer** on the audio pipe (docs/design/audio-feed.md).
The news briefing (:mod:`precis.workers.briefing`) runs in-process on the *agent*
worker (melchior) and persists a dated ``briefing-<date>`` ``news`` ref. This pass
finds the newest briefing ref that has no audio yet and renders its markdown to
speech via :func:`precis.tts.render.render_episode` — **container-first**: a
one-shot ``podman run precis-tts`` (so the worker needs no ``[tts]`` extra), or an
in-process synth for local runs. Self-scheduling — it fires off the *existence* of
an un-narrated briefing, so no separate cron is needed.

Idempotent: the produced episode id is stamped on the briefing ref as
``meta.audio_episode_id``; a briefing already carrying that marker is skipped, so
a re-tick — or a second host — can't double-publish. A render failure stamps
``meta.audio_failed_at`` (an hourly backoff) so a bad image can't spin a container
every tick. Gated default-OFF (``PRECIS_BRIEFING_AUDIO_ENABLED`` +
``PRECIS_TTS_IMAGE``), so it merges dark.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.draft.narrate import markdown_segments
from precis.tts.render import render_episode

log = logging.getLogger(__name__)

#: How far back a briefing may be and still be worth narrating. A missed day is
#: stale news; don't publish a week-old digest if the pass was off for a while.
_MAX_AGE_HOURS = 30
#: After a render fails (bad image / dead synth), back off this long before
#: retrying that briefing — so a persistent failure can't spin a podman run every
#: worker tick. Reset implicitly once it narrates (marker stamped).
_FAIL_BACKOFF_MINUTES = 60


def _latest_unnarrated_briefing(store: Any, *, max_age_hours: int, now: datetime):
    """The newest ``briefing`` news ref with no ``audio_episode_id`` yet, within
    ``max_age_hours`` and not in a render-failure backoff window. Returns the ref
    (via ``store.get_ref``) or ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'news' AND deleted_at IS NULL "
            "AND meta @> '{\"briefing\": true}'::jsonb "
            "AND NOT (meta ? 'audio_episode_id') "
            "AND updated_at >= %s "
            "AND (meta->>'audio_failed_at' IS NULL "
            "     OR (meta->>'audio_failed_at')::timestamptz < %s) "
            "ORDER BY updated_at DESC LIMIT 1",
            (
                now - timedelta(hours=max_age_hours),
                now - timedelta(minutes=_FAIL_BACKOFF_MINUTES),
            ),
        ).fetchone()
    if not row:
        return None
    return store.get_ref(kind="news", id=int(row[0]))


def has_pending_briefing(
    store: Any, *, now: datetime | None = None, max_age_hours: int = _MAX_AGE_HOURS
) -> bool:
    """Cheap existence check — is there an un-narrated briefing to work on?

    The worker gates on this **before** constructing the (heavy, model-loading)
    Kokoro synth, so an idle tick — the overwhelming majority, since a briefing
    lands once a day — costs one indexed SQL and never touches the TTS model."""
    now = now or datetime.now(UTC)
    return (
        _latest_unnarrated_briefing(store, max_age_hours=max_age_hours, now=now)
        is not None
    )


def _briefing_text(store: Any, ref_id: int) -> str:
    """Reconstruct the briefing markdown from its body chunks (``ord >= 0``, so
    the embeddable ``card_*`` variants at negative ord are excluded), in order."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks "
            "WHERE ref_id = %s AND retired_at IS NULL AND ord >= 0 "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return "\n\n".join((r[0] or "").strip() for r in rows if (r[0] or "").strip())


def run_briefing_audio(
    store: Any,
    *,
    image: str | None = None,
    synth: Any | None = None,
    podcast_dir: str | Path | None,
    voice: str = "af_heart",
    lang: str = "en-us",
    speed: float = 1.0,
    now: datetime | None = None,
    max_age_hours: int = _MAX_AGE_HOURS,
    encode: Callable[[Path, Path], None] | None = None,
    run: Callable[..., Any] = subprocess.run,
    container_cmd: str = "podman",
    scratch_dir: str | Path | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Narrate the latest un-narrated briefing and (optionally) publish it.

    Backend is container-first: ``image`` set → ``podman run precis-tts`` (the
    worker needs no ``[tts]`` extra); else ``synth`` → in-process. Returns
    ``{"published": bool, "reason": str, "ref_id": int|None, "episode_id":
    str|None, "segments": int, "duration_s": float}``.

    ``publish=False`` (or ``podcast_dir=None``) is a dry render — audio is
    produced and reported but nothing is published and no idempotency marker is
    stamped, so a later real run still fires. On a real publish the episode id is
    stamped on the ref so the next tick skips it.
    """
    from precis import audio_feed

    now = now or datetime.now(UTC)
    empty = {
        "published": False,
        "ref_id": None,
        "episode_id": None,
        "segments": 0,
        "duration_s": 0.0,
    }

    ref = _latest_unnarrated_briefing(store, max_age_hours=max_age_hours, now=now)
    if ref is None:
        return {**empty, "reason": "no-unnarrated-briefing"}

    text = _briefing_text(store, ref.id)
    segments = markdown_segments(text, voice=voice, lang=lang)
    if not segments:
        return {**empty, "ref_id": ref.id, "reason": "empty-briefing"}

    do_publish = publish and podcast_dir is not None
    date_tag = str((ref.meta or {}).get("date") or now.date().isoformat())
    render_kw: dict[str, Any] = {} if encode is None else {"encode": encode}
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / f"briefing-{date_tag}.mp3"
        try:
            result = render_episode(
                segments,
                out_path,
                image=image,
                synth=synth,
                speed=speed,
                scratch_dir=scratch_dir,
                container_cmd=container_cmd,
                run=run,
                **render_kw,
            )
        except Exception as exc:  # a bad image / dead synth mustn't crash the tick
            log.warning("briefing_audio: render failed (%s)", exc)
            # Back off this briefing so a persistent failure can't spin per-tick.
            store.update_ref(ref.id, meta_patch={"audio_failed_at": now.isoformat()})
            return {**empty, "ref_id": ref.id, "reason": f"render-failed: {exc}"}
        seg_n = int(result.get("segments", len(segments)))
        dur = float(result.get("duration_s", 0.0))
        if not do_publish:
            log.info(
                "briefing_audio: dry render %s (%d seg, %.0fs) — not published",
                date_tag,
                seg_n,
                dur,
            )
            return {
                "published": False,
                "reason": "dry-run",
                "ref_id": ref.id,
                "episode_id": None,
                "segments": seg_n,
                "duration_s": dur,
            }
        assert podcast_dir is not None  # do_publish ⇒ set (narrows for mypy)
        episode_id = f"news-{date_tag}"
        audio_feed.publish_episode(
            podcast_dir,
            result.get("audio_path", out_path),
            episode_id=episode_id,
            title=f"🗞 Morning briefing — {date_tag}",
            description=f"Narrated news briefing for {date_tag} ({seg_n} sections).",
            published_at=now,
            duration_seconds=int(dur),
            source="news",
        )

    # Stamp the idempotency marker only after a successful publish.
    store.update_ref(ref.id, meta_patch={"audio_episode_id": episode_id})
    log.info(
        "briefing_audio: published %s (%d seg, %.0fs) → ref %s",
        episode_id,
        seg_n,
        dur,
        ref.id,
    )
    return {
        "published": True,
        "reason": "published",
        "ref_id": ref.id,
        "episode_id": episode_id,
        "segments": seg_n,
        "duration_s": dur,
    }


__all__ = ["has_pending_briefing", "run_briefing_audio"]
