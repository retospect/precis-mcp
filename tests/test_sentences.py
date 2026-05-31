"""Sentence splitter contract tests.

These lock the parts the downstream discovery layer relies on:

* abbreviation-aware splitting (no break inside "et al.", "Fig.",
  "i.e.", "vs.", "e.g.") — critical for scientific prose,
* citation parentheticals stay attached,
* char offsets point at the sentence's first character in the
  source so the verifier sub-call can grep verbatim text,
* deterministic output for cache invalidation reasoning,
* version constant matches the convention so downstream rows can
  detect upgrades.
"""

from __future__ import annotations

import pytest

from precis.utils.sentences import (
    SENTENCE_SPLITTER_VERSION,
    Sentence,
    split_sentences,
)


class TestTrivial:
    def test_empty_returns_empty(self) -> None:
        assert split_sentences("") == []

    def test_whitespace_returns_empty(self) -> None:
        assert split_sentences("   \n\t  ") == []

    def test_single_sentence_returns_one(self) -> None:
        out = split_sentences("This is one sentence.")
        assert len(out) == 1
        assert out[0].text.strip() == "This is one sentence."

    def test_two_simple_sentences_split(self) -> None:
        out = split_sentences("First sentence. Second sentence.")
        assert len(out) == 2


class TestScientificAbbreviations:
    """The whole point of switching off the naive ``. `` splitter."""

    @pytest.mark.parametrize(
        "text",
        [
            "Smith et al. demonstrated the effect.",
            "Cf. Fig. 3 for details on the spectra.",
            "We used DFT (i.e. density functional theory) calculations.",
            "Compare vs. the control sample shown above.",
            "Add e.g. lithium or sodium salts to the electrolyte.",
        ],
    )
    def test_abbreviation_does_not_split(self, text: str) -> None:
        out = split_sentences(text)
        assert len(out) == 1, (
            f"abbreviation should not break sentence; got {len(out)} pieces "
            f"from {text!r}: {[s.text for s in out]}"
        )

    def test_citation_parenthetical_stays_attached(self) -> None:
        text = (
            "Recent work (Smith et al., 2020) shows that MOF catalysts "
            "improve CO2 reduction. We extend this analysis."
        )
        out = split_sentences(text)
        assert len(out) == 2, (
            f"expected 2 sentences; got {len(out)}: {[s.text for s in out]}"
        )

    def test_figure_reference_stays_attached(self) -> None:
        text = "The spectra in Fig. 3 confirm the assignment. We then varied X."
        out = split_sentences(text)
        assert len(out) == 2


class TestCharOffsets:
    def test_offset_points_at_first_char(self) -> None:
        text = "First sentence. Second sentence."
        out = split_sentences(text)
        assert len(out) == 2
        for sent in out:
            sliced = text[sent.char_offset : sent.char_offset + len(sent.text)]
            assert sliced == sent.text, (
                f"offset {sent.char_offset} does not align with "
                f"{sent.text!r}; sliced={sliced!r}"
            )

    def test_offset_consistent_with_leading_whitespace(self) -> None:
        text = "\n\n   First sentence.   Second sentence."
        out = split_sentences(text)
        assert len(out) == 2
        first = out[0]
        # The offset should land on 'F' of 'First', past the
        # leading whitespace + newlines.
        assert text[first.char_offset] == "F"


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        text = (
            "We synthesized Cu-MOF (i.e. copper metal-organic framework) "
            "samples. They achieved 12% FE for CO2 reduction at -0.3 V. "
            "Details are in Fig. 2 and Table 1."
        )
        a = split_sentences(text)
        b = split_sentences(text)
        assert [s.text for s in a] == [s.text for s in b]
        assert [s.char_offset for s in a] == [s.char_offset for s in b]


class TestVersion:
    def test_version_constant_present(self) -> None:
        # Downstream readers compare stored sentence_splitter_version
        # against this constant for lazy invalidation. Make sure
        # something non-empty exists.
        assert isinstance(SENTENCE_SPLITTER_VERSION, str)
        assert SENTENCE_SPLITTER_VERSION

    def test_version_format_includes_engine(self) -> None:
        # Convention: <engine>-<engine-version>-<adapter-version>.
        # Loosely checked so a sane bump doesn't break the test.
        assert "pysbd" in SENTENCE_SPLITTER_VERSION


class TestSentenceDataclass:
    def test_immutable(self) -> None:
        s = Sentence(text="hello.", char_offset=0)
        with pytest.raises(Exception):
            s.text = "x"  # type: ignore[misc]
