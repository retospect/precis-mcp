"""Tests for the ``alert`` kind — producer module + read handler.

The producer (:mod:`precis.alerts`) is the write side any worker uses;
:class:`precis.handlers.alert.AlertHandler` is the agent-facing read /
triage side. Nursery's end-to-end alert behaviour is covered in
``test_nursery.py``; these tests pin the producer's dedup / resolve /
severity semantics and the handler's list views directly.
"""

from __future__ import annotations

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
    aid = raise_alert(
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
    a1 = raise_alert(
        store, source="s", fingerprint="fp:1", title="first", severity="info"
    )
    a2 = raise_alert(
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
    a1 = raise_alert(store, source="s", fingerprint="fp:1", title="a")
    a2 = raise_alert(store, source="s", fingerprint="fp:2", title="b")
    assert a1 != a2


def test_raise_alert_severity_change_keeps_single_tag(store: Store) -> None:
    aid = raise_alert(store, source="s", fingerprint="fp:1", title="a", severity="info")
    raise_alert(store, source="s", fingerprint="fp:1", title="a", severity="critical")
    sev_tags = {t for t in _tags(store, aid) if t.startswith("severity:")}
    assert sev_tags == {"severity:critical"}


def test_raise_alert_coerces_unknown_severity(store: Store) -> None:
    aid = raise_alert(store, source="s", fingerprint="fp:1", title="a", severity="oops")
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
    aid = raise_alert(store, source="s", fingerprint="fp", title="t")
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
    aid = raise_alert(store, source="s", fingerprint="fp:1", title="readable alert")
    resp = handler.get(id=aid)
    assert "readable alert" in resp.body
