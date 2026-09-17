"""Contract tests for :mod:`precis.utils.ref_hybrid` and its call sites.

Regression cover for the 2026-08-22 fix: ref-level search was
``search_refs_lexical`` only — a ``websearch_to_tsquery`` AND over
``refs.title`` — so one query word absent from the title zeroed the result and
there was no semantic leg at all, even though ~100% of body chunks are
embedded.

**What is and is not testable here.** ``MockEmbedder`` hashes text to a vector
(SHA-256 → unit L2), so semantically related sentences get *unrelated* vectors.
A "paraphrase finds the claim" test would therefore pass or fail for reasons
having nothing to do with the change. These tests cover what is deterministic —
that the block leg is wired and contributes, that the notation leg works, that
``mode=`` is honoured, and that the chunk-less kinds are not regressed. True
paraphrase recall is verified against prod with the real embedder.
"""

from __future__ import annotations

from typing import Any

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.finding import FindingHandler
from precis.store.types import ChunkInsert
from precis.utils.ref_hybrid import fused_ref_hits


def _hub(store) -> Hub:
    return Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))


def _seed_finding(store, *, title: str, body: str) -> int:
    """A finding whose title and ``finding_body`` chunk may differ.

    They diverge on purpose in these tests: that is how we prove which leg
    produced a hit.
    """
    ref = store.insert_ref(kind="finding", slug=None, title=title, meta={})
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "finding_body"})],
    )
    return ref.id


class TestBlockLegContributes:
    def test_word_absent_from_title_still_matches_via_body_chunk(self, store) -> None:
        """The regression: title-AND alone returns nothing here.

        Every query term is present in the body chunk but one ("spectrum") is
        absent from the title, which is exactly the shape that made
        ``nanobud transmission spectrum Fermi energy`` return zero rows on
        prod against a hub that plainly discussed it.
        """
        ref_id = _seed_finding(
            store,
            title="Nanobud junctions suppress conductance above the Fermi energy",
            body=(
                "Nanobud junctions suppress conductance above the Fermi energy, "
                "and the transmission spectrum retains a plateau below it."
            ),
        )
        q = "nanobud transmission spectrum Fermi energy"

        # The old behaviour, still reachable directly — proves the test is
        # exercising a real gap rather than a tautology.
        assert store.search_refs_lexical(q=q, kind="finding", limit=10) == []

        hits = fused_ref_hits(
            store,
            MockEmbedder(dim=store.embedding_dim()),
            q=q,
            kind="finding",
            limit=10,
            chunk_kinds=["finding_body"],
        )
        assert ref_id in [r.id for r in hits]


class TestNotationLeg:
    def test_ascii_query_matches_canonical_notation(self, store) -> None:
        """``kOhm`` finds a claim written ``kΩ`` after corpus normalization."""
        ref_id = _seed_finding(
            store,
            title="Grain-boundary resistance reaches 40 kΩ between adjacent grains",
            body="Grain-boundary resistance reaches 40 kΩ between adjacent grains.",
        )
        q = "40 kOhm grain-boundary resistance"

        # Without the notation leg this is unfindable: the corpus holds Ω and
        # the query holds "kOhm", and FTS matches tokens, not meanings.
        assert store.search_refs_lexical(q=q, kind="finding", limit=10) == []

        hits = fused_ref_hits(
            store,
            None,  # lexical-only: isolates the notation leg from the embedder
            q=q,
            kind="finding",
            limit=10,
            chunk_kinds=["finding_body"],
        )
        assert ref_id in [r.id for r in hits]

    def test_query_needing_no_normalization_is_not_double_counted(self, store) -> None:
        """An already-canonical query runs two legs, not four, and still hits."""
        ref_id = _seed_finding(
            store,
            title="Contact resistance is 40 kΩ",
            body="Contact resistance is 40 kΩ.",
        )
        hits = fused_ref_hits(
            store, None, q="contact resistance", kind="finding", limit=10
        )
        assert [r.id for r in hits] == [ref_id]


class TestModeIsHonoured:
    def test_lexical_mode_skips_the_embed(self, store) -> None:
        """``mode='lexical'`` must not call the embedder.

        Before this change ``mode=`` was accepted and ignored on these paths,
        so a caller asking for a deterministic keyword pass silently got one
        anyway — right answer, wrong reason. Now it is load-bearing.
        """

        class _SpyEmbedder(MockEmbedder):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self.calls = 0

            def embed_one(self, text: str) -> list[float]:
                self.calls += 1
                return super().embed_one(text)

        spy = _SpyEmbedder(dim=store.embedding_dim())
        _seed_finding(store, title="A claim about tubes", body="A claim about tubes.")

        fused_ref_hits(
            store, spy, q="claim about tubes", kind="finding", limit=5, mode="lexical"
        )
        assert spy.calls == 0

        fused_ref_hits(
            store, spy, q="claim about tubes", kind="finding", limit=5, mode="hybrid"
        )
        assert spy.calls == 1


class TestSemanticModeGatesTitleLexicalLeg:
    """gr343635: ``mode='semantic'`` on a ref-level kind (``job``, ``todo``,
    ``quest`` — ``NumericRefHandler`` kinds without body-chunk search) used
    to still let pure title-keyword matches through, because the
    title-lexical leg (and its notation-canonical twin) fused in
    unconditionally regardless of ``mode``. Only the block leg is
    ``mode``-aware.
    """

    def test_title_keyword_only_ref_excluded_under_semantic_mode(self, store) -> None:
        e = MockEmbedder(dim=store.embedding_dim())
        marker = "gr343635uniq"
        query = f"{marker} phrase used as the semantic anchor"

        # Title contains every query keyword, verbatim — but has no body
        # chunk at all, so there is zero semantic signal to find it by.
        title_only = store.insert_ref(kind="todo", slug=None, title=query, meta={})

        # Unrelated title; a body chunk whose text IS the query, embedded
        # with the same (deterministic-hash) embedder, so its distance to
        # the query vector is exactly 0 — well inside SEMANTIC_DISTANCE_FLOOR.
        semantic_match = store.insert_ref(
            kind="todo", slug=None, title="unrelated title text", meta={}
        )
        store.chunks.insert_chunks(
            semantic_match.id,
            [ChunkInsert(ord=0, text=query, embedding=e.embed_one(query))],
        )

        # Default hybrid: the title-lexical leg still surfaces the
        # keyword-only match — no regression on the default path.
        hybrid_ids = {
            r.id for r in fused_ref_hits(store, e, q=query, kind="todo", limit=10)
        }
        assert title_only.id in hybrid_ids
        assert semantic_match.id in hybrid_ids

        # mode='semantic': the title-keyword-only ref is excluded outright;
        # the semantically-close ref survives.
        semantic_ids = {
            r.id
            for r in fused_ref_hits(
                store, e, q=query, kind="todo", limit=10, mode="semantic"
            )
        }
        assert title_only.id not in semantic_ids
        assert semantic_match.id in semantic_ids

        # mode='lexical': title-keyword matching still works.
        lexical_ids = {
            r.id
            for r in fused_ref_hits(
                store, e, q=query, kind="todo", limit=10, mode="lexical"
            )
        }
        assert title_only.id in lexical_ids


class TestNoRegressionForChunklessKinds:
    def test_todo_without_body_chunks_is_still_found_by_title(self, store) -> None:
        """The guard on the additive design.

        ``todo`` has ~2.7k refs and ~37 body chunks on prod. If the block leg
        had *replaced* the title leg rather than fusing with it, todo search
        would have quietly returned almost nothing.
        """
        ref = store.insert_ref(
            kind="todo", slug=None, title="Rotate the agent_rw password", meta={}
        )
        hits = fused_ref_hits(
            store,
            MockEmbedder(dim=store.embedding_dim()),
            q="rotate password",
            kind="todo",
            limit=10,
        )
        assert ref.id in [r.id for r in hits]

    def test_missing_embedder_degrades_without_raising(self, store) -> None:
        """An embedder outage costs recall, not a 500 (search_embed_guard)."""
        ref_id = _seed_finding(
            store, title="Bandgap opens to 0.549 eV", body="Bandgap opens to 0.549 eV."
        )
        hits = fused_ref_hits(store, None, q="bandgap opens", kind="finding", limit=10)
        assert ref_id in [r.id for r in hits]


class TestFindingHandlerWiring:
    def test_handler_search_uses_the_fused_path(self, store) -> None:
        """End-to-end through the handler, not just the helper."""
        _seed_finding(
            store,
            title="Defect concentration drives the tube toward metallic behaviour",
            body=(
                "Defect concentration drives the tube toward metallic behaviour, "
                "with differential conductivity confirming continuous control."
            ),
        )
        handler = FindingHandler(hub=_hub(store))
        resp = handler.search(
            q="defect differential conductivity metallic", status="*", page_size=5
        )
        assert "no finding matches" not in resp.body
