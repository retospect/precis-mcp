"""Read recorded entity changes without treating bookkeeping as evidence.

Most entity kinds do not enable revision history. A newer updated_at is
therefore only a reason to look, never proof that content or role changed.
Missing or discontinuous SHA history is explicitly not_checked. STATUS is
outside the SHA registry: only a dated status event followed by a different
current assignment can prove a change, not the assignment timestamp alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis.utils.handle_registry import parse as parse_handle

if TYPE_CHECKING:
    from precis.store import Store

HISTORY_CAP = 100
ENTITY_KINDS = frozenset({"machine", "skill", "markdown", "gripe", "todo"})


def ref_change(store: Store, handle: str, touched: datetime) -> dict[str, Any]:
    """Compare a bounded, sealed revision chain with the current entity.

    A prior state recorded *after* the memory touch is sufficient: a change
    from that state to now necessarily postdates the memory. This also avoids
    inventing a baseline when the first recorded revision is much later.
    """
    parsed = parse_handle(handle)
    kind: str | None = None
    rid = 0
    if parsed and not parsed[1] and parsed[0] in ENTITY_KINDS:
        kind, rid = parsed[0], parsed[2]
    elif handle.startswith("ma_"):
        kind = "machine"
    elif handle.startswith("precis-"):
        kind = "skill"
    if kind is None:
        return {"status": "not_checked", "reason": "unsupported entity handle"}
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id, r.kind, r.meta, r.updated_at, "
            "precis_target_sha('ref', r.ref_id), "
            "(SELECT i.id_value FROM ref_identifiers i WHERE i.ref_id = r.ref_id "
            "AND i.id_kind = 'cite_key' LIMIT 1), "
            "coalesce((SELECT string_agg(c.text, E'\\n' ORDER BY c.ord, c.chunk_id) "
            "FROM chunks c WHERE c.ref_id = r.ref_id AND c.ord >= 0 "
            "AND c.retired_at IS NULL), r.title) "
            "FROM refs r WHERE r.kind = %(kind)s AND r.retired_at IS NULL "
            "AND (r.ref_id = %(rid)s OR (%(rid)s = 0 AND EXISTS ("
            "SELECT 1 FROM ref_identifiers i WHERE i.ref_id = r.ref_id "
            "AND i.id_kind = 'cite_key' AND i.id_value = %(handle)s))) LIMIT 1",
            {"kind": kind, "rid": rid, "handle": handle},
        ).fetchone()
        if row is None:
            return {"status": "not_checked", "reason": "entity unavailable"}
        rid, kind, meta, updated, current_sha, slug, text = row
        revisions = conn.execute(
            "SELECT at, prev_sha, new_sha, prev_state FROM revisions "
            "WHERE target_kind = 'ref' AND target_id = %s AND at > %s "
            "ORDER BY at, revision_id LIMIT %s",
            (rid, touched, HISTORY_CAP + 1),
        ).fetchall()
        status_row = conn.execute(
            "SELECT t.value, rt.created_at FROM ref_tags rt "
            "JOIN tags t USING (tag_id) WHERE rt.ref_id = %s "
            "AND t.namespace = 'STATUS' ORDER BY rt.created_at DESC LIMIT 1",
            (rid,),
        ).fetchone()
        # These events are used by todo completion. Generic 'retagged' reason
        # records deliberately do not qualify: they carry no old status.
        status_event = (
            conn.execute(
                "SELECT event, ts FROM ref_events WHERE ref_id = %s "
                "AND ts > %s AND ts < %s AND starts_with(event, 'status:') "
                "ORDER BY ts, event_id LIMIT 1",
                (rid, touched, status_row[1]),
            ).fetchone()
            if status_row and status_row[1] > touched
            else None
        )
    info: dict[str, Any] = {
        "ref_id": int(rid),
        "kind": kind,
        "slug": slug,
        "new_sha": current_sha,
        "excerpt": str(text)[:1200],
    }
    changes: dict[str, Any] = {}
    unchecked: list[str] = []
    if len(revisions) > HISTORY_CAP:
        unchecked.append("revision history exceeds bound")
    elif revisions:
        prior = revisions[0][1]
        last = prior
        complete = bool(prior)
        for _at, before, after, _state in revisions:
            if not before or not after or before != last:
                complete = False
                break
            last = after
        if complete and last == current_sha:
            if prior != current_sha:
                changes["content"] = {"old_sha": prior, "new_sha": current_sha}
            info["old_sha"] = prior
        else:
            unchecked.append("prior SHA chain cannot be established")
        # A recorded row preserves role even when the SHA excludes it. A
        # different current role is independently verifiable evidence.
        previous_meta = revisions[0][3].get("meta") or {}
        if kind == "machine":
            if "role" in previous_meta and "role" in meta:
                if previous_meta["role"] != meta["role"]:
                    changes["role"] = {
                        "old": previous_meta["role"],
                        "new": meta["role"],
                    }
            else:
                unchecked.append("prior machine role unavailable")
        info["changed_after"] = revisions[0][0].isoformat()
    elif updated > touched:
        unchecked.append("prior SHA unavailable")
    if status_event and status_row:
        old_status = status_event[0].removeprefix("status:")
        if old_status != status_row[0]:
            changes["STATUS"] = {
                "old": old_status,
                "new": status_row[0],
                "observed_at": status_event[1].isoformat(),
                "changed_at": status_row[1].isoformat(),
            }
    elif status_row and status_row[1] > touched:
        unchecked.append("prior STATUS unavailable")
    if changes:
        return {
            **info,
            "status": "changed",
            "changes": changes,
            "not_checked": unchecked,
        }
    if unchecked:
        return {**info, "status": "not_checked", "reason": "; ".join(unchecked)}
    # No recorded transition is not proof of an unchanged body: several
    # kinds replace chunks without updating the ref row or keeping history.
    if not revisions:
        return {**info, "status": "not_checked", "reason": "prior SHA unavailable"}
    return {**info, "status": "unchanged"}
