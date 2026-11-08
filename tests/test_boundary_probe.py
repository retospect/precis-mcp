"""Tests for the boundary probes (``scripts/probes/``) and the
``boundary_probe`` job_type (td574934 A4, gr596928).

Red-first contract: each probe exits 0 on the current tree and 1 against
a planted breach fixture; exit 2 maps to class ``cannot-run``; a probe
changed on the branch runs twice; a boundary with no registered probe is
refused ``unprobed`` instead of passing.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers.job_types import boundary_probe as bp
from precis.workers.job_types import run_slice

REPO = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO / "scripts" / "probes"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: the probes run scripts/ship and git in subprocesses",
)


def _load_probe(name: str) -> Any:
    """Import a ``scripts/probes/<name>.py`` file as a module."""
    spec = importlib.util.spec_from_file_location(
        f"precis_probe_{name}", PROBE_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the probes: 0 on main, 1 on a planted breach ─────────────────────


def test_envelope_probe_passes() -> None:
    assert _load_probe("envelope").main([str(REPO)]) == 0


def test_envelope_probe_flags_an_unboxed_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted breach: the builder emits a container mcp-config and drops
    the strict guard — the probe must say so."""
    from precis.utils import claude_agent

    def _bad_resolve(*_a: Any, **_k: Any) -> tuple[Any, ...]:
        return (
            "claude",
            [
                "claude",
                "-p",
                "--mcp-config",
                "/etc/precis/agent-mcp.json",
                "--",
                "x",
            ],
            "m",
            1.0,
            0.01,
            None,
        )

    monkeypatch.setattr(claude_agent, "_resolve_agent_args", _bad_resolve)
    assert _load_probe("envelope").main([str(REPO)]) == 1


def test_slice_env_probe_passes() -> None:
    assert _load_probe("slice_env").main([str(REPO)]) == 0


def test_slice_env_probe_flags_a_dsn_on_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_slice,
        "ENV_ALLOWLIST",
        (*run_slice.ENV_ALLOWLIST, "PRECIS_DATABASE_URL"),
    )
    assert _load_probe("slice_env").main([str(REPO)]) == 1


def test_slice_env_probe_flags_a_credential_shaped_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvested set only covers credentials the tree already reads —
    an unreferenced ``ARCHIVE_DSN`` on the allowlist must still breach by
    name SHAPE (td598968)."""
    monkeypatch.setattr(
        run_slice,
        "ENV_ALLOWLIST",
        (*run_slice.ENV_ALLOWLIST, "ARCHIVE_DSN"),
    )
    assert _load_probe("slice_env").main([str(REPO)]) == 1


def test_safe_fetch_probe_passes() -> None:
    assert _load_probe("safe_fetch").main([str(REPO)]) == 0


def test_safe_fetch_probe_flags_an_open_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils import safe_fetch as sf

    monkeypatch.setattr(sf, "_ip_blocked", lambda _ip: False)
    assert _load_probe("safe_fetch").main([str(REPO)]) == 1


def test_deny_list_probe_passes() -> None:
    assert _load_probe("deny_list").main([str(REPO)]) == 0


def test_deny_list_probe_flags_an_open_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_slice, "deny_violations", lambda _p, _d: [])
    assert _load_probe("deny_list").main([str(REPO)]) == 1


def test_deny_list_probe_flags_a_dropped_protected_glob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A glob removed CONSISTENTLY from the lanes tuples — the attack the
    registry floor exists for: every derived set stays self-consistent,
    so only the probe-pinned floor can see ``scripts/ship`` is gone
    (td598968)."""
    from precis.plans import lanes

    factory = tuple(g for g in lanes.FACTORY_DENY_WRITE_PATHS if g != "scripts/ship")
    strict = tuple(dict.fromkeys(factory + lanes.HUMAN_ONLY_PATHS))
    engine = tuple(
        dict.fromkeys(
            g for g in factory + lanes.INTERIM_DENY if g not in lanes.HUMAN_ONLY_PATHS
        )
    )
    monkeypatch.setattr(lanes, "FACTORY_DENY_WRITE_PATHS", factory)
    monkeypatch.setattr(lanes, "STRICT_DENY", strict)
    monkeypatch.setattr(lanes, "ENGINE_PATHS", engine)
    monkeypatch.setattr(lanes, "_ENGINE_PATHS_LOWER", tuple(g.lower() for g in engine))
    monkeypatch.setattr(
        lanes,
        "Lane",
        lambda **_kw: SimpleNamespace(deny_write_paths=strict),
    )
    assert _load_probe("deny_list").main([str(REPO)]) == 1


@pytest.mark.parametrize("name", ["envelope", "slice_env", "safe_fetch", "deny_list"])
def test_probe_subprocess_contract(name: str) -> None:
    """The exit-code contract end to end: exit 0 on this tree."""
    proc = subprocess.run(
        [sys.executable, str(PROBE_DIR / f"{name}.py"), str(REPO)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── exit classification ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        (0, "pass"),
        (1, "breach"),
        (2, "cannot-run"),
        (None, "cannot-run"),
        (137, "cannot-run"),
    ],
)
def test_classify_exit(code: int | None, cls: str) -> None:
    assert bp._classify_exit(code) == cls


# ── boundary hits + the run plan ─────────────────────────────────────


def test_boundary_hits() -> None:
    changed = [
        "src/precis/utils/safe_fetch.py",
        "scripts/probes/slice_env.py",
    ]
    assert bp.boundary_hits(changed) == ["safe_fetch", "slice_env"]


def _plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    changed: list[str],
    on_main: dict[str, str | None],
    on_branch: dict[str, str | None],
    probes: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``probe_plan`` with the git seams faked."""
    tree = tmp_path / "tree"
    tree.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        bp,
        "_blob_sha",
        lambda _tree, spec: on_main.get(spec.rsplit(":", 1)[-1]),
    )
    monkeypatch.setattr(bp, "_branch_probe", lambda _tree, rel: on_branch.get(rel))
    monkeypatch.setattr(
        bp,
        "_extract_probe",
        lambda _tree, _spec, dest: bool(dest.write_text("x", encoding="utf-8")),
    )
    if probes is not None:
        monkeypatch.setattr(bp, "PROBES", probes)
    return bp.probe_plan(tree, changed, "abc123", work)


def test_plan_unchanged_probe_runs_main_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["src/precis/utils/safe_fetch.py"],
        on_main={"scripts/probes/safe_fetch.py": "m1"},
        on_branch={"scripts/probes/safe_fetch.py": "m1"},
    )
    assert records == []
    assert [(r["source"], r["probe_sha"]) for r in runs] == [("main", "m1")]


def test_plan_changed_probe_runs_main_and_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A probe changed on the branch runs twice — both must pass."""
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["scripts/probes/safe_fetch.py"],
        on_main={"scripts/probes/safe_fetch.py": "m1"},
        on_branch={"scripts/probes/safe_fetch.py": "b2"},
    )
    assert records == []
    assert [(r["source"], r["probe_sha"]) for r in runs] == [
        ("main", "m1"),
        ("branch", "b2"),
    ]


def test_plan_new_probe_runs_branch_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["scripts/probes/safe_fetch.py"],
        on_main={},
        on_branch={"scripts/probes/safe_fetch.py": "b2"},
    )
    assert records == []
    assert [(r["source"], r["probe_sha"]) for r in runs] == [("branch", "b2")]


def test_plan_deleted_probe_is_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["scripts/probes/safe_fetch.py"],
        on_main={"scripts/probes/safe_fetch.py": "m1"},
        on_branch={},
    )
    assert [r["source"] for r in runs] == ["main"]
    assert [r["class"] for r in records] == ["cannot-run"]


def test_plan_unregistered_boundary_is_unprobed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A boundary hit with no registered probe refuses (unprobed), it
    does not pass."""
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["src/precis/utils/safe_fetch.py"],
        on_main={},
        on_branch={},
        probes={k: v for k, v in bp.PROBES.items() if k != "safe_fetch"},
    )
    assert runs == []
    assert len(records) == 1
    assert records[0]["class"] == "unprobed"
    assert records[0]["boundary"] == "safe_fetch"


def test_plan_missing_probe_everywhere_is_unprobed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs, records = _plan(
        monkeypatch,
        tmp_path,
        changed=["src/precis/utils/safe_fetch.py"],
        on_main={},
        on_branch={},
    )
    assert runs == []
    assert [r["class"] for r in records] == ["unprobed"]


# ── the child's container run ────────────────────────────────────────


def test_container_argv_shape() -> None:
    argv = bp._container_argv(
        "/probe-src/x.py",
        Path("/wt"),
        "precis-dev",
        name="precis-bprobe-job-7-0",
        tag="job-7",
    )
    assert argv[:2] == [argv[0], "run"]
    assert "--network" in argv and "none" in argv
    joined = " ".join(argv)
    assert "--env" not in joined  # no DSN crosses
    assert "/app:ro" in joined  # the branch tree read-only
    # named + labelled so a dead client can't orphan the container
    assert argv[argv.index("--name") + 1] == "precis-bprobe-job-7-0"
    assert argv[argv.index("--label") + 1] == "precis.boundary-probe=job-7"
    assert argv[-2:] == ["python", "/probe.py"]


def test_run_probe_classifies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _ok(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", _ok)
    code, out = bp._run_probe(
        {"file": "/p/x.py"}, tmp_path, "precis-dev", name="n0", tag="job-1"
    )
    assert code == 0 and "ok" in out
    assert calls and calls[0][1] == "run"


def test_run_probe_timeout_removes_the_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out ``docker run`` client leaves the container running —
    the probe must force-remove it by name before reporting."""
    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 600)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    code, out = bp._run_probe(
        {"file": "/p/x.py"},
        tmp_path,
        "precis-dev",
        name="precis-bprobe-job-1-0",
        tag="job-1",
    )
    assert code is None and "exceeded" in out
    verbs = [c[1] for c in calls]
    assert calls[verbs.index("rm")][1:4] == ["rm", "-f", "precis-bprobe-job-1-0"]


def test_run_probe_launch_failure_removes_the_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crashed client may have created the container before dying —
    sweep it too."""
    calls: list[list[str]] = []

    def _run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "run":
            raise OSError("daemon gone")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    code, out = bp._run_probe(
        {"file": "/p/x.py"}, tmp_path, "precis-dev", name="n0", tag="job-1"
    )
    assert code is None and "launch failed" in out
    assert any(c[1:3] == ["rm", "-f"] for c in calls)


def test_kill_removes_tagged_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job cancellation: after SIGTERMing the child, every container
    labelled with the run tag is force-removed."""
    calls: list[list[str]] = []
    monkeypatch.setattr(bp._rc, "kill", lambda *_a, **_k: None)
    monkeypatch.setattr(bp, "_container_bin", lambda: "docker")

    def _run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        out = "cid1\ncid2\n" if argv[1] == "ps" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    bp.kill(None, {"pid": 0, "container_tag": "job-9"})
    ps = next(c for c in calls if c[1] == "ps")
    assert "label=precis.boundary-probe=job-9" in ps
    removed = [c[3] for c in calls if c[1:3] == ["rm", "-f"]]
    assert removed == ["cid1", "cid2"]


def test_kill_without_tag_only_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[Any] = []
    monkeypatch.setattr(bp._rc, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(
        bp,
        "_rm_tagged",
        lambda _tag: pytest.fail("no containers without a tag"),
    )
    bp.kill(None, {"pid": 0})
    assert len(killed) == 1


def test_main_writes_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The detached child runs each planned probe and folds the planned
    refusal records into the results file."""
    plan = tmp_path / "plan.json"
    out = tmp_path / "out.json"
    plan.write_text(
        json.dumps(
            {
                "tree": str(tmp_path),
                "image": "precis-dev",
                "runs": [
                    {
                        "boundary": "safe_fetch",
                        "probe": "scripts/probes/safe_fetch.py",
                        "probe_sha": "m1",
                        "head_sha": "h0",
                        "source": "main",
                        "file": str(tmp_path / "p.py"),
                    },
                    {
                        "boundary": "deny_list",
                        "probe": "scripts/probes/deny_list.py",
                        "probe_sha": "b2",
                        "head_sha": "h0",
                        "source": "branch",
                        "file": str(tmp_path / "q.py"),
                    },
                ],
                "records": [
                    {
                        "boundary": "slice_env",
                        "probe": None,
                        "probe_sha": None,
                        "head_sha": "h0",
                        "exit": None,
                        "class": "unprobed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    codes = iter([0, 1])
    monkeypatch.setattr(
        bp,
        "_run_probe",
        lambda run, _tree, _img, **_kw: (next(codes), "finding-text"),
    )
    assert bp._main(["run", "--plan", str(plan), "--out", str(out)]) == 0
    records = json.loads(out.read_text(encoding="utf-8"))["records"]
    assert [r["class"] for r in records] == ["pass", "breach", "unprobed"]
    assert records[1]["finding"] == "finding-text"
    for key in ("boundary", "probe", "probe_sha", "head_sha", "exit", "class"):
        assert all(key in r for r in records)


# ── the poll verdict ─────────────────────────────────────────────────


class _Ctx:
    """Minimal DispatchContext double for poll."""

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {"params": {}}
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.failure: str | None = None

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_status(self, status: str) -> None:
        self.status = status

    def record_failure(self, reason: str) -> None:
        self.failure = reason


def _poll_with_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: list[dict[str, Any]]
) -> _Ctx:
    out = tmp_path / "results.json"
    out.write_text(json.dumps({"records": records}), encoding="utf-8")
    monkeypatch.setattr(bp._rc, "finish_run", lambda *_a, **_k: (0, "log text"))
    ctx = _Ctx()
    assert bp.poll(ctx, {"out": str(out), "pid": 1}) is True
    return ctx


def _rec(cls: str, boundary: str = "safe_fetch") -> dict[str, Any]:
    return {
        "boundary": boundary,
        "probe": f"scripts/probes/{boundary}.py",
        "probe_sha": "m1",
        "head_sha": "h0",
        "exit": 0 if cls == "pass" else 1,
        "class": cls,
    }


def test_poll_succeeds_on_all_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _poll_with_records(monkeypatch, tmp_path, [_rec("pass"), _rec("pass")])
    assert ctx.status == "succeeded"
    assert ctx.failure is None
    assert len(ctx.meta["probes"]) == 2


def test_poll_fails_on_breach_and_unprobed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Anything but all-pass fails the job — a breach, and a boundary
    refused as unprobed."""
    ctx = _poll_with_records(
        monkeypatch,
        tmp_path,
        [_rec("pass"), _rec("breach"), _rec("unprobed", "envelope")],
    )
    assert ctx.status is None
    assert ctx.failure is not None and "unprobed" in ctx.failure
    assert len(ctx.meta["probes"]) == 3


def test_registered() -> None:
    from precis.workers.job_types import get_job_type, is_registered_job_type

    assert is_registered_job_type("boundary_probe")
    spec = get_job_type("boundary_probe")
    assert spec is not None and spec.name == "boundary_probe"
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.submit is not None and spec.poll is not None


def test_validate_submit_refuses_without_worktree() -> None:
    assert bp.validate_submit(None, params={}) is not None
    assert bp.validate_submit(None, params={"worktree": "/x"}) is None


def test_validate_submit_refuses_when_factory_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    assert bp.validate_submit(None, params={"worktree": "/x"}) is not None
