"""Slack identifier -> precis conv slug.

Slug shape:
    slack/<team_id>/<channel_id>/<thread_ts>

Every asa-slack conversation is a thread from message 1 (the bridge
always replies in-thread, never to the channel root — see ``bot.py``),
so ``thread_ts`` is always present: a fresh top-level message's own
``ts`` becomes the thread's ts.
"""

from __future__ import annotations


def compute_slug(*, team_id: str, channel_id: str, thread_ts: str) -> str:
    return f"slack/{team_id}/{channel_id}/{thread_ts}"
