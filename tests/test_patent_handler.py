"""Tests for ``PatentHandler`` — get, search, and the put rejection.

Uses ``FakeOpsClient`` so no network calls fly. The handler is
constructed directly here (registry-level env-gating tests live in
``test_patent_registry.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._patent_ops import FakeOpsClient
from precis.handlers.patent import PatentHandler
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
def search_xml() -> bytes:
    return (FIXTURES / "search_cpc_b01j2724.xml").read_bytes()


@pytest.fixture
def fake_ops(
    biblio_xml: bytes,
    description_xml: bytes,
    claims_xml: bytes,
    search_xml: bytes,
) -> FakeOpsClient:
    return FakeOpsClient(
        biblio={"ep1234567b1": biblio_xml},
        description={"ep1234567b1": description_xml},
        claims={"ep1234567b1": claims_xml},
        # The CQL the handler builds for the smoke search will match
        # whatever the q= produces; tests below pre-compute that.
        # ``photocatalytic`` is the query used for the local-merge test
        # because it stems to the same English snowball stem as the
        # text the description fixture ingests (the verbatim word
        # ``photocatalytic`` appears in paragraphs 1, 2 and the abstract,
        # whereas ``photocatalysis`` stems to ``photocatalysi`` — a
        # distinct stem — so a lex query for the latter does not hit
        # the local blocks). Without this match the
        # ``test_search_with_local_marks_local`` case devolves to a
        # remote-only response that cannot exercise the [local] /
        # dedup logic the test pins.
        searches={
            '(ti="photocatalysis" OR ab="photocatalysis")': search_xml,
            '(ti="photocatalytic" OR ab="photocatalytic")': search_xml,
            'cpc="B01J27/24"': search_xml,
        },
    )


@pytest.fixture
def raw_root(tmp_path: Path) -> Path:
    p = tmp_path / "patents"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def handler(hub: Hub, fake_ops: FakeOpsClient, raw_root: Path) -> PatentHandler:
    return PatentHandler(hub=hub, ops=fake_ops, raw_root=raw_root)


# ---------------------------------------------------------------------------
# put — explicitly unsupported
# ---------------------------------------------------------------------------


class TestPutUnsupported:
    def test_put_raises_unsupported(self, handler: PatentHandler) -> None:
        with pytest.raises(Unsupported, match="read-only"):
            handler.put(id="ep1234567b1", text="some note")


# ---------------------------------------------------------------------------
# get — first call ingests, second call hits the local store
# ---------------------------------------------------------------------------


class TestGetIngestFlow:
    def test_get_first_call_ingests(
        self, handler: PatentHandler, fake_ops: FakeOpsClient
    ) -> None:
        response = handler.get(id="EP1234567B1")
        assert "Photocatalytic NOx" in response.body
        assert "ep1234567b1" in response.body
        # Three OPS endpoints called.
        endpoints = {c[0] for c in fake_ops.calls}
        assert endpoints == {"biblio", "description", "claims"}

    def test_get_second_call_no_ops(
        self, handler: PatentHandler, fake_ops: FakeOpsClient
    ) -> None:
        handler.get(id="EP1234567B1")
        n_after_first = len(fake_ops.calls)
        handler.get(id="ep1234567b1")
        # Cache hit — no new OPS calls.
        assert len(fake_ops.calls) == n_after_first

    def test_get_unknown_patent_raises_notfound(
        self, store: Store, raw_root: Path
    ) -> None:
        empty_ops = FakeOpsClient()
        h = PatentHandler(
            hub=Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim())),
            ops=empty_ops,
            raw_root=raw_root,
        )
        with pytest.raises(NotFound, match="not found at OPS"):
            h.get(id="ep9999999z9")


# ---------------------------------------------------------------------------
# get — chunk selectors
# ---------------------------------------------------------------------------


class TestGetChunks:
    def test_single_chunk(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")  # ingest
        r = handler.get(id="ep1234567b1~0")
        assert "ep1234567b1~0" in r.body
        # First description paragraph mentions photocatalytic systems.
        assert "photocatalytic" in r.body.lower()

    def test_chunk_range(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="ep1234567b1~0..2")
        assert "ep1234567b1~0" in r.body
        assert "ep1234567b1~2" in r.body

    def test_chunk_out_of_range(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        with pytest.raises(NotFound, match="no blocks"):
            handler.get(id="ep1234567b1~100..200")

    def test_invalid_chunk_selector(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        with pytest.raises(BadInput, match="invalid chunk selector"):
            handler.get(id="ep1234567b1~abc")


# ---------------------------------------------------------------------------
# get — views
# ---------------------------------------------------------------------------


class TestGetViews:
    def test_view_abstract(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="ep1234567b1", view="abstract")
        assert "Z-scheme" in r.body

    def test_view_biblio(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="ep1234567b1", view="biblio")
        assert "Bibliographic data" in r.body
        assert "SIEMENS AG" in r.body
        assert "B01J27/24" in r.body

    def test_view_bibtex(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="ep1234567b1", view="bibtex")
        assert r.body.startswith("@misc{ep1234567b1,")
        assert "EP1234567B1" in r.body  # in the note line
        assert "espacenet" in r.body.lower()

    def test_unknown_view_rejected(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        with pytest.raises(Unsupported, match="unknown view"):
            handler.get(id="ep1234567b1", view="bogus")


# ---------------------------------------------------------------------------
# get — list views
# ---------------------------------------------------------------------------


class TestListViews:
    def test_empty_corpus_message(self, handler: PatentHandler) -> None:
        r = handler.get()
        assert "no patents" in r.body.lower()

    def test_recent_lists_ingested(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="/recent")
        assert "ep1234567b1" in r.body
        assert "most recently ingested" in r.body.lower()

    def test_published_view(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="/published")
        assert "ep1234567b1" in r.body
        assert "publication date" in r.body.lower()


# ---------------------------------------------------------------------------
# Bad input on get
# ---------------------------------------------------------------------------


class TestGetBadInput:
    def test_non_string_id_rejected(self, handler: PatentHandler) -> None:
        with pytest.raises(BadInput, match="must be a string"):
            handler.get(id=12345)

    def test_bad_slug_rejected(self, handler: PatentHandler) -> None:
        with pytest.raises(BadInput):
            handler.get(id="not-a-patent-id")


# ---------------------------------------------------------------------------
# search — local + remote merging with [local] markers
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_no_local_only_remote(self, handler: PatentHandler) -> None:
        r = handler.search(q="photocatalysis", top_k=10)
        # Remote leg picked up two hits from the search fixture.
        assert "ep1234567b1" in r.body
        assert "wo2023123456a1" in r.body
        # No local patents → no [local] markers.
        assert "[local]" not in r.body
        # Espacenet attribution footer.
        assert "espacenet" in r.body.lower()

    def test_search_with_local_marks_local(self, handler: PatentHandler) -> None:
        # Ingest the patent so it's local.
        handler.get(id="EP1234567B1")
        # ``photocatalytic`` is the literal word in the description
        # fixture; ``photocatalysis`` (used in the no-local sibling
        # test) stems to a different English-snowball lexeme and
        # would not hit the local lex CTE. Either query reaches the
        # remote leg via the equivalent fake_ops mapping.
        r = handler.search(q="photocatalytic", top_k=10)
        # Local hits are block-level, so ep1234567b1 appears in many
        # ## headers — but every one is marked [local].
        assert "[local]" in r.body
        # The remote duplicate (search fixture's ep1234567b1) is
        # suppressed: every header for this slug carries the
        # ``[local]`` marker; none appears as a bare remote hit.
        for line in r.body.splitlines():
            if line.startswith("## ") and "ep1234567b1" in line:
                assert "[local]" in line
        # The other (remote-only) hit still shows up.
        assert "wo2023123456a1" in r.body

    def test_search_via_tag_no_q(self, handler: PatentHandler) -> None:
        # cpc tag lifts to CQL even without q=.
        r = handler.search(tags=["cpc:b01j27/24"], top_k=10)
        assert "ep1234567b1" in r.body or "wo2023123456a1" in r.body

    def test_search_no_hits(
        self, handler: PatentHandler, fake_ops: FakeOpsClient
    ) -> None:
        # An unknown CQL → fake raises OpsNotFound → handler treats
        # remote as empty. With no local hits either, the response
        # carries the no-match message.
        r = handler.search(q="completely-novel-topic-xyz", top_k=10)
        assert "no patents match" in r.body.lower()

    def test_search_axis_enforcement(self, handler: PatentHandler) -> None:
        # patent kind allows {SRC, CACHE} only — STATUS:open should fail.
        with pytest.raises(BadInput, match="not allowed on kind"):
            handler.search(q="solar", tags=["STATUS:open"])

    def test_search_scope_unknown_raises(self, handler: PatentHandler) -> None:
        with pytest.raises(NotFound, match="patent slug"):
            handler.search(q="solar", scope="ep0000000a1")


# ---------------------------------------------------------------------------
# Spec sanity
# ---------------------------------------------------------------------------


class TestSpec:
    def test_spec_declares_kind(self) -> None:
        assert PatentHandler.spec.kind == "patent"
        assert PatentHandler.spec.supports_get is True
        assert PatentHandler.spec.supports_search is True
        assert PatentHandler.spec.supports_put is False

    def test_spec_requires_three_env_vars(self) -> None:
        assert set(PatentHandler.spec.requires_env) == {
            "EPO_OPS_CLIENT_KEY",
            "EPO_OPS_CLIENT_SECRET",
            "PRECIS_PATENT_RAW_ROOT",
        }
