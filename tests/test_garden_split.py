"""grab_bag split detection, apply_split and undo_split on dev-DB fixtures."""

from __future__ import annotations

import argparse

import pytest

from precis.cli.garden import _undo
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.graph.garden import apply as doors
from precis.graph.garden import candidates as cand
from precis.graph.garden.proposals import emit_proposal, key_block, pending_proposals
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from tests.test_garden_memory_apply import _body, _links

# Three topics in four bullets: rg, scheduler/embedder, semanticscholar.
TOPIC_A = (
    "- rg -rn mangles ripgrep output because -r means replace text; "
    "always use rg -n for line numbers.\n"
)
TOPIC_B = (
    "- queue latency spikes came from the scheduler's per-cadence bound, "
    "not from pgbouncer saturation.\n"
)
TOPIC_B2 = (
    "- the embedder 429 response falls back to lexical search for that "
    "one scheduler tick only.\n"
)
TOPIC_C = (
    "- semanticscholar DOI lookup takes the bare doi string, never "
    "a doi.org prefixed URL.\n"
)
BODY = "# Cleanup notes\n" + TOPIC_A + TOPIC_B + TOPIC_B2 + TOPIC_C


def _hub(store: Store) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title="Hub index")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="Index.\n", meta={"chunk_kind": "memory_body"})],
    )
    store.add_tag(ref.id, Tag.open("section:index"), set_by="system")
    store.add_tag(ref.id, Tag.open("SPACE:repo-dev"), set_by="system")
    return int(ref.id)


def _grab_bag(
    store: Store, body: str = BODY, *, title: str = "Cleanup notes", hub: int
) -> int:
    from precis.store.revision_context import revision_context

    with revision_context(
        "split fixture",
        actor="fixture",
        trust_origin="generated",
        trust_actor_type="agent",
    ):
        ref = store.insert_ref(kind="memory", slug=None, title=title)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})],
        )
    store.add_tag(ref.id, Tag.open("section:gotchas"), set_by="system")
    store.add_tag(ref.id, Tag.open("SPACE:repo-dev"), set_by="system")
    store.add_link(src_ref_id=ref.id, dst_ref_id=hub, relation="part-of")
    return int(ref.id)


class _StubEmbedder(MockEmbedder):
    def __init__(self, model: str, dim: int) -> None:
        super().__init__(model=model, dim=dim)
        self.batches: list[list[str]] = []
        self.vectors: dict[str, list[float]] = {}

    @property
    def is_production(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [
            self.vectors.get(text, [1.0] + [0.0] * (self.dim - 1)) for text in texts
        ]


@pytest.fixture
def embedder(store: Store, monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name, dim FROM embedders WHERE is_default = TRUE ORDER BY name LIMIT 1"
        ).fetchone()
    assert row is not None
    stub = _StubEmbedder(str(row[0]), int(row[1]))
    monkeypatch.setattr(
        "precis.embedder.make_embedder_from_config", lambda *a, **kw: stub
    )
    return stub


def _topic_vectors(embedder: _StubEmbedder) -> None:
    """Three orthogonal topics; the scheduler sentences share one."""
    embedder.vectors[TOPIC_A.rstrip()] = [1.0, 0.0, 0.0] + [0.0] * (embedder.dim - 3)
    embedder.vectors[TOPIC_B.rstrip()] = [0.0, 1.0, 0.0] + [0.0] * (embedder.dim - 3)
    embedder.vectors[TOPIC_B2.rstrip()] = [0.0, 0.99, 0.14] + [0.0] * (embedder.dim - 3)
    embedder.vectors[TOPIC_C.rstrip()] = [0.0, 0.0, 1.0] + [0.0] * (embedder.dim - 3)


def _candidate(store: Store, target: int) -> cand.GardenCandidate:
    return next(c for c in cand.grab_bag_candidates(store) if c.target_id == target)


def _children(candidate: cand.GardenCandidate) -> list[dict[str, object]]:
    s = [sent["span"] for sent in candidate.evidence["sentences"]]
    return [
        {
            "title": "rg -rn rewrites matches to n; always use rg -n",
            "section": "gotchas",
            "spans": [s[0]],
        },
        {
            "title": "scheduler cadence bound caused the latency and 429",
            "section": "reference",
            "spans": [s[1], s[2]],
        },
        {
            "title": "semanticscholar takes the bare DOI, not a doi.org URL",
            "section": "gotchas",
            "spans": [s[3]],
        },
    ]


def _propose(
    store: Store, candidate: cand.GardenCandidate, *, fate: str | None = None
) -> int:
    candidate.payload["children"] = _children(candidate)
    if fate is not None:
        candidate.payload["parent_fate"] = fate
    if fate == "keep":
        candidate.payload["keep_body"] = "Index of three claims; see children."
    review_id = emit_proposal(
        store, candidate, rationale="Each child states one claim."
    )
    assert review_id is not None
    return review_id


def test_grab_bag_is_memory_pass_not_default(store: Store) -> None:
    assert "grab_bag" in cand.detectors_for(("memory",))
    assert "grab_bag" not in cand.detectors_for(("all",))
    assert "grab_bag" in cand.detectors_for(("all", "memory"))


def test_title_leg_flags_multi_claim_titles(store: Store) -> None:
    hub = _hub(store)
    flagged = [
        _grab_bag(store, "A body.\n", title=t, hub=hub)
        for t in (
            "Two things, kept separate",
            "draft conversion method and gotchas",
            "ingest/worker notes",
            "waves 1-3 cleanup",
        )
    ]
    clean = _grab_bag(store, "A body.\n", title="rg flag gotcha", hub=hub)
    found = {c.target_id for c in cand.grab_bag_candidates(store)}
    assert set(flagged) <= found
    assert clean not in found


def test_body_leg_clusters_three_topics(store: Store, embedder: _StubEmbedder) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    found = _candidate(store, target)
    assert found.detector == "grab_bag"
    assert found.action == "split"
    assert found.evidence["items"] == 5
    assert found.evidence["clusters"] == [[0], [1, 2], [3]]
    assert found.evidence["parents"] == [hub]
    assert found.payload["base_sha"] == found.evidence["content_sha"]
    assert found.payload["hub"] == hub
    assert len(embedder.batches) == 1
    spans = found.evidence["sentences"]
    assert _body(store, target) == BODY
    for sent in spans:
        assert BODY[sent["span"][0] : sent["span"][1]] == sent["text"]


def test_single_topic_body_is_not_a_candidate(
    store: Store, embedder: _StubEmbedder
) -> None:
    hub = _hub(store)
    target = _grab_bag(
        store,
        TOPIC_B + TOPIC_B + TOPIC_B2 + TOPIC_B2,
        title="Scheduler gotchas",
        hub=hub,
    )
    assert [c.target_id for c in cand.grab_bag_candidates(store)] == []
    assert target is not None


def test_apply_split_retire_mints_children_and_tombstone(
    store: Store, embedder: _StubEmbedder
) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    review_id = _propose(store, candidate)
    before = _links(store, target)

    rec = doors.apply_split(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )

    assert rec.split_id == f"garden-split:{target}:{review_id}"
    assert rec.parent_fate == "retire"
    assert len(rec.children) == 3
    for child in rec.children:
        ref = store.get_ref(kind="memory", id=child["id"])
        assert ref is not None and ref.retired_at is None
        assert ref.title == next(
            c["title"]
            for c in candidate.payload["children"]
            if c["spans"] == child["spans"]
        )
        links = _links(store, child["id"])
        rels = {(link[1], link[3], link[5]) for link in links}
        assert (child["id"], hub, "part-of") in rels
        assert (child["id"], target, "derived-from") in rels
        acts = store.trust_acts_for("ref", child["id"])
        assert any(
            a["action"] == "consider"
            and a["reason"].startswith("kg-review:fixture-reviewer:")
            and a["reason"].endswith(":ok")
            for a in acts
        )
        tags = {str(t) for t in store.tags_for(child["id"])}
        assert "space:repo-dev" in tags
    assert _body(store, rec.children[0]["id"]) == TOPIC_A.rstrip()
    tomb = store.get_ref(kind="memory", id=target, include_deleted=True)
    assert tomb is not None and tomb.retired_at is not None
    assert tomb.meta["split_into"] == [c["id"] for c in rec.children]
    assert tomb.meta["superseded_by"] == f"me{hub}"
    assert tomb.meta["garden_retirement"]["retirement_id"] == rec.split_id
    assert tomb.meta["garden_retirement"]["children"] == [c["id"] for c in rec.children]
    assert not any(p.review_id == review_id for p in pending_proposals(store))
    # The parent's edges migrated onto the hub through the delete door.
    assert before
    assert all(link[1] != target for link in _links(store, target))


def test_undo_split_restores_parent_and_retires_children(
    store: Store, embedder: _StubEmbedder
) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    review_id = _propose(store, candidate)
    before_links = _links(store, target)

    rec = doors.apply_split(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )
    counts = doors.undo_split(store, rec.split_id, actor="operator")

    assert counts["children_deleted"] == 3
    for child in rec.children:
        ref = store.get_ref(kind="memory", id=child["id"], include_deleted=True)
        assert ref is not None and ref.retired_at is not None
        assert _links(store, child["id"]) == []
    restored = store.get_ref(kind="memory", id=target)
    assert restored is not None and restored.retired_at is None
    assert "garden_retirement" not in restored.meta
    assert "split_into" not in restored.meta
    assert "superseded_by" not in restored.meta
    assert _body(store, target).encode("utf-8") == BODY.encode("utf-8")
    assert _links(store, target) == before_links
    assert key_block(store, "ref", target, candidate.key) == "rejected"
    assert emit_proposal(store, candidate, rationale="re-ask") is None


def test_apply_split_keep_shrinks_parent_and_undoes(
    store: Store, embedder: _StubEmbedder
) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    review_id = _propose(store, candidate, fate="keep")

    rec = doors.apply_split(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )

    assert rec.parent_fate == "keep"
    parent = store.get_ref(kind="memory", id=target)
    assert parent is not None and parent.retired_at is None
    assert _body(store, target) == "Index of three claims; see children."
    assert parent.meta["garden_split"]["split_id"] == rec.split_id
    stamp = next(
        a
        for a in reversed(store.trust_acts_for("ref", target))
        if a["action"] == "consider"
    )
    assert stamp["reason"].startswith("kg-review:fixture-reviewer:")
    assert stamp["reason"].endswith(":fixed")

    counts = doors.undo_split(store, rec.split_id, actor="operator")

    assert counts["children_deleted"] == 3
    assert _body(store, target).encode("utf-8") == BODY.encode("utf-8")
    parent = store.get_ref(kind="memory", id=target)
    assert parent is not None and "garden_split" not in parent.meta
    assert key_block(store, "ref", target, candidate.key) == "rejected"


def test_split_scope_refuses_foreign_spans_and_dup_spans(
    store: Store, embedder: _StubEmbedder
) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    spans = [s["span"] for s in candidate.evidence["sentences"]]
    children = _children(candidate)
    children[0]["spans"] = [[spans[0][0], spans[0][1] + 1]]
    children[1]["spans"] = [spans[1], spans[1]]
    candidate.payload["children"] = children
    review_id = emit_proposal(store, candidate, rationale="bad spans")
    assert review_id is not None

    with pytest.raises(BadInput):
        doors.apply_split(
            store, review_id, actor="garden-consumer", model="fixture-reviewer"
        )
    assert _body(store, target).encode("utf-8") == BODY.encode("utf-8")
    assert store.get_ref(kind="memory", id=target) is not None


def test_reviewable_requires_a_children_plan(
    store: Store, embedder: _StubEmbedder
) -> None:
    from precis.graph.garden.consumer import _reviewable

    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    review_id = emit_proposal(store, candidate, rationale="no plan yet")
    assert review_id is not None
    (prop,) = [p for p in pending_proposals(store) if p.review_id == review_id]
    assert not _reviewable(prop)


def test_cli_dispatches_split_undo(
    store: Store, embedder: _StubEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = _hub(store)
    _topic_vectors(embedder)
    target = _grab_bag(store, hub=hub)
    candidate = _candidate(store, target)
    review_id = _propose(store, candidate)
    rec = doors.apply_split(
        store, review_id, actor="garden-consumer", model="fixture-reviewer"
    )

    _undo(argparse.Namespace(apply_id=rec.split_id, by="operator"), store)

    assert rec.split_id in capsys.readouterr().out
    assert store.get_ref(kind="memory", id=target) is not None
    assert key_block(store, "ref", target, candidate.key) == "rejected"
