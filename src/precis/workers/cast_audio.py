"""cast_audio — narrate the daily *casts* (morning brief + nidra) onto the feed.

The audio organ of the cast pipeline (docs/backlog/reading-prep-loop.md §Audio),
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
``meta.audio_failed_at`` + ``meta.audio_fail_count`` (an exponential backoff
from ~2 minutes up to an hour). Gated default-OFF
(``PRECIS_CAST_AUDIO_ENABLED`` + ``PRECIS_TTS_IMAGE``) so it merges dark.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.reading.cast_common import CAST_PROFILES, export_stem
from precis.tts.render import (
    SIGNAL_KILL_RETURNCODES,
    ContainerRenderError,
    render_episode,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: How far back a cast draft may be and still be worth narrating. A cast is a
#: same-day artifact; if the pass was off for days, don't suddenly dump a backlog
#: of stale episodes — publish only the fresh one, let the old ones lapse.
_MAX_AGE_HOURS = 48
#: Ceiling on the retry backoff after a render failure, so a genuinely broken
#: image / dead synth can't spin a container every worker tick.
#:
#: The backoff is **exponential from a short first step** (2, 4, 8, 16, 32, then
#: this ceiling), not a flat hour, because the failure this actually sees in prod
#: is not a broken render — it's a *killed* one. The container runs inside the
#: worker's own systemd cgroup, so any worker restart (a deploy, a jetsam cull, a
#: manual bounce) SIGTERMs it mid-render and ``docker run`` exits 143. A flat
#: 60-minute penalty then charged that seconds-long restart the entire morning
#: episode: 2026-08-06 composed 14:35 UTC, was killed at 14:44, and didn't
#: publish until 16:32 — which is the "brief comes out at 17:00" symptom. Two
#: minutes is long enough for the restarting worker to come back and short
#: enough that a collateral kill costs nothing, while a render that keeps
#: failing still converges on the same hourly cooldown.
_FAIL_BACKOFF_MINUTES = 60
#: Cap on the exponent, so a long-dead draft can't overflow the shift.
_FAIL_BACKOFF_MAX_STEPS = 8
#: The trailing silence stamped on each news lead-in segment (Workstream C) — a
#: wider ~1.5s beat between wire stories/sections, vs. the container's gentler
#: 0.45s default the personal brief's own segments keep (``gap_after=None``).
_NEWS_LEAD_IN_PAUSE_S = 1.5


#: The selection predicate — a draft that is a cast, un-narrated, fresh enough,
#: and out of its render-failure backoff. Exported as a constant (rather than
#: inlined) because the tests need to ask it about *one* ref under a shared test
#: DB; a hand-copied mirror of it there would keep passing against a rule the
#: code no longer applies. Placeholders, in order: the max-age cutoff, the
#: backoff ceiling in minutes, the max backoff exponent, and ``now``.
_SELECTABLE_SQL = (
    "kind = 'draft' AND retired_at IS NULL "
    "AND meta ? 'cast' "
    "AND NOT (meta ? 'audio_episode_id') "
    "AND created_at >= %s "
    "AND (meta->>'audio_failed_at' IS NULL "
    "     OR (meta->>'audio_failed_at')::timestamptz "
    "        + make_interval(mins => LEAST(%s, (2 ^ LEAST(GREATEST("
    "            COALESCE((meta->>'audio_fail_count')::int, 1), 1"
    "          ), %s))::int)) <= %s)"
)


def selection_params(now: datetime, max_age_hours: int) -> tuple[Any, ...]:
    """Bind values for :data:`_SELECTABLE_SQL`, in placeholder order."""
    return (
        now - timedelta(hours=max_age_hours),
        _FAIL_BACKOFF_MINUTES,
        _FAIL_BACKOFF_MAX_STEPS,
        now,
    )


def _latest_unnarrated_cast(store: Store, *, max_age_hours: int, now: datetime):
    """The newest cast ``draft`` with no ``audio_episode_id`` yet, within
    ``max_age_hours`` and not in a render-failure backoff window, or ``None``.

    Aged and ordered by COMPOSE time (``created_at``), not ``updated_at``:
    every failed render stamps ``meta.audio_failed_at`` via
    ``store.update_ref``, which bumps ``updated_at`` — so a stale draft that
    keeps failing would otherwise keep refreshing itself to the top of the
    ``updated_at`` window and be re-selected forever. ``created_at`` doesn't
    move, so a repeatedly-failing draft can't refresh itself back into
    contention this way; the separate ``audio_failed_at`` backoff clause
    below still applies its own cooldown.

    That cooldown is per-row, not a constant: the wait is ``2 **
    audio_fail_count`` minutes capped at :data:`_FAIL_BACKOFF_MINUTES`, so a
    first failure retries in ~2 minutes and only a persistently failing draft
    earns the full hour. A draft that failed before the counter existed reads as
    one failure (the ``COALESCE``) and gets the same short first retry.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT ref_id FROM refs WHERE {_SELECTABLE_SQL} "
            "ORDER BY created_at DESC LIMIT 1",
            selection_params(now, max_age_hours),
        ).fetchone()
    if not row:
        return None
    return store.get_ref(kind="draft", id=int(row[0]))


def has_pending_cast(
    store: Store, *, now: datetime | None = None, max_age_hours: int = _MAX_AGE_HOURS
) -> bool:
    """Cheap existence check — is there an un-narrated cast to work on?

    The worker gates on this **before** constructing the (heavy, model-loading)
    synth / container, so an idle tick costs one indexed SQL."""
    now = now or datetime.now(UTC)
    return (
        _latest_unnarrated_cast(store, max_age_hours=max_age_hours, now=now) is not None
    )


def _stamp_failure(
    store: Store, ref: Any, now: datetime, *, killed: bool = False
) -> None:
    """Record a render failure on ``ref`` and bump the attempt counter that
    chooses the next backoff step (see :func:`_latest_unnarrated_cast`).

    ``killed=True`` marks a render that was **signal-killed** mid-flight (a worker
    restart SIGTERMed the container → exit 143), not a broken draft. The short
    first backoff step already makes one kill cheap, but *counting* it would let a
    rocky deploy window (several restarts in a row) escalate a healthy draft's
    cooldown toward the full hour. So a kill stamps ``audio_failed_at`` (keeping
    the ~2-minute reselect cooldown) but leaves ``audio_fail_count`` untouched —
    only a genuine render failure advances the exponent.

    Tolerant of a missing/garbled counter — this runs on the failure path, and a
    bad meta value must not turn a recoverable render failure into a crashed
    worker tick.
    """
    patch: dict[str, Any] = {"audio_failed_at": now.isoformat()}
    if not killed:
        try:
            prior = int((ref.meta or {}).get("audio_fail_count") or 0)
        except (TypeError, ValueError):
            prior = 0
        patch["audio_fail_count"] = prior + 1
    store.update_ref(ref.id, meta_patch=patch)


def _empty(reason: str, ref_id: int | None = None) -> dict[str, Any]:
    return {
        "published": False,
        "reason": reason,
        "ref_id": ref_id,
        "episode_id": None,
        "segments": 0,
        "duration_s": 0.0,
    }


def _news_lead_in(
    store: Store,
    segments: list[Any],
    *,
    date_tag: str,
    voice: str,
    default_lang: str,
) -> list[Any]:
    """Prepend today's news wire — read in the brief's own voice — ahead of the
    reading cast's segments, so the two compose as one ~30-minute morning
    episode (full news first, then the personal brief). ``voice``/``default_lang``
    narrate the wire in the same voice as the rest of the cast. Degrades to
    ``segments`` unchanged when no ``briefing-<date>`` news ref exists yet (or
    it carries no body) — the reading cast still narrates and publishes on its
    own rather than blocking on the news wire.

    Only the news segments are stamped with ``gap_after=_NEWS_LEAD_IN_PAUSE_S``
    (a ~1.5s beat between wire stories/sections) — this is the one place that
    policy lives. The personal-brief segments that follow keep their own
    ``gap_after=None`` (the container's gentler 0.45s default), so the news
    wire reads with a wider beat while the brief's prose keeps its original
    pace.
    """
    from precis.draft.narrate import markdown_segments
    from precis.reading.briefing_cast import _news_brief_text

    news_ref = store.get_ref(kind="news", id=f"briefing-{date_tag}")
    if news_ref is None:
        log.info(
            "cast_audio: news lead-in skipped — no briefing-%s news ref; "
            "narrating brief-only",
            date_tag,
        )
        return segments
    news_body = _news_brief_text(store, news_ref.id)
    if not news_body:
        log.info(
            "cast_audio: news lead-in skipped — briefing-%s (ref %s) has empty "
            "body; narrating brief-only",
            date_tag,
            news_ref.id,
        )
        return segments
    news_segments = [
        replace(seg, gap_after=_NEWS_LEAD_IN_PAUSE_S)
        for seg in markdown_segments(news_body, voice=voice, lang=default_lang)
    ]
    log.info(
        "cast_audio: news lead-in prepended %d segment(s) (~%d chars) from "
        "briefing-%s (ref %s) ahead of %d brief segment(s)",
        len(news_segments),
        len(news_body),
        date_tag,
        news_ref.id,
        len(segments),
    )
    return [*news_segments, *segments]


def narrate_cast_ref(
    store: Store,
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
    date_tag = str(meta.get("date") or now.date().isoformat())

    lexicon = resolve_lexicon(ref, personal=load_personal_lexicon())
    segments = render_narration(
        store, ref, default_voice=voice, default_lang=default_lang, lexicon=lexicon
    )
    if cast == "reading" and profile is not None:
        # Combined morning episode: the full news wire (same voice), then the
        # personal brief — one episode, no separate news audio (Workstream B).
        segments = _news_lead_in(
            store, segments, date_tag=date_tag, voice=voice, default_lang=default_lang
        )
    if not segments:
        # Nothing speakable — back off so we don't reselect this draft every tick.
        _stamp_failure(store, ref, now)
        return _empty("empty-cast", ref.id)

    do_publish = publish and podcast_dir is not None
    # Human episode id — ``morning_brief_<date>`` / ``evening_meditation_<date>``
    # so the published mp3 shares the export PDF's recognisable stem, not the
    # internal cast key. Casts with no profile fall back to ``<cast>-<date>``.
    episode_id = export_stem(profile, date_tag) if profile else f"{cast}-{date_tag}"
    title = f"{profile.title} — {date_tag}" if profile else (ref.title or episode_id)
    source = profile.source if profile else "cast"
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
            # A signal-killed render (worker restart SIGTERMs the container mid-
            # flight → exit 143) is collateral, not a broken draft: stamp the
            # short reselect cooldown but don't advance the backoff exponent, so a
            # rocky deploy window can't push a healthy cast to the hourly ceiling.
            killed = (
                isinstance(exc, ContainerRenderError)
                and exc.returncode in SIGNAL_KILL_RETURNCODES
            )
            log.warning(
                "cast_audio: render %s for ref %s (%s)",
                "killed mid-flight" if killed else "failed",
                ref.id,
                exc,
            )
            _stamp_failure(store, ref, now, killed=killed)
            reason = "render-killed" if killed else "render-failed"
            return _empty(f"{reason}: {exc}", ref.id)

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
    store: Store,
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
