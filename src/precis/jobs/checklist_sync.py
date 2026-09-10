"""Deploy-time sync of shipped ``checklist`` definitions into the DB.

Modeled on :mod:`precis.jobs.oracle_sync` (sha256 content-state, advisory
lock, idempotent) — that is the working precedent for an idempotent,
content-hash-compared, advisory-locked, read-only-DSN-aware boot-time DB
sync of file content (docs/backlog/checklist-kind.md). Wired at boot in
``dispatch.py`` next to the oracle sync, behind the same
``mcp_read_only`` guard (:func:`precis.dispatch._boot_is_read_only`).

Shipped format: one YAML file per checklist under
``src/precis/data/checklists/*.yaml``::

    name: my-checklist
    items:
      - name: item-one
        phase: setup            # optional
        severity: blocking      # blocking | advisory (default advisory)
        decidability: judgment  # tool | judgment (default judgment)
        prevents: "what breaks if this is skipped"   # mandatory
        applies: "boards with connectors"             # optional
        body: "instructions the agent follows"        # optional

Sync invariants (design doc): (1) content-hash compared, no-op on
unchanged — the whole reconcile runs inside one transaction holding a
Postgres **transaction-scoped** advisory lock (``pg_try_advisory_xact_lock``,
not session-scoped — see :mod:`precis.jobs.oracle_sync`'s docstring for
why a session lock is unsafe through pgbouncer transaction pooling), so
concurrent boots can't double-insert and a crash mid-sync can't leave a
committed "up to date" hash paired with uncommitted item rows; (2)
append-only — inserts item revs or sets ``retired_at``, never rewrites;
(3) ``origin='local'`` rows (and their verdicts/notes) are never touched.
Deliberately simpler than oracle_sync: no wheel-version gate (checklist
content isn't code-version-gated), just a per-checklist sha256 compared
against the last-synced hash recorded in ``app_state``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from psycopg import Connection

log = logging.getLogger(__name__)

#: ``app_state`` key prefix for the last-synced content hash of one
#: shipped checklist, namespaced by name so multiple checklists coexist.
_KEY_PREFIX = "corpus.checklist."


def _state_key(checklist_name: str) -> str:
    return f"{_KEY_PREFIX}{checklist_name}.sha256"


def bundled_checklists_dir() -> Path | None:
    """The bundled ``data/checklists/`` directory, or ``None`` if the
    package shipped without it (e.g. a sdist that excluded data)."""
    try:
        files = resources.files("precis.data.checklists")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        path = Path(str(files))
    except TypeError:
        return None
    if not path.is_dir():
        return None
    return path


def _advisory_lock_id() -> int:
    digest = hashlib.sha256(b"precis.corpus.checklist").digest()
    return int.from_bytes(digest[:8], "big", signed=True) >> 1


@dataclass(frozen=True)
class ChecklistFile:
    """One parsed+hashed shipped checklist file."""

    name: str
    items: list[dict[str, Any]]
    sha256: str
    path: Path


def load_checklist_file(path: Path) -> ChecklistFile:
    """Parse one YAML file into a :class:`ChecklistFile`.

    Raises ``ValueError`` on malformed content (missing ``name``, an
    ``items`` that isn't a list, an item with no ``name``) — the caller
    treats that as a sync error for this file, not a boot failure.
    """
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError(f"{path}: missing top-level 'name'")
    items = data.get("items") or []
    if not isinstance(items, list):
        raise ValueError(f"{path}: 'items' must be a list")
    for raw_item in items:
        if (
            not isinstance(raw_item, dict)
            or not str(raw_item.get("name") or "").strip()
        ):
            raise ValueError(f"{path}: every item needs a non-empty 'name'")
    return ChecklistFile(
        name=name,
        items=items,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        path=path,
    )


def _item_content_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """The fields that decide "did this item change" — name/rev/origin
    excluded (those are identity/bookkeeping, not content)."""
    return (
        item.get("phase"),
        item.get("severity") or "advisory",
        item.get("decidability") or "judgment",
        item.get("prevents"),
        item.get("applies"),
        item.get("body"),
    )


def _row_content_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["phase"],
        row["severity"],
        row["decidability"],
        row["prevents"],
        row["applies"],
        row["body"],
    )


# ---------------------------------------------------------------------------
# app_state I/O — always on the caller's connection so the hash commits
# atomically with the data (see module docstring, invariant 1).
# ---------------------------------------------------------------------------


def _read_state_conn(conn: Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return None if row is None else str(row[0])


def _write_state_conn(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )


# ---------------------------------------------------------------------------
# per-file sync
# ---------------------------------------------------------------------------


def _sync_one(
    store: Any, cf: ChecklistFile, *, conn: Connection | None
) -> dict[str, Any]:
    """Diff one file's items against the DB. Inserts revs / retires
    missing shipped items; never touches ``origin='local'`` rows."""
    checklist = store.checklist_get(cf.name)
    if checklist is None:
        checklist = store.checklist_create(name=cf.name, origin="shipped", conn=conn)

    created = 0
    revved = 0
    retired = 0
    seen_names: set[str] = set()

    for raw_item in cf.items:
        item_name = str(raw_item["name"]).strip()
        seen_names.add(item_name)
        current = store.checklist_item_current(checklist["id"], item_name)
        if current is not None and _row_content_key(current) == _item_content_key(
            raw_item
        ):
            continue  # unchanged — no-op even if the file re-ships identical content
        rev = store.checklist_item_max_rev(checklist["id"], item_name, conn=conn) + 1
        store.checklist_item_add_rev(
            checklist_id=checklist["id"],
            name=item_name,
            rev=rev,
            phase=raw_item.get("phase"),
            severity=raw_item.get("severity") or "advisory",
            decidability=raw_item.get("decidability") or "judgment",
            prevents=raw_item.get("prevents"),
            applies=raw_item.get("applies"),
            body=raw_item.get("body"),
            origin="shipped",
            conn=conn,
        )
        if current is None:
            created += 1
        else:
            revved += 1

    # Retire shipped items no longer present in the file. Queried directly
    # on ``conn`` (not via a second pooled connection) so this scan and the
    # inserts above share one snapshot/transaction throughout.
    if conn is not None:
        rows = conn.execute(
            "SELECT DISTINCT ON (name) name, origin FROM checklist_items "
            "WHERE checklist_id = %s AND retired_at IS NULL "
            "ORDER BY name, rev DESC",
            (checklist["id"],),
        ).fetchall()
    else:
        rows = [
            (it["name"], it["origin"])
            for it in store.checklist_items_current(checklist["id"])
        ]
    for name, origin in rows:
        if name in seen_names or origin != "shipped":
            continue  # local items (and items still in the file) untouched
        store.checklist_item_retire(checklist_id=checklist["id"], name=name, conn=conn)
        retired += 1

    return {
        "checklist": cf.name,
        "created": created,
        "revved": revved,
        "retired": retired,
    }


# ---------------------------------------------------------------------------
# boot-time entry point
# ---------------------------------------------------------------------------


def sync_all(
    store: Any,
    *,
    src_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Reconcile every bundled checklist YAML against the DB.

    Returns ``{"status": ..., "files": [...]}`` (top-level ``status`` one
    of ``no_store``, ``no_data``, ``locked``, ``error``, ``ok``). Never
    raises — the boot caller wraps this in a broad ``except`` too, but
    every internal error is caught here and surfaced as
    ``status='error'`` so a malformed fixture never blocks startup.

    ``force=True`` bypasses the per-checklist hash gate (still inside
    the same locked transaction) — the "I just hand-edited the YAML"
    escape hatch, mirroring ``oracle_sync``'s ``--force``.
    """
    if store is None:
        return {"status": "no_store"}

    if src_dir is None:
        src_dir = bundled_checklists_dir()
    if src_dir is None:
        return {"status": "no_data", "reason": "bundled checklist dir not found"}
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return {"status": "no_data", "reason": f"{src_dir} is not a directory"}

    yaml_files = sorted(src_dir.glob("*.yaml")) + sorted(src_dir.glob("*.yml"))
    if not yaml_files:
        return {"status": "no_data", "reason": "no YAML files in dir"}

    pool = getattr(store, "pool", None)
    if pool is None:
        # Non-Postgres store (test stub / no pool): can't hold a lock or
        # span a tx. Sync directly — the per-checklist hash gate is the
        # only serialisation, same degrade-gracefully shape as oracle_sync.
        return _do_sync(store, yaml_files, force=force, conn=None)

    lock_id = _advisory_lock_id()
    try:
        with store.tx() as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (lock_id,)
            ).fetchone()
            if not (got and got[0]):
                # A peer holds the sync lock; it writes the same content.
                return {"status": "locked"}
            return _do_sync(store, yaml_files, force=force, conn=conn)
    except Exception as exc:
        log.warning("checklist_sync: sync failed: %s", exc)
        return {"status": "error", "reason": str(exc)}


def _do_sync(
    store: Any,
    yaml_files: list[Path],
    *,
    force: bool,
    conn: Connection | None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for path in yaml_files:
        try:
            cf = load_checklist_file(path)
        except ValueError as exc:
            log.warning("checklist_sync: %s", exc)
            results.append(
                {"checklist": path.stem, "status": "error", "reason": str(exc)}
            )
            continue
        key = _state_key(cf.name)
        stored_sha = (
            _read_state_conn(conn, key) if conn is not None else store.get_setting(key)
        )
        if not force and stored_sha == cf.sha256:
            results.append({"checklist": cf.name, "status": "up_to_date"})
            continue
        outcome = _sync_one(store, cf, conn=conn)
        outcome["status"] = "synced"
        if conn is not None:
            _write_state_conn(conn, key, cf.sha256)
        else:
            store.set_setting(key, cf.sha256)
        results.append(outcome)
    return {"status": "ok", "files": results}


# ---------------------------------------------------------------------------
# drift check — DB current shipped revs vs. the shipped files
# ---------------------------------------------------------------------------


def check_drift(store: Any, *, src_dir: Path | None = None) -> list[dict[str, Any]]:
    """Compare each shipped checklist's DB-current ``origin='shipped'``
    item revs against what the file would produce. Empty list = no
    drift. A non-empty finding means either the file changed since the
    last sync ran (transient — the next boot's sync clears it) or a row
    was hand-edited directly in the DB (the case this exists to catch;
    design doc: "a doctor check compares current DB revs against the
    deployed files"). Doctor wiring is a follow-up; this is the function
    + test only for slice 1.
    """
    if src_dir is None:
        src_dir = bundled_checklists_dir()
    if src_dir is None:
        return []
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return []

    findings: list[dict[str, Any]] = []
    for path in sorted(src_dir.glob("*.yaml")) + sorted(src_dir.glob("*.yml")):
        try:
            cf = load_checklist_file(path)
        except ValueError as exc:
            findings.append({"checklist": path.stem, "item": None, "reason": str(exc)})
            continue
        checklist = store.checklist_get(cf.name)
        if checklist is None:
            findings.append(
                {
                    "checklist": cf.name,
                    "item": None,
                    "reason": "checklist missing in DB",
                }
            )
            continue
        file_items = {str(it["name"]).strip(): it for it in cf.items}
        db_items = {
            it["name"]: it
            for it in store.checklist_items_current(checklist["id"])
            if it["origin"] == "shipped"
        }
        for name, raw in file_items.items():
            row = db_items.get(name)
            if row is None:
                findings.append(
                    {
                        "checklist": cf.name,
                        "item": name,
                        "reason": "in file, no current shipped rev in DB (sync pending)",
                    }
                )
            elif _row_content_key(row) != _item_content_key(raw):
                findings.append(
                    {
                        "checklist": cf.name,
                        "item": name,
                        "reason": "DB current rev content differs from the shipped file",
                    }
                )
        for name in db_items:
            if name not in file_items:
                findings.append(
                    {
                        "checklist": cf.name,
                        "item": name,
                        "reason": "shipped in DB but absent from the file (retire pending)",
                    }
                )
    return findings


def is_disabled_by_env() -> bool:
    """True when ``PRECIS_CHECKLIST_AUTO_SYNC`` is set to ``0``/``false``.

    Default is on, mirroring ``jobs/oracle_sync.is_disabled_by_env``.
    """
    val = os.environ.get("PRECIS_CHECKLIST_AUTO_SYNC", "1").strip().lower()
    return val in ("0", "false", "no", "off", "")
