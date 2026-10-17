"""gr494831 (c): hint follow-through over the tool_calls ledger, on fixture
rows in the dev DB."""

from __future__ import annotations

from uuid import uuid4

from precis.store import Store
from precis.tool_ledger import ToolCallRecord, record_call
from precis.workers.call_errors import aggregate_followthrough, hint_followthrough


def _call(
    store: Store, sid: str, seq: int, kind: str, outcome: str, head: str | None = None
) -> None:
    record_call(
        store,
        ToolCallRecord(
            verb="get",
            kind=kind,
            input_keys=[],
            outcome=outcome,
            error_type="BadInput" if outcome == "error" else None,
            session_id=sid,
            seq=seq,
            error_head=head,
        ),
    )


def test_followthrough_counts_ok_error_and_none(store: Store) -> None:
    kind = f"ft{uuid4().hex[:8]}"
    head = "[error:BadInput] unknown view 'x1' for kind=thing"
    # s1: error, other-kind call, then same kind ok  -> ok (within 2)
    s1, s2, s3, s4 = (uuid4().hex for _ in range(4))
    _call(store, s1, 1, kind, "error", head)
    _call(store, s1, 2, "skill", "ok")
    _call(store, s1, 3, kind, "ok")
    # s2: error, then same kind error again -> error
    _call(store, s2, 1, kind, "error", head.replace("x1", "x22"))
    _call(store, s2, 2, kind, "error", head)
    # s3: error, two other-kind calls, then same kind ok -> outside window: none
    _call(store, s3, 1, kind, "error", head)
    _call(store, s3, 2, "skill", "ok")
    _call(store, s3, 3, "skill", "ok")
    _call(store, s3, 4, kind, "ok")
    # s4: error, no further calls -> none
    _call(store, s4, 1, kind, "error", head)

    with store.pool.connection() as conn:
        fams = [f for f in hint_followthrough(conn, days=1) if f.kind == kind]
    assert len(fams) == 1  # the 'x1' / 'x22' heads normalise to one family
    f = fams[0]
    # s2's first error is followed by an error, s2's second by nothing.
    assert f.errors == 5
    assert (f.followed_ok, f.followed_error, f.no_followup) == (1, 1, 3)
    assert f.ok_share == 0.2


def test_aggregate_drops_below_min_errors_and_maps_null_kind() -> None:
    rows = [
        ("get", None, "BadInput", "missing kind=", "ok"),
        ("get", None, "BadInput", "missing kind=", None),
        ("put", "memory", "NotFound", "memory id=12 not found", "error"),
    ]
    fams = aggregate_followthrough(rows, min_errors=2)
    assert [(f.verb, f.kind, f.errors, f.followed_ok) for f in fams] == [
        ("get", "-", 2, 1)
    ]
