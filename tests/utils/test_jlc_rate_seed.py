"""The ``jlcpcb`` / ``jlcpcb-web`` limiter rows (fleet-wide JLC pacing).

Found by name, not number, so a landing-order renumber only renames the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.store import Store
from precis.utils import rate_limit

_MIGRATION = next(
    (Path(rate_limit.__file__).resolve().parents[1] / "migrations").glob(
        "[0-9][0-9][0-9][0-9]_jlcpcb_rate_rows.sql"
    )
)
_ROWS = {"jlcpcb": (1, 1.0, 1, None), "jlcpcb-web": (1, 1.0, 1, None)}


def _rows(store: Store) -> dict[str, tuple[Any, ...]]:
    with store.pool.connection() as conn:
        found = conn.execute(
            "SELECT provider, capacity, refill_per_sec, tokens, daily_cap "
            "FROM external_rate_limits WHERE provider LIKE 'jlcpcb%'"
        ).fetchall()
    return {r[0]: tuple(r[1:]) for r in found}


def test_migrated_db_has_both_rows_one_per_second(store: Store) -> None:
    assert _rows(store) == _ROWS


def test_total_rate_within_two_per_second(store: Store) -> None:
    assert sum(r[1] for r in _rows(store).values()) <= 2.0


def test_migration_resets_a_hand_made_row_and_is_idempotent(store: Store) -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE external_rate_limits SET capacity = 20, refill_per_sec = 20, "
            "tokens = 20, daily_cap = 5 WHERE provider = 'jlcpcb'"
        )
    for _ in range(2):
        with store.pool.connection() as conn:
            conn.execute(sql)
        assert _rows(store) == _ROWS
