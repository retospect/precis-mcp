"""``search(kind=K, view='meta')`` / ``view='tags')`` — the per-kind
introspection views (mcp-introspection gap closures 2026-10-14,
gr484241): the meta-key inventory and tag axis/value counts that
agents used to read from raw ``refs``/``ref_tags`` SQL.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db


def _gripe(store: Store, *open_tags: str) -> int:
    """The SECURITY DEFINER gripe path (ref + body + STATUS:open in one
    transaction) — insert_ref + separate tag writes trip the
    gripe_status_check trigger."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", ("t",)).fetchone()
        assert row is not None
        conn.commit()
        ref_id = int(row[0])
    for t in open_tags:
        store.add_tag(ref_id, Tag.open(t), set_by="agent")
    return ref_id


def _mk(store: Store, kind: str, meta: dict, tags: tuple[str, ...] = ()) -> int:
    ref = store.insert_ref(kind=kind, slug=None, title="t", meta=meta)
    for t in tags:
        if ":" in t and t.split(":")[0].isupper():
            store.add_tag(
                ref.id, Tag.closed(t.split(":")[0], t.split(":", 1)[1]), set_by="system"
            )
        else:
            store.add_tag(ref.id, Tag.open(t), set_by="agent")
    return ref.id


class TestMetaView:
    def test_key_inventory(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _mk(store, "job", {"job_type": "sweep", "params": {"model": "m1"}})
        _mk(store, "job", {"job_type": "fix", "params": {"model": "m2"}, "n": 3})
        body = runtime_with_store.dispatch("search", {"kind": "job", "view": "meta"})
        assert "# meta keys in use — job (2 live refs)" in body
        assert "job_type  string  2" in body
        assert "params  object  2" in body
        assert "n  number  1" in body

    def test_key_drill(self, runtime_with_store: PrecisRuntime, store: Store) -> None:
        _mk(store, "job", {"params": {"model": "m1", "depth": 2}})
        _mk(store, "job", {"params": {"model": "m2"}})
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "job", "view": "meta", "__extras__": {"key": "params"}},
        )
        assert "meta['params'] keys" in body
        assert "model  string  2" in body
        assert "depth  number  1" in body

    def test_drill_non_object(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _mk(store, "job", {"job_type": "sweep"})
        body = runtime_with_store.dispatch(
            "search",
            {"kind": "job", "view": "meta", "__extras__": {"key": "job_type"}},
        )
        assert "is not an object" in body

    def test_cached_within_ttl(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        _mk(store, "job", {"job_type": "sweep"})
        args = {"kind": "job", "view": "meta"}
        first = runtime_with_store.dispatch("search", args)
        _mk(store, "job", {"job_type": "fix", "newkey": 1})
        second = runtime_with_store.dispatch("search", args)
        # Same body — the inventory is cached, so the new key doesn't
        # appear until TTL expiry (or cache clear).
        assert second == first
        assert "newkey" not in second
        runtime_with_store._meta_view_cache.clear()
        third = runtime_with_store.dispatch("search", args)
        assert "newkey" in third


class TestTagsView:
    def test_axis_counts(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        hub: Hub,
    ) -> None:
        _gripe(store, "area:x")
        _gripe(store, "area:x")
        g3 = _gripe(store, "area:y")
        GripeHandler(hub=hub).tag(id=g3, add=["STATUS:done"])
        body = runtime_with_store.dispatch("search", {"kind": "gripe", "view": "tags"})
        assert "# tag counts — gripe (3 live refs)" in body
        assert "STATUS:open  2" in body
        assert "STATUS:done  1" in body
        assert "area:x  2" in body
        assert "area:y  1" in body
        assert "## STATUS" in body and "## OPEN" in body

    def test_status_filter(
        self,
        runtime_with_store: PrecisRuntime,
        store: Store,
        hub: Hub,
    ) -> None:
        _gripe(store, "area:x")
        g2 = _gripe(store, "area:y")
        GripeHandler(hub=hub).tag(id=g2, add=["STATUS:done"])
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe", "view": "tags", "status": "open"}
        )
        assert "(1 live ref)" in body
        assert "area:x  1" in body
        assert "area:y" not in body

    def test_expired_tags_excluded(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        ref_id = _mk(store, "job", {}, tags=("area:expired",))
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE ref_tags SET expires_at = now() - interval '1 hour' "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            conn.commit()
        body = runtime_with_store.dispatch("search", {"kind": "job", "view": "tags"})
        assert "no live job refs carry tags" in body
        assert "area:expired" not in body


class TestProfileIntrospectACL:
    """gr490266: kind='profile' meta/tags inventories span every owner."""

    @pytest.mark.parametrize("view", ["meta", "tags"])
    def test_identified_user_refused_unidentified_ok(
        self, runtime_with_store: PrecisRuntime, store: Store, view: str
    ) -> None:
        from precis.store.revision_context import revision_context
        from precis.users import hash_password

        store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
        _mk(store, "profile", {"k": 1}, tags=("area:x",))
        args = {"kind": "profile", "view": view}
        with revision_context(actor="user:al"):
            body = runtime_with_store.dispatch("search", args)
        assert "not available on kind='profile'" in body
        body = runtime_with_store.dispatch("search", args)
        assert "not available on kind='profile'" not in body


class TestIntrospectErrors:
    def test_requires_single_kind(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "gripe,todo", "view": "meta"}
        )
        assert "one kind" in body

    def test_bad_combo_rejected(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "job", "view": "meta", "since": "2024-01-01"}
        )
        assert "not supported with view='meta'" in body

    def test_unknown_kind(self, runtime_with_store: PrecisRuntime) -> None:
        body = runtime_with_store.dispatch(
            "search", {"kind": "nonsense", "view": "tags"}
        )
        assert "unknown kind" in body
