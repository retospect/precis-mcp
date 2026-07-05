"""Alert producer — the write side of ``kind='alert'``.

A small, store-only surface any worker can call to raise a
machine-detected operational / health condition. Peer to the
agent-facing :class:`precis.handlers.alert.AlertHandler` (the read /
ack side). Kept out of the handler so a background pass (nursery,
sweeper, quota_check, …) can raise an alert without going through the
seven-verb dispatch layer.

Lifecycle, all on the shared ``refs`` table (no new columns):

* **raise** — :func:`raise_alert` upserts on ``(alert_source,
  fingerprint)``. A first sighting inserts a new ``alert`` ref tagged
  ``alert-state:open`` + ``alert-source:<source>`` + ``severity:<sev>``.
  A repeat sighting of a still-open alert bumps ``meta.seen_count`` and
  ``updated_at`` instead of writing a duplicate — this is the dedup the
  old memory-digest fingerprint approximated, but per-condition rather
  than per-digest, so a single churning condition can't spam the table.
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
from collections.abc import Iterable
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
    """
    severity = _norm_severity(severity)
    with store.tx() as conn:
        # Serialize concurrent raises of the SAME (source, fingerprint)
        # across the cluster's many nursery instances. The SELECT-then-
        # INSERT dedup below is not atomic on its own, so without this two
        # nodes could both miss the existing open alert and both INSERT —
        # which the partial unique index uq_alert_open_source_fingerprint
        # (migration 0030) would then reject with a violation. The
        # transaction-scoped advisory lock makes the check-then-insert
        # atomic per fingerprint; it releases at COMMIT/ROLLBACK. The
        # two-arg (classid, objid) form hashes source and fingerprint
        # separately, so "a"+"bc" can't alias "ab"+"c" and there's no NUL
        # separator (PostgreSQL text rejects 0x00).
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (source, fingerprint),
        )
        existing = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND r.meta->>'alert_source' = %s
               AND r.meta->>'fingerprint' = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            (source, fingerprint, STATE_OPEN),
        ).fetchone()

        if existing is not None:
            ref_id = int(existing[0])
            prior = dict(existing[1] or {})
            seen = int(prior.get("seen_count", 1)) + 1
            patch = {
                "seen_count": seen,
                "severity": severity,
                "detail": detail,
            }
            conn.execute(
                """
                UPDATE refs
                   SET title = %s,
                       meta = meta || %s::jsonb,
                       updated_at = now()
                 WHERE ref_id = %s
                """,
                (title, json.dumps(patch), ref_id),
            )
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
    ``meta.resolved_at``. Returns the number resolved. The row is kept
    for history. Call this once per detector pass with the full current
    fingerprint set so a fixed problem disappears from the open list on
    the next pass.
    """
    live = set(live_fingerprints)
    resolved = 0
    with store.tx() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->>'fingerprint'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert'
               AND r.deleted_at IS NULL
               AND r.meta->>'alert_source' = %s
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
                "UPDATE refs SET meta = meta || jsonb_build_object("
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
                   r.meta->>'alert_source'   AS source,
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


#: Env var holding a Discord webhook URL for critical-alert pushes. Unset by
#: default, so the push path merges dark: alerts still land in the ``/alerts``
#: tab and agent triage surface, but nothing leaves the cluster until an
#: operator wires a channel. Set it to actually get paged (the whole point of
#: severity ``critical`` — a stalled planner or a dead worker that would
#: otherwise fester unseen for days). Parallel to the fixer's own
#: ``PRECIS_FIXER_DISCORD_WEBHOOK``.
OPS_ALERT_WEBHOOK_ENV = "PRECIS_OPS_ALERT_WEBHOOK"


def notify_critical_alert(title: str, detail: str = "") -> bool:
    """Best-effort Discord push for a newly-raised critical alert.

    Fires a fire-and-forget HTTP POST to the webhook in
    :data:`OPS_ALERT_WEBHOOK_ENV`. Returns ``True`` if a push was
    attempted, ``False`` if no webhook is configured (the default — the
    push path is off until an operator opts in). Never raises: a failed
    push must not break the detector pass that called it, so network /
    HTTP errors are swallowed with a warning. Intended to be called only
    on the *first* sighting of a ``critical`` alert (``raise_alert`` →
    ``is_new``), so a standing condition pages once, not every minute.
    """
    import os
    import urllib.error
    import urllib.request

    webhook = os.environ.get(OPS_ALERT_WEBHOOK_ENV, "").strip()
    if not webhook:
        return False
    content = f"🚨 **{title}**"
    if detail:
        content += f"\n{detail}"
    # Discord caps message content at 2000 chars.
    body = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except (urllib.error.URLError, OSError, ValueError):
        log.warning("notify_critical_alert: push failed", exc_info=True)
    return True


__all__ = [
    "OPS_ALERT_WEBHOOK_ENV",
    "SEVERITIES",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "list_open_alerts",
    "notify_critical_alert",
    "raise_alert",
    "resolve_stale_alerts",
]
