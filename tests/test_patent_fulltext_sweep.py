"""Tests for the deferred full-text retry sweep.

Uses ``FakeOpsClient`` (no network) and the standard ``store``
fixture from ``conftest.py``. Covers:

- success path (both endpoints return → blocks ingested, tag cleared);
- still-404 path (retry_count bumped, retry_at rescheduled);
- partial-success path (one endpoint returns, the other 404s →
  still-pending, retry rescheduled, tag retained);
- give-up path (pub_date > 6 months ago → swap to fulltext-unavailable);
- fair-use pause (rolling window over limit → no OPS calls);
- dry-run (enumerates due refs, mutates nothing).

All non-give-up tests inject ``now`` close to the fixture's
publication date (2020-01-15) so the six-month give-up window
doesn't fire prematurely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.handlers._patent_ingest import (
    AWAITING_FULLTEXT_TAG,
    FULLTEXT_UNAVAILABLE_TAG,
    ingest_patent,
)
from precis.handlers._patent_ops import FakeOpsClient
from precis.jobs.patent_fulltext_sweep import (
    run_fulltext_sweep,
)
from precis.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "patent"


# ---------------------------------------------------------------------------
# Fixtures — mirror test_patent_ingest.py
# ---------------------------------------------------------------------------


@pytest.fixture
def biblio_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_biblio.xml").read_bytes()


@pytest.fixture
def description_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_description.xml").read_bytes()


@pytest.fixture
def claims_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_claims.xml").read_bytes()


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "patents"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def embedder(store: Store) -> MockEmbedder:
    return MockEmbedder(dim=store.embedding_dim())


def _ingest_biblio_only(
    store: Store,
    raw_root: Path,
    biblio_xml: bytes,
    embedder: MockEmbedder,
) -> int:
    """Ingest a patent with only biblio available (description +
    claims 404). Returns the new ref_id. Tags and meta will
    reflect the awaiting-fulltext state."""
    ops = FakeOpsClient(biblio={"ep1234567b1": biblio_xml})
    result = ingest_patent(
        "ep1234567b1",
        store=store,
        ops=ops,
        embedder=embedder,
        raw_root=raw_root,
    )
    return result.ref_id


def _force_retry_at(store: Store, ref_id: int, when: datetime) -> None:
    """Overwrite ``meta->fulltext_retry_at`` to make the ref due."""
    store.update_ref(ref_id=ref_id, meta_patch={"fulltext_retry_at": when.isoformat()})


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


#: Clock injected into every non-give-up test. The fixture patent is
#: published 2020-01-15; we run the sweep a couple of months later
#: so the six-month give-up window doesn't fire.
_FRESH_NOW = datetime(2020, 3, 1, tzinfo=UTC)


class TestSweepSuccess:
    def test_both_endpoints_return_ingests_blocks(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        description_xml: bytes,
        claims_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        # Force the retry to be due now.
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))

        # Second pass at OPS — this time description + claims succeed.
        ops = FakeOpsClient(
            description={"ep1234567b1": description_xml},
            claims={"ep1234567b1": claims_xml},
        )
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            now=now,
        )

        assert len(summary.outcomes) == 1
        o = summary.outcomes[0]
        assert o.slug == "ep1234567b1"
        assert o.succeeded is True
        assert o.blocks_added > 0

        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        # Both flags flip to True.
        assert ref.meta.get("has_description") is True
        assert ref.meta.get("has_claims") is True
        # Retry bookkeeping cleared.
        assert ref.meta.get("fulltext_retry_at") is None
        assert ref.meta.get("fulltext_retry_count") is None
        # Awaiting tag removed.
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert AWAITING_FULLTEXT_TAG not in tag_values
        # Blocks actually landed.
        assert store.count_blocks(ref.id) == o.blocks_added


# ---------------------------------------------------------------------------
# Still-pending path
# ---------------------------------------------------------------------------


class TestSweepStillPending:
    def test_still_404_bumps_retry_count_and_reschedules(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))

        # Empty OPS — both endpoints still 404.
        ops = FakeOpsClient()
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            now=now,
        )
        assert len(summary.outcomes) == 1
        o = summary.outcomes[0]
        assert o.succeeded is False
        assert o.still_pending is True

        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        # Retry count was 0 after ingest; now 1.
        assert ref.meta.get("fulltext_retry_count") == 1
        # New retry_at is in the future and ≥ 14 days out (2nd attempt).
        retry_at = datetime.fromisoformat(ref.meta["fulltext_retry_at"])
        delta = retry_at - now
        assert delta >= timedelta(days=14) - timedelta(seconds=1)
        # Awaiting tag still present.
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert AWAITING_FULLTEXT_TAG in tag_values


# ---------------------------------------------------------------------------
# Partial success (one endpoint returns, one still 404s)
# ---------------------------------------------------------------------------


class TestSweepPartialSuccess:
    def test_description_returns_claims_still_404(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        description_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))

        # OPS returns description but still 404s claims.
        ops = FakeOpsClient(description={"ep1234567b1": description_xml})
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            now=now,
        )
        o = summary.outcomes[0]
        assert o.blocks_added > 0
        assert o.succeeded is False  # claims still missing
        assert o.still_pending is True

        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        assert ref.meta.get("has_description") is True
        assert ref.meta.get("has_claims") is False
        # Still tagged for retry.
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert AWAITING_FULLTEXT_TAG in tag_values


# ---------------------------------------------------------------------------
# Give-up path
# ---------------------------------------------------------------------------


class TestSweepGiveUp:
    def test_old_publication_gets_unavailable_tag(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        # The fixture publication_date is 2020-01-15 — far past 6 months
        # ago in any realistic run. Force retry due now.
        now = datetime.now(UTC)
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))

        ops = FakeOpsClient()  # doesn't matter; give-up precedes OPS
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            now=now,
        )
        o = summary.outcomes[0]
        assert o.given_up is True

        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert AWAITING_FULLTEXT_TAG not in tag_values
        assert FULLTEXT_UNAVAILABLE_TAG in tag_values
        # Retry bookkeeping cleared so listings don't show a ghost date.
        assert ref.meta.get("fulltext_retry_at") is None


# ---------------------------------------------------------------------------
# Fair-use pause
# ---------------------------------------------------------------------------


class TestSweepFairUse:
    def test_paused_when_rolling_bytes_over_limit(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))
        # Force the rolling byte count over a tiny 1e-9 GiB limit.
        ops = FakeOpsClient()  # shouldn't get called at all
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            fair_use_limit_gb=1e-12,  # effectively zero
            now=now,
        )
        assert summary.paused_global is True
        assert summary.outcomes == []


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestSweepDryRun:
    def test_dry_run_enumerates_but_does_not_mutate(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        description_xml: bytes,
        claims_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now - timedelta(seconds=1))

        ref_before = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref_before is not None
        retry_count_before = ref_before.meta.get("fulltext_retry_count")

        # Even with OPS holding the data, dry_run must mutate nothing.
        ops = FakeOpsClient(
            description={"ep1234567b1": description_xml},
            claims={"ep1234567b1": claims_xml},
        )
        summary = run_fulltext_sweep(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            dry_run=True,
            now=now,
        )
        o = summary.outcomes[0]
        assert o.skipped_dry_run is True

        ref_after = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref_after is not None
        # State is untouched.
        assert ref_after.meta.get("has_description") is False
        assert ref_after.meta.get("has_claims") is False
        assert ref_after.meta.get("fulltext_retry_count") == retry_count_before
        assert store.count_blocks(ref_after.id) == 0


# ---------------------------------------------------------------------------
# Non-due refs are ignored
# ---------------------------------------------------------------------------


class TestSweepSelectsOnlyDue:
    def test_future_retry_is_skipped(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        # Ingest in awaiting-fulltext state but leave retry_at in
        # the future. The sweep must not touch it.
        ref_id = _ingest_biblio_only(store, raw_root, biblio_xml, embedder)
        now = _FRESH_NOW
        _force_retry_at(store, ref_id, now + timedelta(days=5))

        summary = run_fulltext_sweep(
            store=store,
            ops=FakeOpsClient(),
            embedder=embedder,
            raw_root=raw_root,
            now=now,
        )
        assert summary.outcomes == []

    def test_non_awaiting_ref_is_not_selected(
        self,
        store: Store,
        raw_root: Path,
        biblio_xml: bytes,
        description_xml: bytes,
        claims_xml: bytes,
        embedder: MockEmbedder,
    ) -> None:
        # Fully ingested patent — no awaiting-fulltext tag — must
        # never appear in the sweep queue, no matter its meta.
        ops = FakeOpsClient(
            biblio={"ep1234567b1": biblio_xml},
            description={"ep1234567b1": description_xml},
            claims={"ep1234567b1": claims_xml},
        )
        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        summary = run_fulltext_sweep(
            store=store,
            ops=FakeOpsClient(),
            embedder=embedder,
            raw_root=raw_root,
            now=_FRESH_NOW,
        )
        assert summary.outcomes == []
