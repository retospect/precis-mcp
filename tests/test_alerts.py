"""Tests for the ``alert`` kind — producer module + read handler.

The producer (:mod:`precis.alerts`) is the write side any worker uses;
:class:`precis.handlers.alert.AlertHandler` is the agent-facing read /
triage side. Nursery's end-to-end alert behaviour is covered in
``test_nursery.py``; these tests pin the producer's dedup / resolve /
severity semantics and the handler's list views directly.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from precis import alerts as alerts_mod
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
from precis.store.types import Tag


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
        row = conn.execute(
            "SELECT meta, alert_source, fingerprint, resolved_at "
            "FROM refs WHERE ref_id = %s",
            (aid,),
        ).fetchone()
    meta = row[0]
    assert meta["fingerprint"] == "spin-loop:42"
    assert meta["subject_ref_id"] == 42
    assert meta["seen_count"] == 1
    # Migration 0099: dedup identity is a real column, not just a meta key.
    assert row[1] == "nursery:spin-loop"
    assert row[2] == "spin-loop:42"
    assert row[3] is None


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
    """The partial unique index (migration 0030, rebuilt off the
    alert_source/fingerprint columns by 0099) is the DB backstop for the
    cross-node raise_alert race: a *second* open row for the same
    (source, fingerprint) is rejected. raise_alert's own advisory lock
    keeps real callers off this path; here we INSERT + dual-write the
    columns directly (mirroring raise_alert's own insert path), exactly
    as a raced second nursery instance that skipped the lock would."""
    raise_alert(store, source="s", fingerprint="fp:dup", title="first")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="alert",
                slug=None,
                title="raced duplicate",
                meta={"alert_source": "s", "fingerprint": "fp:dup"},
                conn=conn,
            )
            conn.execute(
                "UPDATE refs SET alert_source = %s, fingerprint = %s WHERE ref_id = %s",
                ("s", "fp:dup", ref.id),
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


# ── producer: dedup-write throttle (fix #2) ────────────────────────


def _counting_update_ref(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Wrap ``store.update_ref`` to record every call, mirroring the
    no-op-write assertion pattern in
    ``test_llm_serving.py::test_advertise_skips_noop_write_but_writes_on_change``.
    ``raise_alert``'s dedup path routes its write through
    ``store.update_ref``, so a throttled (skipped) sighting leaves this
    list empty."""
    calls: list[Any] = []
    orig_update_ref = store.update_ref

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return orig_update_ref(*args, **kwargs)

    monkeypatch.setattr(store, "update_ref", counting)
    return calls


def _seen_count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        return int(
            conn.execute(
                "SELECT meta->>'seen_count' FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()[0]
        )


def test_raise_alert_first_repeat_always_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The *first* repeat sighting always bumps seen_count to 2 — even
    with byte-identical content and well inside the throttle window — so
    the `seen_count > 1` display invariant holds from the second sighting
    on."""
    aid, _ = raise_alert(
        store, source="s", fingerprint="fp:first-repeat", title="a", severity="warn"
    )
    calls = _counting_update_ref(store, monkeypatch)
    raise_alert(
        store, source="s", fingerprint="fp:first-repeat", title="a", severity="warn"
    )
    assert len(calls) == 1
    assert _seen_count(store, aid) == 2


def test_raise_alert_throttles_unchanged_repeat_within_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *second* repeat sighting (so the first-repeat guarantee no
    longer applies) with unchanged title/severity/detail, still inside
    the default 900s throttle window, issues NO write at all."""
    aid, _ = raise_alert(
        store,
        source="s",
        fingerprint="fp:throttled",
        title="a",
        severity="warn",
        detail="d",
    )
    raise_alert(  # first repeat — always writes, bumps to 2
        store,
        source="s",
        fingerprint="fp:throttled",
        title="a",
        severity="warn",
        detail="d",
    )
    assert _seen_count(store, aid) == 2

    calls = _counting_update_ref(store, monkeypatch)
    raise_alert(  # second repeat, unchanged — must be throttled (no write)
        store,
        source="s",
        fingerprint="fp:throttled",
        title="a",
        severity="warn",
        detail="d",
    )
    assert calls == []
    assert _seen_count(store, aid) == 2  # unchanged — not bumped again


def test_raise_alert_changed_content_always_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat sighting with changed title/severity/detail writes even
    inside the throttle window — content changes are never throttled."""
    aid, _ = raise_alert(
        store, source="s", fingerprint="fp:changed", title="a", severity="warn"
    )
    raise_alert(  # first repeat — always writes, bumps to 2
        store, source="s", fingerprint="fp:changed", title="a", severity="warn"
    )
    assert _seen_count(store, aid) == 2

    calls = _counting_update_ref(store, monkeypatch)
    raise_alert(  # second repeat, title changed — must write
        store, source="s", fingerprint="fp:changed", title="b", severity="warn"
    )
    assert len(calls) == 1
    assert _seen_count(store, aid) == 3


def test_raise_alert_throttle_env_var_forces_stale_refresh(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_ALERT_RERAISE_THROTTLE_SECONDS=0`` makes every repeat
    sighting stale immediately, so even unchanged content writes."""
    monkeypatch.setenv(alerts_mod.ALERT_RERAISE_THROTTLE_ENV, "0")
    aid, _ = raise_alert(store, source="s", fingerprint="fp:zero", title="a")
    raise_alert(store, source="s", fingerprint="fp:zero", title="a")  # first repeat
    assert _seen_count(store, aid) == 2

    calls = _counting_update_ref(store, monkeypatch)
    raise_alert(store, source="s", fingerprint="fp:zero", title="a")
    assert len(calls) == 1
    assert _seen_count(store, aid) == 3


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
        row = conn.execute(
            "SELECT meta, resolved_at FROM refs WHERE ref_id = %s", (aid,)
        ).fetchone()
    assert "resolved_at" in row[0]  # dual-write into meta kept
    assert row[1] is not None  # migration 0099: real column set too


def test_resolve_stale_alerts_column_and_tag_flip_together(store: Store) -> None:
    """The `resolved_at` column (0099) and the alert-state:resolved tag
    flip in the same transaction — and once resolved, the (source,
    fingerprint) pair is free again (the unique index only covers
    resolved_at IS NULL rows) so a recurrence can re-raise cleanly."""
    aid1, _ = raise_alert(store, source="s", fingerprint="fp:re2", title="a")
    resolve_stale_alerts(store, source="s", live_fingerprints=[])
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT resolved_at FROM refs WHERE ref_id = %s", (aid1,)
        ).fetchone()
    assert row[0] is not None

    # Re-raise the same (source, fingerprint) — the index must not block it.
    aid2, is_new = raise_alert(store, source="s", fingerprint="fp:re2", title="b")
    assert is_new is True
    assert aid2 != aid1
    open_s = [a for a in list_open_alerts(store) if a["source"] == "s"]
    assert [a["ref_id"] for a in open_s] == [aid2]


# ── producer: rolling-deploy transition shim (NULL columns, meta-only) ──


def _insert_old_code_open_alert(
    store: Store, *, source: str, fingerprint: str, title: str
) -> int:
    """Mint an OPEN alert row shaped like an *old-code* node would leave
    it mid-rollout: migration 0099's ``alert_source``/``fingerprint``
    columns are left NULL, and the dedup identity lives only in
    ``meta`` (the pre-0099 write path)."""
    meta = {
        "alert_source": source,
        "fingerprint": fingerprint,
        "severity": "warn",
        "detail": "",
        "seen_count": 1,
    }
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="alert", slug=None, title=title, meta=meta, conn=conn
        )
        for tag in (
            Tag.open(STATE_OPEN),
            Tag.open(f"alert-source:{source}"),
            Tag.open("severity:warn"),
        ):
            store.add_tag(ref.id, tag, set_by="system", conn=conn)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT alert_source, fingerprint FROM refs WHERE ref_id = %s", (ref.id,)
        ).fetchone()
    assert row == (None, None)  # sanity: this is the NULL-column shape
    return int(ref.id)


def test_raise_alert_dedups_onto_null_column_row_during_rollout(
    store: Store,
) -> None:
    """A new-code node's ``raise_alert`` must still find and dedup onto
    an open alert row an old-code node left with NULL alert_source /
    fingerprint columns (meta-only), rather than missing it and
    inserting a duplicate that then never dedups or resolves."""
    old_id = _insert_old_code_open_alert(
        store, source="s", fingerprint="fp:rollout", title="old"
    )
    new_id, is_new = raise_alert(
        store, source="s", fingerprint="fp:rollout", title="new", severity="warn"
    )
    assert is_new is False
    assert new_id == old_id
    # No duplicate row was inserted for this (source, fingerprint) — count
    # via COALESCE since the pre-existing row's own columns are still
    # NULL (raise_alert's dedup-write path only patches meta/title, not
    # the columns; the column dual-write only happens on first INSERT).
    with store.pool.connection() as conn:
        n = conn.execute(
            """
            SELECT count(*) FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND COALESCE(r.alert_source, r.meta->>'alert_source') = 's'
               AND COALESCE(r.fingerprint, r.meta->>'fingerprint') = 'fp:rollout'
               AND t.namespace = 'OPEN'
               AND t.value = %s
            """,
            (STATE_OPEN,),
        ).fetchone()[0]
    assert n == 1


def test_resolve_stale_alerts_resolves_null_column_row_during_rollout(
    store: Store,
) -> None:
    """``resolve_stale_alerts`` must also see and resolve a NULL-column
    (meta-only) row left by an old-code node."""
    old_id = _insert_old_code_open_alert(
        store, source="s", fingerprint="fp:rollout-resolve", title="old"
    )
    n = resolve_stale_alerts(store, source="s", live_fingerprints=[])
    assert n == 1
    tags = _tags(store, old_id)
    assert STATE_RESOLVED in tags
    assert STATE_OPEN not in tags
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT resolved_at FROM refs WHERE ref_id = %s", (old_id,)
        ).fetchone()
    assert row[0] is not None


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


def test_notify_critical_alert_prefers_canonical_target_over_webhook_alias(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical ``PRECIS_OPS_ALERT_TARGET`` wins over the deprecated
    ``PRECIS_OPS_ALERT_WEBHOOK`` alias (the name is a misnomer — it is a Discord
    channel target, not a webhook URL)."""
    from precis.alerts import notify_critical_alert

    monkeypatch.setenv("PRECIS_OPS_ALERT_WEBHOOK", "discord/9/9/9")
    monkeypatch.setenv("PRECIS_OPS_ALERT_TARGET", "discord/1/2/3")
    ok = notify_critical_alert(store, "spin", "many ticks")
    assert ok is True
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'target' FROM refs WHERE kind='message'"
        ).fetchone()
    assert row is not None
    assert row[0] == "discord/1/2/3"


def test_notify_critical_alert_accepts_webhook_alias(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deprecated ``PRECIS_OPS_ALERT_WEBHOOK`` alias still delivers when it
    is the only variable set (an already-deployed env keeps paging)."""
    from precis.alerts import notify_critical_alert

    monkeypatch.delenv("PRECIS_OPS_ALERT_TARGET", raising=False)
    monkeypatch.setenv("PRECIS_OPS_ALERT_WEBHOOK", "discord/9/9/9")
    assert notify_critical_alert(store, "spin", "many ticks") is True
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'target' FROM refs WHERE kind='message' "
            "AND meta->>'target'='discord/9/9/9'"
        ).fetchone()
    assert row is not None


def test_notify_critical_alert_is_dark_without_target(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target configured → no push, no message row (default)."""
    from precis.alerts import notify_critical_alert

    monkeypatch.delenv("PRECIS_OPS_ALERT_WEBHOOK", raising=False)
    monkeypatch.delenv("PRECIS_OPS_ALERT_TARGET", raising=False)
    assert notify_critical_alert(store, "x", "y") is False
