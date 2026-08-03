"""``taproot_backfill`` job_type — registration, submit-time param
validation, and the plugin dispatch (checkpointed, per-chunk-isolated
``[pc]``/``[pa]`` → ``[fi]`` conversion) against a fake DispatchContext.

Mirrors ``tests/test_draft_export_job.py``'s registration+dispatch shape.
For a genuine end-to-end conversion assertion (hub mint, evidence edge,
prose rewrite) with no LLM/embedder, ``precis.taproot.backfill.apply_chunk``
is monkeypatched to a thin wrapper around the REAL function with fake
``extract_fn``/``block_fn``/``judge_fn``/``merge_confirm_fn`` bound in —
the same cascade-injection technique ``tests/test_taproot_backfill.py``
uses directly against ``apply_chunk``. ``_dispatch`` does a local
``from precis.taproot.backfill import apply_chunk`` on every call, so the
monkeypatched module attribute is picked up each dispatch.
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
from precis.store.store import Store

# Captured at import time — BEFORE any test monkeypatches
# ``precis.taproot.backfill.apply_chunk`` to a fake. The fakes below call
# THIS reference (never re-import from the module), or patching it would
# recurse into the fake itself.
from precis.taproot.backfill import apply_chunk as _REAL_APPLY_CHUNK
from precis.taproot.canon import CanonicalClaim
from precis.taproot.seniority import is_claim_hub
from precis.workers.job_types import get_job_type, known_job_types
from tests.workers._helpers import seed_chunk, seed_ref


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


def _chunk_id(dc: str) -> int:
    return int(dc[2:])


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _pc_of(store: Store, *, paper_title: str = "src paper") -> tuple[int, str]:
    """A paper + one chunk on it; return ``(paper_ref_id, 'pc<chunk_id>')``."""
    paper = seed_ref(store, title=paper_title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=paper, text="grounding passage")
    return paper, f"pc{chunk_id}"


def _seed_sectioned_draft(
    draft: DraftHandler, hub: Hub, *, pc1: str, pc2: str
) -> dict[str, str]:
    """Build a draft ``nt``::

        T (title)
        Section A
          Para A1 [<pc1>].
        Section B
          Para B1 [<pc2>].

    Returns the ``dc<id>`` handle of every node, keyed by role.
    """
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    title_dc = hub.store.reading_order(ref.id)[0].dc

    r = draft.put(
        id="nt", chunk_kind="heading", text="Section A", at={"after": title_dc}
    )
    sec_a_dc = _dc(r.body)
    r = draft.put(
        id="nt", chunk_kind="paragraph", text=f"Para A1 [{pc1}].", at={"into": sec_a_dc}
    )
    para_a1_dc = _dc(r.body)

    r = draft.put(
        id="nt", chunk_kind="heading", text="Section B", at={"after": sec_a_dc}
    )
    sec_b_dc = _dc(r.body)
    r = draft.put(
        id="nt", chunk_kind="paragraph", text=f"Para B1 [{pc2}].", at={"into": sec_b_dc}
    )
    para_b1_dc = _dc(r.body)

    return {
        "title": title_dc,
        "sec_a": sec_a_dc,
        "para_a1": para_a1_dc,
        "sec_b": sec_b_dc,
        "para_b1": para_b1_dc,
    }


# ── fake DispatchContext (mirrors tests/test_draft_export_job.py) ─────────


@dataclass
class _FakeCtx:
    store: Any
    meta: dict[str, Any]
    ref_id: int = 0
    title: str = "taproot_backfill"
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
    spec = get_job_type("taproot_backfill")
    assert spec is not None and spec.dispatch is not None
    return spec


# ── registration ───────────────────────────────────────────────────────────


def test_taproot_backfill_registered() -> None:
    spec = _spec()
    assert "claude_inproc" in spec.compatible_executors
    assert not spec.requires  # in-process, no executor capability needed
    assert "taproot_backfill" in known_job_types()


# ── submit-time param validation ────────────────────────────────────────────


def test_submit_requires_scope(hub: Hub) -> None:
    pid = _proj(hub)
    with pytest.raises(BadInput, match="requires params.scope"):
        JobHandler(hub=hub).put(job_type="taproot_backfill", parent_id=pid, params={})


def test_submit_rejects_unknown_param(hub: Hub) -> None:
    pid = _proj(hub)
    with pytest.raises(BadInput, match="unknown params"):
        JobHandler(hub=hub).put(
            job_type="taproot_backfill",
            parent_id=pid,
            params={"scope": "nt", "bogus": 1},
        )


def test_submit_ok(hub: Hub) -> None:
    pid = _proj(hub)
    out = JobHandler(hub=hub).put(
        job_type="taproot_backfill", parent_id=pid, params={"scope": "nt"}
    )
    assert "id=" in out.body


# ── dispatch: real conversion (fake cascade fns, no LLM/embedder) ──────────


def _claim(sentence: str) -> CanonicalClaim:
    return CanonicalClaim(sentence=sentence, scope={})


def _extract_const(sentence: str) -> Any:
    return lambda span: _claim(sentence)


def _block_none(claim: Any, store: Any, embedder: Any) -> list[Any]:
    return []


def _never_called(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("cascade fn should not have been called")


def _wrap_apply_chunk(sentence_for: Any) -> Any:
    """Stand-in for ``precis.taproot.backfill.apply_chunk`` that calls the
    REAL function with a fake extract/block/judge/merge_confirm bound in —
    exercises the actual write path (hub mint, evidence edge, prose rewrite)
    with no LLM/embedder. ``sentence_for(chunk_id) -> str | None``; ``None``
    raises (the failure-isolation tests' per-chunk trigger)."""

    def _fake(
        store: Any, embedder: Any, draft_handler: Any, chunk_id: int, **kw: Any
    ) -> Any:
        sentence = sentence_for(chunk_id)
        if sentence is None:
            raise RuntimeError(f"boom on dc{chunk_id}")
        return _REAL_APPLY_CHUNK(
            store,
            embedder,
            draft_handler,
            chunk_id,
            extract_fn=_extract_const(sentence),
            block_fn=_block_none,
            judge_fn=_never_called,
            merge_confirm_fn=_never_called,
            **kw,
        )

    return _fake


def test_dispatch_converts_pc_cites_and_lands_evidence(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    paper_a, pc1 = _pc_of(store, paper_title="paper A")
    _paper_b, pc2 = _pc_of(store, paper_title="paper B")
    handles = _seed_sectioned_draft(draft, hub, pc1=pc1, pc2=pc2)

    monkeypatch.setattr(
        "precis.taproot.backfill.apply_chunk",
        _wrap_apply_chunk(lambda _cid: "A claim."),
    )

    ctx = _FakeCtx(store=store, meta={"params": {"scope": "nt"}})
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    chunk = store.get_draft_chunk(handles["para_a1"])
    assert chunk is not None
    assert "[fi" in chunk.text, (chunk.text, ctx.events, ctx.meta_set)
    assert "[pc" not in chunk.text
    m = re.search(r"\[fi(\d+)\]", chunk.text)
    assert m is not None
    hub_ref_id = int(m.group(1))
    assert is_claim_hub(store, hub_ref_id)
    # evidence edge: the citing paper grounds the minted hub.
    with store.pool.connection() as conn:
        edge = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (paper_a, hub_ref_id),
        ).fetchone()
    assert edge is not None

    summaries = [t for k, t in ctx.events if k == "job_summary"]
    assert summaries and "converted" in summaries[0]
    assert ctx.meta_set.get("converted", 0) >= 1
    assert _chunk_id(handles["para_a1"]) in ctx.meta_set.get("done_chunk_ids", [])
    assert _chunk_id(handles["para_b1"]) in ctx.meta_set.get("done_chunk_ids", [])


def test_dispatch_isolates_one_chunk_failure(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    _paper_a, pc1 = _pc_of(store, paper_title="paper A")
    _paper_b, pc2 = _pc_of(store, paper_title="paper B")
    handles = _seed_sectioned_draft(draft, hub, pc1=pc1, pc2=pc2)
    bad_id = _chunk_id(handles["para_a1"])

    def _sentence_for(chunk_id: int) -> str | None:
        return None if chunk_id == bad_id else "A claim."

    monkeypatch.setattr(
        "precis.taproot.backfill.apply_chunk", _wrap_apply_chunk(_sentence_for)
    )

    ctx = _FakeCtx(store=store, meta={"params": {"scope": "nt"}})
    _spec().dispatch(ctx, _spec())

    # No terminal job failure — the bad chunk is isolated, not fatal.
    assert not ctx.failures, ctx.failures
    failed_events = [t for k, t in ctx.events if k == "job_event" and "FAILED" in t]
    assert any(f"dc{bad_id}" in t for t in failed_events)
    # the OTHER chunk still converted.
    good = store.get_draft_chunk(handles["para_b1"])
    assert good is not None and "[fi" in good.text
    # the bad chunk's prose is untouched ([pc…] survives).
    bad = store.get_draft_chunk(handles["para_a1"])
    assert bad is not None and f"[{pc1}]" in bad.text
    summaries = [t for k, t in ctx.events if k == "job_summary"]
    assert summaries and "1 failed" in summaries[0]
    # both chunks marked done (a failure still checkpoints — no infinite retry).
    assert bad_id in ctx.meta_set.get("done_chunk_ids", [])
    assert _chunk_id(handles["para_b1"]) in ctx.meta_set.get("done_chunk_ids", [])


def test_dispatch_skips_already_done_chunks(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    _paper_a, pc1 = _pc_of(store, paper_title="paper A")
    _paper_b, pc2 = _pc_of(store, paper_title="paper B")
    handles = _seed_sectioned_draft(draft, hub, pc1=pc1, pc2=pc2)
    done_id = _chunk_id(handles["para_a1"])

    calls: list[int] = []
    real_fake = _wrap_apply_chunk(lambda _cid: "A claim.")

    def _recording_fake(
        store: Any, embedder: Any, draft_handler: Any, chunk_id: int, **kw: Any
    ) -> Any:
        calls.append(chunk_id)
        return real_fake(store, embedder, draft_handler, chunk_id, **kw)

    monkeypatch.setattr("precis.taproot.backfill.apply_chunk", _recording_fake)

    ctx = _FakeCtx(
        store=store,
        meta={"params": {"scope": "nt"}, "done_chunk_ids": [done_id]},
    )
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    assert done_id not in calls  # checkpoint: never re-run
    assert _chunk_id(handles["para_b1"]) in calls
    # the pre-done chunk's prose is untouched (never rewrote it this pass).
    skipped = store.get_draft_chunk(handles["para_a1"])
    assert skipped is not None and f"[{pc1}]" in skipped.text
    assert done_id in ctx.meta_set.get("done_chunk_ids", [])


def test_dispatch_dc_scope_processes_only_that_sections_chunks(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = DraftHandler(hub=hub)
    _paper_a, pc1 = _pc_of(store, paper_title="paper A")
    _paper_b, pc2 = _pc_of(store, paper_title="paper B")
    handles = _seed_sectioned_draft(draft, hub, pc1=pc1, pc2=pc2)

    calls: list[int] = []
    real_fake = _wrap_apply_chunk(lambda _cid: "A claim.")

    def _recording_fake(
        store: Any, embedder: Any, draft_handler: Any, chunk_id: int, **kw: Any
    ) -> Any:
        calls.append(chunk_id)
        return real_fake(store, embedder, draft_handler, chunk_id, **kw)

    monkeypatch.setattr("precis.taproot.backfill.apply_chunk", _recording_fake)

    ctx = _FakeCtx(store=store, meta={"params": {"scope": handles["sec_a"]}})
    _spec().dispatch(ctx, _spec())

    assert not ctx.failures, ctx.failures
    got = set(calls)
    assert got == {_chunk_id(handles["sec_a"]), _chunk_id(handles["para_a1"])}
    assert _chunk_id(handles["sec_b"]) not in got
    assert _chunk_id(handles["para_b1"]) not in got
    # section B's prose is untouched — out of scope.
    untouched = store.get_draft_chunk(handles["para_b1"])
    assert untouched is not None and f"[{pc2}]" in untouched.text


def test_dispatch_missing_scope_records_failure(hub: Hub) -> None:
    ctx = _FakeCtx(store=hub.store, meta={"params": {}})
    _spec().dispatch(ctx, _spec())
    assert any("params.scope is required" in f for f in ctx.failures)


def test_dispatch_unknown_scope_records_failure(hub: Hub) -> None:
    ctx = _FakeCtx(store=hub.store, meta={"params": {"scope": "no-such-draft-xyz"}})
    _spec().dispatch(ctx, _spec())
    assert ctx.failures
