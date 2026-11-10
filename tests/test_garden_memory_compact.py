"""The compact detector, apply_compact and undo_compact on the dev fixtures.

gr561025: no node retires and no sentence drops before a per-sentence
compaction plan — edits into the pinned neighbourhood, atomic puts, and
the shell's fate — runs as one undoable unit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.graph.garden import apply as doors
from precis.graph.garden import candidates as cand
from precis.graph.garden.apply import MergeRefused
from precis.graph.garden.consumer import ConsumerVerdict, consume_proposals
from precis.graph.garden.proposals import (
    emit_proposal,
    pending_proposals,
    resolve_proposal,
)
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import ChunkInsert, Tag

_EMB = MockEmbedder(dim=1024)
_EMBEDDER = "bge-m3"  # the migration-seeded default embedder


def _memory(
    store: Store,
    body: str,
    *,
    origin: str = "generated",
    meta: dict[str, Any] | None = None,
) -> int:
    with revision_context(
        "compact fixture",
        actor="fixture",
        trust_origin=origin,
        trust_actor_type="human" if origin == "human" else "agent",
    ):
        ref = store.insert_ref(
            kind="memory", slug=None, title="Memory", meta=meta or {}
        )
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
        )
    return int(ref.id)


def _body(store: Store, target: int) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'memory_body' AND retired_at IS NULL ORDER BY ord",
            (target,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _refs_row(store: Store, target: int) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at IS NOT NULL, meta->>'superseded_by', meta "
            "FROM refs WHERE ref_id = %s",
            (target,),
        ).fetchone()
    assert row is not None
    return row


def _compact_candidate(store: Store, target: int) -> cand.GardenCandidate:
    found = [c for c in cand.compact_candidates(store) if c.target_id == target]
    assert len(found) == 1
    return found[0]


def _dispositions(
    candidate: cand.GardenCandidate, *entries: dict[str, Any]
) -> list[dict[str, Any]]:
    """One plan.sentences entry per evidence sentence, echoing its span."""
    sentences = candidate.evidence["sentences"]
    assert len(entries) == len(sentences)
    return [
        dict(entry, span=s["span"]) for entry, s in zip(entries, sentences, strict=True)
    ]


def _card(store: Store, ref_id: int, text: str) -> None:
    """A ``card_combined`` chunk plus its default-embedder vector."""
    cid = store.chunks.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, "
            "attempts) VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _EMBEDDER, _EMB.embed_one(text)),
        )


def _proposal(
    store: Store,
    body: str,
    *,
    meta: dict[str, Any] | None = None,
    plan: Any = None,
) -> tuple[int, int, cand.GardenCandidate]:
    target = _memory(store, body, meta=meta)
    candidate = _compact_candidate(store, target)
    assert candidate.action == "compact"
    if callable(plan):
        plan = plan(candidate)
    if plan is not None:
        candidate.payload["plan"] = plan
    review_id = emit_proposal(store, candidate, rationale="Checked the node.")
    assert review_id is not None
    return target, review_id, candidate


SHELL_BODY = (
    "The cache must be flushed after every schema migration.\n"
    "The worker never calls fill_embeddings from ingest.\n"
    "This restates the hub's standing gotcha verbatim.\n"
    "2026-01-03 the migration landed.\n"
    "2026-01-05 the backfill shipped at 932364d.\n"
    "Done: the watcher ran on 2026-01-07.\n"
)


def test_compact_detector_expired_trigger(store: Store) -> None:
    target = _memory(
        store, "A standing fact.\n", meta={"expires": "2020-01-01T00:00:00+00:00"}
    )
    candidate = _compact_candidate(store, target)
    assert candidate.evidence["reasons"] == ["expired"]
    assert candidate.evidence["expiry_source"] == "meta.expires"
    assert candidate.payload["base_sha"]
    assert candidate.payload["plan"] is None
    assert candidate.evidence["sentences"]


def test_compact_detector_cold_trigger(store: Store) -> None:
    target = _memory(store, "A standing fact nobody reads.\n")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '100 days' "
            "WHERE ref_id = %s",
            (target,),
        )
    candidate = _compact_candidate(store, target)
    assert "cold" in candidate.evidence["reasons"]
    assert candidate.evidence["reads"]["last_read_at"] is None


def test_compact_detector_history_trigger(store: Store) -> None:
    body = (
        "2026-01-03 the migration landed.\n"
        "2026-01-05 the backfill shipped at 932364d.\n"
        "Done: the watcher ran on 2026-01-07.\n"
        "One live claim without a date.\n"
    )
    target = _memory(store, body)
    candidate = _compact_candidate(store, target)
    assert "history" in candidate.evidence["reasons"]
    assert candidate.evidence["history_density"] == 0.75


def test_compact_detector_retire_proposal_trigger(store: Store) -> None:
    target = _memory(store, "Dated snapshot: the campaign started.\n")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    retire = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "retire"
    )
    review_id = emit_proposal(store, retire, rationale="stale thread snapshot")
    assert review_id is not None
    candidate = _compact_candidate(store, target)
    assert candidate.evidence["reasons"] == ["retire_proposal"]
    assert candidate.evidence["retire_proposals"] == [review_id]


def test_compact_apply_put_retire_then_undo_round_trip(store: Store) -> None:
    hub = _memory(store, "Left: keep watching the campaign\n")
    sibling = _memory(store, "Cache discipline: flush on schema change.\n")
    target = _memory(store, SHELL_BODY, meta={"expires": "2020-01-01T00:00:00+00:00"})
    store.add_link(src_ref_id=target, dst_ref_id=hub, relation="part-of")
    store.add_link(src_ref_id=sibling, dst_ref_id=hub, relation="part-of")
    candidate = _compact_candidate(store, target)
    assert hub in candidate.evidence["related"]["parents"]
    assert sibling in candidate.evidence["related"]["siblings"]
    prior_sibling = _body(store, sibling)
    candidate.payload["plan"] = {
        "edits": [
            {
                "ref": sibling,
                "ops": [
                    {
                        "find": "Cache discipline: flush on schema change.",
                        "text": "Cache discipline: flush on schema change. "
                        "The cache must be flushed after every schema migration.",
                    }
                ],
                "reason": "the standing gotcha belongs with its sibling",
            }
        ],
        "puts": [
            {
                "title": "Ingest never calls fill_embeddings",
                "body": "The worker never calls fill_embeddings from ingest "
                f"(carried out of me{target}).",
                "section": "section:gotchas",
                "qualifies": [hub],
                "links": [{"dst": sibling, "relation": "related-to"}],
            }
        ],
        "sentences": _dispositions(
            candidate,
            {"disposition": "move", "ref": sibling},
            {"disposition": "mint", "put": 0},
            {"disposition": "drop", "reason": "restatement"},
            {"disposition": "drop", "reason": "history"},
            {"disposition": "drop", "reason": "history"},
            {"disposition": "drop", "reason": "history"},
        ),
        "shell": {"fate": "retire", "successor": {"put": 0}},
    }
    review_id = emit_proposal(store, candidate, rationale="compact the shell")
    assert review_id is not None

    rec = doors.apply_compact(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    child = rec["puts"][0]["ref_id"]
    assert "fill_embeddings" in _body(store, child)
    retired, superseded_by, meta = _refs_row(store, target)
    assert retired is True
    assert superseded_by == f"me{child}"
    assert (
        "flush on schema change. The cache must be flushed after every "
        "schema migration." in _body(store, sibling)
    )
    with store.pool.connection() as conn:
        edges = conn.execute(
            "SELECT relation, dst_ref_id FROM links WHERE src_ref_id = %s "
            "ORDER BY relation",
            (child,),
        ).fetchall()
    assert ("part-of", hub) in [(str(r), int(d)) for r, d in edges]
    assert ("qualifies", hub) in [(str(r), int(d)) for r, d in edges]

    counts = doors.undo_compact(store, rec["compact_id"], actor="fixture")
    assert counts["puts_retired"] == 1
    assert counts["edits_restored"] == 1
    retired, superseded_by, meta = _refs_row(store, target)
    assert retired is False
    assert "garden_compact" not in meta
    assert _body(store, target) == SHELL_BODY
    assert _body(store, sibling) == prior_sibling
    assert _refs_row(store, child)[0] is True
    reviews = [
        json.loads(note)["garden"]
        for _rid, _actor, verdict, note in _review_rows(store, target)
        if verdict == "rejected"
    ]
    assert any(g.get("reason") == "undone" for g in reviews)


def _review_rows(store: Store, target: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT review_id, actor, verdict, note FROM reviews "
            "WHERE target_kind = 'ref' AND target_id = %s ORDER BY review_id",
            (target,),
        ).fetchall()


def test_compact_keep_summary_rearms_expiry_and_undoes(store: Store) -> None:
    body = "Live state: the campaign is still running.\nMore context lines.\n"
    target, review_id, _candidate = _proposal(
        store,
        body,
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
                "summary": "Live state: the campaign is still running.\nOwner: ops.",
                "expires_days": 14,
            },
        },
    )
    rec = doors.apply_compact(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    assert (
        _body(store, target)
        == "Live state: the campaign is still running.\nOwner: ops."
    )
    _retired, _sup, meta = _refs_row(store, target)
    assert meta["expires"] > "2020-01-02"

    doors.undo_compact(store, rec["compact_id"], actor="fixture")
    assert _body(store, target) == body
    _retired, _sup, meta = _refs_row(store, target)
    assert meta["expires"] == "2020-01-01T00:00:00+00:00"
    assert "garden_compact" not in meta


def test_compact_refuses_edit_outside_related(store: Store) -> None:
    outsider = _memory(store, "Unrelated live memory.\n")
    _target, review_id, _candidate = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=lambda c: {
            "edits": [
                {
                    "ref": outsider,
                    "ops": [{"find": "Unrelated live memory.", "text": "Rewritten."}],
                }
            ],
            "puts": [],
            "sentences": _dispositions(
                c,
                {"disposition": "keep"},
                {"disposition": "keep"},
                {"disposition": "keep"},
                {"disposition": "drop", "reason": "history"},
                {"disposition": "drop", "reason": "history"},
                {"disposition": "drop", "reason": "history"},
            ),
            "shell": {"fate": "keep", "summary": "Summary.", "expires_days": 7},
        },
    )
    with pytest.raises(BadInput, match="related live memory"):
        doors.apply_compact(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _body(store, outsider) == "Unrelated live memory.\n"


def test_compact_requires_a_plan(store: Store) -> None:
    _target, review_id, _candidate = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
    )
    with pytest.raises(BadInput, match="well-formed payload.plan"):
        doors.apply_compact(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )


def test_apply_retire_refuses_undecided_compact(store: Store) -> None:
    """A pending retire proposal is itself a compact trigger (compact first)."""
    target = _memory(store, "Dated snapshot: the campaign started.\n")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    retire = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "retire"
    )
    review_id = emit_proposal(store, retire, rationale="stale thread snapshot")
    assert review_id is not None
    with pytest.raises(MergeRefused, match="compaction"):
        doors.apply_retire(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _refs_row(store, target)[0] is False


def test_apply_retire_refuses_pending_compact(store: Store) -> None:
    hub = _memory(store, "Left: parent hub\n")
    target = _memory(store, SHELL_BODY, meta={"expires": "2020-01-01T00:00:00+00:00"})
    store.add_link(src_ref_id=target, dst_ref_id=hub, relation="part-of")
    compact_candidate = _compact_candidate(store, target)
    emit_proposal(store, compact_candidate, rationale="compact first")
    # A pending compact proposal blocks a retirement of the same node even
    # when the retire proposal's own detector check still holds.
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    retire = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "retire"
    )
    review_id = emit_proposal(store, retire, rationale="stale thread snapshot")
    assert review_id is not None
    with pytest.raises(MergeRefused, match="pending compaction"):
        doors.apply_retire(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _refs_row(store, target)[0] is False


def _keep_plan(c: cand.GardenCandidate) -> dict[str, Any]:
    return {
        "edits": [],
        "puts": [],
        "sentences": _dispositions(
            c, *({"disposition": "keep"} for _ in c.evidence["sentences"])
        ),
        "shell": {"fate": "keep", "summary": "Summary.", "expires_days": 7},
    }


def test_compact_plan_needs_full_sentence_accounting(store: Store) -> None:
    """Empty edits/puts plus a summary cannot silently discard claims."""
    _t1, review_id, _c = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=lambda c: {
            "edits": [],
            "puts": [],
            "shell": {"fate": "keep", "summary": "Summary.", "expires_days": 7},
        },
    )
    with pytest.raises(BadInput, match="well-formed payload.plan"):
        doors.apply_compact(store, review_id, actor="fixture")

    _t2, review_id, _c = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=lambda c: {
            "edits": [],
            "puts": [],
            "sentences": _dispositions(
                c,
                *({"disposition": "drop", "reason": "history"} for _ in range(5)),
                {"disposition": "drop"},
            ),
            "shell": {"fate": "keep", "summary": "Summary.", "expires_days": 7},
        },
    )
    with pytest.raises(BadInput, match="drops need reason"):
        doors.apply_compact(store, review_id, actor="fixture")

    _t3, review_id, _c = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=lambda c: {
            "edits": [],
            "puts": [],
            "sentences": [{"disposition": "keep"}],
            "shell": {"fate": "keep", "summary": "Summary.", "expires_days": 7},
        },
    )
    with pytest.raises(BadInput, match="one plan.sentences disposition"):
        doors.apply_compact(store, review_id, actor="fixture")


def test_compact_rejected_plan_is_not_preservation(store: Store) -> None:
    """A rejected compaction decides a question; it preserves nothing."""
    target = _memory(store, SHELL_BODY, meta={"expires": "2020-01-01T00:00:00+00:00"})
    review_id = emit_proposal(
        store, _compact_candidate(store, target), rationale="try it"
    )
    assert review_id is not None
    assert (
        resolve_proposal(store, review_id, actor="reviewer", verdict="rejected")
        is not None
    )
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    retire = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "retire"
    )
    retire_id = emit_proposal(store, retire, rationale="stale thread snapshot")
    assert retire_id is not None
    with pytest.raises(MergeRefused, match="compaction"):
        doors.apply_retire(
            store, retire_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _refs_row(store, target)[0] is False


def test_apply_supersede_also_requires_completed_compaction(store: Store) -> None:
    """A supersede is a retirement: the compact-first gate covers it too."""
    target = _memory(store, "Dated snapshot: the campaign started.\n")
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    successor = _memory(store, "Left: finish campaign\nEarlier: campaign started.")
    store.add_tag(successor, Tag.open("section:threads"), set_by="system")
    store.add_link(src_ref_id=target, dst_ref_id=successor, relation="part-of")
    sup = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "supersede"
    )
    review_id = emit_proposal(store, sup, rationale="ancestor holds the line")
    assert review_id is not None
    # A pending retire proposal is itself a compact trigger — the supersede
    # may not retire the node before that question is answered.
    emit_proposal(
        store,
        cand.GardenCandidate(
            detector="thread_shape",
            action="retire",
            target_kind="ref",
            target_id=target,
            title="fixture retire trigger",
            evidence={"content_sha": "x"},
            payload={"base_sha": sup.payload.get("base_sha")},
        ),
        rationale="fixture",
    )
    with pytest.raises(MergeRefused, match="compaction"):
        doors.apply_supersede(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _refs_row(store, target)[0] is False


def test_completed_compact_permits_retire(store: Store) -> None:
    """An applied compaction covering the current content is preservation."""
    target, compact_id, _candidate = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=_keep_plan,
    )
    rec = doors.apply_compact(store, compact_id, actor="garden-consumer")
    assert rec["shell"]["fate"] == "keep"
    store.add_tag(target, Tag.open("section:threads"), set_by="system")
    retire = next(
        c
        for c in cand.thread_shape_candidates(store)
        if c.target_id == target and c.action == "retire"
    )
    review_id = emit_proposal(store, retire, rationale="stale thread snapshot")
    assert review_id is not None
    out = doors.apply_retire(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    assert out["target"] == target
    assert _refs_row(store, target)[0] is True


def test_compact_undo_restores_hub_edited_and_retired_into(store: Store) -> None:
    """Edit the hub, retire into it: undo must reverse in dependency order."""
    hub_body = "Hub index.\nThe standing gotcha lives here.\n"
    hub = _memory(store, hub_body)
    target = _memory(store, SHELL_BODY, meta={"expires": "2020-01-01T00:00:00+00:00"})
    store.add_link(src_ref_id=target, dst_ref_id=hub, relation="part-of")
    candidate = _compact_candidate(store, target)
    assert hub in candidate.evidence["related"]["parents"]
    candidate.payload["plan"] = {
        "edits": [
            {
                "ref": hub,
                "ops": [
                    {
                        "find": "The standing gotcha lives here.",
                        "text": "The standing gotcha lives here. "
                        "The cache must be flushed after every schema "
                        "migration.",
                    }
                ],
                "reason": "the claim belongs on the hub",
            }
        ],
        "puts": [],
        "sentences": _dispositions(
            candidate,
            {"disposition": "move", "ref": hub},
            {"disposition": "drop", "reason": "restatement"},
            {"disposition": "drop", "reason": "restatement"},
            {"disposition": "drop", "reason": "history"},
            {"disposition": "drop", "reason": "history"},
            {"disposition": "drop", "reason": "history"},
        ),
        "shell": {"fate": "retire", "successor": hub},
    }
    review_id = emit_proposal(store, candidate, rationale="fold into the hub")
    assert review_id is not None

    rec = doors.apply_compact(store, review_id, actor="garden-consumer")
    assert "schema\nmigration." in _body(store, hub) or "schema migration." in _body(
        store, hub
    )
    retired, superseded_by, _meta = _refs_row(store, target)
    assert retired is True
    assert superseded_by == f"me{hub}"

    counts = doors.undo_compact(store, rec["compact_id"], actor="fixture")
    assert counts["edits_restored"] == 1
    retired, superseded_by, _meta = _refs_row(store, target)
    assert retired is False
    assert _body(store, target) == SHELL_BODY
    assert _body(store, hub) == hub_body


def test_compact_related_includes_recall_and_restated(store: Store) -> None:
    """The brief's move destinations: +recall neighbours, restated matches."""
    keeper = _memory(store, "The keeper holds the claim.\n")
    target = _memory(store, SHELL_BODY, meta={"expires": "2020-01-01T00:00:00+00:00"})
    neighbour = _memory(store, "A neighbouring memory body.\n")
    _card(store, target, "the same card text")
    _card(store, neighbour, "the same card text")
    rid = emit_proposal(
        store,
        cand.GardenCandidate(
            detector="restated_claim",
            action="rewrite",
            target_kind="ref",
            target_id=target,
            other_id=keeper,
            title=f"me{target} restates a claim in me{keeper}",
            evidence={"hub": "me0", "matches": []},
            payload={"base_sha": "x", "keeper": f"me{keeper}", "ops": []},
        ),
        rationale="sentence match",
    )
    assert rid is not None
    candidate = _compact_candidate(store, target)
    assert neighbour in candidate.evidence["related"]["recall"]
    assert keeper in candidate.evidence["related"]["restated"]


def test_compact_consumer_approves_but_never_applies(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consumer records the approval and holds for the explicit door."""
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    target, review_id, _candidate = _proposal(
        store,
        SHELL_BODY,
        meta={"expires": "2020-01-01T00:00:00+00:00"},
        plan=_keep_plan,
    )

    def approve(_store: Store, _prop: Any) -> ConsumerVerdict:
        return ConsumerVerdict(
            verdict="approve", why="the plan preserves every claim", model="fx"
        )

    report = consume_proposals(store, review=approve)
    assert report.applied == 0
    assert report.held == 1
    assert {p.review_id for p in pending_proposals(store)} == {review_id}
    retired, _sup, meta = _refs_row(store, target)
    assert retired is False
    assert "garden_compact" not in meta
    assert _body(store, target) == SHELL_BODY
