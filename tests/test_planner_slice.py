"""Tests for the planner-coroutine slice: LLM:* tag guards, dispatch
pickup, prompt builder, and the three guardrails.

The actual ``claude -p`` spawn is mocked via ``PRECIS_CLAUDE_BIN``
pointing at a stub script — exercised in ``test_claude_inproc.py``.
This file covers the surfaces the planner relies on without
shelling out.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers import planner_guardrails
from precis.workers.planner_prompt import (
    _build_skill_index,
    _build_system_prompt,
    _render_ancestry_toon,
)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


# ── LLM:* / executor:* tag guards ──────────────────────────────────


def test_put_accepts_known_llm_models(handler: TodoHandler) -> None:
    """The three sanctioned models pass the closed-vocab guard."""
    for model in ["opus", "sonnet", "haiku"]:
        r = handler.put(text=f"{model} task", tags=[f"LLM:{model}"])
        assert "id=" in r.body


def test_put_rejects_unknown_llm_model(handler: TodoHandler) -> None:
    """Typos and unknown models reject at write time, not silently."""
    with pytest.raises(BadInput, match="LLM:"):
        handler.put(text="bad model", tags=["LLM:opos"])


def test_tag_rejects_unknown_llm_model(handler: TodoHandler) -> None:
    """Same closed-vocab discipline applies on add via tag()."""
    r = handler.put(text="task")
    rid = _id_of(r.body)
    with pytest.raises(BadInput, match="LLM:"):
        handler.tag(id=rid, add=["LLM:gpt-5"])


# ── dispatch picks up LLM:*-tagged todos ──────────────────────────


def test_dispatch_picks_up_llm_tagged_todo(handler: TodoHandler, store: Store) -> None:
    """An LLM:*-tagged leaf is dispatchable without meta.executor."""
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(text="dispatchable", tags=["LLM:sonnet"])
    rid = _id_of(r.body)
    ids = _candidate_parent_ids(store, limit=10)
    assert rid in ids


def test_dispatch_skips_llm_tagged_with_open_child(
    handler: TodoHandler, store: Store
) -> None:
    """Parent with a live (open) child todo yields — coroutine pattern."""
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="parent", tags=["LLM:opus"])
    parent_id = _id_of(parent.body)
    # First sweep: parent is a candidate.
    assert parent_id in _candidate_parent_ids(store, limit=10)
    # Spawn a child.
    handler.put(text="child", parent_id=parent_id, tags=["LLM:sonnet"])
    # Now parent yields — child is the candidate, parent is not.
    ids = _candidate_parent_ids(store, limit=10)
    assert parent_id not in ids


def test_dispatch_re_picks_up_after_children_done(
    handler: TodoHandler, store: Store
) -> None:
    """When all open children resolve, parent re-becomes a candidate."""
    from precis.workers.dispatch import _candidate_parent_ids

    parent = handler.put(text="parent", tags=["LLM:opus"])
    parent_id = _id_of(parent.body)
    child = handler.put(text="child", parent_id=parent_id, tags=["LLM:sonnet"])
    child_id = _id_of(child.body)
    # Parent yields.
    assert parent_id not in _candidate_parent_ids(store, limit=10)
    # Child resolves.
    handler.tag(id=child_id, add=["STATUS:done"])
    # Parent re-becomes candidate.
    assert parent_id in _candidate_parent_ids(store, limit=10)


def test_dispatch_skips_untagged_todos(handler: TodoHandler, store: Store) -> None:
    """No LLM:*, no executor:*, no meta.executor → not dispatchable."""
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(text="ordinary doable, not auto-run")
    rid = _id_of(r.body)
    ids = _candidate_parent_ids(store, limit=10)
    assert rid not in ids


def test_dispatch_skips_with_halt_reason(handler: TodoHandler, store: Store) -> None:
    """``halt:cost-cap`` blocks dispatch the same as bare halt."""
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(text="halted", tags=["LLM:sonnet"])
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("halt:cost-cap"), set_by="system")
    ids = _candidate_parent_ids(store, limit=10)
    assert rid not in ids


# ── prompt builder ─────────────────────────────────────────────────


def test_system_prompt_contains_pinned_skill_and_index() -> None:
    """Cached layer carries precis-tasks-help + the skill index header."""
    out = _build_system_prompt(store=None)  # type: ignore[arg-type]
    assert "Available skills" in out
    # Pinned skill header line.
    assert "precis-tasks-help" in out
    # Planner contract section header.
    assert "Planner contract" in out


def test_skill_index_lists_active_skills_with_summaries() -> None:
    """The boot index lists `slug — summary` lines for active skills."""
    out = _build_skill_index(store=None)  # type: ignore[arg-type]
    assert "precis-tasks-help —" in out
    assert "precis-decomposition-help —" in out
    # Sanity: no skill without a summary leaks in.
    for line in out.splitlines():
        if line.startswith("- "):
            assert " — " in line, f"skill index entry missing summary: {line!r}"


def test_ancestry_toon_renders_chain() -> None:
    """TOON list with id, title, from for each ancestor level."""
    chain = [
        {"id": 100, "title": "Strategic root", "level": "level:strategic"},
        {"id": 420, "title": "Tactical work", "level": "level:tactical"},
        {"id": 6647, "title": "Leaf", "level": None},
    ]
    out = _render_ancestry_toon(chain, leaf_id=6647)
    assert "ancestry: [3]{id,title,from}" in out
    assert "#100,Strategic root,owner" in out
    assert "#420,Tactical work,owner" in out
    assert "#6647,Leaf,planner" in out


def test_ancestry_toon_handles_root_self() -> None:
    """Root leaf renders the single-level chain correctly."""
    chain = [{"id": 7, "title": "Just me", "level": None}]
    out = _render_ancestry_toon(chain, leaf_id=7)
    assert "ancestry: [1]" in out


# ── guardrails ────────────────────────────────────────────────────


def test_guardrails_allow_fresh_parent(handler: TodoHandler, store: Store) -> None:
    """A brand-new LLM:*-tagged parent passes all three checks."""
    r = handler.put(text="fresh", tags=["LLM:sonnet"])
    rid = _id_of(r.body)
    verdict = planner_guardrails.check_parent(store, parent_ref_id=rid)
    assert verdict.allow is True


def test_guardrails_halt_on_tick_cap(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once tick_count >= PRECIS_MAX_TICKS, parent gets halt:tick-cap."""
    monkeypatch.setenv("PRECIS_MAX_TICKS", "3")
    r = handler.put(text="prone-to-loop", tags=["LLM:sonnet"])
    rid = _id_of(r.body)
    # Push tick count to the cap.
    for _ in range(3):
        planner_guardrails.bump_tick_count(store, rid)
    verdict = planner_guardrails.check_parent(store, parent_ref_id=rid)
    assert verdict.allow is False
    assert verdict.halt_tag == "halt:tick-cap"
    # Tag landed on the ref.
    from precis.workers.dispatch import _candidate_parent_ids

    assert rid not in _candidate_parent_ids(store, limit=10)


def test_guardrails_halt_on_cost_cap(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When sum(child.meta.cost_usd) >= cap, parent gets halt:cost-cap."""
    monkeypatch.setenv("PRECIS_MAX_TODO_USD", "1.0")
    r = handler.put(text="expensive parent", tags=["LLM:opus"])
    rid = _id_of(r.body)
    # Mint a fake job with cost_usd = 1.5 to push over the cap.
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="dummy",
        meta={"cost_usd": 1.5},
        parent_id=rid,
    )
    _ = job
    verdict = planner_guardrails.check_parent(store, parent_ref_id=rid)
    assert verdict.allow is False
    assert verdict.halt_tag == "halt:cost-cap"


def test_guardrails_skip_on_daily_ceiling(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When daily cost >= PRECIS_DAILY_COST_CEILING, dispatch is paused
    (but the parent is NOT individually halted — round-wide pause).

    Setup: keep per-todo cap loose, push global cost via a job under
    a DIFFERENT parent so the parent-under-test has $0 of its own
    cost and only the daily-ceiling branch fires.
    """
    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "5.0")
    monkeypatch.setenv("PRECIS_MAX_TODO_USD", "1000.0")
    other = handler.put(text="big spender", tags=["LLM:opus"])
    other_id = _id_of(other.body)
    store.insert_ref(
        kind="job",
        slug=None,
        title="dummy",
        meta={"cost_usd": 10.0},
        parent_id=other_id,
    )
    r = handler.put(text="cheap parent", tags=["LLM:sonnet"])
    rid = _id_of(r.body)
    verdict = planner_guardrails.check_parent(store, parent_ref_id=rid)
    assert verdict.allow is False
    # No per-parent halt: the global ceiling is round-scoped.
    assert verdict.halt_tag is None
