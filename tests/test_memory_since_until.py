"""gr797062: a queryless dated tag listing on kind='memory' must not
reach the handler's since=/until= refusal."""

from __future__ import annotations

from precis.runtime import PrecisRuntime


def test_memory_tag_listing_with_since_until(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    rt.dispatch("put", {"kind": "memory", "text": "alpha note", "tags": ["project:x"]})
    body, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "memory",
            "tags": ["project:x"],
            "page_size": 100,
            "__extras__": {"since": "2000-01-01", "until": "2999-01-01"},
        },
    )
    assert "apply only to" not in body, body
    assert not is_error, body
    assert "alpha note" in body


def test_memory_tag_listing_window_excludes(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    rt.dispatch("put", {"kind": "memory", "text": "beta note", "tags": ["project:y"]})
    body, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "memory",
            "tags": ["project:y"],
            "__extras__": {"until": "2000-01-01"},
        },
    )
    assert not is_error, body
    assert "beta note" not in body
