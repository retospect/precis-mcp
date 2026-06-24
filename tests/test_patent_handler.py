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
from precis.store import Store, Tag
from tests.conftest import chunk_handle, record_handle

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
        assert (
            record_handle(handler.store, "ep1234567b1", kind="patent") in response.body
        )
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
        assert (
            chunk_handle(handler.store, "ep1234567b1", kind="patent", ord=0) in r.body
        )
        # First description paragraph mentions photocatalytic systems.
        assert "photocatalytic" in r.body.lower()

    def test_chunk_range(self, handler: PatentHandler) -> None:
        handler.get(id="EP1234567B1")
        r = handler.get(id="ep1234567b1~0..2")
        assert (
            chunk_handle(handler.store, "ep1234567b1", kind="patent", ord=0) in r.body
        )
        assert (
            chunk_handle(handler.store, "ep1234567b1", kind="patent", ord=2) in r.body
        )

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
        r = handler.search(q="photocatalysis", page_size=10)
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
        r = handler.search(q="photocatalytic", page_size=10)
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
        r = handler.search(tags=["cpc:b01j27/24"], page_size=10)
        assert "ep1234567b1" in r.body or "wo2023123456a1" in r.body

    def test_search_no_hits(
        self, handler: PatentHandler, fake_ops: FakeOpsClient
    ) -> None:
        # An unknown CQL → fake raises OpsNotFound → handler treats
        # remote as empty. With no local hits either, the response
        # carries the no-match message.
        r = handler.search(q="completely-novel-topic-xyz", page_size=10)
        assert "no patents match" in r.body.lower()

    def test_search_axis_enforcement(self, handler: PatentHandler) -> None:
        # patent kind allows {SRC, CACHE} only — STATUS:open should fail.
        with pytest.raises(BadInput, match="not allowed on kind"):
            handler.search(q="solar", tags=["STATUS:open"])

    def test_search_scope_unknown_raises(self, handler: PatentHandler) -> None:
        with pytest.raises(NotFound, match="patent slug"):
            handler.search(q="solar", scope="ep0000000a1")


# ---------------------------------------------------------------------------
# source='local' / 'remote' / 'both' — prior-art sweep affordance
# ---------------------------------------------------------------------------


class TestSearchSourceKwarg:
    """``source=`` picks which leg(s) run.  ``'remote'`` also dedupes
    OPS hits against the local store so the agent sees only patents
    it hasn't fetched yet — the natural prior-art sweep mode.
    See ``docs/user-facing/search-future-filters.md`` §7.
    """

    def test_source_invalid_raises(self, handler: PatentHandler) -> None:
        with pytest.raises(BadInput, match="invalid source"):
            handler.search(q="photocatalytic", source="nonsense")

    def test_source_local_skips_ops_call(
        self, handler: PatentHandler, fake_ops: FakeOpsClient
    ) -> None:
        # No patents locally and source='local' → no OPS call fires and
        # the envelope reports zero matches.
        before_searches = [c for c in fake_ops.calls if c[0] == "search"]
        r = handler.search(q="photocatalytic", source="local")
        assert "no patent" in r.body.lower() or "no patents match" in r.body.lower()
        # The OPS client's search() was never called for this query.
        after_searches = [c for c in fake_ops.calls if c[0] == "search"]
        assert after_searches == before_searches

    def test_source_remote_skips_local_leg(self, handler: PatentHandler) -> None:
        # Ingest the fixture patent so the local store is non-empty.
        handler.get(id="EP1234567B1")
        # source='remote' should not render block-level local hits
        # (the [local] marker disappears when local leg is skipped).
        r = handler.search(q="photocatalytic", source="remote")
        # ``wo2023123456a1`` is remote-only and NOT in the local store,
        # so it must surface.
        assert "wo2023123456a1" in r.body
        # ``ep1234567b1`` IS in the local store, so source='remote'
        # dedupes it out — the agent only sees patents it hasn't
        # fetched yet.
        assert "ep1234567b1" not in r.body

    def test_source_both_is_default(self, handler: PatentHandler) -> None:
        # source defaults to 'both' — local AND remote hits render.
        handler.get(id="EP1234567B1")
        default = handler.search(q="photocatalytic", page_size=10)
        explicit = handler.search(q="photocatalytic", page_size=10, source="both")
        assert default.body == explicit.body


# ---------------------------------------------------------------------------
# Overview affordance: missing full text is explained via a single
# agent-facing trailer ("queued for auto-retry") and the precis-
# internal "N blocks" jargon is suppressed entirely.
# ---------------------------------------------------------------------------


class TestOverviewFullTextStatus:
    def test_awaiting_fulltext_trailer_with_retry_date(
        self,
        hub: Hub,
        biblio_xml: bytes,
        raw_root: Path,
    ) -> None:
        # Biblio-only OPS (no description, no claims) — typical for a
        # fresh US application that hasn't been fully indexed yet. The
        # overview must render a single "queued for auto-retry on <date>"
        # sentence and MUST NOT leak the internal "N blocks" vocabulary.
        ops = FakeOpsClient(biblio={"ep1234567b1": biblio_xml})
        h = PatentHandler(hub=hub, ops=ops, raw_root=raw_root)
        r = h.get(id="EP1234567B1")
        assert "full text not yet indexed by OPS" in r.body
        assert "queued for auto-retry on" in r.body
        # The old "N blocks" / "OPS did not serve" lines are gone.
        assert "0 blocks" not in r.body
        assert "OPS did not serve" not in r.body

    def test_no_trailer_on_full_patent(self, handler: PatentHandler) -> None:
        # A patent with full text ingests cleanly and the overview
        # says nothing about blocks, retries, or availability.
        r = handler.get(id="EP1234567B1")
        assert "full text not yet indexed" not in r.body
        assert "full text unavailable" not in r.body
        assert "0 blocks" not in r.body

    def test_fulltext_unavailable_trailer(
        self,
        hub: Hub,
        biblio_xml: bytes,
        raw_root: Path,
        store: Store,
    ) -> None:
        # Simulate the sweep-job-has-given-up state: biblio-only ingest
        # plus a manual tag swap. The overview switches to the terminal
        # "unavailable" sentence.
        ops = FakeOpsClient(biblio={"ep1234567b1": biblio_xml})
        h = PatentHandler(hub=hub, ops=ops, raw_root=raw_root)
        h.get(id="EP1234567B1")  # ingest — adds awaiting-fulltext
        ref = store.get_ref(kind="patent", id="ep1234567b1")
        assert ref is not None
        store.remove_tag(ref.id, Tag.open("awaiting-fulltext"))
        store.add_tag(ref.id, Tag.open("fulltext-unavailable"), set_by="system")
        r = h.get(id="EP1234567B1")
        assert "full text unavailable from OPS" in r.body
        assert "searchable by abstract + biblio only" in r.body
        assert "queued for auto-retry" not in r.body


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
