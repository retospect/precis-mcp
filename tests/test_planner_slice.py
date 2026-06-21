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
    _build_user_prompt,
    _load_ref_body,
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


# ── body loading (the brief lives in refs.title) ──────────────────


def test_load_ref_body_reads_todo_title(handler: TodoHandler, store: Store) -> None:
    """A todo's brief lives in refs.title — _load_ref_body must return it.

    Regression for the brief-blindness bug: todos emit no body chunk, so
    reading only ``chunks`` handed the planner an empty body.
    """
    brief = (
        "NOx to Ammonia\n\n"
        + "Build a tightly coupled DFT-operando-MS design loop. " * 40
    )
    r = handler.put(text=brief, tags=["LLM:opus"])
    rid = _id_of(r.body)
    body = _load_ref_body(store, rid)
    assert "tightly coupled DFT-operando-MS design loop" in body


def test_user_prompt_body_not_empty_for_todo(
    handler: TodoHandler, store: Store
) -> None:
    """The ## Body section of the planner prompt carries the real brief."""
    r = handler.put(
        text="NOx to Ammonia\n\nThe real brief is long and very specific.",
        tags=["LLM:opus"],
    )
    rid = _id_of(r.body)
    user = _build_user_prompt(store, ref_id=rid, model="opus")
    assert "## Body" in user
    body_section = user.split("## Body", 1)[1]
    assert "The real brief is long and very specific." in body_section
    assert "(empty)" not in body_section


def test_load_ref_body_excludes_tag_overflow(
    handler: TodoHandler, store: Store
) -> None:
    """The planner's own overflowed ask-user question (a tag_overflow
    chunk on the ref) must NOT be read back as part of the brief."""
    r = handler.put(text="Genuine brief text here.", tags=["LLM:opus"])
    rid = _id_of(r.body)
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                "VALUES (%s, 0, 'tag_overflow', %s, '{}'::jsonb)",
                (rid, "ask-user: Body is empty — what shape should this take?"),
            )
    body = _load_ref_body(store, rid)
    assert "Genuine brief text here." in body
    assert "Body is empty" not in body


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


def test_generated_child_defaults_to_llm_opus_root_does_not(
    handler: TodoHandler, store: Store
) -> None:
    """A parented (generated) todo auto-gets LLM:opus → dispatchable; a
    deliberately-created root does not (keeps the no-auto-run reminder)."""
    from precis.workers.dispatch import _candidate_parent_ids

    root = _id_of(handler.put(text="deliberate root").body)
    child = _id_of(handler.put(text="generated child", parent_id=root).body)
    ids = _candidate_parent_ids(store, limit=50)
    assert child in ids  # parented → auto LLM:opus → runs
    assert root not in ids  # root → no default → not auto-run


def test_user_prompt_surfaces_anchor_chunk(hub: Hub, store: Store) -> None:
    """A change-request todo with meta.anchor='¶<handle>' must put the
    anchored chunk's text in the agent's prompt — so it acts on that
    chunk instead of yielding ask-user 'which paragraph?' (the
    see-chunk-0 loop)."""
    from precis.handlers.draft import DraftHandler

    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="d1", title="T", project=proj)
    dref = store.get_ref(kind="draft", id="d1")
    title_h = store.reading_order(dref.id)[0].handle
    draft.put(
        id="d1",
        chunk_kind="paragraph",
        text="The quick brown fox paragraph to remove.",
        at={"after": f"¶{title_h}"},
    )
    para_h = next(
        c.handle for c in store.reading_order(dref.id) if c.text.startswith("The quick")
    )
    todo = store.insert_ref(kind="todo", slug=None, title="remove this paragraph")
    store.stamp_ref_meta(todo.id, {"anchor": f"¶{para_h}"})

    prompt = _build_user_prompt(store, ref_id=todo.id, model="opus")
    assert f"¶{para_h}" in prompt
    assert "The quick brown fox paragraph to remove." in prompt
    assert "Act on THIS chunk" in prompt


def test_user_prompt_anchor_missing_chunk(hub: Hub, store: Store) -> None:
    """An anchor pointing at a nonexistent chunk tells the agent to ask a
    grounded question, not guess."""
    todo = store.insert_ref(kind="todo", slug=None, title="fix it")
    store.stamp_ref_meta(todo.id, {"anchor": "¶ZZZZZZ"})
    prompt = _build_user_prompt(store, ref_id=todo.id, model="opus")
    assert "¶ZZZZZZ" in prompt and "no longer exists" in prompt


def test_anchor_block_lists_linked_connections(hub: Hub, store: Store) -> None:
    """The anchor block feeds the agent what's linked to the chunk
    (provenance / dream-memories), so it works with the context."""
    from precis.handlers.draft import DraftHandler

    draft = DraftHandler(hub=hub)
    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="d2", title="T", project=proj)
    dref = store.get_ref(kind="draft", id="d2")
    title_h = store.reading_order(dref.id)[0].handle
    draft.put(
        id="d2",
        chunk_kind="paragraph",
        text="A claim to support.",
        at={"after": f"¶{title_h}"},
    )
    para = next(c for c in store.reading_order(dref.id) if c.text.startswith("A claim"))
    mem = store.insert_ref(kind="memory", slug=None, title="Supporting evidence")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, src_chunk_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, %s, 'derived-from', 'agent')",
            (dref.id, para.chunk_id, mem.id),
        )
    todo = store.insert_ref(kind="todo", slug=None, title="support this claim")
    store.stamp_ref_meta(todo.id, {"anchor": f"¶{para.handle}"})

    prompt = _build_user_prompt(store, ref_id=todo.id, model="opus")
    assert "Linked to this chunk" in prompt
    assert f"memory:{mem.id}" in prompt and "Supporting evidence" in prompt
