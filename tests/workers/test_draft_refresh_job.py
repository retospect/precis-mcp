"""``draft_refresh`` job_type — registration, submit-time param validation,
and the plugin dispatch (critique+rewrite, growth-gated apply, process
memory) against a fake DispatchContext.

Mirrors ``tests/workers/test_taproot_backfill_job.py``'s registration+
dispatch shape. The LLM call itself is monkeypatched at
``precis.utils.llm.router.dispatch`` (the module `_dispatch` local-imports
from on every call, so the patch is picked up each dispatch) to a canned
critique+rewrite — no real LLM/embedder involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.handlers.job import JobHandler
from precis.quest import dossier as dossier_mod
from precis.store.store import Store
from precis.utils.llm.router import LlmResult, Tier
from precis.workers.job_types import get_job_type, known_job_types
from tests.workers._helpers import seed_ref

_SENTINEL = "=== REWRITE ==="


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _seed_section_draft(
    draft: DraftHandler, hub: Hub, *, slug: str = "nt", paras: tuple[str, ...]
) -> dict[str, Any]:
    """Build a draft ``slug``::

        T (title)
        Section A
          <paras[0]>
          <paras[1]>
          ...

    Returns the ``dc<id>`` handle of the title + section heading + the
    list of paragraph handles, plus the draft's ref_id.
    """
    proj = _proj(hub)
    draft.put(id=slug, title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_dc = hub.live_store.drafts.reading_order(ref.id)[0].dc

    r = draft.put(
        id=slug, chunk_kind="heading", text="Section A", at={"after": title_dc}
    )
    sec_dc = _dc(r.body)
    para_dcs = []
    prev = sec_dc
    for text in paras:
        r = draft.put(id=slug, chunk_kind="paragraph", text=text, at={"into": sec_dc})
        para_dcs.append(_dc(r.body))
        prev = _dc(r.body)
    return {
        "ref_id": ref.id,
        "title": title_dc,
        "sec": sec_dc,
        "paras": para_dcs,
    }


def _link_serves_quest(draft: DraftHandler, *, slug: str, quest_id: int) -> None:
    draft.link(id=slug, target=f"quest:{quest_id}", rel="serves")


def _rewrite_result(
    critique: str, heading: str, paragraphs: list[str], *, error: str | None = None
) -> LlmResult:
    body = "\n\n".join(paragraphs)
    text = f"{critique}\n\n{_SENTINEL}\n{heading}\n\n{body}"
    return LlmResult(
        text=text,
        cost_usd=None,
        turns_used=None,
        model="fake",
        tier=Tier.BIG,
        error=error,
    )


# ── fake DispatchContext (mirrors tests/workers/test_taproot_backfill_job.py) ──


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "draft_refresh"
    events: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    meta_set: dict[str, Any] = field(default_factory=dict)
    status: str = "running"

    def set_status(self, v: str) -> None:
        self.status = v

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)
        self.meta_set.update(kw)

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)

    def is_cancel_requested(self) -> bool:
        return False


def _spec() -> Any:
    spec = get_job_type("draft_refresh")
    assert spec is not None and spec.dispatch is not None
    return spec


# ── registration ────────────────────────────────────────────────────────────


def test_draft_refresh_registered() -> None:
    spec = _spec()
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires
    assert "draft_refresh" in known_job_types()


# ── submit-time param validation ────────────────────────────────────────────


def test_submit_requires_draft(hub: Hub) -> None:
    pid = _proj(hub)
    with pytest.raises(BadInput, match="draft"):
        JobHandler(hub=hub).put(
            job_type="draft_refresh", parent_id=pid, params={"scope": "dc1"}
        )


def test_submit_requires_scope(hub: Hub) -> None:
    pid = _proj(hub)
    with pytest.raises(BadInput, match="scope"):
        JobHandler(hub=hub).put(
            job_type="draft_refresh", parent_id=pid, params={"draft": "nt"}
        )


def test_submit_rejects_unknown_param(hub: Hub) -> None:
    pid = _proj(hub)
    with pytest.raises(BadInput, match="unknown params"):
        JobHandler(hub=hub).put(
            job_type="draft_refresh",
            parent_id=pid,
            params={"draft": "nt", "scope": "dc1", "bogus": 1},
        )


def test_submit_ok(hub: Hub) -> None:
    pid = _proj(hub)
    out = JobHandler(hub=hub).put(
        job_type="draft_refresh",
        parent_id=pid,
        params={"draft": "nt", "scope": "dc1"},
    )
    assert "id=" in out.body


# ── dispatch: bad scope shape / missing params (record_failure paths) ──────


def test_dispatch_missing_draft_records_failure(hub: Hub) -> None:
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"scope": "dc1"}})
    _spec().dispatch(ctx, _spec())
    assert any("params.draft is required" in f for f in ctx.failures)


def test_dispatch_missing_scope_records_failure(hub: Hub) -> None:
    ctx = _FakeCtx(store=hub.live_store, meta={"params": {"draft": "nt"}})
    _spec().dispatch(ctx, _spec())
    assert any("params.scope is required" in f for f in ctx.failures)


def test_dispatch_non_dc_scope_records_failure(hub: Hub) -> None:
    ctx = _FakeCtx(
        store=hub.live_store, meta={"params": {"draft": "nt", "scope": "nt"}}
    )
    _spec().dispatch(ctx, _spec())
    assert any("dc<id> heading anchor" in f for f in ctx.failures)


def test_dispatch_scope_not_a_heading_records_failure(store: Store, hub: Hub) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_section_draft(draft, hub, paras=("Para one.",))
    para_dc = seeded["paras"][0]
    ctx = _FakeCtx(store=store, meta={"params": {"draft": "nt", "scope": para_dc}})
    _spec().dispatch(ctx, _spec())
    assert any("not a heading anchor" in f for f in ctx.failures)


# ── dispatch: happy path (owning quest present) ─────────────────────────────


def test_dispatch_happy_path_applies_and_writes_process_memory(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_section_draft(
        draft, hub, paras=("Old para one about a topic.", "Old para two, more detail.")
    )
    quest_id = seed_ref(store, title="Q", kind="quest")
    _link_serves_quest(draft, slug="nt", quest_id=quest_id)

    old_para_ids = [int(dc[2:]) for dc in seeded["paras"]]

    result = _rewrite_result(
        "The section repeats itself; tightened below.",
        "Section A",
        ["New para one, tightened.", "New para two, tightened."],
    )
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    assert ctx.meta_set.get("applied") is True

    # old chunks retired (no longer live), new chunks under the heading.
    live = store.drafts.reading_order(seeded["ref_id"])
    live_ids = {c.chunk_id for c in live}
    assert not (set(old_para_ids) & live_ids)
    sec_chunk = next(c for c in live if c.dc == seeded["sec"])
    assert sec_chunk.text == "Section A"  # heading unchanged (same text)
    new_body = [c for c in live if c.parent_chunk_id == sec_chunk.chunk_id]
    assert len(new_body) == 2
    assert {c.text for c in new_body} == {
        "New para one, tightened.",
        "New para two, tightened.",
    }

    summaries = [t for k, t in ctx.events if k == "job_summary"]
    assert summaries and "applied" in summaries[0]

    # process memory: logbook entry + attempt-tree node on the owning quest.
    blocks = store.blocks.list_blocks_for_ref(quest_id)
    logs = [b for b in blocks if b.chunk_kind == "quest_log"]
    assert logs and "refreshed" in logs[0].text
    ledger = dossier_mod.read_ledger(store, quest_id)
    assert "[tried]" in ledger
    assert f"draft_refresh nt {seeded['sec']}" in ledger


def test_dispatch_questless_draft_applies_without_process_memory(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_section_draft(draft, hub, paras=("Old para about a topic.",))

    result = _rewrite_result("Tightened.", "Section A", ["New para, tightened."])
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    assert ctx.meta_set.get("applied") is True
    live = store.drafts.reading_order(seeded["ref_id"])
    sec_chunk = next(c for c in live if c.dc == seeded["sec"])
    new_body = [c for c in live if c.parent_chunk_id == sec_chunk.chunk_id]
    assert [c.text for c in new_body] == ["New para, tightened."]


# ── dispatch: growth-gate refusal (section left unchanged) ─────────────────


def test_dispatch_gate_refusal_leaves_section_unchanged(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    old_text = "Short original."
    seeded = _seed_section_draft(draft, hub, paras=(old_text,))

    # Balloon the word count with no new citation handles — trips the
    # no-progress-growth ratchet (default: prev*1.15 + 50).
    ballooned = " ".join(f"word{i}" for i in range(200))
    result = _rewrite_result("Expanded a lot.", "Section A", [ballooned])
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures  # a refusal is not a job failure
    assert ctx.meta_set.get("applied") is False
    assert ctx.meta_set.get("gate_reason") == "no-progress-growth"
    para_chunk = store.drafts.get_draft_chunk(seeded["paras"][0])
    assert para_chunk is not None and para_chunk.text == old_text  # untouched
    events = [t for k, t in ctx.events if k == "job_event"]
    assert any("growth gate refused" in t for t in events)
    summaries = [t for k, t in ctx.events if k == "job_summary"]
    assert summaries and "refused" in summaries[0]


# ── dispatch: parse failure (no sentinel) ───────────────────────────────────


def test_dispatch_parse_failure_records_failure(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    old_text = "Original text."
    seeded = _seed_section_draft(draft, hub, paras=(old_text,))

    no_sentinel = LlmResult(
        text="Just a critique, no rewrite marker.",
        cost_usd=None,
        turns_used=None,
        model="fake",
        tier=Tier.BIG,
        error=None,
    )
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: no_sentinel)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert any("could not parse" in f for f in ctx.failures)
    para_chunk = store.drafts.get_draft_chunk(seeded["paras"][0])
    assert para_chunk is not None and para_chunk.text == old_text  # untouched


# ── dispatch: preserved chunks (prose refresh, never destroys a table) ─────


def test_dispatch_preserves_table_chunk(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    seeded = _seed_section_draft(draft, hub, paras=("Old paragraph about a topic.",))

    r = draft.put(
        id="nt",
        chunk_kind="table",
        table={"header": ["x", "y"], "rows": [[1, 2], [3, 4]]},
        caption="A table.",
        at={"into": seeded["sec"]},
    )
    table_dc = _dc(r.body)
    table_before = store.drafts.get_draft_chunk(table_dc)
    assert table_before is not None
    table_chunk_id = table_before.chunk_id
    table_text_before = table_before.text

    result = _rewrite_result(
        "Tightened the prose; left the table alone.",
        "Section A",
        ["New paragraph, tightened."],
    )
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    assert ctx.meta_set.get("applied") is True

    # the table chunk survives un-retired, same chunk_id, same text.
    table_after = store.drafts.get_draft_chunk(table_dc)
    assert table_after is not None
    assert table_after.chunk_id == table_chunk_id
    assert table_after.text == table_text_before

    live = store.drafts.reading_order(seeded["ref_id"])
    live_ids = {c.chunk_id for c in live}
    assert table_chunk_id in live_ids
    old_para = store.drafts.get_draft_chunk(seeded["paras"][0])
    assert old_para is not None
    assert old_para.chunk_id not in live_ids  # retired, no longer live
    sec_chunk = next(c for c in live if c.dc == seeded["sec"])
    new_paras = [
        c
        for c in live
        if c.parent_chunk_id == sec_chunk.chunk_id and c.chunk_kind == "paragraph"
    ]
    assert [c.text for c in new_paras] == ["New paragraph, tightened."]


def test_dispatch_preserves_sub_heading_subtree(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested subsection (its heading + its own paragraph) is preserved
    whole — never a retire target, and counted/rendered as PRESERVED
    content, not silently dropped."""
    draft = DraftHandler(hub=hub)
    seeded = _seed_section_draft(draft, hub, paras=("Old paragraph about a topic.",))

    r = draft.put(
        id="nt", chunk_kind="heading", text="Sub B", at={"into": seeded["sec"]}
    )
    sub_dc = _dc(r.body)
    r = draft.put(
        id="nt", chunk_kind="paragraph", text="Inner para.", at={"into": sub_dc}
    )
    inner_para_dc = _dc(r.body)
    inner_before = store.drafts.get_draft_chunk(inner_para_dc)
    assert inner_before is not None
    inner_chunk_id = inner_before.chunk_id

    result = _rewrite_result(
        "Tightened; left the subsection alone.",
        "Section A",
        ["New paragraph, tightened."],
    )
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    assert ctx.meta_set.get("applied") is True

    # the nested subsection (its heading AND its inner paragraph) survives.
    sub_after = store.drafts.get_draft_chunk(sub_dc)
    assert sub_after is not None
    live = store.drafts.reading_order(seeded["ref_id"])
    live_ids = {c.chunk_id for c in live}
    assert sub_after.chunk_id in live_ids
    assert inner_chunk_id in live_ids
    inner_after = store.drafts.get_draft_chunk(inner_para_dc)
    assert inner_after is not None and inner_after.text == "Inner para."

    # the direct paragraph events/summary count the subsection as preserved.
    events = [t for k, t in ctx.events if k == "job_event" and "preserved" in t]
    assert any("2 preserved chunk(s)" in t for t in events)  # heading + inner para


# ── dispatch: insert-before-retire (a mid-apply failure never orphans) ─────


def test_dispatch_insert_before_retire_survives_mid_apply_failure(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between insert and retire (e.g. ``retire_chunk`` raising)
    must never leave the section with zero live paragraphs — the new
    paragraph lands FIRST, so the section has duplicated (old + new) live
    prose instead, and the job records failure."""
    draft = DraftHandler(hub=hub)
    old_text = "Old paragraph."
    seeded = _seed_section_draft(draft, hub, paras=(old_text,))

    result = _rewrite_result("Tightened.", "Section A", ["New paragraph, tightened."])
    monkeypatch.setattr("precis.utils.llm.router.dispatch", lambda _req: result)

    calls: list[str] = []

    def _boom(handle: str, **_kw: Any) -> None:
        calls.append(handle)
        raise RuntimeError("boom: retire failed")

    monkeypatch.setattr(store.drafts, "retire_chunk", _boom)

    ctx = _FakeCtx(
        store=store, meta={"params": {"draft": "nt", "scope": seeded["sec"]}}
    )
    _spec().dispatch(ctx, _spec())

    # the job records failure (not a silent success).
    assert ctx.failures
    assert any("apply failed" in f for f in ctx.failures)
    # retire was reached — i.e. AFTER the insert already ran/committed.
    assert calls

    # the section still has live paragraph(s) — never orphaned.
    live = store.drafts.reading_order(seeded["ref_id"])
    sec_chunk = next(c for c in live if c.dc == seeded["sec"])
    live_paras = [
        c
        for c in live
        if c.parent_chunk_id == sec_chunk.chunk_id and c.chunk_kind == "paragraph"
    ]
    assert live_paras  # not empty — insert-before-retire held
    texts = {c.text for c in live_paras}
    assert "New paragraph, tightened." in texts  # the insert succeeded
    assert old_text in texts  # the retire failed, so the old one is still live
