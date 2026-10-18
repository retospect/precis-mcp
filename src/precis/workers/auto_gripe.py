"""Shared lifecycle for system-filed gripes (health_digest router, call_errors).

A worker that files a gripe for a condition it can observe owns two ends of
that gripe's life:

- **Filing prio.** :data:`AUTO_PRIO` (7) sinks auto-filed gripes below the
  unset default (5), so the open view (PRIO, then oldest) puts human-filed
  gripes first. A critical-severity condition files at :data:`CRIT_PRIO`
  (4). Coordinator ruling 2026-10-18 (gripe system batch F).
- **Closing when the condition clears.** :func:`close_auto_gripe` writes a
  normal close: a ``resolved: auto-closed: …`` comment, then
  ``STATUS:done``. It never soft-deletes: the 2026-10-18 audit found 621
  gripes retired while still ``STATUS:open`` with no record of why, which
  made the open/closed counts untrue.

Writes go straight through the store (these passes run write-capable), in
one transaction, so the closed STATUS and its ``resolved:`` comment land
together.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from precis.store import Store

log = logging.getLogger(__name__)

#: Filing prio for a system-filed gripe (unset counts as 5 in the sort).
AUTO_PRIO = 7
#: Filing prio for a critical-severity condition.
CRIT_PRIO = 4


def close_auto_gripe(
    store: Store, gripe_id: int, *, reason: str, last_seen: datetime
) -> None:
    """Close one system-filed gripe whose condition cleared.

    Appends ``resolved: auto-closed: <reason> (not seen since <UTC time>)``
    and sets ``STATUS:done`` in the same transaction.
    """
    from precis.store.types import ChunkInsert, Tag

    next_pos = len(store.chunks.list_chunks_for_ref(gripe_id))
    seen = last_seen.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    comment = f"resolved: auto-closed: {reason} (not seen since {seen})"
    with store.tx() as conn:
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=next_pos, text=comment, meta={"chunk_kind": "gripe_comment"}
                )
            ],
            conn=conn,
        )
        store.add_tag(
            gripe_id,
            Tag.closed("STATUS", "done"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    log.info("auto_gripe: closed gripe id=%d (%s)", gripe_id, reason)
