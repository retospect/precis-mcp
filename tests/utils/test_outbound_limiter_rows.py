"""The gr493222 outbound limiter rows.

Found by name, not number, so a landing-order renumber only renames the file.
"""

from __future__ import annotations

from pathlib import Path

from precis.store import Store
from precis.utils import rate_limit
from precis.utils.http import POLICIES

_MIGRATION = next(
    (Path(rate_limit.__file__).resolve().parents[1] / "migrations").glob(
        "[0-9][0-9][0-9][0-9]_outbound_limiter_rows.sql"
    )
)


def _rate(store: Store, provider: str) -> float | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT refill_per_sec FROM external_rate_limits WHERE provider = %s",
            (provider,),
        ).fetchone()
    return None if row is None else float(row[0])


def test_every_policy_row_exists(store: Store) -> None:
    for service, policy in POLICIES.items():
        if policy.provider is not None:
            assert _rate(store, policy.provider) is not None, service


def test_crossref_at_most_ten_and_list_never_faster(store: Store) -> None:
    crossref = _rate(store, "crossref")
    listing = _rate(store, "crossref-list")
    assert crossref is not None and crossref <= 10.0
    assert listing is not None and listing <= min(3.0, crossref)


def test_migration_never_raises_a_stricter_row_and_is_idempotent(
    store: Store,
) -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE external_rate_limits SET capacity = 1, refill_per_sec = 1, "
            "tokens = 1 WHERE provider IN ('crossref', 'orcid')"
        )
    for _ in range(2):
        with store.pool.connection() as conn:
            conn.execute(sql)
        assert _rate(store, "crossref") == 1.0
        assert _rate(store, "orcid") == 1.0
