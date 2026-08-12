"""A dangling link to a soft-deleted ref renders a tombstone (404) with an
Undelete affordance, and the undelete POST restores it — the fix for a
pathway's deleted ``candidate_ref`` showing a bare "Request error (400)".
"""

from __future__ import annotations

from typing import Any

from .conftest import make_ref


def _seed_deleted(runtime: Any, **kw: Any) -> int:
    ref = make_ref(**kw)
    runtime.store.deleted_refs[ref.id] = ref
    return ref.id


def test_deleted_ref_renders_tombstone_404(runtime: Any, client: Any) -> None:
    ref_id = _seed_deleted(runtime, id=193554, kind="memory", title="a removed thought")
    resp = client.get(f"/refs/memory/{ref_id}")
    assert resp.status_code == 404
    body = resp.text
    assert "deleted" in body.lower()
    assert "a removed thought" in body
    # the undelete affordance points at the POST route
    assert f'action="/refs/memory/{ref_id}/undelete"' in body


def test_genuinely_absent_ref_is_not_a_tombstone(client: Any) -> None:
    # nothing seeded → the include-deleted fallback finds nothing → the
    # existing NotFound path (PrecisError → 400), NOT a tombstone.
    resp = client.get("/refs/memory/424242")
    assert resp.status_code == 400
    assert "undelete" not in resp.text.lower()


def test_undelete_restores_and_redirects(runtime: Any, client: Any) -> None:
    ref_id = _seed_deleted(runtime, id=193555, kind="memory", title="bring me back")
    resp = client.post(f"/refs/memory/{ref_id}/undelete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/refs/memory/{ref_id}"
    assert runtime.store.restored_ref_ids == [ref_id]
    # once restored it is live again — no longer a tombstone
    assert ref_id not in runtime.store.deleted_refs


def test_undelete_rejects_kind_mismatch(runtime: Any, client: Any) -> None:
    # ref is a memory; a request under the wrong kind segment must NOT restore
    # it (restore_ref keys on id alone — the route guards kind).
    ref_id = _seed_deleted(runtime, id=193556, kind="memory", title="stay hidden")
    resp = client.post(f"/refs/oracle/{ref_id}/undelete", follow_redirects=False)
    assert resp.status_code == 400  # NotFound → PrecisError → 400
    assert runtime.store.restored_ref_ids == []
    assert ref_id in runtime.store.deleted_refs  # still deleted
