"""Persistence for saved EDGAR FTS queries and their accession checkpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

from precis.errors import BadInput, NotFound

if TYPE_CHECKING:
    from precis.store import Store

_COLUMNS = "id, name, params, interval_s, last_run_at, last_seen_accessions"


@dataclass(frozen=True, slots=True)
class EdgarWatch:
    id: int
    name: str
    params: dict[str, str]
    interval_s: int
    last_run_at: datetime | None
    last_seen_accessions: list[str]


def _name(name: str) -> str:
    slug = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        raise BadInput(
            "invalid EDGAR watch name", next="use a slug such as issuer-risk"
        )
    return slug


def create(
    store: Store, *, name: str, params: dict[str, str], interval_s: int
) -> EdgarWatch:
    name = _name(name)
    if not params or not set(params) <= {"q", "forms", "ciks"}:
        raise BadInput(
            "EDGAR watch requires FTS keyword parameters",
            next="supply a keyword, form or CIK to precis jobs watch-edgar",
        )
    if not all(value.strip() for value in params.values()):
        raise BadInput("empty EDGAR watch parameter", next="use a non-empty value")
    if not 0 < interval_s <= 2_147_483_647:
        raise BadInput(
            "invalid watch interval", next="pass --every with a valid interval"
        )
    with store.tx() as conn:
        row = conn.execute(
            f"""
            INSERT INTO edgar_watches (name, params, interval_s)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            (name, Jsonb(params), interval_s),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"EDGAR watch {name!r} already exists",
            next="use a new name or remove it with watch-edgar --delete",
        )
    return EdgarWatch(*row)


def delete(store: Store, name: str) -> None:
    with store.tx() as conn:
        count = conn.execute(
            "DELETE FROM edgar_watches WHERE name = %s", (_name(name),)
        ).rowcount
    if not count:
        raise NotFound(
            f"no EDGAR watch named {name!r}", next="precis jobs list-edgar-watches"
        )


def get_by_name(store: Store, name: str) -> EdgarWatch:
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM edgar_watches WHERE name = %s", (_name(name),)
        ).fetchone()
    if row is None:
        raise NotFound(
            f"no EDGAR watch named {name!r}", next="precis jobs list-edgar-watches"
        )
    return EdgarWatch(*row)


def list_all(store: Store) -> list[EdgarWatch]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM edgar_watches ORDER BY name"
        ).fetchall()
    return [EdgarWatch(*row) for row in rows]


def list_due(store: Store) -> list[EdgarWatch]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS} FROM edgar_watches
            WHERE last_run_at IS NULL
               OR last_run_at + interval_s * interval '1 second' <= now()
            ORDER BY last_run_at NULLS FIRST, id
            """
        ).fetchall()
    return [EdgarWatch(*row) for row in rows]


def record_pass(
    store: Store, watch_id: int, completed: list[str], *, finished: bool
) -> None:
    """Save successes even on a partial pass, leaving incomplete watches due."""
    with store.tx() as conn:
        conn.execute(
            """
            UPDATE edgar_watches
            SET last_run_at = CASE WHEN %s THEN now() ELSE last_run_at END,
                last_seen_accessions = ARRAY(
                    SELECT DISTINCT unnest(last_seen_accessions || %s::text[])
                )
            WHERE id = %s
            """,
            (finished, completed, watch_id),
        )
