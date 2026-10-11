"""Stall capture (gr465931): off by default, rate limiter, command shape."""

from __future__ import annotations

import httpx
import pytest

from precis.utils import stall_capture as sc

T0 = 1_800_000_000.0


def _timeout() -> Exception:
    return httpx.ConnectTimeout("_ssl.c:989: The handshake operation timed out")


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class _P:
        def wait(self):
            return 0

    def _popen(cmd, **kw):
        assert kw.get("shell") is None
        calls.append(cmd)
        return _P()

    monkeypatch.setattr(sc.subprocess, "Popen", _popen)
    monkeypatch.setattr(sc.shutil, "which", lambda _n: "/usr/sbin/tcpdump")
    monkeypatch.setattr(
        sc.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.0.2.7", 443))],
    )
    monkeypatch.setenv(sc.ENV_DIR, str(tmp_path))
    return calls


def test_off_by_default(monkeypatch, spawned):
    monkeypatch.delenv(sc.ENV_DIR)
    assert sc.maybe_capture(_timeout(), "https://doi.org/x", now=T0) is None
    assert spawned == []


def test_ignores_other_hosts_and_errors(spawned):
    assert sc.maybe_capture(_timeout(), "https://example.com/", now=T0) is None
    assert sc.maybe_capture(ValueError("x"), "doi.org", now=T0) is None
    assert spawned == []


def test_command_construction(spawned, tmp_path):
    pcap = sc.maybe_capture(_timeout(), "https://api.figshare.com/v2/x", now=T0)
    assert pcap == tmp_path / "stall-api.figshare.com-20270115T080000Z.pcap"
    (cmd,) = spawned
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert cmd[0] == "/usr/sbin/tcpdump"
    assert cmd[cmd.index("-G") + 1] == "30" and cmd[cmd.index("-W") + 1] == "1"
    assert cmd[cmd.index("-w") + 1] == str(pcap)
    assert cmd[-1] == "tcp port 443 and (host 192.0.2.7)"


def test_rate_limit(spawned):
    h = "api.crossref.org"
    assert sc.maybe_capture(_timeout(), h, now=T0)
    assert sc.maybe_capture(_timeout(), h, now=T0 + 600) is None  # < 1 h gap
    for i in range(1, 4):
        assert sc.maybe_capture(_timeout(), h, now=T0 + i * 3700)
    assert sc.maybe_capture(_timeout(), h, now=T0 + 4 * 3700) is None  # 4/day
    assert sc.maybe_capture(_timeout(), h, now=T0 + 86400 + 10)  # window rolls
    assert sc.maybe_capture(_timeout(), "doi.org", now=T0 + 600)  # per host


def test_missing_tcpdump_is_noop(monkeypatch, spawned):
    monkeypatch.setattr(sc.shutil, "which", lambda _n: None)
    assert sc.maybe_capture(_timeout(), "doi.org", now=T0) is None
    assert spawned == []
