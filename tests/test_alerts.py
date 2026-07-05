"""Tests for the ``alert`` kind — producer module + read handler.

The producer (:mod:`precis.alerts`) is the write side any worker uses;
:class:`precis.handlers.alert.AlertHandler` is the agent-facing read /
triage side. Nursery's end-to-end alert behaviour is covered in
``test_nursery.py``; these tests pin the producer's dedup / resolve /
severity semantics and the handler's list views directly.
"""

from __future__ import annotations

import psycopg
import pytest

from precis.alerts import (
    STATE_OPEN,
    STATE_RESOLVED,
    list_open_alerts,
    raise_alert,
    resolve_stale_alerts,
)
from precis.dispatch import Hub
from precis.handlers.alert import AlertHandler
from precis.store import Store


def _tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING(tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {r[0] for r in rows}


# ── producer: raise / dedup ────────────────────────────────────────


def test_raise_alert_inserts_open_with_tags(store: Store) -> None:
    aid, _ = raise_alert(
        store,
        source="nursery:spin-loop",
        fingerprint="spin-loop:42",
        title="[spin-loop] chase on #42",
        detail="1872 chase events in 24h",
        severity="warn",
        subject_ref_id=42,
    )
    tags = _tags(store, aid)
    assert STATE_OPEN in tags
    assert "alert-source:nursery:spin-loop" in tags
    assert "severity:warn" in tags
    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (aid,)
        ).fetchone()[0]
    assert meta["fingerprint"] == "spin-loop:42"
    assert meta["subject_ref_id"] == 42
    assert meta["seen_count"] == 1


def test_raise_alert_dedups_on_fingerprint(store: Store) -> None:
    a1, _ = raise_alert(
        store, source="s", fingerprint="fp:1", title="first", severity="info"
    )
    a2, _ = raise_alert(
        store, source="s", fingerprint="fp:1", title="second", severity="info"
    )
    assert a1 == a2  # same row
    assert len([a for a in list_open_alerts(store) if a["source"] == "s"]) == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta->>'seen_count' FROM refs WHERE ref_id = %s", (a1,)
        ).fetchone()
    assert row[0] == "second"  # title refreshed
    assert int(row[1]) == 2  # seen bumped


def test_raise_alert_distinct_fingerprints_are_distinct_rows(store: Store) -> None:
    a1, _ = raise_alert(store, source="s", fingerprint="fp:1", title="a")
    a2, _ = raise_alert(store, source="s", fingerprint="fp:2", title="b")
    assert a1 != a2


def test_open_alert_unique_index_blocks_duplicate(store: Store) -> None:
    """The partial unique index (migration 0030) is the DB backstop for
    the cross-node raise_alert race: a *second* open row for the same
    (source, fingerprint) is rejected. raise_alert's own advisory lock
    keeps real callers off this path; here we INSERT directly, exactly as
    a raced second nursery instance that skipped the lock would."""
    raise_alert(store, source="s", fingerprint="fp:dup", title="first")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with store.tx() as conn:
            store.insert_ref(
                kind="alert",
                slug=None,
                title="raced duplicate",
                meta={"alert_source": "s", "fingerprint": "fp:dup"},
                conn=conn,
            )


def test_resolved_then_reraise_does_not_conflict(store: Store) -> None:
    """A resolved alert (meta.resolved_at set) is outside the partial
    index predicate, so when the condition recurs the fresh open row
    doesn't collide with the historical resolved one."""
    raise_alert(store, source="s", fingerprint="fp:re", title="a")
    resolve_stale_alerts(store, source="s", live_fingerprints=[])  # resolve it
    aid, _ = raise_alert(store, source="s", fingerprint="fp:re", title="b")
    open_s = [a for a in list_open_alerts(store) if a["source"] == "s"]
    assert len(open_s) == 1
    assert open_s[0]["ref_id"] == aid


def test_raise_alert_severity_change_keeps_single_tag(store: Store) -> None:
    aid, _ = raise_alert(
        store, source="s", fingerprint="fp:1", title="a", severity="info"
    )
    raise_alert(store, source="s", fingerprint="fp:1", title="a", severity="critical")
    sev_tags = {t for t in _tags(store, aid) if t.startswith("severity:")}
    assert sev_tags == {"severity:critical"}


def test_raise_alert_coerces_unknown_severity(store: Store) -> None:
    aid, _ = raise_alert(
        store, source="s", fingerprint="fp:1", title="a", severity="oops"
    )
    assert "severity:warn" in _tags(store, aid)


# ── producer: resolve ──────────────────────────────────────────────


def test_resolve_stale_only_resolves_absent_fingerprints(store: Store) -> None:
    raise_alert(store, source="s", fingerprint="keep", title="keep")
    raise_alert(store, source="s", fingerprint="drop", title="drop")
    n = resolve_stale_alerts(store, source="s", live_fingerprints=["keep"])
    assert n == 1
    open_fps = {a["title"] for a in list_open_alerts(store) if a["source"] == "s"}
    assert open_fps == {"keep"}


def test_resolve_stale_scoped_to_source(store: Store) -> None:
    raise_alert(store, source="a", fingerprint="x", title="ax")
    raise_alert(store, source="b", fingerprint="x", title="bx")
    # Resolving source 'a' with an empty live set must not touch 'b'.
    resolve_stale_alerts(store, source="a", live_fingerprints=[])
    sources = {al["source"] for al in list_open_alerts(store)}
    assert "b" in sources
    assert "a" not in sources


def test_resolved_alert_carries_resolved_tag_and_timestamp(store: Store) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint="fp", title="t")
    resolve_stale_alerts(store, source="s", live_fingerprints=[])
    tags = _tags(store, aid)
    assert STATE_RESOLVED in tags
    assert STATE_OPEN not in tags
    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (aid,)
        ).fetchone()[0]
    assert "resolved_at" in meta


# ── producer: list ordering ────────────────────────────────────────


def test_list_open_alerts_orders_critical_first(store: Store) -> None:
    raise_alert(store, source="s", fingerprint="i", title="info one", severity="info")
    raise_alert(store, source="s", fingerprint="c", title="crit", severity="critical")
    raise_alert(store, source="s", fingerprint="w", title="warn", severity="warn")
    order = [a["severity"] for a in list_open_alerts(store)]
    assert order[0] == "critical"
    assert order.index("critical") < order.index("warn") < order.index("info")


# ── handler: read / triage surface ─────────────────────────────────


def test_handler_open_view_lists_open_alerts(hub: Hub, store: Store) -> None:
    handler = AlertHandler(hub=hub)
    raise_alert(
        store, source="s", fingerprint="fp:1", title="open alert", severity="warn"
    )
    resp = handler.get(id="/open")
    assert "open alert" in resp.body
    assert "1 open alert" in resp.body


def test_handler_open_view_empty_is_all_clear(hub: Hub) -> None:
    handler = AlertHandler(hub=hub)
    resp = handler.get(id="/open")
    assert "no open alerts" in resp.body.lower()


def test_handler_get_by_id_reads_one_alert(hub: Hub, store: Store) -> None:
    handler = AlertHandler(hub=hub)
    aid, _ = raise_alert(store, source="s", fingerprint="fp:1", title="readable alert")
    resp = handler.get(id=aid)
    assert "readable alert" in resp.body


# ── critical push (asa_bot message path) ───────────────────────────


def test_notify_critical_alert_queues_message_when_target_set(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With PRECIS_OPS_ALERT_TARGET set, a critical push queues a
    `kind='message'` to that channel (asa_bot then posts it)."""
    from precis.alerts import notify_critical_alert

    monkeypatch.setenv("PRECIS_OPS_ALERT_TARGET", "discord/1/2/3")
    ok = notify_critical_alert(
        store, "dead-worker: agent on melchior", "silent 12h", fingerprint="dw:m:agent"
    )
    assert ok is True
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta->>'target', meta->>'status', meta->>'proactive' "
            "FROM refs WHERE kind='message' AND meta->>'target'='discord/1/2/3' "
            "ORDER BY ref_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "dead-worker" in row[0]
    assert row[1] == "discord/1/2/3"
    assert row[2] == "queued"
    assert row[3] == "true"


def test_notify_critical_alert_is_dark_without_target(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target configured → no push, no message row (default)."""
    from precis.alerts import notify_critical_alert

    monkeypatch.delenv("PRECIS_OPS_ALERT_TARGET", raising=False)
    assert notify_critical_alert(store, "x", "y") is False
