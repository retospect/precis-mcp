"""Tests for the force-reingest backfill pass
(``precis.jobs.patent_reingest``).

Drives ``ingest_patent(force=True)`` over existing patents so their
claim blocks pick up the slice-1 ``patent_block`` markers
(docs/design/patent-authoring-loop.md). Uses ``FakeOpsClient`` (no
network) + the ephemeral ``store`` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.handlers._patent_ingest import ingest_patent
from precis.handlers._patent_ops import FakeOpsClient
from precis.jobs.patent_reingest import run_reingest_pass
from precis.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "patent"


@pytest.fixture
def fake_ops() -> FakeOpsClient:
    return FakeOpsClient(
        biblio={"ep1234567b1": (FIXTURES / "ep1234567b1_biblio.xml").read_bytes()},
        description={
            "ep1234567b1": (FIXTURES / "ep1234567b1_description.xml").read_bytes()
        },
        claims={"ep1234567b1": (FIXTURES / "ep1234567b1_claims.xml").read_bytes()},
    )


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "patents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ingest_then_unmark(store: Store, fake_ops: FakeOpsClient, raw_root: Path) -> int:
    """Ingest ep1234567b1 and strip its markers (a pre-slice-1 ref)."""
    result = ingest_patent(
        "ep1234567b1",
        store=store,
        ops=fake_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET meta = '{}'::jsonb WHERE ref_id = %s",
            (result.ref_id,),
        )
    return result.ref_id


def test_pass_remarks_existing_patent(
    store: Store, fake_ops: FakeOpsClient, raw_root: Path
) -> None:
    ref_id = _ingest_then_unmark(store, fake_ops, raw_root)
    summary = run_reingest_pass(
        store=store,
        ops=fake_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
    )
    assert len(summary.outcomes) == 1
    o = summary.outcomes[0]
    assert o.slug == "ep1234567b1"
    assert o.error is None
    assert o.blocks_after == 7
    # Markers are back on the chunks.
    kinds = [
        (b.meta or {}).get("patent_block") for b in store.list_blocks_for_ref(ref_id)
    ]
    assert kinds == ["description"] * 4 + ["claim"] * 3


def test_dry_run_mutates_nothing(
    store: Store, fake_ops: FakeOpsClient, raw_root: Path
) -> None:
    ref_id = _ingest_then_unmark(store, fake_ops, raw_root)
    calls_before = len(fake_ops.calls)
    summary = run_reingest_pass(
        store=store,
        ops=fake_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
        dry_run=True,
    )
    assert summary.outcomes[0].skipped_dry_run is True
    # No OPS calls, chunks still unmarked.
    assert len(fake_ops.calls) == calls_before
    kinds = [
        (b.meta or {}).get("patent_block") for b in store.list_blocks_for_ref(ref_id)
    ]
    assert all(k is None for k in kinds)


def test_only_slugs_restricts_targets(
    store: Store, fake_ops: FakeOpsClient, raw_root: Path
) -> None:
    _ingest_then_unmark(store, fake_ops, raw_root)
    summary = run_reingest_pass(
        store=store,
        ops=fake_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
        only_slugs=["ep1234567b1"],
    )
    assert [o.slug for o in summary.outcomes] == ["ep1234567b1"]


def test_per_patent_error_is_isolated(
    store: Store, fake_ops: FakeOpsClient, raw_root: Path
) -> None:
    # Ingest one patent, then run with an OPS client that has no canned
    # responses → the re-fetch 404s and the outcome records an error
    # rather than raising.
    _ingest_then_unmark(store, fake_ops, raw_root)
    empty_ops = FakeOpsClient()
    summary = run_reingest_pass(
        store=store,
        ops=empty_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
    )
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].error is not None


def test_limit_caps_attempts(
    store: Store, fake_ops: FakeOpsClient, raw_root: Path
) -> None:
    _ingest_then_unmark(store, fake_ops, raw_root)
    summary = run_reingest_pass(
        store=store,
        ops=fake_ops,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        raw_root=raw_root,
        limit=0,
    )
    assert summary.outcomes == []
