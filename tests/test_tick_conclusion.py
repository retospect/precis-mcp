"""Contract tests for :mod:`precis.utils.tick_conclusion`."""

from __future__ import annotations

from precis.utils.tick_conclusion import parse


class TestParse:
    def test_returns_none_when_block_absent(self) -> None:
        assert parse("just some output with no block at all") is None
        assert parse("") is None

    def test_returns_none_on_empty_input(self) -> None:
        assert parse("") is None

    def test_minimal_block(self) -> None:
        stdout = """
        prefix output...

        === TICK CONCLUSION ===
        verdict: done
        summary: Wrote the intro section.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "done"
        assert out.summary == "Wrote the intro section."
        assert out.files == []

    def test_multi_line_summary(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: continue
        summary: First line of summary.
        Second line continues the synth.
        Third line wraps it.
        files: tex/intro.tex
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "continue"
        assert "First line" in out.summary
        assert "Second line" in out.summary
        assert "Third line" in out.summary
        assert out.files == ["tex/intro.tex"]

    def test_files_comma_separated(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: done
        summary: Wrote three files.
        files: tex/intro.tex, tex/methods.tex, tex/results.tex
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.files == ["tex/intro.tex", "tex/methods.tex", "tex/results.tex"]

    def test_unknown_verdict_kept_as_none(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: ambivalent
        summary: I'm not sure how I feel.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict is None
        assert out.summary == "I'm not sure how I feel."

    def test_yield_verdict_recognised(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: yield
        summary: Need a value judgement from the human.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "yield"

    def test_halt_verdict_recognised(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: halt
        summary: Impossible as specified.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "halt"

    def test_last_block_wins(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        verdict: continue
        summary: First draft of the synth.
        === END ===

        Actually let me reconsider...

        === TICK CONCLUSION ===
        verdict: done
        summary: The real synth.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "done"
        assert out.summary == "The real synth."

    def test_case_insensitive_keys_and_delimiters(self) -> None:
        stdout = """
        === tick conclusion ===
        Verdict: Done
        Summary: Mixed case keys parse fine.
        === end ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict == "done"
        assert out.summary == "Mixed case keys parse fine."

    def test_summary_alone(self) -> None:
        stdout = """
        === TICK CONCLUSION ===
        summary: Just a summary, no verdict.
        === END ===
        """
        out = parse(stdout)
        assert out is not None
        assert out.verdict is None
        assert out.summary == "Just a summary, no verdict."
        assert out.files == []
