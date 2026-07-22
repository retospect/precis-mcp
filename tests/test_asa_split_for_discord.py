"""``_split_for_discord`` must never cut a message mid-URL.

Regression: the morning news briefing is pre-split link-safely by
``briefing._deliver`` (``precis.utils.msgsplit.split_message``, gr51155),
but ``asa_bot.bot._handle_outbound`` re-split each already-safe part again
with its own paragraph-only, hard-character-cut splitter. A digest of
single-newline bullet/link lines (no blank-line separators) is one giant
"paragraph" to that splitter, so a part that crossed its limit got chopped
mid-URL a second time, downstream of the first (already-correct) split.
``_split_for_discord`` now delegates to the same link-safe ``split_message``.
"""

import pytest

# asa_bot.bot imports discord.py (the `[asa]` extra). Skip cleanly where
# it isn't installed — mirrors the habanero/importorskip pattern used for
# the paper extra (see tests/test_ack_dedup.py).
pytest.importorskip("discord")

from asa_bot.bot import _split_for_discord


def test_short_text_passes_through() -> None:
    assert _split_for_discord("hello", limit=1900) == ["hello"]


def test_never_breaks_a_markdown_link_mid_url() -> None:
    # The exact shape a Discord news-briefing digest takes: bullet lines,
    # each with a markdown link, separated by single newlines (no blank
    # lines between items).
    link = (
        "- **UK military explores electric flying taxis** for 2029 "
        "deployment — [link](https://www.theguardian.com/business/2026/jul/"
        "21/uk-military-flying-taxis-2029)"
    )
    body = "\n".join(f"{link}#{i}" for i in range(30))
    assert len(body) > 1900

    parts = _split_for_discord(body, limit=1900)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 1900
        for line in part.split("\n"):
            if line.startswith("- [") or "- **" in line:
                assert line.rstrip(")").split("#")[-1].isdigit() or line.endswith(
                    ")"
                ), f"line cut mid-link: {line!r}"

    # every bullet line survives whole, in order, across the parts
    got = [ln for part in parts for ln in part.split("\n") if ln]
    want = [f"{link}#{i}" for i in range(30)]
    assert got == want
