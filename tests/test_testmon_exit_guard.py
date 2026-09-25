"""Unit tests for ``scripts/lib/testmon-exit-guard.py`` (gr450298, gr449802).

No DB fixtures — must stay in the fast, no-Postgres set (`scripts/test
--fast`). Loaded via ``importlib.util.spec_from_file_location`` (the module
lives under ``scripts/``, outside the ``precis`` package — same pattern as
``tests/test_mutate_driver.py``; the file already carries a ``.py`` suffix
so, unlike ``scripts/coderef``/``scripts/main-ci-status``, no copy-to-temp
workaround is needed here).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_GUARD = (
    Path(__file__).resolve().parents[1] / "scripts" / "lib" / "testmon-exit-guard.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("testmon_exit_guard", _GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load()

# A real fixture: pytest's own INTERNALERROR traceback shape for this exact
# bug (testmon_core.py get_file -> IndexError on rsplit), trimmed to the
# parts the detector actually keys on.
_INTERNALERROR_TAIL = """\
INTERNALERROR> Traceback (most recent call last):
INTERNALERROR>   File ".../_pytest/main.py", line 350, in wrap_session
INTERNALERROR>     session.exitstatus = doit(config, session) or 0
INTERNALERROR>   File ".../pytest_testmon.py", line 210, in pytest_runtest_logreport
INTERNALERROR>     self.testmon_data.get_tests_fingerprints(...)
INTERNALERROR>   File ".../testmon_core.py", line 303, in get_tests_fingerprints
INTERNALERROR>     module = self.source_tree.get_file(filename)
INTERNALERROR>   File ".../testmon_core.py", line 93, in get_file
INTERNALERROR>     ext=filename.rsplit(".", 1)[1],
INTERNALERROR> IndexError: list index out of range
"""


def _clean_run(
    n_passed: int = 10222, n_skipped: int = 28, seconds: str = "3180.86"
) -> str:
    return f"========= {n_passed} passed, {n_skipped} skipped in {seconds}s ========="


# ── testmon_signature_present ───────────────────────────────────────────


def test_signature_absent_on_an_ordinary_log() -> None:
    log = "collecting ...\n" + _clean_run() + "\n"
    assert guard.testmon_signature_present(log) is False


def test_signature_present_on_the_real_traceback_shape() -> None:
    assert guard.testmon_signature_present(_INTERNALERROR_TAIL) is True


def test_signature_requires_both_internalerror_and_get_file() -> None:
    # A bare INTERNALERROR from some OTHER plugin must not false-positive.
    other = "INTERNALERROR> Traceback (most recent call last):\nINTERNALERROR> ValueError: boom\n"
    assert guard.testmon_signature_present(other) is False


# ── decide_exit_code ─────────────────────────────────────────────────────


def test_ordinary_pass_is_untouched() -> None:
    log = "collecting ...\n" + _clean_run() + "\n"
    code, msg = guard.decide_exit_code(log, pytest_exit_code=0)
    assert (code, msg) == (0, None)


def test_ordinary_failure_is_untouched() -> None:
    log = "collecting ...\n========= 3 failed, 10219 passed in 40.10s =========\n"
    code, msg = guard.decide_exit_code(log, pytest_exit_code=1)
    assert (code, msg) == (1, None)


def test_green_run_plus_testmon_crash_forces_exit_0_with_a_warning() -> None:
    # gr450298: 10222 passed, 0 failed, but pytest itself returned 3.
    log = "collecting ...\n" + _clean_run() + "\n" + _INTERNALERROR_TAIL
    code, msg = guard.decide_exit_code(log, pytest_exit_code=3)
    assert code == 0
    assert msg is not None
    assert "WARNING" in msg
    assert "10222 passed" in msg


def test_green_run_plus_testmon_crash_at_exit_0_stays_0_with_a_warning() -> None:
    # gr449802: pytest recovered on its own and returned 0 already — still
    # worth a WARNING (the log holds a crash a caller could easily miss),
    # but no exit-code change is needed.
    log = "collecting ...\n" + _INTERNALERROR_TAIL + "\n" + _clean_run() + "\n"
    code, msg = guard.decide_exit_code(log, pytest_exit_code=0)
    assert code == 0
    assert msg is not None and "WARNING" in msg


def test_testmon_crash_with_no_summary_is_unknown_not_bare_3() -> None:
    # The crash truncated the run before any summary line printed — do not
    # claim this was a pass OR silently trust an opaque internal-error code.
    log = "collecting ...\n" + _INTERNALERROR_TAIL
    code, msg = guard.decide_exit_code(log, pytest_exit_code=3)
    assert code != 0
    assert msg is not None
    assert "result unknown" in msg


def test_testmon_crash_with_no_summary_and_a_zero_raw_code_still_nonzero() -> None:
    log = "collecting ...\n" + _INTERNALERROR_TAIL
    code, msg = guard.decide_exit_code(log, pytest_exit_code=0)
    assert code != 0
    assert msg is not None and "result unknown" in msg


def test_real_failure_alongside_a_testmon_crash_is_not_masked() -> None:
    # If the suite genuinely failed AND testmon also crashed, the failure
    # must win — never launder a red run to green.
    log = (
        "collecting ...\n========= 2 failed, 100 passed in 5.00s =========\n"
        + _INTERNALERROR_TAIL
    )
    code, msg = guard.decide_exit_code(log, pytest_exit_code=1)
    assert code != 0


# ── clear_testmon_datafiles ─────────────────────────────────────────────


def test_clear_testmon_datafiles_removes_only_the_map(tmp_path: Path) -> None:
    (tmp_path / ".testmondata").write_text("x", encoding="utf-8")
    (tmp_path / ".testmondata-journal").write_text("x", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")

    removed = guard.clear_testmon_datafiles(tmp_path)

    assert sorted(removed) == [".testmondata", ".testmondata-journal"]
    assert not (tmp_path / ".testmondata").exists()
    assert not (tmp_path / ".testmondata-journal").exists()
    assert (tmp_path / "keep.txt").exists()


def test_clear_testmon_datafiles_is_a_noop_when_nothing_is_there(
    tmp_path: Path,
) -> None:
    assert guard.clear_testmon_datafiles(tmp_path) == []


# ── main() ────────────────────────────────────────────────────────────────


def test_main_clears_the_map_and_reports_when_signature_present(tmp_path: Path) -> None:
    log_file = tmp_path / "run.log"
    log_file.write_text(
        "collecting ...\n" + _clean_run() + "\n" + _INTERNALERROR_TAIL, encoding="utf-8"
    )
    (tmp_path / ".testmondata").write_text("x", encoding="utf-8")

    rc = guard.main([str(log_file), "3", str(tmp_path)])

    assert rc == 0
    assert not (tmp_path / ".testmondata").exists()


def test_main_leaves_the_map_alone_when_signature_absent(tmp_path: Path) -> None:
    log_file = tmp_path / "run.log"
    log_file.write_text("collecting ...\n" + _clean_run() + "\n", encoding="utf-8")
    (tmp_path / ".testmondata").write_text("x", encoding="utf-8")

    rc = guard.main([str(log_file), "0", str(tmp_path)])

    assert rc == 0
    assert (tmp_path / ".testmondata").exists()
