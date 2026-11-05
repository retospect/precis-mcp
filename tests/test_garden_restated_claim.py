"""Sentence-level memory proposals on dev-DB fixtures, with a stub embedder."""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.graph.garden import candidates as cand
from precis.graph.garden.proposer import MEMORY_ACTIONS, Verdict, _prompt_for
from precis.graph.garden.runner import run_garden_tick
from precis.store import Store
from precis.store.types import ChunkInsert, Tag

CLAIM = "The worker fills missing embeddings after ingestion stores the body chunks."
REWORDING = (
    "Vector generation belongs to workers once incoming documents have been stored."
)


def _memory(
    store: Store,
    body: str,
    *,
    section: str = "reference",
    hub: int | None = None,
    title: str = "Fixture memory",
) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title)
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open(f"section:{section}"), set_by="system")
    if hub is not None:
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


def test_restated_claim_is_separate_opt_in_pass() -> None:
    from precis.workers.job_types.graph_garden import PARAMS_SCHEMA

    assert "memory-review" in PARAMS_SCHEMA["properties"]["pass"]["anyOf"][0]["enum"]
    assert cand.detectors_for(("memory-review",)) == [
        "entity_claim_stale",
        "negative_undated",
        "restated_claim",
        "review_due",
        "source_drift",
        "title_shape",
    ]
    for passes in (("all",), ("memory",), ("all", "memory")):
        assert "restated_claim" not in cand.detectors_for(passes)
    assert "restated_claim" in cand.detectors_for(("all", "memory-review"))
    assert MEMORY_ACTIONS["restated_claim"] == ("rewrite",)


@pytest.mark.parametrize(
    ("first_type", "second_type", "keeper_first"),
    [
        ("threads", "reference", False),
        ("gotchas", "reference", False),
        ("threads", "gotchas", False),
        ("reference", "threads", True),
        ("reference", "reference", True),
    ],
)
def test_reworded_claim_has_one_keeper_and_pinned_spans(
    store: Store,
    embedder: _StubEmbedder,
    first_type: str,
    second_type: str,
    keeper_first: bool,
) -> None:
    hub = _memory(store, "Hub", section="index")
    first = _memory(store, CLAIM, section=first_type, hub=hub)
    second = _memory(store, REWORDING, section=second_type, hub=hub)
    before = cand._memory_bodies(store, [first, second])
    (found,) = cand.restated_claim_candidates(store)
    keeper, target = (first, second) if keeper_first else (second, first)
    assert found.other_id == keeper
    assert found.target_id == target
    assert found.action == "rewrite"
    assert found.evidence["hub"] == f"me{hub}"
    assert found.evidence["matches"][0]["distance"] == pytest.approx(0)
    assert found.evidence["matches"][0]["lexical_overlap"] < cand.RESTATED_MIN_LEXICAL
    assert found.payload["keeper"] == f"me{keeper}"
    bodies = {rid: body for rid, body, _ in before}
    shas = {str(rid): sha for rid, _, sha in before}
    assert found.payload["shas"] == shas
    assert found.payload["base_sha"] == shas[str(target)]
    assert found.evidence["keeper_sha"] == shas[str(keeper)]
    for match in found.evidence["matches"]:
        for role, rid in (("target", target), ("keeper", keeper)):
            assert bodies[rid][slice(*match[role]["span"])] == match[role]["text"]
    (op,) = found.payload["ops"]
    assert bodies[target][slice(*op["span"])] == op["find"]
    assert op["text"] == f"See me{keeper}."
    assert cand._memory_bodies(store, [first, second]) == before
    assert store.reviews_for("ref", target) == []
    assert len(embedder.batches) == 1
    prompt = _prompt_for(found)
    assert "reference before gotcha before thread" in prompt
    assert "Opposite claims" in prompt


def test_boilerplate_short_sentences_and_title_are_not_embedded(
    store: Store,
    embedder: _StubEmbedder,
) -> None:
    hub = _memory(store, "Hub", section="index")
    title = "This heading contains enough words to look like a standing claim"
    body = (
        f"{title}\n# {title}\nSource: {CLAIM}\nLeft: {CLAIM}\n"
        f"Meta: {CLAIM}\nTitle: {CLAIM}\n**Source:** {CLAIM}\n"
        "Only seven words are in this sentence.\n\n"
        f"{CLAIM}"
    )
    _memory(store, body, hub=hub, title=title)
    _memory(store, REWORDING, hub=hub)
    (found,) = cand.restated_claim_candidates(store)
    assert embedder.batches == [[CLAIM, REWORDING]]
    assert len(found.evidence["matches"]) == 1


def test_soft_wrapped_claim_keeps_exact_offsets() -> None:
    body = f"Source: some route\n\n{CLAIM.replace('after ', 'after\n')}\n"
    ((text, span),) = tuple(cand._claim_sentences(body, "Title"))
    assert text == body[slice(*span)]
    assert "after\n" in text


def test_distinct_nested_hubs_never_compare_claims(
    store: Store,
    embedder: _StubEmbedder,
) -> None:
    root = _memory(store, "Root", section="index")
    a = _memory(store, "Hub A", section="index", hub=root)
    b = _memory(store, "Hub B", section="index", hub=root)
    _memory(store, CLAIM, hub=a)
    _memory(store, REWORDING, hub=b)
    cursor: dict[str, int] = {}
    for _ in range(4):
        assert (
            cand.restated_claim_candidates(store, cursors=cursor, next_cursors=cursor)
            == []
        )
    assert embedder.batches == []
    assert cursor["restated_claim:after_hub"] == 0


def test_summary_children_are_compared_but_dreams_and_retired_are_excluded(
    store: Store,
    embedder: _StubEmbedder,
) -> None:
    hub = _memory(store, "Hub", section="index")
    summary = _memory(store, "Summary", hub=hub)
    a = _memory(store, CLAIM, hub=hub)
    b = _memory(store, REWORDING, hub=summary)
    retired = _memory(store, CLAIM, hub=hub)
    store.retire_ref(retired)
    dream = _memory(store, CLAIM, hub=hub)
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    (found,) = cand.restated_claim_candidates(store)
    assert {found.target_id, found.other_id} == {a, b}
    assert embedder.batches == [[CLAIM, REWORDING]]


def test_lexical_leg_and_cosine_floor(store: Store, embedder: _StubEmbedder) -> None:
    hub = _memory(store, "Hub", section="index")
    _memory(store, CLAIM, hub=hub)
    lexical = "After ingestion stores body chunks, the worker fills missing embeddings."
    _memory(store, lexical, hub=hub)
    _memory(store, REWORDING, hub=hub)
    embedder.vectors[lexical] = [0.0, 1.0] + [0.0] * (embedder.dim - 2)
    embedder.vectors[REWORDING] = [0.0, 0.0, 1.0] + [0.0] * (embedder.dim - 3)
    (found,) = cand.restated_claim_candidates(store)
    (match,) = found.evidence["matches"]
    assert match["distance"] == pytest.approx(1)
    assert match["lexical_overlap"] >= cand.RESTATED_MIN_LEXICAL


@pytest.mark.parametrize(
    "failure", ["unavailable", "wrong_model", "invalid_vectors", "mock"]
)
def test_unavailable_embedder_reports_not_checked_without_lexical_fallback(
    store: Store,
    embedder: _StubEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    hub = _memory(store, "Hub", section="index")
    a = _memory(store, CLAIM, hub=hub)
    b = _memory(store, CLAIM, hub=hub)
    if failure == "unavailable":

        def unavailable(texts: list[str]) -> list[list[float]]:
            raise RuntimeError("fixture unavailable")

        monkeypatch.setattr(embedder, "embed", unavailable)
    elif failure == "wrong_model":
        monkeypatch.setattr(type(embedder), "model", property(lambda self: "wrong"))
    elif failure == "invalid_vectors":
        monkeypatch.setattr(
            embedder, "embed", lambda texts: [[float("nan")]] * len(texts)
        )
    else:
        monkeypatch.setattr(
            type(embedder), "is_production", property(lambda self: False)
        )
    run = cand.run_detectors(store, passes=("memory-review",), considered_days=0)
    assert [(c.detector, c.target_id) for c in run.candidates] == [
        ("review_due", rid) for rid in (hub, a, b)
    ]
    assert [c.payload["not_checked"] for c in run.candidates] == [
        ["current_truth"],
        ["current_truth"],
        ["current_truth"],
    ]
    assert run.not_checked == {"restated_claim": "embedder_unavailable"}
    assert run.cursors == {
        "entity_claim_stale:memory": 0,
        "negative_undated:memory": 0,
        "review_due:memory": 0,
        "review_due:priority": 0,
        "source_drift:memory": 0,
        "title_shape:memory": 0,
    }
    outcome = run_garden_tick(
        store,
        passes=("memory-review",),
        considered_days=0,
        propose=lambda *_: Verdict(accept=False, rationale="fixture review debt only"),
    )
    assert outcome.not_checked == run.not_checked
    assert outcome.cursors == run.cursors
    assert outcome.candidates == 3
    assert outcome.proposals == 0


def test_limit_resumes_pairs_and_then_advances_hub(
    store: Store,
    embedder: _StubEmbedder,
) -> None:
    hub = _memory(store, "Hub", section="index")
    nodes = [_memory(store, CLAIM, hub=hub) for _ in range(3)]
    cursor: dict[str, int] = {}
    pairs: set[tuple[int, int | None]] = set()
    for _ in range(3):
        (found,) = cand.restated_claim_candidates(
            store, limit=1, cursors=cursor, next_cursors=cursor
        )
        pairs.add((found.target_id, found.other_id))
    assert pairs == {(nodes[1], nodes[0]), (nodes[2], nodes[0]), (nodes[2], nodes[1])}
    assert cursor["restated_claim:after_hub"] == hub
    assert (
        cand.restated_claim_candidates(store, cursors=cursor, next_cursors=cursor) == []
    )


def test_sentence_and_match_caps_preserve_next_window(
    store: Store,
    embedder: _StubEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cand, "RESTATED_SENTENCE_CAP", 6)
    monkeypatch.setattr(cand, "RESTATED_NODE_SENTENCE_CAP", 3)
    monkeypatch.setattr(cand, "RESTATED_MATCH_CAP", 2)
    hub = _memory(store, "Hub", section="index")
    nodes = [_memory(store, " ".join([CLAIM] * 4), hub=hub) for _ in range(4)]
    cursor: dict[str, int] = {}
    seen: set[int] = set()
    for _ in range(2):
        (found,) = cand.restated_claim_candidates(
            store, cursors=cursor, next_cursors=cursor
        )
        seen.update((found.target_id, int(found.other_id or 0)))
        assert len(found.evidence["matches"]) == 2
        assert found.payload["matches_capped"]
        assert found.payload["sentences_capped"]
    assert seen == set(nodes)
    assert all(len(batch) == 6 for batch in embedder.batches)


def test_detector_deadline_does_not_save_partial_cursor(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from psycopg.errors import QueryCanceled

    def cancelled(*args: Any, **kwargs: Any) -> list[cand.GardenCandidate]:
        kwargs["next_cursors"]["restated_claim:hub"] = 123
        raise QueryCanceled("fixture deadline")

    monkeypatch.setattr(cand, "restated_claim_candidates", cancelled)
    run = cand.run_detectors(store, passes=("memory-review",), detector_deadline_s=0)
    assert run.skipped == ["restated_claim"]
    assert run.cursors == {
        "entity_claim_stale:memory": 0,
        "negative_undated:memory": 0,
        "review_due:memory": 0,
        "review_due:priority": 0,
        "source_drift:memory": 0,
        "title_shape:memory": 0,
    }


def test_embed_deadline_reports_not_checked(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from precis.utils.deadline import DeadlineExceeded

    hub = _memory(store, "Hub", section="index")
    _memory(store, CLAIM, hub=hub)
    _memory(store, CLAIM, hub=hub)

    def expired(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["deadline_s"] <= cand.RESTATED_EMBED_SECONDS
        raise DeadlineExceeded("fixture expired", thread=threading.Thread())

    monkeypatch.setattr(cand, "call_with_deadline", expired)
    unchecked: dict[str, str] = {}
    cursor: dict[str, int] = {}
    assert (
        cand.restated_claim_candidates(
            store, next_cursors=cursor, not_checked=unchecked
        )
        == []
    )
    assert unchecked == {"restated_claim": "embedder_unavailable"}
    assert cursor == {}


def test_tick_records_rewrite_without_opening_consumer_apply(
    store: Store,
    embedder: _StubEmbedder,
) -> None:
    from precis.graph.garden.consumer import _reviewable
    from precis.graph.garden.proposals import pending_proposals

    hub = _memory(store, "Hub", section="index")
    a = _memory(store, CLAIM, hub=hub)
    b = _memory(store, REWORDING, hub=hub)
    before = cand._memory_bodies(store, [a, b])
    outcome = run_garden_tick(
        store,
        passes=("memory-review",),
        considered_days=0,
        propose=lambda *_: Verdict(accept=True, rationale="fixture same claim"),
    )
    assert outcome.candidates == outcome.proposals == 4
    proposals = pending_proposals(store, action="rewrite")
    assert {(p.detector, p.target_id) for p in proposals} == {
        ("restated_claim", b),
        ("review_due", hub),
        ("review_due", a),
        ("review_due", b),
    }
    (proposal,) = [p for p in proposals if p.detector == "restated_claim"]
    assert proposal.detector == "restated_claim"
    assert not _reviewable(proposal)
    assert proposal.payload["shas"] == {str(rid): sha for rid, _, sha in before}
    assert cand._memory_bodies(store, [a, b]) == before
