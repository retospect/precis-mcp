"""The nightly ``(unrecorded)`` revisions count — local-mesh-upkeep §2's
"nightly count of those rows names the write paths to fix", fired from
the ``revisions_unrecorded`` scheduler cadence.

A covered write made outside ``precis.revisions.revision_context`` still
lands in the log — the 0185 trigger records it with
``reason='(unrecorded)'`` and the DB role as actor. This pass counts the
outstanding rows and groups them by what names the writer (target kind,
event, actor, and the covered subject — a ref's kind, a link's
relation), then reports:

- one ``log.warning`` line per group (``log.info`` when the count is
  zero), and
- ``app_settings['revisions_unrecorded:last']`` — a JSON snapshot
  ``{at, total, groups}`` the count stays queryable through after the
  worker log rotates, the same ``app_settings`` idiom
  ``workers/memory_recall.py`` uses for its last score.

The report is cumulative, not windowed: an unwrapped write path keeps
appearing until it is wrapped, which is the point — the count names the
paths still to fix, not just last night's damage. Read-only over the
mesh itself; the one write is the ``app_settings`` marker row.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``app_settings`` key holding the last report — read for the
#: comparison (a shrinking count is the fix working) and to keep the
#: count visible without digging through worker logs.
LAST_REPORT_KEY = "revisions_unrecorded:last"


def run_unrecorded_revisions_pass(store: Store) -> BatchResult:
    """Count every ``revisions`` row with ``reason='(unrecorded)'`` and
    report the per-write-path breakdown. ``claimed`` = rows counted
    (the pass's whole job is to look at them), ``ok`` = same — nothing
    here can fail a row."""
    from precis.budget import settings as app_settings

    epoch = datetime.min.replace(tzinfo=UTC)
    total = store.unrecorded_revision_count(epoch)
    groups = store.unrecorded_revision_breakdown(epoch)
    if total == 0:
        log.info("revisions_unrecorded: 0 — every covered write path set a reason")
    else:
        log.warning(
            "revisions_unrecorded: %d revision(s) without a reason across %d path(s):",
            total,
            len(groups),
        )
        for g in groups:
            log.warning(
                "  %dx %s %s on %s %s",
                g["count"],
                g["actor"],
                g["event"],
                g["target_kind"],
                g["subject"],
            )
    doc = {
        "at": datetime.now(UTC).isoformat(),
        "total": total,
        "groups": groups,
    }
    try:
        app_settings.set_setting(store, LAST_REPORT_KEY, json.dumps(doc))
    except Exception:
        # The app_settings write is the report's durable half, but a
        # count pass must not fail the cadence over a marker row — the
        # log lines above already carried the numbers.
        log.exception("revisions_unrecorded: app_settings marker write failed")
    return BatchResult(
        handler="revisions_unrecorded", claimed=total, ok=total, failed=0
    )


__all__ = ["LAST_REPORT_KEY", "run_unrecorded_revisions_pass"]
