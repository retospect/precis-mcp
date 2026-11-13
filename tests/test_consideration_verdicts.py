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
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry

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
