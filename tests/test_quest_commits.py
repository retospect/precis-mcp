"""Tests for the ``commits`` tick-output contract — gr573805."""

from __future__ import annotations

import re
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import commits as commits_mod


def test_parse_commits_valid_and_dropped() -> None:
    entries, dropped = commits_mod.parse_commits(
        [
            {"kind": "ask", "text": "ask Reto about the credit", "ref": "td1"},
            {"kind": "bogus", "text": "x"},
            {"kind": "account", "text": ""},
            "junk",
        ]
    )
    assert entries == [
        {"kind": "ask", "text": "ask Reto about the credit", "ref": "td1"}
    ]
    assert dropped == 3


def test_parse_commits_non_list_and_cap() -> None:
    assert commits_mod.parse_commits(None) == ([], 0)
    assert commits_mod.parse_commits("x") == ([], 1)
    many = [
        {"kind": "ask", "text": f"a{i}"} for i in range(commits_mod.COMMITS_MAX + 2)
    ]
    entries, dropped = commits_mod.parse_commits(many)
    assert len(entries) == commits_mod.COMMITS_MAX
    assert dropped == 2


def test_apply_commits_logs_entries(store: Any) -> None:
    resp = QuestHandler(hub=Hub(store=store)).put(text="Scout free compute")
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None
    qid = int(m.group(1))
    out = commits_mod.apply_commits(
        store,
        qid,
        [
            {"kind": "lead-ruled-out", "text": "free tier caps at 1h"},
            {"kind": "nope", "text": "x"},
        ],
        by="agent",
    )
    assert out.logbook_entries == 2
    assert out.accepted == 1
    assert out.dropped == 1
    chunks = [
        c for c in store.chunks.list_chunks_for_ref(qid) if c.chunk_kind == "quest_log"
    ]
    texts = [c.text or "" for c in chunks]
    assert any("commit [lead-ruled-out]" in t and "1h" in t for t in texts)
    assert any("dropped 1 malformed" in t for t in texts)
