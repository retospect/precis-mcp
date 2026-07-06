"""Unit tests for the Discord message splitter (gr51155).

Regression guard: a morning briefing over Discord's 2000-char limit was
posted verbatim by asa_bot and truncated mid-URL, dropping the tail of the
digest. ``split_message`` must break long text into parts that each fit,
never severing a line (and therefore never a markdown link) when the line
itself fits a part.
"""

from __future__ import annotations

from precis.utils.msgsplit import DEFAULT_LIMIT, split_message


def test_empty_and_short_passthrough() -> None:
    assert split_message("") == []
    assert split_message("   ") == []
    assert split_message("hello") == ["hello"]


def test_short_text_is_single_part() -> None:
    text = "line one\nline two\n\nline three"
    assert split_message(text) == [text]


def test_long_text_splits_under_limit() -> None:
    para = ("x" * 200 + "\n") * 40  # ~8k chars, well over the limit
    parts = split_message(para, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    # every original line preserved (order + content), modulo blank trims
    joined_lines = [ln for ln in "\n".join(parts).split("\n") if ln]
    assert joined_lines == ["x" * 200] * 40


def test_never_breaks_a_markdown_link_that_fits() -> None:
    # The gr51155 failure mode: a link line cut mid-URL. Build a body of many
    # link lines that overflows several parts; assert no part ends mid-URL.
    link = "- [Evacuations in Guam as super typhoon approaches](https://www.example.com/news/guam-typhoon-bavi-evacuations-live-updates)"
    body = "\n".join(f"{link}#{i}" for i in range(60))
    parts = split_message(body, limit=500)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 500
        for line in p.split("\n"):
            # a link line is intact iff it still closes its markdown paren
            if line.startswith("- ["):
                assert line.endswith(")") or line[-1].isdigit()
    # and the full set reconstructs every link line
    got = [ln for part in parts for ln in part.split("\n") if ln]
    assert got == [f"{link}#{i}" for i in range(60)]


def test_single_line_longer_than_limit_word_splits_keeping_urls() -> None:
    url = "https://example.com/" + "a" * 300
    line = " ".join([url] * 20)  # one line, no newlines, far over limit
    parts = split_message(line, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    # the URL token (< limit) is never cut: it appears whole in some part
    assert any(url in p for p in parts)
    # word-splitting means each part is made of whole url tokens
    for p in parts:
        for tok in p.split(" "):
            assert tok == url


def test_pathological_token_longer_than_limit_hard_cuts() -> None:
    blob = "z" * 5000  # a single space-free token over any budget
    parts = split_message(blob, limit=1000)
    assert len(parts) == 5
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts) == blob


def test_default_limit_is_under_discord_cap() -> None:
    assert DEFAULT_LIMIT < 2000
