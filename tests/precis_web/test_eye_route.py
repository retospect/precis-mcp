"""``/eye/<handle>`` — the focus page (``fisheye-everywhere.md`` in-scope 4,
AC 5): the same ladder as the MCP ``get(extent=)``, rendered in the
browser with every handle linked to its own focus page."""

from __future__ import annotations

import base64
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.profile import ProfileHandler
from precis.users import hash_password
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.eye import linkify_handles

pytestmark = pytest.mark.db


@pytest.fixture
def client(runtime_with_store: Any) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def _seed(store: Any) -> tuple[Any, Any]:
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    quest = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    store.add_link(src_ref_id=todo.id, dst_ref_id=quest.id, relation="serves")
    return todo, quest


def test_focus_page_stamps_server_timing(client: TestClient, store: Any) -> None:
    """gr489660: every /eye response carries Server-Timing so a cold load
    is attributable in logs/devtools."""
    todo, _quest = _seed(store)
    r = client.get(f"/eye/td{todo.id}")
    assert r.headers.get("server-timing", "").startswith("app;dur=")


def test_focus_page_renders_the_mcp_eye_with_linked_handles(
    client: TestClient, runtime_with_store: Any, store: Any
) -> None:
    todo, quest = _seed(store)
    page = client.get(f"/eye/td{todo.id}")  # default rung: fisheye+1hop
    assert page.status_code == 200
    mcp_body, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "todo", "id": todo.id, "extent": "fisheye+1hop"}
    )
    assert not err
    assert "Roadmap:" in page.text and "serves: <a" in page.text
    assert f'href="/eye/qu{quest.id}?extent=fisheye%2B1hop"' in page.text
    # the MCP head line, with its own handle turned into a self-link
    assert mcp_body.splitlines()[0] == f"td{todo.id} [todo] Ship the ring"
    assert f">td{todo.id}</a> [todo] Ship the ring" in page.text
    # the ladder row names every rung, the current one marked
    for rung in ("kwd", "summary", "verbatim", "fisheye", "fisheye+2hop", "+recall"):
        assert f">{rung}</a>" in page.text
    assert f"/todo?focus={todo.id}" in page.text  # the native reader link


def test_focus_page_takes_the_rung_and_the_group_filter(
    client: TestClient, store: Any
) -> None:
    todo, _quest = _seed(store)
    kwd = client.get(f"/eye/td{todo.id}", params={"extent": "kwd"})
    assert kwd.status_code == 200
    assert "— linked (1 hop) —" not in kwd.text and "Ship the ring" in kwd.text
    bad = client.get(f"/eye/td{todo.id}", params={"extent": "fisheye+9hop"})
    assert bad.status_code == 400 and "Unknown rung" in bad.text
    # q= at fisheye+2hop reaches the dispatcher as the group filter
    grp = client.get(
        f"/eye/td{todo.id}", params={"extent": "fisheye+2hop", "q": "paper:cites"}
    )
    assert grp.status_code == 200 and "no second-hop group" in grp.text


def test_focus_page_on_a_skill_and_on_a_dead_handle(
    client: TestClient, store: Any
) -> None:
    skill = client.get("/eye/sk:precis-fisheye-help", params={"extent": "kwd"})
    assert skill.status_code == 200
    assert ">sk:precis-fisheye-help</a> [skill]" in skill.text
    todo, _quest = _seed(store)
    store.retire_ref(todo.id)
    gone = client.get(f"/eye/td{todo.id}")
    assert gone.status_code == 404 and "[error:" in gone.text
    assert client.get("/eye/not-a-handle").status_code == 404


def test_focus_page_keeps_the_up_link_on_a_soft_deleted_focus(
    client: TestClient, store: Any
) -> None:
    """``/graph`` parity: the focus resolves ``include_deleted``, so a
    soft-deleted focus — body 404s on ``[error:Gone]`` — still shows its
    up link to a live far end, or the rule's reason when none is live."""
    parent = store.insert_ref(kind="quest", slug=None, title="Parent")
    focus = store.insert_ref(kind="quest", slug=None, title="Retired")
    store.add_link(src_ref_id=focus.id, dst_ref_id=parent.id, relation="serves")
    store.retire_ref(focus.id)
    page = client.get(f"/eye/qu{focus.id}")
    assert page.status_code == 404 and "[error:" in page.text
    assert f'data-zoom-out="qu{parent.id}"' in page.text
    assert f'href="/eye/qu{parent.id}?extent=fisheye%2B1hop"' in page.text
    # no live far end: the disabled reason shows rather than hiding the row
    orphan = store.insert_ref(kind="todo", slug=None, title="Orphan")
    store.retire_ref(orphan.id)
    page = client.get(f"/eye/td{orphan.id}")
    assert page.status_code == 404
    assert "no live parent (out) link from this todo" in page.text


def test_focus_page_ring_filters_narrow_and_chip(
    client: TestClient, store: Any
) -> None:
    """``fisheye-everywhere.md`` AC 1: ``?kind=&space=`` narrow every
    ring; the URL is the state — chips show it and every linked handle
    carries it forward."""
    from precis.store import Tag

    focus = store.insert_ref(kind="memory", slug=None, title="Focus")
    repo = store.insert_ref(kind="memory", slug=None, title="Repo note")
    other = store.insert_ref(kind="memory", slug=None, title="Research note")
    paper = store.insert_ref(kind="paper", slug="wenzel16", title="XPS")
    store.add_tag(repo.id, Tag.closed("SPACE", "repo-dev"), set_by="system")
    for near in (repo, other, paper):
        store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="related-to")

    page = client.get(
        f"/eye/me{focus.id}", params={"kind": "memory", "space": "repo-dev"}
    )
    assert page.status_code == 200
    assert f"me{repo.id}" in page.text  # the matching neighbour
    assert f"me{other.id}" not in page.text.split("ring:")[-1]  # untagged: out
    assert f"pa{paper.id}" not in page.text.split("ring:")[-1]  # wrong kind: out
    # the chips reflect the URL state and the links carry it forward
    assert "kind:memory" in page.text and "space:repo-dev" in page.text
    assert (
        f"/eye/me{repo.id}?extent=fisheye%2B1hop&kind=memory&space=repo-dev"
        in page.text
    )
    # the same URL without the params renders the full ring, no chips
    full = client.get(f"/eye/me{focus.id}")
    assert f"me{other.id}" in full.text and f"pa{paper.id}" in full.text
    assert 'aria-label="ring filters"' not in full.text


def test_focus_page_rejects_a_bad_ring_filter(client: TestClient, store: Any) -> None:
    focus = store.insert_ref(kind="memory", slug=None, title="Focus")
    bad = client.get(f"/eye/me{focus.id}", params={"kind": "nosuchkind"})
    assert bad.status_code == 400 and "unknown kind" in bad.text
    bad = client.get(f"/eye/me{focus.id}", params={"space": "outer"})
    assert bad.status_code == 400 and "unknown space" in bad.text


def test_focus_page_redirects_a_draft_handle_to_its_root_section(
    client: TestClient, store: Any
) -> None:
    """gr489478: ``/eye/dr<id>`` focused nothing — a draft's eye is its
    root section, so the record handle redirects to the ``dc`` chunk."""
    ref, title_chunk = store.drafts.create_draft(name="deye", title="D")
    r = client.get(f"/eye/dr{ref.id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/eye/dc{title_chunk.chunk_id}?extent=fisheye%2B1hop"
    )
    # ring filter / rung state rides the redirect
    r = client.get(
        f"/eye/dr{ref.id}",
        params={"extent": "fisheye", "kind": "memory"},
        follow_redirects=False,
    )
    assert (
        r.headers["location"]
        == f"/eye/dc{title_chunk.chunk_id}?extent=fisheye&kind=memory"
    )
    page = client.get(f"/eye/dr{ref.id}")
    assert page.status_code == 200 and f"dc{title_chunk.chunk_id}" in page.text


def test_focus_page_second_hop_counts_link_to_their_expansion(
    client: TestClient, store: Any
) -> None:
    """gr489577: each ``N kind via rel`` line links to the page with that
    group's ``q=<kind>:<label>`` expansion applied."""
    focus = store.insert_ref(kind="todo", slug=None, title="Focus")
    near = store.insert_ref(kind="quest", slug=None, title="Near")
    far = store.insert_ref(kind="paper", slug="g18", title="Far")
    store.add_link(src_ref_id=focus.id, dst_ref_id=near.id, relation="serves")
    store.add_link(src_ref_id=near.id, dst_ref_id=far.id, relation="cites")
    page = client.get(f"/eye/td{focus.id}", params={"extent": "fisheye+2hop"})
    assert page.status_code == 200
    assert "1 paper via cites" in page.text
    assert f"/eye/td{focus.id}?extent=fisheye%2B2hop&q=paper%3Acites" in page.text


def test_index_redirects_a_handle_and_searches_a_query(
    client: TestClient, store: Any
) -> None:
    todo, _quest = _seed(store)
    r = client.get("/eye/", params={"q": f"td{todo.id}"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/eye/td{todo.id}?extent=fisheye%2B1hop"
    hits = client.get("/eye/", params={"q": "ring"})
    assert hits.status_code == 200 and 'name="q"' in hits.text


def test_linkify_handles_links_only_real_handles() -> None:
    out = linkify_handles(
        "see me4641 and pc13 — not ab12, mao18 or 5mx20; sk:precis-get-help"
    )
    assert (
        '<a class="eye-handle" href="/eye/me4641?extent=fisheye%2B1hop">me4641</a>'
        in out
    )
    assert 'href="/eye/pc13?' in out
    assert "ab12" in out and 'href="/eye/ab12' not in out
    assert 'href="/eye/mao18' not in out and 'href="/eye/mx20' not in out
    assert 'href="/eye/sk:precis-get-help?' in out
    assert linkify_handles("<b>") == "&lt;b&gt;"


# ── gr490266: /eye must not leak another owner's profile ─────────────


@pytest.fixture
def authed_client(runtime_with_store: Any) -> TestClient:
    """Basic gate on, so the signed-in user reaches the handler as
    ``user:<abbrev>`` (the plain ``client`` runs auth-off, anonymous)."""
    cfg = WebConfig(corpus_dir=None, auth_required=True)
    return TestClient(create_app(runtime=runtime_with_store, web_config=cfg))


def _basic(login: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _seed_reto_profile(store: Any) -> Any:
    store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.upsert_kinds([ProfileHandler.spec])
    return store.insert_ref(
        kind="profile",
        slug=None,
        title="SecretPalette",
        meta={"profile": {"name": "SecretPalette"}},
        owner_login="reto",
    )


def test_eye_profile_is_hidden_from_another_owner(
    authed_client: TestClient, store: Any
) -> None:
    prof = _seed_reto_profile(store)
    r = authed_client.get(f"/eye/pf{prof.id}", headers=_basic("alice", "pw"))
    assert r.status_code == 404
    assert "SecretPalette" not in r.text


def test_eye_profile_is_visible_to_its_owner(
    authed_client: TestClient, store: Any
) -> None:
    prof = _seed_reto_profile(store)
    r = authed_client.get(f"/eye/pf{prof.id}", headers=_basic("reto", "pw"))
    assert r.status_code == 200
    assert "SecretPalette" in r.text


def test_eye_hub_ring_hides_other_owners_profile(
    authed_client: TestClient, store: Any
) -> None:
    prof = _seed_reto_profile(store)
    hub = store.insert_ref(kind="todo", slug=None, title="Ring hub")
    store.add_link(src_ref_id=hub.id, dst_ref_id=prof.id, relation="related-to")
    url = f"/eye/{handle_registry.format_handle('todo', hub.id)}"
    r = authed_client.get(url, headers=_basic("alice", "pw"))
    assert r.status_code == 200
    assert "Ring hub" in r.text
    assert "SecretPalette" not in r.text
    r = authed_client.get(url, headers=_basic("reto", "pw"))
    assert r.status_code == 200
    assert "SecretPalette" in r.text
