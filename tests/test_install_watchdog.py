"""Install watchdog (gr338977) — a live server whose venv is replaced
must decide "exit cleanly", never wedge. These pin the decision logic;
the thread itself is a sleep-loop around :func:`_install_replaced` plus
``os._exit`` and isn't run here."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from precis import install_watchdog
from precis.install_watchdog import (
    _fingerprint_for,
    _install_replaced,
    start_install_watchdog,
)


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
