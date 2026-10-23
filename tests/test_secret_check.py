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
import contextlib
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
    # record_status (run inside the pass) reads the vault inventory too —
    # keep it canned so the pass never touches the real vault here.
    monkeypatch.setattr(secret_status, "list_secrets", lambda *, store: [])


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
    monkeypatch.setattr(secret_status, "list_secrets", lambda *, store: [])


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


# ── stored status: the checker job's output, the /secrets page's input ────


def test_record_status_persists_per_secret_state_and_time(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """The job records last-checked status and time per secret; the page's
    read path parses the same blob back."""
    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _OK})
    monkeypatch.setattr(secret_status, "list_secrets", lambda *, store: [])

    snap = secret_status.record_status(store)

    assert snap.checked_at is not None
    entry = snap.entries["ANTHROPIC_API_KEY"]
    assert entry.state == "ok" and entry.check == "probe"
    assert entry.checked_at is not None

    loaded = secret_status.stored_status(store)
    assert loaded.checked_at is not None
    assert loaded.entries["ANTHROPIC_API_KEY"].state == "ok"
    assert loaded.entries["SEMANTIC_SCHOLAR_API_KEY"].state == "missing"


def test_record_status_blob_never_holds_a_secret_value(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _BAD})
    monkeypatch.setattr(secret_status, "list_secrets", lambda *, store: [])
    monkeypatch.setattr(
        secret_status, "present", lambda n, *, vault_names=None, store=None: True
    )

    secret_status.record_status(store)

    assert _SENTINEL not in (store.get_setting(secret_status._STATUS_KEY) or "")


def test_infra_class_is_presence_only(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """``svc/infra/*`` names are never probed — even inside a probe group —
    and record as presence-checked."""
    called: list[str] = []

    async def spy_probe(client: Any, values: dict[str, str]) -> CheckResult:
        called.extend(values)
        return _OK

    monkeypatch.setitem(
        secret_status._PROBE_GROUP_MEMBERS, "infra_test", ("svc/infra/DB_KEY",)
    )
    monkeypatch.setitem(secret_status._PROBES, "infra_test", spy_probe)
    try:
        results = asyncio.run(secret_status.run_checks(store))
    finally:
        del secret_status._PROBE_GROUP_MEMBERS["infra_test"]
        del secret_status._PROBES["infra_test"]

    assert results == {}
    assert called == []

    monkeypatch.setattr(
        secret_status,
        "list_secrets",
        lambda *, store: [
            {"name": "svc/infra/DB_KEY", "hint": "h", "updated_at": None}
        ],
    )
    snap = secret_status.record_status(store, results={})
    entry = snap.entries["svc/infra/DB_KEY"]
    assert entry.state == "present" and entry.check == "presence"
    assert "never probed" in entry.detail


def test_mint_status_job_dedupes_and_carries_no_secret(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.secret_check import (
        mint_status_job,
        pending_status_check,
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", _SENTINEL)
    job_id = mint_status_job(store, origin="human", title="secret_status (test)")
    assert job_id is not None
    assert pending_status_check(store)
    # A second mint while one is pending is a no-op.
    assert mint_status_job(store, origin="human", title="x") is None

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta::text FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    assert row[0] == "secret_status (test)"
    assert _SENTINEL not in row[1]
    assert '"secret_status"' in row[1]
    assert '"human"' in row[1]


def test_secret_status_job_type_registered() -> None:
    from precis.workers.job_types import get_job_type, known_job_types

    spec = get_job_type("secret_status")
    assert spec is not None
    assert spec.dispatch is not None
    assert "job_inproc" in spec.compatible_executors
    assert "secret_status" in known_job_types()


def test_secret_status_cadence_registered() -> None:
    cad = next(c for c in CADENCES if c.name == "secret_status")
    assert 3600 <= cad.interval_s <= 24 * 3600
    assert cad.host_affinity is None
    assert not cad.spends


def test_job_dispatch_writes_status(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    """The job_inproc dispatch path runs record_status and meta holds
    counts only — no names, no values."""
    import types

    from precis.workers.job_types.secret_status import SPEC

    _patch_report(monkeypatch, {"ANTHROPIC_API_KEY": _OK})
    monkeypatch.setattr(secret_status, "list_secrets", lambda *, store: [])
    monkeypatch.setattr(
        secret_status, "present", lambda n, *, vault_names=None, store=None: True
    )
    meta: dict[str, Any] = {}
    chunks: list[str] = []
    ctx = types.SimpleNamespace(
        store=store,
        meta={},
        set_meta=lambda **kw: meta.update(kw),
        append_chunk=lambda kind, text: chunks.append(text),
        record_failure=lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("dispatch failed")
        ),
    )
    dispatch = SPEC.dispatch
    assert dispatch is not None
    dispatch(ctx, SPEC)
    assert meta["status"]["ok"] >= 1
    assert _SENTINEL not in repr(meta)
    assert chunks
    assert secret_status.stored_status(store).checked_at is not None


# ── review fixes F1/F2/F4/F7 ─────────────────────────────────────────────


def test_dispatch_failure_names_exception_type_only(
    monkeypatch: pytest.MonkeyPatch, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """F1: a record_status failure reaches the job/log as the exception
    TYPE name only — str(exc)/traceback can echo a key-bearing URL."""
    import logging
    import types

    from precis.workers.job_types.secret_status import SPEC

    caplog.set_level(logging.DEBUG)

    def boom(_store: Any) -> Any:
        raise ValueError("https://probe.invalid/?api_key=" + _SENTINEL)

    monkeypatch.setattr(secret_status, "record_status", boom)
    failures: list[str] = []
    ctx = types.SimpleNamespace(
        store=store,
        meta={},
        set_meta=lambda **kw: None,
        append_chunk=lambda *a: None,
        record_failure=lambda msg, **kw: failures.append(msg),
    )
    dispatch = SPEC.dispatch
    assert dispatch is not None
    dispatch(ctx, SPEC)
    assert len(failures) == 1
    assert "ValueError" in failures[0]
    assert _SENTINEL not in failures[0]
    assert _SENTINEL not in caplog.text


def test_mint_takes_the_advisory_xact_lock_before_checking(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: the pending check + insert are one atomic step — the advisory
    lock statement runs inside the same connection, ahead of the check."""
    from precis.workers.secret_check import mint_status_job

    sql_seen: list[str] = []
    real_conn = store.pool.connection

    @contextlib.contextmanager
    def spy():
        with real_conn() as conn:
            real_execute = conn.execute

            def traced(sql: str, params: Any = None) -> Any:
                sql_seen.append(sql)
                return real_execute(sql, params)

            conn.execute = traced
            yield conn

    monkeypatch.setattr(store.pool, "connection", spy)
    job_id = mint_status_job(store, origin="background", title="t")
    assert job_id is not None
    lock_idx = next(i for i, s in enumerate(sql_seen) if "pg_advisory_xact_lock" in s)
    check_idx = next(i for i, s in enumerate(sql_seen) if "job_type" in s)
    assert lock_idx < check_idx


def test_stale_pending_job_does_not_block_a_new_mint(
    store: Any,
) -> None:
    """F4: a pending check older than the bound is wedged — it neither
    blocks a mint nor reads as 'queued' on the page hint."""
    from precis.workers.secret_check import (
        mint_status_job,
        pending_status_check,
    )

    first = mint_status_job(store, origin="background", title="t1")
    assert first is not None and pending_status_check(store)
    assert mint_status_job(store, origin="human", title="t2") is None

    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '2 hours' WHERE ref_id = %s",
            (first,),
        )
        conn.commit()

    assert not pending_status_check(store)
    second = mint_status_job(store, origin="human", title="t3")
    assert second is not None and second != first


def test_present_and_file_refuse_traversal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """F7: names with '..' or a leading '/' never reach the file layer —
    via present(), _from_file or get_secret's file fallback."""
    from precis import secrets as vault

    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    secret_file = tmp_path / "REAL_KEY"
    secret_file.write_text("v", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}_escape"
    outside.write_text("v", encoding="utf-8")

    assert vault.present("REAL_KEY")
    assert not vault.present(f"../{tmp_path.name}_escape")
    assert not vault.present("/etc/hostname")
    assert not vault.present("..")
    assert vault._from_file("../x") is None
    assert vault.get_secret("../anything") is None
    outside.unlink()
