"""ProfileHandler tests — kind='profile' owner-scoped branding storage.

Slice 6 of draft-poster-genre-and-themes: put/edit validated through the
poster validators, owner-only read + owner-checked write keyed on the
``user:<abbrev>`` revision actor, and the export-resolution seam
(``meta.workspace.poster.profile`` → owner default → product).
"""

from __future__ import annotations

import pytest

from precis import profiles
from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.profile import ProfileHandler
from precis.store.revision_context import revision_context
from precis.users import hash_password
from tests.conftest import id_of

PALETTE = {
    "accent": "#005335",
    "heading": "#005335",
    "band": "#003726",
    "bodytint": "#EFF2EF",
    "citation": "#373A36",
    "measured": "#005335",
    "computed": "#007DBA",
    "bandtext": "#FFFFFF",
    "bodytext": "#000000",
}


@pytest.fixture
def users(store):
    store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    return store


@pytest.fixture
def handler(users) -> ProfileHandler:
    # Boot upserts the kinds row itself; a bare test hub must do it so the
    # refs.kind FK has its target.
    users.upsert_kinds([ProfileHandler.spec])
    return ProfileHandler(hub=Hub(store=users))


def _mk(handler, **kw) -> int:
    args = {"name": "lab", "owner": "reto", "palette": dict(PALETTE), **kw}
    return id_of(handler.put(**args).body)


# ── create / validate ──────────────────────────────────────────────


def test_put_creates_owned_ref(handler, users) -> None:
    pid = _mk(handler, sponsor_line="Funded by X")
    ref = users.get_ref(kind="profile", id=pid)
    assert ref.owner_login == "reto"
    assert ref.title == "lab"
    payload = profiles.payload_of(ref)
    assert payload["palette"]["accent"] == "#005335"
    assert payload["sponsor_line"] == "Funded by X"
    assert f"pf{pid}" in handler.get(id=pid).body


def test_put_validates_palette_and_returns_codes(handler) -> None:
    with pytest.raises(BadInput, match="poster_role_missing"):
        handler.put(name="bad", owner="reto", palette={"accent": "#005335"})
    with pytest.raises(BadInput, match="poster_role_unknown"):
        handler.put(
            name="bad2",
            owner="reto",
            palette={**PALETTE, "chartreuse": "#7FFF00"},
        )
    with pytest.raises(BadInput, match="poster_font_unknown"):
        handler.put(
            name="bad3",
            owner="reto",
            palette=dict(PALETTE),
            fonts={"body": "papyrus"},
        )
    with pytest.raises(BadInput, match="poster_logo_missing"):
        handler.put(
            name="bad4",
            owner="reto",
            palette=dict(PALETTE),
            logos=[{"figure": "no-such-figure"}],
        )


def test_put_requires_owner_without_identity(handler) -> None:
    with pytest.raises(BadInput, match="owner="):
        handler.put(name="anon", palette=dict(PALETTE))


def test_put_unknown_owner(handler) -> None:
    with pytest.raises(BadInput, match="not a web account"):
        handler.put(name="x", owner="nobody", palette=dict(PALETTE))


def test_put_owner_must_be_self_for_identified_caller(handler) -> None:
    with revision_context(actor="user:al"):
        with pytest.raises(BadInput, match="not your login"):
            handler.put(name="x", owner="reto", palette=dict(PALETTE))
        # owner omitted → defaults to the acting login
        resp = handler.put(name="alice-lab", palette=dict(PALETTE))
        pid = id_of(resp.body)
        assert handler.store.get_ref(kind="profile", id=pid).owner_login == "alice"


# ── read ACL ───────────────────────────────────────────────────────


def test_get_lists_own_only(handler) -> None:
    _mk(handler)
    with revision_context(actor="user:al"):
        assert "no profiles yet" in handler.get().body
    with revision_context(actor="user:rs"):
        body = handler.get().body
    assert "lab" in body


def test_get_id_owner_checked(handler) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.get(id=pid)
    with revision_context(actor="user:rs"):
        assert "lab" in handler.get(id=pid).body
    # unidentified (agent) passes
    assert "lab" in handler.get(id=pid).body


def test_get_by_name_and_handle(handler) -> None:
    pid = _mk(handler)
    assert f"profile {pid}" in handler.get(id="lab").body
    assert f"profile {pid}" in handler.get(id=f"pf{pid}").body


# ── edit / delete ──────────────────────────────────────────────────


def test_edit_merges_palette_replaces_fields(handler, users) -> None:
    pid = _mk(handler)
    handler.edit(id=pid, palette={"accent": "#111111"}, sponsor_line="s")
    ref = users.get_ref(kind="profile", id=pid)
    p = profiles.payload_of(ref)
    assert p["palette"]["accent"] == "#111111"
    assert p["palette"]["band"] == PALETTE["band"]  # untouched keys keep
    assert p["sponsor_line"] == "s"


def test_edit_revalidates(handler) -> None:
    pid = _mk(handler)
    with pytest.raises(BadInput, match="poster_role_invalid"):
        handler.edit(id=pid, palette={"accent": "green"})


def test_edit_owner_checked(handler) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        # a non-owner write gets the same NotFound as a read — the row's
        # existence doesn't leak through the write path either
        with pytest.raises(NotFound):
            handler.edit(id=pid, sponsor_line="nope")
        with pytest.raises(NotFound):
            handler.delete(id=pid)
    handler.delete(id=pid)
    with pytest.raises(Gone):
        handler.get(id=pid)


# ── resolution seam (slice b) ──────────────────────────────────────


def test_owner_default_flag_and_sole(handler) -> None:
    a = _mk(handler, name="a")
    b = _mk(handler, name="b")
    # two unflagged → no default
    assert profiles.owner_default(handler.store, "reto") is None
    handler.edit(id=b, default=True)
    dflt = profiles.owner_default(handler.store, "reto")
    assert dflt is not None and dflt.id == b
    handler.edit(id=a, default=True)
    # lowest flagged id wins
    dflt = profiles.owner_default(handler.store, "reto")
    assert dflt is not None and dflt.id == a
    # sole profile counts even unflagged
    handler.delete(id=b)
    handler.edit(id=a, default=False)
    dflt = profiles.owner_default(handler.store, "reto")
    assert dflt is not None and dflt.id == a


def test_draft_user_default_order(handler, users) -> None:
    # named selector in meta.workspace.poster wins over the owner default
    chosen = _mk(handler, name="chosen", palette={**PALETTE, "accent": "#222222"})
    _mk(handler, name="fb", default=True)

    class _Draft:
        def __init__(self, meta, owner_login="reto"):
            self.meta = meta
            self.owner_login = owner_login
            self.id = 0
            self.public_id = "dr0"

    d = _Draft({"workspace": {"poster": {"profile": "chosen"}}})
    out = profiles.draft_user_default(handler.store, d)
    assert out is not None and out["palette"]["accent"] == "#222222"

    d2 = _Draft({"workspace": {}})
    out2 = profiles.draft_user_default(handler.store, d2)
    assert out2 is not None and out2["name"] == "fb"

    d3 = _Draft({}, owner_login=None)
    assert profiles.draft_user_default(handler.store, d3) is None

    with pytest.raises(NotFound):
        profiles.draft_user_default(
            handler.store, _Draft({"workspace": {"poster": {"profile": "gone"}}})
        )


# ── cross-owner ACL on every path (gr490266) ───────────────────────


@pytest.mark.parametrize("form", ["id", "handle", "name"])
def test_resolve_profile_ref_owner_scoped(handler, form) -> None:
    pid = _mk(handler)
    ident = {"id": pid, "handle": f"pf{pid}", "name": "lab"}[form]
    assert profiles.resolve_profile_ref(handler.store, ident, owner="alice") is None
    ref = profiles.resolve_profile_ref(handler.store, ident, owner="reto")
    assert ref is not None and ref.id == pid


class _ACLDraft:
    def __init__(self, meta, owner_login=None):
        self.meta = meta
        self.owner_login = owner_login
        self.id = 987654321  # no draft-of owner row
        self.public_id = "dr0"


@pytest.mark.parametrize("form", ["id", "handle", "name"])
def test_draft_user_default_cross_owner_refused(handler, form) -> None:
    pid = _mk(handler)
    sel = {"id": pid, "handle": f"pf{pid}", "name": "lab"}[form]
    meta = {"workspace": {"poster": {"profile": sel}}}
    with pytest.raises(NotFound):
        profiles.draft_user_default(handler.store, _ACLDraft(meta, "alice"))
    out = profiles.draft_user_default(handler.store, _ACLDraft(meta, "reto"))
    assert out is not None and out["name"] == "lab"


def test_draft_user_default_unowned_draft_scopes_to_acting_user(handler) -> None:
    pid = _mk(handler)
    meta = {"workspace": {"poster": {"profile": pid}}}
    d = _ACLDraft(meta, None)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            profiles.draft_user_default(handler.store, d)
    with revision_context(actor="user:rs"):
        out = profiles.draft_user_default(handler.store, d)
    assert out is not None and out["name"] == "lab"


def test_tag_cross_owner_refused(handler, users) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.tag(id=pid, add=["topic:x"])
    assert not any("topic:x" in str(t) for t in users.tags_for(pid))
    with revision_context(actor="user:rs"):
        handler.tag(id=pid, add=["topic:x"])
    assert any("topic:x" in str(t) for t in users.tags_for(pid))


def test_tag_list_form_cross_owner_refused(handler, users) -> None:
    # profile takes no id=[...] batch; the owner gets BadInput, a
    # non-owner must still get the ACL NotFound (no existence leak).
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.tag(id=[pid], add=["topic:x"])
    assert not any("topic:x" in str(t) for t in users.tags_for(pid))
    with revision_context(actor="user:rs"):
        with pytest.raises(BadInput, match="does not take id"):
            handler.tag(id=[pid], add=["topic:x"])


def test_link_cross_owner_refused(handler) -> None:
    pid = _mk(handler)
    other = _mk(handler, name="other")
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.link(id=pid, target=f"profile:{other}")
    with revision_context(actor="user:rs"):
        assert handler.link(id=pid, target=f"profile:{other}").body


def test_link_list_form_cross_owner_refused(handler) -> None:
    pid = _mk(handler)
    other = _mk(handler, name="other")
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.link(id=[pid], target=f"profile:{other}")
    with revision_context(actor="user:rs"):
        with pytest.raises(BadInput, match="does not take id"):
            handler.link(id=[pid], target=f"profile:{other}")


def test_eye_cross_owner_refused(handler) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.eye(id=pid)
    with revision_context(actor="user:rs"):
        assert handler.eye(id=pid).body


def test_find_in_ref_cross_owner_refused(handler) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            handler.find_in_ref(id=pid, q="lab")
    with revision_context(actor="user:rs"):
        try:
            handler.find_in_ref(id=pid, q="lab")
        except NotFound:
            pytest.fail("owner must not get NotFound")
        except Exception:
            pass


def test_search_hides_other_owners_profiles(handler) -> None:
    pid = _mk(handler)
    with revision_context(actor="user:al"):
        assert f"pf{pid}" not in handler.search(q="lab").body
        assert "0" in handler.search(view="count").body
    with revision_context(actor="user:rs"):
        assert f"pf{pid}" in handler.search(q="lab").body
        assert "1" in handler.search(view="count").body


def test_search_tags_only_hides_other_owners_profiles(handler) -> None:
    # tags-only (no q) goes through _list_by_tags, a separate listing path
    pid = _mk(handler)
    handler.tag(id=pid, add=["topic:x"])
    with revision_context(actor="user:al"):
        assert f"pf{pid}" not in handler.search(tags=["topic:x"]).body
    with revision_context(actor="user:rs"):
        assert f"pf{pid}" in handler.search(tags=["topic:x"]).body


def _sweep_runtime(users):
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.runtime import PrecisRuntime

    return PrecisRuntime(config=PrecisConfig(), hub=boot(store=users))


def test_cross_kind_tags_only_sweep_never_admits_profile(users, handler) -> None:
    # profile is not search-hits-capable, so no kind= spelling reaches it:
    # explicit list is refused, '*' omits it (the sweep filter below is
    # defence in depth should that ever change).
    pid = _mk(handler)
    handler.tag(id=pid, add=["topic:x"])
    rt = _sweep_runtime(users)
    with revision_context(actor="user:rs"):
        out = rt.dispatch("search", {"kind": "profile,memory", "tags": ["topic:x"]})
        assert "do not support cross-kind search: ['profile']" in out
        assert f"pf{pid}" not in rt.dispatch(
            "search", {"kind": "*", "tags": ["topic:x"]}
        )


def test_cross_kind_tags_only_sweep_filters_other_owners_profiles(
    users, handler, monkeypatch
) -> None:
    # Force profile into the eligible set to exercise the sweep's owner
    # filter (_dispatch_cross_kind_tags_only).
    pid = _mk(handler)
    handler.tag(id=pid, add=["topic:x"])
    rt = _sweep_runtime(users)
    real = rt._cross_kind_kinds
    monkeypatch.setattr(rt, "_cross_kind_kinds", lambda: [*real(), "profile"])
    args = {"kind": "profile,memory", "tags": ["topic:x"]}
    # the sweep renders rows as ``profile:<id>`` (not the pf handle)
    with revision_context(actor="user:al"):
        assert f"profile:{pid}" not in rt.dispatch("search", dict(args))
    with revision_context(actor="user:rs"):
        assert f"profile:{pid}" in rt.dispatch("search", dict(args))


def test_search_link_only_refused_for_identified_caller(handler, users) -> None:
    # q-less link= pages the link table unscoped, so a signed-in user is
    # refused (fail closed); the unidentified caller keeps the listing.
    pid = _mk(handler)
    other = users.insert_ref(kind="profile", slug=None, title="anchor", meta={})
    handler.link(id=pid, target=f"profile:{other.id}")
    for actor in ("user:al", "user:rs"):
        with revision_context(actor=actor):
            with pytest.raises(BadInput, match="not available to a"):
                handler.search(link=f"profile:{other.id}")
    assert "BadInput" not in handler.search(link=f"profile:{other.id}").body


# ── gr490266 review fixes: link target, Gone oracle, search totals ──


def test_link_target_to_foreign_profile_is_notfound(handler, users) -> None:
    from precis.utils.link_target import parse_link_target

    pid = _mk(handler)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound):
            parse_link_target(f"pf{pid}", store=users)
    with revision_context(actor="user:rs"):
        assert parse_link_target(f"pf{pid}", store=users).ref_id == pid
    # unidentified (agent) caller resolves
    assert parse_link_target(f"pf{pid}", store=users).ref_id == pid


def test_retired_foreign_profile_is_notfound_not_gone(handler, users) -> None:
    pid = _mk(handler)
    users.retire_ref(pid)
    with revision_context(actor="user:al"):
        with pytest.raises(NotFound) as exc:
            handler.get(id=pid)
    assert type(exc.value) is NotFound
    assert not isinstance(exc.value, Gone)
    with revision_context(actor="user:rs"):
        with pytest.raises(Gone):
            handler.get(id=pid)


def test_ranked_search_is_owner_scoped(handler) -> None:
    a = _mk(handler, name="lab")
    b = _mk(handler, name="lab2")
    with revision_context(actor="user:al"):
        body = handler.search(q="lab").body
    assert f"pf{a}" not in body and f"pf{b}" not in body
    assert "2" not in body
    with revision_context(actor="user:rs"):
        body = handler.search(q="lab").body
        only = handler.search(q="lab2").body
    assert f"pf{a}" in body and f"pf{b}" in body
    assert f"pf{b}" in only and f"pf{a}" not in only
