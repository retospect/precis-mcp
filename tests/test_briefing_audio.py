"""briefing_audio — the news-briefing → podcast-feed producer.

Drives the whole producer over a fake store + fake synth (no PG, no TTS
toolchain beyond numpy/soundfile, which come with the [tts] extra), so it
exercises: finding the latest un-narrated briefing, rendering its markdown to
audio, publishing an episode, and the idempotency marker that stops a re-run
double-publishing.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("numpy")
pytest.importorskip("soundfile")

import numpy as np

from precis import audio_feed
from precis.workers.briefing_audio import has_pending_briefing, run_briefing_audio

_NOW = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)

_BRIEF = (
    "## Top stories\n\n"
    "[Rates held](https://x/a) steady.\n\n"
    "## United States\n\n"
    "**Worth watching**: the vote."
)


class _Ref:
    def __init__(self, ref_id: int, meta: dict[str, Any], updated_at: datetime) -> None:
        self.id = ref_id
        self.meta = meta
        self.updated_at = updated_at
        self.title = "Morning briefing — 2026-07-14"
        self.slug = "briefing-2026-07-14"


class _Cursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        if "FROM refs" in sql:
            ref = self._s.ref
            if ref is not None and "audio_episode_id" not in ref.meta:
                return _Cursor([(ref.id,)])
            return _Cursor([])
        if "FROM chunks" in sql:
            paras = [p for p in self._s.body.split("\n\n") if p.strip()]
            return _Cursor([(p,) for p in paras])
        raise AssertionError(f"unexpected SQL: {sql}")


class _Pool:
    def __init__(self, store: _Store) -> None:
        self._s = store

    @contextmanager
    def connection(self):
        yield _Conn(self._s)


class _Store:
    def __init__(self, ref: _Ref, body: str) -> None:
        self.ref = ref
        self.body = body
        self.pool = _Pool(self)

    def get_ref(self, *, kind: str, id: int) -> _Ref:
        assert kind == "news" and id == self.ref.id
        return self.ref

    def update_ref(self, ref_id: int, *, meta_patch: dict[str, Any]) -> None:
        assert ref_id == self.ref.id
        self.ref.meta.update(meta_patch)


class _FakeSynth:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, text: str, *, voice: str, lang: str):
        self.calls.append(text)
        return np.zeros(2400, dtype=np.float32), 24000  # 0.1s


def _store() -> _Store:
    return _Store(_Ref(42, {"briefing": True, "date": "2026-07-14"}, _NOW), _BRIEF)


def test_publishes_episode_and_stamps_marker(tmp_path):
    store, synth = _store(), _FakeSynth()
    r = run_briefing_audio(
        store, synth=synth, podcast_dir=tmp_path, now=_NOW, encode=None
    )
    assert r["published"] is True and r["episode_id"] == "news-2026-07-14"
    assert r["ref_id"] == 42 and r["segments"] == 4
    # The synth saw the cleaned prose (heading text, link anchor, no URL/markup).
    assert synth.calls[0] == "Top stories"
    assert synth.calls[1] == "Rates held steady."
    # Episode landed on the feed.
    eps = audio_feed.list_episodes(tmp_path)
    assert len(eps) == 1 and eps[0].id == "news-2026-07-14" and eps[0].source == "news"
    # Idempotency marker stamped.
    assert store.ref.meta["audio_episode_id"] == "news-2026-07-14"


def test_second_run_is_idempotent(tmp_path):
    store, synth = _store(), _FakeSynth()
    run_briefing_audio(store, synth=synth, podcast_dir=tmp_path, now=_NOW, encode=None)
    again = run_briefing_audio(
        store, synth=_FakeSynth(), podcast_dir=tmp_path, now=_NOW, encode=None
    )
    assert again["published"] is False and again["reason"] == "no-unnarrated-briefing"
    assert len(audio_feed.list_episodes(tmp_path)) == 1  # no double-publish


def test_dry_run_renders_without_publishing_or_marking(tmp_path):
    store, synth = _store(), _FakeSynth()
    r = run_briefing_audio(
        store, synth=synth, podcast_dir=tmp_path, now=_NOW, encode=None, publish=False
    )
    assert r["published"] is False and r["reason"] == "dry-run" and r["segments"] == 4
    assert audio_feed.list_episodes(tmp_path) == []  # nothing published
    assert (
        "audio_episode_id" not in store.ref.meta
    )  # not marked → a real run still fires


def test_no_briefing_is_a_clean_noop(tmp_path):
    store = _Store(_Ref(1, {}, _NOW), _BRIEF)
    store.ref = None  # type: ignore[assignment]
    r = run_briefing_audio(store, synth=_FakeSynth(), podcast_dir=tmp_path, now=_NOW)
    assert r["published"] is False and r["reason"] == "no-unnarrated-briefing"


def test_has_pending_briefing_gates_on_marker_and_presence():
    # The cheap gate the worker checks before building the (heavy) synth.
    store = _store()
    assert has_pending_briefing(store, now=_NOW) is True
    store.ref.meta["audio_episode_id"] = "news-2026-07-14"  # narrated
    assert has_pending_briefing(store, now=_NOW) is False
    store.ref = None  # nothing at all
    assert has_pending_briefing(store, now=_NOW) is False
