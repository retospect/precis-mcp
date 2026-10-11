"""Alertmanager webhook receiver: payload -> ops message, and the failure
direction (anything not queued answers non-2xx so Alertmanager retries)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from precis import alerts
from precis.workers import alertmanager_receiver as rcv


def _group(
    status: str = "firing", names: tuple[str, ...] = ("PostgresDown",)
) -> dict[str, Any]:
    return {
        "version": "4",
        "groupKey": '{}:{alertname="PostgresDown"}',
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": f"fp{i}",
                "labels": {
                    "alertname": n,
                    "severity": "critical",
                    "instance": "203.0.113.5:9100",
                },
                "annotations": {"summary": f"{n} is down"},
            }
            for i, n in enumerate(names)
        ],
    }


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    monkeypatch.setenv(alerts.OPS_ALERT_TARGET_ENV, "discord/1/2")
    calls: list[tuple[str, str, bool]] = []

    def fake(store, title, body, *, reason="", raise_on_error=False):
        calls.append((title, body, raise_on_error))
        return True

    monkeypatch.setattr(rcv, "queue_ops_message", fake)
    return calls


def test_firing_group_formats_title_and_lines() -> None:
    title, body, _ = rcv.format_group(_group(names=("A", "B")))
    assert "FIRING: A, B (2)" in title
    assert "A @​ 203.0.113.5:9100: A is down" in body
    assert body.splitlines()[0] == title


def test_resolved_group_says_resolved() -> None:
    title, _, _ = rcv.format_group(_group("resolved"))
    assert "RESOLVED" in title and "FIRING" not in title


def test_large_group_is_capped_and_counted() -> None:
    names = tuple(f"R{i}" for i in range(rcv.MAX_ALERTS_LISTED + 5))
    _, body, _ = rcv.format_group(_group(names=names))
    assert "... +5 more" in body
    assert len(body.splitlines()) == rcv.MAX_ALERTS_LISTED + 2


def test_mentions_are_defanged_and_lines_capped() -> None:
    g = _group(names=("@everyone", "<@&123>"))
    g["alerts"][0]["annotations"]["summary"] = "@here " + "x" * 5000
    title, body, _ = rcv.format_group(g)
    for text in (title, body):
        assert "@everyone" not in text and "<@&" not in text and "@here" not in text
        assert all(text[i + 1] == "\u200b" for i, c in enumerate(text[:-1]) if c == "@")
    assert all(len(line) <= rcv.MAX_LINE_CHARS for line in body.splitlines()[1:])


def test_handler_has_socket_timeout() -> None:
    server = rcv.make_server(object(), "127.0.0.1", 0)
    try:
        handler_cls: Any = server.RequestHandlerClass
        assert handler_cls.timeout == rcv.REQUEST_TIMEOUT_S
    finally:
        server.server_close()


@pytest.mark.parametrize(
    "bad", [[], "x", {}, {"alerts": []}, {"alerts": ["x"]}, {"alerts": None}]
)
def test_non_groups_are_400(bad: Any, sent: list) -> None:
    code, _ = rcv.handle_payload(object(), bad, rcv.Deduper())
    assert code == 400 and sent == []


def test_queue_success_and_repeat_dedupe(sent: list) -> None:
    d = rcv.Deduper()
    assert rcv.handle_payload(object(), _group(), d) == (200, "queued")
    assert rcv.handle_payload(object(), _group(), d)[0] == 200
    assert len(sent) == 1, "identical group inside the TTL queues once"
    assert sent[0][2] is True, "receiver must ask queue_ops_message to raise"
    # resolved is a different message, not a duplicate
    rcv.handle_payload(object(), _group("resolved"), d)
    assert len(sent) == 2


def test_dedupe_expires() -> None:
    now = [0.0]
    d = rcv.Deduper(ttl_s=10, clock=lambda: now[0])
    d.mark("k")
    assert d.seen("k")
    now[0] = 11
    assert not d.seen("k")


def test_no_target_is_503_and_not_marked_delivered(
    monkeypatch: pytest.MonkeyPatch, sent: list
) -> None:
    monkeypatch.delenv(alerts.OPS_ALERT_TARGET_ENV)
    d = rcv.Deduper()
    assert rcv.handle_payload(object(), _group(), d)[0] == 503
    assert sent == []
    monkeypatch.setenv(alerts.OPS_ALERT_TARGET_ENV, "discord/1/2")
    assert rcv.handle_payload(object(), _group(), d)[0] == 200, "retry not suppressed"


def test_queue_failure_is_500_and_retry_is_not_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(alerts.OPS_ALERT_TARGET_ENV, "discord/1/2")
    boom = [True]

    def flaky(store, title, body, *, reason="", raise_on_error=False):
        if boom[0]:
            raise RuntimeError("db down")
        return True

    monkeypatch.setattr(rcv, "queue_ops_message", flaky)
    d = rcv.Deduper()
    assert rcv.handle_payload(object(), _group(), d)[0] == 500
    boom[0] = False
    assert rcv.handle_payload(object(), _group(), d) == (200, "queued")


class _BrokenStore:
    def tx(self):
        raise RuntimeError("db down")


def test_queue_ops_message_raise_on_error_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(alerts.OPS_ALERT_TARGET_ENV, "discord/1/2")
    with pytest.raises(RuntimeError):
        alerts.queue_ops_message(
            cast(Any, _BrokenStore()), "t", "b", raise_on_error=True
        )
    # default stays best-effort for existing callers
    assert alerts.queue_ops_message(cast(Any, _BrokenStore()), "t", "b") is True


def _post(port: int, data: bytes, path: str = "/alerts") -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_round_trip(sent: list) -> None:
    server = rcv.make_server(object(), "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert _post(port, json.dumps(_group()).encode()) == 200
        assert len(sent) == 1
        assert _post(port, b"not json") == 400
        assert _post(port, b"") == 400
        assert _post(port, b"{}", path="/elsewhere") == 404
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_cli_refuses_routable_bind(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from precis.cli import alertmanager_receiver as cli

    with pytest.raises(SystemExit) as exc:
        cli.run(argparse.Namespace(host="::1", port=1, database_url=None))
    assert exc.value.code == 2
    assert "loopback" in capsys.readouterr().err
