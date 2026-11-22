"""gr504803: delete/tag take ``reason=`` — the audit line for a cleanup.

An explicit ``reason=`` on either verb lands as an ``agent/deleted`` /
``agent/retagged`` row on each affected ref's ``ref_events`` log, so a
mass cleanup (soft-deleting refs, folding tags) says why. The row only
fires when the caller passed a reason: silent writes and comment
hide/restore (which has its own ``comment_hidden`` row) stay off the
log. Tag ``reason=`` covers the numeric-ref kinds and paper (gr688475);
other slug kinds' tag doesn't declare the door and refuses it loudly.
"""

from __future__ import annotations

import re

import pytest

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


def test_pcb_delete_reason_lands_on_the_event_log(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr523119: ``delete(kind='pcb')`` takes ``reason=`` like the numeric
    kinds — a design delete is a ref soft-delete (``retire_ref`` inside
    ``pcb_delete``), so it lands the same ``agent/deleted`` audit row.
    Dispatched top-level it used to die on the strictness gate before
    reaching the handler at all."""
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "pcb",
            "id": "callerr-a-board",
            "args": {
                "components": [
                    {
                        "refdes": "MH1",
                        "label": "mounting hole",
                        "pins": [{"name": "1"}],
                    }
                ]
            },
        },
    )
    assert "[error" not in out, out
    ref = store.get_ref(kind="pcb", id="callerr-a-board")
    assert ref is not None

    out = runtime_with_store.dispatch(
        "delete",
        {
            "kind": "pcb",
            "id": "callerr-a-board",
            "reason": "dogfood fork, folded",
        },
    )
    assert "[error" not in out, out
    assert "retired pcb design" in out, out
    assert _reason_events(store, ref.id, "deleted") == ["dogfood fork, folded"]


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


def test_paper_tag_reason_lands_on_the_event_log(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr688475: ``tag(kind='paper')`` declares ``reason=`` like the
    numeric kinds. A mass fold removing a quest tag from a sweep of
    papers used to die on the strict-kwargs gate (``does not accept
    ['reason']``) because ``PaperHandler.tag`` never declared the
    door — the call-errors cluster's exact shape."""
    ref = store.insert_ref(
        kind="paper", slug="wang2020state", title="T", provider="manual", meta={}
    )
    out = runtime_with_store.dispatch(
        "tag",
        {
            "kind": "paper",
            "id": "wang2020state",
            "remove": ["quest:164903"],
            "reason": "quest:164903 folded into quest:164900",
        },
    )
    assert "[error" not in out, out
    assert _reason_events(store, ref.id, "retagged") == [
        "quest:164903 folded into quest:164900"
    ]
    # Reasonless paper tags stay off the log, same as the numeric kinds.
    out = runtime_with_store.dispatch(
        "tag", {"kind": "paper", "id": "wang2020state", "add": ["topic:noxrr"]}
    )
    assert "[error" not in out, out
    assert _reason_events(store, ref.id, "retagged") == [
        "quest:164903 folded into quest:164900"
    ]


@pytest.mark.parametrize("explicit_kind", [True, False])
def test_paper_tag_batch(
    runtime_with_store: PrecisRuntime, store: Store, explicit_kind: bool
) -> None:
    first = store.insert_ref(
        kind="paper", slug="batch-first", title="First", provider="manual"
    )
    second = store.insert_ref(
        kind="paper", slug="batch-second", title="Second", provider="manual"
    )
    for ref in (first, second):
        seed = runtime_with_store.dispatch(
            "tag", {"kind": "paper", "id": ref.id, "add": ["old-reading-list"]}
        )
        assert f"updated paper {ref.slug}: +1 tag" in seed, seed
    ids: list[str | int] = [f"pa{first.id}", f"pa{second.id}"]
    kind = {"kind": "paper"} if explicit_kind else {}
    if explicit_kind:
        ids.extend(["batch-first", second.id])
    else:
        ids.append("batch-first")
    out = runtime_with_store.dispatch(
        "tag",
        {
            **kind,
            "id": ids,
            "add": ["aixmat-2026"],
            "remove": ["old-reading-list"],
            "reason": "  conference reading list  ",
        },
    )
    assert "tagged 2 paper refs in one transaction" in out, out
    # Both papers are addressed by handle first, but receipts use cite keys.
    receipts = [line for line in out.splitlines() if line.startswith("updated paper ")]
    assert receipts == [
        "updated paper batch-first: +1 tag, -1 tag",
        "updated paper batch-second: +1 tag, -1 tag",
    ], out
    for ref in (first, second):
        tags = {tag.value for tag in store.tags_for(ref.id)}
        assert "aixmat-2026" in tags
        assert "old-reading-list" not in tags
        assert _reason_events(store, ref.id, "retagged") == ["conference reading list"]


def test_paper_tag_batch_rejects_empty_list(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "tag", {"kind": "paper", "id": [], "add": ["aixmat-2026"]}
    )
    assert "[error:BadInput]" in out
    assert "id=[] is empty" in out


def test_paper_tag_batch_rejects_missing_paper_before_writes(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(
        kind="paper", slug="batch-first", title="First", provider="manual"
    )
    out = runtime_with_store.dispatch(
        "tag",
        {
            "kind": "paper",
            "id": [ref.id, "absent-paper"],
            "add": ["aixmat-2026"],
            "reason": "conference reading list",
        },
    )
    assert "[error:NotFound]" in out
    assert "absent-paper" in out
    assert "aixmat-2026" not in {tag.value for tag in store.tags_for(ref.id)}
    assert _reason_events(store, ref.id, "retagged") == []


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
