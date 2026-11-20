"""Owner isolation of the private gtd kind: every refused reader has a test."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest
from psycopg import sql

from precis.errors import Denied, NotFound
from precis.gtd import GtdStore
from precis.oauth.store import OAuthIdentity
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.tools.core import _personal_call_allowed, _require_personal_scope
from precis.user_context import user_scope
from precis.users import PasswordRecord

pytestmark = pytest.mark.db

_SHARED = ("refs", "chunks", "ref_tags", "tags", "chunk_tags", "links")


@pytest.fixture
def owners(store: Store) -> tuple[int, int]:
    password = PasswordRecord("unused", "unused", "scrypt-v1")
    a = store.create_web_user(login="gtd-a", abbrev="ga", password=password)
    b = store.create_web_user(login="gtd-b", abbrev="gb", password=password)
    return a.id, b.id


def _shared_counts(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    with store.pool.connection() as conn:
        for table in _SHARED:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            out[table] = row[0]
    return out


def _oauth(scopes: tuple[str, ...]) -> OAuthIdentity:
    return OAuthIdentity(
        user_id=7,
        grant_id=3,
        access_token_id=9,
        client_id="test-client",
        scopes=scopes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_no_principal_is_denied_before_any_lookup(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me663455: a call with no principal is Denied before any lookup, so the
    store is never queried and nothing about existence leaks."""
    queried = Mock(side_effect=AssertionError("store queried without a principal"))
    monkeypatch.setattr(GtdStore, "capture", queried)
    monkeypatch.setattr(GtdStore, "get_item", queried)
    with user_scope(None):
        put, put_err = runtime_with_store.dispatch_with_status(
            "put", {"kind": "gtd", "text": "secret"}
        )
        got, get_err = runtime_with_store.dispatch_with_status(
            "get", {"kind": "gtd", "id": "gt1"}
        )
    assert put_err and "[error:Denied]" in put
    assert get_err and "[error:Denied]" in got
    queried.assert_not_called()


def test_foreign_and_missing_handles_give_the_same_not_found(
    store: Store, owners: tuple[int, int]
) -> None:
    """me663455: another user's handle returns NotFound, indistinguishable
    from a handle that does not exist."""
    alice, bob = owners
    gtd = GtdStore(store)
    with user_scope(alice):
        mine = gtd.capture("alice only")
    with user_scope(bob):
        with pytest.raises(NotFound) as foreign:
            gtd.get_item(mine.id)
        with pytest.raises(NotFound) as missing:
            gtd.get_item(mine.id + 10_000)
    assert str(foreign.value) == str(missing.value)


def _rows_as(store: Store, setting: str | None) -> list[Any]:
    """Read gtd_items through a non-bypass role with the owner setting given."""
    role = "gtd_probe_" + uuid.uuid4().hex[:12]
    with store.pool.connection() as conn:
        try:
            try:
                conn.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS").format(
                        sql.Identifier(role)
                    )
                )
                conn.execute(
                    sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role))
                )
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                pytest.skip("test DB role cannot create a non-BYPASSRLS probe role")
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON gtd_items TO {}").format(sql.Identifier(role))
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            if setting is not None:
                conn.execute(
                    "SELECT set_config('precis.gtd_user_id', %s, true)", (setting,)
                )
            return conn.execute("SELECT item_id FROM gtd_items").fetchall()
        finally:
            conn.rollback()


def test_raw_sql_as_another_user_or_unset_returns_no_rows(
    store: Store, owners: tuple[int, int]
) -> None:
    """me663455: forced row-level security hides a user's rows from a raw read
    under another user's setting and under no setting; the owner still sees
    them."""
    alice, bob = owners
    with user_scope(alice):
        GtdStore(store).capture("alice only")
    assert _rows_as(store, str(alice))
    assert _rows_as(store, str(bob)) == []
    assert _rows_as(store, None) == []


def test_personal_token_may_call_gtd_and_oauth_needs_the_scope() -> None:
    """me663455: the owner's personal token may call gtd; an OAuth grant
    of the same owner without mcp:full reaches it only with the gtd scope."""
    with user_scope(7, 11):
        assert _personal_call_allowed("get", {"kind": "gtd", "id": "gt1"})
        assert _personal_call_allowed("put", {"kind": "gtd", "text": "x"})
    with user_scope(7, oauth=_oauth(("learning",))):
        assert not _personal_call_allowed("get", {"kind": "gtd", "id": "gt1"})
    with user_scope(7, oauth=_oauth(("gtd",))):
        assert _personal_call_allowed("get", {"kind": "gtd", "id": "gt1"})
        assert not _personal_call_allowed("get", {"kind": "learning"})


def test_unshipped_gtd_help_is_not_allowlisted_and_refusal_names_capture() -> None:
    """me663455: the personal allowlist does not advertise the unshipped
    precis-gtd-help, and the refusal text sends callers to the registry's
    capture hint rather than a handle-less get."""
    help_call = {"kind": "skill", "id": "precis-gtd-help"}
    with user_scope(7, 11):
        assert not _personal_call_allowed("get", help_call)
        with pytest.raises(Denied) as err:
            _require_personal_scope("search", {"kind": "todo", "q": "x"})
    text = f"{err.value} {getattr(err.value, 'next', '')}"
    assert "precis-gtd-help" not in text
    assert "put(kind='gtd'" in text
    with user_scope(7, oauth=_oauth(("gtd",))):
        assert not _personal_call_allowed("get", help_call)


def test_captured_item_is_absent_from_todo_views_and_cross_kind_search(
    runtime_with_store: PrecisRuntime, store: Store, owners: tuple[int, int]
) -> None:
    """me663435: no shared refs for gtd, so a captured item never appears in
    search(kind='todo') default, active or doable views, nor in the
    cross-kind fan-out."""
    word = "zorblax-unique-capture"
    with user_scope(owners[0]):
        GtdStore(store).capture(f"{word} on the list")
        for view in (None, "active", "doable"):
            payload: dict[str, Any] = {"kind": "todo", "q": word}
            if view:
                payload["view"] = view
            assert "on the list" not in runtime_with_store.dispatch("search", payload)
        assert "on the list" not in runtime_with_store.dispatch("search", {"q": word})


def test_capture_adds_no_shared_ref_chunk_tag_or_link_rows(
    store: Store, owners: tuple[int, int]
) -> None:
    """me663435: no shared chunks, tag rows or link rows for gtd, which is
    what dispatch, the work board, the gardener and digest jobs read."""
    before = _shared_counts(store)
    with user_scope(owners[0]):
        GtdStore(store).capture("nothing shared")
    assert _shared_counts(store) == before


@pytest.mark.parametrize(
    ("verb", "payload"),
    [
        ("search", {"kind": "gtd", "q": "x"}),
        ("edit", {"kind": "gtd", "id": "gt1", "text": "x"}),
        ("delete", {"kind": "gtd", "id": "gt1"}),
        ("tag", {"kind": "gtd", "id": "gt1", "add": ["x"]}),
        ("link", {"kind": "gtd", "id": "gt1", "target": "todo:td1"}),
    ],
)
def test_unsupported_verbs_are_refused_without_touching_rows(
    runtime_with_store: PrecisRuntime,
    store: Store,
    owners: tuple[int, int],
    verb: str,
    payload: dict[str, Any],
) -> None:
    """me663435: there is no tag, link or merge, and search, edit and delete
    are later units; each is refused as unsupported and private rows stay put."""
    with user_scope(owners[0]):
        item = GtdStore(store).capture("keep me")
        body, error = runtime_with_store.dispatch_with_status(verb, payload)
        assert error and "[error:Unsupported]" in body
        assert GtdStore(store).get_item(item.id).text == "keep me"


def test_maximal_capture_round_trips_through_dispatch(
    runtime_with_store: PrecisRuntime, owners: tuple[int, int]
) -> None:
    """me663455, me663441: the owner can capture 4000 bytes through dispatch
    and use the returned gt handle to read the full item."""
    words = "w" * 4000
    with user_scope(owners[0]):
        put, put_err = runtime_with_store.dispatch_with_status(
            "put", {"kind": "gtd", "text": words}
        )
        assert not put_err and f"Captured: {words}." in put
        handle = re.search(r"\bgt\d+\b", put)
        assert handle is not None
        got, get_err = runtime_with_store.dispatch_with_status(
            "get", {"kind": "gtd", "id": handle.group(0)}
        )
    assert not get_err
    assert words in got
    assert "captured: (same as text)" in got
