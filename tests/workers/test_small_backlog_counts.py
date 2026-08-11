"""SQL-validity smoke tests for the SMALL-band backlog counters
(``docs/backlog/small-llm-derived-drain-band.md``).

These counters are hand-written SQL run only against real Postgres (the
materializer never sees a FakeStore), so the real risk is a bad column/predicate
that a FakeStore unit test would hide — exactly the
``psycopg_percent_like_fakestore_gap`` class of bug. So assert only that each
executes on real PG and returns a sane non-negative int; the *value* is an
approximate backlog for a mint threshold, not a contract.
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.workers.classify import unclassified_chunk_count
from precis.workers.llm_summarize import unsummarized_chunk_count

pytestmark = pytest.mark.db


def test_unsummarized_chunk_count_executes_on_real_pg(store: Store) -> None:
    with store.pool.connection() as conn:
        n = unsummarized_chunk_count(conn)
    assert isinstance(n, int) and n >= 0


def test_unclassified_chunk_count_executes_on_real_pg(store: Store) -> None:
    with store.pool.connection() as conn:
        n = unclassified_chunk_count(conn)
    assert isinstance(n, int) and n >= 0
