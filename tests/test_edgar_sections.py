"""Tests for the form→section/item classifier used by the ``edgar`` kind."""

from __future__ import annotations

import pytest

from precis.handlers._edgar_sections import BODY, Section, classify_heading


class TestPeriodicItems:
    """10-K / 10-Q Item headings."""

    def test_risk_factors(self) -> None:
        s = classify_heading("Item 1A. Risk Factors", form="10-K")
        assert s is not None
        assert s.item_code == "1a"
        assert s.canonical_id == "item-1a"
        assert s.section_path == ["Item 1A", "Risk Factors"]

    def test_mdna(self) -> None:
        s = classify_heading(
            "ITEM 7.  MANAGEMENT'S DISCUSSION AND ANALYSIS", form="10-K"
        )
        assert s is not None
        assert s.item_code == "7"
        assert s.canonical_id == "item-7"

    def test_market_risk_7a(self) -> None:
        s = classify_heading("Item 7A — Quantitative Disclosures", form="10-Q")
        assert s is not None
        assert s.item_code == "7a"

    def test_business_item_1(self) -> None:
        s = classify_heading("Item 1. Business", form="10-K")
        assert s is not None
        assert s.item_code == "1"
        assert s.section_path == ["Item 1", "Business"]

    def test_amendment_form_normalised(self) -> None:
        s = classify_heading("Item 1A. Risk Factors", form="10-K/A")
        assert s is not None
        assert s.canonical_id == "item-1a"

    def test_unknown_item_number_still_labelled(self) -> None:
        # Item 99 isn't in the title table but is still a valid boundary.
        s = classify_heading("Item 99. Weird", form="10-K")
        assert s is not None
        assert s.item_code == "99"
        assert s.section_path == ["Item 99"]


class TestEightK:
    """8-K dotted item codes."""

    def test_results_of_operations(self) -> None:
        s = classify_heading("Item 2.02 Results of Operations", form="8-K")
        assert s is not None
        assert s.item_code == "2.02"
        assert s.canonical_id == "item-2.02"
        assert s.section_path[0] == "Item 2.02"

    def test_officer_change(self) -> None:
        s = classify_heading("Item 5.02 Departure of Directors", form="8-K")
        assert s is not None
        assert s.item_code == "5.02"

    def test_bankruptcy(self) -> None:
        s = classify_heading("ITEM 1.03 BANKRUPTCY OR RECEIVERSHIP", form="8-K")
        assert s is not None
        assert s.item_code == "1.03"

    def test_unknown_8k_code_titled_other(self) -> None:
        s = classify_heading("Item 6.99 Nonexistent", form="8-K")
        assert s is not None
        assert s.item_code == "6.99"
        assert s.section_path[1] == "Other"

    def test_periodic_item_not_matched_as_8k(self) -> None:
        # A bare "Item 2" in an 8-K is not a dotted code → no match.
        assert classify_heading("Item 2 Something", form="8-K") is None


class TestS1:
    """S-1 named sections."""

    def test_prospectus_summary(self) -> None:
        s = classify_heading("PROSPECTUS SUMMARY", form="S-1")
        assert s is not None
        assert s.canonical_id == "prospectus-summary"
        assert s.section_path == ["Prospectus Summary"]

    def test_use_of_proceeds(self) -> None:
        s = classify_heading("Use of Proceeds", form="S-1")
        assert s is not None
        assert s.canonical_id == "use-of-proceeds"

    def test_s1_falls_back_to_item(self) -> None:
        # S-1s can also carry Item headings; the periodic matcher covers it.
        s = classify_heading("Item 1A. Risk Factors", form="S-1")
        assert s is not None
        assert s.canonical_id == "item-1a"


class TestNoMatch:
    def test_plain_paragraph_returns_none(self) -> None:
        assert classify_heading("The company sells widgets.", form="10-K") is None

    def test_empty_returns_none(self) -> None:
        assert classify_heading("", form="10-K") is None
        assert classify_heading("   ", form="8-K") is None


class TestBodyConstant:
    def test_body_shape(self) -> None:
        assert isinstance(BODY, Section)
        assert BODY.canonical_id == "body"
        assert BODY.item_code == ""

    def test_section_frozen(self) -> None:
        s = classify_heading("Item 1A. Risk Factors", form="10-K")
        assert s is not None
        with pytest.raises(AttributeError):
            s.item_code = "x"  # type: ignore[misc]
