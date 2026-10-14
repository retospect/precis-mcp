"""``get(kind='alert', id=N, view='context')`` — the alert plus a
bounded ``worker_logs`` window around first/last seen (mcp-introspection
gap closures 2026-10-14, gr484235). Secrets never echo; every line is
``mask_secrets``-filtered before render.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.handlers.alert import AlertHandler, _fingerprint_vars
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db

_NOW = datetime.now(UTC)

#: Assembled from fragments — the credential gate
#: (tests/test_deploy_tree_no_secrets.py) flags any source LINE that is
#: credential-shaped; a runtime-built fake keeps the masking assertion
#: identical while no literal ever looks like a key.
_FAKE_KEY = "sk-" + "ant-" + "FAKETESTKEY" + "abc123"


def _alert(
    store: Store,
    *,
    source: str = "nursery:long-wait",
    fingerprint: str = "long-wait:4242",
    meta_extra: dict | None = None,
) -> int:
    ref = store.insert_ref(
        kind="alert",
        slug=None,
        title="long-wait:4242 todo td4242 waiting on a job",
        meta={
            "alert_source": source,
            "fingerprint": fingerprint,
            "severity": "info",
            "seen_count": 3,
            "detail": "a todo has been waiting longer than its budget",
            **(meta_extra or {}),
        },
    )
    store.add_tag(ref.id, Tag.open("alert-state:open"), set_by="system")
    return ref.id


def _log(
    store: Store,
    *,
    message: str,
    pass_: str = "nursery",
    host: str = "testhost",
    process: str = "precis-worker",
    level: str = "WARN",
    ts: datetime | None = None,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, pass, level, message) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ts or _NOW, host, process, pass_, level, message),
        )
        conn.commit()


@pytest.fixture
def alerts(hub: Hub) -> AlertHandler:
    return AlertHandler(hub=hub)


class TestContextView:
    def test_detail_plus_log_window(self, alerts: AlertHandler, store: Store) -> None:
        ref_id = _alert(store)
        _log(store, message="long-wait:4242 td4242 still waiting")
        resp = alerts.get(id=ref_id, view="context")
        body = resp.body
        assert f"# alert {ref_id} [open]" in body
        assert "seen_count: 3" in body
        assert "## worker_logs" in body
        assert "long-wait:4242 td4242 still waiting" in body
        assert "pass 'nursery'" in body

    def test_secret_in_log_line_masked(
        self, alerts: AlertHandler, store: Store
    ) -> None:
        ref_id = _alert(store)
        _log(
            store,
            message=f"env dump: ANTHROPIC_API_KEY={_FAKE_KEY} tail",
        )
        resp = alerts.get(id=ref_id, view="context")
        assert _FAKE_KEY not in resp.body
        assert "## worker_logs" in resp.body

    def test_no_rows_in_window(self, alerts: AlertHandler, store: Store) -> None:
        ref_id = _alert(store)
        _log(
            store,
            message="outside pass",
            pass_="dispatch",
        )
        resp = alerts.get(id=ref_id, view="context")
        assert "(no rows in window" in resp.body

    def test_out_of_window_excluded(self, alerts: AlertHandler, store: Store) -> None:
        ref_id = _alert(store)
        _log(
            store,
            message="yesterday's line",
            ts=_NOW - timedelta(hours=2),
        )
        resp = alerts.get(id=ref_id, view="context")
        assert "yesterday's line" not in resp.body

    def test_host_override_widens_then_narrows(
        self, alerts: AlertHandler, store: Store
    ) -> None:
        ref_id = _alert(store)
        _log(store, message="from host-a", host="host-a")
        _log(store, message="from host-b", host="host-b")
        resp = alerts.get(id=ref_id, view="context")
        assert "from host-a" in resp.body and "from host-b" in resp.body
        resp = alerts.get(id=ref_id, view="context", host="host-a")
        assert "from host-a" in resp.body
        assert "from host-b" not in resp.body

    def test_via_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        ref_id = _alert(store)
        _log(store, message="dispatch-seen line")
        body = runtime_with_store.dispatch(
            "get", {"kind": "alert", "id": ref_id, "view": "context"}
        )
        assert "## worker_logs" in body
        assert "dispatch-seen line" in body

    def test_unknown_view_lists_context(
        self, alerts: AlertHandler, store: Store
    ) -> None:
        ref_id = _alert(store)
        with pytest.raises(Exception, match="unknown view"):
            alerts.get(id=ref_id, view="histogram")


class TestFingerprintVars:
    def test_dead_worker_pattern(self) -> None:
        got = _fingerprint_vars(
            "dead-worker:{host}:{process}", "dead-worker:caspar:precis-worker-agent"
        )
        assert got == {"host": "caspar", "process": "precis-worker-agent"}

    def test_pass_dead_slash_pattern(self) -> None:
        got = _fingerprint_vars(
            "pass-dead:{host}/{process}/{handler}",
            "pass-dead:node1/precis-worker/dispatch",
        )
        assert got == {
            "host": "node1",
            "process": "precis-worker",
            "handler": "dispatch",
        }

    def test_no_match_returns_empty(self) -> None:
        assert _fingerprint_vars("long-wait:{ref_id}", "other:thing") == {}
        assert _fingerprint_vars("", "x") == {}
