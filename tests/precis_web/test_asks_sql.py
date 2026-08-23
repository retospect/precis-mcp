"""Real-PG regression tests for the asks/Needs-you raw SQL.

The ``test_routes.py`` suite runs against the web ``FakeStore``, which
never parses SQL, so a query naming a column that does not exist renders
fine there and 500s in prod. That is exactly how ``_project_draft`` came
to project ``refs.slug`` — a column ``refs`` has never had (the
agent-facing slug lives in ``ref_identifiers`` as a ``cite_key``) —
which took the whole ``/needs-you`` landing down with ``UndefinedColumn``
whenever an ask had no chunk-level context to show.

These exercise the three raw-SQL helpers on that path (``_load_asks``,
``_crumb``, ``_project_draft``) against the live ``store`` fixture.
"""

from __future__ import annotations

from precis.store.types import Tag
from precis_web.routes.asks import _crumb, _load_asks, _project_draft


def _todo(store, title: str, *, parent_id: int | None = None):
    return store.insert_ref(
        kind="todo", slug=None, title=title, meta={}, parent_id=parent_id
    )


def test_project_draft_resolves_the_draft_bound_to_an_ancestor(store) -> None:
    """The ``draft-of`` draft is found and linked by its slug, not by a
    ``refs.slug`` column (there isn't one)."""
    project = _todo(store, "project root")
    draft = store.insert_ref(
        kind="draft", slug="ask-ctx-draft", title="The Draft\nsecond line", meta={}
    )
    store.add_link(src_ref_id=draft.id, dst_ref_id=project.id, relation="draft-of")

    doc = _project_draft(store, [project.id])

    assert doc == {"title": "The Draft", "url": "/smartdraft/ask-ctx-draft"}


def test_project_draft_falls_back_on_a_titleless_draft_and_no_link(store) -> None:
    """An untitled draft still names itself; an unbound todo gets ``None``."""
    project = _todo(store, "untitled-draft project")
    draft = store.insert_ref(kind="draft", slug="untitled-draft", title="", meta={})
    store.add_link(src_ref_id=draft.id, dst_ref_id=project.id, relation="draft-of")

    doc = _project_draft(store, [project.id])
    assert doc == {
        "title": f"draft #{draft.id}",
        "url": "/smartdraft/untitled-draft",
    }

    assert _project_draft(store, [_todo(store, "unbound").id]) is None
    assert _project_draft(store, []) is None


def test_crumb_returns_ancestors_root_first(store) -> None:
    """The breadcrumb walks ``refs.parent_id`` up, excluding the todo itself."""
    root = _todo(store, "root project")
    mid = _todo(store, "sub-project", parent_id=root.id)
    leaf = _todo(store, "the ask's todo", parent_id=mid.id)

    crumb = _crumb(store, leaf.id)

    assert [c["id"] for c in crumb] == [root.id, mid.id]
    assert [c["title"] for c in crumb] == ["root project", "sub-project"]


def test_load_asks_rows_carry_questions_crumb_and_project_draft(store) -> None:
    """The full ``/needs-you`` row build — the path that used to 500."""
    project = _todo(store, "project root")
    todo = _todo(store, "wire up the thing", parent_id=project.id)
    store.add_tag(todo.id, Tag.open("ask-user:which database?"))
    draft = store.insert_ref(
        kind="draft", slug="needs-you-draft", title="Bound draft", meta={}
    )
    store.add_link(src_ref_id=draft.id, dst_ref_id=project.id, relation="draft-of")

    rows = _load_asks(store, limit=10)

    row = next(r for r in rows if r["id"] == todo.id)
    assert row["questions"] == ["which database?"]
    assert row["tags"] == ["ask-user:which database?"]
    assert [c["id"] for c in row["crumb"]] == [project.id]
    # No ``meta.anchor`` and no dc/pe handle in the prose, so the row
    # falls back to the project-level draft — the _project_draft branch.
    assert row["context"]["draft"] == {
        "title": "Bound draft",
        "url": "/smartdraft/needs-you-draft",
    }


def test_load_asks_excludes_closed_todos(store) -> None:
    """A ``STATUS:done`` todo drops out even while its ask tag lingers."""
    todo = _todo(store, "already handled")
    store.add_tag(todo.id, Tag.open("ask-user:stale question?"))
    store.add_tag(todo.id, Tag.closed("STATUS", "done"))

    assert all(r["id"] != todo.id for r in _load_asks(store, limit=50))
