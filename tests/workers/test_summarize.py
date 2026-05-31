"""Tests for ``precis.workers.summarize``.

Pure-Python RAKE — no DB, no embeddings. Tests cover the algorithm
plus the handler wrapper (deferring DB persistence to
``test_runner.py`` / ``test_status.py``).
"""

from __future__ import annotations

import pytest

from precis.utils.rake import _candidate_phrases, extract_keywords
from precis.workers.base import ChunkRow
from precis.workers.summarize import RakeLemmaHandler

# ---------------------------------------------------------------------------
# extract_keywords — pure algorithm
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_empty_text_returns_empty(self):
        assert extract_keywords("") == []

    def test_whitespace_only_returns_empty(self):
        assert extract_keywords("   \n\t ") == []

    def test_stopword_only_returns_empty(self):
        # All words filtered as stopwords -> no phrases survive.
        assert extract_keywords("the and of to a") == []

    def test_single_phrase_extraction(self):
        # A single multi-word phrase wins by default scoring.
        text = "Quantum error correction is essential for fault tolerance."
        out = extract_keywords(text, max_keywords=5)
        assert "quantum error correction" in out
        # 'fault tolerance' is a separate phrase split by 'for'.
        assert "fault tolerance" in out

    def test_score_ordering_top_phrase_first(self):
        # 'surface code' co-occurs in two phrases -> higher score
        # than 'quantum'.
        text = (
            "The surface code is a quantum code. "
            "Surface code thresholds are well known."
        )
        out = extract_keywords(text, max_keywords=10)
        # Higher-scoring multi-word phrases must outrank single-word ones.
        # 'surface code' appears with 'thresholds' giving it degree boost.
        assert out[0].startswith("surface code")

    def test_lowercases_output(self):
        out = extract_keywords("Surface Codes Matter.", max_keywords=2)
        assert all(p == p.lower() for p in out)

    def test_max_keywords_caps_output(self):
        # 6 distinct phrases, request 2 -> exactly 2 returned.
        text = (
            "Alpha beta. Gamma delta. Epsilon zeta. Eta theta. Iota kappa. Lambda mu."
        )
        out = extract_keywords(text, max_keywords=2)
        assert len(out) == 2

    def test_max_keywords_zero_returns_empty(self):
        # Edge: caller wants zero keywords; pure-fn should oblige.
        out = extract_keywords("non-trivial input here.", max_keywords=0)
        assert out == []

    def test_min_phrase_words_filters_short(self):
        # 'quantum' and 'codes' are split by 'are' (stopword) so each
        # is a single-word phrase; min=2 drops them.
        text = "Quantum are codes."
        out = extract_keywords(text, min_phrase_words=2)
        assert out == []
        # Sanity check: at min=1 we DO get them.
        out_min1 = extract_keywords(text, min_phrase_words=1)
        assert "quantum" in out_min1 and "codes" in out_min1

    def test_max_phrase_words_filters_long(self):
        # max=2 drops phrases with 3+ words.
        text = "quantum error correction codes are useful."
        out = extract_keywords(text, max_phrase_words=2)
        assert "quantum error correction codes" not in out
        # Pure-letter phrases shorter than the cap survive when
        # split by stopwords. With no stopwords inside the phrase,
        # 'quantum error correction codes' is a single 4-word phrase
        # which the cap drops; nothing else qualifies in this text.

    def test_invalid_min_phrase_words_raises(self):
        with pytest.raises(ValueError):
            extract_keywords("x", min_phrase_words=0)

    def test_invalid_max_phrase_words_raises(self):
        with pytest.raises(ValueError):
            extract_keywords("x", min_phrase_words=3, max_phrase_words=2)

    def test_invalid_max_keywords_raises(self):
        with pytest.raises(ValueError):
            extract_keywords("x", max_keywords=-1)

    def test_deterministic_output(self):
        # Same input -> same output, order included.
        text = "Surface codes. Quantum threshold. Surface codes again."
        a = extract_keywords(text)
        b = extract_keywords(text)
        assert a == b

    def test_dedupes_by_surface_form(self):
        # Repeated phrases collapse to one entry.
        text = "surface code. surface code. surface code."
        out = extract_keywords(text)
        assert out.count("surface code") == 1

    def test_strips_pure_digit_tokens(self):
        # Pure-digit tokens act as phrase separators (RAKE convention).
        text = "Equation 42 governs the result."
        out = extract_keywords(text)
        # Both 'equation' and 'governs the result' (filtered by stopwords)
        # may appear, but '42' must not.
        assert all("42" not in phrase.split() for phrase in out)


# ---------------------------------------------------------------------------
# _candidate_phrases — boundary behaviour
# ---------------------------------------------------------------------------


class TestCandidatePhrases:
    def test_sentence_boundary_breaks_phrase(self):
        # "alpha beta" and "gamma delta" are separated by '.', they
        # must not merge.
        phrases = _candidate_phrases(
            "alpha beta. gamma delta",
            stopwords=frozenset(),
            min_words=1,
            max_words=10,
        )
        assert ["alpha", "beta"] in phrases
        assert ["gamma", "delta"] in phrases

    def test_stopword_breaks_phrase(self):
        phrases = _candidate_phrases(
            "alpha and beta",
            stopwords=frozenset({"and"}),
            min_words=1,
            max_words=10,
        )
        # ['alpha'] and ['beta'] split at 'and'.
        assert phrases == [["alpha"], ["beta"]]

    def test_min_words_filter(self):
        phrases = _candidate_phrases(
            "alpha and beta gamma",
            stopwords=frozenset({"and"}),
            min_words=2,
            max_words=10,
        )
        # ['alpha'] dropped; ['beta', 'gamma'] kept.
        assert phrases == [["beta", "gamma"]]

    def test_max_words_filter(self):
        phrases = _candidate_phrases(
            "alpha beta gamma delta",
            stopwords=frozenset(),
            min_words=1,
            max_words=2,
        )
        # 4-word phrase exceeds cap and is dropped.
        assert phrases == []


# ---------------------------------------------------------------------------
# RakeLemmaHandler — pure (no DB)
# ---------------------------------------------------------------------------


class TestRakeLemmaHandlerPure:
    def test_name_and_metadata(self):
        h = RakeLemmaHandler()
        assert h.output_table == "chunk_summaries"
        assert h.model_column == "summarizer"
        assert h.model_name == "rake-lemma"
        assert h.name == "summarize:rake-lemma"

    def test_custom_model_name(self):
        h = RakeLemmaHandler(model_name="rake-v2")
        assert h.model_name == "rake-v2"
        assert h.name == "summarize:rake-v2"

    def test_process_returns_joined_keywords(self):
        h = RakeLemmaHandler(max_keywords=3)
        row = ChunkRow(
            chunk_id=1,
            text="Surface codes for quantum computing. Surface codes scale.",
        )
        out = h.process(row)
        # joined by '; '
        assert "; " in out
        assert "surface codes" in out

    def test_process_empty_text_returns_empty(self):
        h = RakeLemmaHandler()
        row = ChunkRow(chunk_id=1, text="")
        assert h.process(row) == ""

    def test_process_honours_max_keywords(self):
        h = RakeLemmaHandler(max_keywords=2)
        row = ChunkRow(
            chunk_id=1,
            text=("Alpha beta. Gamma delta. Epsilon zeta. Eta theta. Iota kappa."),
        )
        out = h.process(row)
        # Two phrases, joined -> exactly one '; ' separator.
        assert out.count("; ") == 1
