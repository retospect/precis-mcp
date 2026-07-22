"""Tests for the paper→topic-dossier cascade classifier (ADR 0060).

Pure helpers (tier-0 screen / prompt / parse) run everywhere. The end-to-end
pass runs against real PG (the ``store`` fixture) with a fake LLM client — no
network — so it exercises the claim SQL, multi-label tag writes, and
idempotency.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.store.types import Tag
from precis.workers.classify_topics import (
    CLASSIFY_TOPICS_VERSION,
    MARKER_NAMESPACE,
    _build_prompt,
    _extract_json,
    _load_topics,
    _tier0_candidates,
    run_classify_topics_pass,
)


class _FakeClient:
    """Records calls; returns a fixed completion text (like ``LlmClient``)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=7)


# ── pure helpers ───────────────────────────────────────────────────────


class TestPure:
    def test_topics_load_with_required_fields(self) -> None:
        topics = _load_topics()
        slugs = {t["slug"] for t in topics}
        assert {"healthspan", "molelec", "noxrr", "llm-improvements"} <= slugs
        for t in topics:
            assert t.get("description")
            assert isinstance(t.get("keywords"), list) and t["keywords"]

    def test_tier0_candidates_matches_keyword(self) -> None:
        topics = _load_topics()
        hits = _tier0_candidates(
            topics, "A study of senescence and inflammaging in mice"
        )
        assert "healthspan" in hits

    def test_tier0_candidates_multi_label(self) -> None:
        topics = _load_topics()
        hits = _tier0_candidates(
            topics,
            "A MOF catalyst for NOx reduction that also modulates biomarker "
            "levels via an inflammatory cascade",
        )
        assert "noxrr" in hits
        assert "healthspan" in hits

    def test_tier0_candidates_none_for_unrelated_text(self) -> None:
        topics = _load_topics()
        assert _tier0_candidates(topics, "A survey of medieval Latin poetry") == []

    def test_extract_json_plain_and_embedded(self) -> None:
        assert _extract_json('{"topics": ["healthspan"]}') == {"topics": ["healthspan"]}
        assert _extract_json('junk {"topics": []} junk') == {"topics": []}
        assert _extract_json("not json") is None
        assert _extract_json("") is None
        assert _extract_json("[1, 2]") is None  # a list is not a topics object

    def test_build_prompt_includes_candidates_and_topics(self) -> None:
        topics = _load_topics()
        prompt = _build_prompt(
            topics, ["healthspan"], "A study of aging", "We studied mice."
        )
        assert "A study of aging" in prompt
        assert "healthspan:" in prompt
        assert "healthspan" in prompt.split("flagged")[1]  # candidate line mentions it
        assert '"topics"' in prompt


# ── end-to-end pass (real PG, fake client) ─────────────────────────────


def _seed_paper(store: Any, title: str, body: str) -> int:
    from tests.workers._helpers import seed_chunk, seed_ref

    ref_id = seed_ref(store, title=title)
    seed_chunk(store, ref_id=ref_id, text=body, ord=0)
    return ref_id


def _topic_tags(store: Any, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value LIKE 'topic:%%'",
            (ref_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _has_marker(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s AND t.value = %s",
            (ref_id, MARKER_NAMESPACE, CLASSIFY_TOPICS_VERSION),
        ).fetchone()
    return row is not None


class TestPass:
    def test_writes_multi_label_topic_tags(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "A MOF catalyst for NOx reduction and inflammatory biomarkers",
            "We report a metal-organic-framework catalyst for NOx reduction "
            "that also modulates an inflammatory cascade biomarker.",
        )
        client = _FakeClient('{"topics": ["noxrr", "healthspan"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {
            "claimed": 1,
            "ok": 1,
            "failed": 0,
            "dist": {"noxrr": 1, "healthspan": 1},
        }
        assert len(client.calls) == 1
        assert _topic_tags(store, ref_id) == {"topic:noxrr", "topic:healthspan"}
        assert _has_marker(store, ref_id)

    def test_no_keyword_hits_skips_llm_call_writes_marker(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "A survey of medieval Latin poetry",
            "This paper has nothing to do with our topics.",
        )
        client = _FakeClient('{"topics": ["healthspan"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {}}
        assert client.calls == []  # no candidates → no LLM call
        assert _topic_tags(store, ref_id) == set()
        assert _has_marker(store, ref_id)

    def test_llm_rejects_all_candidates(self, store: Any) -> None:
        ref_id = _seed_paper(
            store, "A study of senescence", "We studied senescence markers in mice."
        )
        client = _FakeClient('{"topics": []}')  # keyword hit, but model says no

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {}}
        assert len(client.calls) == 1
        assert _topic_tags(store, ref_id) == set()
        assert _has_marker(store, ref_id)

    def test_idempotent_not_reclaimed(self, store: Any) -> None:
        ref_id = _seed_paper(
            store, "A study of senescence", "We studied senescence markers in mice."
        )
        client = _FakeClient('{"topics": ["healthspan"]}')
        first = run_classify_topics_pass(
            store, client=client, batch_size=100, ref_ids=[ref_id]
        )
        assert first == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"healthspan": 1}}

        second = run_classify_topics_pass(
            store, client=client, batch_size=100, ref_ids=[ref_id]
        )
        assert second == {"claimed": 0, "ok": 0, "failed": 0}
        assert len(client.calls) == 1  # only the first pass called the model

    def test_unparseable_output_is_failed_no_write_not_reclaim_safe(
        self, store: Any
    ) -> None:
        ref_id = _seed_paper(
            store, "A study of senescence", "We studied senescence markers in mice."
        )
        client = _FakeClient("sorry, I cannot help with that")

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 0, "failed": 1, "dist": {}}
        assert _topic_tags(store, ref_id) == set()
        assert not _has_marker(store, ref_id)  # stays claimable for a retry

    def test_invalid_slug_from_model_is_dropped(self, store: Any) -> None:
        ref_id = _seed_paper(
            store, "A study of senescence", "We studied senescence markers in mice."
        )
        # Model hallucinates a slug that isn't in the taxonomy.
        client = _FakeClient('{"topics": ["healthspan", "not-a-real-topic"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"healthspan": 1}}
        assert _topic_tags(store, ref_id) == {"topic:healthspan"}

    def test_existing_open_tag_helper_matches_written_value(self, store: Any) -> None:
        # Sanity: our raw-SQL read of ref_tags/tags matches what Tag.open()
        # actually produces (lowercased, namespace='OPEN').
        assert Tag.open("topic:HealthSpan").value == "topic:healthspan"
