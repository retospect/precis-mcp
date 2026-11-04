"""Presentation Meta tags share the paper controls and the tag verb."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.presentation import PresentationHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime


@pytest.mark.parametrize(
    ("kind", "path", "ref_id"),
    [("pres", "/pres/2001-lecture01", 60), ("paper", "/papers/smith2024", 10)],
)
def test_meta_tag_controls_render_open_and_closed_tags(
    client: TestClient,
    runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    path: str,
    ref_id: int,
) -> None:
    """The paper controls retain their actions, labels, classes and inputs."""
    monkeypatch.setattr(
        runtime.store,
        "tags_for",
        lambda *_args, **_kw: [
            Tag.open("needs-triage"),
            Tag.closed("STATUS", "active"),
            Tag.flag("pinned"),
        ],
    )

    response = client.get(path)

    assert response.status_code == 200
    html = unescape(response.text)
    prefix = "papers" if kind == "paper" else "pres"
    forms = re.findall(
        rf'<form method="post" action="/{prefix}/{ref_id}/tags"[^>]*>(.*?)</form>',
        html,
        re.S,
    )
    assert len(forms) == 4
    assert 'name="remove" value="needs-triage"' in forms[0]
    assert 'title="remove tag">×</button>' in forms[0]
    assert "bg-surface-muted" in forms[0]
    assert 'name="add" value="read-later"' in forms[1]
    assert "+ read later</button>" in forms[1]
    assert 'name="add" value="starred"' in forms[2]
    assert "+ starred</button>" in forms[2]
    assert 'name="add" placeholder="+ tag"' in forms[3]
    assert "bg-accent-subtle" in html
    assert (
        f"closed-vocab tag — remove via tag(kind='{kind}', id={ref_id}, remove=[…])"
        in html
    )
    assert ">STATUS:active</span>" in html
    assert ">pinned</span>" in html
    assert not any("active" in form or "pinned" in form for form in forms)
    assert ">open:needs-triage</span>" not in html
    assert ">closed:active</span>" not in html
    assert ">flag:pinned</span>" not in html


@pytest.mark.parametrize("path", ["/pres/2001-lecture01", "/papers/smith2024"])
def test_meta_tags_empty_state(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert "no tags yet" in response.text
    assert 'placeholder="+ tag"' in response.text


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ({"add": "foo, bar baz"}, {"add": ["foo", "bar", "baz"]}),
        ({"remove": "needs-triage"}, {"remove": ["needs-triage"]}),
        (
            {"add": "starred", "remove": "read-later"},
            {"add": ["starred"], "remove": ["read-later"]},
        ),
    ],
)
def test_pres_tags_dispatch_and_return_to_meta(
    client: TestClient,
    runtime: FakeRuntime,
    form: dict[str, str],
    expected: dict[str, list[str]],
) -> None:
    response = client.post("/pres/60/tags", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/pres/60?tab=Meta"
    assert runtime.calls[-1] == ("tag", {"kind": "pres", "id": 60, **expected})


def test_pres_tags_empty_submit_does_not_dispatch(
    client: TestClient, runtime: FakeRuntime
) -> None:
    response = client.post("/pres/60/tags", data={"add": " , "}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/pres/60?tab=Meta"
    assert not any(verb == "tag" for verb, _args in runtime.calls)


@pytest.mark.parametrize("ref_id", [10, 999999])
def test_pres_tags_reject_wrong_kind_or_missing_ref(
    client: TestClient, runtime: FakeRuntime, ref_id: int
) -> None:
    response = client.post(f"/pres/{ref_id}/tags", data={"add": "starred"})

    assert response.status_code == 404
    assert not any(verb == "tag" for verb, _args in runtime.calls)


def test_pres_tags_post_only(client: TestClient, runtime: FakeRuntime) -> None:
    response = client.get("/pres/60/tags")

    assert response.status_code == 405
    assert not any(verb == "tag" for verb, _args in runtime.calls)


def test_pres_tags_surface_handler_error(
    client: TestClient, runtime: FakeRuntime
) -> None:
    runtime.error_verbs.add("tag")

    response = client.post("/pres/60/tags", data={"add": "starred"})

    assert response.status_code == 400
    assert "rejected by handler" in response.text
    assert "location" not in response.headers


@pytest.mark.parametrize("path", ["/pres/60/tags", "/papers/10/tags"])
def test_meta_tag_posts_require_authentication(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(
        runtime.store, "count_web_users", lambda **_kw: 1, raising=False
    )
    app = create_app(runtime=runtime, web_config=WebConfig(auth_required=True))

    with TestClient(app) as authenticated_client:
        response = authenticated_client.post(path, data={"add": "starred"})

    assert response.status_code == 401
    assert not any(verb == "tag" for verb, _args in runtime.calls)


@pytest.mark.parametrize(
    "form",
    [
        {"add": "STATUS:done"},
        {"add": "CACHE:pinned"},
        {"remove": "STATUS:done"},
        {"add": "starred, CACHE:pinned", "remove": "read-later"},
        {"add": "starred", "remove": "read-later, STATUS:done"},
    ],
)
def test_pres_tags_reject_closed_tags_without_changes(
    runtime_with_store: PrecisRuntime,
    store: Store,
    tmp_path: Path,
    form: dict[str, str],
) -> None:
    """Reject the whole form, preserving tags that MCP can still edit."""
    handler = PresentationHandler(hub=Hub(store=store))
    handler.put(id="guarded-deck", title="Guard this deck", text="Slide body")
    ref = store.get_ref(kind="pres", id="guarded-deck")
    assert ref is not None
    handler.tag(id=ref.id, add=["STATUS:done", "read-later"])
    before = store.tags_for(ref.id)
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )

    with TestClient(app) as real_client:
        response = real_client.post(
            f"/pres/{ref.id}/tags", data=form, follow_redirects=False
        )

    assert response.status_code == 400
    assert "Tag error" in response.text
    assert "Only open tags can be edited here" in response.text
    assert "location" not in response.headers
    assert store.tags_for(ref.id) == before


def test_pres_tags_add_and_remove_round_trip(
    runtime_with_store: PrecisRuntime, store: Store, tmp_path: Path
) -> None:
    """Real dispatch/storage verifies the route changes the requested deck."""
    handler = PresentationHandler(hub=Hub(store=store))
    handler.put(id="tagged-deck", title="Tag this deck", text="Slide body")
    ref = store.get_ref(kind="pres", id="tagged-deck")
    assert ref is not None
    handler.put(id=str(ref.id), title="Keep numeric slug", text="Other slide")
    other = store.get_ref(kind="pres", id=str(ref.id))
    assert other is not None
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )

    with TestClient(app) as real_client:
        added = real_client.post(
            f"/pres/{ref.id}/tags", data={"add": "starred"}, follow_redirects=False
        )
        assert added.status_code == 303, added.text
        assert any(tag.value == "starred" for tag in store.tags_for(ref.id))
        assert not any(tag.value == "starred" for tag in store.tags_for(other.id))
        rendered = real_client.get(f"/pres/{ref.id}")
        assert rendered.status_code == 200
        assert 'name="remove" value="starred"' in rendered.text

        removed = real_client.post(
            f"/pres/{ref.id}/tags", data={"remove": "starred"}, follow_redirects=False
        )
        assert removed.status_code == 303, removed.text

    assert not any(tag.value == "starred" for tag in store.tags_for(ref.id))
