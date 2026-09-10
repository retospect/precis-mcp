"""Store ops for the ``checklist`` kind (docs/backlog/checklist-kind.md,
slice 1) — named, versioned check ledgers with per-target accumulating
verdicts. See ``migrations/0158_checklist_kind.sql`` for the schema and
the write-ownership rules this module enforces:

- ``checklist_items`` is append-only per ``(checklist_id, name)``:
  :meth:`checklist_item_add_rev` always inserts a new row (never an
  UPDATE); "current" is derived as the highest live rev.
- ``checklist_verdicts`` is append-only: :meth:`checklist_verdict_insert`
  only ever inserts.
- ``checklist_notes`` follows the ``se_notes`` shape (name-keyed,
  append-only; :meth:`checklist_note_remove` retires rather than
  deletes).

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

_CHECKLIST_COLS = "id, name, origin, created_at, retired_at"
_ITEM_COLS = (
    "id, checklist_id, name, rev, phase, severity, decidability, "
    "prevents, applies, body, origin, target_ref_id, created_at, retired_at"
)
_VERDICT_COLS = (
    "id, target_ref_id, checklist_id, item_name, item_rev, verdict, "
    "evidence, fingerprint, checked_at, checked_by, retired_at"
)
_NOTE_COLS = (
    "id, target_ref_id, checklist_id, item_name, name, kind, body, re, "
    "about, origin, created_at, retired_at"
)


def _row_to_checklist(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "origin": row[2],
        "created_at": row[3],
        "retired_at": row[4],
    }


def _row_to_item(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "checklist_id": row[1],
        "name": row[2],
        "rev": row[3],
        "phase": row[4],
        "severity": row[5],
        "decidability": row[6],
        "prevents": row[7],
        "applies": row[8],
        "body": row[9],
        "origin": row[10],
        "target_ref_id": row[11],
        "created_at": row[12],
        "retired_at": row[13],
    }


def _row_to_verdict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "target_ref_id": row[1],
        "checklist_id": row[2],
        "item_name": row[3],
        "item_rev": row[4],
        "verdict": row[5],
        "evidence": row[6],
        "fingerprint": row[7],
        "checked_at": row[8],
        "checked_by": row[9],
        "retired_at": row[10],
    }


def _row_to_note(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "target_ref_id": row[1],
        "checklist_id": row[2],
        "item_name": row[3],
        "name": row[4],
        "kind": row[5],
        "body": row[6],
        "re": row[7],
        "about": row[8],
        "origin": row[9],
        "created_at": row[10],
        "retired_at": row[11],
    }


class ChecklistMixin:
    pool: Any
    tx: Any

    # -- checklists ------------------------------------------------------

    def checklist_get(self, name: str) -> dict[str, Any] | None:
        """A live checklist by name, or ``None``."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_CHECKLIST_COLS} FROM checklists "
                "WHERE name = %s AND retired_at IS NULL",
                (name,),
            ).fetchone()
        return None if row is None else _row_to_checklist(row)

    def checklist_get_by_id(self, checklist_id: int) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_CHECKLIST_COLS} FROM checklists WHERE id = %s",
                (checklist_id,),
            ).fetchone()
        return None if row is None else _row_to_checklist(row)

    def checklist_create(
        self, *, name: str, origin: str = "local", conn: Connection | None = None
    ) -> dict[str, Any]:
        """Insert a new checklist row. Caller has already checked ``name``
        doesn't exist (the DB UNIQUE constraint is the backstop, not the
        primary error path — the handler wants a friendlier message)."""
        sql = (
            "INSERT INTO checklists (name, origin) VALUES (%s, %s) "
            f"RETURNING {_CHECKLIST_COLS}"
        )
        if conn is not None:
            row = conn.execute(sql, (name, origin)).fetchone()
        else:
            with self.tx() as c:
                row = c.execute(sql, (name, origin)).fetchone()
        assert row is not None
        return _row_to_checklist(row)

    def checklist_list(
        self, *, q: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Live checklists, optionally name-filtered (case-insensitive
        substring)."""
        clauses = ["retired_at IS NULL"]
        params: list[Any] = []
        if q:
            clauses.append("name ILIKE %s")
            params.append(f"%{q}%")
        params.append(limit)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_CHECKLIST_COLS} FROM checklists "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY name LIMIT %s",
                params,
            ).fetchall()
        return [_row_to_checklist(r) for r in rows]

    # -- items -------------------------------------------------------------

    def checklist_item_current(
        self, checklist_id: int, name: str
    ) -> dict[str, Any] | None:
        """The current (highest-rev, live) row for one item name, or
        ``None`` when the item has no live rev (never existed, or its
        latest rev was retired)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLS} FROM checklist_items "
                "WHERE checklist_id = %s AND name = %s AND retired_at IS NULL "
                "ORDER BY rev DESC LIMIT 1",
                (checklist_id, name),
            ).fetchone()
        return None if row is None else _row_to_item(row)

    def checklist_item_max_rev(
        self, checklist_id: int, name: str, *, conn: Connection | None = None
    ) -> int:
        """Highest rev ever recorded for this item name (live or retired) —
        the append-only sequence never reuses a rev number."""
        sql = (
            "SELECT COALESCE(MAX(rev), 0) FROM checklist_items "
            "WHERE checklist_id = %s AND name = %s"
        )
        if conn is not None:
            row = conn.execute(sql, (checklist_id, name)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (checklist_id, name)).fetchone()
        assert row is not None
        return int(row[0])

    def checklist_items_current(
        self, checklist_id: int, *, target_ref_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Every live current-rev item, scoped to the checklist-wide items
        (``target_ref_id IS NULL``) plus any items scoped to
        ``target_ref_id`` when given. Ordered by phase then name for a
        stable, phase-grouped rendering."""
        clauses = ["ci.retired_at IS NULL", "ci.checklist_id = %s"]
        params: list[Any] = [checklist_id]
        if target_ref_id is None:
            clauses.append("ci.target_ref_id IS NULL")
        else:
            clauses.append("(ci.target_ref_id IS NULL OR ci.target_ref_id = %s)")
            params.append(target_ref_id)
        cols = ", ".join("ci." + c.strip() for c in _ITEM_COLS.split(", "))
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (ci.name) {cols} "
                "FROM checklist_items ci "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY ci.name, ci.rev DESC",
                params,
            ).fetchall()
        items = [_row_to_item(r) for r in rows]
        items.sort(key=lambda it: (it.get("phase") or "", it["name"]))
        return items

    def checklist_item_add_rev(
        self,
        *,
        checklist_id: int,
        name: str,
        rev: int,
        phase: str | None,
        severity: str,
        decidability: str,
        prevents: str | None,
        applies: str | None,
        body: str | None,
        origin: str,
        target_ref_id: int | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        """Insert a new item rev. Never updates a prior rev — the caller
        (handler or sync) has already computed ``rev`` (current max + 1)."""
        sql = (
            "INSERT INTO checklist_items "
            "(checklist_id, name, rev, phase, severity, decidability, "
            " prevents, applies, body, origin, target_ref_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"RETURNING {_ITEM_COLS}"
        )
        params = (
            checklist_id,
            name,
            rev,
            phase,
            severity,
            decidability,
            prevents,
            applies,
            body,
            origin,
            target_ref_id,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.tx() as c:
                row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_item(row)

    def checklist_item_retire(
        self, *, checklist_id: int, name: str, conn: Connection | None = None
    ) -> bool:
        """Retire the current live rev of one item. Returns ``False`` when
        there was nothing live to retire."""
        sql = (
            "UPDATE checklist_items SET retired_at = now() "
            "WHERE checklist_id = %s AND name = %s AND retired_at IS NULL"
        )
        if conn is not None:
            cur = conn.execute(sql, (checklist_id, name))
        else:
            with self.tx() as c:
                cur = c.execute(sql, (checklist_id, name))
        return cur.rowcount > 0

    # -- assignments -------------------------------------------------------

    def checklist_assign(self, *, target_ref_id: int, checklist_id: int) -> bool:
        """Idempotent: ``True`` when a new assignment row was created,
        ``False`` when one was already live."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO checklist_assignments (target_ref_id, checklist_id) "
                "SELECT %s, %s WHERE NOT EXISTS ("
                "  SELECT 1 FROM checklist_assignments "
                "  WHERE target_ref_id = %s AND checklist_id = %s "
                "  AND retired_at IS NULL"
                ") RETURNING id",
                (target_ref_id, checklist_id, target_ref_id, checklist_id),
            ).fetchone()
        return row is not None

    def checklist_unassign(self, *, target_ref_id: int, checklist_id: int) -> bool:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE checklist_assignments SET retired_at = now() "
                "WHERE target_ref_id = %s AND checklist_id = %s "
                "AND retired_at IS NULL",
                (target_ref_id, checklist_id),
            )
        return cur.rowcount > 0

    def checklist_assignment_live(
        self, *, target_ref_id: int, checklist_id: int
    ) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM checklist_assignments "
                "WHERE target_ref_id = %s AND checklist_id = %s "
                "AND retired_at IS NULL",
                (target_ref_id, checklist_id),
            ).fetchone()
        return row is not None

    def checklist_assignments_for_target(
        self, target_ref_id: int
    ) -> list[dict[str, Any]]:
        """Every checklist live-assigned to a target."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT c.id, c.name, c.origin, c.created_at, c.retired_at "
                "FROM checklist_assignments ca "
                "JOIN checklists c ON c.id = ca.checklist_id "
                "WHERE ca.target_ref_id = %s AND ca.retired_at IS NULL "
                "AND c.retired_at IS NULL "
                "ORDER BY c.name",
                (target_ref_id,),
            ).fetchall()
        return [_row_to_checklist(r) for r in rows]

    # -- verdicts ------------------------------------------------------------

    def checklist_verdict_insert(
        self,
        *,
        target_ref_id: int,
        checklist_id: int,
        item_name: str,
        item_rev: int,
        verdict: str,
        evidence: dict[str, Any] | None = None,
        fingerprint: str | None = None,
        checked_by: str | None = None,
    ) -> int:
        """Append one verdict row. Never updates a prior verdict — the
        ledger is the primitive, "current status" is a read-time query
        over ``checked_at DESC``."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO checklist_verdicts "
                "(target_ref_id, checklist_id, item_name, item_rev, verdict, "
                " evidence, fingerprint, checked_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    target_ref_id,
                    checklist_id,
                    item_name,
                    item_rev,
                    verdict,
                    Jsonb(evidence or {}),
                    fingerprint,
                    checked_by,
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def checklist_verdict_latest(
        self, *, target_ref_id: int, checklist_id: int, item_name: str
    ) -> dict[str, Any] | None:
        """The most recently checked live verdict for one item, or
        ``None`` — "not checked"."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_VERDICT_COLS} FROM checklist_verdicts "
                "WHERE target_ref_id = %s AND checklist_id = %s "
                "AND item_name = %s AND retired_at IS NULL "
                "ORDER BY checked_at DESC, id DESC LIMIT 1",
                (target_ref_id, checklist_id, item_name),
            ).fetchone()
        return None if row is None else _row_to_verdict(row)

    def checklist_verdicts_latest_for_target(
        self, *, target_ref_id: int, checklist_id: int
    ) -> dict[str, dict[str, Any]]:
        """Every item's latest live verdict for one (target, checklist),
        keyed by ``item_name`` — one query for the whole status view
        instead of N."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (item_name) {_VERDICT_COLS} "
                "FROM checklist_verdicts "
                "WHERE target_ref_id = %s AND checklist_id = %s "
                "AND retired_at IS NULL "
                "ORDER BY item_name, checked_at DESC, id DESC",
                (target_ref_id, checklist_id),
            ).fetchall()
        out = {}
        for r in rows:
            v = _row_to_verdict(r)
            out[v["item_name"]] = v
        return out

    # -- notes (se_notes shape) ------------------------------------------

    def checklist_note_insert(
        self,
        *,
        target_ref_id: int,
        checklist_id: int,
        name: str,
        kind: str,
        body: str,
        item_name: str | None = None,
        re: str | None = None,
        about: list[str] | None = None,
        origin: str = "user",
    ) -> int:
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO checklist_notes "
                "(target_ref_id, checklist_id, item_name, name, kind, body, "
                " re, about, origin) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    target_ref_id,
                    checklist_id,
                    item_name,
                    name,
                    kind,
                    body,
                    re,
                    Jsonb(about or []),
                    origin,
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def checklist_note_remove(
        self, *, target_ref_id: int, checklist_id: int, name: str
    ) -> bool:
        """Retire (not delete) the live note(s) with this name — a genuine
        retraction, mirroring ``se_notes``'s ``remove_note``."""
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE checklist_notes SET retired_at = now() "
                "WHERE target_ref_id = %s AND checklist_id = %s AND name = %s "
                "AND retired_at IS NULL",
                (target_ref_id, checklist_id, name),
            )
        return cur.rowcount > 0

    def checklist_notes_list(
        self,
        *,
        target_ref_id: int,
        checklist_id: int,
        item_name: str | None = None,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["target_ref_id = %s", "checklist_id = %s"]
        params: list[Any] = [target_ref_id, checklist_id]
        if item_name is not None:
            clauses.append("item_name = %s")
            params.append(item_name)
        if not include_retired:
            clauses.append("retired_at IS NULL")
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_NOTE_COLS} FROM checklist_notes "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at",
                params,
            ).fetchall()
        return [_row_to_note(r) for r in rows]

    # -- generic ref resolution (any kind, not just checklist) ------------

    def checklist_resolve_ref_any_kind(
        self, ref_id: int
    ) -> tuple[int, str, str | None] | None:
        """``(ref_id, kind, title)`` for a bare numeric target, regardless
        of kind — ``get_ref`` requires the caller to already know the
        kind, which a checklist target generically doesn't."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id, kind, title FROM refs "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
        return None if row is None else (row[0], row[1], row[2])


__all__ = ["ChecklistMixin"]
