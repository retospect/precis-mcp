"""Consideration verdicts (spec me626852, gr662069).

The general "checked, does not fit for this question, at <UTC>, because
<reason>" record: a ``considered`` edge (scope memory/question → source)
carries the latest verdict in ``links.meta`` while every judgment lands an
append-only ``considerations`` row; ``search(args={'novel_for': <scope>})``
consumes the valid scoped exclusions, and staleness reopens review.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.graph.garden import apply as doors
from precis.graph.garden.apply import apply_merge
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.store.revision_context import revision_context
from precis.utils import handle_registry
from tests.test_garden_memory_apply import _apply
from tests.test_garden_memory_apply import _proposal as _rewrite_proposal
from tests.test_garden_memory_compact import _dispositions
from tests.test_garden_memory_compact import _proposal as _compact_proposal
from tests.test_garden_memory_retirement_undo import (
    _propose as _garden_proposal,
)
from tests.test_garden_memory_retirement_undo import (
    _record as _retirement_record,
)
from tests.test_garden_memory_retirement_undo import (
    _successor,
    _thread,
)

_Q = "consideration candidate topic"


def _mk_memory(store: Store, *, title: str, text: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "memory_body"})],
    )
    return ref.id


def _mk_paper(store: Store, *, slug: str, title: str, text: str) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title)
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text)])
    return ref.id


def _pc(store: Store, ref_id: int) -> str:
    """The ref's first body chunk handle (``pc<N>``) — the shape a paper
    hit renders."""
    chunk_id = store.chunks.list_chunks_for_ref(ref_id)[0].id
    return handle_registry.format_handle("paper", chunk_id, chunk=True)


def _judge(
    store: Store,
    scope_id: int,
    source_id: int,
    *,
    verdict: str = "not-useful",
    depth: str = "abstract",
    reason: str = "off the question's axis",
    **meta: object,
) -> None:
    store.add_link(
        src_ref_id=scope_id,
        dst_ref_id=source_id,
        relation="considered",
        meta={"verdict": verdict, "depth": depth, "reason": reason, **meta},
    )


def _history(store: Store, scope_id: int) -> list[dict]:
    return store.consideration_history(scope_ref_id=scope_id)


# ── write door ──────────────────────────────────────────────────────────


def test_considered_edge_stamps_and_writes_history(store: Store) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p1", title="P1", text=f"{_Q} off-axis method.")
    _judge(
        store,
        scope,
        src,
        reason="lithium-only, question is sodium",
        model="m-1",
        run="run-7",
        passages=["pc-quote"],
    )
    edge = store.links_for(scope, relation="considered", direction="out")
    assert len(edge) == 1
    meta = edge[0].meta
    assert meta["verdict"] == "not-useful"
    assert meta["depth"] == "abstract"
    assert meta["scope_sha"] and meta["source_sha"]
    assert meta["checked_at"]
    assert meta["judgments"] == 1
    hist = _history(store, scope)
    assert len(hist) == 1
    j = hist[0]
    assert j["verdict"] == "not-useful"
    assert j["reason"] == "lithium-only, question is sodium"
    assert j["reading_depth"] == "abstract"
    assert j["model"] == "m-1" and j["run"] == "run-7"
    assert j["passages"] == ["pc-quote"]
    assert j["link_id"] == edge[0].id


def test_considered_requires_verdict_reason_depth(store: Store) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p2", title="P2", text=f"{_Q} x.")
    for meta in (
        {"depth": "abstract", "reason": "r"},  # no verdict
        {"verdict": "not-useful", "reason": "r"},  # no depth
        {"verdict": "not-useful", "depth": "abstract"},  # no reason
        {"verdict": "bogus", "depth": "abstract", "reason": "r"},
        {"verdict": "not-useful", "depth": "bogus", "reason": "r"},
        {"verdict": "not-useful", "depth": "abstract", "reason": "."},
        {
            "verdict": "not-useful",
            "depth": "abstract",
            "reason": "r",
            "scope_sha": "forged",
        },
    ):
        with pytest.raises(BadInput):
            store.add_link(
                src_ref_id=scope,
                dst_ref_id=src,
                relation="considered",
                meta=meta,
            )


def test_considered_depth_none_is_acquisition_not_rejection(
    store: Store,
) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p3", title="P3", text=f"{_Q} y.")
    # Missing text is acquisition state — never a rejection.
    for verdict in ("not-useful", "useful"):
        with pytest.raises(BadInput):
            _judge(store, scope, src, verdict=verdict, depth="none")
    _judge(
        store, scope, src, verdict="uncertain", depth="none", reason="no fulltext yet"
    )
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert len(found.needs_text) == 1


def test_considered_rejudgment_keeps_every_verdict(store: Store) -> None:
    """Canonical-identity dedup keeps the edge at one row but never
    collapses two runs' verdicts: the edge shows the latest, the history
    keeps all."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p4", title="P4", text=f"{_Q} z.")
    _judge(store, scope, src, verdict="not-useful", reason="first run: out")
    _judge(
        store,
        scope,
        src,
        verdict="useful",
        depth="fulltext",
        reason="second run: actually on-axis",
    )
    edge = store.links_for(scope, relation="considered", direction="out")
    assert len(edge) == 1
    assert edge[0].meta["verdict"] == "useful"
    assert edge[0].meta["judgments"] == 2
    hist = _history(store, scope)
    assert len(hist) == 2
    assert {j["verdict"] for j in hist} == {"not-useful", "useful"}
    # One paper rejected for one scope, useful to another (me626852 AC).
    other = _mk_memory(store, title="other scope", text="another question")
    assert src not in store.consideration_exclusions(other).excluded


# ── validity / reconsideration ──────────────────────────────────────────


def test_material_change_queues_reconsideration(store: Store) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p5", title="P5", text=f"{_Q} w.")
    _judge(store, scope, src)
    assert src in store.consideration_exclusions(scope).excluded
    # A material scope change (retitle) leaves the verdict stale.
    store.update_ref(scope, title="reframed question")
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert len(found.stale) == 1
    assert found.stale[0].stale == "scope"
    queue = store.consideration_reconsiderations(scope_ref_id=scope)
    assert [v.dst_ref_id for v in queue] == [src]
    # History is untouched — ordinary edits never clear it.
    assert len(_history(store, scope)) == 1


def test_source_change_queues_reconsideration(store: Store) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p6", title="P6", text=f"{_Q} v.")
    _judge(store, scope, src)
    store.update_ref(src, title="P6 corrected")
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert found.stale[0].stale == "source"


def test_unlink_keeps_history(store: Store) -> None:
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-p7", title="P7", text=f"{_Q} u.")
    _judge(store, scope, src)
    store.remove_link(src_ref_id=scope, dst_ref_id=src, relation="considered")
    # An explicit door removal is a withdrawal: the pair leaves the
    # scoped read AND the reconsider queue (unlike a cascade edge loss),
    # and the judgment row is stamped — never deleted.
    assert store.scoped_considerations(scope) == []
    assert store.consideration_reconsiderations(scope_ref_id=scope) == []
    hist = _history(store, scope)
    assert len(hist) == 1
    assert hist[0]["withdrawn_at"] is not None


# ── the novelty facet ───────────────────────────────────────────────────


def test_novel_for_excludes_and_reports(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    scope = _mk_memory(store, title="scope", text="the question")
    rejected = _mk_paper(
        store, slug="cv-n1", title="Out", text=f"{_Q} rejected method."
    )
    kept = _mk_paper(store, slug="cv-n2", title="In", text=f"{_Q} kept method.")
    _judge(store, scope, rejected)
    out, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": _Q,
            "page_size": 20,
            "__extras__": {"novel_for": f"me{scope}"},
        },
    )
    assert not is_error, out
    pc_rejected = _pc(store, rejected)
    pc_kept = _pc(store, kept)
    assert pc_kept in out
    assert pc_rejected not in out
    assert "novel_for=" in out and "excluded" in out
    # Without the facet the same paper surfaces — global search unaffected.
    plain, _ = rt.dispatch_with_status(
        "search", {"kind": "paper", "q": _Q, "page_size": 20}
    )
    assert pc_rejected in plain
    # Another scope does not inherit the exclusion.
    other = _mk_memory(store, title="other", text="another question")
    scoped, _ = rt.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": _Q,
            "page_size": 20,
            "__extras__": {"novel_for": f"me{other}"},
        },
    )
    assert pc_rejected in scoped


def test_novel_for_reports_stale_not_exclude(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-n3", title="Stale", text=f"{_Q} stale.")
    _judge(store, scope, src)
    store.update_ref(src, title="Stale revised")
    out, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": _Q,
            "page_size": 20,
            "__extras__": {"novel_for": f"me{scope}"},
        },
    )
    assert not is_error, out
    # A stale verdict reopens review — it does NOT exclude.
    assert _pc(store, src) in out
    assert "stale" in out


def test_novel_for_bad_scope_refuses(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    out, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "paper", "q": _Q, "__extras__": {"novel_for": "me999999"}},
    )
    assert is_error
    assert "novel_for" in out and "resolve" in out


# ── the inspect view ────────────────────────────────────────────────────


def test_get_view_considerations(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-g1", title="Seen", text=f"{_Q} seen.")
    _judge(store, scope, src, reason="out of scope", model="m-9")
    out, is_error = rt.dispatch_with_status(
        "get",
        {"kind": "memory", "id": f"me{scope}", "view": "considerations"},
    )
    assert not is_error, out
    assert "not-useful" in out and "out of scope" in out
    assert "abstract" in out  # an abstract-only verdict shows as such
    assert "Judgment history" in out


# ── review td662210: replace-on-rejudgment, chunk survival, quest door ──


def test_rejudgment_replaces_omitted_judgment_fields(store: Store) -> None:
    """A re-judgment replaces the verdict block: optional keys the new
    judgment omits (passages, actor/model/run/prompt) are gone from the
    edge — never silently retained from the previous judgment — while the
    history keeps each run's own provenance."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r1", title="R1", text=f"{_Q} r1.")
    _judge(
        store,
        scope,
        src,
        verdict="not-useful",
        reason="first run: out",
        passages=["pc-quote"],
        actor="a-1",
        model="m-1",
        run="run-1",
        prompt="p-1",
    )
    _judge(
        store,
        scope,
        src,
        verdict="useful",
        depth="fulltext",
        reason="second run: on-axis after all",
    )
    meta = store.links_for(scope, relation="considered", direction="out")[0].meta
    assert meta["verdict"] == "useful"
    assert meta["reason"] == "second run: on-axis after all"
    for omitted in ("passages", "actor", "model", "run", "prompt"):
        assert omitted not in meta, f"{omitted} retained from the old judgment"
    hist = _history(store, scope)
    assert len(hist) == 2
    by_verdict = {j["verdict"]: j for j in hist}
    assert by_verdict["not-useful"]["model"] == "m-1"
    assert by_verdict["not-useful"]["passages"] == ["pc-quote"]
    assert by_verdict["not-useful"]["prompt"] == "p-1"
    assert by_verdict["useful"]["model"] is None


def test_chunk_replacement_preserves_judgment_history(store: Store) -> None:
    """A chunk-grounded verdict survives the routine re-ingest chunk
    replacement (DELETE + INSERT): the edge cascades with its chunk, but
    the judgment row — the auditable record — stays. Its reconsideration
    state survives independently of the live link too: the judgment sits
    in the reopen queue as ``edge-lost`` until re-judged (review
    td662210), distinct from an explicit withdrawal."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r2", title="R2", text=f"{_Q} r2.")
    chunk_id = store.chunks.list_chunks_for_ref(src)[0].id
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "passage-level mismatch",
        },
    )
    assert len(_history(store, scope)) == 1
    # Re-ingest replaces body chunks: old chunk row deleted, new one inserted.
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    # The edge died with its chunk (links FK cascade — pre-existing). The
    # judgment stays queued as edge-lost — never excluded, never dropped.
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert [v.stale for v in found.stale] == ["edge-lost"]
    assert [v.dst_ref_id for v in found.stale] == [src]
    assert store.scoped_considerations(scope)[0].stale == "edge-lost"
    queue = store.consideration_reconsiderations(scope_ref_id=scope)
    assert [v.dst_ref_id for v in queue] == [src]
    # …and the unscoped queue sees it too.
    assert src in {v.dst_ref_id for v in store.consideration_reconsiderations()}
    hist = _history(store, scope)
    assert len(hist) == 1
    assert hist[0]["source_chunk_id"] == chunk_id
    assert hist[0]["reason"] == "passage-level mismatch"
    assert hist[0]["withdrawn_at"] is None


def test_scope_chunk_replacement_queues_edge_lost(store: Store) -> None:
    """The scope side cascades identically: a chunk-grounded scope's own
    re-ingest deletes the edge, and the judgment reopens as edge-lost
    rather than disappearing."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r5", title="R5", text=f"{_Q} r5.")
    scope_chunk = store.chunks.list_chunks_for_ref(scope)[0].id
    store.add_link(
        src_ref_id=scope,
        src_pos=0,
        dst_ref_id=src,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "abstract",
            "reason": "off the question's axis",
        },
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (scope_chunk,))
    queue = store.consideration_reconsiderations(scope_ref_id=scope)
    assert [(v.dst_ref_id, v.stale) for v in queue] == [(src, "edge-lost")]


def test_rejudgment_after_edge_loss_restores_verdict(store: Store) -> None:
    """Re-judging a pair whose edge was lost lands a fresh edge: the
    orphan disappears and the new verdict block governs."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r6", title="R6", text=f"{_Q} r6.")
    chunk_id = store.chunks.list_chunks_for_ref(src)[0].id
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "first run: passage mismatch",
        },
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    new_chunk = store.chunks.list_chunks_for_ref(src)
    assert new_chunk == []  # the replacement's INSERT is the ingest's job
    _judge(
        store,
        scope,
        src,
        verdict="useful",
        depth="fulltext",
        reason="second run: on-axis after all",
    )
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    assert src not in found.excluded  # 'useful' never excludes
    hist = _history(store, scope)
    assert len(hist) == 2


def test_withdrawal_then_rejudgment_reactivates(store: Store) -> None:
    """A withdrawn pair reactivates only through a new judgment: the
    fresh row is unwithdrawn while the earlier rows keep their stamp."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r7", title="R7", text=f"{_Q} r7.")
    _judge(store, scope, src)
    store.remove_link(src_ref_id=scope, dst_ref_id=src, relation="considered")
    assert store.consideration_reconsiderations(scope_ref_id=scope) == []
    _judge(
        store,
        scope,
        src,
        verdict="useful",
        depth="fulltext",
        reason="second look: on-axis",
    )
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    hist = _history(store, scope)
    assert len(hist) == 2
    by_verdict = {j["verdict"]: j for j in hist}
    assert by_verdict["not-useful"]["withdrawn_at"] is not None
    assert by_verdict["useful"]["withdrawn_at"] is None


def test_orphan_judgment_withdrawable_without_live_edge(store: Store) -> None:
    """After chunk loss no live edge remains, yet the door removal still
    withdraws the orphan judgment and it leaves the reconsider queue."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-w1", title="W1", text=f"{_Q} w1.")
    chunk_id = store.chunks.list_chunks_for_ref(src)[0].id
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={"verdict": "not-useful", "depth": "passages", "reason": "mismatch"},
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    assert len(store.consideration_reconsiderations(scope_ref_id=scope)) == 1
    store.remove_link(src_ref_id=scope, dst_ref_id=src, relation="considered")
    assert store.consideration_reconsiderations(scope_ref_id=scope) == []
    assert all(j["withdrawn_at"] is not None for j in _history(store, scope))


def test_withdraw_after_rejudgment_does_not_resurrect_orphan(
    store: Store,
) -> None:
    """Re-judging at a new endpoint retires the old orphan; withdrawing
    the new edge then leaves nothing queued."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-w2", title="W2", text=f"{_Q} w2.")
    chunk_id = store.chunks.list_chunks_for_ref(src)[0].id
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={"verdict": "not-useful", "depth": "passages", "reason": "first"},
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    _judge(store, scope, src, verdict="useful", depth="fulltext", reason="second")
    store.remove_link(src_ref_id=scope, dst_ref_id=src, relation="considered")
    assert store.consideration_reconsiderations(scope_ref_id=scope) == []
    assert store.scoped_considerations(scope) == []


def test_conflicting_chunk_endpoints_resolve_to_latest_verdict(
    store: Store,
) -> None:
    """Judging chunk A not-useful, then chunk B useful, leaves ONE current
    verdict for the ref pair (the later one) — the source is no longer
    excluded — while the history keeps both judgments."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-c1", title="C1", text=f"{_Q} one. {_Q} two.")
    chunks = store.chunks.list_chunks_for_ref(src)
    assert len(chunks) >= 1
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={"verdict": "not-useful", "depth": "passages", "reason": "A: off"},
    )
    assert src in store.consideration_exclusions(scope).excluded
    # Re-judge at the ref level (a different endpoint of the same pair).
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=src,
        relation="considered",
        meta={"verdict": "useful", "depth": "fulltext", "reason": "B: fits"},
    )
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    current = store.scoped_considerations(scope)
    assert [v.verdict for v in current] == ["useful"]
    assert len(_history(store, scope)) == 2


def test_quest_link_records_considered_verdict(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The general question/quest write surface: ``link(kind='quest',
    ..., rel='considered', meta=…)`` carries the verdict block — the base
    numeric-ref link accepts edge meta, so a quest scope needs no special
    door (gr662069 review)."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    src = _mk_paper(store, slug="cv-r3", title="R3", text=f"{_Q} r3.")
    out, is_error = rt.dispatch_with_status(
        "link",
        {
            "kind": "quest",
            "id": quest.id,
            "target": f"pa{src}",
            "rel": "considered",
            "meta": {
                "verdict": "not-useful",
                "reason": "off the quest's axis",
                "depth": "abstract",
            },
        },
    )
    assert not is_error, out
    edge = store.links_for(quest.id, relation="considered", direction="out")
    assert len(edge) == 1
    assert edge[0].meta["verdict"] == "not-useful"
    assert edge[0].meta["judgments"] == 1
    found = store.consideration_exclusions(quest.id)
    assert src in found.excluded


def test_considerations_view_pages_and_shows_evidence(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The inspect route: the judgment history pages (limit/offset with a
    window against the total) and each row shows its recorded evidence —
    passages, prompt and the revision pins it was judged against."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-r4", title="R4", text=f"{_Q} r4.")
    for n in range(3):
        _judge(
            store,
            scope,
            src,
            verdict="not-useful",
            reason=f"run {n}: out",
            passages=[f"pc-quote-{n}"],
            prompt=f"prompt-{n}",
        )
    out, is_error = rt.dispatch_with_status(
        "get",
        {
            "kind": "memory",
            "id": f"me{scope}",
            "view": "considerations",
            "__extras__": {"limit": 2, "offset": 0},
        },
    )
    assert not is_error, out
    assert "1–2 of 3" in out
    assert "prompt-2" in out and "pc-quote-2" in out  # newest first
    assert "prompt-0" not in out
    assert "scope_sha=" in out and "source_sha=" in out
    out2, is_error = rt.dispatch_with_status(
        "get",
        {
            "kind": "memory",
            "id": f"me{scope}",
            "view": "considerations",
            "__extras__": {"limit": 2, "offset": 2},
        },
    )
    assert not is_error, out2
    assert "3–3 of 3" in out2
    assert "prompt-0" in out2 and "prompt-2" not in out2


# ── review td662210: ref-level scope pin, per-pair write serialization ──


def test_scope_chunk_grounded_verdict_tracks_ref_revision(store: Store) -> None:
    """A chunk-grounded scope edge pins the scope REF's revision, not the
    grounded chunk's content: a retitle — or an edit to ANOTHER scope
    chunk — is a material question change and must reopen review even
    though the grounded passage is byte-identical (review td662210)."""
    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-s1", title="S1", text=f"{_Q} s1.")
    store.add_link(
        src_ref_id=scope,
        src_pos=0,
        dst_ref_id=src,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "abstract",
            "reason": "off the question's axis",
        },
    )
    assert src in store.consideration_exclusions(scope).excluded
    # A retitle never touched the grounded chunk, yet the question
    # changed: the exclusion drops and the verdict queues as 'scope'.
    store.update_ref(scope, title="reframed question")
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert [v.stale for v in found.stale] == ["scope"]
    # Re-judge valid, then change a DIFFERENT scope chunk — still a
    # material question change (the grounded ord-0 chunk is untouched).
    store.add_link(
        src_ref_id=scope,
        src_pos=0,
        dst_ref_id=src,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "abstract",
            "reason": "re-judged after the retitle",
        },
    )
    assert src in store.consideration_exclusions(scope).excluded
    store.chunks.insert_chunks(
        scope, [ChunkInsert(ord=1, text="an amended constraint")]
    )
    found = store.consideration_exclusions(scope)
    assert src not in found.excluded
    assert [v.stale for v in found.stale] == ["scope"]


def test_concurrent_pair_judgments_serialize_to_one_edge(store: Store) -> None:
    """Two ``rel='considered'`` writes at different chunk endpoints hit no
    unique-index conflict: without the per-pair lock each side's
    supersede-DELETE could miss the other's uncommitted edge, both
    commit live, and a superseded ``not-useful`` keeps excluding beside
    the newer verdict (review td662210). Deterministic half: a foreign
    holder of the pair advisory lock stalls the write until it lets go.
    Burst half: concurrent judgments at both endpoints leave exactly ONE
    live edge — one current verdict — while the history keeps every
    judgment."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from precis.store._consideration_ops import lock_consideration_pair

    scope = _mk_memory(store, title="scope", text="the question")
    src = _mk_paper(store, slug="cv-cc", title="CC", text=f"{_Q} cc.")

    # ── the pair lock is what a considered write waits on ───────────
    done = threading.Event()
    with store.pool.connection() as other:
        lock_consideration_pair(other, scope, src)

        def _judge_then_flag() -> None:
            _judge(store, scope, src, reason="held behind the pair lock")
            done.set()

        t = threading.Thread(target=_judge_then_flag)
        t.start()
        assert not done.wait(0.5)  # blocked behind the foreign lock holder
        other.rollback()  # releases the xact lock
    t.join(timeout=30)
    assert done.is_set()

    # ── burst: one current verdict survives any interleaving ────────
    start = threading.Barrier(8)

    def _judge_at(i: int) -> None:
        start.wait(timeout=10)
        store.add_link(
            src_ref_id=scope,
            dst_ref_id=src,
            dst_pos=0 if i % 2 else None,
            relation="considered",
            meta={
                "verdict": "not-useful" if i % 2 else "useful",
                "depth": "passages" if i % 2 else "fulltext",
                "reason": f"concurrent run {i}",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_judge_at, i) for i in range(8)]
        for f in futures:
            f.result(timeout=30)

    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1
    current = store.scoped_considerations(scope)
    assert len(current) == 1
    # The deterministic-half judgment plus the eight burst ones — the
    # history never collapses concurrent verdicts either.
    assert len(_history(store, scope)) == 9


# ── review td662210 FIX: the quest-scope ledger view ────────────────────


def test_quest_considerations_view_shows_ledger(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A quest scope exposes the same ledger memory does (review
    td662210): the write door already accepted quest judgments, but only
    kind='memory' served view='considerations' — a quest scope was
    pointed at view='links', which lists live edges but cannot show the
    judgment history or an edge-lost reconsideration."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    src = _mk_paper(store, slug="cv-q1", title="Q1", text=f"{_Q} q1.")
    _judge(store, quest.id, src, reason="off the quest's axis", model="m-4")
    out, is_error = rt.dispatch_with_status(
        "get",
        {"kind": "quest", "id": f"qu{quest.id}", "view": "considerations"},
    )
    assert not is_error, out
    assert f"qu{quest.id}" in out
    assert "not-useful" in out and "off the quest's axis" in out
    assert "abstract" in out  # an abstract-only verdict shows as such
    assert "Judgment history" in out and "m-4" in out


def test_quest_considerations_view_shows_edge_lost(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Chunk-loss inspection on a quest scope: a chunk-grounded verdict
    whose edge died in a chunk-replacement cascade surfaces in the
    REOPEN queue as edge-lost — the row view='links' structurally cannot
    show (no live edge is left to list)."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    src = _mk_paper(store, slug="cv-q2", title="Q2", text=f"{_Q} q2.")
    chunk_id = store.chunks.list_chunks_for_ref(src)[0].id
    store.add_link(
        src_ref_id=quest.id,
        dst_ref_id=src,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "passage-level mismatch",
        },
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    out, is_error = rt.dispatch_with_status(
        "get",
        {"kind": "quest", "id": quest.id, "view": "considerations"},
    )
    assert not is_error, out
    assert "edge-lost" in out and "REOPEN" in out
    assert "passage-level mismatch" in out
    assert "Judgment history" in out


def test_quest_considerations_view_pages_and_validates(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The quest ledger takes the same history window the memory one
    does — ``args={'limit': N, 'offset': M}`` — and refuses a bad one."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    src = _mk_paper(store, slug="cv-q3", title="Q3", text=f"{_Q} q3.")
    for n in range(3):
        _judge(store, quest.id, src, reason=f"run {n}: out")
    out, is_error = rt.dispatch_with_status(
        "get",
        {
            "kind": "quest",
            "id": quest.id,
            "view": "considerations",
            "__extras__": {"limit": 2, "offset": 0},
        },
    )
    assert not is_error, out
    assert "1–2 of 3" in out
    out2, is_error = rt.dispatch_with_status(
        "get",
        {
            "kind": "quest",
            "id": quest.id,
            "view": "considerations",
            "__extras__": {"limit": 2, "offset": 2},
        },
    )
    assert not is_error, out2
    assert "3–3 of 3" in out2
    bad, is_error = rt.dispatch_with_status(
        "get",
        {
            "kind": "quest",
            "id": quest.id,
            "view": "considerations",
            "__extras__": {"limit": 0},
        },
    )
    assert is_error
    assert "limit" in bad


def test_quest_considerations_view_refuses_id_list(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    out, is_error = rt.dispatch_with_status(
        "get",
        {"kind": "quest", "id": [quest.id], "view": "considerations"},
    )
    assert is_error
    assert "one quest id" in out


def test_novel_for_quest_scope_points_at_ledger(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The facet's inspect hint names the ledger view for a quest scope
    — not view='links', which cannot reach the judgment history or the
    edge-lost queue."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    quest = store.insert_ref(kind="quest", slug=None, title="the quest question")
    rejected = _mk_paper(store, slug="cv-q4", title="Out", text=f"{_Q} q4.")
    kept = _mk_paper(store, slug="cv-q5", title="In", text=f"{_Q} q5.")
    _judge(store, quest.id, rejected)
    out, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": _Q,
            "page_size": 20,
            "__extras__": {"novel_for": f"qu{quest.id}"},
        },
    )
    assert not is_error, out
    assert "view='considerations'" in out
    assert "view='links'" not in out
    assert _pc(store, rejected) not in out
    assert _pc(store, kept) in out


# ── review td662210 FIX: canonical-merge reconciliation ──────────────────


def _merge(store: Store, old: int, new: int) -> None:
    """The dedup/supersede link-migration step of a canonical merge."""
    with store.tx() as conn:
        store.migrate_links(old, new, conn=conn)


def test_merge_loser_later_verdict_beats_the_twin(store: Store) -> None:
    """Canonical-identity dedup: the survivor's pre-existing twin edge
    must NOT keep its verdict when the loser carries the LATER judgment
    — the canonical pair resolves to the latest judgment, whatever side
    it was judged on (review td662210)."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m1", title="M1", text=f"{_Q} m1.")
    dup = _mk_paper(store, slug="cv-m2", title="M1", text=f"{_Q} m1.")
    _judge(store, scope, surv, verdict="not-useful", reason="first run: out")
    _judge(
        store,
        scope,
        dup,
        verdict="useful",
        depth="fulltext",
        reason="second run: on-axis after all",
    )
    _merge(store, dup, surv)
    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1
    assert edges[0].dst_ref_id == surv
    assert edges[0].meta["verdict"] == "useful"
    found = store.consideration_exclusions(scope)
    assert surv not in found.excluded
    # Both judgments survive, re-pointed to the canonical source.
    hist = _history(store, scope)
    assert len(hist) == 2
    assert {j["source_ref_id"] for j in hist} == {surv}


def test_merge_survivor_later_verdict_keeps_exclusion_off(
    store: Store,
) -> None:
    """The symmetric order: the loser's earlier not-useful must not
    resurrect over the survivor's later useful judgment."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m3", title="M3", text=f"{_Q} m3.")
    dup = _mk_paper(store, slug="cv-m4", title="M4", text=f"{_Q} m4.")
    _judge(store, scope, dup, verdict="not-useful", reason="first run: out")
    _judge(
        store,
        scope,
        surv,
        verdict="useful",
        depth="fulltext",
        reason="second run: on-axis",
    )
    _merge(store, dup, surv)
    found = store.consideration_exclusions(scope)
    assert surv not in found.excluded
    current = store.scoped_considerations(scope)
    assert [v.verdict for v in current] == ["useful"]
    assert len(_history(store, scope)) == 2


def test_merge_later_not_useful_on_loser_still_excludes(store: Store) -> None:
    """Judgment order cuts the other way too: a later not-useful on the
    loser beats an earlier useful on the survivor."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m5", title="M5", text=f"{_Q} m5.")
    dup = _mk_paper(store, slug="cv-m6", title="M6", text=f"{_Q} m6.")
    _judge(store, scope, surv, verdict="useful", reason="first run: in")
    _judge(store, scope, dup, verdict="not-useful", reason="second run: out")
    _merge(store, dup, surv)
    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1
    assert edges[0].meta["verdict"] == "not-useful"
    # The two sources had different content, so the carried pin is stale
    # on the canonical ref — it excludes nothing, but the CURRENT
    # verdict on the edge is still the latest judgment's.
    found = store.consideration_exclusions(scope)
    assert [(v.dst_ref_id, v.stale) for v in found.stale] == [(surv, "source")]
    assert surv not in found.excluded


def test_merge_identical_source_stays_valid_no_false_reconsideration(
    store: Store,
) -> None:
    """The false-reconsideration regression: identical records deduped —
    the judgment's ref pin still matches (ref shas are content-derived),
    so the exclusion persists and nothing queues as edge-lost on the
    retired endpoint."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m7", title="Same", text=f"{_Q} same.")
    dup = _mk_paper(store, slug="cv-m8", title="Same", text=f"{_Q} same.")
    _judge(store, scope, dup, reason="lithium-only, question is sodium")
    _merge(store, dup, surv)
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    assert surv in found.excluded
    # Nothing lingers on the retired endpoint as a false edge-lost
    # reconsideration — scoped or unscoped.
    assert store.consideration_reconsiderations(scope_ref_id=scope) == []
    assert surv not in {v.dst_ref_id for v in store.consideration_reconsiderations()}
    hist = _history(store, scope)
    assert len(hist) == 1
    assert hist[0]["source_ref_id"] == surv


def test_merge_changed_source_goes_stale(store: Store) -> None:
    """A canonical merge onto genuinely different content IS a material
    source change: the carried verdict goes stale and queues
    reconsideration rather than excluding."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m9", title="Winner", text=f"{_Q} m9.")
    dup = _mk_paper(store, slug="cv-m10", title="Different", text=f"{_Q} m10.")
    _judge(store, scope, dup, reason="out")
    _merge(store, dup, surv)
    found = store.consideration_exclusions(scope)
    assert surv not in found.excluded
    assert [(v.dst_ref_id, v.stale) for v in found.stale] == [(surv, "source")]
    assert len(_history(store, scope)) == 1


def test_merge_collapses_conflicting_endpoint_edges(store: Store) -> None:
    """Two pairs can collapse onto different chunk endpoints of the
    canonical pair, leaving two live ``considered`` edges — the write
    door never leaves that state, so the merge keeps only the edge the
    latest judgment grounds."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m11", title="M11", text=f"{_Q} m11.")
    dup = _mk_paper(store, slug="cv-m12", title="M12", text=f"{_Q} m12.")
    # Loser judged first, ref-level; survivor judged later at its chunk.
    _judge(store, scope, dup, verdict="not-useful", reason="first: out")
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=surv,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "useful",
            "depth": "passages",
            "reason": "second: the passage fits",
        },
    )
    _merge(store, dup, surv)
    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1
    assert edges[0].meta["verdict"] == "useful"
    assert edges[0].dst_chunk_id is not None
    found = store.consideration_exclusions(scope)
    assert surv not in found.excluded


def test_merge_chunk_grounded_verdict_restamps_source_pin(
    store: Store,
) -> None:
    """A passage judgment re-grounded onto the canonical ref's
    identical-text chunk re-pins ``source_sha`` onto that twin: the
    judged text IS the canonical endpoint, so the verdict stays valid
    (chunk shas are id-derived — keeping the dead pin would read
    stale)."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m13", title="M13", text="shared passage.")
    dup = _mk_paper(store, slug="cv-m14", title="M14", text="shared passage.")
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=dup,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "the passage is off-axis",
        },
    )
    _merge(store, dup, surv)
    (edge,) = store.links_for(scope, relation="considered", direction="out")
    surv_chunk = store.chunks.list_chunks_for_ref(surv)[0].id
    assert edge.dst_chunk_id == surv_chunk
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    assert surv in found.excluded


def test_merge_body_move_keeps_passage_verdict_valid(store: Store) -> None:
    """The real dedup order: ``move_body`` lands the donor's chunks on
    the survivor (deleting the donor's) BEFORE ``migrate_links`` runs.
    The judgment's chunk pin must re-anchor onto the copy inside that
    move — a pin left on the deleted chunk id would read the verdict
    stale against the copy's id-derived sha (review td662210)."""
    from precis.ingest.body_ops import move_body

    scope = _mk_memory(store, title="scope", text="the question")
    surv = store.insert_ref(kind="paper", slug="cv-m22", title="M22")
    dup = _mk_paper(store, slug="cv-m23", title="M23", text="the moved passage.")
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=dup,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "the passage is off-axis",
        },
    )
    with store.tx() as conn:
        move_body(src_ref_id=dup, dst_ref_id=surv.id, conn=conn)
        store.migrate_links(dup, surv.id, conn=conn)
    (edge,) = store.links_for(scope, relation="considered", direction="out")
    surv_chunk = store.chunks.list_chunks_for_ref(surv.id)[0].id
    assert edge.dst_chunk_id == surv_chunk
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    assert surv.id in found.excluded
    hist = _history(store, scope)
    assert hist[0]["source_ref_id"] == surv.id
    assert hist[0]["source_chunk_id"] == surv_chunk


def test_merge_scope_side_history_follows_and_goes_stale(
    store: Store,
) -> None:
    """Scope-side merge (memory consolidation): the loser's judgments
    re-point onto the consolidated scope, and the carried verdict goes
    honestly stale — the consolidated question differs from what was
    judged — instead of stranding on the retired scope as a false
    edge-lost orphan."""
    old_scope = _mk_memory(store, title="old q", text="the old question")
    new_scope = _mk_memory(store, title="new q", text="the consolidated question")
    src = _mk_paper(store, slug="cv-m15", title="M15", text=f"{_Q} m15.")
    _judge(store, old_scope, src, reason="out")
    _merge(store, old_scope, new_scope)
    # The judgment and its edge moved; the old scope's ledger is empty.
    assert store.scoped_considerations(old_scope) == []
    hist = _history(store, new_scope)
    assert len(hist) == 1
    assert hist[0]["scope_ref_id"] == new_scope
    found = store.consideration_exclusions(new_scope)
    assert src not in found.excluded
    assert [(v.dst_ref_id, v.stale) for v in found.stale] == [(src, "scope")]
    # No edge-lost false reconsideration under the retired scope's pair.
    unscoped = store.consideration_reconsiderations()
    assert not any(v.stale == "edge-lost" and v.dst_ref_id == src for v in unscoped)


def test_merge_withdrawn_pair_stays_withdrawn(store: Store) -> None:
    """A deliberate withdrawal rides the merge too: the re-pointed
    history rows keep ``withdrawn_at``, so no exclusion and no reopen
    queue entry appears on the canonical source."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m16", title="M16", text=f"{_Q} m16.")
    dup = _mk_paper(store, slug="cv-m17", title="M17", text=f"{_Q} m17.")
    _judge(store, scope, dup, reason="out")
    store.remove_link(src_ref_id=scope, dst_ref_id=dup, relation="considered")
    _merge(store, dup, surv)
    found = store.consideration_exclusions(scope)
    assert surv not in found.excluded
    assert found.stale == ()
    assert store.scoped_considerations(scope) == []
    hist = _history(store, scope)
    assert len(hist) == 1
    assert hist[0]["source_ref_id"] == surv
    assert hist[0]["withdrawn_at"] is not None


def test_merge_losers_orphan_suppressed_by_canonical_edge(
    store: Store,
) -> None:
    """The exact false-reconsideration the review named: the loser's
    orphan judgment (its edge died to a chunk cascade) re-points onto
    the canonical pair, where the live edge suppresses it — pre-fix it
    stayed on the retired ref and queued ``edge-lost`` forever."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m18", title="M18", text=f"{_Q} m18.")
    dup = _mk_paper(store, slug="cv-m19", title="M19", text=f"{_Q} m19.")
    dup_chunk = store.chunks.list_chunks_for_ref(dup)[0].id
    store.add_link(
        src_ref_id=scope,
        dst_ref_id=dup,
        dst_pos=0,
        relation="considered",
        meta={
            "verdict": "not-useful",
            "depth": "passages",
            "reason": "first: the passage is off-axis",
        },
    )
    with store.pool.connection() as c:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (dup_chunk,))
    # Sanity: a live edge-lost orphan on the legacy pair before merge.
    assert [
        (v.dst_ref_id, v.stale)
        for v in store.consideration_reconsiderations(scope_ref_id=scope)
    ] == [(dup, "edge-lost")]
    _judge(
        store,
        scope,
        surv,
        verdict="useful",
        depth="fulltext",
        reason="second: on-axis",
    )
    _merge(store, dup, surv)
    found = store.consideration_exclusions(scope)
    assert found.stale == ()
    assert surv not in found.excluded
    assert [v.verdict for v in store.scoped_considerations(scope)] == ["useful"]
    assert len(_history(store, scope)) == 2


def test_merge_withdrawn_later_verdict_does_not_resurrect(
    store: Store,
) -> None:
    """Resolution reads UNWITHDRAWN judgments only: a later useful the
    owner deliberately withdrew stays withdrawn, and the loser's
    earlier standing not-useful still governs the canonical pair."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-m20", title="M20", text=f"{_Q} m20.")
    dup = _mk_paper(store, slug="cv-m21", title="M21", text=f"{_Q} m20.")
    _judge(store, scope, dup, verdict="not-useful", reason="first: out")
    _judge(store, scope, surv, verdict="useful", reason="second: in")
    store.remove_link(src_ref_id=scope, dst_ref_id=surv, relation="considered")
    _merge(store, dup, surv)
    # The canonical pair's live edge is the migrated not-useful; its
    # content pin differs (different title/text), so it queues 'source'
    # rather than excluding — but the withdrawn useful never comes back
    # as an orphan either.
    found = store.consideration_exclusions(scope)
    assert len(found.verdicts) == 1
    assert found.verdicts[0].verdict == "not-useful"
    assert [v.stale for v in found.stale] == ["source"]
    hist = _history(store, scope)
    assert len(hist) == 2
    by_verdict = {j["verdict"]: j for j in hist}
    assert by_verdict["useful"]["withdrawn_at"] is not None
    assert by_verdict["not-useful"]["withdrawn_at"] is None


# ── review td662210 FIX: the garden merge door refuses judged losers ────


def _live(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT retired_at IS NULL FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return bool(row and row[0])


def test_garden_merge_refuses_judged_loser_source(store: Store) -> None:
    """``apply_merge`` snapshots links for undo but has no judgment
    reconcile — a merge that would strand a live verdict on the retired
    loser (or let a collapsed edge keep the wrong twin's verdict)
    refuses outright; ``migrate_links`` is the door that reconciles
    (review td662210)."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-g1", title="G1", text=f"{_Q} g1.")
    dup = _mk_paper(store, slug="cv-g2", title="G2", text=f"{_Q} g2.")
    _judge(store, scope, dup, verdict="not-useful", reason="off-axis")
    with pytest.raises(BadInput, match="consideration"):
        apply_merge(store, dup, surv, actor="reto")
    # Nothing applied: the loser lives and the verdict is untouched.
    assert _live(store, dup)
    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1 and edges[0].dst_ref_id == dup
    hist = _history(store, scope)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None


def test_garden_merge_refuses_judged_loser_scope(store: Store) -> None:
    """The scope side is symmetric: a memory loser that judged a source
    refuses too — its verdicts would strand under a retired scope as
    permanent edge-lost orphans."""
    old_scope = _mk_memory(store, title="old scope", text="the question")
    new_scope = _mk_memory(store, title="new scope", text="the question")
    src = _mk_paper(store, slug="cv-g3", title="G3", text=f"{_Q} g3.")
    _judge(store, old_scope, src, verdict="not-useful", reason="off-axis")
    with pytest.raises(BadInput, match="consideration"):
        apply_merge(store, old_scope, new_scope, actor="reto")
    assert _live(store, old_scope)
    assert len(store.links_for(old_scope, relation="considered")) == 1


def test_garden_merge_allows_withdrawn_only_judgments(store: Store) -> None:
    """Withdrawn judgments are terminal history: they may keep the
    honest pre-merge pin, so they do not block the merge."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-g4", title="G4", text=f"{_Q} g4.")
    dup = _mk_paper(store, slug="cv-g5", title="G5", text=f"{_Q} g5.")
    _judge(store, scope, dup, verdict="not-useful", reason="off-axis")
    store.remove_link(src_ref_id=scope, dst_ref_id=dup, relation="considered")
    apply_merge(store, dup, surv, actor="reto")
    assert not _live(store, dup)
    # The withdrawn row still names the ref it actually judged — the
    # same provenance the merge record's prior_ref_id keeps.
    hist = _history(store, scope)
    assert len(hist) == 1
    assert hist[0]["source_ref_id"] == dup
    assert hist[0]["withdrawn_at"] is not None


def test_garden_merge_winner_side_judgments_untouched(store: Store) -> None:
    """A verdict naming only the winner never moves — the repoint
    cannot strand it, so the merge proceeds and the edge carries on."""
    scope = _mk_memory(store, title="scope", text="the question")
    surv = _mk_paper(store, slug="cv-g6", title="G6", text=f"{_Q} g6.")
    dup = _mk_paper(store, slug="cv-g7", title="G7", text=f"{_Q} g7.")
    _judge(store, scope, surv, verdict="not-useful", reason="off-axis")
    apply_merge(store, dup, surv, actor="reto")
    assert not _live(store, dup)
    edges = store.links_for(scope, relation="considered", direction="out")
    assert len(edges) == 1
    assert edges[0].dst_ref_id == surv
    assert edges[0].meta["verdict"] == "not-useful"
    # An identical-source judgment stays valid: the merge is not a
    # material change to either pinned revision.
    found = store.consideration_exclusions(scope)
    assert found.excluded == frozenset({surv})


# ── review td662210 FIX: garden retirement doors refuse judged refs ────
#
# ``apply_supersede`` / ``apply_retire`` / a split or compact retire all
# reach the memory delete door — ``migrate_links`` →
# ``migrate_considerations`` — from ``_retirement_snapshot``. The
# ``garden_retirement`` record snapshots links only: a re-pointed pin, a
# resolved pair, or a ``withdrawn_at`` the undo's ``delete_links``
# stamps on a migrated ``considered`` edge cannot be replayed. The
# apply refuses live judgments on the target or a canonical pair; the
# undo refuses live judgments naming the recorded scope.


def test_garden_retire_refuses_scope_with_live_verdict(store: Store) -> None:
    """A live judgment naming the retirement target refuses the apply
    (review td662210): the links-only snapshot could not replay the
    re-pointed pin or a withdrawn verdict."""
    target = _thread(store)
    src = _mk_paper(store, slug="cv-rt1", title="RT1", text=f"{_Q} rt1.")
    _judge(store, target, src, verdict="not-useful", reason="off-axis")
    prop = _garden_proposal(store, target, "retire")
    with pytest.raises(doors.MergeRefused, match="consideration") as exc:
        _apply(store, prop)
    assert exc.value.reason == "apply_refused"
    assert _live(store, target)
    edges = store.links_for(target, relation="considered", direction="out")
    assert len(edges) == 1
    assert edges[0].meta["verdict"] == "not-useful"
    hist = _history(store, target)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None


def test_garden_retire_refuses_judged_source(store: Store) -> None:
    """The source pin is symmetric: a memory judged AS a source refuses
    — the scope's verdict would strand on the tombstone."""
    scope = _mk_memory(store, title="scope", text="the question")
    target = _thread(store)
    _judge(store, scope, target, reason="off-axis")
    prop = _garden_proposal(store, target, "retire")
    with pytest.raises(doors.MergeRefused, match="consideration"):
        _apply(store, prop)
    assert _live(store, target)


def test_garden_supersede_refuses_judged_loser(store: Store) -> None:
    """The supersede door runs the same snapshot: the delete migrates the
    loser's edges onto the successor, so a live verdict on the loser
    refuses before any ledger reconcile runs."""
    successor = _successor(store)
    target = _thread(store, successor=successor)
    src = _mk_paper(store, slug="cv-rt2", title="RT2", text=f"{_Q} rt2.")
    _judge(store, target, src, reason="off-axis")
    prop = _garden_proposal(store, target, "supersede")
    with pytest.raises(doors.MergeRefused, match="consideration"):
        _apply(store, prop)
    assert _live(store, target)


def test_garden_supersede_refuses_canonical_pair_verdict(store: Store) -> None:
    """The successor side: a migrated ``considered`` copy lands in the
    undo record's ``removed`` set, and deleting it through the door
    withdraws the WHOLE canonical pair — including verdicts the
    successor already carried. A live judgment on a pair the migration
    would collapse onto refuses (review td662210)."""
    successor = _successor(store)
    target = _thread(store, successor=successor)
    src = _mk_paper(store, slug="cv-rt3", title="RT3", text=f"{_Q} rt3.")
    # A live considered edge whose judgments are all withdrawn — the
    # residue a garden undo leaves when it restores the edge row but
    # cannot un-stamp history — carries no live verdict on the target.
    _judge(store, target, src, verdict="not-useful", reason="stale marker")
    with store.pool.connection() as c:
        c.execute("UPDATE considerations SET withdrawn_at = now()")
    _judge(store, successor, src, reason="the canonical pair was judged")
    prop = _garden_proposal(store, target, "supersede")
    with pytest.raises(doors.MergeRefused, match="consideration"):
        _apply(store, prop)
    assert _live(store, target)
    # The successor's own verdict is untouched.
    hist = _history(store, successor)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None


def test_garden_retire_allows_withdrawn_verdicts(store: Store) -> None:
    """Withdrawn judgments are terminal history: withdraw first and the
    retirement applies; the undo restores the node while the stamped
    rows keep their honest pins."""
    scope = _mk_memory(store, title="scope", text="the question")
    target = _thread(store)
    src = _mk_paper(store, slug="cv-rt5", title="RT5", text=f"{_Q} rt5.")
    _judge(store, scope, target, reason="off-axis")
    store.remove_link(src_ref_id=scope, dst_ref_id=target, relation="considered")
    prop = _garden_proposal(store, target, "retire")
    _apply(store, prop)
    assert not _live(store, target)
    hist = _history(store, scope)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is not None
    record = _retirement_record(store, prop)
    doors.undo_retirement(store, record["retirement_id"], actor="operator")
    assert _live(store, target)


def test_garden_retirement_undo_refuses_migrated_judgments(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The undo side of the review's FIX: a record written with live
    judgments (pre-guard, or raced past it) has the delete's
    ``migrate_considerations`` re-point them onto the successor. The
    replay cannot un-pin them, and deleting the migrated ``considered``
    edge would stamp the pair withdrawn — ``undo_retirement`` refuses
    rather than corrupt the ledger."""
    successor = _successor(store)
    target = _thread(store, successor=successor)
    src = _mk_paper(store, slug="cv-rt4", title="RT4", text=f"{_Q} rt4.")
    _judge(store, target, src, reason="off-axis")
    prop = _garden_proposal(store, target, "supersede")
    # Simulate a pre-guard record: the apply migrates the judgment.
    monkeypatch.setattr(doors, "_guard_retirement_considerations", lambda *a, **k: None)
    _apply(store, prop)
    monkeypatch.undo()
    record = _retirement_record(store, prop)
    # Sanity: the migrate re-pointed the judgment onto the successor.
    hist = _history(store, successor)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None
    with pytest.raises(BadInput, match="consideration"):
        doors.undo_retirement(store, record["retirement_id"], actor="operator")
    # Nothing was replayed: the target stays retired, the judgment
    # stays unwithdrawn on the canonical pair — not silently withdrawn.
    tomb = store.get_ref(kind="memory", id=target, include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None
    hist = _history(store, successor)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None
    # The migrated edge still carries the verdict — no silent ledger loss.
    edges = store.links_for(successor, relation="considered", direction="out")
    assert len(edges) == 1 and edges[0].meta["verdict"] == "not-useful"


# ── review td662210 FIX: the garden memory replay keeps the ledger ────
#
# ``undo_rewrite``, ``undo_compact``'s edit restores and the ``keep``
# split undo replay edges verbatim: a ``considered`` edge the apply
# remapped onto a replacement chunk is deleted, then restored. The
# delete must not take the ``delete_links`` withdrawal door — a stamped
# ``withdrawn_at`` would leave the restored edge carrying a verdict
# whose history reads dead: it would still exclude sources while a
# later cascade loss queued no reconsideration.


def test_garden_rewrite_undo_restores_remapped_verdict(store: Store) -> None:
    """``undo_rewrite``'s replay delete is not a withdrawal: the
    ``considered`` edge remapped onto the rewritten body chunk comes
    back with its judgment live — a ``delete_links`` withdrawal would
    strand a live edge over dead history (review td662210)."""
    prop = _rewrite_proposal(store, "rewrite")
    target = prop.target
    src = _mk_paper(store, slug="cv-ur1", title="UR1", text=f"{_Q} ur1.")
    with revision_context(
        "judged scope chunk",
        actor="fixture",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        store.add_link(
            src_ref_id=target,
            src_pos=0,
            dst_ref_id=src,
            relation="considered",
            meta={
                "verdict": "not-useful",
                "depth": "abstract",
                "reason": "off the question's axis",
            },
        )
    rec = _apply(store, prop)
    # Apply remapped the edge onto the new body chunk; the judgment
    # stayed live through the chunk cascade (no door deleted it) and
    # the material scope change queues the verdict for reconsideration.
    assert _history(store, target)[0]["withdrawn_at"] is None
    queue = store.consideration_reconsiderations(scope_ref_id=target)
    assert [v.stale for v in queue] == ["scope"]

    doors.undo_rewrite(store, rec.rewrite_id, actor="operator")

    hist = _history(store, target)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None
    found = store.scoped_considerations(target)
    assert [(v.verdict, v.valid) for v in found] == [("not-useful", True)]
    assert src in store.consideration_exclusions(target).excluded
    # …and a later cascade loss still queues the reconsideration the
    # stamped withdrawal would have swallowed.
    with revision_context(
        "re-ingest replaces the body chunk",
        actor="fixture",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        store.chunks.replace_body_chunk(
            target,
            "Replacement body after the undo.",
            chunk_kind="memory_body",
            source="fixture",
        )
    assert [v.stale for v in store.scoped_considerations(target)] == ["edge-lost"]


def test_garden_rewrite_undo_keeps_ref_level_verdict(store: Store) -> None:
    """A ref-level ``considered`` edge never enters the replay set:
    apply leaves it in place, the undo touches nothing, and the verdict
    rides stale-on-scope at apply back to valid after undo."""
    prop = _rewrite_proposal(store, "rewrite")
    target = prop.target
    src = _mk_paper(store, slug="cv-ur2", title="UR2", text=f"{_Q} ur2.")
    _judge(store, target, src, reason="off the question's axis")
    rec = _apply(store, prop)
    assert _history(store, target)[0]["withdrawn_at"] is None
    doors.undo_rewrite(store, rec.rewrite_id, actor="operator")
    hist = _history(store, target)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None
    found = store.scoped_considerations(target)
    assert [(v.verdict, v.valid) for v in found] == [("not-useful", True)]
    assert src in store.consideration_exclusions(target).excluded


def test_garden_compact_undo_restores_remapped_verdict(store: Store) -> None:
    """The compact undo replays the same edit snapshot
    (``_restore_compact_body``): a ``considered`` edge the shell's
    keep-summary edit remapped survives the undo with its judgment —
    the same transient-delete rule as ``undo_rewrite``."""
    target, review_id, _ = _compact_proposal(
        store,
        "Live claim one.\nLive claim two.\n",
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=lambda c: {
            "edits": [],
            "puts": [],
            "sentences": _dispositions(
                c,
                {"disposition": "keep"},
                {"disposition": "keep"},
            ),
            "shell": {
                "fate": "keep",
                "summary": "Live state: kept.\nOwner: ops.",
                "expires_days": 14,
            },
        },
    )
    src = _mk_paper(store, slug="cv-ur3", title="UR3", text=f"{_Q} ur3.")
    with revision_context(
        "judged scope chunk",
        actor="fixture",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        store.add_link(
            src_ref_id=target,
            src_pos=0,
            dst_ref_id=src,
            relation="considered",
            meta={
                "verdict": "not-useful",
                "depth": "abstract",
                "reason": "off the question's axis",
            },
        )
    rec = doors.apply_compact(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    assert _history(store, target)[0]["withdrawn_at"] is None
    doors.undo_compact(store, rec["compact_id"], actor="fixture")
    hist = _history(store, target)
    assert len(hist) == 1 and hist[0]["withdrawn_at"] is None
    found = store.scoped_considerations(target)
    assert [(v.verdict, v.valid) for v in found] == [("not-useful", True)]
    assert src in store.consideration_exclusions(target).excluded
