"""Per-slice containment of a build session (gr762463, ``_slice_limits``).

A builder's pytest-xdist experiment in ``/tmp`` forked 5604 processes in a
group of its own and took the build host down: the bound is a transient
systemd scope on Linux, ``RLIMIT_NPROC`` without one, and the run's end
stops the whole scope.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from precis.workers.job_types import _slice_limits as sl
from precis.workers.job_types import run_slice

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only rlimits, systemd scopes"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (sl.SCOPE_ENV, sl.TASKS_MAX_ENV, sl.MEMORY_MAX_ENV, sl.NPROC_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sl, "_probe", None)


def test_tasks_max_defaults_parses_and_switches_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sl.tasks_max() == 2048
    monkeypatch.setenv(sl.TASKS_MAX_ENV, "512")
    assert sl.tasks_max() == 512
    for off in ("0", "off", "infinity"):
        monkeypatch.setenv(sl.TASKS_MAX_ENV, off)
        assert sl.tasks_max() is None
    monkeypatch.setenv(sl.TASKS_MAX_ENV, "lots")
    assert sl.tasks_max() == 2048


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("", "25%"),
        ("16G", "16G"),
        ("16g", "16G"),
        ("2048M", "2048M"),
        ("12.5%", "12.5%"),
        ("100%", "100%"),
        ("off", None),
        ("0", None),
        # anything systemd would not read, or a zero, falls back to the default
        ("16 G", "25%"),
        ("0G", "25%"),
        ("150%", "25%"),
        ("1G;TasksMax=infinity", "25%"),
    ],
)
def test_memory_max_only_passes_a_systemd_size(
    monkeypatch: pytest.MonkeyPatch, raw: str, want: str | None
) -> None:
    monkeypatch.setenv(sl.MEMORY_MAX_ENV, raw)
    assert sl.memory_max() == want


def test_nproc_is_off_unless_the_unit_sets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(sl.sys, "platform", platform)
        assert sl.nproc_limit() is None
    monkeypatch.setenv(sl.NPROC_ENV, "900")
    assert sl.nproc_limit() == 900
    for off in ("off", "0", "lots"):
        monkeypatch.setenv(sl.NPROC_ENV, off)
        assert sl.nproc_limit() is None


def test_scope_argv_wraps_the_session_with_its_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sl, "scope_runner", lambda: ("/usr/bin/systemd-run", "ok"))
    monkeypatch.setattr(sl, "_runtime_dir", lambda: "/run/user/1001")
    monkeypatch.setenv(sl.TASKS_MAX_ENV, "1024")
    monkeypatch.setenv(sl.MEMORY_MAX_ENV, "20G")
    c = sl.containment("td7-j9")
    assert c.scope is not None and c.scope.startswith("precis-slice-td7-j9-")
    assert c.scope.endswith(".scope") and c.nproc is None
    argv = c.wrap(["claude", "-p"])
    i = argv.index("/usr/bin/systemd-run")
    assert argv[:i] == ["/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/1001"]
    assert argv[i + 1 : i + 6] == [
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={c.scope}",
    ]
    head = argv[: argv.index("--")]
    props = [head[k + 1] for k, a in enumerate(head) if a == "-p"]
    assert props == [
        f"TimeoutStopSec={sl.STOP_TIMEOUT_S}",
        "TasksMax=1024",
        "MemoryMax=20G",
        "MemorySwapMax=0",
    ]
    # the session's own env is unchanged: the runtime dir is unset after --
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR", "claude", "-p"]
    # two launches of one job never collide on the unit name
    assert sl.containment("td7-j9").scope != c.scope


def test_scope_with_limits_off_still_contains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl, "scope_runner", lambda: ("/usr/bin/systemd-run", "ok"))
    monkeypatch.setattr(sl, "_runtime_dir", lambda: "/run/user/1001")
    monkeypatch.setenv(sl.TASKS_MAX_ENV, "off")
    monkeypatch.setenv(sl.MEMORY_MAX_ENV, "off")
    argv = sl.containment("td1-j2").wrap(["x"])
    props = [argv[k + 1] for k, a in enumerate(argv) if a == "-p"]
    assert props == [f"TimeoutStopSec={sl.STOP_TIMEOUT_S}"]


def test_without_a_scope_the_child_gets_an_nproc_rlimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sl, "scope_runner", lambda: (None, "no cgroups on darwin"))
    c = sl.containment("td1-j2")
    assert (c.scope, c.prefix, c.nproc) == (None, (), None)
    assert c.preexec() is None and "process group only" in c.note
    monkeypatch.setenv(sl.NPROC_ENV, "2048")
    c = sl.containment("td1-j2")
    assert (c.scope, c.prefix, c.nproc) == (None, (), 2048)
    assert c.wrap(["x"]) == ["x"]
    assert c.preexec() is not None


@pytest.mark.parametrize("mode", [None, "", "off", "on", "yes"])
def test_scope_is_off_unless_the_unit_says_auto(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    def probe() -> tuple[str | None, str]:
        raise AssertionError("probed")

    monkeypatch.setattr(sl, "_scope_probe", probe)
    if mode is not None:
        monkeypatch.setenv(sl.SCOPE_ENV, mode)
    exe, why = sl.scope_runner()
    assert exe is None and why.startswith(f"{sl.SCOPE_ENV}=")
    assert sl.containment("td1-j2").scope is None


def test_probe_answer_is_reused_until_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def probe() -> tuple[str | None, str]:
        calls.append(1)
        return "/usr/bin/systemd-run", "ok"

    monkeypatch.setattr(sl, "_scope_probe", probe)
    monkeypatch.setenv(sl.SCOPE_ENV, "auto")
    assert sl.scope_runner()[0] == "/usr/bin/systemd-run"
    assert sl.scope_runner()[0] == "/usr/bin/systemd-run"
    assert calls == [1]
    sl.forget_probe()
    assert sl.scope_runner()[0] == "/usr/bin/systemd-run"
    assert calls == [1, 1]


def test_a_vanished_runtime_dir_after_a_cached_yes_is_no_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe said yes, then the user manager went away: the launch never
    carries ``XDG_RUNTIME_DIR=None``; it falls back and the cache goes."""
    monkeypatch.setenv(sl.SCOPE_ENV, "auto")
    monkeypatch.setattr(sl, "_probe", (time.monotonic(), "/usr/bin/systemd-run", "ok"))
    monkeypatch.setattr(sl, "_runtime_dir", lambda: None)
    c = sl.containment("td1-j2")
    assert (c.scope, c.prefix) == (None, ())
    assert "runtime dir gone" in c.note
    assert sl._probe is None


def _linux_probe_host(monkeypatch: pytest.MonkeyPatch, *, lingering: bool) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    monkeypatch.setattr(sl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sl, "_runtime_dir", lambda: "/run/user/1001")
    monkeypatch.setattr(sl, "_account", lambda: "coder")
    monkeypatch.setattr(sl, "_lingering", lambda user: lingering)


def test_probe_reports_a_failing_systemd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _linux_probe_host(monkeypatch, lingering=True)
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kw["env"]))
        return subprocess.CompletedProcess(
            argv, 1, "", "Failed to connect to bus: No medium found"
        )

    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    exe, why = sl._scope_probe()
    assert exe is None and "No medium found" in why
    [(argv, env)] = seen
    assert argv[:3] == ["systemd-run", "--user", "--scope"]
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1001"
    assert set(env) <= {"PATH", "HOME", "XDG_RUNTIME_DIR"}


def test_probe_passes_the_launchs_exact_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value this host's systemd rejects must fail the probe, not every
    launch: the probe carries the same ``-p`` set the launch will."""
    _linux_probe_host(monkeypatch, lingering=True)
    monkeypatch.setenv(sl.TASKS_MAX_ENV, "777")
    monkeypatch.setenv(sl.MEMORY_MAX_ENV, "12G")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    assert sl._scope_probe()[0] == "/usr/bin/systemd-run"
    [argv] = seen
    probe_props = [argv[k + 1] for k, a in enumerate(argv) if a == "-p"]
    assert (
        probe_props
        == sl.scope_props()
        == [
            f"TimeoutStopSec={sl.STOP_TIMEOUT_S}",
            "TasksMax=777",
            "MemoryMax=12G",
            "MemorySwapMax=0",
        ]
    )
    assert argv[-2:] == ["--", "true"]
    monkeypatch.setattr(sl, "scope_runner", lambda: ("/usr/bin/systemd-run", "ok"))
    launch = sl.containment("td1-j2").wrap(["agent"])
    head = launch[: launch.index("--")]
    assert [head[k + 1] for k, a in enumerate(head) if a == "-p"] == probe_props


def test_probe_needs_lingering(monkeypatch: pytest.MonkeyPatch) -> None:
    """No lingering: no scope, with the reason, and no systemd-run at all."""
    _linux_probe_host(monkeypatch, lingering=False)
    monkeypatch.setattr(
        sl.subprocess, "run", lambda *a, **k: pytest.fail("ran systemd-run")
    )
    exe, why = sl._scope_probe()
    assert exe is None and why == "lingering is off for coder"
    monkeypatch.setenv(sl.SCOPE_ENV, "auto")
    monkeypatch.setattr(sl, "_scope_probe", lambda: (None, why))
    c = sl.containment("td1-j2")
    assert c.scope is None and "lingering is off" in c.note


def test_lingering_reads_the_flag_file_then_loginctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sl, "_LINGER_DIR", str(tmp_path))
    answers = {"out": "Linger=no\n"}
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, answers["out"], "")

    monkeypatch.setattr(sl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    (tmp_path / "coder").touch()
    assert sl._lingering("coder") is True
    assert calls == []  # the flag file is enough
    assert sl._lingering("deploy") is False
    assert calls == [["loginctl", "show-user", "deploy", "-p", "Linger"]]
    answers["out"] = "Linger=yes\n"
    assert sl._lingering("deploy") is True
    monkeypatch.setattr(sl.shutil, "which", lambda name: None)
    assert sl._lingering("deploy") is False


@pytest.mark.parametrize(
    ("scope", "log", "code", "want"),
    [
        ("precis-slice-x.scope", "Failed to connect to bus: No such file\n", 1, True),
        (
            "precis-slice-x.scope",
            "Failed to start transient scope unit: Access denied\n",
            1,
            True,
        ),
        (
            "precis-slice-x.scope",
            "Failed to create bus connection: No such file or directory\n",
            1,
            True,
        ),
        # not scoped, a clean exit, or no exit record: never
        (None, "Failed to connect to bus: x\n", 1, False),
        ("precis-slice-x.scope", "Failed to connect to bus: x\n", 0, False),
        ("precis-slice-x.scope", "Failed to connect to bus: x\n", None, False),
        # a session that ran (a transcript) quoting the phrase: never
        ("precis-slice-x.scope", "a\nb\nc\nd\ne\nFailed to connect to bus\n", 1, False),
        (
            "precis-slice-x.scope",
            "the agent said: Failed to connect to bus\n",
            1,
            False,
        ),
        ("precis-slice-x.scope", "", 1, False),
    ],
)
def test_scope_launch_failed_reads_only_systemd_runs_own_failure(
    scope: str | None, log: str, code: int | None, want: bool
) -> None:
    assert sl.scope_launch_failed(scope, log, code) is want


def test_stop_scope_stops_only_slice_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "linux")
    monkeypatch.setattr(sl.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    for foreign in ("docker.service", "user@1001.service", None, 7, "precis-slice-x"):
        assert sl.stop_scope(foreign) is False
    assert seen == []
    assert sl.stop_scope("precis-slice-td7-j9-0a1b2c3d.scope") is True
    assert seen == [
        [
            "systemctl",
            "--user",
            "stop",
            "--no-block",
            "precis-slice-td7-j9-0a1b2c3d.scope",
        ]
    ]


def test_stop_scope_is_a_no_op_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl.sys, "platform", "darwin")
    monkeypatch.setattr(
        sl.subprocess, "run", lambda *a, **k: pytest.fail("ran systemctl")
    )
    assert sl.stop_scope("precis-slice-td7-j9-0a1b2c3d.scope") is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX rlimits")
def test_nproc_preexec_sets_soft_and_hard_in_the_child() -> None:
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    want = 4000 if hard == resource.RLIM_INFINITY else min(4000, hard)
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resource; print(*resource.getrlimit(resource.RLIMIT_NPROC))",
        ],
        preexec_fn=sl.nproc_preexec(4000),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.split()
    assert out == [str(want), str(want)]


def _nproc_sentinel(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The rlimit preexec the containment chains, replaced by a marker so the
    test can see it reach the launcher without lowering its own limits."""

    def marker() -> bool:
        return True

    monkeypatch.delenv("PRECIS_JOB_MEM_LIMIT", raising=False)
    monkeypatch.setattr(
        sl, "nproc_preexec", lambda limit: marker if limit == 900 else None
    )
    return marker


_CONTAIN = sl.Containment("precis-slice-x.scope", ("systemd-run", "--"), 900, "t")


def test_launch_wraps_argv_and_chains_the_rlimit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The legacy (unpooled) launcher: wrapped argv, the rlimit preexec."""
    marker = _nproc_sentinel(monkeypatch)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        pid = 4242

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(run_slice.subprocess, "Popen", fake_popen)
    run_slice._launch(
        ["claude", "-p"],
        cwd=tmp_path,
        stdin=None,
        log=tmp_path / "log",
        exit_file=tmp_path / "exit",
        env={"PATH": "/usr/bin"},
        contain=_CONTAIN,
    )
    [(argv, kwargs)] = calls
    assert argv[-4:] == ["systemd-run", "--", "claude", "-p"]
    assert kwargs["start_new_session"] is True
    assert kwargs["preexec_fn"] is marker


def test_admitted_launch_wraps_argv_and_carries_the_rlimit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pooled store goes through ``admission.spawn``: it gets the same
    wrapped argv and the same rlimit preexec as the legacy path."""
    import types

    marker = _nproc_sentinel(monkeypatch)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_spawn(*args: Any, **kwargs: Any) -> int:
        calls.append((args, kwargs))
        return 4242

    monkeypatch.setattr(run_slice.admission, "spawn", fake_spawn)
    pid = run_slice._launch(
        ["claude", "-p"],
        cwd=tmp_path,
        stdin=None,
        log=tmp_path / "log",
        exit_file=tmp_path / "exit",
        env={"PATH": "/usr/bin"},
        store=types.SimpleNamespace(pool=object()),
        contain=_CONTAIN,
    )
    assert pid == 4242
    [(args, kwargs)] = calls
    assert args[4] == ["systemd-run", "--", "claude", "-p"]
    assert kwargs["preexec_fn"] is marker
