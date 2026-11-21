"""``admission.spawn`` when the babysitter thread cannot start (gr741695).

The child is already running by then and the caller only sees the raise, so
its pid is unknown: ``spawn`` stops the child's process group and releases the
holds before it re-raises. DB-free: admission and the thread are faked, the
child is a real ``sleep`` in its own session."""

from __future__ import annotations

import signal
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from precis.workers import admission
from precis.workers.admission import Footprint

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: process groups and signals"
)


def test_a_thread_that_cannot_start_leaves_no_running_child_and_no_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    adm = SimpleNamespace(holds=[])
    released: list[Any] = []
    monkeypatch.setattr(admission, "_try_admit", lambda *a, **k: adm)
    monkeypatch.setattr(admission, "_release_quietly", lambda s, a: released.append(a))

    class _NoThread:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(admission.threading, "Thread", _NoThread)
    children: list[subprocess.Popen[bytes]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        children.append(child)
        return child

    with pytest.raises(RuntimeError, match="can't start new thread"):
        admission.spawn(
            object(),
            "h",
            Footprint(cpu_milli=1, mem_mb=1),
            "t:spawn",
            ["echo", "hi"],
            log_path=tmp_path / "run.log",
            exit_file=tmp_path / "exit",
            popen=popen,
        )
    [child] = children
    try:
        assert child.wait(timeout=10) == -signal.SIGKILL  # its group was stopped
    finally:
        if child.poll() is None:
            child.kill()
    assert released == [adm]  # the holds went with it
