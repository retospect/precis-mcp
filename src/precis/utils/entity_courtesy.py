"""Read-time entity state for memory prose and link-neighbour rows.

One query reads the referenced rows, STATUS tags and heartbeat overlay together.
Do not use resolve_handle here: it follows supersession, hiding the tombstone
whose redirect the reader needs. Nothing is persisted or added to index bullets.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store.store import Store

_TOKEN = re.compile(r"\b[a-z]{2}\d+\b", re.IGNORECASE)
log = logging.getLogger(__name__)


def entity_handles(text: str) -> list[str]:
    """Distinct entity record handles, in first-mention order."""
    handles: dict[str, None] = {}
    for match in _TOKEN.finditer(text):
        parsed = handle_registry.parse(match.group().lower())
        if parsed is not None:
            kind, is_chunk, pk = parsed
            if not is_chunk and kind != "profile":
                handles.setdefault(handle_registry.format_handle(kind, pk), None)
    return list(handles)


def entity_states(store: Store, handles: Iterable[str]) -> dict[str, str]:
    """One batched lookup, including retired rows; absent/wrong-kind ids vanish."""
    wanted = {
        handle: parsed
        for handle in handles
        if (parsed := handle_registry.parse(handle)) is not None
        and not parsed[1]
        and parsed[0] != "profile"
    }
    if not wanted:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.kind, r.title, r.meta, r.retired_at, h.ts, "
            "(SELECT ri.id_value FROM ref_identifiers ri "
            " WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
            " ORDER BY ri.created_at DESC LIMIT 1) AS slug, "
            "(SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            " WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' "
            " ORDER BY t.value LIMIT 1) "
            "FROM refs r LEFT JOIN host_heartbeat h "
            "ON r.kind = 'machine' AND h.host = r.meta->>'host' "
            "WHERE r.ref_id = ANY(%s)",
            (sorted({p[2] for p in wanted.values()}),),
        ).fetchall()
    from precis.handlers.machine import status_of
    from precis.store._heartbeat_ops import HostHeartbeat
    from precis.utils.surface_scope import masked_ref_placeholder

    now = datetime.now(UTC)
    states: dict[str, str] = {}
    for rid, kind, title, meta, retired_at, heartbeat_at, slug, status in rows:
        handle = handle_registry.try_format(kind, int(rid))
        if handle is None or handle not in wanted:
            continue
        if masked_ref_placeholder(SimpleNamespace(kind=kind, slug=slug)) is not None:
            continue
        meta = meta or {}
        state = ""
        if retired_at is not None:
            state = "retired"
            successor = meta.get("superseded_by")
            if successor is not None:
                successor_handle = (
                    handle_registry.format_handle(kind, int(successor))
                    if str(successor).isdigit()
                    else str(successor)
                )
                state += f" → {successor_handle}"
        elif kind == "machine":
            heartbeat = (
                HostHeartbeat("", heartbeat_at, None, None, None, None, {})
                if heartbeat_at is not None
                else None
            )
            state = (
                f"role: {meta.get('role') or '—'} · "
                f"status: {status_of(heartbeat, now=now)}"
            )
        elif kind in {"gripe", "todo"}:
            state = f"STATUS:{status or 'open'}"
        elif kind == "skill":
            from precis.handlers.skill import _skill_title

            state = _skill_title(str(slug)) or str(title or "")
        elif kind == "markdown":
            state = str(title or "")
        if state:
            states[handle] = " ".join(state.split())
    return states


def safe_entity_states(store: Store, handles: Iterable[str]) -> dict[str, str]:
    """Optional render overlay; lookup failures must not hide the source body."""
    try:
        return entity_states(store, handles)
    except Exception:
        log.debug("entity courtesy read failed", exc_info=True)
        return {}


def courtesy_lines(
    handles: Iterable[str], states: Mapping[str, str], *, cap: int
) -> str:
    """One line per resolved handle, with an explicit overflow count."""
    lines = [f"  {h} — {states[h]}" for h in dict.fromkeys(handles) if h in states]
    if not lines:
        return ""
    shown = lines[:cap]
    if len(lines) > cap:
        shown.append(f"  … +{len(lines) - cap} more")
    return "\n".join(["— referenced entities —", *shown])
