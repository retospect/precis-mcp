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
        # Verify tags via direct SQL — open tags live in ref_open_tags.
        with store.pool.connection() as conn:
            tags = {
                row[0]
                for row in conn.execute(
                    "SELECT value FROM ref_open_tags WHERE ref_id = %s",
                    (result.ref_id,),
                ).fetchall()
            }
        assert "country:ep" in tags
        assert "kind:b1" in tags
        assert "family:012345678" in tags
        assert "cpc:b01j27/24" in tags
        assert "applicant:siemens-ag" in tags


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
