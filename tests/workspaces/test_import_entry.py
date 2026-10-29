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

import errno
import hashlib
import io
import json
import runpy
import tarfile
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.ingest import pdf_upload
from precis.store import Store
from precis_se import persist, workspace_import
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


def test_files_ref_lint_flags_unresolved_handles(store: Store) -> None:
    """gr514049 layer 2: a ``# <handle>`` annotation that does not
    resolve is flagged in the import echo — warn, not refuse."""
    se = _se(store)
    _design(se)
    paper = store.insert_ref(kind="paper", slug="lintp20", title="t", meta={})
    ok = f"pa{paper.id}"
    resp = se.edit(
        id="ws-op",
        ops=[
            _op(
                files={
                    "main.py": f"x = 1  # {ok} (fine)\ny = 2  # pa999999999\n",
                }
            )
        ],
    )
    assert "ref-lint: 1 unresolved db handle(s)" in resp.body
    assert "main.py:2 pa999999999" in resp.body
    assert ok not in resp.body.split("ref-lint:")[-1]


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


# ── The staged door (gr517643 item 2) ──────────────────────────────────
#
# ``{'op': 'workspace_upload_link'}`` mints the single-use POST URL;
# ``import_workspace``'s ``'staged': '<sha256>'`` then resolves the
# ``<ref_id>-<sha>.tar`` the ``/files/upload_tree`` route left under
# ``PRECIS_UPLOAD_STAGING`` — the out/ bytes ride HTTP, never model
# tokens. The tests stage the file the route would write, so the op
# half is covered without the web app.


@pytest.fixture
def staging_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """``PRECIS_UPLOAD_STAGING`` + a link key — what the mint and the
    ``staged`` resolver both read."""
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    monkeypatch.setenv("PRECIS_FILE_LINK_KEY", "ws-upload-test-key")
    return staging


def _tar_bytes(tree: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text in tree.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _stage_tree(store: Store, staging: Path, design: str, data: bytes) -> str:
    """Write what ``POST /files/upload_tree`` leaves behind; return the
    sha256 it answers."""
    sha = hashlib.sha256(data).hexdigest()
    ref = store.get_ref(kind="se", id=design)
    assert ref is not None
    (staging / pdf_upload.staged_tree_name(int(ref.id), sha)).write_bytes(data)
    return sha


def _staged_op(**kw: Any) -> dict[str, Any]:
    op: dict[str, Any] = {"op": "import_workspace", "workspace": "hcol-op"}
    op.update(kw)
    return op


@pytest.mark.parametrize("component", ["a" * 300, "é" * 128], ids=["ascii", "utf8"])
def test_staged_long_path_component_refused(tmp_path: Path, component: str) -> None:
    """The component cap counts UTF-8 bytes, including parent directories."""
    tar_path = tmp_path / "run.tar"
    tar_path.write_bytes(_tar_bytes({f"{component}/file.txt": "x"}))
    out_dir = tmp_path / "out"
    with pytest.raises(BadInput) as exc:
        workspace_import._extract_staged_tree(tar_path, out_dir)
    assert "255" in str(exc.value.next)
    assert not out_dir.exists()


@pytest.mark.parametrize(
    ("cap", "members", "message"),
    [
        ("MAX_WS_FILE_BYTES", [("a", "xx")], "file cap"),
        ("MAX_WS_OUT_BYTES", [("a", "x"), ("b", "x")], "totals over"),
        (None, [("a", "x"), ("a", "x")], "twice"),
        ("MAX_WS_FILES", [("a", "x"), ("b", "x")], "file-count cap"),
    ],
    ids=["file-size", "total-size", "duplicate-name", "file-count"],
)
def test_staged_limit_refusals_carry_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cap: str | None,
    members: list[tuple[str, str]],
    message: str,
) -> None:
    """Each bounded-tree refusal must tell the caller how to fix the tar."""
    if cap is not None:
        monkeypatch.setattr(workspace_import, cap, 1)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text in members:
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    tar_path = tmp_path / "run.tar"
    tar_path.write_bytes(buf.getvalue())
    with pytest.raises(BadInput, match=message) as exc:
        workspace_import._extract_staged_tree(tar_path, tmp_path / "out")
    assert exc.value.next


@pytest.mark.parametrize("method", ["mkdir", "write_bytes"])
@pytest.mark.parametrize("error", [errno.ENAMETOOLONG, errno.EACCES])
def test_staged_filesystem_refusals_carry_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, error: int
) -> None:
    """Full-path limits and other write failures must not escape as OSError."""
    tar_path = tmp_path / "run.tar"
    tar_path.write_bytes(_tar_bytes({"blocks/file.txt": "x"}))
    out_dir = tmp_path / "out"
    original = getattr(Path, method)

    def refuse_destination(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.is_relative_to(out_dir):
            raise OSError(error, "filesystem refused destination", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, refuse_destination)
    with pytest.raises(BadInput) as exc:
        workspace_import._extract_staged_tree(tar_path, out_dir)
    assert exc.value.next
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == error


def test_staged_tar_imports(store: Store, staging_dir: Path) -> None:
    """The staged door imports the same out/ tree the inline map
    carries — same blocks, same verified-sha echo."""
    se = _se(store)
    _design(se)
    run = _run_dict()
    m = json.loads(run["manifest.json"])
    for blk in m["blocks"]:
        blk["sha"] = hashlib.sha256(run[blk["file"]].encode("utf-8")).hexdigest()
    run["manifest.json"] = json.dumps(m)
    sha = _stage_tree(store, staging_dir, "ws-op", _tar_bytes(run))
    resp = se.edit(
        id="ws-op",
        ops=[_staged_op(staged=sha, files={"main.py": MAIN_PY})],
    )
    assert "2 block(s) written" in resp.body
    assert "sha256 verified:" in resp.body
    assert "+main.py" in resp.body
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert {"p", "plate", "pin"} <= set(persist.load_tree(store, se_ref.id).blocks)


def test_staged_dry_run_writes_nothing(store: Store, staging_dir: Path) -> None:
    se = _se(store)
    _design(se)
    sha = _stage_tree(store, staging_dir, "ws-op", _tar_bytes(_run_dict()))
    resp = se.edit(id="ws-op", ops=[_staged_op(staged=sha, dry_run=True)])
    assert "DRY RUN" in resp.body
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_staged_and_run_are_exclusive(store: Store, staging_dir: Path) -> None:
    se = _se(store)
    _design(se)
    sha = _stage_tree(store, staging_dir, "ws-op", _tar_bytes(_run_dict()))
    with pytest.raises(BadInput, match="not both"):
        se.edit(id="ws-op", ops=[_op(staged=sha)])


def test_staged_bad_sha_refused(store: Store, staging_dir: Path) -> None:
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="must be the sha256") as exc:
        se.edit(id="ws-op", ops=[_staged_op(staged="not-a-sha")])
    assert "workspace_upload_link" in str(exc.value.next)


def test_staged_unknown_sha_refused(store: Store, staging_dir: Path) -> None:
    """A well-formed sha that names nothing staged under THIS design
    refuses toward the mint door."""
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="no staged tree") as exc:
        se.edit(id="ws-op", ops=[_staged_op(staged="ab" * 32)])
    assert "workspace_upload_link" in str(exc.value.next)


def test_staged_tree_bound_to_its_design(store: Store, staging_dir: Path) -> None:
    """The ref in the staged name binds it: a tar staged for design A
    can't feed design B's import."""
    se = _se(store)
    _design(se)
    se.put(id="ws-other", text="{}")
    data = _tar_bytes(_run_dict())
    sha = hashlib.sha256(data).hexdigest()
    other = store.get_ref(kind="se", id="ws-other")
    assert other is not None
    (staging_dir / pdf_upload.staged_tree_name(int(other.id), sha)).write_bytes(data)
    with pytest.raises(BadInput, match="no staged tree"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])


def test_staged_no_manifest_refused(store: Store, staging_dir: Path) -> None:
    se = _se(store)
    _design(se)
    sha = _stage_tree(store, staging_dir, "ws-op", _tar_bytes({"readme.txt": "hi"}))
    with pytest.raises(BadInput, match="no manifest.json"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])


def test_staged_not_a_tar_refused(store: Store, staging_dir: Path) -> None:
    """The route's ustar gate is the format check, but a hand-placed
    staging can hold anything — the attach must still refuse."""
    se = _se(store)
    _design(se)
    sha = _stage_tree(store, staging_dir, "ws-op", b"definitely not a tar")
    with pytest.raises(BadInput, match="not a readable tar"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])


def test_staged_traversal_member_refused(store: Store, staging_dir: Path) -> None:
    """A ``../`` member must never reach ``out_dir`` — the extraction
    vet refuses it like the inline map's path vet."""
    se = _se(store)
    _design(se)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("manifest.json", "../evil.txt"):
            data = b"{}"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    sha = _stage_tree(store, staging_dir, "ws-op", buf.getvalue())
    with pytest.raises(BadInput, match="not a clean relative path"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])
    se_ref = store.get_ref(kind="se", id="ws-op")
    assert se_ref is not None
    assert persist.load_tree(store, se_ref.id).blocks == {}


def test_staged_link_member_refused(store: Store, staging_dir: Path) -> None:
    """Links, devices and fifos refuse — only regular files extract."""
    se = _se(store)
    _design(se)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("manifest.json")
        data = b"{}"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("blocks/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    sha = _stage_tree(store, staging_dir, "ws-op", buf.getvalue())
    with pytest.raises(BadInput, match="not a regular file"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])


def test_staged_file_dir_nesting_refused_in_both_orders(
    store: Store, staging_dir: Path
) -> None:
    """The file/dir nesting vet runs in both tar orders: 'a/b' before
    'a' used to die on a raw IsADirectoryError at write time — both
    orders now refuse BadInput like the inline map's vet."""
    se = _se(store)
    _design(se)
    for members in (("d/f", "d"), ("d", "d/f")):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name in members:
                data = b"x"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        sha = _stage_tree(store, staging_dir, "ws-op", buf.getvalue())
        with pytest.raises(BadInput, match="both file and directory") as exc:
            se.edit(id="ws-op", ops=[_staged_op(staged=sha)])
        assert exc.value.next


def test_staged_non_utf8_manifest_and_trace_refused(
    store: Store, staging_dir: Path
) -> None:
    """The tar carries raw bytes, so a non-UTF-8 manifest.json or
    trace.json reaches the import's ``read_text`` — it must map to
    BadInput, not escape as an unhandled UnicodeDecodeError."""
    se = _se(store)
    _design(se)
    good_manifest = b'{"schema": 1, "blocks": []}'
    for members in (
        (("manifest.json", b"\xff\xfe not utf-8"),),
        (("manifest.json", good_manifest), ("trace.json", b"\xff\xfe bad")),
    ):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        sha = _stage_tree(store, staging_dir, "ws-op", buf.getvalue())
        with pytest.raises(BadInput, match="UTF-8") as exc:
            se.edit(id="ws-op", ops=[_staged_op(staged=sha)])
        assert "UTF-8" in str(exc.value.next)


def test_staged_dir_member_flood_refused(store: Store, staging_dir: Path) -> None:
    """Directory members are implicit in each file's parent mkdir, but a
    tar is free to carry thousands of bare dir headers — they count
    against a MAX_WS_FILES * 4 cap."""
    se = _se(store)
    _design(se)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b'{"schema": 1, "blocks": []}'
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        for i in range(MAX_WS_FILES * 4 + 1):
            d = tarfile.TarInfo(f"d{i}/")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
    sha = _stage_tree(store, staging_dir, "ws-op", buf.getvalue())
    with pytest.raises(BadInput, match="directory-member cap"):
        se.edit(id="ws-op", ops=[_staged_op(staged=sha)])


def test_staged_unconfigured_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``PRECIS_UPLOAD_STAGING`` — the honest Unsupported naming the
    var, like every other upload door."""
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    se = _se(store)
    _design(se)
    with pytest.raises(Unsupported, match="PRECIS_UPLOAD_STAGING"):
        se.edit(id="ws-op", ops=[_staged_op(staged="ab" * 32)])


def test_upload_link_mints_the_url(store: Store, staging_dir: Path) -> None:
    se = _se(store)
    _design(se)
    resp = se.edit(id="ws-op", ops=[{"op": "workspace_upload_link"}])
    ref = store.get_ref(kind="se", id="ws-op")
    assert ref is not None
    assert f"/files/upload_tree/{int(ref.id)}?t=v1.put.upload_tree." in resp.body
    assert "single use" in resp.body
    assert "'staged': '<sha256>'" in resp.body


def test_upload_link_unconfigured_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    se = _se(store)
    _design(se)
    with pytest.raises(Unsupported, match="PRECIS_UPLOAD_STAGING"):
        se.edit(id="ws-op", ops=[{"op": "workspace_upload_link"}])


def test_upload_link_op_vet(store: Store, staging_dir: Path) -> None:
    """Sole op of its call, edit-only, unknown keys named."""
    se = _se(store)
    _design(se)
    with pytest.raises(BadInput, match="only op"):
        se.edit(
            id="ws-op",
            ops=[{"op": "workspace_upload_link"}, {"op": "add_block", "name": "x"}],
        )
    with pytest.raises(BadInput, match="unknown key 'bogus'"):
        se.edit(id="ws-op", ops=[{"op": "workspace_upload_link", "bogus": 1}])
    with pytest.raises(BadInput, match="edit\\(kind='se'\\) op"):
        se.put(
            id="ws-put-link",
            text=json.dumps({"ops": [{"op": "workspace_upload_link"}]}),
        )
