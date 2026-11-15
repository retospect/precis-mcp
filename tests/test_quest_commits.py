"""Tests for the ``commits`` tick-output contract — gr573805.

Every quest body's tick output accepts an optional ``commits`` list —
the NON-STRUCTURE progress channel (an operational quest's real forward
motion never mints a `structure`): ``{"type": "ask", …}`` mints an
``ask-user`` todo linked ``blocks`` → quest, ``{"type": "account", …}``
records a DISABLED ``llm_account`` lead row (create-only, no credentials —
enabling stays the owner's put door), and ``{"type": "rule-out", …}``
pins a lead ``ruled-out`` in the ledger plus a ``dead-end`` deed. Applied
commits reset the tick's stall counter like a dispatch does.

Runs against real PG (the ``store`` fixture); the model is a canned
payload / scripted ``.complete()``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import commits as commits_mod
from precis.quest.tick import build_tick_prompt, run_quest_tick
from precis.quest.weave_tick import QUEST_BODY_INQUIRY, QUEST_BODY_MATERIALS


def _mk_quest(store: Any, text: str = "A striving") -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _fake_dispatch(payload: dict[str, Any] | None) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text="", error=None, cost_usd=0.01, paused=False
        )

    return _d


def _log_texts(store: Any, qid: int) -> list[str]:
    return [
        b.text or ""
        for b in store.chunks.list_chunks_for_ref(qid)
        if b.chunk_kind == "quest_log"
    ]


def _log_entry(store: Any, qid: int, needle: str) -> Any | None:
    return next(
        (
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log" and needle in (b.text or "")
        ),
        None,
    )


def _open_tags(store: Any, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _account_row(store: Any, name: str) -> dict[str, Any] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name, vendor, plan, adapter, enabled, api_key_ref, "
            "profile, pools, task_classes, promo_expiry, entitlement "
            "FROM llm_account WHERE name = %s",
            (name,),
        ).fetchone()
    if row is None:
        return None
    cols = (
        "name",
        "vendor",
        "plan",
        "adapter",
        "enabled",
        "api_key_ref",
        "profile",
        "pools",
        "task_classes",
        "promo_expiry",
        "entitlement",
    )
    return dict(zip(cols, row, strict=True))


# ── parse_commits (the contract's lenient reader) ─────────────────────


class TestParseCommits:
    def test_none_and_missing(self) -> None:
        assert commits_mod.parse_commits(None) == ([], 0)

    def test_non_list_dropped(self) -> None:
        for raw in ("ask", 42, {"type": "ask"}, True):
            commits, dropped = commits_mod.parse_commits(raw)
            assert commits == [] and dropped == 1

    def test_valid_entries(self) -> None:
        raw = [
            {"type": "ask", "question": "approve the groq free tier?"},
            {
                "type": "account",
                "name": "groq-free",
                "vendor": "groq",
                "plan": "free",
                "adapter": "api",
            },
            {"type": "rule-out", "lead": "huggingface free", "reason": "no SLA"},
        ]
        commits, dropped = commits_mod.parse_commits(raw)
        assert dropped == 0
        assert [c.type for c in commits] == ["ask", "account", "rule-out"]

    def test_type_aliases(self) -> None:
        for spell in ("rule_out", "ruleout", "ruled-out"):
            commits, dropped = commits_mod.parse_commits(
                [{"type": spell, "lead": "x", "reason": "y"}]
            )
            assert dropped == 0 and commits[0].type == "rule-out"
        commits, dropped = commits_mod.parse_commits(
            [
                {
                    "type": "account-lead",
                    "name": "n",
                    "vendor": "v",
                    "plan": "p",
                    "adapter": "a",
                }
            ]
        )
        assert dropped == 0 and commits[0].type == "account"

    def test_missing_floor_dropped(self) -> None:
        raw = [
            {"type": "bogus", "question": "q"},
            {"type": "ask"},  # no question
            {"type": "account", "name": "n"},  # missing vendor/plan/adapter
            {"type": "rule-out", "lead": "x"},  # no reason
            {"type": "rule-out", "reason": "y"},  # no lead
            "a bare string",
        ]
        commits, dropped = commits_mod.parse_commits(raw)
        assert commits == [] and dropped == len(raw)

    def test_count_cap(self) -> None:
        raw = [
            {"type": "rule-out", "lead": f"lead-{i}", "reason": "r"}
            for i in range(commits_mod.COMMITS_MAX + 2)
        ]
        commits, dropped = commits_mod.parse_commits(raw)
        assert len(commits) == commits_mod.COMMITS_MAX
        assert dropped == 2

    def test_field_lengths_capped(self) -> None:
        commits, dropped = commits_mod.parse_commits(
            [{"type": "rule-out", "lead": "x" * 500, "reason": "y" * 500}]
        )
        assert dropped == 0
        assert len(commits[0].data["lead"]) == commits_mod.FIELD_CHARS
        assert len(commits[0].data["reason"]) == commits_mod.FIELD_CHARS


# ── the tick prompts carry the contract ───────────────────────────────


class TestCommitsPrompt:
    def test_materials_prompt(self, store: Any) -> None:
        qid = _mk_quest(store)
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, quest_body=QUEST_BODY_MATERIALS)
        assert '"commits"' in prompt
        assert '"rule-out"' in prompt
        assert "NON-STRUCTURE progress channel" in prompt

    def test_inquiry_prompt(self, store: Any) -> None:
        qid = _mk_quest(store)
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, quest_body=QUEST_BODY_INQUIRY)
        assert '"commits"' in prompt
        assert "NON-STRUCTURE progress channel" in prompt


# ── tick apply: malformed drops with a note, never fails ──────────────


class TestTickCommits:
    def test_malformed_commits_drops_with_logbook_note(self, store: Any) -> None:
        qid = _mk_quest(store)
        payload = {
            "logbook": [{"entry_type": "observation", "text": "obs"}],
            "commits": "an-ask",  # not a list — malformed
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert any("malformed `commits`" in t for t in _log_texts(store, qid))

    def test_commits_only_payload_is_a_payload(self, store: Any) -> None:
        # `commits` is in _PAYLOAD_KEYS — a commits-only reply must parse
        # as a tick payload, not fail as "unparseable model output".
        qid = _mk_quest(store)
        payload = {"commits": [{"type": "rule-out", "lead": "lead-x", "reason": "r"}]}
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"

    def test_tick_without_commits_unchanged(self, store: Any) -> None:
        qid = _mk_quest(store)
        payload = {
            "logbook": [{"entry_type": "observation", "text": "plain obs"}],
            "dossier_text": "short rewrite",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert not any("commit:" in t for t in _log_texts(store, qid))


# ── ask: the ask-user todo + blocks edge ──────────────────────────────


def _ask_commit(question: str = "Approve the groq free tier?") -> dict[str, Any]:
    return {
        "type": "ask",
        "question": question,
        "options": ["approve groq-free", "skip it"],
        "context": "lead qualified; needs a yes/no",
    }


class TestAskCommit:
    def test_ask_mints_ask_user_todo(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [_ask_commit()]})
        )
        assert out.status == "succeeded"
        todos = store.list_refs(kind="todo")
        assert len(todos) == 1
        todo = todos[0]
        assert "Approve the groq free tier?" in todo.title
        assert "ask-user" in _open_tags(store, todo.id)
        ask = (todo.meta or {}).get("ask") or {}
        assert ask.get("kind") == "ruling"
        assert ask.get("blocks") == "nothing"
        # the quest reads blocked-by the ask (canonical blocks edge)
        with store.pool.connection() as conn:
            edge = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'blocks'",
                (todo.id, qid),
            ).fetchone()
        assert edge is not None
        assert any(f"td{todo.id}" in t for t in _log_texts(store, qid))

    def test_ask_dedups_on_open_question(self, store: Any) -> None:
        qid = _mk_quest(store)
        run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [_ask_commit()]})
        )
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {"commits": [_ask_commit("approve the groq free tier?")]}
            ),
        )
        assert out.status == "succeeded"
        assert len(store.list_refs(kind="todo")) == 1
        assert any("already queued" in t for t in _log_texts(store, qid))

    def test_ask_counted_as_forward_motion(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [_ask_commit()]})
        )
        assert out.status == "succeeded"
        assert out.commits_applied == 1

    def test_ask_commit_resets_the_stall(self, store: Any) -> None:
        """gr573805 — an applied commit is the agent ACTING: a stalled
        quest that commits real work never reaches the structure-forcing
        ladder."""
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"ticks_since_experiment": 1})
        out = run_quest_tick(
            store,
            qid,
            compute=True,
            dispatch_fn=_fake_dispatch({"commits": [_ask_commit()]}),
        )
        assert out.status == "succeeded"
        qref = store.get_ref(kind="quest", id=qid)
        assert qref.meta.get("ticks_since_experiment") == 0


# ── account: the disabled llm_account lead row ─────────────────────────


def _account_commit(name: str = "groq-free") -> dict[str, Any]:
    return {
        "type": "account",
        "name": name,
        "vendor": "groq",
        "plan": "free tier",
        "adapter": "api",
        "promo_expiry": "2027-01-31",
        "models": ["llama-3.3-70b"],
        "note": "launch promo — free while it lasts",
    }


class TestAccountCommit:
    @pytest.fixture(autouse=True)
    def _cleanup_lead_rows(self, store: Any) -> Any:
        """``llm_account`` is a _PRESERVE_TABLES seeded-ledger table —
        fixture TRUNCATE never touches it, so quest-minted lead rows
        would leak across tests (and the whole session's clone). The
        ``quest_lead`` entitlement marker makes the cleanup precise:
        only commit-minted rows, never a seeded account."""
        yield
        with store.tx() as conn:
            conn.execute(
                "DELETE FROM llm_account "
                "WHERE name = ANY(%s) "
                "AND entitlement -> 'quest_lead' IS NOT NULL",
                (["groq-free", "hf-free"],),
            )

    def test_account_records_disabled_row(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [_account_commit()]})
        )
        assert out.status == "succeeded"
        row = _account_row(store, "groq-free")
        assert row is not None
        assert row["vendor"] == "groq"
        assert row["plan"] == "free tier"
        assert row["adapter"] == "api"
        assert row["enabled"] is False
        # The narrow door: no credential, profile, pool or class ever
        # rides a commit — enabling stays the owner's put.
        assert row["api_key_ref"] is None
        assert row["profile"] is None
        assert row["pools"] == []
        assert row["task_classes"] == []
        assert str(row["promo_expiry"]) == "2027-01-31"
        assert row["entitlement"]["quest_lead"]["quest"] == f"qu{qid}"
        assert "launch promo" in row["entitlement"]["quest_lead"]["note"]

    def test_account_create_only_conflict_notes(self, store: Any) -> None:
        qid = _mk_quest(store)
        run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [_account_commit()]})
        )
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch({"commits": [_account_commit()]}),
        )
        assert out.status == "succeeded"
        with store.pool.connection() as conn:
            (n_rows,) = conn.execute(
                "SELECT count(*) FROM llm_account WHERE name = 'groq-free'"
            ).fetchone()
        assert n_rows == 1
        assert any("already recorded" in t for t in _log_texts(store, qid))

    def test_account_bad_adapter_refused(self, store: Any) -> None:
        qid = _mk_quest(store)
        commit = _account_commit()
        commit["adapter"] = "not-an-adapter"
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"commits": [commit]})
        )
        assert out.status == "succeeded"
        assert _account_row(store, "groq-free") is None
        assert any("refused" in t for t in _log_texts(store, qid))
        # A refused commit wrote no artifact — it buys no forward motion:
        # a model emitting refusable commits every tick must still stall.
        assert out.commits_applied == 0


# ── rule-out: pinned ledger + dead-end deed ────────────────────────────


class TestRuleOutCommit:
    def test_rule_out_pins_and_deeds(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {
                    "commits": [
                        {
                            "type": "rule-out",
                            "lead": "huggingface free inference",
                            "reason": "no SLA and gated models",
                        }
                    ]
                }
            ),
        )
        assert out.status == "succeeded"
        entry = _log_entry(store, qid, "commit: rule-out huggingface free inference")
        assert entry is not None
        assert (entry.meta or {}).get("entry_type") == "dead-end"
        # the pinned ledger node carries the ruled-out status
        from precis.quest.dossier import read_ledger

        ledger = read_ledger(store, qid)
        assert "huggingface free inference" in ledger
        assert "ruled-out" in ledger

    def test_rule_out_dedups(self, store: Any) -> None:
        qid = _mk_quest(store)
        commit = {"type": "rule-out", "lead": "hf free", "reason": "no SLA"}
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"commits": [commit]}))
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"commits": [commit]}))
        dead_ends = [
            b
            for b in store.chunks.list_chunks_for_ref(qid)
            if b.chunk_kind == "quest_log"
            and (b.meta or {}).get("entry_type") == "dead-end"
        ]
        assert len(dead_ends) == 1
        assert any("already ruled out" in t for t in _log_texts(store, qid))


# ── the needs capability hint: declared, not re-filed ─────────────────


class TestNeedsCapabilityHint:
    """A tick re-declaring the missing "non-materials commit artifact"
    (the exact declaration gr573805 was filed for) is answered with the
    `commits` affordance — not a duplicate missing-tool gripe."""

    def test_commit_artifact_need_files_no_gripe(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {
                    "needs": [
                        {
                            "tool": "non-materials 'commit' artifact type for "
                            "operational quests (e.g. queue-an-ask)",
                            "why": "deliverables have no commit channel",
                            "would_unblock": "persisting operational artifacts",
                        }
                    ]
                }
            ),
        )
        assert out.status == "succeeded"
        assert store.list_refs(kind="gripe") == []
        texts = _log_texts(store, qid)
        assert any("already exists" in t and "commits" in t for t in texts)
