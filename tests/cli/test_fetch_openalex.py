"""Tests for ``precis fetch-openalex`` — the backfill claim semantics.

The paid OpenAlex-content sweep must only touch stubs that have **exhausted
the free legs** and haven't been OpenAlex-tried, so it never pays before the
free cascade ran and never double-pays on a re-run. The claim query
(:func:`_backfill_batch`) is where that lives; the download path itself is
covered by ``tests/workers/test_fetch_oa.py::TestTryOpenalexContent``.
"""

from __future__ import annotations

from precis.cli.fetch_openalex import _backfill_batch
from precis.store import Store


def _stub(store: Store, *, doi: str, events: list[tuple[str, str]]) -> int:
    """A paper stub (no PDF) with a DOI and the given (source, event) rows."""
    ref = store.insert_ref(kind="paper", slug=doi.replace("/", "_"), title="X", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, doi),
        )
        for source, event in events:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload) "
                "VALUES (%s, %s, %s, '{}')",
                (ref.id, source, event),
            )
    return ref.id


def test_backfill_claims_only_free_exhausted(store: Store) -> None:
    free_exhausted = _stub(store, doi="10.1/a", events=[("fetcher:s2", "fetch_failed")])
    got_free = _stub(store, doi="10.1/b", events=[("fetcher:s2", "fetch_ok")])
    never_tried = _stub(store, doi="10.1/c", events=[])
    already_oa = _stub(
        store, doi="10.1/d", events=[("fetcher:openalex_content", "no_oa_version")]
    )

    claimed = {s.ref_id for s in _backfill_batch(store, limit=5000)}

    assert free_exhausted in claimed  # free legs ran and failed → try OpenAlex
    assert got_free not in claimed  # already have the PDF
    assert never_tried not in claimed  # free legs haven't run — don't pay yet
    assert already_oa not in claimed  # resumable: never double-pay


def test_backfill_excludes_held_and_docless(store: Store) -> None:
    # A stub with no DOI can't be resolved via OpenAlex → excluded.
    ref = store.insert_ref(kind="paper", slug="noid", title="X", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "VALUES (%s, 'fetcher:s2', 'fetch_failed', '{}')",
            (ref.id,),
        )
    claimed = {s.ref_id for s in _backfill_batch(store, limit=5000)}
    assert ref.id not in claimed


def test_backfill_carries_identifiers(store: Store) -> None:
    rid = _stub(store, doi="10.1/withids", events=[("fetcher:s2", "no_oa_version")])
    stub = next(s for s in _backfill_batch(store, limit=5000) if s.ref_id == rid)
    assert stub.doi == "10.1/withids"
    assert stub.cite_key == "10.1_withids"
