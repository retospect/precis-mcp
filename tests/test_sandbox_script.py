"""sandbox_run ``mode:script`` — submit gate, argv, lock + wheel stage,
launch / poll / harvest on any exit.

Stub ``podman``, ``uv`` and ``pip`` binaries stand in for the host (no
real container, no network). The podman stub simulates the script
container: it reads ``/work/main.py`` and the read-only wheel mount,
"installs" numpy from a recorded wheel and prints its version, so the
end-to-end test runs ``print(numpy.__version__)`` without a container.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import _sandbox_script, claude_docker
from precis.workers.job_types import sandbox_run

# ── stubs ──────────────────────────────────────────────────────────

_PODMAN_STUB = """#!/usr/bin/env python3
import glob, json, os, sys
d = os.environ["SANDBOX_STUB_DIR"]
args = sys.argv[1:]
cmd = args[0] if args else ""
def sf(name, ext="state"): return os.path.join(d, name + "." + ext)
if cmd == "run":
    name = args[args.index("--name") + 1]
    json.dump(args, open(sf(name, "argv.json"), "w"))
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    work = next(m.split(":")[0] for m in mounts if m.endswith(":/work"))
    wheels = next(m.split(":")[0] for m in mounts if ":/work/wheels" in m)
    code = open(os.path.join(work, "main.py")).read()
    found = glob.glob(os.path.join(wheels, "numpy-*.whl"))
    out, err = "", ""
    rc = int(os.environ.get("SCRIPT_STUB_EXIT", "0"))
    if "numpy.__version__" in code and found:
        version = os.path.basename(found[0]).split("-")[1]
        out = version + "\\n"
        with open(os.path.join(work, "out", "version.txt"), "w") as f:
            f.write(version)
    if rc:
        with open(os.path.join(work, "out", "partial.txt"), "w") as f:
            f.write("half done")
        err = "Traceback (most recent call last):\\nValueError: boom\\n"
    open(sf(name, "stdout"), "w").write(out)
    open(sf(name, "stderr"), "w").write(err)
    with open(sf(name), "w") as f:
        f.write("exited %d" % rc)
    print("ctr-" + name)
    sys.exit(0)
if cmd == "inspect":
    p = sf(args[-1])
    if not os.path.exists(p):
        sys.exit(1)
    sys.stdout.write(open(p).read().strip() + "\\n")
    sys.exit(0)
if cmd == "logs":
    name = args[-1]
    if os.path.exists(sf(name, "stdout")):
        sys.stdout.write(open(sf(name, "stdout")).read())
        sys.stderr.write(open(sf(name, "stderr")).read())
    sys.exit(0)
if cmd == "rm":
    p = sf(args[-1])
    if os.path.exists(p):
        os.remove(p)
    sys.exit(0)
sys.exit(0)
"""

_LOCK_TEXT = "numpy==2.1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"

_UV_STUB = f"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["SANDBOX_STUB_DIR"]
with open(os.path.join(d, "uv.calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
rc = int(os.environ.get("UV_STUB_EXIT", "0"))
if rc:
    sys.stderr.write("No solution found when resolving dependencies\\n")
    sys.exit(rc)
out = sys.argv[sys.argv.index("-o") + 1]
open(out, "w").write({_LOCK_TEXT!r})
"""

_PIP_STUB = """#!/usr/bin/env python3
import json, os, sys
d = os.environ["SANDBOX_STUB_DIR"]
with open(os.path.join(d, "pip.calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
dest = sys.argv[sys.argv.index("-d") + 1]
open(os.path.join(dest, "numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl"), "w").write("wheel")
"""

_LOCK_SHA = hashlib.sha256(_LOCK_TEXT.encode("utf-8")).hexdigest()


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stub podman/uv/pip + the sandbox env; returns the stub state dir."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SANDBOX_STUB_DIR", str(state))
    monkeypatch.setenv(
        "PRECIS_PODMAN_BIN", str(_write_exe(tmp_path / "podman", _PODMAN_STUB))
    )
    monkeypatch.setenv("PRECIS_SANDBOX_UV", str(_write_exe(tmp_path / "uv", _UV_STUB)))
    monkeypatch.setenv(
        "PRECIS_SANDBOX_PIP", str(_write_exe(tmp_path / "pip", _PIP_STUB))
    )
    monkeypatch.setenv("PRECIS_SANDBOX_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("PRECIS_SANDBOX_WHEEL_CACHE", str(tmp_path / "wheels"))
    monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
    monkeypatch.setenv("PRECIS_NODE", "balthazar")
    # mode:script never needs the OAuth token.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_docker, "_reconciled", True)
    # The poll/reap path resolves its binary via the runtime detector;
    # point it at the stub too.
    monkeypatch.setattr(claude_docker, "_reap_bin", lambda: str(tmp_path / "podman"))
    return state


def _calls(state: Path, tool: str) -> list[list[str]]:
    p = state / f"{tool}.calls"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]


def _script_params(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {
        "mode": "script",
        "code": "import numpy\nprint(numpy.__version__)\n",
        "deps": ["numpy>=2"],
        "target_node": "balthazar",
        "resources": {"wall_seconds": 600},
    }
    p.update(over)
    return p


# ── submit gate (no DB) ────────────────────────────────────────────


class TestScriptSubmit:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_SANDBOX_HOSTS", "balthazar spark")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    def test_valid_passes_without_oauth_token(self) -> None:
        assert sandbox_run.validate_submit(None, params=_script_params()) is None

    def test_no_deps_passes(self) -> None:
        params = _script_params()
        del params["deps"]
        assert sandbox_run.validate_submit(None, params=params) is None

    def test_rejects_missing_code(self) -> None:
        params = _script_params()
        del params["code"]
        err = sandbox_run.validate_submit(None, params=params)
        assert err is not None and "next:" in err and "code" in err

    def test_rejects_code_and_code_ref(self) -> None:
        err = sandbox_run.validate_submit(
            None, params=_script_params(code_ref="md:script")
        )
        assert err is not None and "exactly one" in err

    def test_rejects_code_ref_alone_with_next(self) -> None:
        params = _script_params(code_ref="md:script")
        del params["code"]
        err = sandbox_run.validate_submit(None, params=params)
        assert err is not None and "next: pass the script inline as code=" in err

    def test_rejects_empty_code(self) -> None:
        err = sandbox_run.validate_submit(None, params=_script_params(code="  \n"))
        assert err is not None and "params.code" in err

    def test_rejects_oversize_code(self) -> None:
        big = "x = 1\n" * (sandbox_run.MAX_SCRIPT_BYTES // 6 + 1)
        err = sandbox_run.validate_submit(None, params=_script_params(code=big))
        assert err is not None and "cap" in err and "next:" in err

    def test_rejects_inputs(self) -> None:
        err = sandbox_run.validate_submit(
            None, params=_script_params(inputs=["structure:7"])
        )
        assert err is not None and "inputs" in err and "next:" in err

    def test_rejects_precis_access_read_even_with_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_SANDBOX_READ_MCP", "1")
        err = sandbox_run.validate_submit(
            None, params=_script_params(precis_access="read")
        )
        assert err is not None and "mode:script" in err and "next:" in err

    def test_rejects_secrets(self) -> None:
        err = sandbox_run.validate_submit(
            None, params=_script_params(secrets=["OPENAI_KEY"])
        )
        assert err is not None and "secrets" in err

    @pytest.mark.parametrize(
        "dep",
        [
            "git+https://github.com/numpy/numpy",
            "numpy @ https://example.org/numpy.whl",
            "numpy@git+https://example.org/numpy",
            "file:./local",
            "./local",
            "/abs/path/pkg",
            "-e .",
            "--index-url https://evil.example/simple",
            "-r other.txt",
            "numpy\nrequests",
            "",
        ],
    )
    def test_rejects_non_pypi_requirement(self, dep: str) -> None:
        err = sandbox_run.validate_submit(None, params=_script_params(deps=[dep]))
        assert err is not None and "PyPI" in err and "next:" in err, dep

    @pytest.mark.parametrize(
        "dep",
        [
            "numpy",
            "numpy>=2",
            "scipy~=1.14",
            "requests[socks]==2.32.3",
            "matplotlib>=3.8,<4",
            'tomli; python_version < "3.11"',
            "pre-release==1.0rc1",
        ],
    )
    def test_accepts_pypi_requirement(self, dep: str) -> None:
        assert (
            sandbox_run.validate_submit(None, params=_script_params(deps=[dep])) is None
        )

    def test_rejects_deps_not_a_list_of_strings(self) -> None:
        err = sandbox_run.validate_submit(None, params=_script_params(deps=[1]))
        assert err is not None and "deps" in err

    def test_rejects_too_many_deps(self) -> None:
        deps = [f"pkg{i}" for i in range(sandbox_run.MAX_SCRIPT_DEPS + 1)]
        err = sandbox_run.validate_submit(None, params=_script_params(deps=deps))
        assert err is not None and "cap" in err


# ── argv (pure) ────────────────────────────────────────────────────


def test_build_script_argv_invariants() -> None:
    argv = claude_docker.build_script_argv(
        podman_bin="podman",
        job_id=42,
        image="code-task:abc",
        work_dir="/tmp/precis-sandbox/sandbox-42",
        wheels_dir="/tmp/precis-sandbox/wheels/abc",
        memory="8g",
        cpus="2",
        pids_limit=512,
    )
    joined = " ".join(argv)
    assert argv[argv.index("--name") + 1] == "sandbox-42"
    assert "-d" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv.count("--network") == 1
    assert "--env" not in argv
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in joined
    assert "ANTHROPIC_API_KEY" not in joined
    assert "--device" not in argv
    assert "--memory" in argv and "--cpus" in argv and "--pids-limit" in argv
    assert "/tmp/precis-sandbox/wheels/abc:/work/wheels:ro" in argv
    assert argv[argv.index("--") + 1] == "code-task:abc"
    # The explicit in-container command, never the image CMD.
    assert argv[-3:] == ["sh", "-c", claude_docker.SCRIPT_SHELL_CMD]
    assert "--offline --no-index --find-links /work/wheels" in argv[-1]
    assert argv[-1].endswith("/tmp/v/bin/python /work/main.py")


# ── lock + wheel stage (no DB) ─────────────────────────────────────


def test_stage_deps_locks_with_hashes_and_downloads_binary_only(
    stubs: Path, tmp_path: Path
) -> None:
    staged = _sandbox_script.stage_deps(["numpy>=2"], cache_root=tmp_path / "cache")
    assert staged.lock_text == _LOCK_TEXT
    assert staged.lock_sha == _LOCK_SHA
    assert staged.wheels_dir == tmp_path / "cache" / _LOCK_SHA
    assert not staged.reused
    assert list(staged.wheels_dir.glob("numpy-2.1.0-*.whl"))
    assert not (staged.wheels_dir / "lock.txt").exists()

    (uv_argv,) = _calls(stubs, "uv")
    assert uv_argv[:2] == ["pip", "compile"]
    assert "--generate-hashes" in uv_argv and "--no-config" in uv_argv
    assert uv_argv[uv_argv.index("--python-version") + 1] == "3.12"
    assert uv_argv[uv_argv.index("--default-index") + 1] == "https://pypi.org/simple"

    (pip_argv,) = _calls(stubs, "pip")
    assert pip_argv[0] == "download"
    assert "--only-binary=:all:" in pip_argv and "--no-deps" in pip_argv
    assert pip_argv[pip_argv.index("--index-url") + 1] == "https://pypi.org/simple"


def test_stage_deps_reuses_a_complete_wheel_dir(stubs: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _sandbox_script.stage_deps(["numpy>=2"], cache_root=cache)
    again = _sandbox_script.stage_deps(["numpy>=2"], cache_root=cache)
    assert again.reused
    assert len(_calls(stubs, "pip")) == 1  # no second download


def test_stage_deps_without_deps_runs_nothing(stubs: Path, tmp_path: Path) -> None:
    staged = _sandbox_script.stage_deps([], cache_root=tmp_path / "cache")
    assert staged.lock_text == ""
    assert staged.wheels_dir.is_dir()
    assert _calls(stubs, "uv") == [] and _calls(stubs, "pip") == []


def test_stage_deps_lock_failure_raises(
    stubs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_STUB_EXIT", "1")
    with pytest.raises(_sandbox_script.ScriptStageError, match="No solution found"):
        _sandbox_script.stage_deps(["numpy>=2"], cache_root=tmp_path / "cache")


def test_stage_deps_missing_pip_names_the_override(
    stubs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SANDBOX_PIP", str(tmp_path / "no-such-pip"))
    cache = tmp_path / "cache"
    with pytest.raises(_sandbox_script.ScriptStageError, match="PRECIS_SANDBOX_PIP"):
        _sandbox_script.stage_deps(["numpy>=2"], cache_root=cache)
    # A failed download never leaves a trusted wheel dir behind.
    assert not (cache / _LOCK_SHA).exists()


# ── launch / poll / harvest (DB) ───────────────────────────────────


@pytest.fixture
def script_env(stubs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    precis_root = tmp_path / "PRECIS_ROOT"
    precis_root.mkdir()
    monkeypatch.setenv("PRECIS_ROOT", str(precis_root))
    monkeypatch.setenv("PRECIS_SANDBOX_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    return stubs


def _record_wheel_dir(tmp_path: Path) -> Path:
    """A wheel cache entry as a previous job would have left it."""
    wheels = tmp_path / "wheels" / _LOCK_SHA
    wheels.mkdir(parents=True)
    (wheels / "numpy-2.1.0-cp312-cp312-manylinux_2_17_x86_64.whl").write_text(
        "wheel", encoding="utf-8"
    )
    (wheels / ".complete").write_text(_LOCK_SHA + "\n", encoding="utf-8")
    return wheels


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


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _events(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_event' "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _harvested_files(store: Store, folder_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'harvest_orig_path' FROM refs WHERE parent_id = %s",
            (folder_id,),
        ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.db
def test_script_end_to_end_prints_numpy_version_from_recorded_wheels(
    store: Store, script_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator-pinned network must not leak into mode:script.
    monkeypatch.setenv("PRECIS_SANDBOX_NETWORK", "bridge")
    _record_wheel_dir(tmp_path)
    jid = _mk_queued_job(store, _script_params())

    claude_docker.run_claude_docker_pass(store, limit=4)  # lock + launch
    assert _status(store, jid) == "running"
    meta = _meta(store, jid)
    assert meta["deps_lock_sha"] == _LOCK_SHA
    assert _calls(script_env, "pip") == []  # recorded wheel dir reused

    argv = json.loads(
        (script_env / f"sandbox-{jid}.argv.json").read_text(encoding="utf-8")
    )
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[-3:] == ["sh", "-c", claude_docker.SCRIPT_SHELL_CMD]
    work = tmp_path / "work" / f"sandbox-{jid}"
    assert (work / "main.py").read_text(encoding="utf-8") == _script_params()["code"]
    assert (work / "lock.txt").read_text(encoding="utf-8") == _LOCK_TEXT
    assert not (work / "PROMPT.md").exists()

    claude_docker.run_claude_docker_pass(store, limit=4)  # poll → harvest → reap
    assert _status(store, jid) == "succeeded"
    meta = _meta(store, jid)
    assert meta["exit_code"] == 0
    assert "version.txt" in _harvested_files(store, meta["harvest_folder_id"])
    stdout_events = [e for e in _events(store, jid) if e.startswith("script stdout")]
    assert stdout_events and "2.1.0" in stdout_events[0]
    assert not work.exists()


@pytest.mark.db
def test_script_nonzero_exit_still_harvests_out(
    store: Store, script_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRIPT_STUB_EXIT", "1")
    _record_wheel_dir(tmp_path)
    jid = _mk_queued_job(store, _script_params())
    claude_docker.run_claude_docker_pass(store, limit=4)
    claude_docker.run_claude_docker_pass(store, limit=4)

    assert _status(store, jid) == "failed"
    meta = _meta(store, jid)
    assert meta["exit_code"] == 1
    assert "partial.txt" in _harvested_files(store, meta["harvest_folder_id"])
    stderr_events = [e for e in _events(store, jid) if e.startswith("script stderr")]
    assert stderr_events and "ValueError: boom" in stderr_events[0]


@pytest.mark.db
def test_script_lock_failure_fails_job_without_container(
    store: Store, script_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_STUB_EXIT", "1")
    jid = _mk_queued_job(store, _script_params())
    claude_docker.run_claude_docker_pass(store, limit=4)

    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)
    assert any("uv pip compile" in e for e in _events(store, jid))


@pytest.mark.db
def test_script_launch_refuses_code_ref_at_launch_time(
    store: Store, script_env: Path
) -> None:
    """A dispatch-minted job skips the put-time gate; launch re-checks."""
    params = _script_params(code_ref="md:script")
    del params["code"]
    jid = _mk_queued_job(store, params)
    claude_docker.run_claude_docker_pass(store, limit=4)
    assert _status(store, jid) == "failed"
    assert "container" not in _meta(store, jid)
    assert _calls(script_env, "uv") == []


@pytest.mark.db
def test_put_time_gate_for_script(hub: Any, store: Store, script_env: Path) -> None:
    from precis.errors import BadInput
    from precis.handlers.job import JobHandler

    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    handler = JobHandler(hub=hub)
    assert handler.put(
        job_type="sandbox_run", parent_id=parent.id, params=_script_params()
    )
    with pytest.raises(BadInput, match="PyPI"):
        handler.put(
            job_type="sandbox_run",
            parent_id=parent.id,
            params=_script_params(deps=["git+https://github.com/numpy/numpy"]),
        )
