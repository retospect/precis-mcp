"""Tests for the paper→topic-dossier cascade classifier.

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
    _SYS,
    CLASSIFY_TOPICS_VERSION,
    MARKER_NAMESPACE,
    _build_prompt,
    _context_text,
    _extract_json,
    _load_topics,
    _tier0_candidates,
    all_topic_slugs,
    prompt_preview,
    run_classify_topics_pass,
    topic_marker_value,
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
        assert {
            "healthspan",
            "molelec",
            "noxrr",
            "llm",
            "ml-general",
            "bayesian-statistics",
            "co2-conversion",
            "catalyst-stability",
            "nh3-synthesis",
            "mof",
            "nanobuds",
            "carbon-cad",
            "mof-tools",
            "catalysis-tools",
        } <= slugs
        for t in topics:
            assert t.get("description")
            assert isinstance(t.get("keywords"), list) and t["keywords"]

    def test_tier0_candidates_matches_new_topic_keyword(self) -> None:
        topics = _load_topics()
        hits = _tier0_candidates(
            topics, "A study of zeolitic imidazolate frameworks for gas storage"
        )
        assert "mof" in hits

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


class TestPromptPreview:
    """``prompt_preview`` — the ``/categorizers`` hover popover's source of
    truth (follow-up, #5). Must reuse ``_build_prompt``/``_SYS`` so
    the preview can't drift from the real pass."""

    def test_full_taxonomy_preview_has_system_and_user(self) -> None:
        preview = prompt_preview()
        assert preview["system"] == _SYS
        assert preview["user"]
        assert "healthspan:" in preview["user"]  # a real topic slug is listed

    def test_narrows_to_enabled_subset(self) -> None:
        preview = prompt_preview(enabled_slugs=["healthspan"])
        assert "healthspan:" in preview["user"]
        assert "mof:" not in preview["user"]  # disabled slug does not appear


class TestTopicMarkerValue:
    """``topic_marker_value`` — order-independent, set-sensitive,
    stable digest of the enabled-topic set, keyed under the pass's version."""

    def test_order_independent(self) -> None:
        assert topic_marker_value(["a", "b"]) == topic_marker_value(["b", "a"])

    def test_set_sensitive(self) -> None:
        assert topic_marker_value(["a"]) != topic_marker_value(["a", "b"])

    def test_stable_across_calls(self) -> None:
        assert topic_marker_value(["nh3-synthesis", "mof"]) == topic_marker_value(
            ["nh3-synthesis", "mof"]
        )

    def test_carries_the_pass_version_prefix(self) -> None:
        assert topic_marker_value(["mof"]).startswith(f"{CLASSIFY_TOPICS_VERSION}-")


# ── end-to-end pass (real PG, fake client) ─────────────────────────────


def _seed_paper(store: Any, title: str, body: str, *, kind: str = "paper") -> int:
    from tests.workers._helpers import seed_chunk, seed_ref

    ref_id = seed_ref(store, title=title, kind=kind)
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


def _has_marker(store: Any, ref_id: int, marker_value: str | None = None) -> bool:
    """Whether ``ref_id`` carries the ``TOPICCASCADE`` marker.

    ``marker_value`` defaults to the value a default (``enabled_slugs=None``)
    pass call writes — ``topic_marker_value(all_topic_slugs())``.
    """
    value = (
        marker_value
        if marker_value is not None
        else topic_marker_value(all_topic_slugs())
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s AND t.value = %s",
            (ref_id, MARKER_NAMESPACE, value),
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

    def test_patent_ref_with_body_chunk_no_abstract_is_swept(self, store: Any) -> None:
        # No card_abstract chunk — _context_text() must fall back to the
        # first ord>=0 body chunks. kind='patent' must now be in the claim's
        # scope.
        ref_id = _seed_paper(
            store,
            "A metal-organic framework device for gas separation",
            "This patent discloses a zeolitic imidazolate framework used "
            "for selective gas separation.",
            kind="patent",
        )
        client = _FakeClient('{"topics": ["mof"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"mof": 1}}
        assert len(client.calls) == 1  # tier-0 keyword hit reached the LLM
        assert _topic_tags(store, ref_id) == {"topic:mof"}
        assert _has_marker(store, ref_id)

    def test_new_topic_tier0_hit_produces_topic_tag(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "Reticular chemistry for porous coordination polymers",
            "We report a new zeolitic imidazolate framework (ZIF) with "
            "record surface area.",
        )
        client = _FakeClient('{"topics": ["mof"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"mof": 1}}
        assert _topic_tags(store, ref_id) == {"topic:mof"}
        assert _has_marker(store, ref_id)

    def test_ref_ids_scopes_the_sweep_to_named_papers_only(self, store: Any) -> None:
        """``ref_ids`` restricts the claim to specific refs — a sibling paper
        outside scope must stay completely untouched (targeted backfill via
        `precis classify topics`, mirroring the ``role3`` scoping test)."""
        ref_a = _seed_paper(
            store,
            "A MOF catalyst for NOx reduction",
            "We report a metal-organic-framework catalyst for NOx reduction.",
        )
        ref_b = _seed_paper(
            store,
            "A MOF catalyst for NOx reduction (sibling)",
            "We report a metal-organic-framework catalyst for NOx reduction.",
        )
        client = _FakeClient('{"topics": ["noxrr"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_a]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"noxrr": 1}}
        assert len(client.calls) == 1
        assert _topic_tags(store, ref_a) == {"topic:noxrr"}
        assert _topic_tags(store, ref_b) == set()  # untouched — outside scope
        assert _has_marker(store, ref_a)
        assert not _has_marker(store, ref_b)

    def test_context_text_thin_abstract_falls_back_to_body_chunks(
        self, store: Any
    ) -> None:
        from tests.workers._helpers import seed_chunk, seed_ref

        ref_id = seed_ref(store, title="A paper with a thin abstract")
        seed_chunk(
            store, ref_id=ref_id, ord=-1, chunk_kind="card_abstract", text="TBD."
        )
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            text="We report a metal-organic-framework catalyst for NOx reduction.",
        )
        seed_chunk(
            store, ref_id=ref_id, ord=1, text="Second body paragraph with more detail."
        )

        with store.pool.connection() as conn:
            context = _context_text(conn, ref_id)

        assert "metal-organic-framework catalyst for NOx reduction" in context
        assert "Second body paragraph" in context

    def test_context_text_full_abstract_skips_body_fallback(self, store: Any) -> None:
        from tests.workers._helpers import seed_chunk, seed_ref

        full_abstract = "A robust catalyst study. " * 20  # well over 400 chars
        assert len(full_abstract.strip()) >= 400
        ref_id = seed_ref(store, title="A paper with a full abstract")
        seed_chunk(
            store, ref_id=ref_id, ord=-1, chunk_kind="card_abstract", text=full_abstract
        )
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            text="BODY-ONLY-MARKER should not appear in the returned context.",
        )

        with store.pool.connection() as conn:
            context = _context_text(conn, ref_id)

        assert context == full_abstract.strip()
        assert "BODY-ONLY-MARKER" not in context

    def test_thin_abstract_body_only_keyword_reaches_tier0_and_tier1(
        self, store: Any
    ) -> None:
        """A paper whose abstract is thin/uninformative but whose body carries
        a topic keyword must still get flagged by tier-0 and confirmed by
        tier-1 — proving the fallback context actually reaches both stages."""
        from tests.workers._helpers import seed_chunk, seed_ref

        ref_id = seed_ref(store, title="Some paper")
        seed_chunk(
            store, ref_id=ref_id, ord=-1, chunk_kind="card_abstract", text="TBD."
        )
        seed_chunk(
            store,
            ref_id=ref_id,
            ord=0,
            text="We report a zeolitic imidazolate framework with record surface area.",
        )
        client = _FakeClient('{"topics": ["mof"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"mof": 1}}
        assert len(client.calls) == 1  # tier-0 hit via body-only fallback text
        assert _topic_tags(store, ref_id) == {"topic:mof"}

    def test_existing_open_tag_helper_matches_written_value(self, store: Any) -> None:
        # Sanity: our raw-SQL read of ref_tags/tags matches what Tag.open()
        # actually produces (lowercased, namespace='OPEN').
        assert Tag.open("topic:HealthSpan").value == "topic:healthspan"


# ── per-topic gating ─────────────────────────────────────────


class TestPerTopicGating:
    """``enabled_slugs`` filters the taxonomy the pass classifies against
    (route: ``/categorizers`` per-topic toggles), and the marker it writes
    encodes that set — driving the lazy backfill re-sweep on a toggle."""

    def test_enabled_slugs_filters_to_only_that_topic(self, store: Any) -> None:
        # Body carries keyword hits for BOTH nh3-synthesis ("Haber-Bosch")
        # and mof ("zeolitic imidazolate framework") — but only nh3-synthesis
        # is enabled, so mof must never be tagged even if the (fake) model
        # hallucinates it back.
        ref_id = _seed_paper(
            store,
            "Ammonia synthesis over a novel catalyst",
            "We report a Haber-Bosch catalyst and also discuss a zeolitic "
            "imidazolate framework used in the reactor housing.",
        )
        client = _FakeClient('{"topics": ["nh3-synthesis", "mof"]}')

        result = run_classify_topics_pass(
            store,
            client=client,
            batch_size=10,
            enabled_slugs=["nh3-synthesis"],
            ref_ids=[ref_id],
        )

        assert result == {
            "claimed": 1,
            "ok": 1,
            "failed": 0,
            "dist": {"nh3-synthesis": 1},
        }
        assert _topic_tags(store, ref_id) == {"topic:nh3-synthesis"}
        marker = topic_marker_value(["nh3-synthesis"])
        assert _has_marker(store, ref_id, marker)

    def test_disabled_topic_never_tagged_even_on_keyword_match(
        self, store: Any
    ) -> None:
        # mof's own keyword is present, but mof is NOT in enabled_slugs — it
        # must not even reach tier-0 candidacy for the LLM call.
        ref_id = _seed_paper(
            store,
            "A metal-organic framework",
            "We report a zeolitic imidazolate framework with record surface area.",
        )
        client = _FakeClient('{"topics": ["mof"]}')

        result = run_classify_topics_pass(
            store,
            client=client,
            batch_size=10,
            enabled_slugs=["nh3-synthesis"],
            ref_ids=[ref_id],
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {}}
        assert client.calls == []  # mof isn't a candidate under this subset
        assert _topic_tags(store, ref_id) == set()

    def test_empty_enabled_slugs_is_a_noop(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "A MOF catalyst for NOx reduction",
            "We report a metal-organic-framework catalyst for NOx reduction.",
        )
        client = _FakeClient('{"topics": ["mof"]}')

        result = run_classify_topics_pass(
            store, client=client, batch_size=10, enabled_slugs=[], ref_ids=[ref_id]
        )

        assert result == {"claimed": 0, "ok": 0, "failed": 0}
        assert client.calls == []
        assert _topic_tags(store, ref_id) == set()
        assert not _has_marker(store, ref_id, topic_marker_value([]))

    def test_toggling_enabled_set_backfills_via_new_marker(self, store: Any) -> None:
        """A paper marked done under set {A} is re-claimed once the enabled
        set grows to {A, B} (a different marker value) — the toggle-driven
        lazy backfill — and stays done (not re-claimed) once re-run against
        the same {A, B} set (idempotent within a set)."""
        ref_id = _seed_paper(
            store,
            "Ammonia synthesis over a novel catalyst",
            "We report a Haber-Bosch catalyst for ammonia synthesis.",
        )
        client = _FakeClient('{"topics": ["nh3-synthesis"]}')

        first = run_classify_topics_pass(
            store,
            client=client,
            batch_size=10,
            enabled_slugs=["nh3-synthesis"],
            ref_ids=[ref_id],
        )
        assert first == {
            "claimed": 1,
            "ok": 1,
            "failed": 0,
            "dist": {"nh3-synthesis": 1},
        }
        assert _has_marker(store, ref_id, topic_marker_value(["nh3-synthesis"]))

        # Enabling a second topic changes the marker -> re-claimed.
        second = run_classify_topics_pass(
            store,
            client=client,
            batch_size=10,
            enabled_slugs=["nh3-synthesis", "mof"],
            ref_ids=[ref_id],
        )
        assert second["claimed"] == 1
        assert _has_marker(store, ref_id, topic_marker_value(["nh3-synthesis", "mof"]))
        # The prior marker is gone — replaced (Tag.closed replace_prefix=True).
        assert not _has_marker(store, ref_id, topic_marker_value(["nh3-synthesis"]))

        # Re-running against the SAME {nh3-synthesis, mof} set is idempotent.
        third = run_classify_topics_pass(
            store,
            client=client,
            batch_size=10,
            enabled_slugs=["nh3-synthesis", "mof"],
            ref_ids=[ref_id],
        )
        assert third == {"claimed": 0, "ok": 0, "failed": 0}
