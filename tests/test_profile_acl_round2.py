"""Profile ACL round 2 (gr490266): store listings and link renderers drop
another owner's (and any soft-deleted) ``kind='profile'`` row."""

from __future__ import annotations

from typing import Any

import pytest

from precis import profiles
from precis.dispatch import Hub
from precis.handlers._links_render import render_links_section
from precis.handlers.memory import MemoryHandler
from precis.handlers.profile import ProfileHandler
from precis.store.revision_context import revision_context
from precis.users import hash_password

pytestmark = pytest.mark.db


def _world(store: Any) -> dict[str, Any]:
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.create_web_user(login="bob", abbrev="bo", password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])

    def mk(title: str, owner: str) -> Any:
        return store.insert_ref(
            kind="profile",
            slug=None,
            title=title,
            meta={"profile": {"name": title}},
            owner_login=owner,
        )

    return {
        "mine": mk("AlicePalette", "alice"),
        "bobs": mk("BobPalette", "bob"),
        "dead": mk("AliceDeadPalette", "alice"),
    }


def test_recent_refs_and_count_filter_by_viewer(store: Any) -> None:
    w = _world(store)
    store.retire_ref(w["dead"].id)
    for viewer, want in (("alice", {"AlicePalette"}), ("bob", {"BobPalette"})):
        rows = store.recent_refs(["profile"], viewer=viewer)
        assert {r.title for r in rows} == want
        assert store.count_recent_refs(["profile"], viewer=viewer) == len(want)
        # trash never lists a profile
        assert store.recent_refs(["profile"], viewer=viewer, deleted=True) == []
        assert store.count_recent_refs(["profile"], viewer=viewer, deleted=True) == 0


def test_recent_refs_unidentified_viewer_sees_no_deleted_profile(store: Any) -> None:
    w = _world(store)
    store.retire_ref(w["dead"].id)
    titles = {r.title for r in store.recent_refs(["profile"], viewer=None)}
    assert "AliceDeadPalette" not in titles


def _subject_with_links(store: Any, w: dict[str, Any]) -> int:
    store.retire_ref(w["dead"].id)
    subj = store.insert_ref(kind="memory", slug=None, title="subject note").id
    for key in ("mine", "bobs", "dead"):
        store.add_link(src_ref_id=subj, dst_ref_id=w[key].id, relation="related-to")
    return subj


def test_render_links_section_hides_foreign_and_deleted_profiles(store: Any) -> None:
    w = _world(store)
    subj = _subject_with_links(store, w)
    ref = store.fetch_refs_by_ids([subj])[subj]
    with revision_context(actor="user:al"):
        out = render_links_section(store, ref)
    assert f"pf{w['mine'].id}" in out and "AlicePalette" in out
    assert f"pf{w['bobs'].id}" not in out and "BobPalette" not in out
    assert f"pf{w['dead'].id}" not in out and "AliceDeadPalette" not in out
    # control: bob sees his own, not alice's
    with revision_context(actor="user:bo"):
        out = render_links_section(store, ref)
    assert "BobPalette" in out and "AlicePalette" not in out


def test_handler_view_links_hides_foreign_and_deleted_profiles(store: Any) -> None:
    w = _world(store)
    subj = _subject_with_links(store, w)
    handler = MemoryHandler(hub=Hub(store=store))
    with revision_context(actor="user:al"):
        body = handler.get(id=subj, view="links").body
    assert f"pf{w['mine'].id}" in body
    assert f"pf{w['bobs'].id}" not in body and "BobPalette" not in body
    assert f"pf{w['dead'].id}" not in body


def test_handler_view_links_only_foreign_profile_is_no_links(store: Any) -> None:
    w = _world(store)
    subj = store.insert_ref(kind="memory", slug=None, title="lonely").id
    store.add_link(src_ref_id=subj, dst_ref_id=w["bobs"].id, relation="related-to")
    handler = MemoryHandler(hub=Hub(store=store))
    with revision_context(actor="user:al"):
        body = handler.get(id=subj, view="links").body
    assert "(no links)" in body and f"pf{w['bobs'].id}" not in body


def test_inbound_link_from_foreign_profile_is_hidden(store: Any) -> None:
    w = _world(store)
    subj = store.insert_ref(kind="memory", slug=None, title="target note").id
    store.add_link(src_ref_id=w["bobs"].id, dst_ref_id=subj, relation="related-to")
    store.add_link(src_ref_id=w["mine"].id, dst_ref_id=subj, relation="related-to")
    ref = store.fetch_refs_by_ids([subj])[subj]
    with revision_context(actor="user:al"):
        out = render_links_section(store, ref)
    assert "AlicePalette" in out and "BobPalette" not in out


def test_viewing_as_scopes_link_rendering(store: Any) -> None:
    w = _world(store)
    subj = _subject_with_links(store, w)
    ref = store.fetch_refs_by_ids([subj])[subj]
    with profiles.viewing_as("alice"):
        out = render_links_section(store, ref)
    assert "AlicePalette" in out and "BobPalette" not in out


def test_hidden_ids_helper(store: Any) -> None:
    w = _world(store)
    store.retire_ref(w["dead"].id)
    refs = list(
        store.fetch_refs_by_ids([w[k].id for k in w], include_deleted=True).values()
    )
    assert profiles.hidden_ids(store, refs, "alice") == {w["bobs"].id, w["dead"].id}
