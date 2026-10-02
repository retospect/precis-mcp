"""gr248866 — per-process NAS attestation (``meta.nas_ok_by_process``).

Entry builder, the heartbeat beat carrying its keyed entry, the nested-merge
in ``record_heartbeat``, the UPDATE-only ``record_nas_attestation`` store op,
the attest thread, and the ``nas-denied`` detector's per-process symptoms.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from precis.store import Store
from precis.workers import heartbeat as hb
from precis.workers.nursery import _detect_nas_denied, _nas_denied_remedy


@pytest.fixture(autouse=True)
def _not_in_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite itself runs in a container; pin the host case so entries
    are built (the container case has its own test)."""
    monkeypatch.setattr(hb, "_in_container", lambda: False)
    monkeypatch.setattr(hb, "_launch_class", None)


# ── who macOS holds responsible (launched_by) ───────────────────────


@pytest.mark.parametrize(
    ("resp", "launched_by", "grant_target"),
    [
        (None, "unknown", None),
        ("self", "launchd", os.path.realpath(sys.executable)),
        ((684, "/usr/libexec/sshd-session"), "ssh", None),
        (
            (
                500,
                "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
            ),
            "terminal",
            "/System/Applications/Utilities/Terminal.app",
        ),
        ((501, "/opt/homebrew/bin/tmux"), "other", "/opt/homebrew/bin/tmux"),
    ],
)
def test_tcc_launch_class(
    monkeypatch: pytest.MonkeyPatch,
    resp: Any,
    launched_by: str,
    grant_target: str | None,
) -> None:
    value = (os.getpid(), sys.executable) if resp == "self" else resp
    monkeypatch.setattr(hb, "_responsible_process", lambda: value)
    assert hb.tcc_launch_class() == {
        "launched_by": launched_by,
        "grant_target": grant_target,
    }


def test_entry_carries_launch_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hb, "_responsible_process", lambda: (684, "/usr/libexec/sshd-session")
    )
    e = hb.nas_attestation_entry({"nas_ok": False, "nas_path": "/opt/nas/x"})
    assert e is not None
    assert e["launched_by"] == "ssh"
    assert e["grant_target"] is None


def test_entry_in_container_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container's NAS comes through the VM's mount — no host grant to name."""
    monkeypatch.setattr(hb, "_in_container", lambda: True)
    assert hb.nas_attestation_entry({"nas_ok": False, "nas_path": "/x"}) is None


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({"launched_by": "ssh"}, "Allow full disk access for remote users"),
        (
            {"launched_by": "terminal", "grant_target": "/Applications/iTerm.app"},
            "/Applications/iTerm.app, the app that launched",
        ),
        ({"launched_by": "other", "grant_target": "/opt/x/bin/y"}, "to /opt/x/bin/y:"),
        (
            {"launched_by": "launchd", "grant_target": "/opt/py/bin/python3.14"},
            "Re-grant FDA to /opt/py/bin/python3.14",
        ),
        ({"exe": "/opt/py/bin/python3.12"}, "Re-grant FDA to /opt/py/bin/python3.12"),
    ],
)
def test_nas_denied_remedy_by_launch_class(entry: dict[str, Any], needle: str) -> None:
    assert needle in _nas_denied_remedy(entry)


# ── entry builder ───────────────────────────────────────────────────


def test_entry_ok() -> None:
    e = hb.nas_attestation_entry({"nas_ok": True, "nas_path": "/opt/nas/x"})
    assert e is not None
    assert e["ok"] is True
    assert e["path"] == "/opt/nas/x"
    assert e["exe"] == os.path.realpath(sys.executable)
    assert e["pid"] == os.getpid()
    ts = datetime.fromisoformat(e["ts"])
    assert ts.tzinfo is not None
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 60
    assert "errno" not in e and "err" not in e


def test_entry_denied_carries_errno() -> None:
    e = hb.nas_attestation_entry(
        {"nas_ok": False, "nas_path": "/opt/nas/x", "nas_errno": 1}
    )
    assert e is not None
    assert e["ok"] is False
    assert e["errno"] == 1


def test_entry_transient_is_none_ok() -> None:
    e = hb.nas_attestation_entry({"nas_probe_err": "timeout", "nas_path": "/opt/nas/x"})
    assert e is not None
    assert e["ok"] is None
    assert e["err"] == "timeout"


def test_entry_absent_path_is_none() -> None:
    assert hb.nas_attestation_entry({}) is None


# ── heartbeat beat ──────────────────────────────────────────────────


class _RecordingStore:
    def __init__(self) -> None:
        self.meta: dict[str, Any] | None = None

    def record_heartbeat(self, host: str, **kwargs: Any) -> None:
        self.meta = kwargs.get("meta")


def test_collect_and_upsert_puts_keyed_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[int] = []

    def _probe() -> dict[str, Any]:
        probes.append(1)
        return {"nas_ok": False, "nas_path": "/opt/nas/x", "nas_errno": 1}

    monkeypatch.setattr(hb, "_probe_nas", _probe)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker")
    store: Any = _RecordingStore()
    hb._collect_and_upsert(store, "h")
    assert store.meta is not None
    assert len(probes) == 1
    assert store.meta["nas_ok"] is False  # legacy field kept
    entry = store.meta["nas_ok_by_process"]["precis-worker"]
    assert entry["ok"] is False and entry["errno"] == 1


def test_collect_and_upsert_default_key_and_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    monkeypatch.setattr(hb, "_probe_nas", lambda: {"nas_ok": True, "nas_path": "/p"})
    store: Any = _RecordingStore()
    hb._collect_and_upsert(store, "h")
    assert store.meta is not None
    assert "precis-heartbeat" in store.meta["nas_ok_by_process"]

    monkeypatch.setattr(hb, "_probe_nas", lambda: {})
    store2: Any = _RecordingStore()
    hb._collect_and_upsert(store2, "h")
    assert store2.meta is not None
    assert "nas_ok_by_process" not in store2.meta


# ── store ops (DB) ──────────────────────────────────────────────────


def _host() -> str:
    return f"na-{uuid4().hex[:8]}"


def _entry(ok: bool | None, *, ts: datetime | str | None = None, **kw: Any) -> dict:
    if ts is None:
        ts = datetime.now(UTC)
    return {
        "ok": ok,
        "path": "/opt/nas/botshome",
        "exe": "/opt/venv/bin/python3.13",
        "pid": 1,
        "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
        **kw,
    }


def _get(store: Store, host: str):
    return next(r for r in store.recent_heartbeats() if r.host == host)


def test_record_heartbeat_merges_nas_entries(store: Store) -> None:
    host = _host()
    a = _entry(False, errno=1)
    b = _entry(True)
    store.record_heartbeat(host, meta={"nas_ok_by_process": {"proc-a": a}})
    store.record_heartbeat(host, meta={"nas_ok_by_process": {"proc-b": b}})
    got = _get(store, host).meta["nas_ok_by_process"]
    assert got["proc-a"]["ok"] is False
    assert got["proc-b"]["ok"] is True
    store.record_heartbeat(host, meta={"nas_ok_by_process": {"proc-b": _entry(False)}})
    got = _get(store, host).meta["nas_ok_by_process"]
    assert got["proc-b"]["ok"] is False and got["proc-a"]["ok"] is False


def test_record_nas_attestation_updates_without_ts_bump(store: Store) -> None:
    host = _host()
    store.record_heartbeat(host, meta={"platform": "Darwin"})
    ts0 = _get(store, host).ts
    time.sleep(0.05)
    assert store.record_nas_attestation(host, "precis-web", _entry(False, errno=1))
    hbrow = _get(store, host)
    assert hbrow.ts == ts0
    assert hbrow.meta["platform"] == "Darwin"
    assert hbrow.meta["nas_ok_by_process"]["precis-web"]["ok"] is False
    assert store.record_nas_attestation(host, "precis-serve", _entry(True))
    got = _get(store, host).meta["nas_ok_by_process"]
    assert set(got) == {"precis-web", "precis-serve"}


def test_record_nas_attestation_unknown_host_no_insert(store: Store) -> None:
    host = _host()
    assert store.record_nas_attestation(host, "precis-web", _entry(True)) is False
    assert not [r for r in store.recent_heartbeats() if r.host == host]


# ── attest thread ───────────────────────────────────────────────────


class _AttestStore:
    def __init__(self, landed: bool = True) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.landed = landed

    def record_nas_attestation(self, host: str, process: str, entry: dict) -> bool:
        self.calls.append((host, process, entry))
        return self.landed


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_attest_thread_ticks_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_probe_nas", lambda: {"nas_ok": True, "nas_path": "/p"})
    monkeypatch.setattr(hb, "_NAS_ATTEST_INTERVAL_SECONDS", 0.05)
    monkeypatch.delenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    monkeypatch.setenv("PRECIS_HOST_NAME", "hh")
    stop = threading.Event()
    store = _AttestStore()
    t = hb.start_nas_attest_thread(
        store, default_process="precis-web", should_stop=stop.is_set
    )
    assert _wait(lambda: len(store.calls) >= 1)
    host, proc, entry = store.calls[0]
    assert (host, proc, entry["ok"]) == ("hh", "precis-web", True)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_attest_thread_warns_once_when_no_row(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hb, "_probe_nas", lambda: {"nas_ok": True, "nas_path": "/p"})
    monkeypatch.setattr(hb, "_NAS_ATTEST_INTERVAL_SECONDS", 0.02)
    monkeypatch.delenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("PRECIS_HOST_NAME", "caspar-x")
    stop = threading.Event()
    store = _AttestStore(landed=False)
    with caplog.at_level("WARNING", logger=hb.log.name):
        t = hb.start_nas_attest_thread(
            store, default_process="precis-serve", should_stop=stop.is_set
        )
        assert _wait(lambda: len(store.calls) >= 3, timeout=10)
        stop.set()
        t.join(timeout=5)
    warns = [r for r in caplog.records if "nowhere to land" in r.getMessage()]
    assert len(warns) == 1
    assert "caspar-x" in warns[0].getMessage()


def test_attest_thread_survives_store_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_probe_nas", lambda: {"nas_ok": True, "nas_path": "/p"})
    monkeypatch.setattr(hb, "_NAS_ATTEST_INTERVAL_SECONDS", 0.02)
    monkeypatch.delenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", raising=False)
    n = {"c": 0}

    class _Boom:
        def record_nas_attestation(self, *a: Any) -> bool:
            n["c"] += 1
            raise RuntimeError("db down")

    stop = threading.Event()
    t = hb.start_nas_attest_thread(
        _Boom(), default_process="p", should_stop=stop.is_set
    )
    assert _wait(lambda: n["c"] >= 2)
    stop.set()
    t.join(timeout=5)


def test_attest_interval_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_NAS_ATTEST_INTERVAL_SECONDS", 600.0)
    monkeypatch.setenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", "bogus")
    assert hb._nas_attest_interval_s() == 600.0
    monkeypatch.setenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", "-3")
    assert hb._nas_attest_interval_s() == 600.0
    monkeypatch.setenv("PRECIS_NAS_ATTEST_INTERVAL_SECONDS", "30")
    assert hb._nas_attest_interval_s() == 30.0


# ── detector (DB) ───────────────────────────────────────────────────


def _seed(store: Store, host: str, meta: dict, minutes_ago: float = 0.0) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) "
            "VALUES (%s, now() - (%s || ' minutes')::interval, %s::jsonb) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts, meta = EXCLUDED.meta",
            (host, minutes_ago, json.dumps(meta)),
        )
        conn.commit()


def _hits(store: Store, host: str):
    return [
        f
        for f in _detect_nas_denied(store)
        if f"nas-denied:{host}" in (f.fingerprint_key or "")
    ]


def test_detector_fresh_denied_entry_one_symptom(store: Store) -> None:
    host = _host()
    _seed(
        store,
        host,
        {"nas_ok": True, "nas_ok_by_process": {"precis-web": _entry(False, errno=1)}},
    )
    hits = _hits(store, host)
    assert len(hits) == 1
    s = hits[0]
    assert s.fingerprint_key == f"nas-denied:{host}:precis-web"
    assert s.title == f"precis-web on {host} locked out of the NAS"
    assert "/opt/venv/bin/python3.13" in s.detail
    assert "/opt/nas/botshome" in s.detail
    assert "precis-web" in s.detail


def test_detector_stale_ok_and_transient_entries_silent(store: Store) -> None:
    host = _host()
    old = datetime.now(UTC) - timedelta(minutes=45)
    _seed(
        store,
        host,
        {
            "nas_ok_by_process": {
                "stale": _entry(False, ts=old),
                "good": _entry(True),
                "flaky": _entry(None, err="timeout"),
                "garbled": _entry(False, ts="not-a-date"),
            }
        },
    )
    assert _hits(store, host) == []


def test_detector_malformed_entry_does_not_break_others(store: Store) -> None:
    host = _host()
    _seed(
        store,
        host,
        {
            "nas_ok_by_process": {
                "junk": "string",
                "z": _entry(False, ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            }
        },
    )
    hits = _hits(store, host)
    assert [h.fingerprint_key for h in hits] == [f"nas-denied:{host}:z"]


def test_detector_legacy_only_row_alerts(store: Store) -> None:
    host = _host()
    _seed(store, host, {"nas_ok": False, "nas_path": "/opt/nas/botshome"})
    hits = _hits(store, host)
    assert [h.fingerprint_key for h in hits] == [f"nas-denied:{host}"]


def test_detector_legacy_suppressed_when_by_process_present(store: Store) -> None:
    host = _host()
    _seed(
        store,
        host,
        {
            "nas_ok": False,
            "nas_path": "/opt/nas/botshome",
            "nas_ok_by_process": {"precis-worker": _entry(True)},
        },
    )
    assert _hits(store, host) == []
