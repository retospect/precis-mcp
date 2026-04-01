"""Tests for formatting utilities."""

from precis.formatting import group_paragraphs


class TestGroupParagraphs:
    def test_simple_lines(self):
        text = "First paragraph.\n\nSecond paragraph."
        assert group_paragraphs(text) == ["First paragraph.", "Second paragraph."]

    def test_single_bib(self):
        text = "[@smith2020]: Smith, J. Title. Journal, 2020."
        assert group_paragraphs(text) == [
            "[@smith2020]: Smith, J. Title. Journal, 2020."
        ]

    def test_multiple_bibs_one_line(self):
        """Consecutive bib definitions on one line should be split."""
        text = (
            "[@smith2020]: Smith, J. Title. Journal, 2020. "
            "[@jones2021]: Jones, A. Other. Nature, 2021."
        )
        result = group_paragraphs(text)
        assert len(result) == 2
        assert result[0].startswith("[@smith2020]:")
        assert result[1].startswith("[@jones2021]:")

    def test_multiple_bibs_separate_lines(self):
        """Bib definitions on separate lines should stay separate."""
        text = (
            "[@smith2020]: Smith, J. Title. Journal, 2020.\n"
            "[@jones2021]: Jones, A. Other. Nature, 2021."
        )
        result = group_paragraphs(text)
        assert len(result) == 2

    def test_text_before_bibs(self):
        """Text before bib definitions should be a separate chunk."""
        text = (
            "## References\n"
            "[@smith2020]: Smith, J. Title. Journal, 2020. "
            "[@jones2021]: Jones, A. Other. Nature, 2021."
        )
        result = group_paragraphs(text)
        assert result[0] == "## References"
        assert len(result) == 3

    def test_list_grouping_preserved(self):
        """List items should still be grouped together."""
        text = "- item one\n- item two\n- item three"
        result = group_paragraphs(text)
        assert len(result) == 1
        assert "item one" in result[0]
        assert "item three" in result[0]

    def test_five_bibs_one_line(self):
        """Real-world case: five bib entries crammed onto one line."""
        text = "[@a]: A ref. [@b]: B ref. [@c]: C ref. [@d]: D ref. [@e]: E ref."
        result = group_paragraphs(text)
        assert len(result) == 5
