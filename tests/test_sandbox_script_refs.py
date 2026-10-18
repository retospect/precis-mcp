"""sandbox_run ``mode:script`` — ``code_ref`` resolution and ``inputs``
staging (``_sandbox_inputs``) plus the executor wiring in
``claude_docker._launch_script``.

Refs are real rows in the RAM test DB with files under a tmp
``PRECIS_ROOT``; the podman stub stands in for the host.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.plaintext import PlaintextHandler
from precis.store import Store
from precis.store.types import Tag
from precis.structure import export
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis.workers.executors import _sandbox_harvest, _sandbox_inputs, claude_docker
from precis.workers.executors._sandbox_inputs import ScriptRefError
from precis.workers.job_types import sandbox_run

# ── fixtures / helpers ─────────────────────────────────────────────

_PODMAN_STUB = """#!/usr/bin/env python3
import json, os, sys
d = os.environ["SANDBOX_STUB_DIR"]
args = sys.argv[1:]
cmd = args[0] if args else ""
def sf(name, ext="state"): return os.path.join(d, name + "." + ext)
if cmd == "run":
    name = args[args.index("--name") + 1]
    json.dump(args, open(sf(name, "argv.json"), "w"))
    with open(sf(name), "w") as f:
        f.write("running")
    print("ctr-" + name)
    sys.exit(0)
if cmd == "inspect":
    p = sf(args[-1])
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p).read().strip() + "\\n")
    sys.exit(0)
sys.exit(0)
"""

_PY_BODY = "print('hello from a ref')\n"
_TWO_BLOCKS = "# Notes\n\n```python\nA = 1\n```\n\ntext\n\n```python\nB = 2\n```\n"


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "PRECIS_ROOT"
    r.mkdir()
    return r


@pytest.fixture
def hub_(store: Store) -> Hub:
    return Hub(store=store, embedder=None)


def _plaintext(root: Path, hub: Hub, name: str, body: str) -> str:
    """Write ``root/<name>.txt`` and ingest it; returns the ref target."""
    (root / f"{name}.txt").write_text(body, encoding="utf-8")
    ref = PlaintextHandler(hub=hub, root=root).ensure_ingested(name)
    assert ref is not None
    return f"plaintext:{name}"


def _markdown(root: Path, hub: Hub, name: str, body: str) -> str:
    (root / f"{name}.md").write_text(body, encoding="utf-8")
    ref = MarkdownHandler(hub=hub, root=root).ensure_ingested(name)
    assert ref is not None
    return f"markdown:{name}"


def _structure(store: Store, slug: str = "pt-test") -> str:
    scene = Scene(
        cell=Cell.from_lengths_angles(10, 10, 10, pbc=(True, True, True)),
        atoms={"aC1": Atom("aC1", "C", np.zeros(3))},
    )
    store.structure_save(
        slug=slug, title="t", scene=scene, version=1, card_text="test structure"
    )
    return f"structure:{slug}"


def _folder(store: Store, root: Path, tmp_path: Path, files: list[str]) -> str:
    """A harvested folder ref holding ``files`` (as a sandbox run leaves it)."""
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="j",
        meta={"executor": "claude_docker", "job_type": "sandbox_run"},
    )
    work = tmp_path / "hwork"
    (work / "out").mkdir(parents=True)
    for f in files:
        (work / "out" / f).write_text(_PY_BODY, encoding="utf-8")
    res = _sandbox_harvest.harvest_out(
        store,
        job_ref_id=int(job.id),
        container_name="sandbox-h",
        work_dir=work,
        image="code-task:abc",
        model="m",
        root=root,
        artifact_store_root=tmp_path / "artifacts",
    )
    assert res.folder_ref_id is not None
    return f"folder:{res.folder_ref_id}"


def _resolve(store: Store, root: Path, target: str, **kw: Any) -> Any:
    return _sandbox_inputs.resolve_code_ref(store, target, root=root, **kw)


# ── 1. pure block extraction ───────────────────────────────────────


class TestExtractPythonBlock:
    def test_single_block_returns_its_body(self) -> None:
        body = "intro\n```python\nx = 1\ny = 2\n```\noutro\n"
        out = _sandbox_inputs.extract_python_block(body, block=None, label="md:a")
        assert out == "x = 1\ny = 2\n"

    def test_zero_blocks_refuses_with_next(self) -> None:
        with pytest.raises(ScriptRefError, match="no ```python") as ei:
            _sandbox_inputs.extract_python_block(
                "just prose\n", block=None, label="md:a"
            )
        assert "next:" in str(ei.value)

    def test_two_blocks_without_block_names_the_arg(self) -> None:
        with pytest.raises(ScriptRefError) as ei:
            _sandbox_inputs.extract_python_block(_TWO_BLOCKS, block=None, label="md:a")
        assert "args={'block': N}" in str(ei.value) and "next:" in str(ei.value)

    def test_block_two_picks_the_second(self) -> None:
        out = _sandbox_inputs.extract_python_block(_TWO_BLOCKS, block=2, label="md:a")
        assert out == "B = 2\n"

    def test_block_past_the_end_refuses(self) -> None:
        with pytest.raises(ScriptRefError) as ei:
            _sandbox_inputs.extract_python_block(_TWO_BLOCKS, block=3, label="md:a")
        assert "block 3 does not exist" in str(ei.value)
        assert "next:" in str(ei.value)

    @pytest.mark.parametrize("lang", ["py", "text", "bash"])
    def test_other_fences_are_not_counted(self, lang: str) -> None:
        body = f"```{lang}\nnot python\n```\n\n```python\nreal = 1\n```\n"
        out = _sandbox_inputs.extract_python_block(body, block=None, label="md:a")
        assert out == "real = 1\n"

    def test_only_other_fences_counts_as_zero(self) -> None:
        with pytest.raises(ScriptRefError, match="no ```python"):
            _sandbox_inputs.extract_python_block(
                "```py\nx = 1\n```\n", block=None, label="md:a"
            )


# ── 2-5. resolve_code_ref ──────────────────────────────────────────


@pytest.mark.db
class TestResolveCodeRef:
    def test_plaintext_is_the_whole_body(
        self, store: Store, root: Path, hub_: Hub
    ) -> None:
        t = _plaintext(root, hub_, "scr", _PY_BODY)
        got = _resolve(store, root, t)
        assert got.entry == "main.py"
        assert got.source == _PY_BODY
        assert got.folder_id is None

    def test_markdown_single_block(self, store: Store, root: Path, hub_: Hub) -> None:
        t = _markdown(root, hub_, "one", "# T\n\n```python\nz = 3\n```\n")
        got = _resolve(store, root, t)
        assert got.entry == "main.py" and got.source == "z = 3\n"

    def test_markdown_two_blocks_refuses_naming_block(
        self, store: Store, root: Path, hub_: Hub
    ) -> None:
        t = _markdown(root, hub_, "two", _TWO_BLOCKS)
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, t)
        assert "args={'block': N}" in str(ei.value) and "next:" in str(ei.value)

    def test_markdown_block_two(self, store: Store, root: Path, hub_: Hub) -> None:
        t = _markdown(root, hub_, "two", _TWO_BLOCKS)
        assert _resolve(store, root, t, block=2).source == "B = 2\n"

    def test_folder_with_main_py(
        self, store: Store, root: Path, tmp_path: Path
    ) -> None:
        t = _folder(store, root, tmp_path, ["main.py", "util.py"])
        got = _resolve(store, root, t)
        assert got.entry == "code/main.py"
        assert got.source is None
        assert got.folder_id == int(t.split(":")[1])

    def test_folder_without_main_py_names_entry(
        self, store: Store, root: Path, tmp_path: Path
    ) -> None:
        t = _folder(store, root, tmp_path, ["run.py"])
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, t)
        assert "args={'entry': '<file>'}" in str(ei.value)
        assert "next:" in str(ei.value)

    def test_folder_with_entry(self, store: Store, root: Path, tmp_path: Path) -> None:
        t = _folder(store, root, tmp_path, ["run.py"])
        got = _resolve(store, root, t, entry="run.py")
        assert got.entry == "code/run.py"
        assert got.folder_id is not None

    def test_folder_entry_that_is_absent_refuses(
        self, store: Store, root: Path, tmp_path: Path
    ) -> None:
        t = _folder(store, root, tmp_path, ["run.py"])
        with pytest.raises(ScriptRefError, match="has no other.py") as ei:
            _resolve(store, root, t, entry="other.py")
        assert "next:" in str(ei.value)

    @pytest.mark.parametrize("kind", ["todo", "job"])
    def test_disallowed_kind_refuses(self, store: Store, root: Path, kind: str) -> None:
        ref = store.insert_ref(kind=kind, slug=None, title="x", meta={})
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, f"{kind}:{ref.id}")
        assert "next:" in str(ei.value)

    def test_unknown_ref_refuses(self, store: Store, root: Path) -> None:
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, "plaintext:no-such-file")
        assert "next:" in str(ei.value)

    def test_block_on_plaintext_refuses(
        self, store: Store, root: Path, hub_: Hub
    ) -> None:
        t = _plaintext(root, hub_, "scr", _PY_BODY)
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, t, block=1)
        assert "params.block" in str(ei.value) and "next:" in str(ei.value)

    def test_entry_on_markdown_refuses(
        self, store: Store, root: Path, hub_: Hub
    ) -> None:
        t = _markdown(root, hub_, "one", "```python\nz = 3\n```\n")
        with pytest.raises(ScriptRefError) as ei:
            _resolve(store, root, t, entry="run.py")
        assert "params.entry" in str(ei.value) and "next:" in str(ei.value)


# ── 6. stage_inputs ────────────────────────────────────────────────


@pytest.mark.db
class TestStageInputs:
    def test_plaintext_copies_its_file(
        self, store: Store, root: Path, hub_: Hub, tmp_path: Path
    ) -> None:
        t = _plaintext(root, hub_, "data", "a,b\n1,2\n")
        dest = tmp_path / "in"
        manifest = _sandbox_inputs.stage_inputs(store, [t], dest, root=root)
        assert (dest / "data.txt").read_text(encoding="utf-8") == "a,b\n1,2\n"
        assert manifest == {t: ["data.txt"]}

    def test_structure_writes_extxyz_and_cif_when_ase(
        self, store: Store, tmp_path: Path
    ) -> None:
        t = _structure(store)
        dest = tmp_path / "in"
        manifest = _sandbox_inputs.stage_inputs(store, [t], dest)
        (paths,) = manifest.values()
        extxyz = [p for p in paths if p.endswith(".extxyz")]
        assert len(extxyz) == 1
        handle = extxyz[0][: -len(".extxyz")]
        assert (dest / extxyz[0]).read_text(encoding="utf-8").strip()
        if export.ase_available():
            assert f"{handle}.cif" in paths
            assert (dest / f"{handle}.cif").is_file()
        else:
            assert paths == [f"{handle}.extxyz"]

    def test_manifest_json_maps_each_target(
        self, store: Store, root: Path, hub_: Hub, tmp_path: Path
    ) -> None:
        p = _plaintext(root, hub_, "data", "x\n")
        s = _structure(store)
        dest = tmp_path / "in"
        manifest = _sandbox_inputs.stage_inputs(store, [p, s], dest, root=root)
        on_disk = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        assert on_disk == manifest
        assert set(on_disk) == {p, s}
        for paths in on_disk.values():
            assert paths and all((dest / x).exists() for x in paths)

    def test_unknown_ref_refuses(self, store: Store, tmp_path: Path) -> None:
        with pytest.raises(ScriptRefError) as ei:
            _sandbox_inputs.stage_inputs(store, ["structure:99999999"], tmp_path / "in")
        assert "next:" in str(ei.value)

    def test_disallowed_kind_refuses(self, store: Store, tmp_path: Path) -> None:
        ref = store.insert_ref(kind="todo", slug=None, title="x", meta={})
        with pytest.raises(ScriptRefError) as ei:
            _sandbox_inputs.stage_inputs(store, [f"todo:{ref.id}"], tmp_path / "in")
        assert "next:" in str(ei.value)


# ── 7. submit gate with the store ──────────────────────────────────


def _script_params(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "mode": "script",
        "target_node": "balthazar",
        "resources": {"wall_seconds": 600},
    }
    p.update(over)
    return p


@pytest.fixture
def submit_env(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("PRECIS_ROOT", str(root))
    return root


@pytest.mark.db
class TestValidateSubmitWithStore:
    def test_two_block_markdown_refuses(
        self, store: Store, hub_: Hub, submit_env: Path
    ) -> None:
        t = _markdown(submit_env, hub_, "two", _TWO_BLOCKS)
        err = sandbox_run.validate_submit(store, params=_script_params(code_ref=t))
        assert err is not None and "args={'block'" in err and "next:" in err

    def test_block_two_passes(self, store: Store, hub_: Hub, submit_env: Path) -> None:
        t = _markdown(submit_env, hub_, "two", _TWO_BLOCKS)
        params = _script_params(code_ref=t, block=2)
        assert sandbox_run.validate_submit(store, params=params) is None

    def test_valid_plaintext_code_ref_passes(
        self, store: Store, hub_: Hub, submit_env: Path
    ) -> None:
        t = _plaintext(submit_env, hub_, "scr", _PY_BODY)
        params = _script_params(code_ref=t)
        assert sandbox_run.validate_submit(store, params=params) is None

    def test_unresolvable_input_refuses(
        self, store: Store, hub_: Hub, submit_env: Path
    ) -> None:
        t = _plaintext(submit_env, hub_, "scr", _PY_BODY)
        err = sandbox_run.validate_submit(
            store,
            params=_script_params(code_ref=t, inputs=["structure:99999999"]),
        )
        assert err is not None and "next:" in err


# ── 8. executor end-to-end ─────────────────────────────────────────


@pytest.fixture
def exec_env(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SANDBOX_STUB_DIR", str(state))
    monkeypatch.setenv(
        "PRECIS_PODMAN_BIN", str(_write_exe(tmp_path / "podman", _PODMAN_STUB))
    )
    monkeypatch.setenv("PRECIS_SANDBOX_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PRECIS_SANDBOX_WHEEL_CACHE", str(tmp_path / "wheels"))
    monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
    monkeypatch.setenv("PRECIS_NODE", "balthazar")
    monkeypatch.setenv("PRECIS_ROOT", str(root))
    monkeypatch.setenv("PRECIS_SANDBOX_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_docker, "_reconciled", True)
    monkeypatch.setattr(claude_docker, "_reap_bin", lambda: str(tmp_path / "podman"))
    return state


def _mk_queued_job(store: Store, params: dict[str, Any]) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="sandbox_run script job",
        meta={"executor": "claude_docker", "job_type": "sandbox_run", "params": params},
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


@pytest.mark.db
def test_script_job_with_code_ref_and_inputs_launches(
    store: Store, hub_: Hub, root: Path, exec_env: Path, tmp_path: Path
) -> None:
    code = _plaintext(root, hub_, "scr", _PY_BODY)
    struct = _structure(store)
    jid = _mk_queued_job(store, _script_params(code_ref=code, inputs=[struct]))

    claude_docker.run_claude_docker_pass(store, limit=4)

    assert _status(store, jid) == "running"
    argv = json.loads(
        (exec_env / f"sandbox-{jid}.argv.json").read_text(encoding="utf-8")
    )
    assert argv[argv.index("--network") + 1] == "none"
    assert argv.count("--network") == 1
    assert any(a.endswith(f"sandbox-{jid}-in:/work/in:ro") for a in argv), argv
    work = tmp_path / "work" / f"sandbox-{jid}"
    assert (work / "main.py").read_text(encoding="utf-8") == _PY_BODY
    inputs = tmp_path / "work" / f"sandbox-{jid}-in"
    assert list(inputs.glob("*.extxyz"))
    assert (inputs / "manifest.json").is_file()
    assert argv[-1].endswith("/work/main.py")


@pytest.mark.db
def test_script_job_with_stale_code_ref_fails_without_container(
    store: Store, hub_: Hub, root: Path, exec_env: Path
) -> None:
    code = _plaintext(root, hub_, "scr", _PY_BODY)
    jid = _mk_queued_job(store, _script_params(code_ref=code))
    (root / "scr.txt").unlink()  # the ref no longer resolves to a file

    claude_docker.run_claude_docker_pass(store, limit=4)

    assert _status(store, jid) == "failed"
    assert not (exec_env / f"sandbox-{jid}.argv.json").exists()


@pytest.mark.db
def test_script_job_with_unresolvable_input_fails_without_container(
    store: Store, hub_: Hub, root: Path, exec_env: Path
) -> None:
    code = _plaintext(root, hub_, "scr", _PY_BODY)
    jid = _mk_queued_job(
        store, _script_params(code_ref=code, inputs=["structure:99999999"])
    )

    claude_docker.run_claude_docker_pass(store, limit=4)

    assert _status(store, jid) == "failed"
    assert not (exec_env / f"sandbox-{jid}.argv.json").exists()
