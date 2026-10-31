"""Tests for the ``youtube_bulk`` job_type + the ``fetch_many`` put door
(gr531240).

``youtube_transcript_api`` is stubbed at the import boundary (same
shape as ``test_youtube.py``) — no network. ``youtube_bulk.drain_sleep``
is patched so a throttle backoff records without sleeping.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.youtube import YouTubeHandler
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, known_job_types, youtube_bulk
from precis.workers.registry import SERVICES_BY_NAME, ServiceKind

# ── stub youtube-transcript-api at the import boundary ────────────────


class _Snippet:
    def __init__(self, text: str) -> None:
        self.text = text


class _Blocked(Exception):
    """Stand-in for the client's RequestBlocked — mapped onto precis
    ``RateLimited`` by ``YouTubeHandler._fetch``'s blocked_errors."""


class _StubApi:
    """``fetch`` returns a two-snippet transcript; ids listed in
    ``throttle_once`` raise the client blocked error exactly once (the
    job's backoff+retry then lands them)."""

    fetch_calls: list[str] = []
    throttle_once: set[str] = set()

    @classmethod
    def reset(cls) -> None:
        cls.fetch_calls = []
        cls.throttle_once = set()

    def fetch(self, video_id: str, languages: list[str]) -> Any:
        type(self).fetch_calls.append(video_id)
        if video_id in type(self).throttle_once:
            type(self).throttle_once.discard(video_id)
            raise _Blocked(video_id)
        return [_Snippet("hello"), _Snippet("world")]

    def list(self, video_id: str) -> Any:
        return []


@pytest.fixture(autouse=True)
def _patch_yt(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubApi.reset()
    fake_yt = type(sys)("youtube_transcript_api")
    setattr(fake_yt, "YouTubeTranscriptApi", _StubApi)

    fake_errors = type(sys)("youtube_transcript_api._errors")

    class _Boom(Exception):
        pass

    class TranscriptsDisabled(_Boom):
        pass

    class NoTranscriptFound(_Boom):
        pass

    class VideoUnavailable(_Boom):
        pass

    setattr(fake_errors, "TranscriptsDisabled", TranscriptsDisabled)
    setattr(fake_errors, "NoTranscriptFound", NoTranscriptFound)
    setattr(fake_errors, "VideoUnavailable", VideoUnavailable)
    setattr(fake_errors, "RequestBlocked", _Blocked)
    setattr(fake_errors, "IpBlocked", _Blocked)

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake_yt)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", fake_errors)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Throttle backoffs record but never sleep."""
    monkeypatch.setattr(youtube_bulk, "drain_sleep", lambda _s: False)


def _job_ref(store: Store, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """A real ``kind='job'`` ref — ``renew_own_lease``'s NULL-safe lease
    identity match needs the row to exist (ref_id=0 reads as a lost
    lease and the drain stops before any fetch)."""
    meta: dict[str, Any] = {"job_type": youtube_bulk.JOB_TYPE, "params": params}
    ref = store.insert_ref(kind="job", slug=None, title="youtube_bulk", meta=meta)
    return ref.id, meta


def _ctx(store: Store, params: dict[str, Any]) -> DispatchContext:
    ref_id, meta = _job_ref(store, params)
    return DispatchContext(
        store=store,
        ref_id=ref_id,
        title="youtube_bulk",
        meta=meta,
        set_status=lambda s: None,
        append_chunk=lambda k, t: None,
        set_meta=lambda **_kw: None,
        record_failure=lambda msg, **kw: (_ for _ in ()).throw(
            AssertionError(f"record_failure: {msg}")
        ),
        is_cancel_requested=lambda: False,
    )


def _cached(store: Store, vid: str, lang_part: str = "en") -> bool:
    h = YouTubeHandler(hub=Hub(store=store))
    entry = store.get_cache_entry(
        provider="youtube", request_hash=h._hash(f"{vid}:{lang_part}")
    )
    return entry is not None and h._is_fresh(entry[1])


def _seed_cached(store: Store, vid: str) -> None:
    h = YouTubeHandler(hub=Hub(store=store))
    store.put_cache_entry(
        kind="youtube",
        slug=vid,
        title=f"cached {vid}",
        body_blocks=[ChunkInsert(ord=0, text="old transcript")],
        provider="youtube",
        request_hash=h._hash(f"{vid}:en"),
        ttl_seconds=3600,
        cache_meta={"video_id": vid, "languages": ["en"]},
    )


# ── registration ──────────────────────────────────────────────────────


def test_registered_as_inproc_job_type() -> None:
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "youtube_bulk" in known_job_types()
    svc = SERVICES_BY_NAME["youtube_bulk"]
    assert svc.kind is ServiceKind.JOB
    assert svc.doc_skill == "precis-youtube-help"


# ── dispatch ──────────────────────────────────────────────────────────


def test_bulk_fetch_five_ids_with_one_throttle(store: Store) -> None:
    """5 ids, one RateLimited mid-list: every id ends cached, the backoff
    is recorded, and the job does not fail."""
    ids = [f"vid{i:05d}xxx" for i in range(5)]
    _StubApi.throttle_once = {ids[2]}
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    result = spec.dispatch(_ctx(store, {"ids": ids}), spec)
    assert result == {"ok": 5, "failed": 0, "skipped": 0}
    for vid in ids:
        assert _cached(store, vid), vid
    # The throttled id was fetched twice: blocked once, then the retry.
    assert _StubApi.fetch_calls.count(ids[2]) == 2


def test_backoff_is_recorded_in_progress(store: Store) -> None:
    metas: list[dict[str, Any]] = []
    chunks: list[tuple[str, str]] = []
    _StubApi.throttle_once = {"vid00000xxx"}
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    ref_id, meta = _job_ref(store, {"ids": ["vid00000xxx"]})
    ctx = DispatchContext(
        store=store,
        ref_id=ref_id,
        title="youtube_bulk",
        meta=meta,
        set_status=lambda s: None,
        append_chunk=lambda k, t: chunks.append((k, t)),
        set_meta=lambda **kw: metas.append(kw),
        record_failure=lambda msg, **kw: None,
        is_cancel_requested=lambda: False,
    )
    spec.dispatch(ctx, spec)
    assert metas and metas[-1]["progress"]["backoffs"] == 1
    assert any("backing off" in t for k, t in chunks if k == "job_event")
    assert any(k == "job_result" for k, _t in chunks)


def test_already_cached_id_is_skipped(store: Store) -> None:
    _seed_cached(store, "vid00000xxx")
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    result = spec.dispatch(_ctx(store, {"ids": ["vid00000xxx", "vid00001xxx"]}), spec)
    assert result == {"ok": 1, "failed": 0, "skipped": 1}
    assert _StubApi.fetch_calls == ["vid00001xxx"]


def test_resubmit_is_a_noop_for_done_ids(store: Store) -> None:
    ids = [f"vid{i:05d}xxx" for i in range(3)]
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    spec.dispatch(_ctx(store, {"ids": ids}), spec)
    assert len(_StubApi.fetch_calls) == 3
    _StubApi.fetch_calls.clear()
    result = spec.dispatch(_ctx(store, {"ids": ids}), spec)
    assert result == {"ok": 0, "failed": 0, "skipped": 3}
    assert _StubApi.fetch_calls == []


def test_persistent_throttle_fails_the_id_not_the_job(store: Store) -> None:

    class AlwaysBlocked(_StubApi):
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise _Blocked(video_id)

    sys.modules["youtube_transcript_api"].__dict__["YouTubeTranscriptApi"] = (
        AlwaysBlocked
    )
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.dispatch is not None
    chunks: list[tuple[str, str]] = []
    ref_id, meta = _job_ref(store, {"ids": ["vid00000xxx", "vid00001xxx"]})
    ctx = DispatchContext(
        store=store,
        ref_id=ref_id,
        title="youtube_bulk",
        meta=meta,
        set_status=lambda s: None,
        append_chunk=lambda k, t: chunks.append((k, t)),
        set_meta=lambda **kw: None,
        record_failure=lambda msg, **kw: chunks.append(("fail", msg)),
        is_cancel_requested=lambda: False,
    )
    spec.dispatch(ctx, spec)
    # Every id throttled every attempt — the whole run records a failure
    # (the paper_pull ruling), it does not silently 'done'.
    assert any(k == "fail" for k, _ in chunks)


# ── submit door ───────────────────────────────────────────────────────


def test_fetch_many_mints_job_under_ephemeral_todo(store: Store, hub: Hub) -> None:
    h = YouTubeHandler(hub=hub)
    resp = h.put(
        args={
            "op": "fetch_many",
            "ids": ["vid00000xxx", "https://youtu.be/vid00001xxx"],
        }
    )
    assert resp.ref_id is not None
    assert "queued" in resp.body
    with store.pool.connection() as conn:
        job = conn.execute(
            "SELECT meta->>'job_type', meta->'params', parent_id "
            "FROM refs WHERE ref_id = %s",
            (resp.ref_id,),
        ).fetchone()
        assert job is not None
        assert job[0] == "youtube_bulk"
        assert sorted(job[1]["ids"]) == ["vid00000xxx", "vid00001xxx"]
        parent = conn.execute(
            "SELECT kind, retired_at, meta->'auto_check' FROM refs WHERE ref_id = %s",
            (job[2],),
        ).fetchone()
        assert parent is not None
        assert parent[0] == "todo" and parent[1] is None
        assert parent[2] == {"type": "child_job_succeeded"}


def test_fetch_many_resubmit_reuses_inflight_job(store: Store, hub: Hub) -> None:
    h = YouTubeHandler(hub=hub)
    args: dict[str, Any] = {
        "op": "fetch_many",
        "ids": ["vid00000xxx"],
    }
    first = h.put(args=args)
    second = h.put(args=args)
    assert second.reused is True and second.ref_id == first.ref_id


def test_fetch_many_rejects_bad_id(store: Store, hub: Hub) -> None:
    h = YouTubeHandler(hub=hub)
    with pytest.raises(BadInput, match="not parseable"):
        h.put(args={"op": "fetch_many", "ids": ["not a video"]})


def test_fetch_many_rejects_unknown_op(store: Store, hub: Hub) -> None:
    h = YouTubeHandler(hub=hub)
    with pytest.raises(BadInput, match="unknown put op"):
        h.put(args={"op": "explode"})


def test_job_put_validates_ids(store: Store, hub: Hub) -> None:
    """A direct put(kind='job') hits the same parse gate via the spec's
    ``validate`` hook."""
    spec = get_job_type("youtube_bulk")
    assert spec is not None and spec.validate is not None
    with pytest.raises(BadInput, match="not parseable"):
        spec.validate(store, {"ids": ["garbage??"]})
    with pytest.raises(BadInput, match="non-empty"):
        spec.validate(store, {"ids": []})


def test_parse_ids_dedupes_url_forms() -> None:
    clean, bad = youtube_bulk.parse_ids(
        [
            "vid00000xxx",
            "https://youtu.be/vid00000xxx",
            "https://www.youtube.com/watch?v=vid00001xxx",
            "junk",
        ]
    )
    assert clean == ["vid00000xxx", "vid00001xxx"]
    assert bad == ["junk"]
