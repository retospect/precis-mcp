"""CfpHandler — the spec-role sibling of PaperHandler (proposal writing).

Verifies the *declared* differences from paper (corpus_role, URL-only
put, restricted views, handle code) without ingesting a real PDF. The
shared get/search machinery is exercised by the paper handler tests; here
we only assert the cfp-specific spec + a registration smoke test."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.types import CallToolResult

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cfp import CfpHandler
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store, Tag
from precis.tools import core as tools_core
from precis.tools.mcp_slim import make_slim_verb
from precis.utils import handle_registry
from precis.workers.cfp_watch import META_KEY, run_cfp_watch_pass


def test_cfp_is_a_spec_role_document() -> None:
    assert CfpHandler.spec.kind == "cfp"
    assert CfpHandler.spec.corpus_role == "spec"
    # Paper is evidence, cfp is spec — the anti-citation distinction.
    assert PaperHandler.spec.corpus_role == "evidence"


def test_cfp_subclasses_paper_for_dry_reuse() -> None:
    assert issubclass(CfpHandler, PaperHandler)


def test_cfp_put_is_url_intake_and_no_citation_export_views(hub: Hub) -> None:
    # gr690299: put is the URL intake door (TestUrlIntake) — it was
    # ``False`` while a cfp could only enter as an ingested PDF.
    assert CfpHandler.spec.supports_put is True
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

    def test_browse_intersects_ids(self, store: Store) -> None:
        """gr719628: the queryless cfp browse forwards ``args={'ids': …}``
        to the tags-only renderer — without the forward the browse
        widened past the named cohort."""
        h, (later, sooner, _undated) = self._three(store)
        body = h.search(ids=[sooner]).body
        assert sooner in body and later not in body, body
        empty = h.search(ids=["missing-cfp-slug"]).body
        assert "no cfp entries" in empty

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


def test_cfp_browse_lists_status_from_saved_dates(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_AGENT_TABLE_FORMAT", "toon")
    calls = [
        ("closed", {"deadline": "2000-01-01"}),
        ("open", {"deadline": "2099-01-01"}),
        ("upcoming", {"opens_on": "2099-01-01", "deadline": "2099-02-01"}),
        ("unknown", {"deadline": "2000-02-30"}),
    ]
    refs = [
        store.insert_ref(
            kind="cfp", slug=f"{status}-call", title=f"Call {index}", meta=meta
        )
        for index, (status, meta) in enumerate(calls)
    ]
    h = CfpHandler(hub=Hub(store=store))
    for sort in ("deadline", "opens_on", "recency"):
        lines = h.search(sort=sort).body.splitlines()
        for index, ((status, _), ref) in enumerate(zip(calls, refs, strict=True)):
            rows = [
                line.split("\t") for line in lines if line.startswith(f"cf{ref.id}\t")
            ]
            assert len(rows) == 1
            assert rows[0][2:4] == [status, f"Call {index}"]
            assert "deadline" in rows[0][1]


def _tool_body(out: Any) -> str:
    """The text of an MCP tool result (a bare string, or a
    ``CallToolResult`` once a deprecation note is appended)."""
    if isinstance(out, CallToolResult):
        return str(out.content[0].text)  # type: ignore[union-attr]
    return str(out)


class TestUrlIntake:
    """gr690299 (gr462088 slice 2) — ``put(kind='cfp', url=…)`` mints a
    cfp ref, never a paper, with ``meta.source_url`` set: the page
    ``workers/cfp_watch.py`` re-checks. Nothing is fetched at put time."""

    URL = "https://example.org/call"

    def _only_cfp(self, store: Store) -> Any:
        refs = store.list_refs(kind="cfp")
        assert len(refs) == 1, refs
        return refs[0]

    def test_put_url_mints_a_cfp_not_a_paper(self, store: Store) -> None:
        h = CfpHandler(hub=Hub(store=store))
        resp = h.put(url=self.URL)
        ref = self._only_cfp(store)
        assert ref.kind == "cfp"
        assert resp.ref_id == ref.id and resp.reused is False
        assert handle_registry.format_handle("cfp", ref.id) == f"cf{ref.id}"
        assert f"cf{ref.id}" in resp.body
        assert ref.meta["source_url"] == self.URL
        assert store.list_refs(kind="paper") == []
        # The minted ref reads back like any cfp and names its page.
        assert ref.slug
        assert f"source: {self.URL}" in h.get(id=ref.slug).body

    def test_put_url_through_dispatch_args(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """``put(kind='cfp', args={'url': …})`` — the slim-schema spelling."""
        out = runtime_with_store.dispatch(
            "put", {"kind": "cfp", "__extras__": {"url": self.URL}}
        )
        ref = self._only_cfp(store)
        assert f"cf{ref.id}" in out, out
        assert ref.meta["source_url"] == self.URL
        assert store.list_refs(kind="paper") == []

    def test_put_url_over_the_slim_mcp_verb(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        """The gripe's literal call, through the callable FastMCP wires up:
        a top-level ``url=`` rides ``args=`` on the slim verb."""
        tools_core._runtime = runtime_with_store
        try:
            slim_put = make_slim_verb("put", tools_core.put)
            top = _tool_body(slim_put(kind="cfp", url=self.URL))
            nested = _tool_body(
                slim_put(
                    kind="cfp", args={"url": f"{self.URL}-two", "title": "Call Two"}
                )
            )
        finally:
            tools_core._runtime = None
        by_url = {r.meta["source_url"]: r for r in store.list_refs(kind="cfp")}
        assert set(by_url) == {self.URL, f"{self.URL}-two"}, (top, nested)
        assert f"cf{by_url[self.URL].id}" in top
        assert f"cf{by_url[f'{self.URL}-two'].id}" in nested
        assert by_url[f"{self.URL}-two"].title == "Call Two"
        assert store.list_refs(kind="paper") == []

    def test_put_refuses_a_non_http_url_with_the_edit_message(
        self, store: Store
    ) -> None:
        store.insert_ref(kind="cfp", slug="editurl0", title="Call")
        h = CfpHandler(hub=Hub(store=store))
        with pytest.raises(BadInput) as edit_err:
            h.edit(id="editurl0", args={"source_url": "ftp://example.org/call"})
        assert edit_err.value.cause == "source_url must be an http(s) URL"
        for bad in ("ftp://example.org/call", "example.org/call", "  "):
            with pytest.raises(BadInput) as put_err:
                h.put(url=bad)
            assert put_err.value.cause == edit_err.value.cause
        # Only the seeded ref: a refused put mints nothing.
        assert [r.slug for r in store.list_refs(kind="cfp")] == ["editurl0"]
        assert store.list_refs(kind="paper") == []

    def test_put_same_url_is_idempotent(self, store: Store) -> None:
        h = CfpHandler(hub=Hub(store=store))
        first = h.put(url=self.URL)
        again = h.put(url=self.URL)
        assert again.ref_id == first.ref_id and again.reused is True
        self._only_cfp(store)

    def test_put_without_url_or_with_paper_fields_mints_nothing(
        self, store: Store
    ) -> None:
        h = CfpHandler(hub=Hub(store=store))
        # On the inherited paper path a bare title= minted a title-only
        # *paper* stub.
        with pytest.raises(BadInput, match="url"):
            h.put(title="Call X")
        with pytest.raises(BadInput, match="doi"):
            h.put(url=self.URL, doi="10.1234/not-a-call")
        assert store.list_refs(kind="cfp") == []
        assert store.list_refs(kind="paper") == []

    def test_paper_doors_are_refused_through_dispatch(
        self, runtime_with_store: PrecisRuntime, store: Store
    ) -> None:
        out = runtime_with_store.dispatch(
            "put", {"kind": "cfp", "doi": "10.1234/not-a-call"}
        )
        assert "[error:" in out, out
        assert store.list_refs(kind="cfp") == []
        assert store.list_refs(kind="paper") == []

    def test_put_with_watch_tag_is_picked_up_by_cfp_watch(self, store: Store) -> None:
        h = CfpHandler(hub=Hub(store=store))
        resp = h.put(url=self.URL, tags=["WATCH:weekly"])
        assert resp.ref_id is not None
        assert "tagged: WATCH:weekly" in resp.body
        fetched: list[str] = []

        def fetch(url: str) -> str:
            fetched.append(url)
            return "<p>Submission deadline: 15 March 2027</p>"

        result = run_cfp_watch_pass(store, fetch=fetch, now=datetime.now(UTC))
        assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
        assert fetched == [self.URL]
        ref = store.get_ref(kind="cfp", id=resp.ref_id)
        assert ref is not None
        assert ref.meta[META_KEY]["dates"] == ["15 march 2027"]
