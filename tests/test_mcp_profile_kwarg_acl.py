"""Profile kwargs over the ``tools/core.py`` MCP door, with the owner ACL.

``put``/``edit`` forward name/palette/fonts/logos/sponsor_line/owner; the
handler still derives the owner from the acting identity and refuses a
foreign ``owner=`` or a foreign profile edit (gr490266).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis import profiles
from precis.handlers.profile import ProfileHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.tools import core as tools_core
from precis.users import hash_password

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
OTHER = {**PALETTE, "accent": "#112233"}


@pytest.fixture
def mounted(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Store]:
    store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])
    monkeypatch.setattr(tools_core, "_runtime", runtime_with_store)
    yield store


def _body(out: Any) -> str:
    if isinstance(out, CallToolResult):
        return str(getattr(out.content[0], "text", ""))
    return str(out)


def _ref(store: Store, pid: int) -> Any:
    ref = store.get_ref(kind="profile", id=pid)
    assert ref is not None
    return ref


def _id(out: Any) -> int:
    m = re.search(r"\b(?:pf|id=)(\d+)", _body(out))
    assert m, _body(out)
    return int(m.group(1))


def _reto_profile(store: Store) -> int:
    ref = store.insert_ref(
        kind="profile",
        slug=None,
        title="retos",
        meta={"profile": {"name": "retos", "palette": dict(PALETTE)}},
        owner_login="reto",
    )
    return ref.id


def _profile_rows(store: Store, owner: str) -> list[Any]:
    return store.list_refs(kind="profile", owner_login=owner, limit=50)


def test_put_derives_owner_and_round_trips_kwargs(mounted: Store) -> None:
    with revision_context(actor="user:al"):
        out = tools_core.put(
            kind="profile",
            name="mine",
            palette=dict(PALETTE),
            sponsor_line="Funded by X",
        )
    pid = _id(out)
    ref = _ref(mounted, pid)
    assert ref.owner_login == "alice"
    payload = profiles.payload_of(ref)
    assert payload["palette"]["accent"] == "#005335"
    assert payload["sponsor_line"] == "Funded by X"


def test_put_with_foreign_owner_is_refused(mounted: Store) -> None:
    with revision_context(actor="user:al"):
        out = tools_core.put(
            kind="profile", name="x", palette=dict(PALETTE), owner="reto"
        )
    assert "owner" in _body(out).lower()
    assert "created" not in _body(out).lower()
    assert _profile_rows(mounted, "reto") == []
    assert _profile_rows(mounted, "alice") == []


def test_edit_cannot_reassign_owner(mounted: Store) -> None:
    with revision_context(actor="user:al"):
        pid = _id(tools_core.put(kind="profile", name="mine", palette=dict(PALETTE)))
        out = tools_core.edit(kind="profile", id=pid, owner="reto")
    assert "owner" in _body(out).lower()
    assert _ref(mounted, pid).owner_login == "alice"


def test_edit_foreign_profile_is_not_found_and_unchanged(mounted: Store) -> None:
    pid = _reto_profile(mounted)
    with revision_context(actor="user:al"):
        out = tools_core.edit(kind="profile", id=pid, palette=dict(OTHER))
    assert "not found" in _body(out).lower() or "NotFound" in _body(out)
    ref = _ref(mounted, pid)
    assert profiles.payload_of(ref)["palette"]["accent"] == "#005335"


def test_edit_own_profile_sponsor_line_works(mounted: Store) -> None:
    with revision_context(actor="user:al"):
        pid = _id(tools_core.put(kind="profile", name="mine", palette=dict(PALETTE)))
        out = tools_core.edit(kind="profile", id=pid, sponsor_line="x")
    assert not (isinstance(out, CallToolResult) and out.isError), _body(out)
    ref = _ref(mounted, pid)
    assert profiles.payload_of(ref)["sponsor_line"] == "x"
