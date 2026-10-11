"""gr477174 (todo focus page doable queue) and gr477173 (smartdraft Needs pane)."""

from __future__ import annotations


def _doable_calls(runtime) -> list[dict]:
    return [a for v, a in runtime.calls if v == "search" and a.get("view") == "doable"]


def test_focus_page_scopes_doable_queue(client, runtime) -> None:
    """A focused subtree page asks for doable leaves ``under`` that node, not
    the global rotation queue (which named an unrelated cast watch)."""
    resp = client.get("/todo?focus=1")
    assert resp.status_code == 200
    assert _doable_calls(runtime)[-1]["under"] == 1
    assert "Doable under #1" in resp.text


def test_unfocused_page_keeps_global_doable_queue(client, runtime) -> None:
    resp = client.get("/todo")
    assert resp.status_code == 200
    assert "under" not in _doable_calls(runtime)[-1]
    assert "Doable next" in resp.text


def test_linkify_todo_handles_links_and_escapes() -> None:
    from precis_web.routes.todo import _linkify_todo_handles

    out = str(_linkify_todo_handles("td161192 <b>x</b> & td7"))
    assert (
        '<a class="text-sky-700 hover:underline" href="/todo?focus=161192">td161192</a>'
        in out
    )
    assert 'href="/todo?focus=7">td7</a>' in out
    assert "&lt;b&gt;" in out
    assert "<b>" not in out


def _item(tid, jobs=(), asks=(), blocked=False) -> dict:
    return {
        "todo_id": tid,
        "title": f"t{tid}",
        "blocked": blocked,
        "jobs": [{"id": i, "status": s, "reason": None} for i, s in jobs],
        "asks": [{"tag": "ask-user", "question": q} for q in asks],
        "ask_tags": [],
    }


def test_needs_items_hides_idle_project_container(monkeypatch) -> None:
    """The draft's own project todo is dropped when it has no asks, isn't
    blocked and its latest job succeeded; ordinary child todos stay."""
    from precis_web.routes import drafts as drafts_mod
    from precis_web.routes.smartdraft import _needs_items

    items = [
        _item(10, jobs=[(1, "failed"), (2, "succeeded")]),
        _item(11, jobs=[(3, "succeeded")]),
    ]
    monkeypatch.setattr(drafts_mod, "_work_items", lambda store, ref_id: items)
    monkeypatch.setattr(drafts_mod, "_project_id", lambda store, ref_id: 10)
    assert [r["todo_id"] for r in _needs_items(None, 700)] == [11]


def test_needs_items_keeps_project_that_wants_something(monkeypatch) -> None:
    from precis_web.routes import drafts as drafts_mod
    from precis_web.routes.smartdraft import _needs_items

    items: list[dict] = []
    monkeypatch.setattr(drafts_mod, "_work_items", lambda store, ref_id: items)
    monkeypatch.setattr(drafts_mod, "_project_id", lambda store, ref_id: 10)
    for keep in (
        _item(10, asks=["which?"]),
        _item(10, blocked=True),
        _item(10, jobs=[(1, "running")]),
        _item(10, jobs=[(1, "failed")]),
    ):
        items[:] = [keep]
        assert [r["todo_id"] for r in _needs_items(None, 700)] == [10]
