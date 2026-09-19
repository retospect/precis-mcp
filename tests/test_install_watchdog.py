"""Install watchdog (gr338977) — a live server whose venv is replaced
must decide "exit cleanly", never wedge. These pin the decision logic;
the thread itself is a sleep-loop around :func:`_install_replaced` plus
``os._exit`` and isn't run here."""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from precis import install_watchdog
from precis.install_watchdog import (
    InstallWatchdog,
    _fingerprint_for,
    _install_replaced,
    consume_last_exit_breadcrumb,
    install_exit_breadcrumb_hooks,
    start_install_watchdog,
)


@pytest.fixture(autouse=True)
def _no_watchdog_outlives_its_test() -> Iterator[None]:
    """Stop every watchdog thread a test armed (gr347099).

    A leaked thread carries a tmp-path baseline and, once monkeypatch has
    restored the real ``install_fingerprint``, its next poll reads as
    "install replaced" and calls the real ``os._exit(0)`` — on the xdist
    worker, one interval later. Two tests here armed 3600 s intervals, so
    any gate still running an hour on lost a worker and hung at 95%.
    """
    yield
    for thread in threading.enumerate():
        if isinstance(thread, InstallWatchdog):
            thread.stop()
    assert not [t for t in threading.enumerate() if isinstance(t, InstallWatchdog)]


def _fake_install(tmp_path: Path) -> Path:
    pkg = tmp_path / "venv" / "lib" / "site-packages" / "precis"
    pkg.mkdir(parents=True)
    init = pkg / "__init__.py"
    init.write_text("# v1\n", encoding="utf-8")
    return init


def test_source_install_is_not_watchable(tmp_path: Path) -> None:
    """A path outside site-packages (worktree / editable install) yields
    ``None`` — git churn there is not a reinstall."""
    init = tmp_path / "src" / "precis" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text("# source\n", encoding="utf-8")
    assert _fingerprint_for(init) is None


def test_site_packages_install_fingerprints(tmp_path: Path) -> None:
    init = _fake_install(tmp_path)
    fp = _fingerprint_for(init)
    assert fp is not None
    assert fp[0] == str(init.resolve())
    # Stable across repeated stats of an untouched install.
    assert _fingerprint_for(init) == fp


def test_replaced_file_changes_fingerprint(tmp_path: Path) -> None:
    """A reinstall deletes + rewrites ``__init__.py`` — new inode."""
    init = _fake_install(tmp_path)
    fp = _fingerprint_for(init)
    init.unlink()
    init.write_text("# v2 — longer body\n", encoding="utf-8")
    assert _fingerprint_for(init) != fp


def test_install_replaced_false_on_same_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init = _fake_install(tmp_path)
    baseline = _fingerprint_for(init)
    assert baseline is not None
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    assert _install_replaced(baseline) is False


def test_install_replaced_true_on_swap_and_on_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init = _fake_install(tmp_path)
    baseline = _fingerprint_for(init)
    assert baseline is not None
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )

    init.unlink()
    init.write_text("# v2 — a release differs in size\n", encoding="utf-8")
    assert _install_replaced(baseline) is True

    # Mid-swap window: the file is gone entirely. That IS the wedge —
    # must read as replaced, not as an error to ride out.
    init.unlink()
    assert _install_replaced(baseline) is True


def test_start_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_INSTALL_WATCHDOG", "0")
    assert start_install_watchdog() is None


def test_start_not_armed_for_source_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the test environment precis runs from the worktree, so the real
    ``install_fingerprint`` returns ``None`` and the watchdog must stay
    off — no thread, no baseline."""
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    monkeypatch.setattr(install_watchdog, "install_fingerprint", lambda: None)
    assert start_install_watchdog() is None


def test_start_arms_thread_for_real_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init = _fake_install(tmp_path)
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    thread = start_install_watchdog(interval_s=3600.0)
    assert isinstance(thread, threading.Thread)
    assert thread.daemon is True
    assert thread.is_alive()


def test_stop_ends_the_thread_without_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop()`` returns promptly and the loop never reaches ``os._exit``
    even though the baseline has become stale — the leak gr347099 hit."""
    init = _fake_install(tmp_path)
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    exits: list[int] = []
    monkeypatch.setattr(install_watchdog.os, "_exit", exits.append)
    thread = start_install_watchdog(interval_s=3600.0)
    assert thread is not None
    init.unlink()  # baseline now stale — a wake-up would exit
    thread.stop()
    assert not thread.is_alive()
    assert exits == []


def test_install_fingerprint_reads_the_precis_module_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public entry resolves through ``precis.__file__`` — pin that a
    watchable module file yields its fingerprint (kills the ``__file__ is
    None`` guard inverting into "never watchable")."""
    import precis

    init = _fake_install(tmp_path)
    monkeypatch.setattr(precis, "__file__", str(init))
    fp = install_watchdog.install_fingerprint()
    assert fp is not None
    assert fp[0] == str(init.resolve())


def test_watch_loop_exits_zero_on_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real thread loop against a fake install and capture the
    exit: replacement must produce exactly ``_exit(0)`` — the clean-exit
    code is what makes the client restart instead of back off."""
    init = _fake_install(tmp_path)
    codes: list[int] = []
    fired = threading.Event()

    def _fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()
        raise SystemExit  # end the watch thread in place of the process

    monkeypatch.setattr(install_watchdog.os, "_exit", _fake_exit)
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    thread = start_install_watchdog(interval_s=0.05)
    assert thread is not None

    init.unlink()
    init.write_text("# v2 — replaced by a deploy\n", encoding="utf-8")
    assert fired.wait(10.0), "watchdog never reacted to the replaced install"
    assert codes == [0]
    thread.join(5.0)


def test_exit_zero_is_the_contract() -> None:
    """The recoverable path the client log demonstrated is exit code 0
    ("MCP server process exited cleanly" → restart). Pin that the
    watchdog exits 0, not some error code a supervisor might back off
    on, by asserting the only ``os._exit`` call site uses 0."""
    source = Path(install_watchdog.__file__).read_text(encoding="utf-8")
    assert "os._exit(0)" in source
    # Every exit call is exit(0) — no non-zero path a supervisor might
    # treat as a crash loop.
    assert source.count("os._exit") == source.count("os._exit(0)")


def test_watchdog_wired_into_server_main() -> None:
    """server.main must arm the watchdog — a module nobody calls fixes
    nothing."""
    from precis import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "start_install_watchdog()" in source


def test_env_kill_switch_documented_value_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the literal '0' disables — '' or 'false' don't, so a typo'd
    export fails safe (watchdog stays on)."""
    init = _fake_install(tmp_path)
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    monkeypatch.setenv("PRECIS_INSTALL_WATCHDOG", "false")
    assert start_install_watchdog(interval_s=3600.0) is not None
    assert os.environ["PRECIS_INSTALL_WATCHDOG"] == "false"


# ---------------------------------------------------------------------------
# gr341515 — exit breadcrumb: the watchdog's exit was invisible past a
# stderr line no MCP client surfaces. These pin the write/consume contract
# `handlers/skill.py`'s precis-status renderer relies on.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _breadcrumb_in_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Redirect every breadcrumb read/write in this file at a tmp path —
    never touch the real ``~/.cache/precis`` (a named, persistent gate
    volume — see ``docker/dev/compose.yaml`` — so a real write here would
    leak stale breadcrumbs across unrelated gate runs).

    Also isolates the crash/exit hooks themselves: several existing tests
    call ``start_install_watchdog()``, which now unconditionally arms
    them (real ``sys.excepthook`` + a real ``atexit.register``). Reset
    ``_hooks_installed`` so each test gets a fresh armed/not-armed state
    to assert on, and unregister/restore afterward so no test leaks a
    live hook (or a real atexit callback pointed at the *un-patched*
    ``_breadcrumb_path``) into the rest of the suite.
    """
    target = tmp_path / "last-exit.json"
    monkeypatch.setattr(install_watchdog, "_breadcrumb_path", lambda: target)
    monkeypatch.setattr(install_watchdog, "_hooks_installed", False)
    monkeypatch.setattr(install_watchdog, "_crash_detail", None)
    original_excepthook = sys.excepthook
    yield target
    sys.excepthook = original_excepthook
    atexit.unregister(install_watchdog._atexit_breadcrumb)


def test_watchdog_exit_writes_breadcrumb_before_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _breadcrumb_in_tmp: Path
) -> None:
    """The install-swap exit path must drop the breadcrumb *before*
    ``os._exit`` — the watchdog fires it on a daemon thread, so once the
    real ``os._exit`` runs there is no further chance to flush anything."""
    init = _fake_install(tmp_path)
    baseline = _fingerprint_for(init)
    assert baseline is not None

    codes: list[int] = []
    fired = threading.Event()

    def _fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()
        raise SystemExit

    monkeypatch.setattr(install_watchdog.os, "_exit", _fake_exit)
    monkeypatch.setattr(
        install_watchdog, "install_fingerprint", lambda: _fingerprint_for(init)
    )
    monkeypatch.delenv("PRECIS_INSTALL_WATCHDOG", raising=False)
    thread = start_install_watchdog(interval_s=0.05)
    assert thread is not None

    init.unlink()
    init.write_text("# v2 — replaced by a deploy\n", encoding="utf-8")
    assert fired.wait(10.0), "watchdog never reacted to the replaced install"
    thread.join(5.0)

    assert codes == [0]
    payload = json.loads(_breadcrumb_in_tmp.read_text(encoding="utf-8"))
    assert payload["reason"] == "install-swapped"
    assert payload["old_fingerprint"] is not None
    assert payload["new_fingerprint"] is not None
    assert payload["old_fingerprint"] != payload["new_fingerprint"]
    assert "git_sha" in payload
    assert "written_at" in payload


def test_write_exit_breadcrumb_never_raises_on_a_bad_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Best-effort per the docstring: an unwritable target must not raise
    past the call — this runs on an exit path where an exception here
    would itself become the failure it's trying to record."""
    unwritable = tmp_path / "not-a-dir" / "last-exit.json"
    (tmp_path / "not-a-dir").write_text("blocks mkdir", encoding="utf-8")
    monkeypatch.setattr(install_watchdog, "_breadcrumb_path", lambda: unwritable)
    install_watchdog._write_exit_breadcrumb("install-swapped")  # must not raise


def test_excepthook_marks_crash_reason(
    monkeypatch: pytest.MonkeyPatch, _breadcrumb_in_tmp: Path
) -> None:
    """A genuine unhandled exception must produce ``reason="crash"`` with
    the exception named in ``detail``, and must still forward to the
    original hook so the traceback still reaches stderr."""
    monkeypatch.setattr(install_watchdog, "_hooks_installed", False)
    forwarded: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        install_watchdog,
        "_ORIGINAL_EXCEPTHOOK",
        lambda *a: forwarded.append(a),
    )
    monkeypatch.setattr(install_watchdog, "_crash_detail", None)
    install_exit_breadcrumb_hooks()
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    assert forwarded, "original excepthook was not chained"

    install_watchdog._atexit_breadcrumb()
    payload = json.loads(_breadcrumb_in_tmp.read_text(encoding="utf-8"))
    assert payload["reason"] == "crash"
    assert "ValueError" in payload["detail"]
    assert "boom" in payload["detail"]


def test_atexit_breadcrumb_reason_is_exit_absent_a_crash(
    monkeypatch: pytest.MonkeyPatch, _breadcrumb_in_tmp: Path
) -> None:
    """No exception seen this run → the atexit breadcrumb reads
    ``reason="exit"``, distinguishing a normal/graceful shutdown from
    both a crash and a watchdog-triggered ``install-swapped`` exit."""
    monkeypatch.setattr(install_watchdog, "_crash_detail", None)
    install_watchdog._atexit_breadcrumb()
    payload = json.loads(_breadcrumb_in_tmp.read_text(encoding="utf-8"))
    assert payload["reason"] == "exit"
    assert "detail" not in payload


def test_install_exit_breadcrumb_hooks_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call must not register a second ``atexit`` callback —
    every ``start_install_watchdog()`` call (including in this test
    file) calls it, so without the guard a long-lived process would
    write its exit breadcrumb once per call site."""
    monkeypatch.setattr(install_watchdog, "_hooks_installed", False)
    registrations: list[object] = []
    monkeypatch.setattr(install_watchdog.atexit, "register", registrations.append)
    install_exit_breadcrumb_hooks()
    install_exit_breadcrumb_hooks()
    assert len(registrations) == 1


def test_consume_last_exit_breadcrumb_reads_and_deletes(
    _breadcrumb_in_tmp: Path,
) -> None:
    """Consume-on-read is the age-out mechanism (gr341515 item 3, picked
    over a TTL check for the simpler invariant): the second read must
    come back empty even though nothing else changed."""
    _breadcrumb_in_tmp.parent.mkdir(parents=True, exist_ok=True)
    _breadcrumb_in_tmp.write_text(
        json.dumps({"reason": "install-swapped", "written_at": "2026-09-14T22:19:41Z"}),
        encoding="utf-8",
    )
    first = consume_last_exit_breadcrumb()
    assert first is not None
    assert first["reason"] == "install-swapped"
    assert not _breadcrumb_in_tmp.exists()

    assert consume_last_exit_breadcrumb() is None


def test_consume_last_exit_breadcrumb_missing_file(_breadcrumb_in_tmp: Path) -> None:
    assert consume_last_exit_breadcrumb() is None


def test_consume_last_exit_breadcrumb_corrupt_json(_breadcrumb_in_tmp: Path) -> None:
    """Malformed content must not raise — it's consumed (deleted) and
    treated as absent, same as a missing file."""
    _breadcrumb_in_tmp.parent.mkdir(parents=True, exist_ok=True)
    _breadcrumb_in_tmp.write_text("{not json", encoding="utf-8")
    assert consume_last_exit_breadcrumb() is None
    assert not _breadcrumb_in_tmp.exists()
