"""Finding retirement uses the audited memory retirement contract (gr676807)."""

from __future__ import annotations

import pytest
from psycopg import Connection

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.runtime import PrecisRuntime
from tests.test_memory_retiredoor import _memory, _snapshot


def test_finding_retirement_preview_and_redirect(
    runtime_with_store: PrecisRuntime,
) -> None:
    runtime = runtime_with_store
    store = runtime.live_store
    old = store.insert_ref(kind="finding", slug=None, title="Duplicate").id
    successor = store.insert_ref(kind="finding", slug=None, title="Survivor").id
    source = _memory(store, f"Use fi{old} for builds.")
    store.add_link(src_ref_id=source, dst_ref_id=old, relation="related-to")
    before = _snapshot(store, old)
    payload = {
        "kind": "finding",
        "id": f"fi{old}",
        "superseded_by": f"fi{successor}",
        "reason": "Duplicate claim",
    }
    preview, failed = runtime.dispatch_with_status(
        "delete", {**payload, "dry_run": True}
    )
    assert not failed, preview
    assert "would retire finding" in preview
    assert f"me{source}" in preview
    assert _snapshot(store, old) == before
    receipt, failed = runtime.dispatch_with_status("delete", payload)
    assert not failed, receipt
    assert "1 link(s) migrated" in receipt
    tombstone = store.get_ref(kind="finding", id=old, include_deleted=True)
    assert tombstone is not None and tombstone.retired_at is not None
    assert tombstone.meta["superseded_by"] == f"fi{successor}"
    resolved = store.resolve_handle(f"fi{old}")
    assert resolved is not None and resolved.ref_id == successor
    with store.pool.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s AND relation = %s",
            (source, successor, "related-to"),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM ref_events WHERE ref_id = %s AND event = %s",
            (old, "deleted"),
        ).fetchone()


def test_finding_mentions_require_disposition(hub: Hub) -> None:
    store = hub.live_store
    old = store.insert_ref(kind="finding", slug=None, title="Old").id
    source = _memory(store, f"Use fi{old} today.")
    handler = FindingHandler(hub=hub)
    before = _snapshot(store, old)
    preview = handler.delete(id=old, dry_run=True).body
    assert "would refuse" in preview and f"me{source}" in preview
    with pytest.raises(BadInput, match="live mention"):
        handler.delete(id=old)
    assert _snapshot(store, old) == before
    assert "deleted finding" in handler.delete(id=old, force=True).body


@pytest.mark.parametrize("successor_form", ["self", "missing", "chunk", "retired"])
def test_finding_rejects_invalid_successors(hub: Hub, successor_form: str) -> None:
    store = hub.live_store
    old = store.insert_ref(kind="finding", slug=None, title="Old").id
    successor = store.insert_ref(kind="finding", slug=None, title="New").id
    if successor_form == "retired":
        store.retire_ref(successor)
    target = {
        "self": f"fi{old}",
        "missing": "fi999999999999",
        "chunk": f"finding:fi{successor}~0",
        "retired": f"fi{successor}",
    }[successor_form]
    before = _snapshot(store, old)
    with pytest.raises(BadInput):
        FindingHandler(hub=hub).delete(id=old, superseded_by=target)
    assert _snapshot(store, old) == before


def test_finding_retirement_rolls_back(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.live_store
    old = store.insert_ref(kind="finding", slug=None, title="Old").id
    successor = store.insert_ref(kind="finding", slug=None, title="New").id
    store.add_link(src_ref_id=successor, dst_ref_id=old, relation="supersedes")
    before = _snapshot(store, old)
    retire = store.retire_ref

    def fail(ref_id: int, *, conn: Connection | None = None) -> None:
        assert conn is not None
        retire(ref_id, conn=conn)
        raise RuntimeError("rollback fixture")

    monkeypatch.setattr(store, "retire_ref", fail)
    with pytest.raises(RuntimeError, match="rollback fixture"):
        FindingHandler(hub=hub).delete(id=old)
    assert _snapshot(store, old) == before
