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
