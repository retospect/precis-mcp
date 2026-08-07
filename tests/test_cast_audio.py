"""Tests for the cast audio pass (selection predicate + narrate/publish tail)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from precis.reading.cast_common import CAST_PROFILES, create_cast_draft
from precis.workers import cast_audio


def _make_cast_draft(store: Any, cast: str = "reading") -> Any:
    date_tag = f"{cast[:3]}-{uuid.uuid4().hex[:8]}"
    ref, _ = create_cast_draft(store, profile=CAST_PROFILES[cast], date_tag=date_tag)
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="Good morning.\n\nHere is your day.",
        split=True,
    )
    return store.get_ref(kind="draft", id=ref.id)


def _selectable(store: Any, ref_id: int, now: datetime) -> bool:
    """Ask cast_audio's *own* selection predicate about one ref — scoping it to
    a single id keeps the marker + backoff assertions deterministic under the
    shared test DB, without hand-copying the SQL (a copy would keep passing
    against a rule the code had since changed)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT 1 FROM refs WHERE ref_id = %s AND {cast_audio._SELECTABLE_SQL}",
            (ref_id, *cast_audio.selection_params(now, cast_audio._MAX_AGE_HOURS)),
        ).fetchone()
    return row is not None


class TestSelection:
    def test_fresh_cast_is_selectable_then_marker_excludes(self, store: Any) -> None:
        now = datetime.now(UTC)
        ref = _make_cast_draft(store)
        assert _selectable(store, ref.id, now) is True
        assert cast_audio.has_pending_cast(store) is True  # at least this one exists
        store.update_ref(ref.id, meta_patch={"audio_episode_id": "reading-x"})
        assert _selectable(store, ref.id, now) is False

    def test_failure_backoff_excludes_then_expires(self, store: Any) -> None:
        now = datetime.now(UTC)
        ref = _make_cast_draft(store)
        # A recent failure is inside the backoff window → not selectable.
        store.update_ref(ref.id, meta_patch={"audio_failed_at": now.isoformat()})
        assert _selectable(store, ref.id, now) is False
        # An old failure has aged past the window → selectable again.
        old = (
            now - timedelta(minutes=cast_audio._FAIL_BACKOFF_MINUTES + 5)
        ).isoformat()
        store.update_ref(ref.id, meta_patch={"audio_failed_at": old})
        assert _selectable(store, ref.id, now) is True

    def test_first_failure_retries_in_minutes_not_an_hour(self, store: Any) -> None:
        """The backoff is exponential from a short first step.

        The failure this sees in prod is a *killed* render, not a broken one:
        the TTS container lives in the worker's own systemd cgroup, so a deploy
        or a jetsam cull SIGTERMs it mid-render (exit 143). Charging that a flat
        hour is what turned a seconds-long restart into the 2026-08-06 morning
        episode landing at 16:32 UTC instead of ~07:10.
        """
        now = datetime.now(UTC)
        ref = _make_cast_draft(store)
        failed = (now - timedelta(minutes=3)).isoformat()

        # First failure: 2-minute step, so a 3-minute-old failure is eligible.
        store.update_ref(
            ref.id, meta_patch={"audio_failed_at": failed, "audio_fail_count": 1}
        )
        assert _selectable(store, ref.id, now) is True

        # A draft that keeps failing earns a longer wait — 2**5 = 32 minutes.
        store.update_ref(ref.id, meta_patch={"audio_fail_count": 5})
        assert _selectable(store, ref.id, now) is False

        # ...and converges on the ceiling rather than growing without bound.
        store.update_ref(
            ref.id,
            meta_patch={
                "audio_fail_count": 99,
                "audio_failed_at": (
                    now - timedelta(minutes=cast_audio._FAIL_BACKOFF_MINUTES + 1)
                ).isoformat(),
            },
        )
        assert _selectable(store, ref.id, now) is True

    def test_failure_stamp_bumps_the_attempt_counter(self, store: Any) -> None:
        """``_stamp_failure`` is what makes the curve advance — without the
        counter every retry would sit on the 2-minute first step forever."""
        now = datetime.now(UTC)
        ref = _make_cast_draft(store)
        cast_audio._stamp_failure(store, ref, now)
        after_one = store.get_ref(kind="draft", id=ref.id)
        assert after_one is not None
        assert after_one.meta["audio_fail_count"] == 1
        assert after_one.meta["audio_failed_at"] == now.isoformat()

        cast_audio._stamp_failure(store, after_one, now)
        after_two = store.get_ref(kind="draft", id=ref.id)
        assert after_two is not None
        assert after_two.meta["audio_fail_count"] == 2

    def test_failure_stamp_survives_a_garbled_counter(self, store: Any) -> None:
        """Runs on the failure path — a bad meta value must not escalate a
        recoverable render failure into a crashed worker tick."""
        now = datetime.now(UTC)
        ref = _make_cast_draft(store)
        store.update_ref(ref.id, meta_patch={"audio_fail_count": "not-a-number"})
        cast_audio._stamp_failure(store, store.get_ref(kind="draft", id=ref.id), now)
        healed = store.get_ref(kind="draft", id=ref.id)
        assert healed is not None
        assert healed.meta["audio_fail_count"] == 1

    def test_selection_immune_to_updated_at_churn(self, store: Any) -> None:
        """Regression: every failed render stamps ``meta.audio_failed_at``
        via ``store.update_ref``, which bumps ``updated_at``. A stale draft
        that keeps failing must not be able to refresh itself back to the
        top of the selection window via that churn — selection has to be
        pinned to COMPOSE time (``created_at``), which a metadata patch
        never touches."""
        now = datetime.now(UTC)
        old_ref = _make_cast_draft(store)
        recent_ref = _make_cast_draft(store)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET created_at = %s, updated_at = %s WHERE ref_id = %s",
                (now - timedelta(days=5), now, old_ref.id),
            )
            conn.commit()
        picked = cast_audio._latest_unnarrated_cast(store, max_age_hours=48, now=now)
        assert picked is not None
        assert picked.id == recent_ref.id, (
            "the old-composed draft's churned updated_at must not win selection"
        )


class TestNarrateTail:
    def _patch_render(self, monkeypatch: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_render_episode(segments: Any, out: Any, **kw: Any) -> dict[str, Any]:
            captured["segments"] = list(segments)
            return {"segments": len(list(captured["segments"])), "duration_s": 3.0}

        def fake_publish(podcast_dir: Any, audio_path: Any, **kw: Any) -> Any:
            captured["publish"] = kw
            return SimpleNamespace(id=kw["episode_id"], bytes=1, mime="audio/mp4")

        from precis import audio_feed

        monkeypatch.setattr(cast_audio, "render_episode", fake_render_episode)
        monkeypatch.setattr(audio_feed, "publish_episode", fake_publish)
        return captured

    def test_publishes_and_stamps_marker(self, store: Any, monkeypatch: Any) -> None:
        captured = self._patch_render(monkeypatch)
        ref = _make_cast_draft(store, cast="reading")

        r = cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir="/tmp/pods"
        )

        assert r["published"] is True
        # The reading cast's human episode id is its export stem, not the
        # internal cast key: ``morning_brief_<date>`` (cast_common.export_stem).
        assert r["episode_id"].startswith("morning_brief_")
        # The reading cast publishes under the distinct producer tag "brief"
        # (not "reading" — that borrowed tag collided with nidra's episodes).
        assert captured["publish"]["source"] == "brief"
        assert captured["publish"]["duration_seconds"] == 3
        # Idempotency marker stamped on the draft.
        after = store.get_ref(kind="draft", id=ref.id)
        assert (after.meta or {}).get("audio_episode_id") == r["episode_id"]

    def test_dry_run_does_not_stamp(self, store: Any, monkeypatch: Any) -> None:
        self._patch_render(monkeypatch)
        ref = _make_cast_draft(store, cast="nidra")

        r = cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir=None, publish=False
        )

        assert r["published"] is False
        assert r["reason"] == "dry-run"
        after = store.get_ref(kind="draft", id=ref.id)
        assert "audio_episode_id" not in (after.meta or {})

    def test_render_episode_gets_no_scalar_pause_kw(
        self, store: Any, monkeypatch: Any
    ) -> None:
        # The pause is now a per-segment NarrationSegment.gap_after (stamped by
        # _news_lead_in), not a CastProfile-derived scalar forwarded here.
        captured_kw: dict[str, Any] = {}

        def fake_render_episode(segments: Any, out: Any, **kw: Any) -> dict[str, Any]:
            captured_kw.update(kw)
            return {"segments": len(list(segments)), "duration_s": 3.0}

        def fake_publish(podcast_dir: Any, audio_path: Any, **kw: Any) -> Any:
            return SimpleNamespace(id=kw["episode_id"], bytes=1, mime="audio/mp4")

        from precis import audio_feed

        monkeypatch.setattr(cast_audio, "render_episode", fake_render_episode)
        monkeypatch.setattr(audio_feed, "publish_episode", fake_publish)
        ref = _make_cast_draft(store, cast="reading")

        cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir="/tmp/pods"
        )

        assert "pause_s" not in captured_kw


class TestNewsLeadIn:
    """Workstream B: the reading cast prepends today's news wire (read in the
    brief's own voice) ahead of the personal brief's segments — one combined
    morning episode."""

    def _seed_news(self, store: Any, date_tag: str, text: str) -> Any:
        news = store.insert_ref(
            kind="news",
            slug=f"briefing-{date_tag}",
            title=f"Morning briefing — {date_tag}",
            meta={"briefing": True, "date": date_tag},
        )
        store.add_chunks(
            ref_id=news.id, chunk_kind="paragraph", text=text, split=True, kind="news"
        )
        return news

    def _patch_render(self, monkeypatch: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_render_episode(segments: Any, out: Any, **kw: Any) -> dict[str, Any]:
            captured["segments"] = list(segments)
            return {"segments": len(captured["segments"]), "duration_s": 3.0}

        def fake_publish(podcast_dir: Any, audio_path: Any, **kw: Any) -> Any:
            captured["publish"] = kw
            return SimpleNamespace(id=kw["episode_id"], bytes=1, mime="audio/mp4")

        from precis import audio_feed

        monkeypatch.setattr(cast_audio, "render_episode", fake_render_episode)
        monkeypatch.setattr(audio_feed, "publish_episode", fake_publish)
        return captured

    def test_news_segments_prepended_ahead_of_the_brief(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="precis.workers.cast_audio")
        captured = self._patch_render(monkeypatch)
        date_tag = f"news-{uuid.uuid4().hex[:8]}"
        self._seed_news(store, date_tag, "A world-news headline for the wire.")

        ref, _ = create_cast_draft(
            store, profile=CAST_PROFILES["reading"], date_tag=date_tag
        )
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Good morning. Here is your personal brief.",
            split=True,
        )
        ref = store.get_ref(kind="draft", id=ref.id)

        r = cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir="/tmp/pods"
        )

        assert r["published"] is True
        segments = captured["segments"]
        assert len(segments) >= 2
        # The news content leads; the brief's own content follows.
        assert "headline" in segments[0].text
        assert any("personal brief" in s.text for s in segments[1:])
        # News is narrated in the reading cast's own voice (bm_george).
        assert segments[0].voice == CAST_PROFILES["reading"].voice
        # Still one combined episode, unchanged id/source.
        assert r["episode_id"].startswith("morning_brief_")
        assert captured["publish"]["source"] == "brief"
        # The news lead-in carries the wider ~1.5s inter-story beat; the
        # brief's own segments keep the container's default (no override).
        brief_segments = [s for s in segments if "personal brief" in s.text]
        news_segments = [s for s in segments if s not in brief_segments]
        assert news_segments and all(s.gap_after == 1.5 for s in news_segments)
        assert brief_segments and all(s.gap_after is None for s in brief_segments)
        # Observability: the combine path logs what it prepended.
        assert any("news lead-in prepended" in rec.message for rec in caplog.records)

    def test_degrades_to_brief_only_when_no_news_ref(
        self, store: Any, monkeypatch: Any, caplog: Any
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="precis.workers.cast_audio")
        captured = self._patch_render(monkeypatch)
        date_tag = f"nonews-{uuid.uuid4().hex[:8]}"
        ref, _ = create_cast_draft(
            store, profile=CAST_PROFILES["reading"], date_tag=date_tag
        )
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Good morning. Brief-only content.",
            split=True,
        )
        ref = store.get_ref(kind="draft", id=ref.id)

        r = cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir="/tmp/pods"
        )

        assert r["published"] is True  # still publishes, one episode
        segments = captured["segments"]
        assert all("headline" not in s.text for s in segments)
        assert any("Brief-only content" in s.text for s in segments)
        # Observability: the degrade path logs why it skipped the news lead-in.
        assert any("news lead-in skipped" in rec.message for rec in caplog.records)

    def test_nidra_never_gets_news_prepended(
        self, store: Any, monkeypatch: Any
    ) -> None:
        captured = self._patch_render(monkeypatch)
        date_tag = f"nidra-{uuid.uuid4().hex[:8]}"
        self._seed_news(store, date_tag, "A world-news headline for the wire.")

        ref, _ = create_cast_draft(
            store, profile=CAST_PROFILES["nidra"], date_tag=date_tag
        )
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="Settle in, and let the breath slow.",
            split=True,
        )
        ref = store.get_ref(kind="draft", id=ref.id)

        cast_audio.narrate_cast_ref(
            store, ref, image=None, synth=object(), podcast_dir="/tmp/pods"
        )

        segments = captured["segments"]
        assert all("headline" not in s.text for s in segments)
