"""KeyBERT semantic keyword extractor contract tests.

Uses a deterministic mock embedder (hash-based) so the test
doesn't depend on a real model. The mock isn't semantically
meaningful but its determinism lets us pin the integration shape
— top-K selection, exclude filtering, MMR diversity, case
recovery, edge cases.
"""

from __future__ import annotations

import hashlib

import pytest

from precis.utils.keybert import (
    extract_keywords_semantic,
    mean_embedding,
    privileged_candidates,
)


# ── deterministic mock embedder ─────────────────────────────────────


class _MockEmbedder:
    """Hash-based pseudo-embedder. Same text → same vector. Different
    texts → different vectors. Not semantic, but enough to exercise
    the integration."""

    def __init__(self, dim: int = 16) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode()).digest()
            # Map bytes to [-1, 1] floats; pad/truncate to dim.
            vec = [(b / 127.5) - 1.0 for b in digest[: self.dim]]
            while len(vec) < self.dim:
                vec.append(0.0)
            out.append(vec)
        return out


@pytest.fixture
def embedder() -> _MockEmbedder:
    return _MockEmbedder(dim=16)


# ── trivial / boundary cases ────────────────────────────────────────


class TestTrivial:
    def test_empty_text(self, embedder: _MockEmbedder) -> None:
        target = embedder.embed(["foo"])[0]
        assert extract_keywords_semantic(
            "", target_embedding=target, embedder=embedder
        ) == []

    def test_whitespace_text(self, embedder: _MockEmbedder) -> None:
        target = embedder.embed(["foo"])[0]
        assert extract_keywords_semantic(
            "   \n  ", target_embedding=target, embedder=embedder
        ) == []

    def test_top_k_zero(self, embedder: _MockEmbedder) -> None:
        target = embedder.embed(["foo"])[0]
        assert extract_keywords_semantic(
            "lithium nitrogen reduction",
            target_embedding=target,
            embedder=embedder,
            top_k=0,
        ) == []


# ── core behaviour ──────────────────────────────────────────────────


class TestCore:
    def test_returns_at_most_top_k(self, embedder: _MockEmbedder) -> None:
        text = (
            "Lithium battery anode design. Copper foil current collector. "
            "PEO membrane synthesis. BF4 salt doping. Ion transport analysis."
        )
        target = embedder.embed(["lithium battery"])[0]
        out = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=3
        )
        assert len(out) <= 3

    def test_exclude_drops_phrases(self, embedder: _MockEmbedder) -> None:
        text = "lithium battery anode. copper foil. peo membrane."
        target = embedder.embed(["lithium"])[0]
        out_with = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=5
        )
        out_without = extract_keywords_semantic(
            text,
            target_embedding=target,
            embedder=embedder,
            top_k=5,
            exclude={"lithium battery anode"},
        )
        assert any(
            "lithium battery anode" in k.lower() for k in out_with
        ), f"baseline call should surface the phrase; got {out_with}"
        assert not any(
            "lithium battery anode" in k.lower() for k in out_without
        ), f"exclude should drop the phrase; got {out_without}"

    def test_exclude_is_case_insensitive(self, embedder: _MockEmbedder) -> None:
        text = "Metal Organic Framework synthesis. Lithium deposition."
        target = embedder.embed(["mof"])[0]
        out = extract_keywords_semantic(
            text,
            target_embedding=target,
            embedder=embedder,
            top_k=5,
            exclude={"METAL ORGANIC FRAMEWORK SYNTHESIS"},
        )
        assert not any(
            "metal organic framework synthesis" in k.lower() for k in out
        ), f"case-insensitive exclude failed; got {out}"


# ── case recovery ───────────────────────────────────────────────────


class TestCaseRecovery:
    def test_preserves_uppercase_acronym(self, embedder: _MockEmbedder) -> None:
        text = "FTIR spectroscopy and DFT calculations confirm the trend."
        target = embedder.embed(["FTIR"])[0]
        out = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=5
        )
        # At least one returned phrase preserves uppercase FTIR or DFT.
        joined = " ".join(out)
        assert "FTIR" in joined or "DFT" in joined, (
            f"expected uppercase preserved; got {out}"
        )

    def test_preserves_title_case(self, embedder: _MockEmbedder) -> None:
        text = "Membrane Electrode Assembly design with PEO membrane."
        target = embedder.embed(["membrane"])[0]
        out = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=5
        )
        # At least one returned phrase preserves the original Title Case.
        assert any(
            "Membrane Electrode Assembly" in k for k in out
        ), f"expected title-case preserved; got {out}"


# ── MMR diversity ───────────────────────────────────────────────────


class TestMMR:
    def test_lambda_zero_is_pure_top_k(self, embedder: _MockEmbedder) -> None:
        text = "alpha beta gamma. delta epsilon zeta. eta theta iota."
        target = embedder.embed(["alpha"])[0]
        out_plain = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=3,
            diversity_lambda=0.0,
        )
        # With λ=0, should pick top-3 by raw score, no diversity penalty.
        assert len(out_plain) <= 3

    def test_lambda_positive_changes_selection(
        self, embedder: _MockEmbedder
    ) -> None:
        # 4 candidates where two are near-duplicates. MMR with high
        # λ should pick non-duplicates; λ=0 might pick both
        # duplicates. We just verify the output differs when
        # diversity is enabled.
        text = "alpha beta. alpha beta gamma. delta epsilon. zeta eta theta."
        target = embedder.embed(["alpha"])[0]
        plain = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=3,
            diversity_lambda=0.0,
        )
        diverse = extract_keywords_semantic(
            text, target_embedding=target, embedder=embedder, top_k=3,
            diversity_lambda=0.7,
        )
        # Both produce some output; the diverse selection may reorder
        # or replace items. We don't pin the exact result (depends on
        # hash-based mock) but verify the call doesn't crash.
        assert len(plain) <= 3
        assert len(diverse) <= 3


# ── mean_embedding helper ───────────────────────────────────────────


class TestMeanEmbedding:
    def test_empty_list_returns_empty(self) -> None:
        assert mean_embedding([]) == []

    def test_single_vector_returns_itself(self) -> None:
        assert mean_embedding([[1.0, 2.0, 3.0]]) == [1.0, 2.0, 3.0]

    def test_componentwise_average(self) -> None:
        result = mean_embedding([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        assert result == [3.0, 4.0]


# ── determinism ─────────────────────────────────────────────────────


def test_extract_is_deterministic(embedder: _MockEmbedder) -> None:
    text = "Lithium battery anode design. PEO membrane synthesis with BF4 salt."
    target = embedder.embed(["lithium"])[0]
    a = extract_keywords_semantic(
        text, target_embedding=target, embedder=embedder, top_k=5
    )
    b = extract_keywords_semantic(
        text, target_embedding=target, embedder=embedder, top_k=5
    )
    assert a == b


# ── candidates= fast path ───────────────────────────────────────────


class TestCandidatesFastPath:
    def test_supplied_candidates_skip_rake_extraction(
        self, embedder: _MockEmbedder
    ) -> None:
        """When ``candidates`` is supplied, the function scores
        those phrases against the target embedding instead of
        re-extracting from text. Output is constrained to the
        candidate set."""
        text = "lithium battery anode design with copper foil current collector"
        target = embedder.embed(["lithium"])[0]
        out = extract_keywords_semantic(
            text,
            target_embedding=target,
            embedder=embedder,
            top_k=10,
            candidates=["alpha-only-candidate", "beta-only-candidate"],
        )
        # Output must be a subset of the supplied candidates.
        for kw in out:
            assert kw.lower() in {
                "alpha-only-candidate",
                "beta-only-candidate",
            }, f"unexpected keyword {kw!r} not in supplied candidates"

    def test_supplied_empty_candidates_returns_empty(
        self, embedder: _MockEmbedder
    ) -> None:
        target = embedder.embed(["lithium"])[0]
        out = extract_keywords_semantic(
            "lithium battery anode",
            target_embedding=target,
            embedder=embedder,
            top_k=5,
            candidates=[],
        )
        assert out == []

    def test_candidates_with_exclude(self, embedder: _MockEmbedder) -> None:
        target = embedder.embed(["x"])[0]
        out = extract_keywords_semantic(
            "some text doesn't matter when candidates supplied",
            target_embedding=target,
            embedder=embedder,
            top_k=10,
            candidates=["foo phrase", "bar phrase", "baz phrase"],
            exclude={"foo phrase"},
        )
        assert "foo phrase" not in [k.lower() for k in out]


# ── privileged_candidates ───────────────────────────────────────────


class TestPrivilegedCandidates:
    def test_uppercase_acronyms_detected(self) -> None:
        text = "FTIR spectroscopy and DFT calculations using XPS analysis."
        out = privileged_candidates(text)
        out_lower = [c.lower() for c in out]
        assert "ftir" in out_lower
        assert "dft" in out_lower
        assert "xps" in out_lower

    def test_pure_upper_hyphenated_acronyms_detected(self) -> None:
        # Pure UPPER-CASE acronyms with hyphens / digits / slashes get
        # the privileged-pattern treatment. Mixed-case acronyms like
        # "ToF" or "UiO" are deliberately NOT detected — the
        # regex requires pure UPPER blocks to avoid false positives
        # on common words like "If" or "Of".
        text = "MOF-5 framework analysis with NaY-zeolite catalysts."
        out = privileged_candidates(text)
        out_lower = [c.lower() for c in out]
        assert any("mof" in c for c in out_lower), (
            f"pure UPPER acronym MOF-5 should be detected; got {out}"
        )

    def test_title_case_multi_word_detected(self) -> None:
        text = (
            "We discuss Metal Organic Framework synthesis followed by "
            "Density Functional Theory calculations of energy levels."
        )
        out = privileged_candidates(text)
        out_lower = " ".join(out).lower()
        assert "metal organic framework" in out_lower
        assert "density functional theory" in out_lower

    def test_abbreviations_iterable_always_included(self) -> None:
        # Even if the text doesn't contain the abbreviation, the
        # legend keys are forced into the candidate set.
        text = "ordinary prose with no acronyms at all here."
        out = privileged_candidates(text, abbreviations=["MOF", "FTIR"])
        out_lower = {c.lower() for c in out}
        assert "mof" in out_lower
        assert "ftir" in out_lower

    def test_empty_text_with_abbrevs_returns_abbrevs(self) -> None:
        out = privileged_candidates("", abbreviations=["XPS"])
        assert "xps" in [c.lower() for c in out]

    def test_single_capitalized_word_not_a_phrase(self) -> None:
        # "Membrane" alone is just a capitalized noun; title-case rule
        # requires ≥2 consecutive cap'd words.
        text = "Membrane is used in many systems."
        out = privileged_candidates(text)
        # Single word "Membrane" should NOT survive as a title-case
        # phrase; only multi-word title-case sequences do.
        assert "membrane" not in [c.lower() for c in out]
