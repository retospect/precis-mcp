"""Structure-workspaces slice 2p acceptance — the python-kind join
(docs/backlog/structure-workspaces.md §Join with the python kind):

- ``get(kind='python', id='ws-hcol::main.bundle', view='placed')``
  lists the design, 3 occurrences and counts;
- ``frame:qual=main.bundle`` resolves to the union of the three
  ``main.bundle`` frames' atoms;
- an unrun workspace has no root;
- ``extent='fisheye+1hop'`` on that id shows ``Placed:`` with the same
  numbers and the selector, and on ``main::…`` shows none;
- the block eye's ``made-by:`` id resolves with ``get(kind='python')``;
- user-frame trace lines carry the ``ws-<slug>::`` python id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._python_ws import WorkspaceRoots
from precis.handlers.python import PythonHandler
from precis.store import Store
from precis.store.types import ChunkInsert
from precis_se import datums as se_datums
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "py_join" / "out"

#: frame keys of the three ``main.bundle`` occurrences in the fixture.
BUNDLE_KEYS = ("bbbb000000000002", "bbbb000000000004", "bbbb000000000006")

MAIN_PY = (
    "def build(n):\n"
    "    for i in range(n):\n"
    "        bundle(i)\n"
    "\n"
    "\n"
    "def bundle(i):\n"
    "    cell(i)\n"
    "    out.block('p')\n"
    "\n"
    "\n"
    "def cell(i):\n"
    "    return i\n"
)


def _design(store: Store, slug: str = "hcol-array-1") -> int:
    ref = store.insert_ref(kind="se", slug=slug, title="ws join", meta={})
    return int(ref.id)


def _job(store: Store) -> int:
    ref = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    return int(ref.id)


def _folder(store: Store, slug: str = "hcol", design: str = "hcol-array-1") -> None:
    """The workspace's code folder as slice 1 will mint it: a ``folder``
    ref (numeric) with ``meta.workspace``, ``plaintext`` children
    carrying ``harvest_orig_path`` + chunk bodies."""
    folder = store.insert_ref(
        kind="folder",
        slug=None,
        title=f"workspace {slug}",
        meta={
            "workspace": {
                "slug": slug,
                "design": design,
                "created_by": "test",
            }
        },
    )
    child = store.insert_ref(
        kind="plaintext",
        slug=f"ws-{slug}--main-py",
        title="main.py",
        parent_id=int(folder.id),
        meta={"harvest_orig_path": "main.py"},
    )
    store.chunks.insert_chunks(int(child.id), [ChunkInsert(ord=0, text=MAIN_PY)])


def _import(store: Store, design: str = "hcol-array-1") -> int:
    job = _job(store)
    res = import_workspace_run(
        store,
        workspace="hcol",
        design=design,
        out_dir=FIXTURE,
        run_job=job,
        script_sha="deadbee5",
    )
    assert res.trace is not None
    return job


def _py(store: Store, tmp_path: Path, roots: dict | None = None) -> PythonHandler:
    return PythonHandler(
        hub=Hub(store=store),
        roots=roots or {},
        workspaces=WorkspaceRoots(store, stage_dir=tmp_path / "wsroots"),
    )


@pytest.fixture
def built(store: Store, tmp_path: Path) -> PythonHandler:
    _design(store)
    _folder(store)
    _import(store)
    return _py(store, tmp_path)


def test_placed_view_lists_design_occurrences_counts(
    built: PythonHandler,
) -> None:
    body = built.get(id="ws-hcol::main.bundle", view="placed").body
    assert "hcol-array-1" in body
    assert "3 occurrences" in body
    # 6, not 14: the fixture's collapsed frame (id 7..id_end 11) counts
    # its made once, not once per covered id.
    assert "made 6 atoms" in body
    assert "frame:qual=main.bundle" in body


def test_frame_qual_selector_is_the_union(store: Store, built: PythonHandler) -> None:
    ref = store.get_ref(kind="se", id="hcol-array-1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    union: set[int] = set()
    for key in BUNDLE_KEYS:
        rdat = se_datums.resolve(tree, "p", f"frame:{key}")
        assert rdat.error is None
        union |= set(rdat.atoms)
    rdat = se_datums.resolve(tree, "p", "frame:qual=main.bundle")
    assert rdat.error is None
    assert set(rdat.atoms) == union == {0, 1, 2, 3, 4, 5}


def test_unrun_workspace_has_no_root(store: Store, tmp_path: Path) -> None:
    _folder(store)  # folder exists, but no import ever ran
    py = _py(store, tmp_path)
    with pytest.raises(NotFound, match="no run"):
        py.get(id="ws-hcol::main.bundle")


def test_unknown_workspace_alias(store: Store, tmp_path: Path) -> None:
    py = _py(store, tmp_path)
    with pytest.raises(NotFound, match="unknown workspace root"):
        py.get(id="ws-nope::main.bundle")


def test_placed_on_a_static_root_refuses(
    store: Store, tmp_path: Path, built: PythonHandler
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "foo.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    py = PythonHandler(
        hub=Hub(store=store),
        roots={"main": repo},
        workspaces=WorkspaceRoots(store, stage_dir=tmp_path / "wsroots"),
    )
    with pytest.raises(BadInput, match="ws-<slug>"):
        py.get(id="main::foo", view="placed")


def test_eye_shows_placed_on_ws_root(
    store: Store, tmp_path: Path, built: PythonHandler
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    py = PythonHandler(
        hub=Hub(store=store),
        roots={"main": repo},
        workspaces=WorkspaceRoots(store, stage_dir=tmp_path / "wsroots"),
    )
    body = py.eye(id="ws-hcol::main.bundle", extent="fisheye+1hop").body
    assert "Placed:" in body
    assert "3 occurrences" in body
    assert "made 6 atoms" in body  # the same numbers as view='placed'
    assert "frame:qual=main.bundle" in body
    body = py.eye(id="main::foo", extent="fisheye+1hop").body
    assert "Placed:" not in body


def test_block_view_made_by_resolves(store: Store, built: PythonHandler) -> None:
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="hcol-array-1", view="block", args={"block": "p"}).body
    made_by = next(l for l in body.splitlines() if l.startswith("made-by:"))
    assert "ws-hcol::main.bundle" in made_by
    assert "main.py:8" in made_by
    assert "out.block" in made_by
    resp = built.get(id="ws-hcol::main.bundle")
    assert "main.bundle" in resp.body


def test_trace_lines_carry_python_id(store: Store, built: PythonHandler) -> None:
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="hcol-array-1", view="trace", args={"atom": "p/0"}).body
    # Both user frames in the chain (main.build, main.bundle) carry the
    # id; the core leaf (sheetnet.cell) carries none.
    assert "main.py:3 main.build(n=3) · ws-hcol::main.build" in body
    assert "main.py:8 main.bundle · ws-hcol::main.bundle" in body
    assert body.count("· ws-hcol::") == 2


def test_placed_refuses_on_module(store: Store, built: PythonHandler) -> None:
    with pytest.raises(BadInput, match="not module"):
        built.get(id="ws-hcol::main", view="placed")


def test_placed_refuses_on_unknown_symbol(store: Store, built: PythonHandler) -> None:
    with pytest.raises(NotFound, match="not found"):
        built.get(id="ws-hcol::main.nosuch", view="placed")


def test_placed_run_pin(store: Store, tmp_path: Path) -> None:
    _design(store)
    _folder(store)
    job = _import(store)
    py = _py(store, tmp_path)
    body = py.get(id="ws-hcol::main.bundle", view="placed", args={"run": job}).body
    assert "3 occurrences" in body and f"run {job}" in body
    with pytest.raises(BadInput, match="run"):
        py.get(id="ws-hcol::main.bundle", view="placed", args={"run": "abc"})
    ref = store.get_ref(kind="se", id="hcol-array-1")
    assert ref is not None
    with pytest.raises(BadInput, match="not a job ref"):
        py.get(
            id="ws-hcol::main.bundle",
            view="placed",
            args={"run": int(ref.id)},
        )


def test_se_handler_pocket_over_frame_qual(store: Store, built: PythonHandler) -> None:
    """The se handler path: ``frame:qual=`` resolves inside a pocket
    region, not only via ``se_datums.resolve`` directly."""
    from precis_se.ops import apply_ops

    ref = store.get_ref(kind="se", id="hcol-array-1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "p",
                "name": "qual_pocket",
                "regions": [{"selector": "frame:qual=main.bundle"}],
            }
        ],
    )
    persist.save_tree(store, ref_id=ref.id, tree=tree, card_text="qual pocket")
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="hcol-array-1", view="pockets").body
    assert "qual_pocket" in body
    assert "frame:qual=main.bundle" in body
    tree = persist.load_tree(store, ref.id)
    rdat = se_datums.resolve(tree, "p", "frame:qual=main.bundle")
    assert rdat.error is None
    assert set(rdat.atoms) == {0, 1, 2, 3, 4, 5}
