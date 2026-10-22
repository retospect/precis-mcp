"""gr504803: delete/tag take ``reason=`` — the audit line for a cleanup.

An explicit ``reason=`` on either verb lands as an ``agent/deleted`` /
``agent/retagged`` row on each affected ref's ``ref_events`` log, so a
mass cleanup (soft-deleting refs, folding tags) says why. The row only
fires for numeric-ref kinds when the caller passed a reason: silent
writes, file/slug kinds, and comment hide/restore (which has its own
``comment_hidden`` row) stay off the log.
"""

from __future__ import annotations

import re

from precis.runtime import PrecisRuntime
from precis.store import Store


def _put_memory(runtime: PrecisRuntime, text: str) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1))


def _put_gripe(runtime: PrecisRuntime, text: str) -> int:
    out = runtime.dispatch("put", {"kind": "gripe", "text": text})
    m = re.search(r"created gripe id=(\d+)", out)
    assert m, out
    return int(m.group(1))


def _reason_events(store: Store, ref_id: int, event: str) -> list[str | None]:
    return [
        str(e.payload.get("reason"))
        for e in store.events_for(ref_id, source="agent", event=event)
    ]


def test_delete_reason_lands_on_the_event_log(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "stale claim to retire")
    out = runtime_with_store.dispatch(
        "delete",
        {"kind": "memory", "id": mid, "reason": "dupe of me1, folded in cleanup"},
    )
    assert f"deleted memory id={mid}" in out, out
    assert _reason_events(store, mid, "deleted") == ["dupe of me1, folded in cleanup"]


def test_delete_reason_accepts_handle_and_kind_prefixed_ids(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "by handle")
    out = runtime_with_store.dispatch(
        "delete", {"kind": "memory", "id": f"me{mid}", "reason": "by handle"}
    )
    assert f"deleted memory id={mid}" in out, out
    assert _reason_events(store, mid, "deleted") == ["by handle"]
    mid2 = _put_memory(runtime_with_store, "by kind prefix")
    out = runtime_with_store.dispatch(
        "delete", {"kind": "memory", "id": f"memory:{mid2}", "reason": "by prefix"}
    )
    assert f"deleted memory id={mid2}" in out, out
    assert _reason_events(store, mid2, "deleted") == ["by prefix"]


def test_tag_reason_lands_and_shows_in_view_log(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "tag me")
    out = runtime_with_store.dispatch(
        "tag",
        {"kind": "memory", "id": mid, "add": ["stale"], "reason": "annual sweep"},
    )
    assert "[error" not in out, out
    assert _reason_events(store, mid, "retagged") == ["annual sweep"]
    log = runtime_with_store.dispatch(
        "get", {"kind": "memory", "id": mid, "view": "log"}
    )
    assert "retagged" in log, log
    assert "reason='annual sweep'" in log, log


def test_tag_reason_batch_writes_one_row_per_ref(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = _put_memory(runtime_with_store, "batch a")
    b = _put_memory(runtime_with_store, "batch b")
    out = runtime_with_store.dispatch(
        "tag",
        {
            "kind": "memory",
            "id": [a, b],
            "remove": ["pinned"],
            "reason": "folded into me1",
        },
    )
    assert "[error" not in out, out
    assert _reason_events(store, a, "retagged") == ["folded into me1"]
    assert _reason_events(store, b, "retagged") == ["folded into me1"]


def test_reasonless_writes_leave_no_audit_rows(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "no reason given")
    runtime_with_store.dispatch("tag", {"kind": "memory", "id": mid, "add": ["x"]})
    runtime_with_store.dispatch("delete", {"kind": "memory", "id": mid})
    assert _reason_events(store, mid, "retagged") == []
    assert _reason_events(store, mid, "deleted") == []


def test_comment_hide_with_reason_is_not_a_deleted_row(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    gid = _put_gripe(runtime_with_store, "a gripe with a comment")
    runtime_with_store.dispatch(
        "put", {"kind": "gripe", "id": gid, "text": "a stale comment"}
    )
    out = runtime_with_store.dispatch(
        "delete",
        {
            "kind": "gripe",
            "id": gid,
            "reason": "comment was wrong",
            "__extras__": {"comment": 1},
        },
    )
    assert "[error" not in out, out
    assert _reason_events(store, gid, "deleted") == []
    # The comment op's own audit row carries through untouched.
    assert [
        str(e.payload.get("comment"))
        for e in store.events_for(gid, source="agent", event="comment_hidden")
    ] == ["1"]
