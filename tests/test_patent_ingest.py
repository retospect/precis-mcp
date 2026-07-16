"""Tests for ``ingest_patent`` — fetch+parse+store pipeline.

Uses ``FakeOpsClient`` (no network) and the standard ``store``
fixture from ``conftest.py`` (ephemeral postgres DB with all
migrations applied).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.errors import NotFound
from precis.handlers._patent_ingest import ingest_patent
from precis.handlers._patent_ops import FakeOpsClient
from precis.handlers._patent_slug import parse_docdb_id
from precis.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "patent"


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
def fake_ops(
    biblio_xml: bytes,
    description_xml: bytes,
    claims_xml: bytes,
) -> FakeOpsClient:
    """Pre-loaded fake — three endpoints answer for ``ep1234567b1``."""
    return FakeOpsClient(
        biblio={"ep1234567b1": biblio_xml},
        description={"ep1234567b1": description_xml},
        claims={"ep1234567b1": claims_xml},
    )


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    """Per-test raw-XML cache root — under tmp_path."""
    p = tmp_path / "patents"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestIngestFirstCall:
    def test_inserts_ref_blocks_meta(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        result = ingest_patent(
            "EP1234567B1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        assert result.inserted is True
        assert result.slug == "ep1234567b1"
        # 4 description paragraphs + 3 claims = 7 blocks.
        assert result.block_count == 7

        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        assert ref.title == "Photocatalytic NOx reduction system"
        assert ref.provider == "epo_ops"
        assert ref.meta["country"] == "ep"
        assert ref.meta["kind_code"] == "b1"
        assert ref.meta["family_id"] == "012345678"
        assert ref.meta["publication_date"] == "2020-01-15"
        assert "B01J27/24" in ref.meta["cpc_classes"]
        assert ref.meta["applicants"][0]["name"] == "SIEMENS AG"

    def test_writes_raw_xml_to_disk(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        # Disk layout: <root>/ep/1234567/b1/{biblio,description,claims}.xml
        d = raw_root / "ep" / "1234567" / "b1"
        assert (d / "biblio.xml").exists()
        assert (d / "description.xml").exists()
        assert (d / "claims.xml").exists()
        # Bytes round-trip (atomic write).
        assert (d / "biblio.xml").read_bytes().startswith(b'<?xml version="1.0"')

    def test_calls_three_ops_endpoints(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        endpoints = {call[0] for call in fake_ops.calls}
        assert endpoints == {"biblio", "description", "claims"}


class TestClaimMarking:
    """Slice 1: each block carries a ``patent_block`` meta marker, and
    claim blocks record their independent/dependent structure
    (docs/design/patent-authoring-loop.md)."""

    def _ingest(self, store: Store, fake_ops: FakeOpsClient, raw_root: Path) -> object:
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        return ref

    def test_blocks_are_marked_description_then_claims(
        self, store: Store, fake_ops: FakeOpsClient, raw_root: Path
    ) -> None:
        ref = self._ingest(store, fake_ops, raw_root)
        blocks = store.list_blocks_for_ref(ref.id)  # type: ignore[attr-defined]
        kinds = [(b.meta or {}).get("patent_block") for b in blocks]
        # 4 description paragraphs first, then 3 claims.
        assert kinds == ["description"] * 4 + ["claim"] * 3

    def test_claim_structure_recorded(
        self, store: Store, fake_ops: FakeOpsClient, raw_root: Path
    ) -> None:
        ref = self._ingest(store, fake_ops, raw_root)
        claims = [
            b.meta
            for b in store.list_blocks_for_ref(ref.id)  # type: ignore[attr-defined]
            if (b.meta or {}).get("patent_block") == "claim"
        ]
        # Claim 1 independent; claims 2 and 3 each depend on claim 1.
        assert [c["claim_number"] for c in claims] == [1, 2, 3]
        assert claims[0]["claim_independent"] is True
        assert claims[0]["depends_on"] == []
        assert claims[1]["claim_independent"] is False
        assert claims[1]["depends_on"] == [1]
        assert claims[2]["depends_on"] == [1]

    def test_auto_tags_applied(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        result = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        # Verify tags via direct SQL — v2 unifies the legacy
        # ref_open_tags into ref_tags JOIN tags with namespace='OPEN'.
        with store.pool.connection() as conn:
            tags = {
                row[0]
                for row in conn.execute(
                    "SELECT t.value FROM ref_tags rt "
                    "JOIN tags t USING (tag_id) "
                    "WHERE rt.ref_id = %s AND t.namespace = 'OPEN'",
                    (result.ref_id,),
                ).fetchall()
            }
        assert "country:ep" in tags
        assert "kind:b1" in tags
        assert "family:012345678" in tags
        # Auto-tags removed 2026-06-16 (T10.4): applicant/cpc/ipc
        # cluttered the cluster's global tag table (one row per Chinese
        # university, one per IPC subclass). The data is in
        # ``meta.applicants`` / ``meta.cpc_classes`` / ``meta.ipc_classes``
        # already; CQL lift consumes the slug directly without a meta
        # lookup. Country / kind / family stay — short, distinct, and
        # useful as plain tag filters.
        assert not any(t.startswith("applicant:") for t in tags)
        assert not any(t.startswith("cpc:") for t in tags)
        assert not any(t.startswith("ipc:") for t in tags)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIngestIdempotency:
    def test_second_call_skips_ops(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        first = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        first_call_count = len(fake_ops.calls)

        second = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        # Same ref_id, no inserted, no extra OPS calls.
        assert second.ref_id == first.ref_id
        assert second.inserted is False
        assert second.bytes_fetched == 0
        assert len(fake_ops.calls) == first_call_count


class TestForceReingest:
    """``force=True`` re-fetches OPS and swaps an existing ref's blocks in
    place, so patents ingested before the ``patent_block`` marker existed
    get re-marked (the claim-marking backfill,
    docs/design/patent-authoring-loop.md)."""

    def test_force_refetches_and_replaces_blocks(
        self, store: Store, fake_ops: FakeOpsClient, raw_root: Path
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        first = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        calls_after_first = len(fake_ops.calls)

        second = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
            force=True,
        )
        # Same ref (kept), but a fresh fetch happened and blocks were
        # replaced (not appended): still 7, not 14.
        assert second.ref_id == first.ref_id
        assert second.inserted is False
        assert len(fake_ops.calls) > calls_after_first  # OPS was hit again
        assert store.count_blocks(first.ref_id) == 7
        assert second.block_count == 7

    def test_force_stamps_markers_on_previously_unmarked_ref(
        self, store: Store, fake_ops: FakeOpsClient, raw_root: Path
    ) -> None:
        # Simulate a pre-slice-1 ref: ingest, then strip the markers from
        # its chunks (as the old ingest path would have left them).
        embedder = MockEmbedder(dim=store.embedding_dim())
        result = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET meta = '{}'::jsonb WHERE ref_id = %s",
                (result.ref_id,),
            )
        # Confirm they're unmarked now.
        blocks = store.list_blocks_for_ref(result.ref_id)
        assert all(not (b.meta or {}).get("patent_block") for b in blocks)

        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
            force=True,
        )
        kinds = [
            (b.meta or {}).get("patent_block")
            for b in store.list_blocks_for_ref(result.ref_id)
        ]
        assert kinds == ["description"] * 4 + ["claim"] * 3

    def test_force_on_stub_fills_claims_and_clears_awaiting_tag(
        self,
        store: Store,
        biblio_xml: bytes,
        description_xml: bytes,
        claims_xml: bytes,
        raw_root: Path,
    ) -> None:
        embedder = MockEmbedder(dim=store.embedding_dim())
        # First ingest: biblio only → 0 blocks, awaiting-fulltext tag.
        stub_ops = FakeOpsClient(biblio={"ep1234567b1": biblio_xml})
        stub = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=stub_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        assert stub.block_count == 0
        tag_values = {
            t.value for t in store.tags_for(stub.ref_id) if t.namespace == "open"
        }
        assert "awaiting-fulltext" in tag_values

        # Force-reingest now that OPS serves the full text.
        full_ops = FakeOpsClient(
            biblio={"ep1234567b1": biblio_xml},
            description={"ep1234567b1": description_xml},
            claims={"ep1234567b1": claims_xml},
        )
        again = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=full_ops,
            embedder=embedder,
            raw_root=raw_root,
            force=True,
        )
        assert again.block_count == 7
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        assert ref.meta.get("has_claims") is True
        cleared = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert "awaiting-fulltext" not in cleared


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestIngestErrors:
    def test_missing_patent_raises_notfound(
        self,
        store: Store,
        raw_root: Path,
    ) -> None:
        empty_ops = FakeOpsClient()  # no canned responses
        with pytest.raises(NotFound, match="not found at OPS"):
            ingest_patent(
                "ep9999999z9",
                store=store,
                ops=empty_ops,
                embedder=MockEmbedder(dim=store.embedding_dim()),
                raw_root=raw_root,
            )
        # No state mutated.
        assert store.get_ref(kind="patent", id="ep9999999z9") is None

    def test_missing_description_falls_through(
        self,
        store: Store,
        biblio_xml: bytes,
        claims_xml: bytes,
        raw_root: Path,
    ) -> None:
        # Biblio + claims, but no description (e.g. early A-publication).
        ops = FakeOpsClient(
            biblio={"ep1234567b1": biblio_xml},
            claims={"ep1234567b1": claims_xml},
            # description left empty → FakeOpsClient raises OpsNotFound,
            # and ingest treats that as "no description available".
        )
        embedder = MockEmbedder(dim=store.embedding_dim())
        result = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        # 0 description + 3 claims = 3 blocks.
        assert result.block_count == 3

        # No description.xml on disk for this case.
        d = raw_root / "ep" / "1234567" / "b1"
        assert (d / "biblio.xml").exists()
        assert not (d / "description.xml").exists()

        # ``has_description`` flag reflects the 404 so the sweep job
        # can pick this ref up for retry.
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        assert ref.meta.get("has_description") is False
        assert ref.meta.get("has_claims") is True

        # Awaiting-fulltext tag + retry schedule landed in meta.
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert "awaiting-fulltext" in tag_values
        assert isinstance(ref.meta.get("fulltext_retry_at"), str)
        assert ref.meta.get("fulltext_retry_count") == 0

    def test_missing_both_fulltext_endpoints(
        self,
        store: Store,
        biblio_xml: bytes,
        raw_root: Path,
    ) -> None:
        # Recent US application: biblio OK, description + claims
        # both 404. The patent still ingests (searchable by biblio
        # + abstract), both flags are False, and the sweep job
        # will pick it up via the awaiting-fulltext tag.
        ops = FakeOpsClient(biblio={"ep1234567b1": biblio_xml})
        embedder = MockEmbedder(dim=store.embedding_dim())
        result = ingest_patent(
            "ep1234567b1",
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        assert result.block_count == 0
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        assert ref.meta.get("has_description") is False
        assert ref.meta.get("has_claims") is False
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert "awaiting-fulltext" in tag_values
        assert isinstance(ref.meta.get("fulltext_retry_at"), str)

    def test_full_ingest_has_no_retry_bookkeeping(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        # Happy-path ingest (all three endpoints served) — no
        # awaiting-fulltext tag, no retry timestamp in meta.
        embedder = MockEmbedder(dim=store.embedding_dim())
        ingest_patent(
            "ep1234567b1",
            store=store,
            ops=fake_ops,
            embedder=embedder,
            raw_root=raw_root,
        )
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
        assert "awaiting-fulltext" not in tag_values
        assert "fulltext_retry_at" not in ref.meta
        assert "fulltext_retry_count" not in ref.meta


# ---------------------------------------------------------------------------
# DocDbId input
# ---------------------------------------------------------------------------


class TestIngestAcceptsDocDbId:
    def test_pre_parsed_id(
        self,
        store: Store,
        fake_ops: FakeOpsClient,
        raw_root: Path,
    ) -> None:
        parsed = parse_docdb_id("EP1234567B1")
        result = ingest_patent(
            parsed,
            store=store,
            ops=fake_ops,
            embedder=MockEmbedder(dim=store.embedding_dim()),
            raw_root=raw_root,
        )
        assert result.slug == "ep1234567b1"
