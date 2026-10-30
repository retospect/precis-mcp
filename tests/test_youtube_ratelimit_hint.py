"""YouTube throttle errors reached through the dispatcher (gr531240)."""

from __future__ import annotations

import importlib.util

import pytest

from precis.runtime import PrecisRuntime

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pysbd") is None,
    reason="full-hub boot needs the [paper] extra",
)


def test_youtube_rate_limit_carries_a_retry_hint(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr531240: a throttled transcript fetch is Upstream with a wait and a
    pacing rule, not the bare ``YouTube API error: RateLimited``."""
    import sys
    from typing import Any

    class RateLimited(Exception):
        pass

    class _Api:
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            raise RateLimited("RateLimited")

    fake_yt = type(sys)("youtube_transcript_api")
    fake_yt.YouTubeTranscriptApi = _Api  # type: ignore[attr-defined]
    fake_errors = type(sys)("youtube_transcript_api._errors")
    for name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable"):
        setattr(fake_errors, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake_yt)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", fake_errors)

    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": "dQw4w9WgXcQ"}
    )
    assert err
    assert "[error:Upstream]" in out and "RateLimited" in out
    assert "wait about 60 s, then retry: get(kind='youtube', id='dQw4w9WgXcQ')" in out
    assert "one at a time" in out


def test_video_id_containing_429_is_not_a_throttle() -> None:
    from precis.handlers.youtube import _upstream_error

    plain = _upstream_error(
        RuntimeError("could not retrieve abc429xyz"),
        default_next="retry",
        retry_call="get()",
    )
    assert plain.next == "retry"
    throttled = _upstream_error(
        RuntimeError("HTTP 429 Too Many Requests"), default_next="retry", retry_call="g"
    )
    assert throttled.next != "retry"
