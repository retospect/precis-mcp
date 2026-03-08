"""Tests for formatting.py — Markdown ↔ DOCX run conversion."""

from precis.formatting import FormattedRun, markdown_to_runs, runs_to_markdown


class TestRunsToMarkdown:
    def test_plain(self):
        runs = [FormattedRun(text="hello")]
        assert runs_to_markdown(runs) == "hello"

    def test_bold(self):
        runs = [FormattedRun(text="bold", bold=True)]
        assert runs_to_markdown(runs) == "**bold**"

    def test_italic(self):
        runs = [FormattedRun(text="ital", italic=True)]
        assert runs_to_markdown(runs) == "*ital*"

    def test_bold_italic(self):
        runs = [FormattedRun(text="both", bold=True, italic=True)]
        assert runs_to_markdown(runs) == "***both***"

    def test_superscript(self):
        runs = [FormattedRun(text="2", superscript=True)]
        assert runs_to_markdown(runs) == "<sup>2</sup>"

    def test_subscript(self):
        runs = [FormattedRun(text="i", subscript=True)]
        assert runs_to_markdown(runs) == "<sub>i</sub>"

    def test_strike(self):
        runs = [FormattedRun(text="old", strike=True)]
        assert runs_to_markdown(runs) == "~~old~~"

    def test_hyperlink(self):
        runs = [FormattedRun(text="click", url="http://example.com")]
        assert runs_to_markdown(runs) == "[click](http://example.com)"

    def test_mixed(self):
        runs = [
            FormattedRun(text="We found "),
            FormattedRun(text="significant", bold=True),
            FormattedRun(text=" results with p"),
            FormattedRun(text="<0.05", superscript=False),
        ]
        result = runs_to_markdown(runs)
        assert "**significant**" in result
        assert result.startswith("We found ")


class TestMarkdownToRuns:
    def test_plain(self):
        runs = markdown_to_runs("hello world")
        assert len(runs) == 1
        assert runs[0].text == "hello world"
        assert not runs[0].bold

    def test_bold(self):
        runs = markdown_to_runs("some **bold** text")
        assert len(runs) == 3
        assert runs[1].text == "bold"
        assert runs[1].bold

    def test_italic(self):
        runs = markdown_to_runs("some *italic* text")
        assert any(r.italic and r.text == "italic" for r in runs)

    def test_superscript(self):
        runs = markdown_to_runs("x<sup>2</sup>")
        assert any(r.superscript and r.text == "2" for r in runs)

    def test_subscript(self):
        runs = markdown_to_runs("H<sub>2</sub>O")
        assert any(r.subscript and r.text == "2" for r in runs)

    def test_strike(self):
        runs = markdown_to_runs("~~deleted~~")
        assert any(r.strike and r.text == "deleted" for r in runs)

    def test_hyperlink(self):
        runs = markdown_to_runs("[link](http://example.com)")
        assert any(r.url == "http://example.com" and r.text == "link" for r in runs)

    def test_empty(self):
        assert markdown_to_runs("") == []

    def test_no_formatting(self):
        runs = markdown_to_runs("plain text here")
        assert len(runs) == 1
        assert runs[0].text == "plain text here"
