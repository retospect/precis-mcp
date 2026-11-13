"""The ``griped`` detector: a reader's project:memory gripe is a replayable
test — dev-DB fixtures, no model or ledger writes beyond the proposal."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from precis.graph.garden import candidates as cand
from precis.graph.garden import consumer, runner
from precis.graph.garden.apply import apply_rewrite
from precis.graph.garden.proposals import (
    emit_proposal,
    key_block,
    pending_proposals,
    record_rejection,
)
from precis.graph.garden.proposer import Verdict
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from tests._refs import insert_gripe
from tests.test_garden_memory import _memory
from tests.test_garden_memory_apply import _body


def _gripe(
    store: Store,
    body: str,
    *,
    status: str = "open",
    route: list[dict[str, Any]] | None = None,
    tagged: bool = True,
) -> int:
    ref = insert_gripe(store, body.splitlines()[0][:60], status=status)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})]
    )
    if tagged:
        store.add_tag(ref.id, Tag.open("project:memory"), set_by="agent")
    if route is not None:
        store.update_ref(ref.id, meta_patch={"route": route})
    return int(ref.id)


def _route(*calls: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"seq": i, "kind": "memory", **c} for i, c in enumerate(calls, 1)]


def test_griped_joins_the_opt_in_memory_pass() -> None:
    assert "griped" in cand.detectors_for(("memory",))
    assert "griped" not in cand.detectors_for(("all",))


def test_candidates_per_named_memory_pinned_and_replayed(store: Store) -> None:
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(
        store,
        f"Goal: the pour policy. Route: me{keeper} and me{loser} disagree.",
        route=_route(
            {"verb": "search", "arg_sample": {"q": "pour"}},
            {"verb": "get", "arg_sample": {"id": f"me{loser}"}},
        ),
    )
    found = cand.griped_candidates(store)
    assert {c.target_id for c in found} == {keeper, loser}
    for candidate in found:
        assert candidate.detector == "griped"
        assert candidate.action == "rewrite"
        assert candidate.evidence["gripe"] == gid
        assert candidate.payload["gripe"] == gid
        assert candidate.payload["base_sha"] == candidate.evidence["content_sha"]
        assert candidate.evidence["supporting_shas"][f"me{keeper}"]
        assert candidate.payload["evidence_sha"]
        assert [m["ref_id"] for m in candidate.evidence["memories"]] == [
            keeper,
            loser,
        ]
        searches = [e for e in candidate.evidence["replay"] if e["verb"] == "search"]
        assert len(searches) == 1 and searches[0]["ranks"]
        gets = [e for e in candidate.evidence["replay"] if e["verb"] == "get"]
        assert gets == [
            {
                "seq": 2,
                "verb": "get",
                "id": f"me{loser}",
                "ref_id": loser,
                "resolves": True,
            }
        ]
        assert candidate.payload["ops"], "the evaluator needs a span allow-list"
        assert all(op["text"] is None for op in candidate.payload["ops"])


def test_closed_tagless_and_memoryless_gripes_emit_nothing(store: Store) -> None:
    _gripe(store, "a complaint without any handles", route=[])
    _gripe(store, "same, but closed", status="done")
    _gripe(store, "same, but not project:memory", tagged=False)
    _gripe(store, "parked gripe", status="parked")
    assert cand.griped_candidates(store) == []


def test_route_replay_records_hits_gets_and_skips(store: Store) -> None:
    mem = _memory(store, "The interim exception narrows the pour rule.")
    replay = cand.route_replay(
        store,
        _route(
            {"verb": "search", "arg_sample": {"q": "interim exception"}},
            {"verb": "search", "arg_sample": {}},
            {"verb": "get", "arg_sample": {"id": f"me{mem}"}},
            {"verb": "get", "arg_sample": {"id": "me999999"}},
        ),
    )
    assert replay[0]["hits"] and replay[0]["hits"][0]["ref_id"] == mem
    assert replay[0]["hits"][0]["rank"] == 1
    assert replay[1]["not_replayed"]
    assert replay[2]["resolves"] is True and replay[2]["ref_id"] == mem
    assert replay[3]["resolves"] is False


def test_rewrite_applies_through_the_reviewed_door(store: Store) -> None:
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    _gripe(store, f"me{keeper} and me{loser} disagree on pours.", route=[])
    candidate = next(c for c in cand.griped_candidates(store) if c.target_id == loser)
    op = dict(candidate.payload["ops"][0])
    op["text"] = (
        "Pours are refused except interim leaf boards since the 2026-11-01 "
        f"ruling; me{keeper} carries the exception."
    )
    resolved = replace(candidate, payload={**candidate.payload, "ops": [op]})
    review_id = emit_proposal(store, resolved, rationale="same referent, one wrong")
    assert review_id is not None
    rec = apply_rewrite(store, review_id, actor="test")
    assert rec.target == loser
    assert op["find"] not in _body(store, loser)
    assert op["text"] in _body(store, loser)
    # The applied rewrite resolves the proposal row.
    assert pending_proposals(store, action="rewrite") == []


def test_replay_after_apply_closes_the_gripe(store: Store, monkeypatch: Any) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(
        store,
        f"me{keeper} and me{loser} disagree on pours.",
        route=_route({"verb": "search", "arg_sample": {"q": "interim"}}),
    )
    candidate = next(c for c in cand.griped_candidates(store) if c.target_id == loser)
    op = dict(candidate.payload["ops"][0])
    op["text"] = "Pours are refused except interim leaf boards (2026-11-01 ruling)."
    resolved = replace(candidate, payload={**candidate.payload, "ops": [op]})
    emit_proposal(store, resolved, rationale="same referent, one wrong")
    prop = pending_proposals(store, action="rewrite")[0]
    apply_rewrite(store, prop.review_id, actor="test")

    report = consumer.ConsumeReport()
    consumer._griped_close(store, prop, report)

    tags = {v for _ns, v in store.ref_tags_bulk([gid]).get(gid, [])}
    assert "done" in tags
    comments = [
        b.text
        for b in store.chunks.list_chunks_for_ref(gid)
        if b.chunk_kind == "gripe_comment"
    ]
    assert comments and comments[-1].startswith("resolved:")
    todos = store.list_refs(kind="todo", tags=["recall-set-review"], limit=10)
    assert todos, "the proven (query, goal) pairs wait for operator review"
    pairs = todos[0].meta["recall_set_proposals"]
    assert pairs and pairs[0]["want"] == f"me{keeper}"


def test_pending_sibling_proposals_hold_the_close(store: Store) -> None:
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(store, f"me{keeper} and me{loser} disagree.", route=[])
    for rid in (keeper, loser):
        candidate = next(c for c in cand.griped_candidates(store) if c.target_id == rid)
        assert emit_proposal(store, candidate, rationale="pending") is not None
    prop = next(p for p in pending_proposals(store) if p.target_id == loser)
    report = consumer.ConsumeReport()
    consumer._griped_close(store, prop, report)
    assert "done" not in {v for _ns, v in store.ref_tags_bulk([gid]).get(gid, [])}


def test_ask_verdict_files_a_gripe_linked_review_todo(store: Store) -> None:
    """The evaluator's ask becomes the runner's memory-review todo; for a
    griped candidate it links the filing gripe as well as the memory."""
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(store, f"me{keeper} and me{loser} disagree.", route=[])
    candidate = next(c for c in cand.griped_candidates(store) if c.target_id == loser)
    runner._file_review_todo(
        store, candidate, Verdict(accept=False, ask="undecidable, needs a human")
    )
    todos = store.list_refs(kind="todo", tags=["memory-review"], limit=10)
    assert len(todos) == 1
    dsts = {int(l.dst_ref_id) for l in store.links_for(todos[0].id, direction="out")}
    assert dsts >= {loser, gid}
    assert "done" not in {v for _ns, v in store.ref_tags_bulk([gid]).get(gid, [])}


def test_closed_review_todo_blocks_a_repeat_mint(store: Store) -> None:
    """A done review for the same (memory, gripe) pair is the answer: the
    runner must not mint another opus review (gr652974)."""
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    _gripe(store, f"me{keeper} and me{loser} disagree.", route=[])
    candidate = next(c for c in cand.griped_candidates(store) if c.target_id == loser)
    verdict = Verdict(accept=False, ask="needs a human")
    runner._file_review_todo(store, candidate, verdict)
    (todo,) = store.list_refs(kind="todo", tags=["memory-review"], limit=10)
    store.add_tag(
        todo.id, Tag.parse_strict("STATUS:done", kind="todo"), set_by="system"
    )
    runner._file_review_todo(store, candidate, verdict)
    assert len(store.list_refs(kind="todo", tags=["memory-review"], limit=10)) == 1


def test_no_change_rejection_blocks_re_ask_while_evidence_holds(
    store: Store,
) -> None:
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    _gripe(store, f"me{keeper} and me{loser} disagree.", route=[])
    candidate = cand.griped_candidates(store)[0]
    record_rejection(
        store,
        candidate,
        rationale="the replay already passes — consistent scoped answers",
        prompt_version="garden-propose.test",
    )
    # A proposer rejection blocks only the same prompt version re-asking.
    assert (
        key_block(
            store,
            "ref",
            candidate.target_id,
            candidate.key,
            prompt_version="garden-propose.test",
        )
        == "rejected"
    )
    assert (
        key_block(
            store,
            "ref",
            candidate.target_id,
            candidate.key,
            prompt_version="garden-propose.next",
        )
        is None
    )


def test_run_detectors_dispatches_griped(store: Store) -> None:
    mem = _memory(store, "A live claim a reader complained about.")
    _gripe(store, f"me{mem} is wrong.", route=[])
    run = cand.run_detectors(store, passes=("memory",), detector_deadline_s=0)
    assert any(c.detector == "griped" for c in run.candidates)
    assert "griped" not in run.skipped


def test_griped_pass_requires_the_named_hits_the_route_ranked() -> None:
    """A search passes only when the named memories it ranked at pin time
    still arrive — any other named hit is no substitute — and an
    unreplayable route row fails the pass rather than being skipped."""
    pinned = [{"seq": 1, "verb": "search", "ranks": {"me1": 1, "me2": 3, "me3": None}}]
    replay = [{"seq": 1, "verb": "search", "hits": [{"ref_id": 1}, {"ref_id": 2}]}]
    assert consumer._griped_pass(replay, {1, 2, 3}, pinned)
    replay[0]["hits"] = [{"ref_id": 1}, {"ref_id": 3}]  # me2 lost to me3
    assert not consumer._griped_pass(replay, {1, 2, 3}, pinned)
    replay[0]["hits"] = [{"ref_id": 1}, {"ref_id": 2}, {"ref_id": 3}]
    assert consumer._griped_pass(replay, {1, 2, 3}, pinned)
    assert not consumer._griped_pass(
        [{"seq": 2, "verb": "search", "not_replayed": "no recorded query"}],
        {1, 2, 3},
        pinned,
    )
    assert not consumer._griped_pass(
        [{"seq": 2, "verb": "get", "resolves": False}], {1, 2, 3}, pinned
    )
    # A route row with no pinned counterpart still has to reach a named memory.
    assert consumer._griped_pass(
        [{"seq": 9, "verb": "search", "hits": [{"ref_id": 2}]}], {1, 2, 3}, pinned
    )
    assert not consumer._griped_pass(
        [{"seq": 9, "verb": "search", "hits": [{"ref_id": 99}]}],
        {1, 2, 3},
        pinned,
    )


def _hub(store: Store, *members: int) -> int:
    hub = _memory(store, "The subject hub heading its members.")
    store.add_tag(hub, Tag.open("section:index"))
    for member in members:
        store.add_link(src_ref_id=member, dst_ref_id=hub, relation="part-of")
    return hub


def test_recall_guard_scopes_to_the_touched_hubs(store: Store) -> None:
    named = _memory(store, "Named claim about zilch routing tokens.")
    want = _memory(store, "The recall answer about zilch routing tokens.")
    store.add_tag(want, Tag.parse_strict("SPACE:repo-dev", kind="memory"))
    _hub(store, named, want)
    entries = [{"query": "zilch routing", "want": f"me{want}"}]
    assert consumer._griped_recall_guard(store, {named}, entries=entries) == []
    # A want outside the touched hubs is not the hub's recall set.
    other = _memory(store, "An unrelated unhubbed claim.")
    assert consumer._griped_recall_guard(store, {other}, entries=entries) == []
    store.retire_ref(want)
    assert consumer._griped_recall_guard(store, {named}, entries=entries) == [
        f"me{want} (retired)"
    ]


def test_no_change_verdict_closes_the_gripe_after_the_rejection(
    store: Store,
) -> None:
    """The evaluator's no-change is the complaint's resolution: the runner
    records the rejection, then the replay-after door closes the gripe."""
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(
        store,
        f"me{keeper} and me{loser} disagree on pours.",
        route=_route({"verb": "search", "arg_sample": {"q": "interim"}}),
    )
    runner.run_garden_tick(
        store,
        passes=("memory",),
        kinds=("memory",),
        propose=lambda _s, _c: Verdict(
            accept=False, rationale="the replay already passes"
        ),
    )
    tags = {v for _ns, v in store.ref_tags_bulk([gid]).get(gid, [])}
    assert "done" in tags
    comments = [
        b.text
        for b in store.chunks.list_chunks_for_ref(gid)
        if b.chunk_kind == "gripe_comment"
    ]
    assert comments and comments[-1].startswith("resolved:")


def test_no_change_close_waits_on_queued_sibling_candidates(store: Store) -> None:
    """A no-change on one named memory must not close while a sibling
    candidate may still carry the fix — an ask marks the gripe open."""
    keeper = _memory(store, "Interim leaf boards permit the pour since the ruling.")
    loser = _memory(store, "No board may ever carry a pour on any layer, period.")
    gid = _gripe(
        store,
        f"me{keeper} and me{loser} disagree on pours.",
        route=_route({"verb": "search", "arg_sample": {"q": "interim"}}),
    )

    def propose(_s: Any, c: Any) -> Verdict:
        if c.target_id == keeper:
            return Verdict(accept=False, ask="undecidable, needs a human")
        return Verdict(accept=False, rationale="the replay already passes")

    runner.run_garden_tick(
        store, passes=("memory",), kinds=("memory",), propose=propose
    )
    tags = {v for _ns, v in store.ref_tags_bulk([gid]).get(gid, [])}
    assert "done" not in tags
    assert store.list_refs(kind="todo", tags=["memory-review"], limit=10)


def test_griped_qualify_reads_as_ask_never_a_stranded_proposal(
    store: Store,
) -> None:
    from precis.graph.garden.proposer import MEMORY_ACTIONS, _one_action_verdict

    assert "qualify" not in MEMORY_ACTIONS["griped"]
    mem = _memory(store, "A scoped claim needing a caveat.")
    _gripe(store, f"me{mem} needs its scope named.", route=[])
    candidate = cand.griped_candidates(store)[0]
    verdict = _one_action_verdict(
        {"action": "qualify", "reason": "true only in scope", "caveat": "x"},
        candidate,
        model=None,
    )
    assert not verdict.accept and verdict.ask is not None


def test_griped_candidates_come_newest_gripe_first(store: Store) -> None:
    """gr657257: the gardener works the freshest complaint first."""
    mem = _memory(store, "A claim two readers complained about.")
    older = _gripe(store, f"me{mem}: stale since the rename.", route=[])
    newer = _gripe(store, f"me{mem}: contradicts its hub.", route=[])
    found = cand.griped_candidates(store)
    assert [c.payload["gripe"] for c in found] == [newer, older]
    nxt: dict[str, int] = {}
    page = cand.griped_candidates(store, scan_cap=1, next_cursors=nxt)
    assert [c.payload["gripe"] for c in page] == [newer]
    rest = cand.griped_candidates(store, scan_cap=1, cursors=nxt)
    assert [c.payload["gripe"] for c in rest] == [older]


def test_new_complaint_heads_the_next_window_mid_sweep(store: Store) -> None:
    """gr657257 review: a complaint filed mid-sweep sits above the saved
    low-water cursor — the high-water mark must let it head the next pass
    instead of waiting for the wraparound."""
    mem = _memory(store, "A claim readers keep complaining about.")
    oldest = _gripe(store, f"me{mem}: first complaint.", route=[])
    middle = _gripe(store, f"me{mem}: second complaint.", route=[])
    nxt: dict[str, int] = {}
    page = cand.griped_candidates(store, scan_cap=1, next_cursors=nxt)
    assert [c.payload["gripe"] for c in page] == [middle]
    newest = _gripe(store, f"me{mem}: just filed.", route=[])
    nxt2: dict[str, int] = {}
    rest = cand.griped_candidates(store, scan_cap=1, cursors=nxt, next_cursors=nxt2)
    assert [c.payload["gripe"] for c in rest] == [newest]
    last = cand.griped_candidates(store, scan_cap=1, cursors=nxt2)
    assert [c.payload["gripe"] for c in last] == [oldest]


def test_full_new_band_keeps_the_unprocessed_gripes_unseen(store: Store) -> None:
    """gr657257 review: when the new band fills the scan without reaching
    the limit, the gripe between the old mark and the band's oldest id
    must stay ahead of the low-water cursor, not be skipped."""
    mem = _memory(store, "A claim readers keep complaining about.")
    oldest = _gripe(store, f"me{mem}: first complaint.", route=[])
    nxt: dict[str, int] = {}
    page = cand.griped_candidates(store, scan_cap=1, next_cursors=nxt)
    assert [c.payload["gripe"] for c in page] == [oldest]
    skipped = _gripe(store, f"me{mem}: second complaint.", route=[])
    newest = _gripe(store, f"me{mem}: just filed.", route=[])
    nxt2: dict[str, int] = {}
    rest = cand.griped_candidates(store, scan_cap=1, cursors=nxt, next_cursors=nxt2)
    assert [c.payload["gripe"] for c in rest] == [newest]
    last = cand.griped_candidates(store, scan_cap=1, cursors=nxt2)
    assert [c.payload["gripe"] for c in last] == [skipped]


def test_runner_drains_griped_before_the_round_robin(
    store: Store, monkeypatch: Any
) -> None:
    """gr657257: memory gripes go before every self-found smell."""

    def _cand(det: str, target: int) -> cand.GardenCandidate:
        return cand.GardenCandidate(
            detector=det,
            action="rewrite",
            target_kind="ref",
            target_id=target,
            title=f"{det} on {target}",
            payload={"gripe": target} if det == "griped" else {},
        )

    found = [
        _cand("anchor_rot", 900001),
        _cand("anchor_rot", 900002),
        _cand("title_shape", 900003),
        _cand("griped", 900004),
        _cand("griped", 900005),
    ]
    monkeypatch.setattr(
        runner, "run_detectors", lambda *_a, **_k: cand.DetectorRun(candidates=found)
    )
    asked: list[str] = []

    def propose(_s: Any, c: cand.GardenCandidate) -> None:
        asked.append(c.detector)
        return None

    runner.run_garden_tick(store, propose=propose)
    assert asked[:2] == ["griped", "griped"]
    assert sorted(asked[2:]) == ["anchor_rot", "anchor_rot", "title_shape"]


def test_griped_survives_the_freshly_considered_skip(
    store: Store, monkeypatch: Any
) -> None:
    """A reader's open complaint is not silenced by an earlier review."""
    mem = _memory(store, "A reviewed claim a reader still complains about.")
    _gripe(store, f"me{mem}: wrong after the review.", route=[])
    monkeypatch.setattr(cand, "freshly_considered", lambda *_a, **_k: {("ref", mem)})
    run = cand.run_detectors(store, passes=("memory",), detector_deadline_s=0)
    assert any(c.detector == "griped" and c.target_id == mem for c in run.candidates)
