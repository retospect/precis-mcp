"""Tests for the precis-fix loop (slice 2.5).

Pure + LLM-mocked parts run everywhere; the write-back tests use a local
`.anki2` and skip without the optional `anki` wheel.
"""

from __future__ import annotations

import pytest

from precis.anki.fix import (
    FIX_TAG,
    FIXED_TAG,
    FixRequest,
    _extract_instruction,
    build_fix_prompt,
    propose_fix,
)


class TestPure:
    def test_extract_instruction_priority(self) -> None:
        assert _extract_instruction({"precis-fix": "do X", "Back Extra": "y"}) == "do X"
        assert _extract_instruction({"Back Extra": "  fix the date "}) == "fix the date"
        assert _extract_instruction({"Text": "just a card"}) == ""

    def test_build_fix_prompt_includes_card_and_instruction(self) -> None:
        req = FixRequest(
            note_id=1,
            guid="g",
            notetype="Cloze",
            fields={"Text": "The {{c1::heart}} pumps."},
            instruction="the answer should be 'left ventricle'",
        )
        p = build_fix_prompt(req)
        assert "left ventricle" in p
        assert "{{c1::heart}}" in p
        assert "CLOZE" in p  # cloze-specific guidance included
        assert "JSON" in p

    def test_propose_fix_filters_hallucinated_and_unchanged(self, monkeypatch) -> None:
        import precis.utils.claude_p as cp
        from precis.utils.llm import router

        req = FixRequest(
            note_id=1,
            guid="g",
            notetype="Cloze",
            fields={"Text": "old", "Back Extra": "keep"},
            instruction="fix it",
        )

        def fake_call(prompt, **kw):
            return cp.ClaudePResult(
                data={
                    "Text": "new corrected",  # valid change
                    "Back Extra": "keep",  # unchanged → dropped
                    "Nonexistent": "x",  # hallucinated field → dropped
                },
                raw_stdout="",
                cost_usd=None,
            )

        # propose_fix routes through the ADR 0046 router (unit 4b); stub the
        # subprocess helper the CLOUD_SMALL/claude_p transport wraps, so the
        # real dispatch → ClaudePProvider → result_from_claude_p path runs and
        # LlmResult.data carries the parsed dict.
        monkeypatch.setattr(router, "call_claude_p", fake_call)
        out = propose_fix(req)
        assert out == {"Text": "new corrected"}


# ── write-back — needs the anki pylib, local only ─────────────────────────


@pytest.fixture
def col(tmp_path):
    pytest.importorskip("anki")
    from anki.collection import Collection

    c = Collection(str(tmp_path / "fix.anki2"))
    yield c
    c.close()


def _add_tagged_cloze(col, text, instruction):
    cloze = col.models.by_name("Cloze")
    did = col.decks.id("Default")
    note = col.new_note(cloze)
    note["Text"] = text
    note["Back Extra"] = instruction
    note.tags = [FIX_TAG]
    col.add_note(note, did)
    return note.id


class TestFindAndApply:
    def test_find_fix_requests(self, col) -> None:
        from precis.anki.fix import find_fix_requests

        _add_tagged_cloze(col, "The {{c1::heart}} pumps.", "answer is left ventricle")
        reqs = find_fix_requests(col)
        assert len(reqs) == 1
        assert reqs[0].instruction == "answer is left ventricle"
        assert reqs[0].notetype == "Cloze"

    def test_apply_fix_writes_back_and_swaps_tag(self, col) -> None:
        from precis.anki.fix import apply_fix

        nid = _add_tagged_cloze(col, "The {{c1::heart}} pumps.", "fix answer")
        changed = apply_fix(
            col, nid, {"Text": "The {{c1::left ventricle}} pumps oxygenated blood."}
        )
        assert changed is True
        note = col.get_note(nid)
        assert "left ventricle" in note["Text"]
        assert FIX_TAG not in note.tags
        assert FIXED_TAG in note.tags

    def test_apply_fix_swaps_tag_even_with_no_field_change(self, col) -> None:
        """A no-op LLM result still retires the request so it doesn't re-queue."""
        from precis.anki.fix import apply_fix

        nid = _add_tagged_cloze(col, "The {{c1::x}} y.", "nothing really")
        changed = apply_fix(col, nid, {})  # LLM proposed no field change
        assert changed is True  # the tag swap alone is a change
        note = col.get_note(nid)
        assert FIX_TAG not in note.tags and FIXED_TAG in note.tags
