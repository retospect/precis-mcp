"""Tests for the cast substrate (profiles, budgets, standalone dated drafts)."""

from __future__ import annotations

import uuid
from typing import Any

from precis.reading.cast_common import (
    CAST_PROFILES,
    cast_slug,
    compose_max_tokens,
    create_cast_draft,
    find_cast_draft,
    word_budget,
)


class TestBudgets:
    def test_word_budget_is_minutes_times_wpm(self) -> None:
        assert word_budget(CAST_PROFILES["reading"]) == 15 * 150
        assert word_budget(CAST_PROFILES["nidra"]) == 45 * 110

    def test_target_minutes_override(self) -> None:
        assert word_budget(CAST_PROFILES["nidra"], target_minutes=10) == 10 * 110

    def test_max_tokens_scales_with_budget(self) -> None:
        p = CAST_PROFILES["reading"]
        assert compose_max_tokens(p) > word_budget(p)  # tokens > words

    def test_profiles_have_the_two_casts(self) -> None:
        assert set(CAST_PROFILES) == {"reading", "nidra"}
        assert CAST_PROFILES["reading"].voice == "bm_george"
        assert CAST_PROFILES["nidra"].voice == "af_nicole"


class TestCreateCastDraft:
    def test_idempotent_per_slug(self, store: Any) -> None:
        p = CAST_PROFILES["nidra"]
        slug = f"cast-nidra-test-{uuid.uuid4().hex[:8]}"
        ref1, created1 = create_cast_draft(store, profile=p, date_tag="x", slug=slug)
        assert created1 is True
        # Second call for the same slug returns the same ref, writes nothing.
        ref2, created2 = create_cast_draft(store, profile=p, date_tag="x", slug=slug)
        assert created2 is False
        assert int(ref2.id) == int(ref1.id)

    def test_stamps_cast_and_voice_meta(self, store: Any) -> None:
        p = CAST_PROFILES["reading"]
        slug = f"cast-reading-test-{uuid.uuid4().hex[:8]}"
        ref, _ = create_cast_draft(store, profile=p, date_tag="2026-07-15", slug=slug)
        with store.pool.connection() as conn:
            meta = conn.execute(
                "SELECT meta FROM refs WHERE ref_id=%s", (ref.id,)
            ).fetchone()[0]
        assert meta["cast"] == "reading"
        assert meta["voice"] == "bm_george"
        assert meta["date"] == "2026-07-15"

    def test_findable_by_slug(self, store: Any) -> None:
        p = CAST_PROFILES["nidra"]
        date_tag = f"find-{uuid.uuid4().hex[:8]}"
        assert find_cast_draft(store, p, date_tag) is None
        ref, _ = create_cast_draft(store, profile=p, date_tag=date_tag)
        found = find_cast_draft(store, p, date_tag)
        assert found is not None and int(found.id) == int(ref.id)
        assert cast_slug(p, date_tag) == f"cast-nidra-{date_tag}"
