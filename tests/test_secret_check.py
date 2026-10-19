"""gr496305 item 4: the scheduled weekly credential check
(``workers/secret_check.py``) fired from the ``secret_check`` cadence.

Two layers, no network: the state tests replace ``run_checks`` with a
canned outcome map; the leak tests run the REAL probe functions against
an ``httpx.MockTransport`` with every secret resolving to a sentinel, so
a secret-bearing path executes end to end and the sentinel is asserted
absent from alert text, alert meta, log records and exception reprs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from precis import secret_status
from precis.secret_status import CheckResult
from precis.workers.scheduler import CADENCES
from precis.workers.secret_check import ALERT_SOURCE, run_secret_check_pass

_SENTINEL = "sk-SENTINEL-must-not-leak"
_BAD = CheckResult("bad", "HTTP 401 — key rejected")
_OK = CheckResult("ok", "verified")


def _patch_report(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, CheckResult],
    *,
    absent: tuple[str, ...] = (),
) -> None:
    """Canned probe outcomes (a name absent from the map classifies
    unprobed, never failed); names in ``absent`` report missing."""

    async def fake_run_checks(_store: Any) -> dict[str, CheckResult]:
        return results

    monkeypatch.setattr(secret_status, "run_checks", fake_run_checks)
    monkeypatch.setattr(
        secret_status, "is_available", lambda n, store=None: n not in absent
    )
    monkeypatch.setattr(secret_status, "get_secret", lambda n, store=None: _SENTINEL)


def _patch_real_probes(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """The real probe functions run against ``handler``; every secret
    resolves to the sentinel and retries never sleep. The ``mailto`` probe
    is excluded — it is a local format check with no wire to leak onto."""

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=secret_status._PROBE_TIMEOUT_S,
            headers={"User-Agent": "precis-test"},
            event_hooks={"response": [secret_status._raise_retryable]},
        )

    async def _no_sleep(_s: float) -> None:
        return None

    real_backoff = secret_status.async_call_with_backoff
    monkeypatch.setattr(secret_status, "_probe_client", client)
    monkeypatch.setattr(secret_status, "_probed", {})
    monkeypatch.setattr(
        secret_status,
        "async_call_with_backoff",
        lambda fn, **kw: real_backoff(fn, sleep=_no_sleep, **kw),
    )
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://probe.invalid")
    monkeypatch.setattr(
        secret_status,
        "is_available",
        lambda n, store=None: n != "ACATOME_CROSSREF_MAILTO",
    )
    monkeypatch.setattr(secret_status, "get_secret", lambda n, store=None: _SENTINEL)


def _open_alerts(store: Any, name: str) -> list[tuple[Any, ...]]:
    with store.pool.connection() as c:
        return c.execute(
            "SELECT ref_id, title, meta->>'detail' FROM refs "
            "WHERE kind = 'alert' AND alert_source = %s "
            "AND fingerprint = %s AND resolved_at IS NULL",
            (ALERT_SOURCE, f"failed:{name}"),
        ).fetchall()


def _all_alerts(store: Any) -> list[tuple[Any, ...]]:
    with store.pool.connection() as c:
        return c.execute(
            "SELECT title, meta::text FROM refs "
            "WHERE kind = 'alert' AND alert_source = %s",
            (ALERT_SOURCE,),
        ).fetchall()


def _count(store: Any, *, resolved: bool) -> int:
    with store.pool.connection() as c:
        return c.execute(
            "SELECT count(*) FROM refs WHERE kind = 'alert' "
            "AND alert_source = %s AND resolved_at IS "
            + ("NOT NULL" if resolved else "NULL"),
            (ALERT_SOURCE,),
        ).fetchone()[0]


def test_failed_credential_raises_and_dedupes_one_alert(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _BAD})
    r = run_secret_check_pass(store)
    assert r.claimed == len(secret_status.KNOWN_SECRETS)
    assert r.failed == 1
    (alert,) = _open_alerts(store, "ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" in alert[1]
    assert "401" in alert[2]
    assert "console.anthropic.com/settings/keys" in alert[2]

    run_secret_check_pass(store)  # still failing — dedup, no second alert
    assert len(_open_alerts(store, "ANTHROPIC_API_KEY")) == 1


def test_each_failed_credential_gets_its_own_alert(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    _patch_report(
        monkeypatch, {"ANTHROPIC_API_KEY": _BAD, "SEMANTIC_SCHOLAR_API_KEY": _BAD}
    )
    r = run_secret_check_pass(store)
    assert r.failed == 2
    assert len(_open_alerts(store, "ANTHROPIC_API_KEY")) == 1
    assert len(_open_alerts(store, "SEMANTIC_SCHOLAR_API_KEY")) == 1


@pytest.mark.parametrize("non_ok", ["unprobed", "missing"])
def test_only_a_verified_ok_resolves_the_alert(
    monkeypatch: pytest.MonkeyPatch, store: Any, non_ok: str
) -> None:
    """An outage, a 5xx, a 429 or a host where the key is missing must NOT
    silently resolve a real 'key rejected' alert — only ``ok`` does."""
    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _BAD})
    run_secret_check_pass(store)
    assert len(_open_alerts(store, "ANTHROPIC_API_KEY")) == 1

    if non_ok == "unprobed":
        _patch_report(
            monkeypatch,
            {"ANTHROPIC_API_KEY": CheckResult("unknown", "timeout — could not verify")},
        )
    else:
        _patch_report(monkeypatch, {}, absent=("ANTHROPIC_API_KEY",))
    run_secret_check_pass(store)
    assert len(_open_alerts(store, "ANTHROPIC_API_KEY")) == 1  # still open

    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _OK})
    run_secret_check_pass(store)
    assert _open_alerts(store, "ANTHROPIC_API_KEY") == []
    assert _count(store, resolved=True) == 1


def test_passing_again_resolves_only_the_passing_alert(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    _patch_report(
        monkeypatch, {"ANTHROPIC_API_KEY": _BAD, "SEMANTIC_SCHOLAR_API_KEY": _BAD}
    )
    run_secret_check_pass(store)
    assert _count(store, resolved=False) == 2

    _patch_report(
        monkeypatch, {"ANTHROPIC_API_KEY": _OK, "SEMANTIC_SCHOLAR_API_KEY": _BAD}
    )
    run_secret_check_pass(store)
    assert _open_alerts(store, "ANTHROPIC_API_KEY") == []
    assert len(_open_alerts(store, "SEMANTIC_SCHOLAR_API_KEY")) == 1
    assert _count(store, resolved=True) == 1


def _assert_no_sentinel(
    store: Any, caplog: pytest.LogCaptureFixture, sent: list[str]
) -> None:
    """The key went on the wire (the path isn't vacuous) and is in no alert
    title, no alert meta, and no log record."""
    assert any(_SENTINEL in s for s in sent)
    alerts = _all_alerts(store)
    assert alerts
    for title, meta in alerts:
        assert _SENTINEL not in title
        assert _SENTINEL not in meta
    assert _SENTINEL not in caplog.text


def test_real_probe_path_never_leaks_the_sentinel(
    monkeypatch: pytest.MonkeyPatch, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Real probes + MockTransport returning 401: every probed credential
    fails, alerts carry name/status/key-URL, and the sentinel appears
    nowhere — including httpx's INFO request line (suppressed)."""
    caplog.set_level(logging.DEBUG)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url) + " " + str(dict(request.headers)))
        return httpx.Response(401, json={})

    _patch_real_probes(monkeypatch, handler)
    r = run_secret_check_pass(store)
    expected = sum(
        len(names)
        for g, names in secret_status._PROBE_GROUP_MEMBERS.items()
        if g != "mailto"
    )
    # Wolfram and OpenAlex don't treat 401 as a rejection (their bad codes
    # are 403 / 501-with-appid-complaint), so those members classify
    # unprobed rather than failed on a blanket 401.
    assert r.failed == expected - 2
    _assert_no_sentinel(store, caplog, sent)
    # The suppression itself: no httpx/httpcore record at all, even though
    # caplog listens at DEBUG.
    assert not [
        rec for rec in caplog.records if rec.name.split(".")[0] in ("httpx", "httpcore")
    ]


@pytest.mark.parametrize(
    "raise_exc",
    [
        pytest.param(ValueError("boom"), id="exception-path"),
        pytest.param("connect", id="network-down"),
    ],
)
def test_probe_failure_paths_leak_nothing(
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
    caplog: pytest.LogCaptureFixture,
    raise_exc: Any,
) -> None:
    """A probe exception and a dead network both classify 'unprobed'
    (exception type name only) — no alert, and the sentinel is in no log
    record even though retries re-raise a URL-bearing exception inside."""
    caplog.set_level(logging.DEBUG)
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url) + " " + str(dict(request.headers)))
        if raise_exc == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        raise raise_exc

    _patch_real_probes(monkeypatch, handler)
    r = run_secret_check_pass(store)
    assert r.failed == 0
    assert _count(store, resolved=False) == 0
    assert _SENTINEL not in caplog.text
    assert any(_SENTINEL in s for s in sent)  # the key went on the wire
    # The classification surface too: report rows carry the exception type
    # name only — repr them and assert the sentinel is absent.
    rows = asyncio.run(secret_status.weekly_report(store))
    assert _SENTINEL not in repr(rows)
    assert all(row.state != "failed" for row in rows)


def test_cadence_registered_weekly() -> None:
    cad = next(c for c in CADENCES if c.name == "secret_check")
    assert 7 * 24 * 3600 <= cad.interval_s < 8 * 24 * 3600
    assert cad.host_affinity is None
    assert not cad.spends
