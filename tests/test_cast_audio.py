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
    """Replicate cast_audio's selection predicate, scoped to one ref — locks the
    marker + backoff exclusion behaviour deterministically under a shared DB."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND kind='draft' "
            "AND deleted_at IS NULL AND meta ? 'cast' "
            "AND NOT (meta ? 'audio_episode_id') "
            "AND updated_at >= %s "
            "AND (meta->>'audio_failed_at' IS NULL "
            "     OR (meta->>'audio_failed_at')::timestamptz < %s)",
            (
                ref_id,
                now - timedelta(hours=cast_audio._MAX_AGE_HOURS),
                now - timedelta(minutes=cast_audio._FAIL_BACKOFF_MINUTES),
            ),
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
        assert r["episode_id"].startswith("reading-")
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
