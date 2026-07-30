"""Alert producer — the write side of ``kind='alert'``.

A small, store-only surface any worker can call to raise a
machine-detected operational / health condition. Peer to the
agent-facing :class:`precis.handlers.alert.AlertHandler` (the read /
ack side). Kept out of the handler so a background pass (nursery,
sweeper, quota_check, …) can raise an alert without going through the
seven-verb dispatch layer.

Lifecycle, all on the shared ``refs`` table. The dedup/lifecycle identity
(``alert_source``, ``fingerprint``, ``resolved_at``) lives in real ``refs``
columns (migration 0099) — not indexed jsonb ``meta`` keys — so the
partial unique index (0030) and the per-sighting dedup write don't touch
an indexed jsonb column; ``meta`` still carries ``severity`` / ``detail`` /
``seen_count`` (dual-written into the columns above too, for rollback
safety / uniform shape).

* **raise** — :func:`raise_alert` upserts on ``(alert_source,
  fingerprint)``. A first sighting inserts a new ``alert`` ref tagged
  ``alert-state:open`` + ``alert-source:<source>`` + ``severity:<sev>``.
  A repeat sighting of a still-open alert bumps ``meta.seen_count`` and
  ``updated_at`` instead of writing a duplicate — this is the dedup the
  old memory-digest fingerprint approximated, but per-condition rather
  than per-digest, so a single churning condition can't spam the table.
  That dedup write is itself throttled — see :func:`raise_alert`'s
  docstring — so an unchanged, still-open condition doesn't write every
  nursery pass either.
* **resolve** — :func:`resolve_stale_alerts` closes any open alert of a
  given source whose fingerprint is absent from the current live set
  (the condition cleared). The row is retained (``alert-state:resolved``)
  for history; the ``/alerts`` tab and :func:`list_open_alerts` filter
  on ``alert-state:open``.

Severity is advisory (``info`` / ``warn`` / ``critical``) — it drives
sort + colour in the UI, nothing gates on it.

Alerts are intentionally NOT embedded: the body lives in ``refs.title``
+ ``meta`` and no ``card_combined`` chunk is minted, so the embed /
chunk_keywords workers skip them and they never reach semantic search.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.store import Store
from precis.store.types import Tag

log = logging.getLogger(__name__)

#: Open-namespace lifecycle tags. Alerts deliberately stay off the
#: ``STATUS:`` axis (which is restricted to the todo / job lifecycle and
#: its fixed value set) — an alert's open→resolved flip is its own
#: concern, queried as a flat open tag.
STATE_OPEN = "alert-state:open"
STATE_RESOLVED = "alert-state:resolved"

#: Valid severities, low→high. Anything else is coerced to ``warn``.
SEVERITIES = ("info", "warn", "critical")

#: Env var controlling the dedup-write throttle window (seconds) applied
#: to a repeat sighting of an already-open alert — see ``raise_alert``.
ALERT_RERAISE_THROTTLE_ENV = "PRECIS_ALERT_RERAISE_THROTTLE_SECONDS"
_DEFAULT_THROTTLE_SECONDS = 900  # 15 min


def _throttle_seconds() -> int:
    raw = os.environ.get(ALERT_RERAISE_THROTTLE_ENV, "")
    try:
        return int(raw) if raw else _DEFAULT_THROTTLE_SECONDS
    except ValueError:
        return _DEFAULT_THROTTLE_SECONDS


def _norm_severity(sev: str) -> str:
    return sev if sev in SEVERITIES else "warn"


def raise_alert(
    store: Store,
    *,
    source: str,
    fingerprint: str,
    title: str,
    detail: str = "",
    severity: str = "warn",
    subject_ref_id: int | None = None,
) -> tuple[int, bool]:
    """Raise (or refresh) an alert. Returns ``(alert_ref_id, is_new)``.

    ``is_new`` is ``True`` only on the first sighting (a fresh INSERT),
    ``False`` when this call bumped an already-open alert. Callers use it
    to fire a one-shot side effect — e.g. a Discord push for a *new*
    ``critical`` condition — exactly once per condition rather than every
    pass while it stays open (see :func:`notify_critical_alert`).

    Dedup is on ``(source, fingerprint)`` among *open* alerts: a repeat
    sighting bumps ``seen_count`` + ``updated_at`` and refreshes the
    title / detail / severity rather than inserting a duplicate. The
    ``fingerprint`` is the caller's stable identity for the condition
    (e.g. ``"spin-loop:34888:chase"``); pick it so the same underlying
    problem always hashes to the same string.

    The dedup write itself is throttled: a repeat sighting whose title /
    severity / detail are unchanged, and whose prior ``seen_count`` is
    already ``> 1``, and whose ``updated_at`` is within
    ``PRECIS_ALERT_RERAISE_THROTTLE_SECONDS`` (default 900s / 15min) of
    now, is a pure no-op — the nursery calls this every minute for every
    still-open condition, and writing ``meta`` on every one of those
    passes is the reraise-storm this throttle exists to kill. A change in
    content, the *first* repeat sighting (so the seen_count>1 "seen more
    than once" display invariant holds by the second sighting), or a
    sighting stale enough to need a liveness refresh, always writes. Once
    throttled, ``seen_count`` / ``updated_at`` become coarse by design —
    nothing reads the exact count, only a ``seen_count > 1`` display
    boolean, so under-counting during a throttle window is harmless.
    """
    severity = _norm_severity(severity)
    throttle = timedelta(seconds=_throttle_seconds())
    with store.tx() as conn:
        # Serialize concurrent raises of the SAME (source, fingerprint)
        # across the cluster's many nursery instances. The SELECT-then-
        # INSERT dedup below is not atomic on its own, so without this two
        # nodes could both miss the existing open alert and both INSERT —
        # which the partial unique index uq_alert_open_source_fingerprint
        # (migration 0030, rebuilt off columns by 0099) would then reject
        # with a violation. The transaction-scoped advisory lock makes the
        # check-then-insert atomic per fingerprint; it releases at
        # COMMIT/ROLLBACK. The two-arg (classid, objid) form hashes source
        # and fingerprint separately, so "a"+"bc" can't alias "ab"+"c" and
        # there's no NUL separator (PostgreSQL text rejects 0x00).
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (source, fingerprint),
        )
        # Rolling-deploy transition shim: the fleet deploys host-by-host
        # over minutes, so an old-code node can still be inserting open
        # alerts with alert_source/fingerprint COLUMNS NULL (it only wrote
        # meta) while this node is already running the column-based dedup.
        # The COALESCE fallback lets a new-code node still match that old
        # row instead of missing it and inserting a duplicate that then
        # never dedups or resolves. Drop the COALESCE once no open alert
        # row has NULL alert_source/fingerprint columns.
        existing = conn.execute(
            """
            SELECT r.ref_id, r.title, r.updated_at,
                   meta->>'severity' AS severity,
                   meta->>'detail' AS detail,
                   (meta->>'seen_count')::int AS seen_count
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND COALESCE(r.alert_source, r.meta->>'alert_source') = %s
               AND COALESCE(r.fingerprint, r.meta->>'fingerprint') = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            (source, fingerprint, STATE_OPEN),
        ).fetchone()

        if existing is not None:
            ref_id = int(existing[0])
            prior_title = existing[1]
            prior_updated_at = existing[2]
            prior_severity = existing[3]
            prior_detail = existing[4] or ""
            prior_seen_count = int(existing[5] or 1)

            changed = (
                title != prior_title
                or severity != prior_severity
                or detail != prior_detail
            )
            first_repeat = prior_seen_count <= 1
            stale = (datetime.now(UTC) - prior_updated_at) >= throttle
            write = changed or first_repeat or stale

            if write:
                seen = prior_seen_count + 1
                patch = {
                    "seen_count": seen,
                    "severity": severity,
                    "detail": detail,
                }
                store.update_ref(ref_id, title=title, meta_patch=patch, conn=conn)
                # Severity can change between sightings (a loop that gets
                # worse); keep exactly one severity: tag.
                _set_severity_tag(store, ref_id, severity, conn=conn)
            return ref_id, False

        meta: dict[str, Any] = {
            "alert_source": source,
            "fingerprint": fingerprint,
            "severity": severity,
            "detail": detail,
            "seen_count": 1,
        }
        if subject_ref_id is not None:
            meta["subject_ref_id"] = int(subject_ref_id)
        ref = store.insert_ref(
            kind="alert", slug=None, title=title, meta=meta, conn=conn
        )
        # Dual-write the dedup identity onto the real columns (migration
        # 0099) — the partial unique index and every subsequent dedup
        # lookup key off these, not the meta copy above (kept for
        # rollback safety / uniform shape).
        conn.execute(
            "UPDATE refs SET alert_source = %s, fingerprint = %s WHERE ref_id = %s",
            (source, fingerprint, ref.id),
        )
        for tag in (
            Tag.open(STATE_OPEN),
            Tag.open(f"alert-source:{source}"),
            Tag.open(f"severity:{severity}"),
        ):
            store.add_tag(ref.id, tag, set_by="system", conn=conn)
        return int(ref.id), True


def resolve_stale_alerts(
    store: Store,
    *,
    source: str,
    live_fingerprints: Iterable[str],
) -> int:
    """Resolve open alerts of ``source`` whose condition has cleared.

    An open alert whose fingerprint is not in ``live_fingerprints`` is
    flipped ``alert-state:open`` → ``alert-state:resolved`` and stamped
    ``resolved_at`` (both the real column and, dual-written, ``meta``).
    Returns the number resolved. The row is kept for history. Call this
    once per detector pass with the full current fingerprint set so a
    fixed problem disappears from the open list on the next pass. The
    tag flip and the ``resolved_at`` column write happen in the same
    transaction — the invariant is that they flip together, since the
    partial unique index (0099) keys off ``resolved_at IS NULL``, not
    the tag.
    """
    live = set(live_fingerprints)
    resolved = 0
    with store.tx() as conn:
        # Rolling-deploy transition shim (same as raise_alert's dedup
        # SELECT): an old-code node may have left open alert rows with the
        # alert_source/fingerprint COLUMNS NULL (meta-only write). The
        # COALESCE fallback lets this pass still find and resolve them.
        # Drop once no open alert row has NULL alert_source/fingerprint
        # columns.
        rows = conn.execute(
            """
            SELECT r.ref_id, COALESCE(r.fingerprint, r.meta->>'fingerprint')
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND COALESCE(r.alert_source, r.meta->>'alert_source') = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
            """,
            (source, STATE_OPEN),
        ).fetchall()
        for ref_id_raw, fp in rows:
            if fp in live:
                continue
            ref_id = int(ref_id_raw)
            store.remove_tag(ref_id, Tag.open(STATE_OPEN), conn=conn)
            store.add_tag(ref_id, Tag.open(STATE_RESOLVED), set_by="system", conn=conn)
            conn.execute(
                "UPDATE refs SET resolved_at = now(), "
                "meta = meta || jsonb_build_object("
                "'resolved_at', to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SSOF')), "
                "updated_at = now() WHERE ref_id = %s",
                (ref_id,),
            )
            resolved += 1
    return resolved


def list_open_alerts(store: Store, *, limit: int = 200) -> list[dict[str, Any]]:
    """Open alerts, newest-first, with source / severity / counters.

    Shared read used by the ``/alerts`` web tab (and available to any
    operator preamble). Pure SQL — no embedder, no handler.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   r.title,
                   r.alert_source            AS source,
                   r.meta->>'severity'       AS severity,
                   r.meta->>'detail'         AS detail,
                   r.meta->>'subject_ref_id' AS subject_ref_id,
                   COALESCE((r.meta->>'seen_count')::int, 1) AS seen_count,
                   r.created_at,
                   r.updated_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = %s
             ORDER BY CASE r.meta->>'severity'
                        WHEN 'critical' THEN 0
                        WHEN 'warn' THEN 1
                        ELSE 2 END,
                      r.updated_at DESC
             LIMIT %s
            """,
            (STATE_OPEN, limit),
        ).fetchall()
    return [
        {
            "ref_id": int(r[0]),
            "title": r[1],
            "source": r[2],
            "severity": r[3],
            "detail": r[4],
            "subject_ref_id": int(r[5]) if r[5] is not None else None,
            "seen_count": int(r[6]),
            "created_at": r[7],
            "updated_at": r[8],
        }
        for r in rows
    ]


def _set_severity_tag(store: Store, ref_id: int, severity: str, *, conn: Any) -> None:
    """Keep exactly one ``severity:`` open tag on an alert."""
    for sev in SEVERITIES:
        if sev != severity:
            store.remove_tag(ref_id, Tag.open(f"severity:{sev}"), conn=conn)
    store.add_tag(ref_id, Tag.open(f"severity:{severity}"), set_by="system", conn=conn)


#: Env var holding the Discord delivery target for critical-alert pushes — a
#: ``discord/<guild>/<channel>[/<thread>]`` string, the *same* channel the daily
#: news briefing is delivered to. Unset by default, so the push path merges
#: dark: alerts still land in the ``/alerts`` tab and agent triage surface, but
#: nothing is posted until an operator wires the channel. Set it to actually get
#: paged (the whole point of severity ``critical`` — a stalled planner or a dead
#: worker that would otherwise fester unseen for days). Delivery reuses the
#: asa_bot bot + ``pg_notify('precis.messages')`` path (there are no Discord
#: webhooks in this deployment — the bridge is a bot), so no new secret.
#: The value is a **Discord channel target** — ``discord/<guild>/<channel>`` —
#: NOT an outbound webhook URL. There are no Discord webhooks in this deployment
#: (the bridge is the asa_bot bot via ``pg_notify('precis.messages')``), so a
#: ``https://…`` URL here is silently undeliverable. ``PRECIS_OPS_ALERT_TARGET``
#: is the canonical, honestly-named variable; ``PRECIS_OPS_ALERT_WEBHOOK`` is a
#: deprecated alias kept only so an already-deployed env keeps working.
OPS_ALERT_TARGET_ENV = "PRECIS_OPS_ALERT_TARGET"
OPS_ALERT_WEBHOOK_ENV = "PRECIS_OPS_ALERT_WEBHOOK"  # deprecated misnomer alias


def _ops_alert_target() -> str:
    """Return the configured ops-alert **channel target** (not a webhook URL).

    ``PRECIS_OPS_ALERT_TARGET`` is canonical (the value is a Discord channel
    ``discord/<guild>/<channel>``); the historical ``PRECIS_OPS_ALERT_WEBHOOK``
    name is a misnomer — kept as an accepted fallback so a deployment that set
    it still pages — but it is NOT an outbound webhook and a URL there won't
    deliver.
    """
    return os.environ.get(OPS_ALERT_TARGET_ENV, "") or os.environ.get(
        OPS_ALERT_WEBHOOK_ENV, ""
    )


def notify_critical_alert(
    store: Store, title: str, detail: str = "", *, fingerprint: str = ""
) -> bool:
    """Best-effort proactive push for a newly-raised critical alert.

    Queues a ``kind='message'`` to the Discord channel configured by
    ``PRECIS_OPS_ALERT_TARGET`` (or the deprecated ``PRECIS_OPS_ALERT_WEBHOOK``
    alias) and fires ``pg_notify('precis.messages', …)`` in the same tx, as
    ``MessageHandler.put`` / ``briefing._deliver`` do — asa_bot (the one
    process holding a Discord socket) then posts it. Returns ``True`` if a
    push was queued, ``False`` if no target is configured (the default —
    dark until an operator wires the channel). Never raises: a failed push
    must not break the detector pass. Call only on the *first* sighting of
    a ``critical`` alert (``raise_alert`` → ``is_new``), so a standing
    condition pages once, not every minute.
    """
    from precis.store.types import BlockInsert

    target = _ops_alert_target().strip()
    if not target:
        return False
    body = f"🚨 {title}"
    if detail:
        body += f"\n{detail}"
    try:
        with store.tx() as conn:
            meta = {
                "target": target,
                "status": "queued",
                "reason": f"ops-alert {fingerprint or title}",
                "author": "asa",
                "proactive": True,
            }
            ref = store.insert_ref(
                kind="message",
                slug=None,
                title=f"🚨 {title}"[:200],
                meta=meta,
                conn=conn,
            )
            store.insert_blocks(
                ref.id,
                [BlockInsert(pos=0, text=body, meta={"chunk_kind": "message_body"})],
                conn=conn,
            )
            conn.execute(
                "SELECT pg_notify('precis.messages', %s)",
                (json.dumps({"ref_id": ref.id, "target": target, "author": "asa"}),),
            )
    except Exception:
        log.warning("notify_critical_alert: push failed", exc_info=True)
    return True


__all__ = [
    "ALERT_RERAISE_THROTTLE_ENV",
    "OPS_ALERT_TARGET_ENV",
    "OPS_ALERT_WEBHOOK_ENV",
    "SEVERITIES",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "list_open_alerts",
    "notify_critical_alert",
    "raise_alert",
    "resolve_stale_alerts",
]
