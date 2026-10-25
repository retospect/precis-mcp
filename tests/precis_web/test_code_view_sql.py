"""Real-PG tests for the code view's raw SQL (``_FOLDERS_SQL`` / ``_FILES_SQL``).

``FakeStore`` never parses SQL, so the ``meta ? 'workspace'`` jsonb guard,
the design-spelling match and the ``part-of`` EXISTS branch are only proven
here, through ``_workspaces`` against the live ``store`` fixture.
"""

from __future__ import annotations

from types import SimpleNamespace

from precis.store.types import ChunkInsert
from precis_web.routes.code_view import _workspaces


def _folder(store, design: str | None, slug: str = "ws"):
    ws = {"slug": slug}
    if design is not None:
        ws["design"] = design
    return store.insert_ref(
        kind="folder", slug=None, title=slug, meta={"workspace": ws}
    )


def _file(store, folder_id: int, path: str, text: str):
    ref = store.insert_ref(
        kind="plaintext",
        slug=f"f{folder_id}-{path}",
        title=path,
        parent_id=folder_id,
        meta={"workspace_path": path},
    )
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text, meta={})])
    return ref


def test_workspaces_match_design_spelling_and_part_of(store) -> None:
    design = store.insert_ref(kind="se", slug="foo", title="foo", meta={})
    other = store.insert_ref(kind="se", slug="bar", title="bar", meta={})
    by_kind = _folder(store, "se:foo", "by-kind")
    by_bare = _folder(store, "foo", "by-bare")  # bare spelling is se-only
    by_link = _folder(store, None, "by-link")
    _folder(store, "se:bar", "unrelated")
    unmarked = store.insert_ref(
        kind="folder", slug=None, title="plain", meta={"design": "se:foo"}
    )
    store.add_link(src_ref_id=by_link.id, dst_ref_id=design.id, relation="part-of")
    _file(store, by_kind.id, "a.py", "x = 1\n")
    assert unmarked.id not in {by_kind.id, by_bare.id, by_link.id}

    out = _workspaces(store, design, "se", None)
    assert [w["slug"] for w in out] == ["by-kind", "by-bare", "by-link"]
    assert [f["path"] for f in out[0]["files"]] == ["a.py"]
    assert out[1]["files"] == [] and out[2]["files"] == []

    assert [w["slug"] for w in _workspaces(store, other, "se", None)] == ["unrelated"]


def test_bare_design_spelling_is_ignored_for_non_se_kinds(store) -> None:
    pcb = store.insert_ref(kind="pcb", slug="foo", title="foo", meta={})
    _folder(store, "foo", "bare")
    assert _workspaces(store, pcb, "pcb", None) == []


def test_files_sql_orders_by_path_and_skips_retired(store) -> None:
    design = store.insert_ref(kind="se", slug="foo", title="foo", meta={})
    folder = _folder(store, "se:foo")
    _file(store, folder.id, "b.py", "b = 2\n")
    _file(store, folder.id, "a.py", "a = 1\n")
    gone = _file(store, folder.id, "c.py", "c = 3\n")
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (gone.id,))

    (ws,) = _workspaces(
        store, SimpleNamespace(slug="foo", id=design.id, meta={}), "se", None
    )
    assert [f["path"] for f in ws["files"]] == ["a.py", "b.py"]
