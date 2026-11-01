"""``twin:asked`` term capture (gr548276/gr548277): the what-is regex
table, the ``queue_asked_term`` write, the forge's asked-first ordering,
and the dated ``reading.cards_per_day`` cap tier.

Regex + first-sentence helpers are pure; the queue and ordering tests run
against real PG (the ``store`` fixture), like the flashcards tests.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from precis.reading.asked import (
    ASKED_TAG,
    extract_asked_term,
    first_sentence,
    queue_asked_term,
)
from precis.reading.flashcards import (
    _cardless_concepts,
    effective_cards_per_day,
    mint_daily_cards,
    run_card_forge,
)
from precis.reading.promote import create_concept
from precis.users import hash_password


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._text = json.dumps(payload)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=5)


def _concept(store: Any, name: str | None = None) -> int:
    return create_concept(
        store,
        name=name or f"concept-{uuid.uuid4().hex[:10]}",
        definition="a seeded test idea",
    )


def _tags(store: Any, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {r[0] for r in rows}


class TestExtractAskedTerm:
    @pytest.mark.parametrize(
        ("message", "term"),
        [
            ("what is entropy?", "entropy"),
            ("what is a catalyst?", "catalyst"),
            ("what are band gaps?", "band gaps"),
            ("what does NEB mean?", "NEB"),
            ("what does TPD mean?", "TPD"),
            ("remind me what a perovskite is", "perovskite"),
            ("perovskite?", "perovskite"),
            ("what is the Diels-Alder reaction?", "Diels-Alder reaction"),
            ("what do oxidation states mean?", "oxidation states"),
            ("What is spinel ferrite", "spinel ferrite"),
        ],
    )
    def test_positives(self, message: str, term: str) -> None:
        assert extract_asked_term(message) == term

    @pytest.mark.parametrize(
        "message",
        [
            # Ops/status questions are not vocabulary asks.
            "what is the status of X",
            "what is the status of the deploy?",
            "remind me what the status of the job is",
            # Grammar misses — the term must follow the fixed opener.
            "tell me what mitosis is",
            "can you explain gradient descent?",
            "what happened yesterday?",
            "how does photosynthesis work?",
            "what's up?",
            "why is the sky blue?",
            "remind me what to do",
            # "whats" still needs a verb — "a" isn't one.
            "whats a perovskite",
        ],
    )
    def test_negatives(self, message: str) -> None:
        assert extract_asked_term(message) is None

    def test_one_char_term_is_rejected(self) -> None:
        assert extract_asked_term("what is X?") is None


class TestFirstSentence:
    def test_takes_the_first_sentence(self) -> None:
        assert first_sentence("NEB finds saddle points. It is slow.") == (
            "NEB finds saddle points."
        )

    def test_single_sentence_and_empty(self) -> None:
        assert first_sentence("A catalyst speeds a reaction") == (
            "A catalyst speeds a reaction"
        )
        assert first_sentence("") == ""


class TestQueueAskedTerm:
    def test_mints_a_tagged_owned_concept(self, store: Any) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        conv = store.insert_ref(
            kind="conv", slug=f"discord/t-{uuid.uuid4().hex[:8]}", title="t", meta={}
        )
        term = f"term-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(
            store,
            term,
            owner_login="reto",
            conv_ref_id=conv.id,
            definition="a unit test idea",
        )
        assert out["created"] and out["kind"] == "concept"
        ref = store.get_ref(kind="concept", id=out["ref_id"])
        assert ref.owner_login == "reto"
        assert ref.meta["name"] == term
        assert ref.meta["definition"] == "a unit test idea"
        assert ref.meta["asked_count"] == 1
        assert ASKED_TAG in _tags(store, ref.id)
        with store.pool.connection() as conn:
            link = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (ref.id, conv.id),
            ).fetchone()
        assert link is not None

    def test_existing_concept_is_tagged_and_counted(self, store: Any) -> None:
        name = f"asked-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        out = queue_asked_term(store, name.upper())  # case-insensitive
        assert out["created"] is False and out["ref_id"] == cid
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["asked_count"] == 1
        assert ASKED_TAG in _tags(store, cid)
        queue_asked_term(store, name)
        assert store.get_ref(kind="concept", id=cid).meta["asked_count"] == 2

    def test_matches_an_alias_case_insensitively(self, store: Any) -> None:
        name = f"aliased-{uuid.uuid4().hex[:8]}"
        alias = f"aka-{uuid.uuid4().hex[:8]}"
        cid = create_concept(store, name=name, aliases=[alias])
        out = queue_asked_term(store, alias.upper())
        assert out["created"] is False and out["ref_id"] == cid

    def test_a_taxon_match_wins_before_minting(self, store: Any) -> None:
        name = f"taxon-{uuid.uuid4().hex[:8]}"
        tid = store.insert_ref(
            kind="taxon",
            slug=None,
            title=name,
            meta={"name": name, "norm_name": name.lower(), "status": "proposed"},
        ).id
        out = queue_asked_term(store, name)
        assert out["created"] is False
        assert out["kind"] == "taxon" and out["ref_id"] == tid
        assert ASKED_TAG in _tags(store, tid)
        assert store.get_ref(kind="taxon", id=tid).meta["asked_count"] == 1


class TestForgeOrdering:
    def test_asked_concepts_mint_first(self, store: Any) -> None:
        co = f"asked-{uuid.uuid4().hex[:8]}"
        # The asked concept is the OLDER row — recency alone would lose.
        asked = create_concept(store, name=f"a-{uuid.uuid4().hex[:8]}", cohort=co)
        plain = create_concept(store, name=f"p-{uuid.uuid4().hex[:8]}", cohort=co)
        store.add_tag(asked, _open_tag())
        assert asked < plain
        ids = [r[0] for r in _cardless_concepts(store, cohort=co, limit=10)]
        assert ids[0] == asked

        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted[0][0] == asked


class TestCardsPerDay:
    def test_dated_key_wins_for_that_day_only(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.budget import settings as app_settings

        monkeypatch.setenv("PRECIS_READING_CARDS_PER_DAY", "4")
        today = datetime.now(UTC).date()
        tomorrow = today + timedelta(days=1)
        # The durable tier must be empty for the tomorrow leg to prove the
        # dated key (not a stale durable row) is what scoped today.
        app_settings.clear_setting(store, "reading.cards_per_day")
        app_settings.set_setting(
            store, f"reading.cards_per_day:{today.isoformat()}", "9"
        )
        try:
            assert effective_cards_per_day(store, now=datetime.now(UTC)) == 9
            # Tomorrow the dated key no longer applies — env takes over.
            nxt = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
            assert effective_cards_per_day(store, now=nxt) == 4
        finally:
            app_settings.clear_setting(
                store, f"reading.cards_per_day:{today.isoformat()}"
            )

    def test_setting_beats_env_and_dated_beats_setting(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.budget import settings as app_settings

        monkeypatch.setenv("PRECIS_READING_CARDS_PER_DAY", "4")
        today = datetime.now(UTC).date()
        app_settings.set_setting(store, "reading.cards_per_day", "7")
        app_settings.set_setting(
            store, f"reading.cards_per_day:{today.isoformat()}", "11"
        )
        try:
            assert effective_cards_per_day(store, now=datetime.now(UTC)) == 11
            app_settings.clear_setting(
                store, f"reading.cards_per_day:{today.isoformat()}"
            )
            assert effective_cards_per_day(store, now=datetime.now(UTC)) == 7
            app_settings.clear_setting(store, "reading.cards_per_day")
            assert effective_cards_per_day(store, now=datetime.now(UTC)) == 4
        finally:
            for key in (
                "reading.cards_per_day",
                f"reading.cards_per_day:{today.isoformat()}",
            ):
                app_settings.clear_setting(store, key)

    def test_dated_cap_bounds_the_mint_that_day(self, store: Any) -> None:
        from precis.budget import settings as app_settings

        co = f"cap-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            create_concept(store, name=f"c-{uuid.uuid4().hex[:8]}", cohort=co)
        today = datetime.now(UTC).date()
        app_settings.set_setting(
            store, f"reading.cards_per_day:{today.isoformat()}", "1"
        )
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        try:
            report = run_card_forge(store, client=client, cohort=co, act=False)
            assert len(report.minted) == 1
        finally:
            app_settings.clear_setting(
                store, f"reading.cards_per_day:{today.isoformat()}"
            )


def _open_tag() -> Any:
    from precis.store import Tag

    return Tag.open(ASKED_TAG)
