"""The prompt assembler + module library (migration step 1).

Unit tests for the model-agnostic assembler, the claude_agent adapter,
the named-predicate gate, and the four computed tables; plus DB-backed
tests that the planner refactor wired ``doc_context`` / ``glossary`` onto
real store state. The planner's *existing* contract is covered by
``test_planner_slice.py`` / ``test_planner_section_style.py`` (kept
green by this refactor); here we test the new surface.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.store import Store
from precis.utils.handle_registry import code_for_kind
from precis.utils.prompt import (
    AssemblyContext,
    Block,
    ClaudeAgentAdapter,
    Layer,
    Module,
    assemble,
    doc_context_table,
    glossary_table,
    kinds_table,
    section_review_block,
    tools_table,
)
from precis.utils.prompt import predicates as P


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _ctx(store: Store | None = None, ref_id: int = 0) -> AssemblyContext:
    return AssemblyContext(store=store, ref_id=ref_id, model="opus")


# ── assembler ──────────────────────────────────────────────────────


def test_assemble_orders_and_drops_empty() -> None:
    """Blocks come out in declaration order; a falsy build is dropped."""
    mods = [
        Module(id="a", layer=Layer.CACHED, build=lambda c: "alpha"),
        Module(id="empty", layer=Layer.CACHED, build=lambda c: ""),
        Module(id="none", layer=Layer.VARIABLE, build=lambda c: None),
        Module(id="b", layer=Layer.VARIABLE, build=lambda c: "beta"),
    ]
    blocks = assemble(mods, _ctx())
    assert [b.id for b in blocks] == ["a", "b"]
    assert blocks[0].text == "alpha" and blocks[1].text == "beta"


def test_assemble_skips_failing_builder_not_fatal() -> None:
    """One broken optional block must not sink the whole prompt."""

    def boom(c: AssemblyContext) -> str:
        raise RuntimeError("kaboom")

    mods = [
        Module(id="ok1", layer=Layer.CACHED, build=lambda c: "one"),
        Module(id="bad", layer=Layer.CACHED, build=boom),
        Module(id="ok2", layer=Layer.CACHED, build=lambda c: "two"),
    ]
    blocks = assemble(mods, _ctx())
    assert [b.id for b in blocks] == ["ok1", "ok2"]


def test_assemble_required_builder_failure_re_raises() -> None:
    """A ``required`` module's build failure must NOT be swallowed.

    Optional blocks are dropped so an unattended planner tick survives,
    but a reviewer *body* becomes a persisted ``tier:*`` memory digest —
    silently omitting it would ship a truncated digest. ``required=True``
    makes the assembler fail loudly instead.
    """

    def boom(c: AssemblyContext) -> str:
        raise RuntimeError("kaboom")

    mods = [
        Module(id="ok", layer=Layer.CACHED, build=lambda c: "one"),
        Module(id="body", layer=Layer.VARIABLE, build=boom, required=True),
    ]
    with pytest.raises(RuntimeError, match="kaboom"):
        assemble(mods, _ctx())


def test_applies_when_gates_capability_and_data() -> None:
    """A false predicate drops the module without calling build."""
    called = {"n": 0}

    def build(c: AssemblyContext) -> str:
        called["n"] += 1
        return "data"

    P.PREDICATES["_always_false"] = lambda c: False
    P.PREDICATES["_always_true"] = lambda c: True
    try:
        gated_off = Module(
            id="off", layer=Layer.VARIABLE, build=build, applies_when="_always_false"
        )
        gated_on = Module(
            id="on", layer=Layer.VARIABLE, build=build, applies_when="_always_true"
        )
        assert assemble([gated_off], _ctx()) == []
        assert called["n"] == 0  # build never ran for the gated-off module
        on = assemble([gated_on], _ctx())
        assert [b.id for b in on] == ["on"] and called["n"] == 1
    finally:
        del P.PREDICATES["_always_false"]
        del P.PREDICATES["_always_true"]


def test_unknown_predicate_raises() -> None:
    with pytest.raises(KeyError, match="unknown applies_when predicate"):
        P.evaluate("nope_not_a_predicate", _ctx())


def test_has_anchor_false_without_store() -> None:
    """The store-free ctx (cached-only assembly) has no anchor."""
    ctx = _ctx()
    assert P.has_anchor(ctx) is False
    assert ctx.extras["anchor"] is None  # memoised


# ── adapter ────────────────────────────────────────────────────────


def test_adapter_splits_by_layer() -> None:
    blocks = [
        Block(id="m", layer=Layer.CACHED, text="mechanics"),
        Block(id="t", layer=Layer.CACHED, text="tools"),
        Block(id="body", layer=Layer.VARIABLE, text="the body"),
    ]
    system, user = ClaudeAgentAdapter.render(blocks)
    assert system == "mechanics\n\ntools"
    assert user == "the body"


# ── cached tables: tools + kinds ───────────────────────────────────


def test_tools_table_has_all_seven_verbs() -> None:
    out = tools_table()
    for verb in ("get", "search", "put", "edit", "tag", "link"):
        assert verb in out
    assert "{verb\texample\twhat}" in out  # TOON header


def test_kinds_table_codes_match_registry() -> None:
    """The legend's codes are pulled from the handle registry SSOT, so a
    code rename can't silently drift the prompt."""
    out = kinds_table()
    for kind in ("draft", "paper", "patent", "todo", "citation", "skill"):
        assert code_for_kind(kind) in out
        assert kind in out
    # chunk code shown for a chunked kind
    assert f"{code_for_kind('draft')}/{code_for_kind('draft', chunk=True)}" in out


# ── variable tables: glossary + doc_context (DB-backed) ─────────────


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def test_glossary_table_lists_terms(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="g1", title="MOF paper", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g1")
    assert ref is not None
    draft.put(
        id="g1",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    out = glossary_table(hub.live_store, ref.id)
    assert "MOF" in out and "metal-organic framework" in out
    assert "{term\tshort\tlong\thandle}" in out


def test_glossary_table_empty_is_blank(draft: DraftHandler, hub: Hub) -> None:
    draft.put(id="g2", title="no terms", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id="g2")
    assert ref is not None
    assert glossary_table(hub.live_store, ref.id) == ""


def _draft_with_paragraphs(draft: DraftHandler, hub: Hub, slug: str) -> Store:
    """Title + two paragraphs; returns the store for convenience."""
    draft.put(id=slug, title="Doc", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="First para anchor target.",
        at={"after": title_h},
    )
    first_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.text.startswith("First para")
    )
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="Second neighbour paragraph.",
        at={"after": first_h},
    )
    return hub.live_store


def test_doc_context_table_window(draft: DraftHandler, hub: Hub) -> None:
    """doc_context centres on the anchor (verbatim) and shows neighbours.

    The anchor is addressed by its base-58 ``chunks.handle`` (as a
    change-request stamps it) but the table renders the canonical
    ``dc<chunk_id>`` form the prompt tells the agent to use."""
    store = _draft_with_paragraphs(draft, hub, "dctx")
    ref = hub.live_store.get_ref(kind="draft", id="dctx")
    assert ref is not None
    anchor_chunk = next(
        c for c in store.drafts.reading_order(ref.id) if c.text.startswith("First para")
    )
    out = doc_context_table(store, anchor_chunk.handle)
    assert "## doc_context" in out
    assert "{id\twhat\thow\tdetails}" in out
    # rendered as the canonical dc<id> handle, not the base-58 anchor
    assert f"dc{anchor_chunk.chunk_id}" in out
    assert "verbatim" in out  # the anchor row's disclosure level
    assert "First para anchor target." in out
    assert "current (change-request target)" in out
    # at least one neighbour (prev/next sibling) is surfaced as a window row
    assert "sibling" in out


def test_doc_context_missing_anchor_is_blank(hub: Hub) -> None:
    assert doc_context_table(hub.live_store, "dc999999") == ""


# ── planner integration: the new tables ride the real prompt ────────


def test_planner_system_carries_tools_and_kinds(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="do a thing")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "## Tools" in prompts.system
    assert "## Kinds" in prompts.system
    # tables are cached → system, never the variable user layer
    assert "## Tools" not in prompts.user


def test_planner_user_has_doc_context_when_anchored(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    store = _draft_with_paragraphs(draft, hub, "dctx2")
    ref = hub.live_store.get_ref(kind="draft", id="dctx2")
    assert ref is not None
    anchor = next(
        c.handle
        for c in store.drafts.reading_order(ref.id)
        if c.text.startswith("First para")
    )
    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="edit the para")
    hub.live_store.stamp_ref_meta(todo.id, {"anchor": anchor})

    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "## doc_context" in prompts.user
    assert anchor in prompts.user


def test_planner_user_no_doc_context_without_anchor(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="plain todo")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "## doc_context" not in prompts.user


# ── draft-section reviewer (step 3 / Shot 3) ───────────────


def _draft_with_section(draft: DraftHandler, hub: Hub, slug: str) -> str:
    """Title + a Methods heading + two paragraphs under it. Returns the
    Methods heading handle (the section root a review-todo anchors to)."""
    draft.put(id=slug, title="Doc", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(id=slug, chunk_kind="heading", text="Methods", at={"after": title_h})
    methods_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.text == "Methods"
    )
    # nest the paragraphs UNDER the heading (into, not after) so they form
    # the section subtree the reviewer reads
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="We synthesized the catalyst at 80C.",
        at={"into": methods_h, "last": True},
    )
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text="Yield was measured by GC-MS.",
        at={"into": methods_h, "last": True},
    )
    return methods_h


def test_has_review_predicate(draft: DraftHandler, hub: Hub) -> None:
    methods_h = _draft_with_section(draft, hub, "rev1")
    review = hub.live_store.insert_ref(
        kind="todo", slug=None, title="review the methods"
    )
    hub.live_store.stamp_ref_meta(
        review.id, {"anchor": methods_h, "review": "structural"}
    )
    plain = hub.live_store.insert_ref(kind="todo", slug=None, title="plain")

    assert P.has_review(_ctx(hub.live_store, review.id)) is True
    assert P.has_review(_ctx(hub.live_store, plain.id)) is False


def test_section_review_block_lists_subtree_verbatim(
    draft: DraftHandler, hub: Hub
) -> None:
    methods_h = _draft_with_section(draft, hub, "rev2")
    out = section_review_block(hub.live_store, methods_h)
    assert "## Section under review" in out
    # the section's prose is shown verbatim (a reviewer must read it)
    assert "We synthesized the catalyst at 80C." in out
    assert "Yield was measured by GC-MS." in out
    # chunks are labelled by their canonical dc<id> handle to anchor findings
    assert "[dc" in out


def test_section_review_block_missing_anchor_blank(hub: Hub) -> None:
    assert section_review_block(hub.live_store, "dc999999") == ""


def test_planner_review_todo_gets_persona_and_section(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    methods_h = _draft_with_section(draft, hub, "rev3")
    review = hub.live_store.insert_ref(kind="todo", slug=None, title="review methods")
    hub.live_store.stamp_ref_meta(
        review.id, {"anchor": methods_h, "review": "structural"}
    )

    prompts = build_planner_prompts(hub.live_store, ref_id=review.id, model="opus")
    # reviewer stance is specialised in the variable (user) layer
    assert "Reviewer mode" in prompts.user
    assert "structural" in prompts.user
    assert "anchored change" in prompts.user.lower()  # persona body
    # the section to review rides along, verbatim
    assert "## Section under review" in prompts.user
    assert "We synthesized the catalyst at 80C." in prompts.user
    # the cached planner contract is untouched (still the shared prefix)
    assert "Planner contract" in prompts.system


def test_planner_plain_todo_has_no_reviewer_blocks(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="just do it")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "Reviewer mode" not in prompts.user
    assert "## Section under review" not in prompts.user


# ── source-backfill coroutine (slice 4) ─────────────────────────────


def test_has_backfill_predicate(draft: DraftHandler, hub: Hub) -> None:
    methods_h = _draft_with_section(draft, hub, "bf1")
    # explicit targets
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods")
    hub.live_store.stamp_ref_meta(run.id, {"backfill": {"targets": [methods_h]}})
    assert P.has_backfill(_ctx(hub.live_store, run.id)) is True
    assert P._backfill_targets(_ctx(hub.live_store, run.id)) == [methods_h]

    # bare marker falls back to the anchor
    run2 = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill anchored")
    hub.live_store.stamp_ref_meta(run2.id, {"backfill": True, "anchor": methods_h})
    assert P.has_backfill(_ctx(hub.live_store, run2.id)) is True
    assert P._backfill_targets(_ctx(hub.live_store, run2.id)) == [methods_h]

    # plain todo is not a backfill run
    plain = hub.live_store.insert_ref(kind="todo", slug=None, title="plain")
    assert P.has_backfill(_ctx(hub.live_store, plain.id)) is False


def test_planner_backfill_todo_gets_workspace_and_instructions(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    methods_h = _draft_with_section(draft, hub, "bf2")
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods")
    hub.live_store.stamp_ref_meta(run.id, {"backfill": {"targets": [methods_h]}})

    prompts = build_planner_prompts(hub.live_store, ref_id=run.id, model="opus")
    # the backfill task framing + the three actions
    assert "Source backfill — weave the sources you missed" in prompts.user
    assert "DISMISSED_SOURCE" in prompts.user  # the dismiss command
    assert "candidate sources" in prompts.user  # the recall workspace rendered
    assert "grounding" in prompts.user  # the ✓/⚠ coverage line
    # the target section's prose rides along so the model can weave into it
    assert "We synthesized the catalyst at 80C." in prompts.user
    # slice 7: the default (find) phase routes to review, not straight to done
    assert "BACKFILL_PHASE:review" in prompts.user
    assert f"id='{run.id}'" in prompts.user  # the run todo is named for the tag
    # the cached contract is untouched (shared cache prefix preserved)
    assert "Planner contract" in prompts.system


def test_planner_plain_todo_has_no_backfill_block(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="just do it")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "Source backfill — weave" not in prompts.user


def test_backfill_phase_helper(draft: DraftHandler, hub: Hub) -> None:
    from precis.store.types import Tag
    from precis.workers.planner_prompt import (
        PHASE_FIND,
        PHASE_REVIEW,
        _backfill_phase,
    )

    methods_h = _draft_with_section(draft, hub, "bfp")
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods")
    hub.live_store.stamp_ref_meta(run.id, {"backfill": {"targets": [methods_h]}})
    # default (no phase tag) → find
    assert _backfill_phase(hub.live_store, run.id) == PHASE_FIND
    # tagging BACKFILL_PHASE:review advances the run
    hub.live_store.add_tag(run.id, Tag.closed("BACKFILL_PHASE", "review"))
    assert _backfill_phase(hub.live_store, run.id) == PHASE_REVIEW

    # a near-miss value (case / "reviewing") still enters review — a trivial typo
    # must not silently drop the run back to find and re-do the whole weave.
    run2 = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods 2")
    hub.live_store.add_tag(run2.id, Tag.closed("BACKFILL_PHASE", "Reviewing"))
    assert _backfill_phase(hub.live_store, run2.id) == PHASE_REVIEW
    # an unrelated value degrades to the safe, work-producing find phase
    run3 = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods 3")
    hub.live_store.add_tag(run3.id, Tag.closed("BACKFILL_PHASE", "whatever"))
    assert _backfill_phase(hub.live_store, run3.id) == PHASE_FIND


def test_planner_backfill_review_phase_instructions(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.store.types import Tag
    from precis.workers.planner_prompt import build_planner_prompts

    methods_h = _draft_with_section(draft, hub, "bfr")
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="backfill methods")
    hub.live_store.stamp_ref_meta(run.id, {"backfill": {"targets": [methods_h]}})
    hub.live_store.add_tag(run.id, Tag.closed("BACKFILL_PHASE", "review"))

    prompts = build_planner_prompts(hub.live_store, ref_id=run.id, model="opus")
    # the review-phase framing replaces the weave framing
    assert "Source backfill — REVIEW what you wove" in prompts.user
    assert "Source backfill — weave the sources you missed" not in prompts.user
    # the review dimensions: claim↔source, cold-read, coverage
    assert "Claim ↔ source" in prompts.user
    assert "Cold-read test" in prompts.user
    # the workspace (sources still open) rides along for in-context review
    assert "grounding" in prompts.user
    assert "We synthesized the catalyst at 80C." in prompts.user
    # convergence: clean review → done; a real gap reopens find
    assert "STATUS:done" in prompts.user
    assert "BACKFILL_PHASE:find" in prompts.user


def test_is_patent_predicate(hub: Hub) -> None:
    from precis.utils.workspace import Workspace

    ws = Workspace(
        path="projects/pat", format="tex", entrypoint="main.tex", doc_type="patent"
    )
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="patent tick")
    hub.live_store.stamp_ref_meta(run.id, {"workspace": ws.to_meta()})
    assert P.is_patent(_ctx(hub.live_store, run.id)) is True

    # A non-patent genre and a plain todo are both False.
    ws2 = Workspace(
        path="projects/pap", format="tex", entrypoint="main.tex", doc_type="paper"
    )
    run2 = hub.live_store.insert_ref(kind="todo", slug=None, title="paper tick")
    hub.live_store.stamp_ref_meta(run2.id, {"workspace": ws2.to_meta()})
    assert P.is_patent(_ctx(hub.live_store, run2.id)) is False

    plain = hub.live_store.insert_ref(kind="todo", slug=None, title="plain")
    assert P.is_patent(_ctx(hub.live_store, plain.id)) is False


def test_patent_authoring_block_gated_and_content(hub: Hub) -> None:
    from precis.utils.workspace import Workspace
    from precis.workers.planner_prompt import _m_patent

    ws = Workspace(
        path="projects/pat", format="tex", entrypoint="main.tex", doc_type="patent"
    )
    run = hub.live_store.insert_ref(kind="todo", slug=None, title="patent tick")
    hub.live_store.stamp_ref_meta(run.id, {"workspace": ws.to_meta()})
    block = _m_patent(_ctx(hub.live_store, run.id))
    assert "Patent authoring" in block
    assert "source='remote'" in block  # prior-art sweep
    assert "[pk…]" in block  # patent handle, not [pc…]
    assert "freedom to operate" in block.lower()
    assert "plan" in block.lower()  # scoping ledger


def test_has_plan_and_plan_ledger_block(hub: Hub) -> None:
    from precis.handlers.plan import PlanHandler
    from precis.workers.planner_prompt import _m_plan

    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Patent project")
    plan = PlanHandler(hub=hub)
    plan.put(id="frypat-scoping", title="Scoping", project=proj.id)
    plan.put(
        id="frypat-scoping",
        text="considered claiming the mesh geometry; narrowed because [pk7] claims it",
        at={"last": True},
        status="done",
    )
    ctx = _ctx(hub.live_store, proj.id)
    assert P.has_plan(ctx) is True
    block = _m_plan(ctx)
    assert "decision ledger" in block.lower()
    assert "mesh geometry" in block  # the recorded decision is surfaced

    # A project with no plan → predicate false, block empty.
    plain = hub.live_store.insert_ref(kind="todo", slug=None, title="plain")
    ctx2 = _ctx(hub.live_store, plain.id)
    assert P.has_plan(ctx2) is False
    assert _m_plan(ctx2) == ""


# ── pre-rendered source material for a plain draft-bound tick ──────


#: Distinctive terms shared with the matching paper chunk below, so the
#: lexical recall leg (no embedder configured in the test env) finds a
#: real hit via ``websearch_to_tsquery`` — no ``search_blocks_multi``
#: monkeypatch needed.
_SECTION_TEXT = (
    "Piezo oscillator thermal drift calibration routine for the "
    "mechanical cartridge assembly."
)
_PAPER_CHUNK_TEXT = (
    "In this study we describe a piezo oscillator thermal drift "
    "calibration routine for the mechanical cartridge assembly, "
    "achieving sub-micron repeatability across thermal cycles."
)


def _project_with_draft(draft: DraftHandler, hub: Hub, slug: str) -> tuple[int, int]:
    """A fresh project + a one-heading draft bound to it (``draft-of``
    link), the heading's paragraph carrying :data:`_SECTION_TEXT`.
    Returns ``(project_ref_id, draft_ref_id)``."""
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id=slug, title="Doc", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(id=slug, chunk_kind="heading", text="Calibration", at={"after": title_h})
    intro_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.text == "Calibration"
    )
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text=_SECTION_TEXT,
        at={"into": intro_h, "last": True},
    )
    return proj, ref.id


def _paper_with_matching_chunk(hub: Hub, *, slug: str) -> tuple[int, str]:
    """A real paper ref with one body chunk whose text overlaps
    :data:`_SECTION_TEXT`'s distinctive terms. Returns ``(paper_ref_id,
    chunk_text)``."""
    from precis.store.types import BlockInsert

    paper = hub.live_store.insert_ref(kind="paper", slug=slug, title="Cartridge Paper")
    hub.live_store.insert_blocks(paper.id, [BlockInsert(pos=0, text=_PAPER_CHUNK_TEXT)])
    return paper.id, _PAPER_CHUNK_TEXT


def _bound_todo(hub: Hub, project_ref_id: int, title: str = "tick") -> int:
    return hub.live_store.insert_ref(
        kind="todo", slug=None, title=title, parent_id=project_ref_id
    ).id


def test_planner_sources_block_uncited_draft(draft: DraftHandler, hub: Hub) -> None:
    """A plain draft-bound tick (no anchor/review/backfill): the
    pre-rendered source block carries a real candidate — pc handle AND its
    verbatim excerpt — plus the none-yet citations line (nothing cited)."""
    from precis.workers.planner_prompt import build_planner_prompts

    proj, _draft_ref_id = _project_with_draft(draft, hub, "src1")
    _paper_with_matching_chunk(hub, slug="src1-paper")
    todo = _bound_todo(hub, proj)

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    user = prompts.user
    assert "## Source material" in user
    assert "### Candidate corpus sources" in user
    assert re.search(r"\bpc\d+\b", user), user
    # the candidate's excerpt rides verbatim, not just its title
    assert "sub-micron repeatability across thermal cycles" in user
    assert "### Existing citations" in user
    assert "(none yet" in user


def test_planner_sources_block_cited_draft(draft: DraftHandler, hub: Hub) -> None:
    """A draft that already cites a paper surfaces it in the existing-
    citations block, ``✓ pa<id>`` style."""
    from precis.workers.planner_prompt import build_planner_prompts

    proj, draft_ref_id = _project_with_draft(draft, hub, "src2")
    cited = hub.live_store.insert_ref(
        kind="paper", slug="src2-cited", title="Cited Paper"
    )
    intro_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(draft_ref_id)
        if c.text == "Calibration"
    )
    draft.put(
        id=draft_ref_id,
        chunk_kind="paragraph",
        text=f"See prior work paper:{cited.id} for context.",
        at={"into": intro_h, "last": True},
    )
    todo = _bound_todo(hub, proj)

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    assert re.search(r"✓ pa\d+", prompts.user), prompts.user


def test_planner_sources_block_absent_when_anchored(
    draft: DraftHandler, hub: Hub
) -> None:
    """The anchored tick shape owns its own content (fisheye) — the plain
    source block must not also ride along."""
    from precis.workers.planner_prompt import build_planner_prompts

    proj, draft_ref_id = _project_with_draft(draft, hub, "src3")
    intro_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(draft_ref_id)
        if c.text == "Calibration"
    )
    todo = _bound_todo(hub, proj)
    hub.live_store.stamp_ref_meta(todo, {"anchor": intro_h})

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    assert "## Source material" not in prompts.user


def test_planner_sources_block_absent_for_review_todo(
    draft: DraftHandler, hub: Hub
) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    proj, _draft_ref_id = _project_with_draft(draft, hub, "src4")
    todo = _bound_todo(hub, proj, title="review it")
    hub.live_store.stamp_ref_meta(todo, {"review": "cites"})

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    assert "## Source material" not in prompts.user


def test_planner_sources_block_absent_without_bound_draft(hub: Hub) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    todo = hub.live_store.insert_ref(kind="todo", slug=None, title="just do it")
    prompts = build_planner_prompts(hub.live_store, ref_id=todo.id, model="opus")
    assert "## Source material" not in prompts.user


#: Placed mid-way through the oversized chunk so a leaked full-text render
#: (instead of the capped TOON row) is unambiguously detectable.
_LONG_DRAFT_MARKER = "UNIQUE-MID-CHUNK-MARKER-7f3ac91"


def test_planner_sources_block_outline_mode_over_cap(
    draft: DraftHandler, hub: Hub
) -> None:
    """A draft over the inline cap renders the capped TOON table, never the
    oversized chunk's full body text."""
    from precis.workers.planner_prompt import (
        _SOURCES_DRAFT_INLINE_CAP,
        build_planner_prompts,
    )

    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="src5", title="Doc", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="src5")
    assert ref is not None
    title_h = hub.live_store.drafts.reading_order(ref.id)[0].handle
    draft.put(id="src5", chunk_kind="heading", text="Body", at={"after": title_h})
    body_h = next(
        c.handle
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.text == "Body"
    )
    big_text = ("filler word " * 700) + _LONG_DRAFT_MARKER
    assert len(big_text) > _SOURCES_DRAFT_INLINE_CAP
    draft.put(
        id="src5",
        chunk_kind="paragraph",
        text=big_text,
        at={"into": body_h, "last": True},
    )
    todo = _bound_todo(hub, proj)

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    assert "## Source material" in prompts.user
    assert "sections: [" in prompts.user
    assert _LONG_DRAFT_MARKER not in prompts.user


def test_planner_sources_block_inline_mode_under_cap(
    draft: DraftHandler, hub: Hub
) -> None:
    """A small draft rides inline: full chunk text + the section header,
    not the truncated TOON table."""
    from precis.workers.planner_prompt import build_planner_prompts

    proj, _draft_ref_id = _project_with_draft(draft, hub, "src6")
    todo = _bound_todo(hub, proj)

    prompts = build_planner_prompts(hub.live_store, ref_id=todo, model="opus")
    assert "## Source material" in prompts.user
    assert "#### dc" in prompts.user
    assert _SECTION_TEXT in prompts.user
    assert "sections: [" not in prompts.user


def _stub_chunk(cid: int, parent: int | None, kind: str, text: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        chunk_id=cid, parent_chunk_id=parent, chunk_kind=kind, text=text, dc=f"dc{cid}"
    )


def test_draft_section_rows_keep_stray_top_level_chunks() -> None:
    """Every chunk lands in exactly one row: front matter before the first
    heading forms its own row, and a stray top-level paragraph (written
    ``at={'after': …}`` instead of ``'into'``) folds into the section it
    follows — nothing the block calls "the full draft" is dropped."""
    from precis.workers.planner_prompt import _draft_section_rows

    chunks = [
        _stub_chunk(1, None, "paragraph", "front matter"),
        _stub_chunk(2, None, "heading", "Intro"),
        _stub_chunk(3, 2, "paragraph", "intro body"),
        _stub_chunk(4, None, "paragraph", "stray after-sibling"),
        _stub_chunk(5, None, "heading", "Methods"),
        _stub_chunk(6, 5, "paragraph", "methods body"),
    ]
    rows = _draft_section_rows(chunks)
    covered = sorted(c.chunk_id for _, sub in rows for c in sub)
    assert covered == [1, 2, 3, 4, 5, 6]
    assert [t.chunk_id for t, _ in rows] == [1, 2, 5]
    assert 4 in [c.chunk_id for c in rows[1][1]]


def test_draft_state_cites_cell_counts_ref_level_and_hub_handles() -> None:
    """The per-section cites cell must count ref-level (``[pa…]``) and
    claim-hub (``[fi…]``) citations, not only chunk-level handles —
    otherwise it shows ``⚠0`` for a section the Existing-citations block
    in the same prompt lists as cited."""
    from precis.workers.planner_prompt import (
        _SOURCES_DRAFT_INLINE_CAP,
        _draft_state_block,
    )

    filler = "x" * (_SOURCES_DRAFT_INLINE_CAP + 1)  # force outline mode
    chunks = [
        _stub_chunk(1, None, "heading", "Cited section"),
        _stub_chunk(2, 1, "paragraph", f"claim [pa42] and hub [fi7]. {filler}"),
    ]
    block = _draft_state_block(chunks)
    assert "✓2" in block
    assert "⚠0" not in block


def test_draft_state_block_empty_draft() -> None:
    """A chunk-less draft says so instead of rendering a confusing
    "0 sections / full draft text above" shell."""
    from precis.workers.planner_prompt import _draft_state_block

    block = _draft_state_block([])
    assert "draft is empty" in block
    assert "full draft text" not in block
