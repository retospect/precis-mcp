"""Fleet coordination rows — write side (``kind='fleet'``).

One numeric ref per agent tree (``meta.type='agent'``, key
``<vendor>:<host>:<project>:<tree>``) plus one per host
(``meta.type='host'``, key ``host:<host>``). The per-host collector
(``scripts/fleet-report --push``) sends its ``--json`` report through
``put(kind='fleet', mode='report')``; :func:`apply_report` diffs it against
the host's live rows under one transaction and writes only what changed.
Design: ``docs/backlog/fleet-coordination-via-precis.md`` (step 2).

No unique index backs the key: the per-host advisory lock makes the
load/insert race-free, and leaving ``meta`` unindexed keeps updates HOT.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis.store import Store

KIND = "fleet"

#: Fields a report owns on an agent row. ``pane``/``attach`` go to explicit
#: ``None`` when they vanish (``meta || patch`` merges top level only).
REPORTER_FIELDS: tuple[str, ...] = (
    "vendor",
    "host",
    "project",
    "tree",
    "branch",
    "purpose",
    "state",
    "last_active",
    "ctx_pct",
    "dirty",
    "ahead",
    "behind",
    "pane",
    "attach",
    "exceptions",
)
#: Fields only the coordinator edits; a report never touches them.
COORDINATOR_FIELDS: tuple[str, ...] = ("assigned", "slice", "note")

#: A host whose last report is older than this shows its agents as stale.
STALE_HOST_S = 180
#: Dead agent rows are retired this long after they went dead.
DEAD_RETENTION_HOURS = 24


#: What a vanished tree becomes: dead, no exceptions (it would sit under
#: EXCEPTIONS until GC), no pane/attach (the line no longer works).
_DEAD_PATCH: dict[str, Any] = {
    "state": "dead",
    "exceptions": [],
    "pane": None,
    "attach": None,
}


def agent_key(vendor: str, host: str, project: str, tree: str) -> str:
    return f"{vendor}:{host}:{project}:{tree}"


def host_key(host: str) -> str:
    return f"host:{host}"


def exception_code(reason: str) -> str:
    """Short code for a ``fleet-report`` exception reason string."""
    r = reason.lower()
    if r.startswith("waiting"):
        return "waiting"
    if r.startswith("question"):
        return "asking"
    if r.startswith("quiet"):
        return "quiet"
    if r.startswith("context"):
        return "context"
    return r.split()[0] if r.split() else "unknown"


def _agent_fields(row: dict[str, Any], host: str, codes: list[str]) -> dict[str, Any]:
    tmux = row.get("tmux") or {}
    return {
        "vendor": row.get("vendor"),
        "host": host,
        "project": row.get("project"),
        "tree": row.get("tree"),
        "branch": row.get("branch") or None,
        "purpose": row.get("purpose") or None,
        "state": row.get("state"),
        "last_active": row.get("last_active"),
        "ctx_pct": row.get("ctx_pct"),
        "dirty": row.get("dirty"),
        "ahead": row.get("ahead"),
        "behind": row.get("behind"),
        "pane": tmux.get("pane_id") if isinstance(tmux, dict) else None,
        "attach": row.get("attach") or None,
        "exceptions": sorted(set(codes)),
    }


def _fold_rows(report: dict[str, Any], host: str) -> dict[str, dict[str, Any]]:
    """``{key: fields}``: one row per (vendor, project, tree), newest
    ``last_active`` winning when a tree has several sessions."""
    codes_by_who: dict[str, list[str]] = {}
    for e in report.get("exceptions") or []:
        codes_by_who.setdefault(str(e.get("who")), []).append(
            exception_code(str(e.get("reason") or ""))
        )
    out: dict[str, dict[str, Any]] = {}
    for row in report.get("rows") or []:
        vendor, project, tree = row.get("vendor"), row.get("project"), row.get("tree")
        if not (vendor and project and tree):
            continue
        who = f"{vendor}@{host}/{project}/{tree}"
        fields = _agent_fields(row, host, codes_by_who.get(who, []))
        key = agent_key(vendor, host, project, tree)
        prev = out.get(key)
        if prev is None or (fields["last_active"] or "") > (prev["last_active"] or ""):
            out[key] = fields
    return out


def exceptions_hash(report: dict[str, Any]) -> str:
    items = sorted(
        (str(e.get("who")), str(e.get("reason")))
        for e in report.get("exceptions") or []
    )
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()[:16]


@dataclass
class ReportResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dead: int = 0

    def summary(self, host: str) -> str:
        return (
            f"reported host={host}: {self.inserted} new, {self.updated} updated, "
            f"{self.unchanged} unchanged, {self.dead} marked dead"
        )


def apply_report(
    store: Store,
    *,
    host: str,
    report: dict[str, Any],
    now: datetime | None = None,
) -> ReportResult:
    """Fold one host's ``fleet-report --json`` into fleet rows (one tx)."""
    now = now or datetime.now(UTC)
    wanted = _fold_rows(report, host)
    res = ReportResult()
    hkey = host_key(host)
    host_fields: dict[str, Any] = {
        "type": "host",
        "key": hkey,
        "host": host,
        "reported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quota": report.get("quota") or {},
        "reporter_version": report.get("version"),
        "exceptions_hash": exceptions_hash(report),
    }
    with store.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"fleet:{host}",))
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs "
            "WHERE kind = %s AND retired_at IS NULL AND meta->>'host' = %s",
            (KIND, host),
        ).fetchall()
        existing: dict[str, tuple[int, dict[str, Any]]] = {}
        for ref_id, meta in rows:
            meta = meta or {}
            if meta.get("key"):
                existing[str(meta["key"])] = (int(ref_id), meta)

        for key, fields in wanted.items():
            found = existing.get(key)
            if found is None:
                meta = {"type": "agent", "key": key}
                meta.update({k: v for k, v in fields.items() if v is not None})
                store.insert_ref(kind=KIND, slug=None, title=key, meta=meta, conn=conn)
                res.inserted += 1
                continue
            ref_id, old = found
            # An absent key and an explicit None compare equal (both read as None).
            patch = {k: v for k, v in fields.items() if old.get(k) != v}
            if patch:
                store.update_ref(ref_id, meta_patch=patch, conn=conn)
                res.updated += 1
            else:
                res.unchanged += 1

        for key, (ref_id, old) in existing.items():
            if old.get("type") != "agent" or key in wanted:
                continue
            if old.get("state") != "dead":
                store.update_ref(ref_id, meta_patch=_DEAD_PATCH, conn=conn)
                res.dead += 1

        hfound = existing.get(hkey)
        if hfound is None:
            store.insert_ref(
                kind=KIND, slug=None, title=hkey, meta=host_fields, conn=conn
            )
        else:
            store.update_ref(hfound[0], meta_patch=host_fields, conn=conn)
    return res


def gc_dead_rows(store: Store, *, older_than_hours: int = DEAD_RETENTION_HOURS) -> int:
    """Soft-delete fleet rows nobody will report again.

    Agent rows ``dead`` past the window (a dead row gets no further writes,
    so ``updated_at`` is when it went dead), and every row of a host whose
    own row has not been written for the window (the host stopped
    reporting, so nothing would ever mark its agents dead). Returns the
    number retired.
    """
    window = f"{int(older_than_hours)} hours"
    with store.tx() as conn:
        cur = conn.execute(
            "UPDATE refs SET retired_at = now(), updated_at = now() "
            "WHERE kind = %s AND retired_at IS NULL "
            "  AND meta->>'type' = 'agent' AND meta->>'state' = 'dead' "
            "  AND updated_at < now() - %s::interval",
            (KIND, window),
        )
        n = cur.rowcount or 0
        cur = conn.execute(
            "UPDATE refs r SET retired_at = now(), updated_at = now() "
            "WHERE r.kind = %s AND r.retired_at IS NULL "
            "  AND r.meta->>'host' IN ("
            "    SELECT h.meta->>'host' FROM refs h "
            "    WHERE h.kind = %s AND h.retired_at IS NULL "
            "      AND h.meta->>'type' = 'host' "
            "      AND h.updated_at < now() - %s::interval)",
            (KIND, KIND, window),
        )
        return n + (cur.rowcount or 0)


__all__ = [
    "COORDINATOR_FIELDS",
    "REPORTER_FIELDS",
    "ReportResult",
    "agent_key",
    "apply_report",
    "exception_code",
    "gc_dead_rows",
    "host_key",
]
