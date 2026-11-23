"""DB-backed tests for the Drive Return/Showcase store (migration 0220).

``docs/backlog/drive-filter-hierarchy.md`` slice 1: round-trips for the
three tables, per-user privacy of Showcase and history, latest-open
replacement, and the retire/delete lifecycle. Auto-skipped when no
postgres is reachable (the ``store`` fixture handles the skip).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from precis.acting_user import actor_for
from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.embedder import MockEmbedder
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store._drive_state_ops import DriveStateRow
from precis.store.revision_context import revision_context
from precis.user_context import user_scope
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig


def _user(store: Store, login: str) -> int:
    return store.create_web_user(
        login=login, abbrev=login[:3], password=hash_password("pw")
    ).id


def _ref(store: Store, title: str, kind: str = "memory") -> int:
    return store.insert_ref(kind=kind, slug=None, title=title, meta={}).id


def _ids(rows: list[DriveStateRow]) -> list[int]:
    return [r.ref_id for r in rows]


def test_record_open_round_trips_latest_first(store: Store) -> None:
    a = _user(store, "alice")
    d, f = _ref(store, "draft D"), _ref(store, "finding F")
    assert store.record_open(a, d)
    assert store.record_open(a, f)
    assert _ids(store.recent_opens(a)) == [f, d]


def test_reopen_replaces_previous_entry(store: Store) -> None:
    a = _user(store, "alice")
    d, f = _ref(store, "D"), _ref(store, "F")
    store.record_open(a, d)
    store.record_open(a, f)
    first = store.recent_opens(a)[1].at
    assert store.record_open(a, d)
    rows = store.recent_opens(a)
    assert _ids(rows) == [d, f]  # one row per ref, moved to the front
    assert rows[0].at >= first
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM web_ref_views WHERE user_id = %s", (a,)
        ).fetchone()
    assert n is not None and n[0] == 2


def test_record_open_never_moves_backwards(store: Store) -> None:
    a = _user(store, "alice")
    d = _ref(store, "D")
    store.record_open(a, d)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE web_ref_views SET last_opened_at = now() + interval '1 day' "
            "WHERE user_id = %s",
            (a,),
        )
        conn.commit()
    future = store.recent_opens(a)[0].at
    assert store.record_open(a, d)
    assert store.recent_opens(a)[0].at == future


def test_history_is_per_user(store: Store) -> None:
    a, b = _user(store, "alice"), _user(store, "bob")
    d, f = _ref(store, "D"), _ref(store, "F")
    store.record_open(a, d)
    store.record_open(a, f)
    store.record_open(b, d)  # B's later open of D does not reorder A
    assert _ids(store.recent_opens(a)) == [f, d]
    assert _ids(store.recent_opens(b)) == [d]


def test_forget_and_clear_leave_showcase(store: Store) -> None:
    a, b = _user(store, "alice"), _user(store, "bob")
    d, f = _ref(store, "D"), _ref(store, "F")
    for r in (d, f):
        store.record_open(a, r)
    store.record_open(b, d)
    store.add_member(a, d)
    assert store.forget_open(a, d)
    assert not store.forget_open(a, d)
    assert _ids(store.recent_opens(a)) == [f]
    assert store.clear_history(a) == 1
    assert store.recent_opens(a) == []
    assert _ids(store.recent_opens(b)) == [d]
    assert _ids(store.showcase_members(a)) == [d]


def test_showcase_round_trip_keeps_first_added_at(store: Store) -> None:
    a = _user(store, "alice")
    d, f = _ref(store, "D"), _ref(store, "F")
    assert store.showcase_members(a) == []
    with store.pool.connection() as conn:
        none = conn.execute(
            "SELECT 1 FROM web_collections WHERE owner_user_id = %s", (a,)
        ).fetchone()
    assert none is None  # a read never creates the collection
    assert store.add_member(a, d)
    assert store.add_member(a, f)
    added = {r.ref_id: r.at for r in store.showcase_members(a)}
    assert not store.add_member(a, d)  # repeat add: no-op
    rows = store.showcase_members(a)
    assert _ids(rows) == [f, d]
    assert {r.ref_id: r.at for r in rows} == added
    assert store.ensure_showcase(a) == store.ensure_showcase(a)
    assert store.remove_member(a, d)
    assert not store.remove_member(a, d)
    assert _ids(store.showcase_members(a)) == [f]


def test_showcase_is_private_per_user(store: Store) -> None:
    a, b = _user(store, "alice"), _user(store, "bob")
    d = _ref(store, "D")
    store.add_member(a, d)
    assert store.showcase_members(b) == []
    assert not store.remove_member(b, d)  # B cannot touch A's membership
    assert _ids(store.showcase_members(a)) == [d]
    assert store.ensure_showcase(a) != store.ensure_showcase(b)
    store.add_member(b, d)
    assert store.remove_member(b, d)
    assert _ids(store.showcase_members(a)) == [d]


def test_retired_ref_hidden_then_restored(store: Store) -> None:
    a = _user(store, "alice")
    d = _ref(store, "D")
    store.record_open(a, d)
    store.add_member(a, d)
    store.retire_ref(d)
    assert store.recent_opens(a) == []
    assert store.showcase_members(a) == []
    assert not store.record_open(a, d)
    assert not store.add_member(a, d)
    store.restore_ref(d)
    assert _ids(store.recent_opens(a)) == [d]
    assert _ids(store.showcase_members(a)) == [d]


def test_missing_ref_writes_nothing(store: Store) -> None:
    a = _user(store, "alice")
    assert not store.record_open(a, 999_999_999)
    assert not store.add_member(a, 999_999_999)
    assert store.recent_opens(a) == []
    with store.pool.connection() as conn:
        none = conn.execute(
            "SELECT 1 FROM web_collections WHERE owner_user_id = %s", (a,)
        ).fetchone()
    assert none is None  # a refused add creates no collection


def test_hard_delete_cascades(store: Store) -> None:
    a = _user(store, "alice")
    d = _ref(store, "D")
    store.record_open(a, d)
    store.add_member(a, d)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM web_users WHERE id = %s", (a,))
        conn.commit()
        left = conn.execute(
            "SELECT (SELECT count(*) FROM web_ref_views)"
            "     + (SELECT count(*) FROM web_collections)"
            "     + (SELECT count(*) FROM web_collection_members)"
        ).fetchone()
    assert left is not None and left[0] == 0


def test_same_account_tracks_independent_opens(store: Store) -> None:
    alice, bob = _user(store, "alice"), _user(store, "bob")
    shared, human_only = _ref(store, "Shared"), _ref(store, "Human only")

    assert store.record_open(alice, shared)
    assert store.record_open(alice, shared, attribution="human")
    assert store.record_open(alice, human_only, attribution="human")
    with user_scope(alice):
        store.record_actor_open(shared)

    assert _ids(store.recent_opens(alice, attribution="human")) == [human_only, shared]
    assert _ids(store.recent_opens(alice, attribution="agent")) == [shared]
    assert _ids(store.recent_opens(alice, attribution="unknown")) == [shared]
    assert _ids(store.recent_opens(alice)) == [shared, human_only]
    assert store.recent_opens(bob, attribution="agent") == []

    assert store.forget_open(alice, shared, attribution="agent")
    assert store.recent_opens(alice, attribution="agent") == []
    assert _ids(store.recent_opens(alice, attribution="human")) == [human_only, shared]


def test_ownership_attribution_and_legacy_owner(store: Store) -> None:
    alice, bob = _user(store, "alice"), _user(store, "bob")
    with revision_context(actor=actor_for("ali"), trust_actor_type="human"):
        human = _ref(store, "Human creation")
    with user_scope(alice), revision_context(actor=actor_for("ali")):
        agent = _ref(store, "Agent creation")
    legacy = store.insert_ref(
        kind="memory", slug=None, title="Legacy", owner_login="alice"
    ).id
    # Account creation can mint an explicitly owned key memory as well.
    assert legacy in _ids(store.drive_owned_refs(alice, attribution="unknown"))
    assert _ids(store.drive_owned_refs(alice, attribution="human")) == [human]
    assert _ids(store.drive_owned_refs(alice, attribution="agent")) == [agent]
    assert store.drive_owned_refs(bob, attribution="agent") == []
    store.retire_ref(agent)
    assert store.drive_owned_refs(alice, attribution="agent") == []


def test_personal_drive_lists_browser_and_agent_activity(store: Store) -> None:
    alice = _user(store, "alice")
    _user(store, "bob")
    hub = boot(
        store=store, embedder=MockEmbedder(dim=store.embedding_dim()), boot_writes=False
    )
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    with user_scope(alice), revision_context(actor=actor_for("ali")):
        result, is_error = runtime.dispatch_with_status(
            "put", {"kind": "memory", "text": "A uniquely named agent memory"}
        )
        assert not is_error, result
        # A memory put may also mint companion refs; keep the memory itself.
        [created] = [
            row
            for row in store.drive_owned_refs(alice, attribution="agent")
            if store.get_ref(kind="memory", id=row.ref_id) is not None
        ]
        result, is_error = runtime.dispatch_with_status(
            "get", {"kind": "memory", "id": created.ref_id}
        )
        assert not is_error, result

    app = create_app(
        runtime=runtime, web_config=WebConfig(corpus_dir=None, auth_required=True)
    )
    with TestClient(app) as client:
        browser_ref = _ref(store, "A uniquely named browser memory")
        opened = client.get(f"/refs/memory/{browser_ref}", auth=("alice", "pw"))
        assert opened.status_code == 200
        human = client.get("/drive/personal", auth=("alice", "pw"))
        assert human.status_code == 200
        assert human.headers["cache-control"] == "private, no-store"
        assert "A uniquely named browser memory" in human.text
        assert "A uniquely named agent memory" not in human.text

        agent_url = "/drive/personal?attribution=agent"
        agent = client.get(agent_url, auth=("alice", "pw"))
        assert agent.status_code == 200
        assert agent.text.count(f'href="/refs/memory/{created.ref_id}"') == 2
        assert "A uniquely named browser memory" not in agent.text
        other_account = client.get(agent_url, auth=("bob", "pw"))
        assert other_account.status_code == 200
        assert "A uniquely named agent memory" not in other_account.text
