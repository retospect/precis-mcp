"""Tests for RAKE keyword extraction."""

import pytest

from precis.rake import STOPWORDS, _score_phrases, _split_to_phrases, telegram_precis


class TestSplitToPhrases:
    def test_basic(self):
        phrases = _split_to_phrases("the quick brown fox")
        # "the" is a stopword, so we get ["quick", "brown", "fox"]
        words = [w for phrase in phrases for w in phrase]
        assert "quick" in words
        assert "brown" in words
        assert "fox" in words
        assert "the" not in words

    def test_punctuation_splits(self):
        phrases = _split_to_phrases("heat transfer. fluid dynamics")
        # Should split on period
        assert len(phrases) >= 2

    def test_empty(self):
        assert _split_to_phrases("") == []

    def test_all_stopwords(self):
        phrases = _split_to_phrases("the and or but")
        assert phrases == []


class TestScorePhrases:
    def test_multi_word_scores_higher(self):
        phrases = [["heat", "transfer"], ["fluid"], ["heat", "loss"]]
        scored = _score_phrases(phrases)
        # "heat transfer" and "heat loss" should score higher than "fluid"
        scores = {p: s for s, p in scored}
        assert scores["heat transfer"] > scores["fluid"]

    def test_deduplicates(self):
        phrases = [["heat", "transfer"], ["heat", "transfer"]]
        scored = _score_phrases(phrases)
        assert len(scored) == 1

    def test_empty(self):
        assert _score_phrases([]) == []


class TestTelegramPrecis:
    def test_returns_semicolon_joined(self):
        text = (
            "The 12-layer transformer model uses a custom loss function "
            "with AdamW optimizer for training. Batch normalization is "
            "applied after each attention layer. The learning rate follows "
            "a cosine annealing schedule with warm restarts."
        )
        result = telegram_precis(text)
        assert ";" in result

    def test_min_phrases(self):
        text = "Heat transfer coefficient measured at steady state."
        result = telegram_precis(text)
        # Should produce at least some output
        assert len(result) > 0

    def test_short_text_fallback(self):
        result = telegram_precis("hello")
        assert len(result) > 0

    def test_empty_text(self):
        result = telegram_precis("")
        assert isinstance(result, str)

    def test_max_phrases_capped(self):
        # Long text with many distinct phrases
        text = " ".join(
            f"The {word} analysis shows significant results."
            for word in [
                "thermal",
                "structural",
                "dynamic",
                "modal",
                "acoustic",
                "vibration",
                "fatigue",
                "creep",
                "buckling",
                "flutter",
                "aeroelastic",
                "transient",
                "nonlinear",
                "stochastic",
                "probabilistic",
            ]
        )
        result = telegram_precis(text, max_n=10)
        phrases = result.split("; ")
        assert len(phrases) <= 10

    def test_academic_text(self):
        text = (
            "We propose a novel entransy-based optimization method for "
            "heat exchanger networks. The method minimizes total entransy "
            "dissipation subject to heat duty constraints. Numerical results "
            "demonstrate 15% improvement over conventional pinch analysis."
        )
        result = telegram_precis(text)
        # Should capture domain-specific phrases
        assert any(
            kw in result.lower()
            for kw in ["entransy", "heat exchanger", "optimization", "pinch"]
        )


class TestStopwords:
    def test_common_words_present(self):
        for w in ["the", "and", "is", "of", "to", "in", "for", "with"]:
            assert w in STOPWORDS

    def test_content_words_absent(self):
        for w in ["heat", "transfer", "model", "algorithm", "network"]:
            assert w not in STOPWORDS

    def test_frozen(self):
        assert isinstance(STOPWORDS, frozenset)
