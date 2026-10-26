"""The ``import_workspace`` se op — the MCP door into
:func:`precis_se.workspace_import.import_workspace_run`
(docs/backlog/structure-workspaces.md §Code inside the design): an
``edit(kind='se')`` carrying the run's ``out/`` tree inline imports the
blocks AND syncs the workspace's code folder ``part-of`` the design.
Refusals: unknown design points at ``put(kind='se')``; a design holding
blocks the workspace did not make refuses (``target='fork'`` is the
escape); the op must ride alone; ``put`` refuses it; ``dry_run`` writes
nothing.
"""

from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import MAX_WS_FILES, import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "two_blocks" / "out"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "hexfold"

MAIN_PY = "import precis_geom.out as out\n\nout.block('p')\n"


def _se(store: Store) -> SeHandler:
    return SeHandler(hub=Hub(store=store))


def _design(se: SeHandler, slug: str = "ws-op") -> None:
    se.put(id=slug, text="{}")


def _run_dict(out: Path = FIXTURE) -> dict[str, str]:
    """The fixture ``out/`` tree as the op's ``{relpath: text}``."""
    return {
        p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
        for p in out.rglob("*")
        if p.is_file()
    }


def _op(**kw: Any) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "import_workspace",
        "workspace": "hcol-op",
        "run": _run_dict(),
    }
    op.update(kw)
    return op


def _file_paths(store: Store) -> dict[str, int]:
    """relpath → ref_id for every plaintext child carrying
    ``workspace_path``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'workspace_path' FROM refs "
            "WHERE kind = 'plaintext' AND retired_at IS NULL "
            "AND meta ? 'workspace_path'"
        ).fetchall()
    return {str(rel): int(rid) for rid, rel in rows if rel}


def test_edit_op_imports_run_and_files(store: Store) -> None:
    se = _se(store)
    _design(se)
    resp = se.edit(
        id="ws-op",
        ops=[_op(files={"main.py": MAIN_PY, "lib/util.py": "def u():\n    pass\n"})],
    )
    assert "workspace 'hcol-op' import" in resp.body
    assert "2 block(s) written" in resp.body
    assert "+main.py" in resp.body and "+lib/util.py" in resp.body

    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    tree = persist.load_tree(store, se_ref.id)
    assert {"p", "plate", "pin"} <= set(tree.blocks)
    assert (se_ref.meta or {}).get("workspaces", {}).get("hcol-op")

    # The folder ref + plaintext children, part-of the design.
    assert set(_file_paths(store)) == {"main.py", "lib/util.py"}
    main = store.get_ref(kind="plaintext", id="ws-hcol-op--main-py")
    assert main is not None
    assert (main.meta or {}).get("harvest_orig_path") == "main.py"
    with store.pool.connection() as conn:
        folder = conn.execute(
            "SELECT r.ref_id, r.meta FROM refs r "
            "WHERE r.kind = 'folder' AND r.retired_at IS NULL "
            "AND r.meta->'workspace'->>'slug' = 'hcol-op'"
        ).fetchone()
        assert folder is not None
        links = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (int(folder[0]), int(se_ref.id)),
        ).fetchall()
    assert ("part-of",) in [(str(r[0]),) for r in links]
    child = store.get_ref(kind="plaintext", id="ws-hcol-op--main-py")
    assert child is not None and child.parent_id == int(folder[0])


def test_unknown_design_next_points_at_put(store: Store) -> None:
    se = _se(store)
    with pytest.raises(NotFound) as exc:
        se.edit(id="no-such-design", ops=[_op()])
    assert "put(kind='se'" in str(exc.value.next)


def test_foreign_blocks_refused_and_fork_is_the_escape(store: Store) -> None:
    se = _se(store)
    se.put(id="ws-op", text='{"ops": [{"op": "add_block", "name": "hand_made"}]}')
    with pytest.raises(BadInput) as exc:
        se.edit(id="ws-op", ops=[_op()])
    assert "hand_made" in str(exc.value)
    assert "fork" in str(exc.value)
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert set(persist.load_tree(store, se_ref.id).blocks) == {"hand_made"}

    resp = se.edit(id="ws-op", ops=[_op(target="fork")])
    assert "ws-op--hcol-op" in resp.body
    fork_ref = store.get_ref(kind="se", id="ws-op--hcol-op")
    assert fork_ref is not None
    fork_tree = persist.load_tree(store, fork_ref.id)
    assert {"hand_made", "p", "plate", "pin"} <= set(fork_tree.blocks)


def test_reimport_after_import_is_allowed(store: Store) -> None:
    """The second import on 'design' is legal: every block in the tree is
    this workspace's own (the record + bound ``generated.workspace``)."""
    se = _se(store)
    _design(se)
    se.edit(id="ws-op", ops=[_op()])
    resp = se.edit(id="ws-op", ops=[_op()])
    assert "block(s) written" in resp.body


def test_dry_run_writes_nothing(store: Store) -> None:
    se = _se(store)
    _design(se)
    resp = se.edit(id="ws-op", ops=[_op(dry_run=True, files={"main.py": MAIN_PY})])
    assert "DRY RUN" in resp.body
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}
    assert _file_paths(store) == {}
    assert "workspaces" not in (se_ref.meta or {})


def test_files_py_must_compile(store: Store) -> None:
    """gr517643: a workspace ``.py`` that does not parse refuses the op
    naming file:line — before this, a retyped main.py synced happily
    and the run still reported 'N block(s) written'."""
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput) as exc:
        se.edit(
            id="ws-op",
            ops=[_op(files={"main.py": "def broken(:\n", "ok.py": "x = 1\n"})],
        )
    msg = str(exc.value)
    assert "main.py:1" in msg and "does not compile" in msg
    assert exc.value.next
    # the refusal precedes any write — no folder, no blocks
    assert _file_paths(store) == {}
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_files_py_null_byte_refuses(store: Store) -> None:
    """gr517643: ``compile`` raises ValueError on a null byte (not
    SyntaxError) — a corrupted upload must refuse as BadInput, not
    escape as an unhandled exception."""
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput) as exc:
        se.edit(
            id="ws-op",
            ops=[_op(files={"main.py": "x = 1\n\x00\n", "ok.py": "y = 2\n"})],
        )
    msg = str(exc.value)
    assert "main.py" in msg and "does not compile" in msg
    assert exc.value.next
    assert _file_paths(store) == {}
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_dry_run_reports_sha_verification(store: Store) -> None:
    """gr517643: the dry run shares the import path, so it reports the
    same sha256 verification line."""
    se = _se(store)
    _design(se)
    run = _run_dict()
    m = json.loads(run["manifest.json"])
    for blk in m["blocks"]:
        blk["sha"] = hashlib.sha256(run[blk["file"]].encode("utf-8")).hexdigest()
    run["manifest.json"] = json.dumps(m)
    resp = se.edit(id="ws-op", ops=[_op(run=run, dry_run=True)])
    assert "DRY RUN" in resp.body
    assert "sha256 verified: blocks/p.extxyz" in resp.body


def test_files_sync_update_and_retire(store: Store) -> None:
    se = _se(store)
    _design(se)
    se.edit(
        id="ws-op",
        ops=[_op(files={"main.py": MAIN_PY, "stale.py": "x = 1\n"})],
    )
    se.edit(
        id="ws-op",
        ops=[_op(files={"main.py": MAIN_PY + "# v2\n"})],
    )
    assert set(_file_paths(store)) == {"main.py"}
    stale = store.get_ref(kind="plaintext", id="ws-hcol-op--stale-py")
    assert stale is None
    main = store.get_ref(kind="plaintext", id="ws-hcol-op--main-py")
    assert main is not None
    text = "".join(c.text for c in store.chunks.list_chunks_for_ref(int(main.id)))
    assert text.endswith("# v2\n")


def test_sole_op_rule_and_put_refusal(store: Store) -> None:
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="only op"):
        se.edit(
            id="ws-op",
            ops=[_op(), {"op": "add_block", "name": "x"}],
        )
    with pytest.raises(BadInput, match="edit\\(kind='se'\\) op"):
        se.put(id="ws-put", text=json.dumps({"ops": [_op()]}))


def test_manifest_and_path_vet(store: Store) -> None:
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="manifest.json"):
        se.edit(id="ws-op", ops=[_op(run={"readme.txt": "hi"})])
    with pytest.raises(BadInput, match="relative posix"):
        se.edit(
            id="ws-op",
            ops=[_op(run={"../evil.json": "{}", "manifest.json": "{}"})],
        )


def test_unknown_op_key_refused(store: Store) -> None:
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="unknown key 'runn'"):
        se.edit(id="ws-op", ops=[_op(runn={})])


def test_workspace_slug_traversal_refused(store: Store) -> None:
    """gr517172: the slug lands in ``stage_dir/<slug>`` and ``ws-<slug>--``
    file slugs — ``../../app`` must never reach them. The vet stands at
    the op door AND inside ``import_workspace_run`` (the sandbox/harvest
    path)."""
    se = _se(store)
    _design(se)
    for bad in ("../../app", "../x", "a/b", "Has Caps", "under_score", "-lead"):
        with pytest.raises(BadInput, match="workspace slug"):
            se.edit(id="ws-op", ops=[_op(workspace=bad)])
    with pytest.raises(BadInput, match="workspace slug"):
        import_workspace_run(
            store, workspace="../../app", design="ws-op", out_dir=FIXTURE
        )


def test_second_workspace_into_same_design_refused(store: Store) -> None:
    """A design's blocks all belong to the first workspace that imported
    — a second workspace's import sees them as foreign and refuses."""
    se = _se(store)
    _design(se)
    se.edit(id="ws-op", ops=[_op()])
    with pytest.raises(BadInput, match="did not make"):
        se.edit(id="ws-op", ops=[_op(workspace="other-ws")])
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert "other-ws" not in ((se_ref.meta or {}).get("workspaces") or {})


def test_retire_then_readd_file_restores(store: Store) -> None:
    """files {a,b} → {a} → {a,b}: the retired ``b.py`` ref is restored
    (same ref_id, rewritten chunks), not refused on the slug."""
    se = _se(store)
    _design(se)
    se.edit(id="ws-op", ops=[_op(files={"a.py": "A\n", "b.py": "B\n"})])
    b_id = _file_paths(store)["b.py"]
    se.edit(id="ws-op", ops=[_op(files={"a.py": "A\n"})])
    assert set(_file_paths(store)) == {"a.py"}
    resp = se.edit(id="ws-op", ops=[_op(files={"a.py": "A\n", "b.py": "B2\n"})])
    assert "+b.py" in resp.body
    assert _file_paths(store)["b.py"] == b_id
    text = "".join(c.text for c in store.chunks.list_chunks_for_ref(b_id))
    assert text == "B2\n"


def test_case_folded_slug_collision_is_not_a_restore(store: Store) -> None:
    """``_file_slug`` folds case: a retired ``Main.py`` owns
    ``ws-<ws>--main-py``, so a ``files`` dict asking for ``main.py``
    must NOT restore it — the same slug with a different recorded
    ``workspace_path`` is a refusal, not a revival."""
    se = _se(store)
    _design(se)
    se.edit(id="ws-op", ops=[_op(files={"Main.py": "A\n", "keep.py": "k\n"})])
    se.edit(id="ws-op", ops=[_op(files={"keep.py": "k\n"})])
    with pytest.raises(BadInput, match="taken outside"):
        se.edit(
            id="ws-op",
            ops=[_op(files={"keep.py": "k\n", "main.py": "m = 1\n"})],
        )
    # The refused op wrote nothing — Main.py stays retired, keep.py live.
    assert set(_file_paths(store)) == {"keep.py"}
    # …and the same path IS a restore: re-adding 'Main.py' revives the
    # retired ref itself.
    resp = se.edit(id="ws-op", ops=[_op(files={"keep.py": "k\n", "Main.py": "A\n"})])
    assert "+Main.py" in resp.body


def test_folder_belonging_to_another_design_refused(store: Store) -> None:
    se = _se(store)
    _design(se)
    se.put(id="ws-other", text="{}")
    se.edit(id="ws-op", ops=[_op(files={"main.py": MAIN_PY})])
    with pytest.raises(BadInput, match="already belongs to design"):
        se.edit(id="ws-other", ops=[_op(files={"main.py": MAIN_PY})])


def test_run_map_caps_and_path_edges(store: Store) -> None:
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="relative posix"):
        se.edit(id="ws-op", ops=[_op(run={".": "x", "manifest.json": "{}"})])
    many = {f"f{i}.txt": "x" for i in range(MAX_WS_FILES + 1)}
    many["manifest.json"] = "{}"
    with pytest.raises(BadInput, match="file-count cap"):
        se.edit(id="ws-op", ops=[_op(run=many)])
    with pytest.raises(BadInput, match="nests under"):
        se.edit(
            id="ws-op",
            ops=[_op(run={"manifest.json": "{}", "manifest.json/x": "y"})],
        )


def _run_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Exec ``examples/hexfold/<name>/main.py``'s ``__main__`` path with
    ``out.run``'s scratch root redirected into tmp; return the op's
    ``(run, files)`` — the script's ``out/<name>/`` tree (dot-dirs like
    ``.fitcache`` dropped, as the sandbox harvest does) plus its
    source."""
    import precis_geom.out as geom_out

    monkeypatch.setattr(geom_out, "_SCRATCH_DIR", tmp_path / "scratch")
    script = EXAMPLES / name / "main.py"
    runpy.run_path(str(script), run_name="__main__")
    out_root = tmp_path / "scratch" / "out" / name
    run: dict[str, str] = {}
    for p in out_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(out_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        run[rel.as_posix()] = p.read_text(encoding="utf-8")
    assert "manifest.json" in run
    return run, {"main.py": script.read_text(encoding="utf-8")}


@pytest.mark.parametrize("name", ["prism", "nanobud", "pillar"])
def test_example_hexfold_script_imports_via_the_op(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Each ``examples/hexfold/<name>/main.py`` runs under ``out.run``
    and its ``out/`` imports through the ``import_workspace`` op into a
    fresh design — the batch's end-to-end dogfood."""
    se = _se(store)
    slug = f"ws-ex-{name}"
    se.put(id=slug, text="{}")
    run, files = _run_example(tmp_path, monkeypatch, name)
    resp = se.edit(
        id=slug,
        ops=[
            {
                "op": "import_workspace",
                "workspace": f"hexfold-{name}",
                "run": run,
                "files": files,
            }
        ],
    )
    assert "1 block(s) written" in resp.body
    assert "+main.py" in resp.body
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    assert set(persist.load_tree(store, ref.id).blocks) == {name}
    main = store.get_ref(kind="plaintext", id=f"ws-hexfold-{name}--main-py")
    assert main is not None
