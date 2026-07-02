"""Real-PG regression tests for the /drive route's raw SQL (ADR 0045).

The FakeStore suite doesn't parse SQL, so the folder-tree / children /
unfiled / breadcrumb queries are exercised here against the live
``store`` fixture — same posture as ``test_structure_sql.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis.dispatch import Hub
from precis.handlers.folder import FolderHandler
from precis_web.routes.drive import (
    _breadcrumb,
    _children,
    _flatten_tree,
    _folder_tree,
    _unfiled,
)
from tests.conftest import id_of


@pytest.fixture
def seeded(store):
    """Projects/Hardware nesting + one cad design inside, one unfiled."""
    folder = FolderHandler(hub=Hub(store=store))
    top = id_of(folder.put(text="Projects").body)
    sub = id_of(folder.put(text="Hardware").body)
    folder.link(id=sub, target=f"folder:{top}", rel="parent")
    store.insert_ref(kind="cad", slug="bracket", title="a bracket", meta={})
    store.insert_ref(kind="cad", slug="loose", title="a loose part", meta={})
    from precis.handlers.cad import CadHandler

    CadHandler(hub=Hub(store=store)).link(
        id="bracket", target=f"folder:{sub}", rel="parent"
    )
    return {"top": top, "sub": sub}


def test_folder_tree_nests(store, seeded):
    roots = _folder_tree(store)
    assert [r["title"] for r in roots] == ["Projects"]
    assert [c["title"] for c in roots[0]["children"]] == ["Hardware"]
    flat = _flatten_tree(roots)
    assert [(f["title"], f["depth"]) for f in flat] == [
        ("Projects", 0),
        ("Hardware", 1),
    ]
    # child counts: Projects holds Hardware; Hardware holds the cad ref
    assert flat[0]["n_children"] == 1
    assert flat[1]["n_children"] == 1


def test_children_rows_carry_slug_and_reader_fields(store, seeded):
    rows = _children(store, seeded["sub"])
    assert len(rows) == 1
    (row,) = rows
    assert row["kind"] == "cad"
    assert row["ident"] == "bracket"
    assert row["handler_id"] == "bracket"


def test_unfiled_lists_only_parentless_artifacts(store, seeded):
    rows = _unfiled(store, ["draft", "structure", "cad", "todo"])
    idents = [r["ident"] for r in rows]
    assert "loose" in idents
    assert "bracket" not in idents  # filed → not unfiled


def test_breadcrumb_walks_up(store, seeded):
    crumbs = _breadcrumb(store, seeded["sub"])
    assert [c["title"] for c in crumbs] == ["Projects", "Hardware"]
