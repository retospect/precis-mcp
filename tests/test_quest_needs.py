"""Tests for the ``needs`` tick-output contract — gr485483.

Every quest body's tick output accepts an optional ``needs`` list of
``{tool, why, would_unblock}`` — the model declares a missing tool / data
source / sim lane / catalog entry, and the executor (code, never the
model) turns it into a deduped, rate-limited gripe linked ``blocks`` →
quest. ``quest_gaps`` surfaces the open ones and the dry-rest alert cites
them.

Runs against real PG (the ``store`` fixture); the model is a canned
payload / scripted ``.complete()``.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import needs as needs_mod
from precis.quest import roadmap_tick as rt
from precis.quest.gaps import Gap
from precis.quest.tick import build_tick_prompt, run_quest_tick
from precis.quest.weave_tick import QUEST_BODY_INQUIRY, QUEST_BODY_MATERIALS
from tests.test_quest_roadmap_ledger import make_root


class _ScriptedClient:
    """``.complete(messages)`` returns the next scripted JSON payload —
    the same stub ``test_quest_roadmap`` uses, kept local so this module
    doesn't import the ``precis_se`` chain."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.prompts.append("\n".join(m["content"] for m in messages))
        payload = self.replies.pop(0) if self.replies else {}
        return SimpleNamespace(text=json.dumps(payload), data=None)


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


def _gripes(store: Any) -> list[Any]:
    return store.list_refs(kind="gripe")


# ── parse_needs (the contract's lenient reader) ───────────────────────


class TestParseNeeds:
    def test_none_and_missing(self) -> None:
        assert needs_mod.parse_needs(None) == ([], 0)

    def test_valid_entries(self) -> None:
        raw = [
            {"tool": "XRD scanner", "why": "need lattice", "would_unblock": "verify"},
            {"tool": "autocatpath-batch"},
        ]
        needs, dropped = needs_mod.parse_needs(raw)
        assert dropped == 0
        assert [n.tool for n in needs] == ["XRD scanner", "autocatpath-batch"]
        assert needs[0].tool_key == "xrd-scanner"
        assert needs[1].why == ""

    def test_non_list_dropped(self) -> None:
        for raw in ("xrd", 42, {"tool": "x"}, True):
            needs, dropped = needs_mod.parse_needs(raw)
            assert needs == [] and dropped == 1

    def test_malformed_entries_dropped_individually(self) -> None:
        raw = [
            {"tool": "good-one"},
            "a bare string",
            {"tool": "good-two", "why": "w", "would_unblock": "u"},
        ]
        needs, dropped = needs_mod.parse_needs(raw)
        assert [n.tool for n in needs] == ["good-one", "good-two"]
        assert dropped == 1

    def test_toolless_entries_dropped(self) -> None:
        raw = [
            {"why": "no tool key"},
            {"tool": "   "},
            {"tool": "!!!"},  # slugs to nothing
        ]
        needs, dropped = needs_mod.parse_needs(raw)
        assert needs == [] and dropped == 3

    def test_count_cap(self) -> None:
        raw = [{"tool": f"tool-{i}"} for i in range(needs_mod.NEEDS_MAX + 2)]
        needs, dropped = needs_mod.parse_needs(raw)
        assert len(needs) == needs_mod.NEEDS_MAX
        assert dropped == 2

    def test_field_lengths_capped(self) -> None:
        needs, dropped = needs_mod.parse_needs(
            [{"tool": "t" * 500, "why": "w" * 500, "would_unblock": "u" * 500}]
        )
        assert dropped == 0
        n = needs[0]
        assert len(n.tool) == needs_mod.TOOL_CHARS
        assert len(n.why) == needs_mod.FIELD_CHARS
        assert len(n.would_unblock) == needs_mod.FIELD_CHARS
        assert len(n.tool_key) <= 60

    def test_tool_key_normalisation(self) -> None:
        assert needs_mod.tool_key("XRD Scanner (v2)") == "xrd-scanner-v2"
        assert needs_mod.tool_key("  autocatpath  batch  ") == "autocatpath-batch"


# ── the tick prompts carry the contract ───────────────────────────────


class TestNeedsPrompt:
    _LINE = "say so in `needs` instead of guessing around it"

    def test_materials_prompt(self, store: Any) -> None:
        qid = _mk_quest(store)
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, quest_body=QUEST_BODY_MATERIALS)
        assert self._LINE in prompt
        assert '"needs"' in prompt
        assert "would_unblock" in prompt

    def test_inquiry_prompt(self, store: Any) -> None:
        qid = _mk_quest(store)
        quest = store.get_ref(kind="quest", id=qid)
        prompt = build_tick_prompt(store, quest, quest_body=QUEST_BODY_INQUIRY)
        assert self._LINE in prompt
        assert '"needs"' in prompt

    def test_roadmap_role_prompts(self, store: Any) -> None:
        # demand role (no demand set) and bridge role (demand > supply)
        cases: list[dict[str, Any]] = [{"demand": None, "supply": None}, {}]
        for kwargs in cases:
            root, _cap = make_root(store, **kwargs)
            result = rt.roadmap_tick(Hub(store=store), None, root, dry_run=True)
            assert self._LINE in result["prompt"], result["role"]
            assert '"needs"' in result["prompt"]

    def test_roadmap_supply_findings_prompt(self, store: Any) -> None:
        paper: Any = SimpleNamespace(id=1, title="P")
        cards = [rt.SupplyCard(paper=paper, text="t", chunk_id=1)]
        choice = rt.RoleChoice(
            role=rt.ROLE_SUPPLY,
            capability_id=1,
            capability="qu1",
            capability_title="c",
            key="k",
            sense="min",
            unit=None,
            demanded=1.0,
            best_supply=None,
            best_supply_evidence=(),
            gap=Gap(kind="no-supply", detail="k: none", handle="qu1"),
        )
        prompt = rt._supply_findings_prompt(store, choice, cards)
        assert self._LINE in prompt
        assert '"needs"' in prompt

    def test_weave_prompt(self) -> None:
        from precis.quest.weave import _build_prompt

        prompt = _build_prompt("Section", "(empty)", [])
        assert self._LINE in prompt
        assert '"needs"' in prompt


# ── tick apply: malformed drops with a note, never fails ──────────────


class TestTickNeeds:
    def test_malformed_needs_drops_with_logbook_note(self, store: Any) -> None:
        qid = _mk_quest(store)
        payload = {
            "logbook": [{"entry_type": "observation", "text": "obs"}],
            "needs": "an-xrd-scanner",  # not a list — malformed
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert any("malformed `needs`" in t for t in _log_texts(store, qid))

    def test_mixed_needs_valid_ones_apply(self, store: Any) -> None:
        qid = _mk_quest(store)
        payload = {
            "needs": [
                {"tool": "xrd scanner", "why": "w", "would_unblock": "u"},
                {"no_tool": True},
            ]
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert any("malformed `needs`" in t for t in _log_texts(store, qid))

    def test_tick_without_needs_unchanged(self, store: Any) -> None:
        qid = _mk_quest(store)
        payload = {
            "logbook": [{"entry_type": "observation", "text": "plain obs"}],
            "dossier_text": "short rewrite",
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert not any("needs" in t for t in _log_texts(store, qid))

    def test_needs_only_payload_is_a_payload(self, store: Any) -> None:
        # `needs` is in _PAYLOAD_KEYS — a needs-only reply must parse as a
        # tick payload, not fail as "unparseable model output".
        qid = _mk_quest(store)
        payload = {"needs": [{"tool": "xrd scanner"}]}
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"

    def test_roadmap_reply_needs_malformed_notes_the_root(self, store: Any) -> None:
        root, _cap = make_root(store, demand=None, supply=None)  # demand role
        client = _ScriptedClient({"needs": [{"why": "no tool"}]})
        result = rt.roadmap_tick(Hub(store=store), client, root)
        assert result["ok"]
        assert any("malformed `needs`" in t for t in _log_texts(store, root))


# ── the executor files the gripe (code, not the model) ────────────────


def _gripe_body(store: Any, gripe_id: int) -> str:
    for b in store.chunks.list_chunks_for_ref(gripe_id):
        if b.chunk_kind == "gripe_body":
            return b.text or ""
    return ""


def _gripe_comments(store: Any, gripe_id: int) -> list[str]:
    return [
        b.text or ""
        for b in store.chunks.list_chunks_for_ref(gripe_id)
        if b.chunk_kind == "gripe_comment"
    ]


def _need(tool: str = "xrd scanner", **kw: Any) -> dict[str, Any]:
    return {"tool": tool, "why": "w", "would_unblock": "u", **kw}


class TestNeedGripeFiling:
    def test_first_need_files_one_gripe(self, store: Any) -> None:
        qid = _mk_quest(store)
        out = run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"needs": [_need()]})
        )
        assert out.status == "succeeded"
        gripes = _gripes(store)
        assert len(gripes) == 1
        g = gripes[0]
        body = _gripe_body(store, g.id)
        assert body.splitlines()[0] == f"quest-need: qu{qid} xrd-scanner"
        assert "xrd scanner" in body
        tags = {str(t) for t in store.tags_for(g.id)}
        assert f"quest:{qid}" in tags
        assert "needs-tool:xrd-scanner" in tags
        assert "STATUS:open" in tags
        # linked gripe --blocks--> quest
        links = store.links_for(qid, direction="in", relation="blocks")
        assert [ln.src_ref_id for ln in links] == [g.id]
        # logbook records the handle
        assert any(f"filed gripe gr{g.id}" in t for t in _log_texts(store, qid))

    def test_repeat_need_bumps_not_duplicates(self, store: Any) -> None:
        qid = _mk_quest(store)
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"needs": [_need()]}))
        # Same key after normalisation — case/spacing differences don't
        # mint a second gripe.
        run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch({"needs": [_need("XRD  Scanner")]}),
        )
        gripes = _gripes(store)
        assert len(gripes) == 1
        comments = _gripe_comments(store, gripes[0].id)
        assert len(comments) == 1 and "still needed" in comments[0]
        assert any("bumped gripe" in t for t in _log_texts(store, qid))

    def test_different_tool_files_a_second(self, store: Any) -> None:
        qid = _mk_quest(store)
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"needs": [_need()]}))
        run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch({"needs": [_need("p4est mesh lane")]}),
        )
        assert len(_gripes(store)) == 2

    def test_daily_filing_cap(self, store: Any, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_NEEDS_MAX_PER_DAY", "2")
        qid = _mk_quest(store)
        run_quest_tick(
            store,
            qid,
            dispatch_fn=_fake_dispatch(
                {"needs": [_need("tool-a"), _need("tool-b"), _need("tool-c")]}
            ),
        )
        assert len(_gripes(store)) == 2
        texts = _log_texts(store, qid)
        assert any("not filing `tool-c`" in t for t in texts)
        # A NEW tool stays capped; a repeat of an already-filed one still
        # bumps — comments aren't filings.
        run_quest_tick(store, qid, dispatch_fn=_fake_dispatch({"needs": [_need()]}))
        run_quest_tick(
            store, qid, dispatch_fn=_fake_dispatch({"needs": [_need("tool-a")]})
        )
        assert len(_gripes(store)) == 2
        gid = [g.id for g in _gripes(store) if "tool-a" in _gripe_body(store, g.id)][0]
        assert _gripe_comments(store, gid)

    def test_roadmap_reply_files_gripe_on_the_root(self, store: Any) -> None:
        root, _cap = make_root(store, demand=None, supply=None)  # demand role
        client = _ScriptedClient(
            {
                "value": 1.5,
                "source": "se:hexfold-valve",
                "reason": "port pitch",
                "needs": [_need("fib-sem")],
            }
        )
        result = rt.roadmap_tick(Hub(store=store), client, root)
        assert result["ok"]
        gripes = _gripes(store)
        assert len(gripes) == 1
        assert _gripe_body(store, gripes[0].id).splitlines()[0] == (
            f"quest-need: qu{root} fib-sem"
        )
        links = store.links_for(root, direction="in", relation="blocks")
        assert [ln.src_ref_id for ln in links] == [gripes[0].id]

    def test_weave_tick_files_aggregated_needs(self, store: Any) -> None:
        """A weave body — the model's per-section replies carry `needs`, the
        tick aggregates + files once on the quest."""
        from precis.quest.weave_tick import weave_tick
        from tests.test_weave_tick import (
            _NEW_TITLE,
            _TITLE_JSON,
            _FixedClaimsClient,
            _RoutedClient,
            _setup_maintain_and_residual,
            _weave_payload_one,
            _weave_payload_two,
        )

        qid, _did, _a, _o1, _o2 = _setup_maintain_and_residual(store)
        with_needs = json.loads(_weave_payload_one("cited-in"))
        with_needs["needs"] = [_need("claim-verifier")]
        client = _RoutedClient(
            [
                ("Cluster keywords:", _TITLE_JSON),
                ("Section: Existing Section", json.dumps(with_needs)),
                (f"Section: {_NEW_TITLE}", _weave_payload_two("cited-in")),
            ]
        )
        result = weave_tick(store, client, qid, claims_client=_FixedClaimsClient())
        assert result["ok"]
        gripes = _gripes(store)
        assert len(gripes) == 1
        assert _gripe_body(store, gripes[0].id).splitlines()[0] == (
            f"quest-need: qu{qid} claim-verifier"
        )

    def test_weave_dry_run_files_nothing(self, store: Any) -> None:
        from precis.quest.weave_tick import weave_tick
        from tests.test_weave_tick import (
            _TITLE_JSON,
            _FixedClaimsClient,
            _RoutedClient,
            _setup_maintain_and_residual,
            _weave_payload_one,
        )

        qid, _did, _a, _o1, _o2 = _setup_maintain_and_residual(store)
        with_needs = json.loads(_weave_payload_one("cited-in"))
        with_needs["needs"] = [_need()]
        client = _RoutedClient(
            [
                ("Cluster keywords:", _TITLE_JSON),
                ("Section: Existing Section", json.dumps(with_needs)),
            ]
        )
        result = weave_tick(
            store, client, qid, claims_client=_FixedClaimsClient(), dry_run=True
        )
        assert result["ok"]
        assert _gripes(store) == []


# ── visibility: the gap, the tree view, the dry-rest alert ────────────


class TestNeedGripeVisibility:
    def _file(self, store: Any, qid: int, tool: str = "xrd scanner") -> int:
        out = needs_mod.apply_needs(store, qid, [_need(tool)])
        assert len(out.gripes) == 1
        return out.gripes[0]

    def test_gap_appears_and_clears_on_close(self, store: Any) -> None:
        from precis.quest.gaps import quest_gaps
        from precis.store.types import Tag

        qid = _mk_quest(store)
        gid = self._file(store, qid)
        tool_gaps = [g for g in quest_gaps(store, qid) if g.kind == "needs-tool"]
        assert tool_gaps and tool_gaps[0].detail == f"blocked on gr{gid}: xrd-scanner"
        assert tool_gaps[0].handle == f"gr{gid}"

        # Close it (STATUS:done) — the gap clears.
        store.add_tag(
            gid, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
        )
        assert not [g for g in quest_gaps(store, qid) if g.kind == "needs-tool"]

    def test_tree_view_shows_the_gap(self, store: Any) -> None:
        qid = _mk_quest(store)
        gid = self._file(store, qid)
        body = QuestHandler(hub=Hub(store=store)).get(id=qid, view="tree").body
        assert f"blocked on gr{gid}: xrd-scanner" in body

    def test_dry_rest_alert_cites_the_gripe(self, store: Any, monkeypatch: Any) -> None:
        from precis.workers.job_types import quest_tick as wq

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "1")
        qid = _mk_quest(store)
        gid = self._file(store, qid)

        n = wq._register_dry_rest(store, qid)
        assert n >= 1
        alerts = store.list_refs(kind="alert")
        detail = "\n".join(str((a.meta or {}).get("detail") or "") for a in alerts)
        assert f"gr{gid} (xrd-scanner)" in detail

    def test_alert_without_needs_unchanged(self, store: Any, monkeypatch: Any) -> None:
        from precis.workers.job_types import quest_tick as wq

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "1")
        qid = _mk_quest(store)
        wq._register_dry_rest(store, qid)
        alerts = store.list_refs(kind="alert")
        assert alerts
        assert not any("needs-gripe" in str(a.meta or {}) for a in alerts)
