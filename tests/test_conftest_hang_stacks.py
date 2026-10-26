"""conftest hang-stack artifacts (gr514873): unit tests plus one real run.

pytest-timeout's thread method kills a run at its ``timeout`` — under
``-n auto`` the stack dump dies with the worker ("node down", no stacks).
tests/conftest.py therefore re-arms ``faulthandler
.dump_traceback_later`` per test into a per-worker file under
``.test-hang-stacks/`` and names the non-empty dumps in the terminal
summary. The unit tests drive the helpers with fakes; the e2e test runs a
real ``pytest`` subprocess whose conftest re-exports the real hooks with
the dump dir and margin pointed at tmp_path, then wedges a test in a
foreign call the thread method cannot unstick — the same failure the
gripe hit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import tests.conftest as cf

REPO = Path(__file__).resolve().parents[1]


class _Cfg:
    """The ``item.config`` sliver ``_hang_effective_timeout`` reads."""

    def __init__(self, cli: object = None, ini: object = 300) -> None:
        self._cli, self._ini = cli, ini

    def getoption(self, name: str) -> object:
        if name != "timeout":
            raise ValueError(name)
        return self._cli

    def getini(self, name: str) -> object:
        assert name == "timeout"
        return self._ini


class _Item:
    def __init__(self, mark: object = None, cfg: _Cfg | None = None) -> None:
        self._mark = mark
        self.config = cfg or _Cfg()

    def get_closest_marker(self, name: str) -> object:
        return self._mark if name == "timeout" else None


def _eff(item: _Item) -> float:
    return cf._hang_effective_timeout(cast(Any, item))


class TestEffectiveTimeout:
    def test_mark_wins_over_cli_and_ini(self) -> None:
        item = _Item(mark=SimpleNamespace(args=(7,)), cfg=_Cfg(cli=9, ini=300))
        assert _eff(item) == 7.0

    def test_cli_wins_over_ini(self) -> None:
        assert _eff(_Item(cfg=_Cfg(cli=9, ini=300))) == 9.0

    def test_ini_fallback(self) -> None:
        assert _eff(_Item(cfg=_Cfg(cli=None, ini=45))) == 45.0

    def test_default_when_nothing_set(self) -> None:
        assert _eff(_Item(cfg=_Cfg(cli=None, ini=None))) == 300.0

    def test_mark_zero_disables(self) -> None:
        assert _eff(_Item(mark=SimpleNamespace(args=(0,)))) == 0.0


class TestArmed:
    """``_hang_armed``: workers and serial runs arm; the xdist controller
    (its ``dsession`` plugin) does not — it runs no tests and would only
    leave an empty file."""

    def _config(self, *, worker: bool, dsession: bool) -> Any:
        cfg = SimpleNamespace(
            pluginmanager=SimpleNamespace(
                get_plugin=lambda name: object() if dsession else None
            )
        )
        if worker:
            cfg.workerinput = {}
        return cfg

    def test_serial_run_arms(self) -> None:
        assert cf._hang_armed(self._config(worker=False, dsession=False))

    def test_worker_arms(self) -> None:
        assert cf._hang_armed(self._config(worker=True, dsession=True))

    def test_controller_does_not_arm(self) -> None:
        assert not cf._hang_armed(self._config(worker=False, dsession=True))


class _Reporter:
    def __init__(self) -> None:
        self.sep: list[str] = []
        self.lines: list[str] = []

    def write_sep(self, sep: str, title: str) -> None:
        self.sep.append(title)

    def write_line(self, line: str) -> None:
        self.lines.append(line)


class TestSessionFinishCleanup:
    """gr514873: a clean run leaves no per-worker artifact behind."""

    def _session(self) -> Any:
        cfg = SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
            getini=lambda name: 600,
        )
        return SimpleNamespace(config=cfg)

    def test_clean_run_leaves_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "_HANG_DUMP_DIR", tmp_path)
        monkeypatch.setattr(cf, "_hang_dump_fh", None)
        monkeypatch.setattr(cf, "_hang_dump_path", None)
        session = self._session()
        cf.pytest_sessionstart(session)
        assert len(list(tmp_path.glob("hang-*.log"))) == 1
        cf.pytest_sessionfinish(session)
        assert list(tmp_path.glob("hang-*.log")) == []

    def test_dumped_run_keeps_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "_HANG_DUMP_DIR", tmp_path)
        monkeypatch.setattr(cf, "_hang_dump_fh", None)
        monkeypatch.setattr(cf, "_hang_dump_path", None)
        session = self._session()
        cf.pytest_sessionstart(session)
        (path,) = tmp_path.glob("hang-*.log")
        cf._hang_dump_fh.write("Thread 0x1 (most recent call first):\n")
        cf.pytest_sessionfinish(session)
        assert path.exists()


class TestTerminalSummary:
    def test_names_only_files_with_real_dumps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "_HANG_DUMP_DIR", tmp_path)
        (tmp_path / "hang-gw0-1.log").write_text(
            "# ===== t/test_x.py::t =====\nThread 0x1 (main): ...\n",
            encoding="utf-8",
        )
        # Header-only file: armed but never dumped — must not be named.
        (tmp_path / "hang-gw1-2.log").write_text(
            "# ===== t/test_y.py::t =====\n", encoding="utf-8"
        )
        rep = _Reporter()
        cf.pytest_terminal_summary(rep, 0, cast(Any, SimpleNamespace()))
        assert rep.sep == ["worker hang stack dumps (gr514873)"]
        assert len(rep.lines) == 1 and "hang-gw0-1.log" in rep.lines[0]

    def test_quiet_when_no_dumps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cf, "_HANG_DUMP_DIR", tmp_path)
        rep = _Reporter()
        cf.pytest_terminal_summary(rep, 0, cast(Any, SimpleNamespace()))
        assert rep.sep == [] and rep.lines == []


_SHIM_CONFTEST = """\
import sys
sys.path.insert(0, {repo!r})
import tests.conftest as _c
from pathlib import Path
_c._HANG_DUMP_DIR = Path({dump!r})
# Dump 1 s BEFORE the mark's timeout: pytest-timeout's thread method is
# os._exit(1) on a threading.Timer — arming at the timeout itself races
# the kill (the Linux CI flake in run 40189803848).
_c._HANG_DUMP_MARGIN_S = -1.0
pytest_sessionstart = _c.pytest_sessionstart
pytest_runtest_setup = _c.pytest_runtest_setup
pytest_sessionfinish = _c.pytest_sessionfinish
pytest_terminal_summary = _c.pytest_terminal_summary
"""

_PROBE_TEST = """\
import ctypes
import ctypes.util

import pytest


@pytest.mark.timeout(2)
def test_c_wedge() -> None:
    # A foreign call a thread-method timeout cannot unstick — the wedge
    # stays in the libc sleep regardless of what pytest-timeout tries.
    # This is the "node down" hang of gr514873. (The inner run also passes
    # -p no:timeout, so no kill timer races the file dump at all.)
    ctypes.CDLL(ctypes.util.find_library("c")).sleep(90)
"""


@pytest.mark.slow
def test_hung_worker_writes_stack_artifact(tmp_path: Path) -> None:
    """A test wedged in a C call still leaves every thread's stack in the
    per-worker artifact — the evidence the gripe had to re-run ``-n0`` for.
    """
    dump_dir = tmp_path / "stacks"
    (tmp_path / "conftest.py").write_text(
        _SHIM_CONFTEST.format(repo=str(REPO), dump=str(dump_dir)),
        encoding="utf-8",
    )
    (tmp_path / "test_probe.py").write_text(_PROBE_TEST, encoding="utf-8")
    # The inner run must be a clean serial pytest: an outer xdist worker's
    # PYTEST_XDIST_* env (and any addopts) must not leak in — that is what
    # named the CI artifact hang-gw1-* under -n auto (Linux run 40189803848).
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PYTEST_XDIST") and k != "PYTEST_ADDOPTS"
    }
    env["PYTHONPATH"] = str(REPO)
    # -p no:timeout: without the kill timer nothing races the file dump —
    # the mark still arms it via _hang_effective_timeout (read direct).
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:timeout",
            "test_probe.py",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 120
        dumped = ""
        while time.monotonic() < deadline:
            hits = list(dump_dir.glob("hang-*.log"))
            if hits:
                dumped = hits[0].read_text(encoding="utf-8", errors="replace")
                if "Thread " in dumped:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        assert "Thread " in dumped, (
            f"no stack dump in {list(dump_dir.glob('*'))}; proc rc={proc.poll()}"
        )
        assert "test_c_wedge" in dumped
    finally:
        proc.kill()
        proc.wait()
