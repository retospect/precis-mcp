"""The session MCP's served-sha stamp and the watchdog's pending update (gr685985)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from precis import mcp_served_stamp as mss
from precis import settings as psettings
from precis import worktree_server
from precis.install_watchdog import CheckoutWatchdog
from precis.store import Store

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
NEW = "beef" + "2" * 36
T0 = datetime(2026, 11, 20, 5, 0, 0, tzinfo=UTC)


def _lane(monkeypatch: pytest.MonkeyPatch, lane: str, sha: str | None = SHA) -> None:
    from precis.handlers import skill

    info = {"git_sha": sha} if sha else {}
    monkeypatch.setattr(skill, "_git_identity_lane", lambda: (lane, info))
    monkeypatch.delenv(worktree_server.ENV, raising=False)
    monkeypatch.setenv("PRECIS_CHECKOUT_WATCHDOG", "/src")


def test_a_watched_checkout_server_writes_the_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    assert mss.write_stamp(store, T0, now=T0)
    psettings.invalidate()
    rec = mss.read_stamp(store)
    assert rec is not None
    assert rec["sha"] == SHA and rec["served_from"] == "watched-checkout"
    assert rec["started_at"] == "2026-11-20T05:00:00Z" and rec["at"] == T0
    assert "pending_sha" not in rec
    assert rec["instance"].endswith(f":{os.getpid()}")


def test_a_pending_sha_is_carried_only_when_it_differs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    assert mss.write_stamp(store, T0, now=T0, pending_sha=NEW)
    psettings.invalidate()
    rec = mss.read_stamp(store)
    assert rec is not None and rec["pending_sha"] == NEW
    assert "pending_sha" not in mss.build_record(T0, pending_sha=SHA)


@pytest.mark.parametrize(
    "lane", ["working-tree", "image+mount", "image-build", "vcs-install"]
)
def test_a_dev_or_image_server_does_not_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    _lane(monkeypatch, lane)
    assert not mss.stamps_this_process("streamable-http")
    assert mss.write_stamp(store, T0) is False
    assert mss.read_stamp(store) is None


def test_a_watched_checkout_server_without_the_watchdog_env_does_not_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.delenv("PRECIS_CHECKOUT_WATCHDOG")
    assert not mss.stamps_this_process("streamable-http")
    assert mss.write_stamp(store, T0) is False
    assert mss.read_stamp(store) is None


def test_a_worktree_or_stdio_server_does_not_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    assert mss.stamps_this_process("streamable-http")
    assert not mss.stamps_this_process("stdio")
    monkeypatch.setenv(worktree_server.ENV, "my-tree")
    assert not mss.stamps_this_process("streamable-http")
    assert mss.write_stamp(store, T0) is False
    assert mss.start_served_stamp_thread(store, transport="streamable-http") is None


def test_a_store_error_never_raises(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(psettings, "set_setting", boom)
    assert mss.write_stamp(store, T0) is False
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_pending", None)
    t = mss.note_pending(NEW)  # must not raise
    assert t is not None
    t.join(5)


def test_a_malformed_record_reads_as_none(store: Store) -> None:
    for raw in ("not json", "[]", json.dumps({"sha": SHA}), json.dumps({"at": "x"})):
        psettings.set_setting(mss.STAMP_KEY, raw, store=store)
        assert mss.read_stamp(store) is None


def test_note_pending_updates_the_stamp_and_is_a_noop_without_a_stamper(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_state", None)
    assert mss.note_pending(NEW) is None
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_pending", None)
    t = mss.note_pending(NEW)
    assert t is not None
    t.join(10)
    psettings.invalidate()
    rec = mss.read_stamp(store)
    assert rec is not None and rec["sha"] == SHA and rec["pending_sha"] == NEW


def test_note_pending_returns_without_waiting_for_a_slow_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lane(monkeypatch, "watched-checkout")
    release = threading.Event()

    def slow(*_a: Any, **_k: Any) -> None:
        release.wait(30)

    monkeypatch.setattr(psettings, "set_setting", slow)
    monkeypatch.setattr(mss, "_state", (object(), T0))
    monkeypatch.setattr(mss, "_pending", None)
    t0 = time.monotonic()
    t = mss.note_pending(NEW)
    assert time.monotonic() - t0 < 1.0
    assert t is not None and t.is_alive()  # still blocked on the store
    release.set()
    t.join(5)


def test_a_refresh_after_note_pending_still_carries_pending_sha(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh that lands after the pending write must not erase it."""
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_pending", None)
    t = mss.note_pending(NEW)
    assert t is not None
    t.join(10)
    assert mss.write_stamp(store, T0)  # the periodic refresh: no pending argument
    psettings.invalidate()
    rec = mss.read_stamp(store)
    assert rec is not None and rec["pending_sha"] == NEW


def _watchdog(tmp_path: Any) -> CheckoutWatchdog:
    return CheckoutWatchdog(root=tmp_path, baseline=SHA, interval_s=1.0)


def test_the_watchdog_deferral_path_stamps_the_new_head_once(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import install_watchdog as iw

    calls: list[str] = []
    order: list[str] = []

    def note(sha: str) -> bool:
        order.append("stamp")
        calls.append(sha)
        return True

    monkeypatch.setattr(mss, "note_pending", note)
    monkeypatch.setattr(iw, "drain_then_exit", lambda **_k: order.append("exit"))
    _watchdog(tmp_path)._bounce(NEW)
    assert calls == [NEW] and order == ["stamp", "exit"]


def test_a_failing_pending_update_does_not_disturb_the_watchdog(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import install_watchdog as iw

    exits: list[str] = []

    def boom(_sha: str) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(mss, "note_pending", boom)
    monkeypatch.setattr(iw, "drain_then_exit", lambda **k: exits.append(k["reason"]))
    _watchdog(tmp_path)._bounce(NEW)  # must not raise
    assert exits == ["checkout-changed"]
