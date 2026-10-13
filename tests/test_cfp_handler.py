"""CfpHandler — the spec-role sibling of PaperHandler (proposal writing).

Verifies the *declared* differences from paper (corpus_role, no put,
restricted views, handle code) without ingesting a real PDF. The shared
get/search machinery is exercised by the paper handler tests; here we
only assert the cfp-specific spec + a registration smoke test."""

from __future__ import annotations

import inspect

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cfp import CfpHandler
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store, Tag
from precis.utils import handle_registry


def test_cfp_is_a_spec_role_document() -> None:
    assert CfpHandler.spec.kind == "cfp"
    assert CfpHandler.spec.corpus_role == "spec"
    # Paper is evidence, cfp is spec — the anti-citation distinction.
    assert PaperHandler.spec.corpus_role == "evidence"


def test_cfp_subclasses_paper_for_dry_reuse() -> None:
    assert issubclass(CfpHandler, PaperHandler)


def test_cfp_has_no_put_and_no_citation_export_views(hub: Hub) -> None:
    # A CFP is acquired by ingesting a PDF, not by minting a stub.
    assert CfpHandler.spec.supports_put is False
    # Reading verbs are inherited.
    assert CfpHandler.spec.supports_get is True
    assert CfpHandler.spec.supports_search is True
    # Citation-export views are dropped (a spec is never a bib entry).
    views = CfpHandler(hub=hub).accepted_views()
    assert "bibtex" not in views
    assert "ris" not in views
    assert "bibliography" not in views
    # …but the reading views carry over.
    assert "toc" in views
    assert "abstract" in views


def test_cfp_handle_codes_registered() -> None:
    assert handle_registry.code_for_kind("cfp") == "cf"
    assert handle_registry.code_for_kind("cfp", chunk=True) == "qc"
    # Round-trips through the universal handle parser.
    kind, is_chunk = handle_registry.kind_for_code("cf")
    assert kind == "cfp" and is_chunk is False


def test_cfp_empty_search_names_cfp_not_paper(store: Store) -> None:
    """Regression: cfp (and datasheet) subclass PaperHandler and reuse
    its search path, whose empty-result branch hardcoded the noun as
    "no paper blocks match" — leaking the *paper* kind on a cfp miss.
    The empty-result noun must be the actual searched kind, matching the
    per-kind convention every other block handler follows (tex/web/
    markdown/plaintext/perplexity all say "no <kind> blocks match")."""
    lex_only = CfpHandler(hub=Hub(store=store))
    resp = lex_only.search(q="zzqqxx-no-such-token")
    assert "no cfp blocks match" in resp.body
    assert "paper" not in resp.body


def test_cfp_empty_list_names_cfp_not_paper(store: Store) -> None:
    """Regression (gr311347 #12): the ``get(id=None)`` empty-corpus
    listing inherited PaperHandler's "no papers ingested yet - use
    `precis jobs ingest-bundles`" — paper-pipeline phrasing that doesn't
    apply to a cfp (never ingested via that job; acquired by dropping a
    PDF into ``inbox/cfp/`` or ``precis add --as cfp``)."""
    resp = CfpHandler(hub=Hub(store=store)).get()
    assert "no CFPs cached yet" in resp.body
    assert "ingest-bundles" not in resp.body
    assert "inbox/cfp/" in resp.body or "add --as cfp" in resp.body

    # Control: PaperHandler itself keeps its own message unchanged.
    from precis.handlers.paper import PaperHandler

    paper_resp = PaperHandler(hub=Hub(store=store)).get()
    assert "no papers ingested yet" in paper_resp.body
    assert "ingest-bundles" in paper_resp.body


def test_cfp_edit_ack_names_cfp_and_get_offers_no_bibtex(store: Store) -> None:
    """gr462088 #5: the edit ack said "updated paper"; the abstract and
    chunk-nav hints offered a BibTeX entry a cfp does not have."""
    ref = store.insert_ref(kind="cfp", slug="callfor00", title="Call for X")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="Body text.")])
    h = CfpHandler(hub=Hub(store=store))
    ack = h.edit(id="callfor00", year=2026)
    assert ack.body.startswith("updated cfp id=")
    assert "paper" not in ack.body
    got = store.get_ref(kind="cfp", id="callfor00")
    assert got is not None and got.year == 2026
    for view in ("abstract", None):
        body = h.get(id="callfor00" if view else "callfor00~0", view=view).body
        assert "BibTeX" not in body
        assert "bibtex" not in body


def _meta(store: Store, slug: str) -> dict:
    ref = store.get_ref(kind="cfp", id=slug)
    assert ref is not None
    return ref.meta


class TestTypedDates:
    """gr462088 slice 1 — ``meta.deadline``/``meta.opens_on`` (ISO
    YYYY-MM-DD): set via ``edit(args=…)``, shown on the overview, and
    the queryless ``search`` browses deadline-sorted."""

    def test_edit_sets_deadline_and_opens_on(self, store: Store) -> None:
        store.insert_ref(kind="cfp", slug="dated000", title="Call")
        h = CfpHandler(hub=Hub(store=store))
        ack = h.edit(
            id="dated000",
            args={"deadline": "2026-11-15", "opens_on": "2026-08-01"},
        )
        assert "deadline=2026-11-15" in ack.body
        assert "opens_on=2026-08-01" in ack.body
        meta = _meta(store, "dated000")
        assert meta["deadline"] == "2026-11-15"
        assert meta["opens_on"] == "2026-08-01"
        # A date arg composes with a field edit in one call.
        h.edit(id="dated000", year=2027, args={"deadline": "2026-12-01"})
        got = store.get_ref(kind="cfp", id="dated000")
        assert got is not None
        assert got.year == 2027 and got.meta["deadline"] == "2026-12-01"

    def test_edit_rejects_bad_dates_without_writing(self, store: Store) -> None:
        store.insert_ref(kind="cfp", slug="baddate0", title="Call")
        h = CfpHandler(hub=Hub(store=store))
        for key, bad in (
            ("deadline", "Nov 15 2026"),
            ("deadline", "2026-13-40"),
            ("deadline", "20261115"),
            ("opens_on", "15/11/2026"),
        ):
            with pytest.raises(BadInput, match=key):
                h.edit(id="baddate0", args={key: bad})
        meta = _meta(store, "baddate0")
        assert "deadline" not in meta and "opens_on" not in meta

    def test_edit_empty_date_clears(self, store: Store) -> None:
        store.insert_ref(
            kind="cfp",
            slug="clear000",
            title="Call",
            meta={"deadline": "2026-11-15"},
        )
        h = CfpHandler(hub=Hub(store=store))
        ack = h.edit(id="clear000", args={"deadline": ""})
        assert "deadline cleared" in ack.body
        assert _meta(store, "clear000")["deadline"] is None

    def test_overview_shows_typed_dates(self, store: Store) -> None:
        store.insert_ref(
            kind="cfp",
            slug="shownd00",
            title="Call",
            meta={"deadline": "2026-11-15", "opens_on": "2026-08-01"},
        )
        h = CfpHandler(hub=Hub(store=store))
        body = h.get(id="shownd00").body
        assert "deadline: 2026-11-15" in body
        assert "opens on: 2026-08-01" in body
        # Undated calls get no date lines at all.
        store.insert_ref(kind="cfp", slug="nodates0", title="Call 2")
        plain = h.get(id="nodates0").body
        assert "deadline" not in plain and "opens on" not in plain


class TestBrowseSort:
    def _three(self, store: Store) -> tuple[CfpHandler, list[str]]:
        handles = [
            f"cf{store.insert_ref(kind='cfp', slug=s, title=t, meta=m).id}"
            for s, t, m in (
                ("later000", "Later call", {"deadline": "2027-03-01"}),
                ("sooner00", "Sooner call", {"deadline": "2026-11-15"}),
                ("undated0", "Undated call", None),
            )
        ]
        return CfpHandler(hub=Hub(store=store)), handles

    def test_bare_search_browses_deadline_sorted(self, store: Store) -> None:
        h, (later, sooner, undated) = self._three(store)
        body = h.search().body
        order = [body.index(sooner), body.index(later), body.index(undated)]
        assert order == sorted(order)
        assert "deadline 2026-11-15" in body
        assert "(no deadline)" in body

    def test_sort_recency_and_opens_on(self, store: Store) -> None:
        h, (later, sooner, undated) = self._three(store)
        store.stamp_ref_meta(int(undated[2:]), {"opens_on": "2026-01-01"})
        recency = h.search(sort="recency").body
        # Recency = last-written first: undated's meta stamp is newest.
        assert recency.index(undated) < recency.index(later)
        opens = h.search(sort="opens_on").body
        assert opens.index(undated) < opens.index(sooner)
        assert "opens_on 2026-01-01" in opens
        assert "(no opens_on)" in opens

    def test_sort_combines_with_tags(self, store: Store) -> None:
        h, (_, sooner, _) = self._three(store)
        body = h.search(tags=["WATCH:weekly"]).body
        assert "no cfp entries tagged" in body
        watched = store.insert_ref(
            kind="cfp",
            slug="watched0",
            title="Watched",
            meta={"deadline": "2026-10-01"},
        )
        store.add_tag(watched.id, Tag.closed("WATCH", "weekly"))
        body = h.search(tags=["WATCH:weekly"], sort="deadline").body
        assert f"cf{watched.id}" in body and sooner not in body

    def test_sort_rejected_with_q_and_unknown_keys(self, store: Store) -> None:
        h, _ = self._three(store)
        with pytest.raises(BadInput, match="sort="):
            h.search(q="materials", sort="deadline")
        with pytest.raises(BadInput, match="sort="):
            h.search(sort="bogus")
        with pytest.raises(BadInput, match="needs q="):
            h.search(good=True)
        # Paper never learned sort — the kwarg isn't in its signature.
        assert "sort" not in inspect.signature(PaperHandler.search).parameters


def test_cfp_browse_via_dispatch_extras(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The MCP call shape: ``search(kind='cfp', args={'sort': 'deadline'})``
    routes the extras dict through dispatch's kwarg flattening."""
    ref = store.insert_ref(
        kind="cfp",
        slug="disp0000",
        title="Call",
        meta={"deadline": "2026-11-15"},
    )
    out = runtime_with_store.dispatch(
        "search", {"kind": "cfp", "__extras__": {"sort": "deadline"}}
    )
    assert f"cf{ref.id}" in out and "deadline 2026-11-15" in out
    bad = runtime_with_store.dispatch(
        "search", {"kind": "cfp", "__extras__": {"sort": "zz"}}
    )
    assert "BadInput" in bad
    # Paper still rejects sort= outright.
    paper = runtime_with_store.dispatch(
        "search", {"kind": "paper", "__extras__": {"sort": "deadline"}}
    )
    assert "BadInput" in paper
