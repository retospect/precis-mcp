"""Tests for the paper-context-sentence pass (docs/backlog/
paper-context-sentence.md).

Pure lint/propose helpers run everywhere. The backfill-selection query and
the end-to-end pass run against real PG (the ``store`` fixture) with a fake
LLM client — no network, mirroring ``test_paper_glossary``/
``test_hub_tagline``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers.context_sentence import (
    META_FAILED_KEY,
    META_KEY,
    META_NO_ABSTRACT_KEY,
    NO_CONTEXT,
    TransientFailure,
    _generate_with_lint,
    backfill_candidate_ref_ids,
    is_decline,
    lint_violation,
    propose_context_sentence,
    run_context_sentence_pass,
    usable_context,
)
from tests.workers._helpers import seed_chunk, seed_ref


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
        assert ref.meta.get(META_FAILED_KEY) is True

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
        assert meta.get(META_NO_ABSTRACT_KEY) is True
        assert META_KEY not in meta
        # NOT the terminal stamp: no model ever gave a verdict here.
        assert META_FAILED_KEY not in meta

    def test_a_no_abstract_paper_is_not_reclaimed(self, store: Any) -> None:
        ref_id, _chunk = _seed_paper(
            store, title="Helical microtubules", body=_MASTHEAD
        )
        run_context_sentence_pass(store, client=_FakeClient(["x"]), ref_ids=[ref_id])

        again = run_context_sentence_pass(
            store, client=_FakeClient(["x"]), ref_ids=[ref_id]
        )

        assert again["claimed"] == 0

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
