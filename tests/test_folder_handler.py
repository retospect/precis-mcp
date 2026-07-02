"""FolderHandler + placement tests (ADR 0045).

Exercises the container kind end-to-end against a live store: create /
list / open / tree, nesting with cycle rejection, placing artifacts via
the generalized ``rel='parent'`` façade (cad exemplar for slug kinds,
todo strategic roots for the scheduling tree), unfiling, and the
refuse-non-empty delete. Also pins the kind-aware root predicate: a
strategic root placed in a folder stays visible to the roots view.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.cad import CadHandler
from precis.handlers.folder import FolderHandler
from precis.handlers.todo import TodoHandler
from tests.conftest import id_of


@pytest.fixture
def folder(store) -> FolderHandler:
    return FolderHandler(hub=Hub(store=store))


@pytest.fixture
def todo(store) -> TodoHandler:
    return TodoHandler(hub=Hub(store=store))


def _mk(folder: FolderHandler, name: str) -> int:
    return id_of(folder.put(text=name).body)


# ── create / list / open ───────────────────────────────────────────


def test_put_creates_and_index_lists(folder: FolderHandler) -> None:
    fid = _mk(folder, "Hardware")
    idx = folder.get()
    assert "Hardware" in idx.body
    assert f"folder:{fid}" in idx.body


def test_get_empty_folder_renders_hints(folder: FolderHandler) -> None:
    fid = _mk(folder, "Empty")
    resp = folder.get(id=fid)
    assert "(empty)" in resp.body
    assert "rel='parent'" in resp.body


def test_no_folders_yet_hint(folder: FolderHandler) -> None:
    resp = folder.get()
    assert "no folders yet" in resp.body


def test_edit_renames(folder: FolderHandler) -> None:
    fid = _mk(folder, "Hardwear")
    resp = folder.edit(id=fid, text="Hardware")
    assert "renamed" in resp.body
    assert "Hardware" in folder.get(id=fid).body
    with pytest.raises(BadInput, match="rename requires"):
        folder.edit(id=fid, text="  ")


# ── nesting + cycle guard ──────────────────────────────────────────


def test_nest_and_breadcrumb(folder: FolderHandler) -> None:
    top = _mk(folder, "Projects")
    sub = _mk(folder, "Hardware")
    resp = folder.link(id=sub, target=f"folder:{top}", rel="parent")
    assert "placed" in resp.body
    opened = folder.get(id=sub)
    assert "path: /Projects/Hardware" in opened.body
    # parent folder now lists the child
    assert "Hardware" in folder.get(id=top).body


def test_nest_cycle_rejected(folder: FolderHandler) -> None:
    a = _mk(folder, "A")
    b = _mk(folder, "B")
    folder.link(id=b, target=f"folder:{a}", rel="parent")
    with pytest.raises(BadInput, match="cycle"):
        folder.link(id=a, target=f"folder:{b}", rel="parent")


def test_self_placement_rejected(folder: FolderHandler) -> None:
    a = _mk(folder, "A")
    with pytest.raises(BadInput, match="inside itself"):
        folder.link(id=a, target=f"folder:{a}", rel="parent")


def test_placement_target_must_be_folder(folder: FolderHandler, store) -> None:
    a = _mk(folder, "A")
    other = store.insert_ref(kind="memory", slug=None, title="a memory", meta={})
    with pytest.raises(BadInput, match="not a folder"):
        folder.link(id=a, target=f"memory:{other.id}", rel="parent")


def test_placement_missing_folder_raises(folder: FolderHandler) -> None:
    a = _mk(folder, "A")
    with pytest.raises((NotFound, BadInput)):
        folder.link(id=a, target="folder:99999", rel="parent")


# ── placing slug artifacts (cad as the exemplar) ───────────────────


def test_place_and_unfile_cad_design(folder: FolderHandler, store) -> None:
    fid = _mk(folder, "Designs")
    store.insert_ref(kind="cad", slug="widget", title="a widget", meta={})
    cad = CadHandler(hub=Hub(store=store))
    resp = cad.link(id="widget", target=f"folder:{fid}", rel="parent")
    assert "placed" in resp.body
    ref = store.get_ref(kind="cad", id="widget")
    assert ref is not None and ref.parent_id == fid
    # shows in the folder contents
    assert "widget" in folder.get(id=fid).body
    # unfile
    resp = cad.link(id="widget", rel="parent", mode="remove")
    assert "unfiled" in resp.body
    ref = store.get_ref(kind="cad", id="widget")
    assert ref is not None and ref.parent_id is None


def test_unfile_wrong_current_folder_rejected(folder: FolderHandler, store) -> None:
    f1 = _mk(folder, "One")
    f2 = _mk(folder, "Two")
    store.insert_ref(kind="cad", slug="thing", title="a thing", meta={})
    cad = CadHandler(hub=Hub(store=store))
    cad.link(id="thing", target=f"folder:{f1}", rel="parent")
    with pytest.raises(BadInput, match="not in"):
        cad.link(id="thing", target=f"folder:{f2}", rel="parent", mode="remove")


def test_cad_link_rejects_other_relations(store) -> None:
    store.insert_ref(kind="cad", slug="gizmo", title="a gizmo", meta={})
    cad = CadHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="only rel='parent'"):
        cad.link(id="gizmo", target="cad:gizmo", rel="related-to")


# ── todo roots in folders (kind-aware root predicate) ──────────────


def test_strategic_root_placed_in_folder_stays_a_root(
    folder: FolderHandler, todo: TodoHandler, store
) -> None:
    fid = _mk(folder, "Ventures")
    root = id_of(todo.put(text="Build the platform.").body)
    todo.tag(id=root, add=["level:strategic"])
    resp = todo.link(id=root, target=f"folder:{fid}", rel="parent")
    assert "placed" in resp.body
    ref = store.get_ref(kind="todo", id=root)
    assert ref is not None and ref.parent_id == fid
    # still a strategic root for the views (ADR 0045 §4)
    from precis.handlers._todo_views import render_roots

    assert "Build the platform." in render_roots(store).body
    # and the folder shows it as content
    assert "Build the platform." in folder.get(id=fid).body


def test_non_strategic_todo_cannot_be_placed(
    folder: FolderHandler, todo: TodoHandler
) -> None:
    fid = _mk(folder, "Ventures")
    leaf = id_of(todo.put(text="a loose leaf").body)
    with pytest.raises(BadInput, match="strategic"):
        todo.link(id=leaf, target=f"folder:{fid}", rel="parent")


def test_child_under_folder_parented_strategic(
    folder: FolderHandler, todo: TodoHandler, store
) -> None:
    """Folder levels don't consume tree depth: creating children under a
    folder-parented strategic behaves exactly as under a bare root."""
    fid = _mk(folder, "Ventures")
    root = id_of(todo.put(text="Strategic thing.").body)
    todo.tag(id=root, add=["level:strategic"])
    todo.link(id=root, target=f"folder:{fid}", rel="parent")
    child = id_of(todo.put(text="Tactical child.", parent_id=root).body)
    ref = store.get_ref(kind="todo", id=child)
    assert ref is not None and ref.parent_id == root


def test_todo_links_view_renders_folder_parent(
    folder: FolderHandler, todo: TodoHandler
) -> None:
    fid = _mk(folder, "Ventures")
    root = id_of(todo.put(text="Strategic thing.").body)
    todo.tag(id=root, add=["level:strategic"])
    todo.link(id=root, target=f"folder:{fid}", rel="parent")
    links = todo.get(id=root, view="links")
    assert f"folder:{fid}" in links.body


# ── tree view ──────────────────────────────────────────────────────


def test_tree_view_renders_subtree(folder: FolderHandler, store) -> None:
    top = _mk(folder, "Projects")
    sub = _mk(folder, "Hardware")
    folder.link(id=sub, target=f"folder:{top}", rel="parent")
    store.insert_ref(kind="cad", slug="bracket", title="a bracket", meta={})
    CadHandler(hub=Hub(store=store)).link(
        id="bracket", target=f"folder:{sub}", rel="parent"
    )
    tree = folder.get(id=top, view="tree")
    assert "Projects" in tree.body
    assert "Hardware" in tree.body
    assert "bracket" in tree.body
    assert "(2 items)" in tree.body


# ── folder= search scope (ADR 0045 §6) ─────────────────────────────


@pytest.fixture
def rt(store):
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import make_embedder
    from precis.runtime import PrecisRuntime

    return PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(
            store=store, embedder=make_embedder("mock", dim=store.embedding_dim())
        ),
    )


def _scoped_corpus(folder: FolderHandler, todo: TodoHandler) -> int:
    """One strategic todo inside a folder, one identical-topic todo outside."""
    fid = id_of(folder.put(text="Scope").body)
    inside = id_of(todo.put(text="Quantum platform work inside.").body)
    todo.tag(id=inside, add=["level:strategic"])
    todo.link(id=inside, target=f"folder:{fid}", rel="parent")
    todo.put(text="Quantum platform work outside.")
    return fid


def test_search_folder_scope_filters_subtree(
    rt, folder: FolderHandler, todo: TodoHandler
) -> None:
    fid = _scoped_corpus(folder, todo)
    out = rt.dispatch(
        "search", {"kind": "todo", "q": "quantum platform", "folder": fid}
    )
    assert "inside" in out
    assert "outside" not in out


def test_search_folder_scope_by_name(
    rt, folder: FolderHandler, todo: TodoHandler
) -> None:
    _scoped_corpus(folder, todo)
    out = rt.dispatch(
        "search", {"kind": "todo", "q": "quantum platform", "folder": "scope"}
    )
    assert "inside" in out
    assert "outside" not in out


def test_search_folder_scope_unknown_name(rt) -> None:
    out = rt.dispatch("search", {"q": "anything", "folder": "no-such-folder"})
    assert "[error:NotFound]" in out
    assert "no folder named" in out


def test_search_folder_scope_empty_result_names_folder(
    rt, folder: FolderHandler
) -> None:
    fid = id_of(folder.put(text="Barren").body)
    # kind pinned so the fan-out stays local (wildcard would sweep the
    # remote cache kinds too — not something a unit test should do).
    out = rt.dispatch("search", {"kind": "todo", "q": "zebra xylophone", "folder": fid})
    assert "Barren" in out


# ── delete policy ──────────────────────────────────────────────────


def test_delete_refuses_non_empty(folder: FolderHandler) -> None:
    top = _mk(folder, "Projects")
    sub = _mk(folder, "Hardware")
    folder.link(id=sub, target=f"folder:{top}", rel="parent")
    with pytest.raises(BadInput, match="live item"):
        folder.delete(id=top)
    # unfile the child, then delete succeeds
    folder.link(id=sub, rel="parent", mode="remove")
    resp = folder.delete(id=top)
    assert "deleted" in resp.body
