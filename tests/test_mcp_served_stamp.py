"""The session MCP's served-sha stamp and the watchdog's pending update (gr685985)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
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


def _mine(store: Store) -> dict[str, Any] | None:
    """This process's own per-instance record (gr762430)."""
    mine = [r for r in mss.read_stamps(store) if r["instance"] == mss._instance()]
    return mine[0] if mine else None


def test_each_instance_writes_its_own_key_and_never_the_legacy_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr762430: two stamping servers no longer overwrite one record."""
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_instance", lambda: "gw-mcp:7")
    assert mss.write_stamp(store, T0, now=T0)
    monkeypatch.setattr(mss, "_instance", lambda: "laptop-dev:31043")
    assert mss.write_stamp(store, T0, now=T0)
    recs = mss.read_stamps(store)
    assert [r["instance"] for r in recs] == ["gw-mcp:7", "laptop-dev:31043"]
    with store.pool.connection() as conn:
        keys = [
            str(k)
            for (k,) in conn.execute(
                "SELECT key FROM app_settings WHERE key LIKE 'release.mcp%' "
                "ORDER BY key"
            ).fetchall()
        ]
    assert keys == [
        "release.mcp_served.gw-mcp:7",
        "release.mcp_served.laptop-dev:31043",
    ]


def test_at_is_the_store_clock_so_a_writer_far_behind_is_still_live(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr762430 ruling: ``at`` comes from the store's ``now()``; a writer
    whose own clock runs two hours behind still counts as live."""

    class _Behind(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.now(tz) - timedelta(hours=2)

    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_instance", lambda: "behind-host:3")
    monkeypatch.setattr(mss, "datetime", _Behind)
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_pending", None)
    assert mss.write_stamp(store, T0)
    with store.pool.connection() as conn:
        row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    (rec,) = mss.read_stamps(store)
    assert abs((rec["at"] - row[0]).total_seconds()) < 60  # the store's clock
    live = mss.live_stamps(store)
    assert [r["instance"] for r in live] == ["behind-host:3"]


def test_forget_drops_only_this_instance_and_stops_later_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_pending", None)
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_instance", lambda: "other:1")
    assert mss.write_stamp(store, T0)
    monkeypatch.setattr(mss, "_instance", lambda: "mine:2")
    assert mss.write_stamp(store, T0)
    assert mss.forget_own_stamp() is True
    assert [r["instance"] for r in mss.read_stamps(store)] == ["other:1"]
    assert mss.write_stamp(store, T0) is False
    t = mss.note_pending(NEW)  # its write is refused too
    assert t is not None
    t.join(10)
    assert [r["instance"] for r in mss.read_stamps(store)] == ["other:1"]


def test_forget_is_bounded_and_a_noop_without_a_stamper(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_state", None)
    assert mss.forget_own_stamp() is False
    release = threading.Event()

    def slow(*_a: Any, **_k: Any) -> None:
        release.wait(30)

    monkeypatch.setattr(psettings, "clear_setting", slow)
    monkeypatch.setattr(mss, "_state", (store, T0))
    t0 = time.monotonic()
    assert mss.forget_own_stamp(timeout_s=0.5) is False
    assert time.monotonic() - t0 < 2.0
    release.set()


def test_the_drain_signal_keeps_the_stamp_until_the_exit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docker stop`` → the supervisor's SIGHUP → the drain handler. A
    call just ended, so the real quiet window defers the exit — and the
    server still serves while it waits, so the key must stay: dropped at
    drain start it would hide a live stale instance from the release
    confirm (td765317). The drain exit drops it; a SIGKILL before then
    leaves it to age out — the safe direction."""
    import signal

    from precis import inflight
    from precis import install_watchdog as iw

    if not hasattr(signal, "SIGHUP"):
        pytest.skip("no SIGHUP on this platform")
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_pending", None)
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_instance", lambda: "drain-host:4")
    assert mss.write_stamp(store, T0)

    inflight._reset_for_tests()
    inflight.leave(inflight.enter())  # a call just ended
    quiet = iw.resolved_respawn_quiet_s()
    # the drain would defer, and for its whole 2 s cap below
    assert quiet > 2.0 and inflight.busy(quiet)

    exits: list[int] = []

    def fake_exit(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    handlers: dict[int, Any] = {}
    monkeypatch.setattr(iw, "_write_exit_breadcrumb", lambda *a, **k: None)
    monkeypatch.setattr(iw.os, "_exit", fake_exit)
    monkeypatch.setattr(iw.signal, "signal", lambda sig, h: handlers.setdefault(sig, h))
    if threading.current_thread() is not threading.main_thread():
        me = threading.current_thread()
        monkeypatch.setattr(iw.threading, "main_thread", lambda: me)
    try:
        assert iw.install_drain_signal(drain_timeout_s=0.1, defer_cap_s=2.0)
        signalled = time.monotonic()
        handlers[iw.DRAIN_SIGNAL](iw.DRAIN_SIGNAL, None)
        drain = [t for t in threading.enumerate() if t.name == "drain-restart"]
        assert len(drain) == 1
        # The deferral runs its full ~2 s cap (the quiet window is longer),
        # so the stamp must still be there well into it, not just at the
        # signal. ``exits`` is read before the stamps: the exit can land
        # between any two reads, but the forget always precedes it, so an
        # exit seen first means the key must already be gone.
        seen_live_late = False
        while drain[0].is_alive():
            exited = bool(exits)
            elapsed = time.monotonic() - signalled
            live = any(r["instance"] == "drain-host:4" for r in mss.read_stamps(store))
            if exited:
                assert not live  # dropped before the exit, never after
            elif live and elapsed >= 1.0:
                seen_live_late = True
            time.sleep(0.05)
        drain[0].join(15)
        assert seen_live_late  # still live 1 s into the ~2 s deferral
        assert exits == [0]
        assert mss.read_stamps(store) == []
    finally:
        inflight._reset_for_tests()


def test_forget_deletes_inline_when_no_thread_can_start(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.12+ refuses ``Thread.start`` during interpreter shutdown —
    exactly where ``atexit`` calls forget. It deletes inline instead of
    leaving the stamp behind (td765317)."""
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_pending", None)
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_instance", lambda: "atexit-host:6")
    assert mss.write_stamp(store, T0)

    def no_threads(self: Any) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(threading.Thread, "start", no_threads)
    assert mss.forget_own_stamp() is True
    assert mss.read_stamps(store) == []


def test_a_late_write_after_forget_does_not_restore_the_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write slow on the store clock outlives forget's lock wait: it
    re-checks ``_stopped`` right before writing and writes nothing."""
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.setattr(mss, "_stopped", False)
    monkeypatch.setattr(mss, "_pending", None)
    monkeypatch.setattr(mss, "_state", (store, T0))
    monkeypatch.setattr(mss, "_instance", lambda: "late:5")
    entered, release = threading.Event(), threading.Event()
    real_now = mss._db_now

    def slow_now(s: Any) -> datetime:
        entered.set()
        release.wait(10)
        return real_now(s)

    monkeypatch.setattr(mss, "_db_now", slow_now)
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(mss.write_stamp(store, T0)))
    t.start()
    assert entered.wait(5)
    assert mss.forget_own_stamp(timeout_s=0.2) is True  # lock wait timed out
    release.set()
    t.join(10)
    assert result == [False]
    assert mss.read_stamps(store) == []


def test_the_drain_exit_drops_the_stamp_before_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis import install_watchdog as iw

    order: list[str] = []
    monkeypatch.setattr(mss, "forget_own_stamp", lambda: order.append("forget"))
    monkeypatch.setattr(iw, "_write_exit_breadcrumb", lambda *a, **k: None)

    def fake_exit(code: int) -> None:
        order.append(f"exit {code}")
        raise SystemExit(code)

    monkeypatch.setattr(iw.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        iw.drain_then_exit(reason="r", detail="d", why="w", drain_timeout_s=0.1)
    assert order == ["forget", "exit 0"]


def test_a_malformed_instance_row_is_skipped(store: Store) -> None:
    psettings.set_setting(mss.stamp_key("bad:1"), "not json", store=store)
    rec = {"sha": SHA, "at": "2026-11-20T05:00:00Z"}
    psettings.set_setting(mss.stamp_key("good:2"), json.dumps(rec), store=store)
    recs = mss.read_stamps(store)
    assert [r["instance"] for r in recs] == ["good:2"]  # named from its key


def test_a_stamper_start_prunes_day_old_instance_records(store: Store) -> None:
    old = {"sha": SHA, "at": "2026-11-18T05:00:00Z", "instance": "gone:1"}
    new = {"sha": SHA, "at": "2026-11-20T04:00:00Z", "instance": "live:2"}
    psettings.set_setting(mss.stamp_key("gone:1"), json.dumps(old), store=store)
    psettings.set_setting(mss.stamp_key("live:2"), json.dumps(new), store=store)
    assert mss.prune_stamps(store, now=T0) == 1
    assert [r["instance"] for r in mss.read_stamps(store)] == ["live:2"]


def test_the_legacy_single_key_is_never_consulted(store: Store) -> None:
    """gr764909: a leftover ``release.mcp_served`` row (the pre-gr762430
    last-writer key) is invisible — readers consult only
    ``release.mcp_served.<host:pid>`` records, so a stray instance's
    legacy stamp can no longer mask or stand for a live server."""
    fresh = datetime.now(UTC).strftime(mss._TS_FMT)
    legacy = {"sha": SHA, "at": fresh, "instance": "laptop-dev:9"}
    psettings.set_setting("release.mcp_served", json.dumps(legacy), store=store)
    assert mss.read_stamps(store) == []
    assert mss.live_stamps(store) == []
    # Beside live per-instance records it is not merged in either, even
    # under an instance name no per-instance record carries.
    rec = {"sha": SHA, "at": fresh, "instance": "gw-mcp:7"}
    psettings.set_setting(mss.stamp_key("gw-mcp:7"), json.dumps(rec), store=store)
    assert [r["instance"] for r in mss.live_stamps(store)] == ["gw-mcp:7"]
    # And the prune never touches it (migration 0363 deletes the row).
    legacy["at"] = (T0 - mss.PRUNE_AFTER - timedelta(seconds=1)).strftime(mss._TS_FMT)
    psettings.set_setting("release.mcp_served", json.dumps(legacy), store=store)
    assert mss.prune_stamps(store, now=T0) == 0
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM app_settings WHERE key = 'release.mcp_served'"
        ).fetchone()
    assert row is not None and row[0] == 1


def test_a_watched_checkout_server_writes_the_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    assert mss.write_stamp(store, T0, now=T0)
    psettings.invalidate()
    rec = _mine(store)
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
    rec = _mine(store)
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
    assert mss.read_stamps(store) == []


def test_a_watched_checkout_server_without_the_watchdog_env_does_not_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lane(monkeypatch, "watched-checkout")
    monkeypatch.delenv("PRECIS_CHECKOUT_WATCHDOG")
    assert not mss.stamps_this_process("streamable-http")
    assert mss.write_stamp(store, T0) is False
    assert mss.read_stamps(store) == []


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


def test_a_malformed_record_is_skipped(store: Store) -> None:
    for raw in ("not json", "[]", json.dumps({"sha": SHA}), json.dumps({"at": "x"})):
        psettings.set_setting(mss.stamp_key("bad:1"), raw, store=store)
        assert mss.read_stamps(store) == []


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
    rec = _mine(store)
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
    monkeypatch.setattr(mss, "_db_now", lambda _store: T0)
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
    rec = _mine(store)
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
