"""``get(kind='draft', id=<slug>, view='citations')`` — the draft-citation
lifecycle view (to-fetch / re-ground / promote / done).

Covers the view's acceptance criteria:

1. A stub ``[pa]``, a fetched ``[pc]``, and a ``[fi]`` cite each land in
   to-fetch / to-promote / done respectively.
2. Flipping a stub paper to non-zero block-count moves its cite OUT of
   to-fetch, with no link edited.
3. Zero LLM calls, zero writes.
4. Empty-draft and ``[fi]``-only-draft shapes.
5. gr180155's evidence-demand half: an un-fetched evidence paper of a
   cited ``[fi]`` claim hub surfaces as its own to-fetch row (and in
   ``draft_fetch_ref_ids``), fully-fetched evidence adds nothing, and a
   paper cited both directly and as evidence appears once (direct wins).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from tests.workers._helpers import seed_chunk, seed_ref


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Proj").id


def _new_draft(store, draft: DraftHandler, slug: str, title: str = "Dossier"):
    proj = _proj(store)
    draft.put(id=slug, title=title, project=proj)
    return store.get_ref(kind="draft", id=slug)


def _add_para(store, draft: DraftHandler, slug: str, ref, text: str):
    """Append one paragraph chunk with ``text`` to the end of the draft;
    return the new chunk (``DraftChunk``, carrying ``.dc``)."""
    tail = store.drafts.reading_order(ref.id)[-1]
    draft.put(id=slug, chunk_kind="paragraph", text=text, at={"after": tail.dc})
    return store.drafts.reading_order(ref.id)[-1]


def _section(body: str, heading: str) -> str:
    """The text of one ``## HEADING (...)`` section, up to the next ``##``."""
    marker = f"## {heading}"
    start = body.index(marker)
    rest = body[start + len(marker) :]
    nxt = rest.find("\n## ")
    return rest[:nxt] if nxt != -1 else rest


def _row_counts(store) -> tuple[int, int, int]:
    """(refs, chunks, links) row counts — a before/after zero-write guard."""
    with store.pool.connection() as conn:
        refs = conn.execute("SELECT count(*) FROM refs").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        links = conn.execute("SELECT count(*) FROM links").fetchone()[0]
    return int(refs), int(chunks), int(links)


# ---------------------------------------------------------------------------
# 1. Partition rule — stub [pa] / fetched [pc] / [fi] land in the right bucket
# ---------------------------------------------------------------------------


class TestPartitionRule:
    def test_stub_pa_fetched_pc_and_fi_partition_correctly(
        self, store, draft: DraftHandler
    ) -> None:
        stub = seed_ref(store, title="Stub Landmark", kind="paper")
        store.insert_ref_identifiers(stub, [("doi", "10.1000/stub-landmark", "manual")])

        fetched = seed_ref(store, title="Fetched Paper", kind="paper")
        chunk_id = seed_chunk(store, ref_id=fetched, text="grounding passage")

        finding = seed_ref(store, title="A Claim", kind="finding")

        ref = _new_draft(store, draft, "cites-basic")
        row = _add_para(
            store,
            draft,
            "cites-basic",
            ref,
            f"A landmark result [pa{stub}]. A grounded claim [pc{chunk_id}]. "
            f"A settled claim [fi{finding}].",
        )

        resp = draft.get(id="cites-basic", view="citations")
        body = resp.body

        to_fetch = _section(body, "TO FETCH (1)")
        to_promote = _section(body, "TO PROMOTE (1)")
        done = _section(body, "DONE (1)")
        to_re_ground = _section(body, "TO RE-GROUND (0)")

        # to-fetch: the stub, with its row fields present.
        assert f"pa{stub}" in to_fetch
        assert f"[pa{stub}]" in to_fetch
        assert "Stub Landmark" in to_fetch
        assert "10.1000/stub-landmark" in to_fetch
        assert row.dc in to_fetch
        assert "fetch" in to_fetch

        # to-promote: the fetched pc cite.
        assert f"pa{fetched}" in to_promote
        assert f"[pc{chunk_id}]" in to_promote
        assert "Fetched Paper" in to_promote
        assert "promote" in to_promote

        # done: the fi cite.
        assert f"fi{finding}" in done
        assert f"[fi{finding}]" in done
        assert "A Claim" in done

        # nothing bled into to-re-ground.
        assert "pa" not in to_re_ground.replace("re-ground", "")

    def test_pub_id_placeholder_lands_in_done(self, store, draft: DraftHandler) -> None:
        from precis.identity import make_pub_id, make_taproot_hub_paper_id

        finding = seed_ref(store, title="Hub Claim", kind="finding")
        pub_id = make_pub_id(make_taproot_hub_paper_id("some claim text", {}))
        store.insert_ref_identifiers(finding, [("pub_id", pub_id, "manual")])

        ref = _new_draft(store, draft, "cites-pubid")
        _add_para(store, draft, "cites-pubid", ref, f"An early cite [{pub_id}].")

        resp = draft.get(id="cites-pubid", view="citations")
        done = _section(resp.body, "DONE (1)")
        assert f"fi{finding}" in done
        assert f"[{pub_id}]" in done


# ---------------------------------------------------------------------------
# 2. to-fetch is exactly cited ∧ block-count 0 — re-derive after a flip
# ---------------------------------------------------------------------------


class TestReDeriveAfterFetch:
    def test_pa_stub_moves_to_re_ground_after_first_block(
        self, store, draft: DraftHandler
    ) -> None:
        paper = seed_ref(store, title="Landmark", kind="paper")
        ref = _new_draft(store, draft, "cites-flip-pa")
        _add_para(store, draft, "cites-flip-pa", ref, f"See [pa{paper}].")

        before = draft.get(id="cites-flip-pa", view="citations").body
        assert f"pa{paper}" in _section(before, "TO FETCH (1)")
        assert _section(before, "TO RE-GROUND (0)").strip().startswith("(none)")

        links_before = store.links_for(paper, direction="out") + store.links_for(
            paper, direction="in"
        )

        # "ingest" — the paper acquires its first body chunk.
        seed_chunk(store, ref_id=paper, text="now fetched")

        after = draft.get(id="cites-flip-pa", view="citations").body
        assert _section(after, "TO FETCH (0)").strip().startswith("(none)")
        assert f"pa{paper}" in _section(after, "TO RE-GROUND (1)")

        links_after = store.links_for(paper, direction="out") + store.links_for(
            paper, direction="in"
        )
        assert links_before == links_after  # no link edited

    def test_pc_source_moves_to_to_promote_once_grounded(
        self, store, draft: DraftHandler
    ) -> None:
        paper = seed_ref(store, title="Landmark Two", kind="paper")
        ref = _new_draft(store, draft, "cites-flip-pc")
        chunk = _add_para(store, draft, "cites-flip-pc", ref, f"See [pa{paper}].")

        before = draft.get(id="cites-flip-pc", view="citations").body
        assert f"pa{paper}" in _section(before, "TO FETCH (1)")

        # ingest + re-ground: the paper gets its first chunk and the draft
        # prose is updated to cite that specific passage instead of the
        # whole paper (what the dependent [pa]-arm proposal will automate;
        # here just constructing the post-re-ground state).
        pc_id = seed_chunk(store, ref_id=paper, text="the grounding passage")
        draft.edit(id=chunk.dc, text=f"See [pc{pc_id}].")

        after = draft.get(id="cites-flip-pc", view="citations").body
        assert _section(after, "TO FETCH (0)").strip().startswith("(none)")
        assert f"pa{paper}" in _section(after, "TO PROMOTE (1)")


# ---------------------------------------------------------------------------
# 3. Zero LLM calls, zero writes
# ---------------------------------------------------------------------------


class TestNoLLMNoWrites:
    def test_classifies_with_canon_stubbed_to_raise_and_writes_nothing(
        self, store, draft: DraftHandler, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.taproot import canon

        def _boom(*_a, **_k):
            raise AssertionError("view='citations' must never call the LLM cascade")

        monkeypatch.setattr(canon, "extract_claim", _boom)
        monkeypatch.setattr(canon, "dedup_judge", _boom)
        monkeypatch.setattr(canon, "block", _boom)

        stub = seed_ref(store, title="Stub", kind="paper")
        fetched = seed_ref(store, title="Fetched", kind="paper")
        chunk_id = seed_chunk(store, ref_id=fetched, text="body")
        finding = seed_ref(store, title="Claim", kind="finding")

        ref = _new_draft(store, draft, "cites-nowrite")
        _add_para(
            store,
            draft,
            "cites-nowrite",
            ref,
            f"[pa{stub}] [pc{chunk_id}] [fi{finding}]",
        )

        before = _row_counts(store)
        resp = draft.get(id="cites-nowrite", view="citations")
        after = _row_counts(store)

        assert before == after  # zero writes
        assert f"pa{stub}" in _section(resp.body, "TO FETCH (1)")
        assert f"pa{fetched}" in _section(resp.body, "TO PROMOTE (1)")
        assert f"fi{finding}" in _section(resp.body, "DONE (1)")


# ---------------------------------------------------------------------------
# 4. Empty-draft and [fi]-only-draft shapes
# ---------------------------------------------------------------------------


class TestEdgeShapes:
    def test_empty_draft_returns_all_four_partitions_empty(
        self, store, draft: DraftHandler
    ) -> None:
        _new_draft(store, draft, "cites-empty")
        resp = draft.get(id="cites-empty", view="citations")
        body = resp.body
        for heading in (
            "TO FETCH (0)",
            "TO RE-GROUND (0)",
            "TO PROMOTE (0)",
            "DONE (0)",
        ):
            assert heading in body
            assert _section(body, heading).strip().startswith("(none)")

    def test_fi_only_draft_lands_everything_under_done(
        self, store, draft: DraftHandler
    ) -> None:
        f1 = seed_ref(store, title="Claim One", kind="finding")
        f2 = seed_ref(store, title="Claim Two", kind="finding")

        ref = _new_draft(store, draft, "cites-fi-only")
        _add_para(
            store, draft, "cites-fi-only", ref, f"Settled: [fi{f1}] and [fi{f2}]."
        )

        resp = draft.get(id="cites-fi-only", view="citations")
        body = resp.body
        for heading in ("TO FETCH (0)", "TO RE-GROUND (0)", "TO PROMOTE (0)"):
            assert _section(body, heading).strip().startswith("(none)")
        done = _section(body, "DONE (2)")
        assert f"fi{f1}" in done
        assert f"fi{f2}" in done


# ---------------------------------------------------------------------------
# 5. draft_fetch_ref_ids — the /drive?cited_by=<draft> worklist derivation
# ---------------------------------------------------------------------------


class TestDraftFetchRefIds:
    def test_returns_exactly_the_cited_zero_block_papers(
        self, store, draft: DraftHandler
    ) -> None:
        from precis.handlers._citations_view import draft_fetch_ref_ids

        stub_a = seed_ref(store, title="Stub A", kind="paper")
        stub_b = seed_ref(store, title="Stub B", kind="paper")
        fetched = seed_ref(store, title="Fetched", kind="paper")
        chunk_id = seed_chunk(store, ref_id=fetched, text="grounding passage")
        finding = seed_ref(store, title="Settled", kind="finding")
        _uncited_stub = seed_ref(store, title="Not Cited", kind="paper")

        ref = _new_draft(store, draft, "fetch-ids")
        _add_para(
            store,
            draft,
            "fetch-ids",
            ref,
            f"[pa{stub_a}] and again [pa{stub_a}], plus [pa{stub_b}], a fetched "
            f"[pc{chunk_id}], and a settled [fi{finding}].",
        )

        got = draft_fetch_ref_ids(store, ref)
        # exactly the two cited 0-block papers, deduped + sorted; the fetched
        # paper (has blocks), the finding, and the uncited stub are all absent.
        assert got == sorted([stub_a, stub_b])

    def test_empty_when_nothing_to_fetch(self, store, draft: DraftHandler) -> None:
        from precis.handlers._citations_view import draft_fetch_ref_ids

        fetched = seed_ref(store, title="Fetched", kind="paper")
        chunk_id = seed_chunk(store, ref_id=fetched, text="body")
        ref = _new_draft(store, draft, "fetch-ids-empty")
        _add_para(store, draft, "fetch-ids-empty", ref, f"All grounded [pc{chunk_id}].")

        assert draft_fetch_ref_ids(store, ref) == []


# ---------------------------------------------------------------------------
# 6. Evidence-demand half (gr180155) — a [fi] hub's un-fetched evidence
#    papers surface as their own to-fetch rows, the [fi] cite itself stays
#    done, and a paper cited both directly and as evidence appears once.
# ---------------------------------------------------------------------------


def _hub(store, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


class TestEvidenceDemand:
    def test_unfetched_evidence_paper_lands_in_to_fetch(
        self, store, draft: DraftHandler
    ) -> None:
        from precis.handlers._citations_view import draft_fetch_ref_ids

        hub = _hub(store, "A settled claim with unfetched evidence.")
        evidence_paper = seed_ref(store, title="Unfetched Evidence Paper", kind="paper")
        attach_evidence(
            store,
            hub_ref_id=hub,
            paper_ref_id=evidence_paper,
            role="corroborates",
            check_retraction=False,
        )

        ref = _new_draft(store, draft, "cites-evidence-demand")
        row = _add_para(
            store, draft, "cites-evidence-demand", ref, f"Settled: [fi{hub}]."
        )

        resp = draft.get(id="cites-evidence-demand", view="citations")
        body = resp.body
        to_fetch = _section(body, "TO FETCH (1)")
        done = _section(body, "DONE (1)")

        assert f"pa{evidence_paper}" in to_fetch
        assert "Unfetched Evidence Paper" in to_fetch
        assert f"fetch (evidence for fi{hub})" in to_fetch
        assert row.dc in to_fetch
        assert f"[fi{hub}]" in to_fetch  # the citing token, as written

        assert f"fi{hub}" in done
        assert f"[fi{hub}]" in done

        assert draft_fetch_ref_ids(store, ref) == [evidence_paper]

    def test_hub_with_fully_fetched_evidence_adds_no_rows(
        self, store, draft: DraftHandler
    ) -> None:
        from precis.handlers._citations_view import draft_fetch_ref_ids

        hub = _hub(store, "A settled claim with fully-fetched evidence.")
        evidence_paper = seed_ref(store, title="Fetched Evidence Paper", kind="paper")
        seed_chunk(store, ref_id=evidence_paper, text="grounding passage")
        attach_evidence(
            store,
            hub_ref_id=hub,
            paper_ref_id=evidence_paper,
            role="corroborates",
            check_retraction=False,
        )

        ref = _new_draft(store, draft, "cites-evidence-fetched")
        _add_para(store, draft, "cites-evidence-fetched", ref, f"Settled: [fi{hub}].")

        resp = draft.get(id="cites-evidence-fetched", view="citations")
        body = resp.body
        assert _section(body, "TO FETCH (0)").strip().startswith("(none)")
        assert f"fi{hub}" in _section(body, "DONE (1)")

        assert draft_fetch_ref_ids(store, ref) == []

    def test_evidence_paper_also_cited_directly_appears_once(
        self, store, draft: DraftHandler
    ) -> None:
        from precis.handlers._citations_view import draft_fetch_ref_ids

        hub = _hub(store, "A settled claim citing its own unfetched evidence.")
        shared = seed_ref(store, title="Shared Stub", kind="paper")
        attach_evidence(
            store,
            hub_ref_id=hub,
            paper_ref_id=shared,
            role="corroborates",
            check_retraction=False,
        )

        ref = _new_draft(store, draft, "cites-evidence-shared")
        _add_para(
            store,
            draft,
            "cites-evidence-shared",
            ref,
            f"Direct [pa{shared}] and settled [fi{hub}].",
        )

        resp = draft.get(id="cites-evidence-shared", view="citations")
        body = resp.body
        to_fetch = _section(body, "TO FETCH (1)")

        # Exactly one row for the shared paper — the direct [pa] one, plain
        # "fetch" action, no "evidence for" duplicate.
        assert to_fetch.count(f"[pa{shared}]") == 1
        assert "evidence for fi" not in to_fetch

        assert draft_fetch_ref_ids(store, ref) == [shared]
