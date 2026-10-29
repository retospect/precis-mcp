"""Tests for the paper-context-sentence pass (docs/backlog/
paper-context-sentence.md).

Pure lint/propose helpers run everywhere. The backfill-selection query and
the end-to-end pass run against real PG (the ``store`` fixture) with a fake
LLM client — no network, mirroring ``test_paper_glossary``/
``test_hub_tagline``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from precis.store import ChunkInsert
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers.context_sentence import (
    _PASS_VERSION,
    _STAMPED_IDS_TTL_S,
    META_FAILED_KEY,
    META_KEY,
    META_NO_ABSTRACT_KEY,
    NO_CONTEXT,
    TransientFailure,
    _claim,
    _generate_with_lint,
    _stamp_no_abstract,
    _stamped_ref_ids,
    backfill_candidate_ref_ids,
    is_decline,
    lead_body_block,
    lint_violation,
    propose_context_sentence,
    retrieve_body_block,
    run_context_sentence_pass,
    stamps_gate_claim,
    usable_context,
)
from tests.workers._helpers import make_mock_bge_m3, seed_chunk, seed_ref


class _FakeClient:
    """Records calls; returns one completion per call from a fixed
    sequence (repeating the last once exhausted) — like
    ``test_paper_glossary._FakeClient`` but sequenced, so the
    regenerate-once-then-drop tests can script "bad, then good" /
    "bad, then bad again"."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self._texts) - 1)
        return SimpleNamespace(text=self._texts[idx], total_tokens=5)


class _BoomClient:
    def complete(self, messages: list[dict[str, str]]) -> Any:
        raise RuntimeError("dispatch failed")


# ── lint (pure) ──────────────────────────────────────────────────────────


class TestLint:
    def test_clean_sentence_passes(self) -> None:
        assert (
            lint_violation(
                "Computational study; properties are DFT-calculated, no "
                "experimental synthesis or measurement."
            )
            is None
        )

    def test_empty_is_a_violation(self) -> None:
        assert lint_violation("") is not None
        assert lint_violation("   ") is not None

    def test_over_word_cap_is_a_violation(self) -> None:
        assert lint_violation(" ".join(["word"] * 36)) is not None

    def test_at_word_cap_is_clean(self) -> None:
        assert lint_violation(" ".join(["word"] * 35)) is None

    def test_blocklist_rejects_standalone_word_case_insensitive(self) -> None:
        assert lint_violation("This confirms the mechanism directly.") is not None
        assert lint_violation("Confirms the finding directly here.") is not None

    def test_blocklist_word_boundary_spares_a_longer_token(self) -> None:
        # "confirmsX" is not a match — word-boundary, not a substring scan.
        assert lint_violation("The confirmsX device passed a bench test.") is None

    def test_blocklist_covers_every_listed_word(self) -> None:
        for word in (
            "proof",
            "definitive",
            "confirms",
            "demonstrates",
            "proves",
            "establishes",
        ):
            assert lint_violation(f"This is {word} evidence for the claim.") is not None

    @pytest.mark.parametrize(
        "sentence",
        [
            "The authors report on the measurement of X.",
            "This paper reports the discovery of Y.",
            "We measured Z by AFM.",
            "Here we report Z.",
            "In this study, Z was measured.",
            "the study describes Z.",
            "NEC Corporation researchers observed helical microtubules by TEM.",
            "Researchers at NEC observed helical microtubules by TEM.",
            "Scientists measured Z by AFM.",
        ],
    )
    def test_attribution_preamble_is_rejected(self, sentence: str) -> None:
        reason = lint_violation(sentence)
        assert reason is not None
        assert reason.startswith("attribution preamble")

    def test_good_example_sentences_pass(self) -> None:
        assert (
            lint_violation(
                "Elastic properties and intrinsic strength were measured "
                "by atomic force microscopy."
            )
            is None
        )
        assert (
            lint_violation(
                "Helical microtubules of graphitic carbon were observed "
                "by transmission electron microscopy."
            )
            is None
        )

    def test_mid_sentence_the_authors_is_not_rejected(self) -> None:
        assert (
            lint_violation(
                "Elastic moduli were measured by AFM, following a protocol "
                "the authors adapted from earlier work."
            )
            is None
        )


# ── propose (mocked LLM) ──────────────────────────────────────────────────


class TestPropose:
    def test_propose_returns_cleaned_first_line(self) -> None:
        client = _FakeClient(
            ['"Computational study; DFT-only, no synthesis."\nAn aside line.']
        )
        out = propose_context_sentence(client, "Title", "Abstract")
        assert out == "Computational study; DFT-only, no synthesis."

    def test_propose_returns_none_on_empty_completion(self) -> None:
        assert propose_context_sentence(_FakeClient([""]), "T", "A") is None

    def test_propose_returns_none_on_dispatch_error(self) -> None:
        assert propose_context_sentence(_BoomClient(), "T", "A") is None


# ── regenerate-once-then-drop (mocked LLM) ────────────────────────────────


class TestGenerateWithLint:
    def test_clean_first_try_is_used_without_a_retry(self) -> None:
        client = _FakeClient(["Computational study; DFT-only, no synthesis."])
        out = _generate_with_lint(client, "T", "A")
        assert out == "Computational study; DFT-only, no synthesis."
        assert len(client.calls) == 1

    def test_violation_then_clean_uses_the_regenerated_sentence(self) -> None:
        client = _FakeClient(
            [
                "This proves the mechanism beyond doubt.",
                "Computational study; DFT-only, no synthesis.",
            ]
        )
        out = _generate_with_lint(client, "T", "A")
        assert out == "Computational study; DFT-only, no synthesis."
        assert len(client.calls) == 2

    def test_retry_prompt_carries_the_rejected_draft_and_the_rule(self) -> None:
        """A resend of the identical request invites the identical answer
        — prod's 36266 echoed the same blocklisted word twice (gr514381).
        The retry must say WHICH rule the dropped draft broke."""
        client = _FakeClient(
            [
                "This proves the mechanism beyond doubt.",
                "Computational study; DFT-only, no synthesis.",
            ]
        )
        _generate_with_lint(client, "T", "A")

        assert len(client.calls) == 2
        first_user = client.calls[0][-1]["content"]
        retry_user = client.calls[1][-1]["content"]
        assert retry_user != first_user
        assert "This proves the mechanism beyond doubt." in retry_user
        assert "claim-strength word 'proves'" in retry_user

    def test_dispatch_failure_retry_gets_no_hint(self) -> None:
        """An outage is not a verdict — there is no rejected draft to
        carry into the retry."""
        client = _FakeClient(["", "Computational study; DFT-only, no synthesis."])
        _generate_with_lint(client, "T", "A")
        assert client.calls[0][-1]["content"] == client.calls[1][-1]["content"]

    def test_violation_twice_drops_the_sentence(self) -> None:
        client = _FakeClient(
            [
                "This proves the mechanism beyond doubt.",
                "This also demonstrates a definitive result.",
            ]
        )
        assert _generate_with_lint(client, "T", "A") is None
        assert len(client.calls) == 2

    def test_dispatch_failure_twice_raises_transient(self) -> None:
        """No model reply is an outage, not a verdict — the caller must not
        converge the paper on it (prod stamped three papers failed during a
        rate-limit window on 2026-09-17)."""
        with pytest.raises(TransientFailure):
            _generate_with_lint(_BoomClient(), "T", "A")

    def test_dispatch_failure_then_clean_uses_the_retry(self) -> None:
        client = _FakeClient(["", "Computational study; DFT-only, no synthesis."])
        out = _generate_with_lint(client, "T", "A")
        assert out == "Computational study; DFT-only, no synthesis."
        assert len(client.calls) == 2

    def test_violation_then_dispatch_failure_is_transient(self) -> None:
        """One real violation plus an outage is not two violations."""
        client = _FakeClient(["This proves the mechanism beyond doubt.", ""])
        with pytest.raises(TransientFailure):
            _generate_with_lint(client, "T", "A")

    def test_decline_drops_the_paper_without_a_retry(self) -> None:
        """A decline means the supplied text wasn't this paper (prod wrote an
        atmospheric-chemistry sentence onto a C60 paper off a neighbouring
        article's reference list). Retrying would invite the model to invent
        one instead — the exact outcome the escape hatch exists to stop."""
        client = _FakeClient([NO_CONTEXT, "Computational study; DFT-only."])
        assert _generate_with_lint(client, "T", "A") is None
        assert len(client.calls) == 1

    def test_violation_then_decline_drops_the_paper(self) -> None:
        """The regenerate-once path can itself land on a decline: the first
        attempt trips the blocklist, and the retry — looking again at text
        that isn't this paper — declines instead. That must drop, not fall
        through to some third attempt or write the token."""
        client = _FakeClient(["This proves the mechanism beyond doubt.", NO_CONTEXT])
        assert _generate_with_lint(client, "T", "A") is None
        assert len(client.calls) == 2

    def test_a_decline_is_never_written_as_the_sentence(self) -> None:
        # NO_CONTEXT is short and carries no blocklisted word, so the lint
        # alone would happily pass it straight through into refs.meta.
        assert lint_violation(NO_CONTEXT) is None
        assert is_decline(NO_CONTEXT)
        assert is_decline("no_context.")
        assert not is_decline("Computational study; DFT-only.")


# ── backfill selection (real PG) ───────────────────────────────────────


#: Ordinary abstract prose — enough words beyond the title, mostly
#: lowercase, so ``usable_context`` lets the model be called. The pass
#: refuses to spend a call on a block thinner than this, so a fixture with
#: a placeholder body would exercise only the guard.
_ABSTRACT = (
    "We measured the elastic response of suspended monolayer films by "
    "indenting them with an atomic force microscope tip, and fitted the "
    "resulting force-displacement curves to a nonlinear elastic model to "
    "extract the second-order elastic stiffness and the breaking strength "
    "of the material under test."
)

#: The masthead shape the first-body-chunk fallback yields for ~94% of the
#: prod grounding cohort: the title, a byline, an affiliation, no prose.
_MASTHEAD = (
    "Helical microtubules of graphitic carbon\n\nSumio Iijima\n\n"
    "NEC Corporation, Fundamental Research Laboratories, 34 Miyukigaoka, "
    "Tsukuba, Ibaraki 305, Japan"
)


def _seed_paper(
    store: Any, title: str = "Paper", body: str = _ABSTRACT
) -> tuple[int, int]:
    ref_id = seed_ref(store, title=title)
    chunk_id = seed_chunk(store, ref_id=ref_id, text=body, ord=0)
    return ref_id, chunk_id


def _live_publish_row(store: Any, *, chunk_id: int, sentence: str) -> int:
    """A live (``state='reviewed'``) ``nanopub_publish`` row grounded at
    ``chunk_id``. Returns the hub's ``ref_id``."""
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    row = store.nanopub_create_publish_row(hub)
    ok = store.nanopub_approve(
        row.id,
        approved_title=sentence,
        claim_sha=f"{hub:064x}",
        aida_uri=f"http://purl.org/aida/csent{hub}",
        grounding={"passages": [{"chunk_id": chunk_id}]},
    )
    assert ok
    return hub


class TestUsableContext:
    """The guard that decides whether the block is worth a model call at
    all (gr346458) — every shape below is a real prod block."""

    def test_an_abstract_is_usable(self) -> None:
        assert usable_context("Measurement of the Elastic Properties", _ABSTRACT)

    def test_a_masthead_is_not(self) -> None:
        assert not usable_context("Helical microtubules of graphitic carbon", _MASTHEAD)

    def test_sup_markup_does_not_score_a_byline_as_prose(self) -> None:
        """``<sup>`` runs read as lowercase words and scored ref 42558's
        byline at 0.40 before the tags were stripped."""
        byline = (
            "**Electric Field Effect in Atomically Thin Carbon Films**\n\n"
            "K.S. Novoselov<sup>1</sup>, A.K. Geim<sup>1</sup>, "
            "S.V. Morozov<sup>2</sup>, D. Jiang<sup>1</sup>, "
            "Y. Zhang<sup>1</sup>, S.V. Dubonos<sup>2</sup>, "
            "I.V.Grigorieva<sup>1</sup>, A.A. Firsov<sup>2</sup>\n\n"
            "<sup>1</sup>Department of Physics, University of Manchester, "
            "M13 9PL, Manchester, UK\n\n"
            "<sup>2</sup>Institute for Microelectronics Technology, "
            "142432 Chernogolovka, Russia"
        )
        assert not usable_context(
            "Electric Field Effect in Atomically Thin Films", byline
        )

    def test_a_reference_list_tail_is_not_usable(self) -> None:
        """Ref 563's ord-0 chunk: someone else's bibliography plus an
        acknowledgement fragment."""
        refs = (
            "- 21. V. Randle, Scr. Mater. 54, 1011 (2006).\n"
            "- 22. S. R. Ortner, Acta Metall. Mater. 39, 341 (1991).\n"
            "- 23. Y. Z. Huang, J. M. Titchmarsh, Acta Mater. 54, 635 (2006).\n"
            "- 24. A.K. and G.J. wish to acknowledge funding received from the "
            "Engineering and Physical Sciences Research Council (U.K.)"
        )
        assert not usable_context("Measurement of the Elastic Properties", refs)

    def test_empty_and_title_only_blocks_are_not_usable(self) -> None:
        assert not usable_context("A paper", "")
        assert not usable_context("A paper", "A paper")

    def test_title_words_do_not_count_toward_the_floor(self) -> None:
        """A block that merely repeats a long title has nothing new to
        say about the method."""
        title = " ".join(f"word{i}" for i in range(40))
        assert not usable_context(title, title)


#: Method prose the method-seeking queries match — the shape retrieval
#: should surface in place of the masthead.
_METHODS = (
    "We synthesized the films by chemical vapour deposition and "
    "measured their elastic response by atomic force microscopy "
    "indentation, fitting each force-displacement curve to a nonlinear "
    "elastic model across independently prepared samples."
)

#: A references chunk whose text would lexically match the method
#: queries ("measured", "synthesized", "characterized") — excluded by
#: its section role, so a bibliography never becomes "the method".
_REFS_WITH_METHOD_TERMS = (
    "R1. Smith measured similar films, Acta Metall. 54, 635-640 (2006).\n"
    "R2. Jones characterized comparable samples, J. Mater. Sci. 8, 100-120 "
    "(1973).\n"
    "R3. Brown synthesized earlier films, Phil. Mag. 30, 55-70 (1990)."
)

#: The same shape with no section role — caught by citation-fragment
#: density instead.
_REFS_UNSECTIONED = (
    "21. Smith, J. measured similar films. Acta Metall. 12, 345-367 (1964).\n"
    "22. Jones, K. et al. J. Mater. Sci. 8, 100-120 (1973).\n"
    "23. Brown, L. Phil. Mag. 30, 55-70 (1990)."
)

#: Front-matter furniture that scrapes the ``usable_context`` word
#: floor: a licence run and a correspondence run are ordinary
#: lowercase prose over the 25-word floor, so only
#: ``_FRONT_MATTER_RE`` keeps them out of the lead block (gr514381
#: review — the masthead alone never exercised the regex).
_BYLINE = "Sumio Iijima and colleagues"
_CC_BY = (
    "© The Author(s) 2024. This article is licensed under a Creative "
    "Commons Attribution 4.0 International License, which permits use, "
    "sharing, adaptation, distribution and reproduction in any medium "
    "or format, as long as you give appropriate credit to the original "
    "author and the source."
)
_CORRESPONDENCE = (
    "Correspondence and requests for materials should be addressed to "
    "the corresponding author at the department listed in the author "
    "information section of this published research article."
)

#: "Laboratory measurements of ..." is content, not an affiliation
#: line — the ``_FRONT_MATTER_RE`` affiliation alternatives fire only
#: with a comma or "of" directly after the word (gr514381 review).
_LAB_PROSE = (
    "Laboratory measurements of the elastic response used atomic force "
    "microscopy indentation, fitting each force-displacement curve to a "
    "nonlinear elastic model across independently prepared samples in "
    "two separate measurement campaigns."
)


def _seed_body(
    store: Any, title: str, pieces: list[tuple[str, list[str] | None]]
) -> tuple[int, list[int]]:
    """Seed one ref + one chunk per ``(text, section_path)`` piece;
    return ``(ref_id, chunk_ids)`` in ord order."""
    ref_id = seed_ref(store, title=title)
    rows = [
        ChunkInsert(ord=i, text=t, meta={"section_path": sp} if sp else {})
        for i, (t, sp) in enumerate(pieces)
    ]
    return ref_id, [c.id for c in store.chunks.insert_chunks(ref_id, rows)]


class TestBodyRetrieval:
    """With no ``card_abstract``/meta abstract, the block is the top-k
    same-paper body passages the in-paper fused search ranks first —
    never position ord 0, never a hearsay-role chunk (spec AC)."""

    def test_block_is_the_method_passage_not_the_masthead(self, store: Any) -> None:
        ref_id, ids = _seed_body(
            store,
            "Helical microtubules of graphitic carbon",
            [
                (_MASTHEAD, None),
                (_METHODS, ["Experimental"]),
                (_REFS_WITH_METHOD_TERMS, ["References"]),
            ],
        )
        block, chunk_ids = retrieve_body_block(store, embedder=None, ref_id=ref_id)
        assert block == _METHODS
        assert chunk_ids == [ids[1]]

    def test_references_role_is_excluded_even_when_it_matches(self, store: Any) -> None:
        ref_id, ids = _seed_body(
            store,
            "A paper",
            [(_REFS_WITH_METHOD_TERMS, ["References"]), (_METHODS, ["Experimental"])],
        )
        block, chunk_ids = retrieve_body_block(store, embedder=None, ref_id=ref_id)
        assert block == _METHODS
        assert ids[0] not in chunk_ids

    def test_an_unsectioned_bibliography_is_excluded(self, store: Any) -> None:
        ref_id, ids = _seed_body(
            store, "A paper", [(_REFS_UNSECTIONED, None), (_METHODS, None)]
        )
        block, chunk_ids = retrieve_body_block(store, embedder=None, ref_id=ref_id)
        assert block == _METHODS
        assert ids[0] not in chunk_ids

    def test_masthead_and_bibliography_yield_no_block(self, store: Any) -> None:
        ref_id, _ = _seed_body(
            store,
            "Helical microtubules",
            [(_MASTHEAD, None), (_REFS_UNSECTIONED, None)],
        )
        block, chunk_ids = retrieve_body_block(store, embedder=None, ref_id=ref_id)
        assert block == ""
        assert chunk_ids == []

    def test_block_is_bounded_to_four_passages_and_1500_chars(self, store: Any) -> None:
        passage = (
            "We measured the setup with standard methods and characterized "
            "every sample under simulation details from the experimental "
            + "protocol "
            * 60
        )
        ref_id, _ = _seed_body(
            store, "A paper", [(f"{passage} batch {i}", None) for i in range(6)]
        )
        block, chunk_ids = retrieve_body_block(store, embedder=None, ref_id=ref_id)
        assert len(chunk_ids) <= 4
        assert len(block) <= 1500

    def test_semantic_leg_contributes_when_an_embedder_is_wired(
        self, store: Any
    ) -> None:
        """The passage's prose matches none of the method queries
        lexically — only its stored vector (identical to one query's
        embedding here) can surface it, so this exercises the semantic
        legs of the fused search, not just the lexical ones."""
        embedder = make_mock_bge_m3()
        prose = (
            "Geometry optimizations were performed with periodic boundary "
            "conditions and a plane wave basis set under tight convergence "
            "thresholds for every structure examined in this work."
        )
        ref_id = seed_ref(store, title="A computational paper")
        (chunk,) = store.chunks.insert_chunks(
            ref_id,
            [
                ChunkInsert(
                    ord=0,
                    text=prose,
                    embedding=embedder.embed_one("we calculate"),
                )
            ],
        )
        block, chunk_ids = retrieve_body_block(store, embedder=embedder, ref_id=ref_id)
        assert block == prose
        assert chunk_ids == [chunk.id]

    def test_claim_passes_empty_block_for_masthead_only_paper(self, store: Any) -> None:
        """``_claim`` resolves only card_abstract/meta abstract — the
        retrieval fallback lives in the pass."""
        ref_id, _ = _seed_paper(store, title="Helical microtubules", body=_MASTHEAD)
        with store.pool.connection() as conn:
            (row,) = _claim(conn, limit=5, ref_ids=[ref_id])
        assert row[2] == ""
        assert row[3] == []

    def test_pass_makes_no_model_call_on_masthead_only_paper(self, store: Any) -> None:
        ref_id, _ = _seed_paper(store, title="Helical microtubules", body=_MASTHEAD)
        client = _FakeClient(["should never be asked"])
        run_context_sentence_pass(store, client=client, ref_ids=[ref_id])
        assert client.calls == []


class TestPassOverBody:
    """End-to-end: the block the model sees is retrieved body prose —
    or the paper converges on the no-abstract stamp, never on a
    byline-derived sentence (gr346458 / spec AC)."""

    def test_sentence_is_written_from_body_prose(self, store: Any) -> None:
        ref_id, _ = _seed_body(
            store,
            "Helical microtubules of graphitic carbon",
            [(_MASTHEAD, None), (_METHODS, ["Experimental"])],
        )
        client = _FakeClient(["Films were synthesized by chemical vapour deposition."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        user = client.calls[0][-1]["content"]
        assert _METHODS in user
        assert "Iijima" not in user

    def test_card_abstract_still_wins_over_retrieval(self, store: Any) -> None:
        """Ref 207807's shape: a real card_abstract means the block is
        exactly the card — retrieval never runs."""
        ref_id = seed_ref(store, title="A DFT paper")
        seed_chunk(
            store,
            ref_id=ref_id,
            text=_ABSTRACT,
            ord=-1,
            chunk_kind="card_abstract",
        )
        seed_chunk(store, ref_id=ref_id, text=_METHODS, ord=0)
        client = _FakeClient(["Computational study; DFT-calculated properties only."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result["ok"] == 1
        user = client.calls[0][-1]["content"]
        assert _ABSTRACT in user
        assert _METHODS not in user

    def test_masthead_plus_bibliography_stamps_no_abstract(self, store: Any) -> None:
        """The spec's fixture: a paper whose only chunks are a byline and
        a bibliography still scores below usable_context's thresholds —
        stamped no-abstract, no model call, never a failed-from-masthead
        stamp."""
        ref_id, _ = _seed_body(
            store,
            "Helical microtubules",
            [(_MASTHEAD, None), (_REFS_UNSECTIONED, None)],
        )
        client = _FakeClient(["Never asked."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert client.calls == []
        assert result == {"claimed": 1, "ok": 0, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        stamp = meta.get(META_NO_ABSTRACT_KEY)
        assert isinstance(stamp, dict) and stamp.get("v") == _PASS_VERSION
        assert META_FAILED_KEY not in meta

    def test_front_matter_furniture_stamps_no_abstract_without_a_call(
        self, store: Any
    ) -> None:
        """Byline + CC-BY licence + correspondence and no prose: the
        licence/correspondence runs pass the ``usable_context`` floor on
        their own, so this exercises ``_FRONT_MATTER_RE``, not the word
        count (gr514381 review)."""
        assert usable_context("Helical microtubules", _CC_BY)
        assert usable_context("Helical microtubules", _CORRESPONDENCE)
        ref_id, _ = _seed_body(
            store,
            "Helical microtubules",
            [(_BYLINE, None), (_CC_BY, None), (_CORRESPONDENCE, None)],
        )
        client = _FakeClient(["Never asked."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert client.calls == []
        assert result == {"claimed": 1, "ok": 0, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        stamp = meta.get(META_NO_ABSTRACT_KEY)
        assert isinstance(stamp, dict) and stamp.get("v") == _PASS_VERSION

    def test_per_ref_log_names_the_chunk_ids(
        self, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        ref_id, ids = _seed_body(
            store, "A paper", [(_MASTHEAD, None), (_METHODS, None)]
        )
        with caplog.at_level(logging.INFO, logger="precis.workers.context_sentence"):
            run_context_sentence_pass(
                store, client=_FakeClient(["x."]), ref_ids=[ref_id]
            )
        assert any(
            f"ref_id={ref_id}" in r.getMessage() and str(ids[1]) in r.getMessage()
            for r in caplog.records
        )


class TestBackfillSelection:
    def test_selects_distinct_grounding_source_ref_ids(self, store: Any) -> None:
        paper1, chunk1 = _seed_paper(store, title="Paper one")
        paper2, chunk2 = _seed_paper(store, title="Paper two")
        _live_publish_row(
            store, chunk_id=chunk1, sentence="Claim A supports paper one."
        )
        _live_publish_row(
            store, chunk_id=chunk2, sentence="Claim B supports paper two."
        )

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert paper1 in ids
        assert paper2 in ids

    def test_excludes_unrelated_never_grounded_papers(self, store: Any) -> None:
        paper, chunk = _seed_paper(store, title="Grounded")
        other, _ = _seed_paper(store, title="Unrelated, never grounded")
        _live_publish_row(
            store, chunk_id=chunk, sentence="Claim grounded in one paper."
        )

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert paper in ids
        assert other not in ids

    def test_excludes_a_retired_hub(self, store: Any) -> None:
        paper, chunk = _seed_paper(store, title="Excluded via retired hub")
        hub = _live_publish_row(
            store, chunk_id=chunk, sentence="Claim retired via its hub."
        )
        store.retire_ref(hub)

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert paper not in ids

    def test_excludes_a_retired_source_ref(self, store: Any) -> None:
        paper, chunk = _seed_paper(store, title="Excluded via retired source")
        _live_publish_row(store, chunk_id=chunk, sentence="Claim retired via source.")
        store.retire_ref(paper)

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert paper not in ids

    def test_dedupes_a_paper_grounded_at_two_passages(self, store: Any) -> None:
        paper, chunk1 = _seed_paper(store, title="Grounded twice")
        chunk2 = seed_chunk(store, ref_id=paper, text="Second passage.", ord=1)
        hub = mint_hub(
            store, CanonicalClaim(sentence="Claim grounded twice.", scope={})
        )
        row = store.nanopub_create_publish_row(hub)
        ok = store.nanopub_approve(
            row.id,
            approved_title="Claim grounded twice.",
            claim_sha=f"{hub:064x}",
            aida_uri=f"http://purl.org/aida/csentdup{hub}",
            grounding={"passages": [{"chunk_id": chunk1}, {"chunk_id": chunk2}]},
        )
        assert ok

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert ids.count(paper) == 1

    def test_a_non_integral_chunk_id_does_not_kill_the_whole_query(
        self, store: Any
    ) -> None:
        """The frozen payload is reviewer-submitted JSON and the mint gate
        only asserts ``int(chunk_id)`` works — which accepts ``12.0``, a
        literal ``'12.0'::bigint`` rejects. A bare cast would fail the whole
        query and starve the cohort; the CASE guard must skip just that row
        and still return every well-formed paper."""
        good_paper, good_chunk = _seed_paper(store, title="Well-formed neighbour")
        _live_publish_row(
            store, chunk_id=good_chunk, sentence="Claim with a sane chunk_id."
        )

        bad_hub = mint_hub(
            store, CanonicalClaim(sentence="Claim with a float chunk_id.", scope={})
        )
        bad_row = store.nanopub_create_publish_row(bad_hub)
        assert store.nanopub_approve(
            bad_row.id,
            approved_title="Claim with a float chunk_id.",
            claim_sha=f"{bad_hub:064x}",
            aida_uri=f"http://purl.org/aida/csentflt{bad_hub}",
            grounding={"passages": [{"chunk_id": 12.0}, {"chunk_id": "not-a-number"}]},
        )

        with store.pool.connection() as conn:
            ids = backfill_candidate_ref_ids(conn)

        assert good_paper in ids


# ── end-to-end pass (real PG, fake client) ─────────────────────────────


class TestRunPass:
    def test_writes_the_sentence_to_refs_meta(self, store: Any) -> None:
        ref_id, _chunk = _seed_paper(store, title="A DFT paper")
        client = _FakeClient(["Computational study; DFT-calculated properties only."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert (
            ref.meta.get(META_KEY)
            == "Computational study; DFT-calculated properties only."
        )
        assert len(client.calls) == 1

    def test_persistent_lint_failure_writes_nothing_and_stamps_failed(
        self, store: Any
    ) -> None:
        ref_id, _chunk = _seed_paper(store, title="A paper")
        client = _FakeClient(
            [
                "This proves the mechanism beyond doubt.",
                "This also demonstrates a definitive result.",
            ]
        )

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert META_KEY not in (ref.meta or {})
        assert ref.meta.get(META_FAILED_KEY) == {"v": _PASS_VERSION}

    def test_transient_failure_leaves_the_paper_unstamped(self, store: Any) -> None:
        ref_id, _chunk = _seed_paper(store, title="A paper")

        result = run_context_sentence_pass(
            store, client=_BoomClient(), ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 0, "failed": 0}
        ref = store.fetch_refs_by_ids([ref_id])[ref_id]
        assert META_KEY not in (ref.meta or {})
        assert META_FAILED_KEY not in (ref.meta or {})

    def test_a_masthead_is_stamped_without_spending_a_model_call(
        self, store: Any
    ) -> None:
        """The guard runs before the client, so a content-free block costs
        nothing and converges non-terminally (gr346458)."""
        ref_id, _chunk = _seed_paper(
            store, title="Helical microtubules", body=_MASTHEAD
        )
        client = _FakeClient(["Never asked."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert client.calls == []
        assert result == {"claimed": 1, "ok": 0, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        stamp = meta.get(META_NO_ABSTRACT_KEY)
        assert isinstance(stamp, dict) and stamp.get("v") == _PASS_VERSION
        assert META_KEY not in meta
        # NOT the terminal stamp: no model ever gave a verdict here.
        assert META_FAILED_KEY not in meta

    def test_an_unusable_paper_converges_at_the_current_version(
        self, store: Any
    ) -> None:
        """Churn guard (gr514381): a stale-version no_abstract stamp earns
        ONE re-claim so a heal can land; a still-unusable outcome re-stamps
        at the current version and the paper leaves the claim set — a
        permanently masthead-only body must not churn every sweep."""
        ref_id, _chunk = _seed_paper(
            store, title="Helical microtubules", body=_MASTHEAD
        )
        run_context_sentence_pass(store, client=_FakeClient(["x"]), ref_ids=[ref_id])
        stamp = (store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}).get(
            META_NO_ABSTRACT_KEY
        )
        assert isinstance(stamp, dict) and stamp.get("v") == _PASS_VERSION
        assert isinstance(stamp.get("at"), str)

        client = _FakeClient(["never asked"])
        again = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert again == {"claimed": 0, "ok": 0, "failed": 0}
        assert client.calls == []

    def test_a_legacy_true_stamp_is_reclaimed_once_then_converges(
        self, store: Any
    ) -> None:
        ref_id, _chunk = _seed_paper(
            store, title="Helical microtubules", body=_MASTHEAD
        )
        store.update_ref(ref_id, meta_patch={META_NO_ABSTRACT_KEY: True})

        first = run_context_sentence_pass(
            store, client=_FakeClient(["x"]), ref_ids=[ref_id]
        )
        assert first["claimed"] == 1
        stamp = (store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}).get(
            META_NO_ABSTRACT_KEY
        )
        assert isinstance(stamp, dict) and stamp.get("v") == _PASS_VERSION

        second = run_context_sentence_pass(
            store, client=_FakeClient(["x"]), ref_ids=[ref_id]
        )
        assert second["claimed"] == 0

    def test_a_later_meta_abstract_reclaims_a_stamped_paper(self, store: Any) -> None:
        """``refs.updated_at`` newer than the stamp's ``at`` is the heal
        signal — an abstract backfill re-arms the claim."""
        ref_id, _ = _seed_body(store, "Helical microtubules", [(_MASTHEAD, None)])
        claimed_at = store.fetch_refs_by_ids([ref_id])[ref_id].updated_at
        _stamp_no_abstract(store, ref_id, claimed_at)
        gated = run_context_sentence_pass(
            store, client=_FakeClient(["x"]), ref_ids=[ref_id]
        )
        assert gated["claimed"] == 0

        store.update_ref(ref_id, meta_patch={"abstract": _ABSTRACT})
        result = run_context_sentence_pass(
            store,
            client=_FakeClient(["Helical microtubules were observed by TEM."]),
            ref_ids=[ref_id],
        )
        assert result["ok"] == 1

    def test_stamped_rows_claim_after_unstamped_ones(self, store: Any) -> None:
        """A permanently-thin cohort must not starve fresh claims off the
        ``ORDER BY ... LIMIT``."""
        low, _ = _seed_paper(store, title="stamped earlier ref")
        high, _ = _seed_paper(store, title="clean later ref")
        store.update_ref(
            low,
            meta_patch={META_NO_ABSTRACT_KEY: {"v": _PASS_VERSION, "at": "9999-12-31"}},
        )
        with store.pool.connection() as conn:
            rows = _claim(conn, limit=1, ref_ids=[low, high])
        assert [r[0] for r in rows] == [high]

    def test_skips_a_paper_that_already_has_a_sentence(self, store: Any) -> None:
        ref_id, _chunk = _seed_paper(store, title="Already sentenced")
        store.update_ref(ref_id, meta_patch={META_KEY: "Existing sentence."})
        client = _FakeClient(["Should never be used."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert client.calls == []

    def test_no_ref_ids_sweeps_the_backfill_cohort(self, store: Any) -> None:
        paper, chunk = _seed_paper(store, title="Grounded via publish row")
        _live_publish_row(store, chunk_id=chunk, sentence="Claim to ground the sweep.")
        client = _FakeClient(["Computational study; scheduled sweep found it."])

        result = run_context_sentence_pass(store, client=client)

        assert result["ok"] == 1
        ref = store.fetch_refs_by_ids([paper])[paper]
        assert (
            ref.meta.get(META_KEY) == "Computational study; scheduled sweep found it."
        )


# ── lead-body fallback + self-healing stamps (gr514381) ────────────────

#: Lead-of-body prose that matches NONE of the method-seeking queries —
#: only the leading-prose arm of ``lead_body_block`` can reach it, so a
#: test feeding it exercises that arm rather than the retrieval one.
_LEAD_PROSE = (
    "The microtubules consist of concentric graphitic sheets rolled into "
    "helical tubes with diameters ranging from a few nanometres up to "
    "several tens of nanometres, each tube closed by a fullerene cap."
)

#: Refs 783/1209's shape on prod: the paper's own "Abstract" paragraph
#: sits in the body at ord 1 with no card_abstract and no meta.abstract,
#: so retrieval used to walk straight past it.
_ABSTRACT_HEAD = "Abstract\n\n" + _ABSTRACT


class TestLeadBodyBlock:
    def test_abstract_headed_paragraph_starts_the_block(self, store: Any) -> None:
        ref_id, ids = _seed_body(
            store,
            "Carbon nanobuds paper",
            [(_MASTHEAD, None), (_ABSTRACT_HEAD, None), (_LEAD_PROSE, None)],
        )
        block, chunk_ids = lead_body_block(
            store, title="Carbon nanobuds paper", ref_id=ref_id, abstract_only=True
        )
        assert _ABSTRACT in block
        assert _LEAD_PROSE in block
        assert "Iijima" not in block
        assert ids[0] not in chunk_ids
        assert ids[1] in chunk_ids

    def test_no_abstract_heading_falls_back_to_leading_prose(self, store: Any) -> None:
        ref_id, _ = _seed_body(
            store, "A paper", [(_MASTHEAD, None), (_LEAD_PROSE, None)]
        )
        block, _ = lead_body_block(store, title="A paper", ref_id=ref_id)
        assert _LEAD_PROSE in block

    def test_leading_prose_skips_end_matter(self, store: Any) -> None:
        ref_id, ids = _seed_body(
            store, "A paper", [(_REFS_UNSECTIONED, None), (_LEAD_PROSE, None)]
        )
        block, chunk_ids = lead_body_block(store, title="A paper", ref_id=ref_id)
        assert block == _LEAD_PROSE
        assert ids[0] not in chunk_ids

    def test_abstract_head_wins_over_earlier_prose(self, store: Any) -> None:
        """The headed paragraph wins over position: an ord-0 lead that
        isn't the abstract must not shadow the real one at ord 1."""
        ref_id, _ = _seed_body(
            store, "A paper", [(_LEAD_PROSE, None), (_ABSTRACT_HEAD, None)]
        )
        block, _ = lead_body_block(store, title="A paper", ref_id=ref_id)
        assert _ABSTRACT in block
        assert _LEAD_PROSE not in block

    def test_laboratory_lead_prose_is_not_front_matter(self, store: Any) -> None:
        """The affiliation alternatives fire only with a comma or "of"
        directly after the word — "Laboratory measurements of ..."
        is content (gr514381 review)."""
        ref_id, _ = _seed_body(store, "A paper", [(_LAB_PROSE, None)])
        block, _ = lead_body_block(store, title="A paper", ref_id=ref_id)
        assert block == _LAB_PROSE


class TestSelfHealingStamps:
    """gr514381: a no_abstract stamp marks missing context, not a verdict —
    when usable context appears the paper heals without an operator
    clear, and a written sentence removes both stamp keys."""

    def test_stale_no_abstract_heals_on_a_later_meta_abstract(self, store: Any) -> None:
        ref_id, _ = _seed_body(store, "Helical microtubules", [(_MASTHEAD, None)])
        store.update_ref(
            ref_id,
            meta_patch={META_NO_ABSTRACT_KEY: True, META_FAILED_KEY: True},
        )
        store.update_ref(ref_id, meta_patch={"abstract": _ABSTRACT})
        client = _FakeClient(["Helical microtubules were observed by TEM."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert meta.get(META_KEY) == "Helical microtubules were observed by TEM."
        assert META_NO_ABSTRACT_KEY not in meta
        assert META_FAILED_KEY not in meta

    def test_an_abstract_folded_by_a_bodiless_ingest_heals_the_stamp(
        self, store: Any
    ) -> None:
        """gr514381 residual: an ingest that yields no body writes no new
        chunks (its cards collide on ``(ref_id, ord)``), so the folded
        ``meta.abstract`` used to leave no trace in either heal arm —
        ``updated_at`` never moved and no chunk ``created_at`` landed —
        and the paper stayed stamped forever. The fold now bumps
        ``updated_at``, which re-arms the claim."""
        ref_id = seed_ref(store, title="Helical microtubules")
        claimed_at = store.fetch_refs_by_ids([ref_id])[ref_id].updated_at
        _stamp_no_abstract(store, ref_id, claimed_at)
        gated = run_context_sentence_pass(
            store, client=_FakeClient(["x"]), ref_ids=[ref_id]
        )
        assert gated["claimed"] == 0

        from precis.ingest.db_writer import (
            PaperToWrite,
            register_aliases_and_maybe_upgrade,
        )

        bodiless = PaperToWrite(
            title="Helical microtubules",
            authors=[{"name": "Iijima, S"}],
            year=1991,
            paper_id="cs514381",
            cite_key_prefix="iijima91",
            doi="10.9/helical",
            provider="markup",
            content_hash="ab" * 32,
            meta={"abstract": _ABSTRACT},
            chunks=[],
        )
        with store.pool.connection() as conn:
            register_aliases_and_maybe_upgrade(ref_id, bodiless, conn=conn)
            conn.commit()

        client = _FakeClient(["Helical microtubules were observed by TEM."])
        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert meta.get(META_KEY) == "Helical microtubules were observed by TEM."
        assert META_NO_ABSTRACT_KEY not in meta

    def test_stale_no_abstract_heals_on_an_abstract_paragraph(self, store: Any) -> None:
        """The abstract was in the body all along (the 11-paper prod
        cohort): the stamp alone must not keep the pass from finding it."""
        ref_id, _ = _seed_body(
            store,
            "Carbon nanobuds paper",
            [(_MASTHEAD, None), (_ABSTRACT_HEAD, None)],
        )
        store.update_ref(ref_id, meta_patch={META_NO_ABSTRACT_KEY: True})
        client = _FakeClient(["Carbon nanobuds were observed by TEM."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result["ok"] == 1
        user = client.calls[0][-1]["content"]
        assert _ABSTRACT in user
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert META_NO_ABSTRACT_KEY not in meta

    def test_abstract_paragraph_feeds_the_model(self, store: Any) -> None:
        """No stamp at all: ord-1 "Abstract ..." prose reaches the model
        instead of the method-query passages or a decline."""
        ref_id, _ = _seed_body(
            store,
            "Carbon nanobuds paper",
            [(_MASTHEAD, None), (_ABSTRACT_HEAD, None), (_LEAD_PROSE, None)],
        )
        client = _FakeClient(["Carbon nanobuds were observed by TEM."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result["ok"] == 1
        user = client.calls[0][-1]["content"]
        assert _ABSTRACT in user
        assert "Iijima" not in user

    def test_leading_prose_feeds_the_model_when_nothing_else_can(
        self, store: Any
    ) -> None:
        ref_id, _ = _seed_body(
            store, "Helical microtubules", [(_MASTHEAD, None), (_LEAD_PROSE, None)]
        )
        client = _FakeClient(["Helical microtubules were observed by TEM."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result["ok"] == 1
        assert _LEAD_PROSE in client.calls[0][-1]["content"]


class TestStampsGateClaim:
    """The lazy-enqueue prefilter runs inside the render path — a
    malformed stamp or ref timestamp must gate, never raise
    (gr514381 review)."""

    def test_a_naive_stamp_at_is_malformed_not_a_crash(self) -> None:
        meta = {
            META_NO_ABSTRACT_KEY: {
                "v": _PASS_VERSION,
                "at": "2026-10-25 11:00:00",
            }
        }
        assert stamps_gate_claim(meta, ref_updated_at=datetime.now(UTC))

    def test_a_missing_or_naive_ref_updated_at_gates(self) -> None:
        meta = {
            META_NO_ABSTRACT_KEY: {"v": _PASS_VERSION, "at": "2999-01-01T00:00:00Z"}
        }
        assert stamps_gate_claim(meta, ref_updated_at=None)
        assert stamps_gate_claim(meta, ref_updated_at=datetime(2026, 1, 1))

    def test_a_newer_ref_updated_at_reopens_the_gate(self) -> None:
        meta = {
            META_NO_ABSTRACT_KEY: {"v": _PASS_VERSION, "at": "2026-01-01T00:00:00Z"}
        }
        assert not stamps_gate_claim(
            meta, ref_updated_at=datetime(2026, 10, 25, tzinfo=UTC)
        )

    def test_a_stale_version_stamp_never_gates(self) -> None:
        meta = {META_NO_ABSTRACT_KEY: True, META_FAILED_KEY: True}
        assert not stamps_gate_claim(meta, ref_updated_at=datetime.now(UTC))


class TestVersionedFailedStamp:
    """gr514381: ``context_sentence_failed`` carries the pass version —
    a stamp older than :data:`_PASS_VERSION` earns exactly one re-trial,
    so this fix re-runs the prod cohort without a manual clear."""

    def test_legacy_true_stamp_earns_one_retrial(self, store: Any) -> None:
        ref_id, _ = _seed_paper(store, title="A DFT paper")
        store.update_ref(ref_id, meta_patch={META_FAILED_KEY: True})
        client = _FakeClient(["Computational study; DFT-calculated properties only."])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert META_FAILED_KEY not in meta
        assert meta.get(META_KEY) == (
            "Computational study; DFT-calculated properties only."
        )

    def test_decline_on_the_retrial_restamps_at_the_current_version(
        self, store: Any
    ) -> None:
        """A legacy ``true`` failed stamp is claimed once; a NO_CONTEXT
        verdict on the retry re-stamps at :data:`_PASS_VERSION` and the
        paper is not claimed again (wrong-paper text stays declined)."""
        ref_id, _ = _seed_paper(store, title="A DFT paper")
        store.update_ref(ref_id, meta_patch={META_FAILED_KEY: True})
        client = _FakeClient([NO_CONTEXT])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert meta.get(META_FAILED_KEY) == {"v": _PASS_VERSION}

        client2 = _FakeClient([NO_CONTEXT])
        again = run_context_sentence_pass(store, client=client2, ref_ids=[ref_id])
        assert again["claimed"] == 0
        assert client2.calls == []

    def test_current_version_failed_stamp_is_not_reclaimed(self, store: Any) -> None:
        ref_id, _ = _seed_paper(store, title="A DFT paper")
        store.update_ref(ref_id, meta_patch={META_FAILED_KEY: {"v": _PASS_VERSION}})
        client = _FakeClient([NO_CONTEXT])

        result = run_context_sentence_pass(store, client=client, ref_ids=[ref_id])

        assert result["claimed"] == 0
        assert client.calls == []


class TestScheduledSweepCohort:
    """gr514381 follow-up: the scheduled pass (no explicit ``ref_ids``)
    unions stamp-bearing live refs into the default cohort — a stamped
    paper that was never a grounding source still gets its heal /
    re-trial, and an unstamped non-cohort paper stays out of scope."""

    def test_legacy_no_abstract_stamp_heals_without_a_grounding_row(
        self, store: Any
    ) -> None:
        """Prod's shape: ``context_sentence_no_abstract: true`` from the
        unversioned pass, a usable ``meta.abstract``, and no nanopub
        grounding row — the stamp alone makes it a claim candidate, so
        the model runs and the stamp comes off."""
        ref_id = seed_ref(store, title="A DFT paper")
        store.update_ref(
            ref_id,
            meta_patch={META_NO_ABSTRACT_KEY: True, "abstract": _ABSTRACT},
        )
        client = _FakeClient(["Computational study; DFT-calculated properties only."])

        result = run_context_sentence_pass(store, client=client)

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert len(client.calls) == 1
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert (
            meta.get(META_KEY) == "Computational study; DFT-calculated properties only."
        )
        assert META_NO_ABSTRACT_KEY not in meta

    def test_legacy_failed_stamp_earns_exactly_one_retry(self, store: Any) -> None:
        """``context_sentence_failed: true`` enters the cohort, the
        re-trial's verdict re-stamps at :data:`_PASS_VERSION`, and the
        next sweep does not claim the paper again."""
        ref_id = seed_ref(store, title="A DFT paper")
        store.update_ref(
            ref_id, meta_patch={META_FAILED_KEY: True, "abstract": _ABSTRACT}
        )
        client = _FakeClient([NO_CONTEXT])

        result = run_context_sentence_pass(store, client=client)

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        assert len(client.calls) == 1
        meta = store.fetch_refs_by_ids([ref_id])[ref_id].meta or {}
        assert meta.get(META_FAILED_KEY) == {"v": _PASS_VERSION}

        client2 = _FakeClient([NO_CONTEXT])
        again = run_context_sentence_pass(store, client=client2)

        assert again["claimed"] == 0
        assert client2.calls == []

    def test_an_unstamped_non_cohort_paper_is_not_claimed(self, store: Any) -> None:
        """The union adds stamp-bearing refs only — not the whole corpus."""
        ref_id, _chunk = _seed_paper(store, title="Never grounded, never stamped")
        client = _FakeClient(["Never asked."])

        result = run_context_sentence_pass(store, client=client)

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert client.calls == []

    def test_a_retired_stamped_ref_is_not_claimed(self, store: Any) -> None:
        """``_STAMPED_SQL`` is live-refs-only: retiring a stamped ref
        takes it out of the candidate union (and ``_claim``'s own
        ``retired_at IS NULL`` is the belt to that brace)."""
        ref_id = seed_ref(store, title="A DFT paper")
        store.update_ref(
            ref_id, meta_patch={META_FAILED_KEY: True, "abstract": _ABSTRACT}
        )
        store.retire_ref(ref_id)
        client = _FakeClient(["Never asked."])

        result = run_context_sentence_pass(store, client=client)

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert client.calls == []

    def test_the_stamped_union_respects_the_batch_cap(self, store: Any) -> None:
        """3 stamped non-cohort refs + ``batch_size=2`` claims 2 — the
        stamped half of the union rides the same ``LIMIT`` per tick."""
        ids = [seed_ref(store, title=f"DFT paper {i}") for i in range(3)]
        for ref_id in ids:
            store.update_ref(
                ref_id,
                meta_patch={META_NO_ABSTRACT_KEY: True, "abstract": _ABSTRACT},
            )
        client = _FakeClient(["Computational study; DFT-calculated properties only."])

        result = run_context_sentence_pass(store, client=client, batch_size=2)

        assert result == {"claimed": 2, "ok": 2, "failed": 0}
        assert len(client.calls) == 2

    def test_the_stamped_ref_set_is_cached_for_the_ttl(self, store: Any) -> None:
        """``_stamped_ref_ids`` resolves once per
        :data:`_STAMPED_IDS_TTL_S` — a stamp written inside the TTL
        waits for the next resolution, so the full ``refs`` scan is not
        re-run on every worker loop (gr514381 review)."""
        ref_id = seed_ref(store, title="A DFT paper")
        t = [0.0]

        def clock() -> float:
            return t[0]

        with store.pool.connection() as conn:
            assert _stamped_ref_ids(conn, clock=clock) == []
            store.update_ref(ref_id, meta_patch={META_FAILED_KEY: True})
            assert _stamped_ref_ids(conn, clock=clock) == []
            t[0] += _STAMPED_IDS_TTL_S + 1.0
            assert _stamped_ref_ids(conn, clock=clock) == [ref_id]
