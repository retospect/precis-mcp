"""DAO for the ``patent_watches`` table.

Pure SQL; no OPS calls, no ingest. The runner
(``precis.jobs.patent_watch``) and the CLI both go through this
module to read and update saved-watch rows.

Watch states:

    *fresh*       — ``last_run_at IS NULL``. Always picked on the
                    next pass thanks to ``NULLS FIRST`` in the
                    ``patent_watches_due_idx``.
    *due*         — ``last_run_at + interval_s seconds <= now()``.
                    Picked when the runner sweeps.
    *cooling*     — last ran within ``interval_s``; skipped.

The DAO is intentionally narrow: a small, named-method surface
(``create``, ``list_all``, ``list_due``, ``get_by_name``,
``record_pass``, ``delete``). The runner orchestrates; this module
just stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound
from precis.handlers._patent_cql import validate_strict_cql

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class PatentWatch:
    """One saved-watch row.

    ``last_seen_pn`` is the union of all DOCDB ids the runner has
    ever seen for this watch — never trimmed while the watch is
    active. The vacuum cron picks up the array bloat weekly.
    """

    id: int
    name: str
    cql: str
    interval_s: int
    last_run_at: datetime | None
    last_seen_pn: list[str]
    max_per_pass: int | None
    created_at: datetime
    created_by: str | None


# ---------------------------------------------------------------------------
# Slug normalisation
# ---------------------------------------------------------------------------


def _normalise_name(name: str) -> str:
    """Trim + lowercase a watch name for storage and lookup.

    Watch names are user-supplied (``--name`` flag) but referenced by
    the runner and by other CLI subcommands. Storing them lowercased
    means ``run-patent-watches --name MyWatch`` matches the row that
    was created with ``--name mywatch``.

    Empty / whitespace-only names are rejected with ``BadInput`` —
    we *could* derive a synthetic name from the CQL, but that hides
    accidental duplicate watches behind opaque slugs.
    """
    if not isinstance(name, str):
        raise BadInput(
            f"watch name must be a string, got {type(name).__name__!r}",
            next="watch-patents '<cql>' --name my-watch",
        )
    trimmed = name.strip().lower()
    if not trimmed:
        raise BadInput(
            "watch name is empty",
            next="watch-patents '<cql>' --name my-watch",
        )
    return trimmed


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create(
    store: Store,
    *,
    name: str,
    cql: str,
    interval_s: int = 604_800,
    max_per_pass: int | None = None,
    created_by: str | None = "agent",
) -> PatentWatch:
    """Insert a new watch.

    ``cql`` is validated strictly — see ``validate_strict_cql``. The
    returned ``PatentWatch`` carries the trimmed CQL, the canonical
    lowercased name, and a ``last_run_at`` of ``None`` (so it picks
    up on the next runner pass).

    Raises:
        BadInput: name empty, CQL bare-keyword, or interval/budget
            non-positive.
        BadInput: a watch with this name already exists.
    """
    norm_name = _normalise_name(name)
    norm_cql = validate_strict_cql(cql)

    if interval_s <= 0:
        raise BadInput(
            f"interval_s must be > 0, got {interval_s!r}",
            next="watch-patents '<cql>' --every 7d  (default is weekly)",
        )
    if max_per_pass is not None and max_per_pass <= 0:
        raise BadInput(
            f"max_per_pass must be > 0 when set, got {max_per_pass!r}",
            next="omit --max-per-pass for unlimited; or pass a positive int",
        )

    sql = """
        INSERT INTO patent_watches
            (name, cql, interval_s, max_per_pass, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, name, cql, interval_s, last_run_at, last_seen_pn,
                  max_per_pass, created_at, created_by
    """
    with store.pool.connection() as conn:
        try:
            row = conn.execute(
                sql,
                (
                    norm_name,
                    norm_cql,
                    interval_s,
                    max_per_pass,
                    created_by,
                ),
            ).fetchone()
        except Exception as e:
            # Unique-violation on ``name`` — translate into our typed
            # error so the agent sees a recovery hint, not an SQLSTATE.
            if "patent_watches_name_key" in str(e) or "duplicate key" in str(e):
                raise BadInput(
                    f"a watch named {norm_name!r} already exists",
                    next=(
                        "list-patent-watches  # to see existing names; "
                        "or use a different --name"
                    ),
                ) from e
            raise

    assert row is not None
    return _row_to_watch(row)


def get_by_name(store: Store, name: str) -> PatentWatch | None:
    """Look up one watch by its lowercased name. Returns None on miss."""
    norm = _normalise_name(name)
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, cql, interval_s, last_run_at, last_seen_pn,
                   max_per_pass, created_at, created_by
            FROM patent_watches WHERE name = %s
            """,
            (norm,),
        ).fetchone()
    return _row_to_watch(row) if row is not None else None


def list_all(store: Store) -> list[PatentWatch]:
    """Every watch, ordered by name. Used by ``list-patent-watches``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, cql, interval_s, last_run_at, last_seen_pn,
                   max_per_pass, created_at, created_by
            FROM patent_watches ORDER BY name ASC
            """
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def list_due(store: Store) -> list[PatentWatch]:
    """Every watch that's due to run.

    Due = ``last_run_at IS NULL`` (never run) OR
    ``last_run_at + interval_s seconds <= now()``. Ordered by
    ``last_run_at NULLS FIRST`` — the same order as the index, so
    fresh watches always run before re-runs in any given pass.

    Used by the launchd-driven runner (``run-patent-watches``).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, cql, interval_s, last_run_at, last_seen_pn,
                   max_per_pass, created_at, created_by
            FROM patent_watches
            WHERE last_run_at IS NULL
               OR last_run_at + (interval_s * INTERVAL '1 second') <= now()
            ORDER BY last_run_at NULLS FIRST
            """
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def record_pass(
    store: Store,
    *,
    watch_id: int,
    new_pn: list[str],
) -> None:
    """Mark a successful pass: union ``new_pn`` into ``last_seen_pn``,
    bump ``last_run_at`` to ``now()``.

    No-op on ``new_pn=[]`` is **not** what we want — even an
    all-empty pass should bump ``last_run_at`` so the watch waits
    its full interval before re-running. Without that bump, the
    runner would re-fetch on every hourly tick.

    Atomic: a single UPDATE so concurrent runners don't race the
    array union (highly unlikely with a launchd timer, but trivial
    to get right).
    """
    with store.pool.connection() as conn:
        cur = conn.execute(
            """
            UPDATE patent_watches
            SET last_run_at = now(),
                last_seen_pn = COALESCE(
                    (
                        SELECT array_agg(DISTINCT v)
                        FROM unnest(last_seen_pn || %s::text[]) AS v
                    ),
                    '{}'::text[]
                )
            WHERE id = %s
            """,
            (new_pn, watch_id),
        )
    if cur.rowcount == 0:
        raise NotFound(f"patent_watch id={watch_id} no longer exists")


def delete(store: Store, name: str) -> None:
    """Hard delete (no soft-delete column on this table).

    Raises ``NotFound`` if no such watch exists — the CLI surfaces
    this as a clean error rather than silently no-oping.
    """
    norm = _normalise_name(name)
    with store.pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM patent_watches WHERE name = %s",
            (norm,),
        )
    if cur.rowcount == 0:
        raise NotFound(
            f"no patent watch named {norm!r}",
            next="list-patent-watches  # to see existing names",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_watch(row: tuple) -> PatentWatch:
    """psycopg row tuple → ``PatentWatch``. Column order matches the
    SELECT lists above."""
    return PatentWatch(
        id=int(row[0]),
        name=str(row[1]),
        cql=str(row[2]),
        interval_s=int(row[3]),
        last_run_at=row[4],
        last_seen_pn=list(row[5] or []),
        max_per_pass=int(row[6]) if row[6] is not None else None,
        created_at=row[7],
        created_by=str(row[8]) if row[8] is not None else None,
    )


__all__ = [
    "PatentWatch",
    "create",
    "delete",
    "get_by_name",
    "list_all",
    "list_due",
    "record_pass",
]
