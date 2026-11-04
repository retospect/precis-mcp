"""Memory garden detectors on dev-DB fixtures; no model or apply calls."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import QueryCanceled

from precis.graph.garden import candidates as cand
from precis.graph.garden.proposals import emit_proposal, pending_proposals
from precis.graph.garden.proposer import _excerpt_ids
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from tests._refs import insert_gripe


def test_memory_pass_is_available_to_jobs() -> None:
    from precis.workers.job_types.graph_garden import PARAMS_SCHEMA

    assert "memory" in PARAMS_SCHEMA["properties"]["pass"]["anyOf"][0]["enum"]
    assert "dangling_mention" in cand.detectors_for(("memory",))
    assert "thread_shape" in cand.detectors_for(("memory",))
    assert "anchor_rot" in cand.detectors_for(("memory",))


def test_all_pass_leaves_the_memory_detectors_opt_in() -> None:
    everything = cand.detectors_for(("all",))
    assert "dup_pair" in everything
    assert "dangling_mention" not in everything
    assert "thread_shape" not in everything
    assert "anchor_rot" not in everything
    assert "anchor_rot" in cand.detectors_for(("all", "memory"))
    assert "dangling_mention" in cand.detectors_for(("all", "memory"))


def _memory(store: Store, body: str, *, meta: dict[str, Any] | None = None) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title="Memory", meta=meta or {})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    return int(ref.id)


@pytest.mark.parametrize("source", ["meta_id", "meta_handle", "edge", "none"])
def test_retired_mentions_pin_spans_and_record_rewrite(
    store: Store, source: str
) -> None:
    successor = _memory(store, "Current facts")
    meta: dict[str, Any] = {}
    if source == "meta_id":
        meta["superseded_by"] = successor
    elif source == "meta_handle":
        meta["superseded_by"] = f"me{successor}"
    retired = _memory(store, "Old facts", meta=meta)
    if source == "edge":
        store.add_link(src_ref_id=successor, dst_ref_id=retired, relation="supersedes")
    store.retire_ref(retired)
    body = (
        "Unrelated preamble. " * 40 + f"Facts are at me{retired}; compare me{retired}."
    )
    target = _memory(store, body)

    found = cand.dangling_mention_candidates(store)
    assert len(found) == 1
    candidate = found[0]
    assert candidate.target_id == target
    assert candidate.action == "rewrite"
    with store.pool.connection() as conn:
        state = conn.execute(
            "SELECT precis_target_sha('ref', %s)", (target,)
        ).fetchone()
    assert state is not None
    assert candidate.payload["base_sha"] == state[0]
    expected = None if source == "none" else f"me{successor}"
    assert len(candidate.payload["ops"]) == 2
    for op in candidate.payload["ops"]:
        start, end = op["span"]
        assert body[start:end] == op["find"] == f"me{retired}"
        assert op["text"] == op["successor"] == expected
    assert all(e["retired_at"] for e in candidate.evidence["mentions"])
    assert all("Facts are at" in e["context"] for e in candidate.evidence["mentions"])
    if source != "none":
        assert successor in _excerpt_ids(candidate)
    assert store.reviews_for("ref", target) == []
    assert emit_proposal(store, candidate, rationale="fixture") is not None
    assert emit_proposal(store, candidate, rationale="duplicate") is None
    proposal = pending_proposals(store, action="rewrite")[0]
    assert proposal.payload == candidate.payload
    assert proposal.current
    assert store.fetch_refs_by_ids([target])[target].retired_at is None
    assert cand._memory_bodies(store, [target])[0][1] == body


def test_mentions_ignore_live_wrong_kind_retired_body_and_dream(store: Store) -> None:
    retired = _memory(store, "Old")
    store.retire_ref(retired)
    live = _memory(store, "Current")
    _memory(store, f"Live me{live}; wrong kind gr{retired}; partial xme{retired}.")
    old_source = _memory(store, f"me{retired}")
    store.retire_ref(old_source)
    dream = _memory(store, f"me{retired}")
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    assert cand.dangling_mention_candidates(store) == []


@pytest.mark.parametrize("as_handle", [False, True])
@pytest.mark.parametrize("live_end", [False, True])
def test_successor_chain_only_rewrites_to_live_end(
    store: Store, as_handle: bool, live_end: bool
) -> None:
    end = _memory(store, "End")
    if not live_end:
        store.retire_ref(end)
    middle = _memory(
        store, "Middle", meta={"superseded_by": f"me{end}" if as_handle else end}
    )
    store.retire_ref(middle)
    old = _memory(
        store, "Old", meta={"superseded_by": f"me{middle}" if as_handle else middle}
    )
    store.retire_ref(old)
    _memory(store, f"Source me{old}")
    (candidate,) = cand.dangling_mention_candidates(store)
    expected = f"me{end}" if live_end else None
    assert candidate.payload["ops"][0]["text"] == expected
    assert candidate.evidence["mentions"][0]["successor"] == expected


def test_successor_cycle_is_unresolved(store: Store) -> None:
    a = _memory(store, "A")
    b = _memory(store, "B", meta={"superseded_by": a})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_build_object('superseded_by', %s::bigint) "
            "WHERE ref_id = %s",
            (b, a),
        )
    store.retire_ref(a)
    store.retire_ref(b)
    assert cand._retired_mentions(store, {f"me{a}"})[f"me{a}"]["successor"] is None


def test_converted_meta_successor_uses_current_handle(store: Store) -> None:
    successor = store.insert_ref(
        kind="datasheet",
        slug="converted-garden-successor",
        title="Current procedure",
        meta={"converted_from_kind": "paper"},
    )
    old = _memory(store, "Old", meta={"superseded_by": f"pa{successor.id}"})
    store.retire_ref(old)
    _memory(store, f"Use me{old} for builds.")

    (candidate,) = cand.dangling_mention_candidates(store)

    assert candidate.payload["ops"][0]["text"] == f"da{successor.id}"
    assert candidate.evidence["mentions"][0]["successor"] == f"da{successor.id}"


def test_successor_chain_hop_bound(store: Store) -> None:
    end = _memory(store, "Live end")
    for _ in range(cand.SUCCESSOR_MAX_HOPS + 1):
        end = _memory(store, "Retired predecessor", meta={"superseded_by": end})
        store.retire_ref(end)
    assert cand._retired_mentions(store, {f"me{end}"})[f"me{end}"]["successor"] is None


@pytest.mark.parametrize("invalid", ["missing", "wrong_kind", "chunk", "overflow"])
def test_invalid_meta_successor_falls_back_to_live_edge(
    store: Store, invalid: str
) -> None:
    live = _memory(store, "Live")
    raw = {
        "missing": "me9223372036854775807",
        "wrong_kind": f"pa{live}",
        "chunk": f"pc{live}",
        "overflow": "9223372036854775808",
    }[invalid]
    old = _memory(store, "Old", meta={"superseded_by": raw})
    store.retire_ref(old)
    assert cand._retired_mentions(store, {f"me{old}"})[f"me{old}"]["successor"] is None
    store.add_link(src_ref_id=live, dst_ref_id=old, relation="supersedes")
    assert (
        cand._retired_mentions(store, {f"me{old}"})[f"me{old}"]["successor"]
        == f"me{live}"
    )


@pytest.mark.parametrize("extra", [0, 1, 5])
def test_mentions_cap_bounds_evidence_and_ops(store: Store, extra: int) -> None:
    old = _memory(store, "Old")
    store.retire_ref(old)
    live = _memory(store, "Live")
    body = (f"me{live} " * 25) + (f"me{old} " * (cand.MENTION_MATCH_CAP + extra))
    _memory(store, body)
    (candidate,) = cand.dangling_mention_candidates(store)
    assert len(candidate.evidence["mentions"]) == cand.MENTION_MATCH_CAP
    assert len(candidate.payload["ops"]) == cand.MENTION_MATCH_CAP
    assert candidate.payload["matches_capped"] is (extra > 0)
    assert candidate.payload["match_cap"] == cand.MENTION_MATCH_CAP
    for op in candidate.payload["ops"]:
        assert body[slice(*op["span"])] == op["find"] == f"me{old}"


def test_mentions_cursor_does_not_skip_when_limit_fills(store: Store) -> None:
    retired = _memory(store, "Old")
    store.retire_ref(retired)
    targets = [_memory(store, f"me{retired}") for _ in range(3)]
    cursor: dict[str, int] = {}
    seen: list[int] = []
    for _ in targets:
        found = cand.dangling_mention_candidates(
            store, limit=1, scan_cap=2, cursors=cursor, next_cursors=cursor
        )
        seen.extend(c.target_id for c in found)
    assert seen == targets
    assert (
        cand.dangling_mention_candidates(store, cursors=cursor, next_cursors=cursor)
        == []
    )
    assert cursor == {"dangling_mention:memory": 0}


def test_chunk_mention_joins_retired_owner_without_guessing_replacement(
    store: Store,
) -> None:
    paper = store.insert_ref(kind="paper", slug="old-paper", title="Old paper")
    successor = store.insert_ref(kind="paper", slug="new-paper", title="New paper")
    store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="Evidence")])
    store.add_link(src_ref_id=successor.id, dst_ref_id=paper.id, relation="supersedes")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (paper.id,)
        ).fetchone()
    assert row is not None
    store.retire_ref(paper.id)
    target = _memory(store, f"Evidence pc{row[0]}.")
    found = cand.dangling_mention_candidates(store)
    assert len(found) == 1 and found[0].target_id == target
    op = found[0].payload["ops"][0]
    assert op["successor"] == f"pa{successor.id}"
    assert op["text"] is None


@pytest.mark.parametrize("detector", ["dangling_mention", "thread_shape", "anchor_rot"])
def test_memory_detector_timeout_does_not_save_cursor(
    store: Store, monkeypatch: pytest.MonkeyPatch, detector: str
) -> None:
    def cancelled(*args: Any, **kwargs: Any) -> list[cand.GardenCandidate]:
        kwargs["next_cursors"][f"{detector}:memory"] = 99
        raise QueryCanceled("fixture timeout")

    monkeypatch.setattr(cand, f"{detector}_candidates", cancelled)
    run = cand.run_detectors(store, passes=("memory",), detector_deadline_s=0)
    assert detector in run.skipped
    assert f"{detector}:memory" not in run.cursors


def _thread(store: Store, body: str) -> int:
    rid = _memory(store, body)
    store.add_tag(rid, Tag.open("section:threads"), set_by="system")
    return rid


def _part_of(store: Store, child: int, parent: int) -> None:
    store.add_link(src_ref_id=child, dst_ref_id=parent, relation="part-of")


def test_thread_child_without_left_proposes_supersede_and_preservation(
    store: Store,
) -> None:
    parent = _thread(
        store, "Left: finish the campaign\nEarlier: measured the baseline."
    )
    child = _thread(store, "Snapshot: baseline measured 42 units.")
    _part_of(store, child, parent)
    found = cand.thread_shape_candidates(store)
    assert len(found) == 1
    candidate = found[0]
    assert (candidate.target_id, candidate.other_id, candidate.action) == (
        child,
        parent,
        "supersede",
    )
    assert candidate.evidence["reason"] == "missing_left"
    assert candidate.payload["successor"] == f"me{parent}"
    assert candidate.payload["shas"] == {
        str(key): value for key, value in cand._ref_shas(store, [parent, child]).items()
    }
    assert candidate.payload["requires_compaction"] is True
    assert candidate.payload["preserve"]["section"] == "Earlier"
    assert store.reviews_for("ref", child) == []
    assert emit_proposal(store, candidate, rationale="fixture") is not None
    proposal = pending_proposals(store, action="supersede")[0]
    assert proposal.current and proposal.payload == candidate.payload
    assert all(
        r.retired_at is None for r in store.fetch_refs_by_ids([parent, child]).values()
    )
    assert (
        cand._memory_bodies(store, [child])[0][1]
        == "Snapshot: baseline measured 42 units."
    )


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("todo", "done"),
        ("todo", "won't-do"),
        ("todo", "auto-timeout"),
        ("gripe", "done"),
        ("gripe", "wontfix"),
    ],
)
def test_terminal_left_work_proposes_retire(
    store: Store, kind: str, status: str
) -> None:
    if kind == "gripe":
        ref = insert_gripe(store, "Closed work", status=status)
        handle = f"gr{ref.id}"
    else:
        ref = store.insert_ref(kind="todo", slug=None, title="Closed work")
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
        handle = f"td{ref.id}"
    target = _thread(
        store, f"Left: finish {handle}\nEarlier: preserve the measurements."
    )
    found = cand.thread_shape_candidates(store)
    assert len(found) == 1 and found[0].target_id == target
    assert found[0].action == "retire"
    assert found[0].evidence["terminal_work"] == {handle: status}
    assert found[0].payload["requires_compaction"] is True
    assert emit_proposal(store, found[0], rationale="fixture") is not None
    assert pending_proposals(store, action="retire")[0].current
    assert store.fetch_refs_by_ids([target])[target].retired_at is None


def test_thread_nearest_current_ancestor_skips_closed_parent(store: Store) -> None:
    root = _thread(store, "Left: do the remaining work")
    closed = insert_gripe(store, "Closed", status="done")
    parent = _thread(store, f"Left: fix gr{closed.id}")
    child = _thread(store, "Dated end-of-day snapshot")
    _part_of(store, child, parent)
    _part_of(store, parent, root)
    by_id = {c.target_id: c for c in cand.thread_shape_candidates(store)}
    assert by_id[child].other_id == root
    assert by_id[parent].other_id == root


def test_thread_nearest_current_ancestor_and_cycle(store: Store) -> None:
    root = _thread(store, "Left: root work")
    parent = _thread(store, "Left: nearer work")
    child = _thread(store, "Snapshot")
    _part_of(store, child, parent)
    _part_of(store, parent, root)
    assert cand.thread_shape_candidates(store)[0].other_id == parent
    a = _thread(store, "Cycle A")
    b = _thread(store, "Cycle B")
    _part_of(store, a, b)
    _part_of(store, b, a)
    by_id = {c.target_id: c for c in cand.thread_shape_candidates(store)}
    assert by_id[a].action == by_id[b].action == "retire"


def test_thread_ancestor_crosses_untagged_parent_and_skips_left_none(
    store: Store,
) -> None:
    root = _thread(store, "Left: finish campaign")
    parent = _thread(store, "Left: none")
    intermediary = _memory(store, "An untagged grouping node")
    child = _thread(store, "A snapshot without resume state")
    _part_of(store, parent, root)
    _part_of(store, intermediary, parent)
    _part_of(store, child, intermediary)
    found = cand.thread_shape_candidates(store)
    assert len(found) == 1
    assert found[0].target_id == child and found[0].other_id == root


def test_thread_scan_keeps_live_work_history_and_excluded_nodes(store: Store) -> None:
    done = insert_gripe(store, "Closed", status="done")
    opened = insert_gripe(store, "Open", status="open")
    _thread(store, f"Left: work on gr{opened.id}\nEarlier: closed gr{done.id}")
    _thread(store, f"Left: check td{done.id}")  # Wrong kind must not resolve.
    _thread(store, "Left: none")  # Daily sweep owns this case.
    _thread(store, "Left: check abcdef1234567 on origin/prod")  # No git claim.
    _memory(store, "A non-thread without Left:")
    hub = _thread(store, "Section hub")
    store.add_tag(hub, Tag.open("section:index"), set_by="system")
    dream = _thread(store, "Speculation")
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    retired = _thread(store, "Old snapshot")
    store.retire_ref(retired)
    assert cand.thread_shape_candidates(store) == []


def test_thread_scan_limit_cursor_and_dispatch(store: Store) -> None:
    targets = [_thread(store, f"Snapshot {i}") for i in range(3)]
    cursor: dict[str, int] = {}
    seen: list[int] = []
    for _ in targets:
        run = cand.run_detectors(
            store,
            passes=("memory",),
            scan_cap=2,
            limit=1,
            cursors=cursor,
            considered_days=0,
            detector_deadline_s=0,
        )
        seen.extend(c.target_id for c in run.candidates if c.detector == "thread_shape")
        cursor.update(run.cursors)
    assert seen == targets
    assert (
        cand.thread_shape_candidates(store, cursors=cursor, next_cursors=cursor) == []
    )
    assert cursor["thread_shape:memory"] == 0
