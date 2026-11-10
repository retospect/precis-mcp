"""The proposer's memory-body mode (gr560762): the one-action contract,
the pinned checklist, the fisheye+1hop input, and the ask →
memory-review todo path. DB-backed (``store`` fixture); the LLM is never
called — the router is monkeypatched or ``propose=`` returns a
deterministic verdict.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.graph.garden import candidates as cand
from precis.graph.garden import proposer
from precis.graph.garden.proposals import key_block
from precis.graph.garden.proposer import (
    MEMORY_ACTIONS,
    MEMORY_PROMPT_VERSION,
    PROMPT_VERSION,
    Verdict,
    _prompt_for,
)
from precis.graph.garden.runner import run_garden_tick
from precis.ingest.skill_template import IncludeError
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.utils.llm import router
from precis.utils.llm.router import LlmResult, Tier
from precis.utils.memory_reconcile import (
    memory_body_checklist,
    memory_reconciliation_policy,
)


def _memory(store: Store, title: str, body: str = "The verified route.") -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title, meta={})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open("section:reference"), set_by="system")
    return int(ref.id)


def _result(text: str) -> LlmResult:
    return LlmResult(
        text=text,
        cost_usd=None,
        turns_used=None,
        model="m",
        tier=Tier.MEDIUM,
        stop_reason="stop",
    )


def _candidate(rid: int, detector: str = "anchor_rot") -> cand.GardenCandidate:
    return cand.GardenCandidate(
        detector=detector,
        action=MEMORY_ACTIONS[detector][0],
        target_kind="ref",
        target_id=rid,
        title="A fixture memory",
    )


# ── action parsing ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reply", "accept", "action", "overrides", "ask"),
    [
        (
            {
                "action": "rewrite",
                "reason": "anchor moved",
                "ops": [{"find": "old", "text": "new, verified 2026-11-10"}],
            },
            True,
            "rewrite",
            {"ops": [{"find": "old", "text": "new, verified 2026-11-10"}]},
            None,
        ),
        # Invalid ops never override the detector's anchored ops.
        (
            {"action": "rewrite", "reason": "r", "ops": [{"find": "old"}]},
            True,
            "rewrite",
            {},
            None,
        ),
        (
            {"action": "rewrite", "reason": "r", "ops": "delete it"},
            True,
            "rewrite",
            {},
            None,
        ),
        (
            {"action": "no-change", "reason": "body already records it"},
            False,
            None,
            {},
            None,
        ),
        # Out-of-family and missing actions read as no-change.
        ({"action": "merge", "reason": "r"}, False, None, {}, None),
        ({"action": "invented", "reason": "r"}, False, None, {}, None),
        ({"reason": "r"}, False, None, {}, None),
        ({"accept": True, "rationale": "legacy shape"}, False, None, {}, None),
        (
            {"action": "ask", "reason": "which ruling is in force?"},
            False,
            None,
            {},
            "which ruling is in force?",
        ),
        # An ask without a reason degenerates to a plain reject.
        ({"action": "ask"}, False, None, {}, None),
    ],
)
def test_one_action_parsing(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
    accept: bool,
    action: str | None,
    overrides: dict[str, Any],
    ask: str | None,
) -> None:
    rid = _memory(store, "A standing fact")
    result = _result(json.dumps(reply))
    monkeypatch.setattr(router, "route", lambda _req: result)
    verdict = proposer.propose_llm(store, _candidate(rid))
    assert verdict.error is None
    assert verdict.accept is accept
    assert verdict.action == action
    assert verdict.payload_overrides == overrides
    assert verdict.ask == ask


@pytest.mark.parametrize(
    ("detector", "reply", "action", "overrides"),
    [
        (
            "negative_undated",
            {"action": "qualify", "reason": "r", "caveat": "until 2026-11-03"},
            "qualify",
            {"caveat": "until 2026-11-03"},
        ),
        (
            "negative_undated",
            {"action": "qualify", "reason": "r", "qualifies_from": "me42"},
            "qualify",
            {"qualifies_from": "me42"},
        ),
        (
            "thread_shape",
            {"action": "supersede", "reason": "r", "successor": "me7"},
            "supersede",
            {"successor": "me7"},
        ),
        ("thread_shape", {"action": "retire", "reason": "r"}, "retire", {}),
        # A detector's own action set bounds the payload actions:
        # thread_shape cannot rewrite even though the contract lists it.
        ("thread_shape", {"action": "rewrite", "reason": "r"}, None, {}),
    ],
)
def test_one_action_families(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    detector: str,
    reply: dict[str, Any],
    action: str | None,
    overrides: dict[str, Any],
) -> None:
    rid = _memory(store, "A standing fact")
    result = _result(json.dumps(reply))
    monkeypatch.setattr(router, "route", lambda _req: result)
    verdict = proposer.propose_llm(store, _candidate(rid, detector))
    assert verdict.accept is (action is not None)
    assert verdict.action == action
    assert verdict.payload_overrides == overrides


# ── the pinned checklist and the fisheye input ────────────────────


def test_memory_prompt_pins_the_checklist_and_the_fisheye() -> None:
    prompt = _prompt_for(_candidate(1), eye="EYE-MARK")
    for heading in (
        "## The memory-body checklist",
        "## Choose the disagreement shape",
        "## Rewrite to the standard of done",
    ):
        assert heading in prompt
    assert 'the payload actions are "rewrite"' in prompt
    assert '"fisheye": "EYE-MARK"' in prompt
    assert prompt.count(memory_reconciliation_policy()) == 1


def test_s5_handle_rule_is_pinned() -> None:
    """The gotchas row: a handle resolving to a dated gripe/todo counts as
    the dated case — 3 of 3 false alarms in the 2026-11-03 calibration
    came from not resolving the handle."""
    assert "handle resolving to a dated gripe/todo counts" in memory_body_checklist()
    prompt = _prompt_for(_candidate(1))
    assert "handle resolving to a dated gripe/todo counts" in prompt


def test_prompt_fails_loudly_when_the_checklist_loses_a_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> str:
        raise IncludeError(
            "doc include: section 'the-memory-body-checklist' not found "
            "in 'precis-fix-memory-gripe-help'"
        )

    monkeypatch.setattr(proposer, "memory_body_checklist", _boom)
    with pytest.raises(IncludeError):
        _prompt_for(_candidate(1))


def test_propose_carries_the_nodes_fisheye(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "The verified route reaches the evidence with a live anchor."
    rid = _memory(store, "A standing fact", body)
    captured: list[str] = []

    def capture(req: Any) -> LlmResult:
        captured.append(req.prompt)
        return _result(json.dumps({"action": "no-change", "reason": "stands"}))

    monkeypatch.setattr(router, "route", capture)
    verdict = proposer.propose_llm(store, _candidate(rid))
    assert verdict.accept is False
    (prompt,) = captured
    assert '"fisheye"' in prompt
    assert body in prompt


def test_memory_prompt_version_covers_the_checklist() -> None:
    assert MEMORY_PROMPT_VERSION.startswith("garden-propose.memory.v4.")
    assert MEMORY_PROMPT_VERSION != PROMPT_VERSION


# ── prompt-version block and the ask path (runner) ────────────────


def _title_shape_candidate(store: Store) -> tuple[int, cand.GardenCandidate]:
    rid = _memory(store, "ship_trap")
    (candidate,) = cand.title_shape_candidates(store)
    return rid, candidate


def test_no_change_closes_the_key_under_the_prompt_version(store: Store) -> None:
    rid, candidate = _title_shape_candidate(store)
    tick = run_garden_tick(
        store,
        passes=("memory-review",),
        propose=lambda _s, c: Verdict(accept=False, rationale="stands"),
    )
    assert tick.rejected >= 1
    reviews = store.reviews_for("ref", rid)
    rejections = [r for r in reviews if r.verdict == "rejected"]
    assert rejections, "no-change must record a proposer rejection"
    note = json.loads(rejections[0].note)
    assert note["garden"]["proposer_reject"] is True
    assert note["garden"]["prompt"] == MEMORY_PROMPT_VERSION
    assert (
        key_block(
            store, "ref", rid, candidate.key, prompt_version=MEMORY_PROMPT_VERSION
        )
        == "rejected"
    )
    # A new prompt version is a new judge: the key re-opens for it.
    assert (
        key_block(
            store, "ref", rid, candidate.key, prompt_version="garden-propose.memory.v3"
        )
        is None
    )
    # The next tick does not re-ask under the same version.
    before = len(store.reviews_for("ref", rid))
    again = run_garden_tick(
        store,
        passes=("memory-review",),
        propose=lambda _s, c: Verdict(accept=False, rationale="stands"),
    )
    assert again.skipped_rejected >= 1
    assert len(store.reviews_for("ref", rid)) == before


def test_ask_files_a_memory_review_todo_by_smell_class(store: Store) -> None:
    rid, _candidate_ = _title_shape_candidate(store)

    def _ask(s: Store, c: cand.GardenCandidate) -> Verdict:
        if c.detector != "title_shape":
            return Verdict(accept=False, rationale="not the target")
        return Verdict(accept=False, ask="which title does the body support?")

    tick = run_garden_tick(store, passes=("memory-review",), propose=_ask)
    assert tick.asked == 1
    from precis.utils.memory_hubs import review_todos

    todos = review_todos(store, [rid])
    assert todos, "ask must file a memory-review todo pointing at the memory"
    handle = todos[rid].split(":")[0]
    todo_id = int(handle.removeprefix("td"))
    todo = store.fetch_refs_by_ids([todo_id])[todo_id]
    # title_shape is a shape smell: the mechanical tier.
    assert todo.meta.get("llm_tier") == "sonnet"
    with store.pool.connection() as conn:
        link = conn.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (todo_id, rid),
        ).fetchone()
    assert link is not None and link[0] == "raises-concern-about"
    # The key closes under this prompt version; the next tick adds no todo.
    rejections = [r for r in store.reviews_for("ref", rid) if r.verdict == "rejected"]
    notes = [json.loads(r.note)["garden"] for r in rejections]
    assert any(n.get("reason") == "asked" for n in notes)
    again = run_garden_tick(store, passes=("memory-review",), propose=_ask)
    assert again.asked == 0
    assert again.skipped_rejected >= 1
    assert review_todos(store, [rid])[rid].count(",") == 0


def test_ask_tier_for_content_smells(store: Store) -> None:
    """Content smells dispatch the review on opus."""
    from precis.graph.garden.runner import _ASK_LLM_TIERS

    assert _ASK_LLM_TIERS["anchor_rot"] == "opus"
    assert _ASK_LLM_TIERS["source_drift"] == "opus"
    assert _ASK_LLM_TIERS["thread_shape"] == "sonnet"
