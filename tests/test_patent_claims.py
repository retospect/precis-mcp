"""Unit tests for the patent claim-structure heuristic (slice 1).

See ``docs/design/patent-authoring-loop.md`` and
``src/precis/handlers/_patent_claims.py``.
"""

from __future__ import annotations

from precis.handlers._patent_claims import (
    DESCRIPTION_BLOCK_META,
    claim_block_meta,
    classify_claim,
)


class TestIndependent:
    def test_bare_independent(self) -> None:
        s = classify_claim("1. A method for X, comprising: a step of Y.", 1)
        assert s.independent is True
        assert s.depends_on == []

    def test_independent_even_if_it_mentions_magnitudes(self) -> None:
        # "0.5 to 2.0 weight percent" must NOT be read as a claim reference —
        # the dependency regex anchors on the word "claim".
        s = classify_claim(
            "3. A composition comprising a catalyst at 0.5 to 2.0 weight percent.",
            3,
        )
        assert s.independent is True
        assert s.depends_on == []


class TestDependent:
    def test_single_antecedent(self) -> None:
        s = classify_claim("2. The system of claim 1, wherein Z.", 2)
        assert s.independent is False
        assert s.depends_on == [1]

    def test_range_dependency(self) -> None:
        s = classify_claim("6. The method of any of claims 1 to 3, further X.", 6)
        assert s.independent is False
        assert s.depends_on == [1, 2, 3]

    def test_hyphen_range(self) -> None:
        s = classify_claim("6. The method of claims 2-4.", 6)
        assert s.depends_on == [2, 3, 4]

    def test_or_and_list(self) -> None:
        s = classify_claim("5. The apparatus of claim 1 or 2.", 5)
        assert s.depends_on == [1, 2]
        s2 = classify_claim("7. The apparatus of claims 1, 3 and 5.", 7)
        assert s2.depends_on == [1, 3, 5]

    def test_preceding_claim(self) -> None:
        s = classify_claim("4. The device of any preceding claim, wherein W.", 4)
        assert s.independent is False
        assert s.depends_on == [1, 2, 3]

    def test_forward_and_self_reference_ignored(self) -> None:
        # A reference to a same/higher-numbered claim is not a dependency
        # (defensive — well-formed claims never forward-reference).
        s = classify_claim("2. The method of claim 5.", 2)
        assert s.independent is True
        assert s.depends_on == []


class TestBlockMeta:
    def test_claim_marker_shape(self) -> None:
        assert claim_block_meta("2. The system of claim 1.", 2) == {
            "patent_block": "claim",
            "claim_number": 2,
            "claim_independent": False,
            "depends_on": [1],
        }

    def test_description_marker(self) -> None:
        assert DESCRIPTION_BLOCK_META == {"patent_block": "description"}
