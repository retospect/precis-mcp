"""Real-store contracts for unimported citations and later draft promotion.

All source quotations are synthetic. Extraction is replaced at the producer
boundary so import completion still exercises the real writer and store.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.finding_drafts import promote_drafts_for_paper
from precis.handlers.finding import FindingHandler
from precis.handlers.todo import TodoHandler
from precis.ingest.add import DoiInput, precis_add
from precis.ingest.db_writer import ChunkToWrite, PaperToWrite
from precis.store.types import ChunkInsert, Tag
from precis.utils.url import canonical_url

QUOTE = "Synthetic copper lanterns retain their violet glow under steady cooling."
URL = "https://example.org/synthetic-lanterns"
DOI = "10.5555/synthetic-lanterns"
VERDICT = {
    "verdict": "supports",
    "verifier": "test-verifier",
    "checked_at": "2026-10-28T00:00:00Z",
}


def _put(
    store,
    *,
    source=URL,
    quote=QUOTE,
    title="Synthetic lantern finding",
    body="Synthetic lantern glow persists during controlled cooling.",
):
    response = FindingHandler(hub=Hub(store=store)).put(
        title=title,
        body=body,
        cited_in=source,
        tags=["STATUS:staged"],
        meta={"quote": quote, **VERDICT},
    )
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None, response.body
    return int(match.group(1))


def _status(store, finding_id):
    return {
        tag.value
        for tag in store.tags_for(finding_id)
        if tag.namespace == "closed" and tag.prefix == "STATUS"
    }


def _meta(store, finding_id):
    ref = store.get_ref(kind="finding", id=finding_id)
    assert ref is not None
    return ref.meta or {}


def _paper(store, *, url=URL, quote=QUOTE, doi=None):
    ref = store.insert_ref(
        kind="paper",
        slug="synthetic26",
        title="Synthetic lantern study",
        meta={"markup_source_url": url},
    )
    if doi:
        store.insert_ref_identifiers(ref.id, [("doi", doi, "test")])
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=f"Study results: {quote}", meta={})]
    )
    return ref.id


def _assert_promoted(store, finding_id, paper_id):
    assert _status(store, finding_id) == {"tracing"}
    meta = _meta(store, finding_id)
    for key, value in VERDICT.items():
        assert meta[key] == value
    chain = meta["chain"]
    assert len(chain) == 1
    assert chain[0]["ref_id"] == paper_id
    assert chain[0]["ord"] == 0
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0",
            (paper_id,),
        ).fetchone()
    assert row is not None
    assert chain[0]["chunk_id"] == row[0]
    links = store.links_for(finding_id, direction="out", relation="derived-from")
    assert len(links) == 1
    assert links[0].dst_ref_id == paper_id
    assert links[0].dst_chunk_id == row[0]


def test_plain_url_creates_draft_and_preserves_verdict_without_fetch(store):
    # A reserved example domain has no fixture server: creation must stay local.
    with (
        patch(
            "precis.utils.safe_fetch.safe_get",
            side_effect=AssertionError("network fetch"),
        ),
        patch(
            "precis.handlers.web.WebHandler.get",
            side_effect=AssertionError("web fetch"),
        ),
    ):
        finding_id = _put(store, source=URL + "#results")
    assert _status(store, finding_id) == {"staged"}
    meta = _meta(store, finding_id)
    assert meta["draft_source"]["url"] == canonical_url(URL + "#results")
    assert meta["draft_source"]["quote"] == QUOTE
    for key, value in VERDICT.items():
        assert meta[key] == value
    assert not store.links_for(finding_id, direction="out", relation="derived-from")


@pytest.mark.parametrize("cached", [True, False], ids=["cache-url", "legacy-ref-url"])
def test_web_source_preserves_frontier_then_replaces_it_with_chunk(store, cached):
    if cached:
        web, _ = store.put_cache_entry(
            kind="web",
            slug="synthetic-lanterns",
            title="Synthetic source",
            body_blocks=[ChunkInsert(ord=0, text=QUOTE, meta={})],
            provider="web",
            request_hash="synthetic-lanterns-request",
            ttl_seconds=None,
            cache_meta={"url": URL},
        )
        assert "url" not in (web.meta or {})
    else:
        web = store.insert_ref(
            kind="web",
            slug="synthetic-lanterns",
            title="Synthetic source",
            meta={"url": URL},
        )
    finding_id = _put(store, source="web:synthetic-lanterns")
    assert _meta(store, finding_id)["draft_source"]["web_ref_id"] == web.id
    links = store.links_for(finding_id, direction="out", relation="derived-from")
    assert len(links) == 1 and links[0].dst_ref_id == web.id
    paper_id = _paper(store)
    assert promote_drafts_for_paper(store, paper_id) == 1
    _assert_promoted(store, finding_id, paper_id)


def test_canonical_source_url_promotes_and_repeated_completion_is_idempotent(store):
    finding_id = _put(store, source=URL + "#results")
    paper_id = _paper(store)
    assert promote_drafts_for_paper(store, paper_id) == 1
    _assert_promoted(store, finding_id, paper_id)
    before = _meta(store, finding_id)
    assert promote_drafts_for_paper(store, paper_id) == 0
    assert _meta(store, finding_id) == before
    _assert_promoted(store, finding_id, paper_id)


def test_nonmatching_source_stays_draft_unchanged(store):
    finding_id = _put(store, source="https://example.org/unrelated")
    paper_id = _paper(store)
    before = _meta(store, finding_id)
    assert promote_drafts_for_paper(store, paper_id) == 0
    assert _status(store, finding_id) == {"staged"}
    assert _meta(store, finding_id) == before


@pytest.mark.parametrize(
    ("quote", "reason"),
    [
        (
            "An entirely different synthetic quotation about blue umbrellas.",
            "no_quote_match",
        ),
        ("tiny quote", "quote_too_short"),
    ],
    ids=["missing-quotation", "short-quotation"],
)
def test_failed_reanchor_leaves_a_note(store, quote, reason):
    finding_id = _put(store, quote=quote)
    paper_id = _paper(store)
    assert promote_drafts_for_paper(store, paper_id) == 0
    assert _status(store, finding_id) == {"staged"}
    note = _meta(store, finding_id)["staged_reanchor"]
    assert note["paper_id"] == paper_id
    assert note["reason"] == reason
    assert note["at"]


def test_doi_import_completion_promotes_only_matching_draft(store):
    matching = _put(store, source=f"https://doi.org/{DOI.upper()}")
    other = _put(
        store, source="https://doi.org/10.5555/other", title="Other synthetic finding"
    )
    staged = PaperToWrite(
        title="Synthetic lantern study",
        authors=[{"name": "Synthetic, Ada"}],
        year=2026,
        provider="crossref",
        paper_id="synthetic-lantern-import",
        pub_id=f"doi:{DOI}",
        cite_key_prefix="synthetic26",
        doi=DOI,
        chunks=[ChunkToWrite(ord=0, chunk_kind="paragraph", text=QUOTE)],
    )
    with patch("precis.ingest.pipeline.fetch_paper_by_doi", return_value=staged):
        result = precis_add(DoiInput(doi=DOI), store=store)
        assert result is not None
        _assert_promoted(store, matching, result.ref_id)
        assert _status(store, other) == {"staged"}
        # A draft minted after the import finished promotes at create
        # time — the source paper already exists. The duplicate import
        # completion after it is then a promotion no-op.
        later = _put(
            store,
            source=f"https://doi.org/{DOI}",
            title="Later lantern finding",
            body="A second synthetic claim grounded in the same quoted source.",
        )
        assert later != matching
        _assert_promoted(store, later, result.ref_id)
        repeated = precis_add(DoiInput(doi=DOI), store=store)
    assert repeated is not None and repeated.inserted is False
    assert repeated.ref_id == result.ref_id
    _assert_promoted(store, later, result.ref_id)
    _assert_promoted(store, matching, result.ref_id)


def test_staged_put_promotes_when_source_paper_already_exists(store):
    paper_id = _paper(store)
    response = FindingHandler(hub=Hub(store=store)).put(
        title="Synthetic lantern finding",
        body="Synthetic lantern glow persists during controlled cooling.",
        cited_in=URL,
        tags=["STATUS:staged"],
        meta={"quote": QUOTE, **VERDICT},
    )
    assert "STATUS:tracing" in response.body
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None
    _assert_promoted(store, int(match.group(1)), paper_id)


def test_staged_put_promotes_on_existing_doi_paper(store):
    paper_id = _paper(store, url="https://doi.org/elsewhere", doi=DOI)
    finding_id = _put(store, source=f"https://doi.org/{DOI.upper()}")
    _assert_promoted(store, finding_id, paper_id)


def test_promotion_failure_does_not_fail_the_ingest(store):
    staged = PaperToWrite(
        title="Synthetic lantern study",
        authors=[{"name": "Synthetic, Ada"}],
        year=2026,
        provider="crossref",
        paper_id="synthetic-lantern-import",
        pub_id=f"doi:{DOI}",
        cite_key_prefix="synthetic26",
        doi=DOI,
        chunks=[ChunkToWrite(ord=0, chunk_kind="paragraph", text=QUOTE)],
    )
    with (
        patch("precis.ingest.pipeline.fetch_paper_by_doi", return_value=staged),
        patch(
            "precis.finding_drafts.promote_drafts_for_paper",
            side_effect=RuntimeError("synthetic promotion fault"),
        ),
    ):
        result = precis_add(DoiInput(doi=DOI), store=store)
    # The paper is committed — a post-commit promotion fault is logged
    # and swallowed, not surfaced as an ingest failure.
    assert result is not None and result.ref_id


def test_staged_detail_and_status_list_label_the_draft(store):
    finding_id = _put(store)
    handler = FindingHandler(hub=Hub(store=store))
    assert "staged" in handler.get(id=str(finding_id)).body.lower()
    recent = handler.get(id="/recent").body
    assert "Synthetic lantern finding" in recent
    assert "STATUS:staged" in recent
    response = handler.search(status="staged")
    assert "Synthetic lantern finding" in response.body
    assert "staged" in response.body.lower()


@pytest.mark.parametrize(
    "stored_quote",
    [
        QUOTE.upper(),
        QUOTE[:50] + " followed by a deliberately different synthetic ending.",
    ],
    ids=["case-normalization", "first-fifty-character-prefix"],
)
def test_promotion_uses_existing_normalized_prefix_quote_rules(store, stored_quote):
    finding_id = _put(store, quote=stored_quote)
    paper_id = _paper(store)
    assert promote_drafts_for_paper(store, paper_id) == 1
    _assert_promoted(store, finding_id, paper_id)


def test_staged_status_is_valid_for_findings_but_not_todos(store):
    assert Tag.parse_strict("STATUS:staged", kind="finding") == Tag.closed(
        "STATUS", "staged"
    )
    with pytest.raises(BadInput):
        TodoHandler(hub=Hub(store=store)).put(
            text="Synthetic task", tags=["STATUS:staged"]
        )


def test_staged_status_is_finding_scoped_not_union(store):
    # `staged` lives in finding's own STATUS subset, not the shared
    # union: kinds that accept every union value still refuse it.
    with pytest.raises(BadInput):
        Tag.parse_strict("STATUS:staged", kind="plan")
    with pytest.raises(BadInput):
        Tag.parse_strict("STATUS:staged", kind="todo")
    # Unscoped parses (cross-kind tag filters run kind=None) accept the
    # kind-scoped spelling — writes always carry a concrete kind, so the
    # escape can't land the tag on a non-finding.
    assert Tag.parse_strict("STATUS:staged") == Tag.closed("STATUS", "staged")
    # The bare flag stays a legal open tag — no closed-value collision
    # (the whole point of the draft → staged rename).
    assert Tag.parse_strict("staged").value == "staged"
    assert Tag.parse_strict("draft").value == "draft"


def test_finding_status_filter_uses_finding_vocabulary(store):
    handler = FindingHandler(hub=Hub(store=store))
    with pytest.raises(BadInput):
        handler.search(status="open")


def test_tag_refuses_staged_on_a_finding_without_draft_source(store):
    _paper(store)
    handler = FindingHandler(hub=Hub(store=store))
    response = handler.put(
        title="Ordinary chase finding",
        body="A claim grounded in the corpus.",
        cited_in="synthetic26",
    )
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None
    finding_id = int(match.group(1))
    assert "draft_source" not in _meta(store, finding_id)
    with pytest.raises(BadInput, match="draft_source"):
        handler.tag(id=finding_id, add=["STATUS:staged"])


@pytest.mark.parametrize("status", ["tracing", "established"])
def test_tag_refuses_lifecycle_jump_on_anchorless_staged_finding(store, status):
    # A URL-sourced staged finding has no derived-from anchor: a hand-set
    # lifecycle tag would skip the quote re-anchor promotion performs.
    finding_id = _put(store)
    handler = FindingHandler(hub=Hub(store=store))
    with pytest.raises(BadInput):
        handler.tag(id=finding_id, add=[f"STATUS:{status}"])


def test_tag_allows_tracing_on_staged_finding_with_web_anchor(store):
    # A web-source staged finding carries a derived-from frontier, so a
    # hand move to tracing leaves the chase a real starting point.
    store.insert_ref(
        kind="web",
        slug="synthetic-lanterns",
        title="Synthetic source",
        meta={"url": URL},
    )
    finding_id = _put(store, source="web:synthetic-lanterns")
    handler = FindingHandler(hub=Hub(store=store))
    handler.tag(id=finding_id, add=["STATUS:tracing"])
    assert _status(store, finding_id) == {"tracing"}


@pytest.mark.parametrize(
    "extra",
    [
        {"hypothesis": True},
        {"supporters": []},
        {"wants": []},
        {"tags": ["STATUS:staged", "STATUS:tracing"]},
    ],
    ids=["hypothesis", "supporters", "wants", "second-status"],
)
def test_staged_creation_rejects_mixed_modes(store, extra):
    kwargs: dict[str, Any] = {
        "title": "Synthetic mixed mode",
        "body": QUOTE,
        "cited_in": URL,
        "tags": ["STATUS:staged"],
    }
    kwargs.update(extra)
    with pytest.raises(BadInput):
        FindingHandler(hub=Hub(store=store)).put(**kwargs)


@pytest.mark.parametrize("missing", ["verdict", "verifier", "checked_at"])
def test_staged_rejects_incomplete_verifier_metadata(store, missing):
    meta = {"quote": QUOTE, **VERDICT}
    del meta[missing]
    with pytest.raises(BadInput, match="verdict.*verifier.*checked_at"):
        FindingHandler(hub=Hub(store=store)).put(
            title="Synthetic incomplete verdict",
            body=QUOTE,
            cited_in=URL,
            tags=["STATUS:staged"],
            meta=meta,
        )


def test_staged_without_explicit_quote_uses_body_as_source_quotation(store):
    response = FindingHandler(hub=Hub(store=store)).put(
        title="Synthetic body quotation",
        body=QUOTE,
        cited_in=URL,
        tags=["STATUS:staged"],
    )
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None
    finding_id = int(match.group(1))
    assert _meta(store, finding_id)["draft_source"]["quote"] == QUOTE
    assert _status(store, finding_id) == {"staged"}
    paper_id = _paper(store)
    assert promote_drafts_for_paper(store, paper_id) == 1
    assert _status(store, finding_id) == {"tracing"}


def test_staged_rejects_chunk_level_web_source(store):
    store.put_cache_entry(
        kind="web",
        slug="synthetic-lanterns",
        title="Synthetic source",
        body_blocks=[ChunkInsert(ord=0, text=QUOTE, meta={})],
        provider="web",
        request_hash="synthetic-lanterns-request",
        ttl_seconds=None,
        cache_meta={"url": URL},
    )
    with pytest.raises(BadInput):
        _put(store, source="web:synthetic-lanterns~0")


def test_promotion_ignores_quotation_present_only_in_retired_chunk(store):
    finding_id = _put(store)
    paper_id = _paper(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = 0",
            (paper_id,),
        )
    assert promote_drafts_for_paper(store, paper_id) == 0
    assert _status(store, finding_id) == {"staged"}
    assert not store.links_for(finding_id, direction="out", relation="derived-from")


def test_promotion_anchors_the_matching_passage_not_the_first_chunk(store):
    finding_id = _put(store)
    paper_id = _paper(store, quote="Synthetic opening section about silver umbrellas.")
    store.chunks.insert_chunks(paper_id, [ChunkInsert(ord=1, text=QUOTE, meta={})])
    assert promote_drafts_for_paper(store, paper_id) == 1
    chain = _meta(store, finding_id)["chain"]
    assert len(chain) == 1 and chain[0]["ord"] == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (chain[0]["chunk_id"],)
        ).fetchone()
    assert row is not None and row[0] == QUOTE
    links = store.links_for(finding_id, direction="out", relation="derived-from")
    assert len(links) == 1 and links[0].dst_chunk_id == chain[0]["chunk_id"]


def test_wont_do_status_tags_on_and_off_a_finding(store):
    # Prod findings carry STATUS:won't-do from the unrestricted-vocab
    # era (gr520907) — the finding subset must accept it or removing
    # that legacy tag refuses at parse.
    finding_id = _put(store)
    handler = FindingHandler(hub=Hub(store=store))
    handler.tag(id=finding_id, add=["STATUS:won't-do"])
    assert _status(store, finding_id) == {"won't-do"}
    assert "Synthetic lantern finding" in handler.search(status="won't-do").body
    handler.tag(id=finding_id, remove=["STATUS:won't-do"])
    assert _status(store, finding_id) == set()


def test_operational_error_propagates_from_ingest_promotion(store):
    # A transient DB outage mid-promotion is not a promotion miss: the
    # watcher retries the ingest (idempotent — the committed paper is
    # re-probed) only when the fault reaches it.
    staged = PaperToWrite(
        title="Synthetic lantern study",
        authors=[{"name": "Synthetic, Ada"}],
        year=2026,
        provider="crossref",
        paper_id="synthetic-lantern-import",
        pub_id=f"doi:{DOI}",
        cite_key_prefix="synthetic26",
        doi=DOI,
        chunks=[ChunkToWrite(ord=0, chunk_kind="paragraph", text=QUOTE)],
    )
    with (
        patch("precis.ingest.pipeline.fetch_paper_by_doi", return_value=staged),
        patch(
            "precis.finding_drafts.promote_drafts_for_paper",
            side_effect=psycopg.OperationalError("synthetic outage"),
        ),
        pytest.raises(psycopg.OperationalError),
    ):
        precis_add(DoiInput(doi=DOI), store=store)


def test_operational_error_propagates_from_reextract_promotion(store, tmp_path):
    # The reextract job's post-swap promotion re-raises a transient DB
    # fault so the job retry re-runs it — a logged swallow would mark
    # the run finished with the staged drafts still waiting.
    from precis.workers.job_types import paper_reextract
    from tests.test_paper_reextract import _fake_body, _pdf, _seed, _write_pdf

    pdf = tmp_path / "op.pdf"
    _write_pdf(pdf, text="Title of rex-outage — body text.")
    rid = _seed(store, "rex-outage")
    sha = f"{rid:064d}"
    _pdf(store, rid, sha, storage_path=str(pdf))
    with (
        patch(
            "precis.finding_drafts.promote_drafts_for_paper",
            side_effect=psycopg.OperationalError("synthetic outage"),
        ),
        pytest.raises(psycopg.OperationalError),
    ):
        paper_reextract.reextract(
            store,
            rid,
            corpus_dirs=(tmp_path,),
            extractor=lambda p, c: _fake_body(2),
        )


def test_operational_error_propagates_from_create_promotion(store):
    # The create path's promote_drafts_for_source call gets the same
    # treatment: the staged finding exists, but the caller's retry
    # must see the outage, not a logged swallow.
    with (
        patch(
            "precis.finding_drafts.promote_drafts_for_source",
            side_effect=psycopg.OperationalError("synthetic outage"),
        ),
        pytest.raises(psycopg.OperationalError),
    ):
        _put(store)


def test_staged_put_reports_own_status_not_sibling_promotion(store):
    # promote_drafts_for_source returns a count over EVERY staged draft
    # on the source — a sibling's promotion must not move this finding's
    # reported status off staged when its own quote still misses.
    first = _put(store)
    paper_id = _paper(store)
    response = FindingHandler(hub=Hub(store=store)).put(
        title="Synthetic non-matching sibling",
        body="A different synthetic claim so the deterministic pub_id differs.",
        cited_in=URL,
        tags=["STATUS:staged"],
        meta={
            "quote": "An entirely different synthetic quotation about blue umbrellas.",
            **VERDICT,
        },
    )
    assert "STATUS:staged" in response.body
    assert "STATUS:tracing" not in response.body
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None
    second = int(match.group(1))
    # The sibling did promote on this same pass; this finding did not.
    _assert_promoted(store, first, paper_id)
    assert _status(store, second) == {"staged"}
    assert _meta(store, second)["staged_reanchor"]["reason"] == "no_quote_match"


def test_staged_put_promotes_on_noncanonical_stored_source_url(store):
    # Paper meta holds the raw URL (case, tracking params); the create
    # path must canonicalize the stored side too — verbatim equality
    # would miss it and leave the finding staged forever.
    paper_id = _paper(store, url="HTTPS://EXAMPLE.org/synthetic-lanterns?utm_source=x")
    response = FindingHandler(hub=Hub(store=store)).put(
        title="Synthetic lantern finding",
        body="Synthetic lantern glow persists during controlled cooling.",
        cited_in=URL,
        tags=["STATUS:staged"],
        meta={"quote": QUOTE, **VERDICT},
    )
    assert "STATUS:tracing" in response.body
    match = re.search(r"\bid=(\d+)", response.body)
    assert match is not None
    _assert_promoted(store, int(match.group(1)), paper_id)


def test_chunk_reread_miss_records_chunk_missing_note(store):
    # _match_quote names a chunk the locked re-read then can't see
    # (a bogus chunk_id — the retired/foreign-ref cases end at the same
    # None) — the miss is recorded, not a silent skip.
    finding_id = _put(store)
    paper_id = _paper(store)
    with patch("precis.finding_drafts._match_quote", return_value=-1):
        assert promote_drafts_for_paper(store, paper_id) == 0
    assert _status(store, finding_id) == {"staged"}
    note = _meta(store, finding_id)["staged_reanchor"]
    assert note["paper_id"] == paper_id
    assert note["reason"] == "chunk_missing"
    assert note["at"]
