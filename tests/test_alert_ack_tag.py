"""``tag(kind='alert', add=['ack:6h'])`` acks an alert (gr489245).

Reto 2026-10-15, option 3a: the tag verb maps ``ack:<duration>`` onto
:func:`precis.alerts.ack_alert` with that TTL; ``remove=['ack']`` clears it.
The ack lives in ``meta.acked_until``, never as a tag, and the alert stays
open.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.alerts import STATE_OPEN, list_open_alerts, raise_alert
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.alert import AlertHandler
from precis.store import Store


def _meta(store: Store, ref_id: int) -> dict[str, object]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _open_tags(store: Store, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING(tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
            (ref_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _open_ids(store: Store) -> set[int]:
    return {int(a["ref_id"]) for a in list_open_alerts(store)}


@pytest.mark.parametrize(
    ("tag", "ttl"),
    [
        ("ack:6h", timedelta(hours=6)),
        ("ack:30m", timedelta(minutes=30)),
        ("ACK:7d", timedelta(days=7)),
    ],
)
def test_ack_sets_acked_until_with_the_ttl(
    hub: Hub, store: Store, tag: str, ttl: timedelta
) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint=f"fp:{tag}", title="cond")
    before = datetime.now(UTC)
    body = AlertHandler(hub=hub).tag(id=aid, add=[tag]).body
    meta = _meta(store, aid)
    until = datetime.fromisoformat(str(meta["acked_until"]))
    assert before + ttl <= until <= datetime.now(UTC) + ttl
    assert meta["acked_by"] == "agent"
    assert f"alert {aid}: acked until" in body
    # Still open, no ack tag stored, gone from the open list.
    assert STATE_OPEN in _open_tags(store, aid)
    assert not any(t.startswith("ack") for t in _open_tags(store, aid))
    assert aid not in _open_ids(store)


def test_remove_ack_clears_it(hub: Hub, store: Store) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint="fp:unack", title="cond")
    handler = AlertHandler(hub=hub)
    handler.tag(id=aid, add=["ack:6h"])
    assert aid not in _open_ids(store)
    body = handler.tag(id=aid, remove=["ack"]).body
    assert "ack cleared" in body
    assert aid in _open_ids(store)


@pytest.mark.parametrize(
    "bad",
    [
        "ack:6",
        "ack:h",
        "ack:6w",
        "ack:0h",
        "ack:91d",
        "ack:-1h",
        "ack:1.5h",
        "ack",
        "ack:999999999999d",
    ],
)
def test_bad_duration_refuses_with_the_accepted_forms(
    hub: Hub, store: Store, bad: str
) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint=f"fp:bad:{bad}", title="cond")
    with pytest.raises(BadInput, match=r"ack:<N>m\|h\|d"):
        AlertHandler(hub=hub).tag(id=aid, add=[bad])
    assert "acked_until" not in _meta(store, aid)


def test_resolved_alert_refuses_an_ack(hub: Hub, store: Store) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint="fp:resolved", title="cond")
    handler = AlertHandler(hub=hub)
    handler.tag(id=aid, add=["alert-state:resolved"], remove=["alert-state:open"])
    with pytest.raises(BadInput, match="not open"):
        handler.tag(id=aid, add=["ack:6h"])


def test_ack_with_other_tags_applies_both(hub: Hub, store: Store) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint="fp:mixed", title="cond")
    AlertHandler(hub=hub).tag(id=aid, add=["ack:2h", "triaged-by-test"])
    assert "triaged-by-test" in _open_tags(store, aid)
    assert "acked_until" in _meta(store, aid)


def test_open_list_keeps_an_acked_alert_and_marks_it(hub: Hub, store: Store) -> None:
    # Dogfood r38: the ack reply claimed the alert left /open; it stays (it is
    # open) and /open names it as acked with its expiry, like /alerts dims it.
    aid, _ = raise_alert(store, source="s", fingerprint="fp:openlist", title="cond")
    handler = AlertHandler(hub=hub)
    body = handler.tag(id=aid, add=["ack:6h"]).body
    assert "marked acked on /open" in body
    listing = handler.get(id="/open").body
    assert f"al{aid}" in listing
    assert f"- al{aid} acked until" in listing
    handler.tag(id=aid, remove=["ack"])
    assert f"- al{aid} acked until" not in handler.get(id="/open").body


def test_hints_follow_the_ack_state(hub: Hub, store: Store) -> None:
    aid, _ = raise_alert(store, source="s", fingerprint="fp:hints", title="cond")
    handler = AlertHandler(hub=hub)
    acked = handler.tag(id=aid, add=["ack:6h"]).body
    assert "remove=['ack']" in acked
    cleared = handler.tag(id=aid, remove=["ack"]).body
    assert "remove=['ack']" not in cleared
    assert "add=['ack:6h']" in cleared


def test_batch_ack(hub: Hub, store: Store) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp:b1", title="one")
    b, _ = raise_alert(store, source="s", fingerprint="fp:b2", title="two")
    AlertHandler(hub=hub).tag(id=[a, b], add=["ack:1h"])
    assert not ({a, b} & _open_ids(store))
