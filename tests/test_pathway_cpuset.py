"""``params.resources.cpuset`` pins the seed child via ``taskset -c``."""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytest.importorskip("autocatpath")

from precis_pathway import runner, seed_job


def _stub_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_child_cmd", lambda req, out: ["child", req, out])


def _affinity(monkeypatch: pytest.MonkeyPatch, cpus: set[int] | None) -> None:
    """Fake ``os.sched_getaffinity`` (absent on macOS, hence ``raising=False``);
    ``None`` removes it."""
    if cpus is None:
        monkeypatch.delattr(runner.os, "sched_getaffinity", raising=False)
    else:
        monkeypatch.setattr(
            runner.os, "sched_getaffinity", lambda pid: set(cpus), raising=False
        )


def _which(monkeypatch: pytest.MonkeyPatch, found: bool) -> None:
    _affinity(monkeypatch, set(range(64)))
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/usr/bin/taskset" if found else None
    )


def test_pinned_cmd_valid_cpuset_prefixes_taskset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which(monkeypatch, True)
    assert runner._pinned_cmd(["x"], "0-4,10-14") == [
        "taskset",
        "-c",
        "0-4,10-14",
        "x",
    ]


@pytest.mark.parametrize("bad", ["0-4;rm", "a", "1,", "-3", "1-2-3", " 1"])
def test_pinned_cmd_malformed_runs_unpinned_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    _which(monkeypatch, True)
    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        assert runner._pinned_cmd(["x"], bad) == ["x"]
    assert "malformed" in caplog.text


def test_pinned_cmd_without_taskset_runs_unpinned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _which(monkeypatch, False)
    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        assert runner._pinned_cmd(["x"], "0-3") == ["x"]
    assert "taskset not on PATH" in caplog.text


def test_pinned_cmd_missing_cpus_runs_unpinned_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _which(monkeypatch, True)
    _affinity(monkeypatch, set(range(8)))
    with caplog.at_level(logging.WARNING, logger=runner.log.name):
        assert runner._pinned_cmd(["x"], "0-4,10-14") == ["x"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "0-4,10-14" in warnings[0].getMessage()
    assert "10,11,12,13,14" in warnings[0].getMessage()


def test_pinned_cmd_all_cpus_available_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which(monkeypatch, True)
    _affinity(monkeypatch, {0, 1, 2, 3, 4, 10, 11, 12, 13, 14, 15})
    assert runner._pinned_cmd(["x"], "0-4,10-14")[:3] == ["taskset", "-c", "0-4,10-14"]


def test_pinned_cmd_without_sched_getaffinity_runs_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which(monkeypatch, True)
    _affinity(monkeypatch, None)
    assert runner._pinned_cmd(["x"], "0-3") == ["x"]


def test_parse_cpuset() -> None:
    assert runner._parse_cpuset("0-2,5,7-8") == {0, 1, 2, 5, 7, 8}


def test_pinned_cmd_none_is_passthrough() -> None:
    assert runner._pinned_cmd(["x"], None) == ["x"]


def test_detached_submit_pins_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _stub_child(monkeypatch)
    _which(monkeypatch, True)
    seen: dict[str, Any] = {}

    class _P:
        pid = 42

    def _popen(cmd: list[str], **kw: Any) -> _P:
        seen["cmd"] = cmd
        return _P()

    monkeypatch.setattr("subprocess.Popen", _popen)
    h = runner.submit_seed_partial_detached(
        {}, 0, 0, work_dir=str(tmp_path), cpuset="0-4,10-14"
    )
    assert seen["cmd"][:4] == ["taskset", "-c", "0-4,10-14", "child"]
    assert h["pid"] == 42


def test_blocking_launcher_pins_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_child(monkeypatch)
    _which(monkeypatch, True)
    seen: dict[str, Any] = {}

    def _run(cmd: list[str], **kw: Any) -> Any:
        seen["cmd"] = cmd
        raise RuntimeError("stop")

    monkeypatch.setattr("subprocess.run", _run)
    with pytest.raises(RuntimeError, match="stop"):
        runner.run_seed_partial_subprocess({}, 0, 0, cpuset="2,4")
    assert seen["cmd"][:4] == ["taskset", "-c", "2,4", "child"]


class _Ctx:
    def __init__(self, params: dict[str, Any]) -> None:
        self.meta = {"params": params}

    def append_chunk(self, *a: Any, **k: Any) -> None: ...

    def record_failure(self, msg: str, **k: Any) -> None:
        raise AssertionError(msg)


def test_seed_job_submit_passes_cpuset(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(*a: Any, **k: Any) -> dict[str, Any]:
        seen.update(k)
        return {"pid": 1}

    monkeypatch.setattr(runner, "submit_seed_partial_detached", _fake)
    base = {"config": {}, "seed": 0, "model_index": 0}
    seed_job._submit(_Ctx({**base, "resources": {"cpuset": "0-3"}}), seed_job.SPEC)
    assert seen["cpuset"] == "0-3"
    seen.clear()
    seed_job._submit(_Ctx(base), seed_job.SPEC)
    assert "cpuset" not in seen


def test_seed_job_dispatch_passes_cpuset(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(*a: Any, **k: Any) -> Any:
        seen.update(k)
        raise RuntimeError("stop")

    class _FailCtx(_Ctx):
        def record_failure(self, msg: str, **k: Any) -> None:
            seen["failed"] = msg

    monkeypatch.setattr(runner, "run_seed_partial_subprocess", _fake)
    ctx = _FailCtx(
        {"config": {}, "seed": 0, "model_index": 0, "resources": {"cpuset": "1,3"}}
    )
    seed_job._dispatch(ctx, seed_job.SPEC)
    assert seen["cpuset"] == "1,3"
