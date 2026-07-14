"""The first-sentence ack must not be duplicated in the final reply.

Regression for gripe #48766: Asa streams her opening sentence as a
standalone "I heard you" message, then the full reply — which opens with
that same sentence — is posted, so the sentence appeared twice. The
caller now strips the leading ack span before posting.
"""

import pytest

# asa_bot.bot imports discord.py (the `[asa]` extra). Skip cleanly where
# it isn't installed — e.g. the container gate image before it bakes
# `[asa]`, or a host subset run — mirroring the habanero/importorskip
# pattern used for the paper extra.
pytest.importorskip("discord")

from asa_bot.bot import _strip_leading_ack


def test_strips_leading_ack_keeps_remainder():
    body = "Looking at the cluster now. Here are the details: all green."
    assert (
        _strip_leading_ack(body, "Looking at the cluster now.")
        == "Here are the details: all green."
    )


def test_whole_reply_is_ack_collapses_to_empty():
    # The ack already delivered the entire reply — nothing left to post.
    body = "Looking at the cluster now."
    assert _strip_leading_ack(body, "Looking at the cluster now.") == ""


def test_restructured_opening_is_left_intact():
    # Model didn't open with the acked sentence — never mangle it.
    body = "A completely different opening, then the body."
    assert _strip_leading_ack(body, "Looking at the cluster now.") == body


def test_no_ack_recorded_is_noop():
    body = "Body with no ack recorded."
    assert _strip_leading_ack(body, "") == body
