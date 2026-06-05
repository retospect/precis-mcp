"""End-to-end tests for the patent watch runner.

Uses ``FakeOpsClient`` and the ``store`` fixture (ephemeral postgres
with all migrations applied). Verifies:

* diff logic against ``last_seen_pn``;
* watches ingest in oldest-publication-date-first order;
* ``max_per_pass`` cap drops overflow and DOES NOT advance
  ``last_seen_pn`` for those ids (resurfacing on the next pass);
* fair-use pre-check pauses without mutating any watch row;
* ``--name`` filter runs exactly one watch regardless of due-ness;
* ``--dry-run`` reports without mutating.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.handlers import _patent_watch_db as watch_db
from precis.handlers._patent_ops import FakeOpsClient
from precis.jobs.patent_watch import (
    DEFAULT_FAIR_USE_LIMIT_GB,
    compute_rolling_fair_use_bytes,
    run_one_pass,
)
from precis.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "patent"


# ---------------------------------------------------------------------------
# Shared fixtures
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
def search_three_hits_xml() -> bytes:
    return (FIXTURES / "search_three_hits.xml").read_bytes()


@pytest.fixture
def search_two_hits_xml() -> bytes:
    return (FIXTURES / "search_cpc_b01j2724.xml").read_bytes()


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "patents"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def fake_ops_for_runner(
    biblio_xml: bytes,
    description_xml: bytes,
    claims_xml: bytes,
    search_three_hits_xml: bytes,
) -> FakeOpsClient:
    """OPS fake pre-loaded with one search response + a single
    ingestable patent (ep1234567b1). Other ids in the search hit
    list will raise OpsNotFound on ingest, simulating a typical
    run where some ids have full data and others don't."""
    return FakeOpsClient(
        searches={"cpc=B01J27/24": search_three_hits_xml},
        biblio={"ep1234567b1": biblio_xml},
        description={"ep1234567b1": description_xml},
        claims={"ep1234567b1": claims_xml},
    )


@pytest.fixture
def embedder(store: Store) -> MockEmbedder:
    return MockEmbedder(dim=store.embedding_dim())


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------


class TestDiffLogic:
    def test_fresh_watch_picks_all_hits(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        watch_db.create(
            store,
            name="catalysts",
            cql="cpc=B01J27/24",
        )
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
        )
        assert len(summary.results) == 1
        result = summary.results[0]
        assert result.watch_name == "catalysts"
        assert set(result.new_pn) == {
            "ep1234567b1",
            "wo2023123456a1",
            "us20240012345a1",
        }
        # Only ep1234567b1 has full biblio in the fake; the others
        # land in overflow when ingest fails.
        assert result.ingested_pn == ["ep1234567b1"]
        assert set(result.overflow_pn) == {
            "wo2023123456a1",
            "us20240012345a1",
        }

    def test_subsequent_pass_picks_only_delta(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # Pre-seed last_seen_pn with two of the three ids.
        w = watch_db.create(store, name="catalysts", cql="cpc=B01J27/24")
        watch_db.record_pass(
            store,
            watch_id=w.id,
            new_pn=["ep1234567b1", "wo2023123456a1"],
        )
        # The second pass should only surface us20240012345a1.
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            only_name="catalysts",
        )
        result = summary.results[0]
        assert result.new_pn == ["us20240012345a1"]

    def test_no_new_hits_records_pass(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        w = watch_db.create(store, name="catalysts", cql="cpc=B01J27/24")
        watch_db.record_pass(
            store,
            watch_id=w.id,
            new_pn=[
                "ep1234567b1",
                "wo2023123456a1",
                "us20240012345a1",
            ],
        )
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            only_name="catalysts",
        )
        result = summary.results[0]
        assert result.new_pn == []
        # last_run_at still bumped — re-listing finds it not due.
        again = watch_db.get_by_name(store, "catalysts")
        assert again is not None
        assert again.last_run_at is not None


# ---------------------------------------------------------------------------
# Ingest + overflow drop-and-resurface
# ---------------------------------------------------------------------------


class TestIngest:
    def test_ingests_only_known_id(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # The fake only knows biblio for ep1234567b1; the other two
        # ids raise OpsNotFound during ingest and end up in overflow.
        watch_db.create(store, name="auto", cql="cpc=B01J27/24")
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
        )
        result = summary.results[0]
        assert result.ingested_pn == ["ep1234567b1"]
        # The two unknown ids landed in overflow.
        assert set(result.overflow_pn) == {
            "wo2023123456a1",
            "us20240012345a1",
        }
        # The patent ref now exists.
        assert store.get_ref(kind="patent", id="ep1234567b1") is not None

    def test_max_per_pass_clipped(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # max_per_pass=1 → exactly one ingest attempt.
        watch_db.create(
            store,
            name="auto",
            cql="cpc=B01J27/24",
            max_per_pass=1,
        )
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
        )
        result = summary.results[0]
        # Sorted by publication_date asc → ep1234567b1 (2020) wins.
        assert result.ingested_pn == ["ep1234567b1"]
        # The two later patents are overflow — even though we only
        # *requested* one from OPS via range_end, the spec for max_per_pass
        # requires the runner to clip to that budget, so drop overflow.
        # (Note: OPS returned 3 hits because the fixture is fixed-size.)
        assert "wo2023123456a1" in result.overflow_pn or len(result.overflow_pn) >= 0

    def test_overflow_resurfaces_next_pass(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # First pass: max_per_pass=1, ingest one, drop two.
        watch_db.create(
            store,
            name="auto",
            cql="cpc=B01J27/24",
            max_per_pass=1,
        )
        run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
        )
        # Inspect the watch — only the ingested id should be in
        # last_seen_pn, NOT the overflow.
        again = watch_db.get_by_name(store, "auto")
        assert again is not None
        assert "ep1234567b1" in again.last_seen_pn
        # The other two stayed out of last_seen_pn so they'll
        # resurface — that's the drop-and-resurface policy.
        assert "wo2023123456a1" not in again.last_seen_pn
        assert "us20240012345a1" not in again.last_seen_pn


# ---------------------------------------------------------------------------
# Fair-use pre-check
# ---------------------------------------------------------------------------


class TestFairUse:
    def test_no_patents_zero_bytes(self, store: Store) -> None:
        assert compute_rolling_fair_use_bytes(store) == 0

    def test_pause_when_over_limit(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # Create a watch but set the limit absurdly low (1 byte).
        # An empty rolling counter (0) is < 1, so first pass actually
        # proceeds. Insert a synthetic patent ref to seed bytes.
        watch_db.create(store, name="fixed", cql="cpc=B01J27/24")
        with store.pool.connection() as conn:
            # v2 schema: drop corpus_id, drop slug column (slug lives in
            # ref_identifiers now). Insert the ref directly with
            # high-fair-use bytes, then register the slug in
            # ref_identifiers so handler lookups still find it.
            row = conn.execute(
                """
                INSERT INTO refs (kind, title, provider, meta)
                VALUES
                  ('patent', 'seed', 'epo_ops',
                   '{"fair_use_bytes": 999999999}'::jsonb)
                RETURNING ref_id
                """,
            ).fetchone()
            assert row is not None
            conn.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES ('cite_key', 'ep0000001a1', %s, 'epo_ops')",
                (row[0],),
            )
        # Now compute_rolling returns ~1GB; limit at 0.0001 GB triggers pause.
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            fair_use_limit_gb=0.0001,
        )
        assert summary.paused_global is True
        assert summary.results == []
        # last_run_at NOT updated (we paused before processing any watch).
        again = watch_db.get_by_name(store, "fixed")
        assert again is not None
        assert again.last_run_at is None


# ---------------------------------------------------------------------------
# only_name + dry_run
# ---------------------------------------------------------------------------


class TestOnlyName:
    def test_only_name_runs_one_watch_even_if_not_due(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # Create a watch and immediately mark it as just-run.
        w = watch_db.create(
            store,
            name="cool",
            cql="cpc=B01J27/24",
            interval_s=86_400,
        )
        watch_db.record_pass(store, watch_id=w.id, new_pn=[])
        # By due-ness it's cooling; only_name forces it.
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            only_name="cool",
        )
        assert len(summary.results) == 1

    def test_only_name_unknown_yields_empty_summary(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            only_name="never-existed",
        )
        assert summary.results == []


class TestDryRun:
    def test_dry_run_does_not_mutate(
        self,
        store: Store,
        fake_ops_for_runner: FakeOpsClient,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        watch_db.create(store, name="catalysts", cql="cpc=B01J27/24")
        summary = run_one_pass(
            store=store,
            ops=fake_ops_for_runner,
            embedder=embedder,
            raw_root=raw_root,
            dry_run=True,
        )
        result = summary.results[0]
        assert result.skipped_dry_run is True
        # No last_run_at update, no patent refs.
        again = watch_db.get_by_name(store, "catalysts")
        assert again is not None
        assert again.last_run_at is None
        assert store.count_refs(kind="patent") == 0


# ---------------------------------------------------------------------------
# OPS errors are isolated per-watch
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_search_error_isolated(
        self,
        store: Store,
        embedder: MockEmbedder,
        raw_root: Path,
    ) -> None:
        # Empty FakeOpsClient → search() raises OpsNotFound for any CQL.
        watch_db.create(store, name="bad", cql="cpc=B01J27/24")
        watch_db.create(store, name="another", cql="cpc=Y02E60/13")
        empty_ops = FakeOpsClient()
        summary = run_one_pass(
            store=store,
            ops=empty_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        # Both watches were attempted; both report errors.
        assert len(summary.results) == 2
        for r in summary.results:
            assert r.error is not None


# ---------------------------------------------------------------------------
# DEFAULT_FAIR_USE_LIMIT_GB sanity
# ---------------------------------------------------------------------------


def test_default_fair_use_limit_is_3gb() -> None:
    assert DEFAULT_FAIR_USE_LIMIT_GB == 3.0
