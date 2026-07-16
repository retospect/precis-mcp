"""cast_audio — narrate the daily *casts* (morning brief + nidra) onto the feed.

The audio organ of the cast pipeline (docs/design/reading-prep-loop.md §Audio),
sibling to :mod:`precis.workers.briefing_audio`. The cast *producers*
(:func:`precis.reading.briefing_cast.build_reading_briefing`,
:func:`precis.reading.meditation.build_meditation`) run on any node and persist a
standalone dated ``draft`` marked ``meta.cast``. This pass — TTS-host-only (spark)
— finds the newest cast draft with no audio yet and renders it to speech via
:func:`precis.tts.render.render_episode` (container-first ``podman/docker run
precis-tts``), honouring the draft's per-chunk + draft-level voice through
:func:`precis.draft.narrate.render_narration`, then publishes onto the shared
podcast feed.

Idempotent + self-throttling exactly like ``briefing_audio``: the episode id is
stamped as ``meta.audio_episode_id`` (a marked draft is skipped, so a re-tick or a
second host can't double-publish), and a render failure stamps
``meta.audio_failed_at`` (an hourly backoff). Gated default-OFF
(``PRECIS_CAST_AUDIO_ENABLED`` + ``PRECIS_TTS_IMAGE``) so it merges dark.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.reading.cast_common import CAST_PROFILES
from precis.tts.render import render_episode

log = logging.getLogger(__name__)

#: How far back a cast draft may be and still be worth narrating. A cast is a
#: same-day artifact; if the pass was off for days, don't suddenly dump a backlog
#: of stale episodes — publish only the fresh one, let the old ones lapse.
_MAX_AGE_HOURS = 48
#: After a render fails, back off this long before retrying the same draft so a
#: bad image / dead synth can't spin a container every worker tick.
_FAIL_BACKOFF_MINUTES = 60


def _latest_unnarrated_cast(store: Any, *, max_age_hours: int, now: datetime):
    """The newest cast ``draft`` with no ``audio_episode_id`` yet, within
    ``max_age_hours`` and not in a render-failure backoff window, or ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'draft' AND deleted_at IS NULL "
            "AND meta ? 'cast' "
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
    return store.get_ref(kind="draft", id=int(row[0]))


def has_pending_cast(
    store: Any, *, now: datetime | None = None, max_age_hours: int = _MAX_AGE_HOURS
) -> bool:
    """Cheap existence check — is there an un-narrated cast to work on?

    The worker gates on this **before** constructing the (heavy, model-loading)
    synth / container, so an idle tick costs one indexed SQL."""
    now = now or datetime.now(UTC)
    return (
        _latest_unnarrated_cast(store, max_age_hours=max_age_hours, now=now) is not None
    )


def _empty(reason: str, ref_id: int | None = None) -> dict[str, Any]:
    return {
        "published": False,
        "reason": reason,
        "ref_id": ref_id,
        "episode_id": None,
        "segments": 0,
        "duration_s": 0.0,
    }


def narrate_cast_ref(
    store: Any,
    ref: Any,
    *,
    image: str | None = None,
    synth: Any | None = None,
    podcast_dir: str | Path | None,
    now: datetime | None = None,
    default_lang: str = "en-us",
    speed: float = 1.0,
    encode: Callable[[Path, Path], None] | None = None,
    run: Callable[..., Any] = subprocess.run,
    container_cmd: str = "podman",
    scratch_dir: str | Path | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Narrate one cast ``draft`` ref and (optionally) publish it — the shared
    render→publish tail reused by the worker pass and the ``precis cast`` CLI.

    Backend is container-first: ``image`` set → ``podman/docker run precis-tts``;
    else ``synth`` → in-process. Per-chunk ``meta.voice``/``meta.lang`` win over
    the draft-level voice (``ref.meta.voice``, from the cast profile). Returns the
    same shape as :func:`run_cast_audio`.

    ``publish=False`` (or ``podcast_dir=None``) is a dry render — nothing is
    published and no idempotency marker is stamped, so a later real run still
    fires.
    """
    from precis import audio_feed
    from precis.draft.narrate import (
        load_personal_lexicon,
        render_narration,
        resolve_lexicon,
    )

    now = now or datetime.now(UTC)
    meta = ref.meta or {}
    cast = str(meta.get("cast") or "cast")
    profile = CAST_PROFILES.get(cast)
    voice = str(meta.get("voice") or (profile.voice if profile else "af_heart"))

    lexicon = resolve_lexicon(ref, personal=load_personal_lexicon())
    segments = render_narration(
        store, ref, default_voice=voice, default_lang=default_lang, lexicon=lexicon
    )
    if not segments:
        # Nothing speakable — back off so we don't reselect this draft every tick.
        store.update_ref(ref.id, meta_patch={"audio_failed_at": now.isoformat()})
        return _empty("empty-cast", ref.id)

    do_publish = publish and podcast_dir is not None
    date_tag = str(meta.get("date") or now.date().isoformat())
    episode_id = f"{cast}-{date_tag}"
    title = f"{profile.title} — {date_tag}" if profile else (ref.title or episode_id)
    source = profile.source if profile else "reading"
    render_kw: dict[str, Any] = {} if encode is None else {"encode": encode}

    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / f"{episode_id}.mp3"
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
            log.warning("cast_audio: render failed for ref %s (%s)", ref.id, exc)
            store.update_ref(ref.id, meta_patch={"audio_failed_at": now.isoformat()})
            return _empty(f"render-failed: {exc}", ref.id)

        seg_n = int(result.get("segments", len(segments)))
        dur = float(result.get("duration_s", 0.0))
        if not do_publish:
            log.info(
                "cast_audio: dry render %s (%d seg, %.0fs) — not published",
                episode_id,
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
        audio_feed.publish_episode(
            podcast_dir,
            result.get("audio_path", out_path),
            episode_id=episode_id,
            title=title,
            description=f"{cast} cast for {date_tag} ({seg_n} sections).",
            published_at=now,
            duration_seconds=int(dur),
            source=source,
        )

    # Stamp the idempotency marker only after a successful publish.
    store.update_ref(ref.id, meta_patch={"audio_episode_id": episode_id})
    log.info(
        "cast_audio: published %s (%d seg, %.0fs) → ref %s",
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


def run_cast_audio(
    store: Any,
    *,
    image: str | None = None,
    synth: Any | None = None,
    podcast_dir: str | Path | None,
    now: datetime | None = None,
    max_age_hours: int = _MAX_AGE_HOURS,
    default_lang: str = "en-us",
    speed: float = 1.0,
    encode: Callable[[Path, Path], None] | None = None,
    run: Callable[..., Any] = subprocess.run,
    container_cmd: str = "podman",
    scratch_dir: str | Path | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Narrate the latest un-narrated cast draft and (optionally) publish it.

    Selection is by existence (self-scheduling): the newest cast ``draft`` with no
    ``audio_episode_id``. Returns ``{"published", "reason", "ref_id",
    "episode_id", "segments", "duration_s"}``.
    """
    now = now or datetime.now(UTC)
    ref = _latest_unnarrated_cast(store, max_age_hours=max_age_hours, now=now)
    if ref is None:
        return _empty("no-unnarrated-cast")
    return narrate_cast_ref(
        store,
        ref,
        image=image,
        synth=synth,
        podcast_dir=podcast_dir,
        now=now,
        default_lang=default_lang,
        speed=speed,
        encode=encode,
        run=run,
        container_cmd=container_cmd,
        scratch_dir=scratch_dir,
        publish=publish,
    )


__all__ = ["has_pending_cast", "narrate_cast_ref", "run_cast_audio"]
