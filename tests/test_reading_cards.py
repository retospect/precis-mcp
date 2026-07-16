"""Tests for the morning card work (`reading/cards.py`) and mastery-from-Anki
(`reading/mastery.py`).

Pure strength/report helpers run everywhere; the mint/rework/mastery passes run
against real PG (the `store` fixture) with seeded concepts + a fake client — no
Anki dependency (stats are patched straight into ``meta.anki_stats``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from precis.reading.cards import (
    ForgeReport,
    author_card,
    mint_daily_cards,
    rework_stale_cards,
)
from precis.reading.mastery import card_strength, concept_mastery, run_mastery_pass
from precis.reading.promote import create_concept


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._text = json.dumps(payload)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=5)


def _concept(
    store: Any,
    *,
    mastery: float | None = None,
    state: str | None = None,
    cohort: str | None = None,
) -> int:
    cid = create_concept(
        store,
        name=f"concept-{uuid.uuid4().hex[:10]}",
        definition="a seeded test idea",
        cohort=cohort,
    )
    patch: dict[str, Any] = {}
    if mastery is not None:
        patch["mastery"] = mastery
    if state is not None:
        patch["state"] = state
    if patch:
        store.update_ref(cid, meta_patch=patch)
    return cid


def _card(store: Any, concept_id: int, *, stats: dict[str, Any] | None = None) -> int:
    card_id = author_card(
        store,
        text=f"The answer is {{{{c1::{uuid.uuid4().hex[:6]}}}}}.",
        concept_id=concept_id,
        deck="Precis::reading",
    )
    if stats is not None:
        store.update_ref(card_id, meta_patch={"anki_stats": stats})
    return card_id


def _age(store: Any, ref_id: int, days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(days => %s) "
            "WHERE ref_id = %s",
            (days, ref_id),
        )


_LEECH = {"interval_min": 2, "ease_min": 1.8, "lapses_total": 5, "reps_total": 12}
_STRONG = {"interval_min": 30, "ease_min": 2.5, "lapses_total": 0, "reps_total": 6}


class TestStrength:
    def test_unreviewed_and_empty_are_zero(self) -> None:
        assert card_strength(None) == 0.0
        assert card_strength({"unreviewed": True, "interval_min": 40}) == 0.0

    def test_interval_scales_and_saturates(self) -> None:
        assert card_strength({"interval_min": 30}) == 1.0  # past the 21d horizon
        assert card_strength({"interval_min": 10.5}) == pytest.approx(0.5)

    def test_leech_is_capped(self) -> None:
        # A long interval can't certify mastery while the card keeps lapsing.
        assert card_strength({"interval_min": 40, "lapses_total": 5}) == 0.3
        assert card_strength({"interval_min": 40, "ease_min": 1.9}) == 0.3

    def test_concept_mastery_is_mean(self) -> None:
        assert concept_mastery([]) == 0.0
        assert concept_mastery([{"interval_min": 21}, {"unreviewed": True}]) == 0.5


class TestMasteryPass:
    def test_aggregates_cards_and_derives_state(self, store: Any) -> None:
        cid = _concept(store)
        _card(store, cid, stats=_STRONG)
        _card(store, cid, stats={"unreviewed": True})
        run_mastery_pass(store)
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["mastery"] == pytest.approx(0.5)
        assert ref.meta["state"] == "active"
        assert ref.meta["mastery_updated_at"]

    def test_all_strong_cards_master_the_concept(self, store: Any) -> None:
        cid = _concept(store)
        _card(store, cid, stats=_STRONG)
        _card(store, cid, stats=_STRONG)
        run_mastery_pass(store)
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["mastery"] == pytest.approx(1.0)
        assert ref.meta["state"] == "mastered"


class TestRecoveryReset:
    """gripe 161957: escalation is a state, not a life sentence — a concept
    whose cards prove healthy again gets streak + escalation cleared."""

    def _escalated(self, store: Any) -> int:
        cid = _concept(store)
        store.update_ref(
            cid,
            meta_patch={"remunge_streak": 3, "escalated_at": "2026-07-10T05:30:00Z"},
        )
        return cid

    def test_proven_healthy_card_resets_streak_and_escalation(self, store: Any) -> None:
        cid = self._escalated(store)
        card = _card(store, cid, stats=_STRONG)  # reviewed, no leech
        _age(store, card, 5)  # past the proving window
        out = run_mastery_pass(store)
        assert out["recovered"] >= 1
        meta = store.get_ref(kind="concept", id=cid).meta
        assert meta["remunge_streak"] == 0
        assert meta["escalated_at"] is None

    def test_fresh_rewrite_does_not_reset(self, store: Any) -> None:
        # The morning after a rewrite the new card isn't a leech *yet* — that
        # must not clear the streak, or the escalation cap could never engage.
        cid = self._escalated(store)
        _card(store, cid, stats={"unreviewed": True})  # brand-new card
        run_mastery_pass(store)
        meta = store.get_ref(kind="concept", id=cid).meta
        assert meta["remunge_streak"] == 3
        assert meta["escalated_at"]

    def test_lingering_leech_does_not_reset(self, store: Any) -> None:
        cid = self._escalated(store)
        good = _card(store, cid, stats=_STRONG)
        bad = _card(store, cid, stats=_LEECH)
        _age(store, good, 5)
        _age(store, bad, 5)
        run_mastery_pass(store)
        meta = store.get_ref(kind="concept", id=cid).meta
        assert meta["remunge_streak"] == 3  # still failing — no fresh budget

    def test_recovered_concept_gets_fresh_rewrite_budget(self, store: Any) -> None:
        # End-to-end: reset via the mastery pass, then a new leech on the same
        # concept goes down the rewrite path instead of escalating. The healthy
        # card is deliberately not strong enough to *master* the concept —
        # a mastered concept would route the leech to retire, not rewrite.
        cid = self._escalated(store)
        good = _card(
            store, cid, stats={"interval_min": 10, "ease_min": 2.5, "lapses_total": 0}
        )
        _age(store, good, 5)
        run_mastery_pass(store)

        bad = _card(store, cid, stats=_LEECH)
        _age(store, bad, 5)
        client = _FakeClient({"text": "A fresh {{c1::angle}}."})
        report = rework_stale_cards(store, client=client, act=True, streak_cap=3)
        d = next(d for d in report.decisions if d.card_id == bad)
        assert d.action == "rewrite" and d.applied


class TestAuthorCard:
    def test_writes_ref_link_and_search_card(self, store: Any) -> None:
        cid = _concept(store)
        card_id = author_card(
            store,
            text="Water boils at {{c1::100}} degrees.\n---\nat sea level",
            concept_id=cid,
            deck="Precis::reading",
        )
        ref = store.get_ref(kind="anki", id=card_id)
        assert ref.meta["fields"]["Text"] == "Water boils at {{c1::100}} degrees."
        assert ref.meta["fields"]["Back Extra"] == "at sea level"
        assert ref.meta["authored_by"] == "card_forge"
        with store.pool.connection() as conn:
            chunk = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'card_combined'",
                (card_id,),
            ).fetchone()
            link = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'represents'",
                (cid, card_id),
            ).fetchone()
        assert chunk is not None and "100" in chunk[0] and "{{c1" not in chunk[0]
        assert link is not None

    def test_rejects_non_cloze(self, store: Any) -> None:
        cid = _concept(store)
        with pytest.raises(ValueError):
            author_card(store, text="no deletion here", concept_id=cid, deck="Precis")


class TestMint:
    """Each test scopes to a unique cohort — the shared test DB holds concepts
    from every other test, and the daily cap counts cohort-scoped."""

    def test_mints_up_to_cap_and_activates_concepts(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        c2 = _concept(store, cohort=co)  # newest — picked first
        client = _FakeClient(
            {"cards": [{"text": "X is {{c1::Y}}.", "back_extra": "src"}]}
        )
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert len(report.minted) == 1
        concept_id, cards = report.minted[0]
        assert concept_id == c2  # newest cardless concept first
        ref = store.get_ref(kind="anki", id=cards[0])
        assert ref.meta["fields"]["Back Extra"] == "src"
        assert ref.meta["deck"] == f"Precis::{co}"
        assert store.get_ref(kind="concept", id=concept_id).meta["state"] == "active"

    def test_rerun_tops_up_to_cap_not_beyond(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        _concept(store, cohort=co)
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        first = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert len(first.minted) == 1
        again = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert again.minted == []  # today's cap already spent

    def test_skips_concepts_that_already_have_cards(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, cohort=co)
        _card(store, cid)
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=50, cohort=co)
        assert cid not in [c for c, _ in report.minted]

    def test_invalid_model_output_is_skipped(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        client = _FakeClient({"cards": [{"text": "no cloze markup"}]})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted == [] and report.skipped == 1


class TestRework:
    def _stale_leech(self, store: Any, cid: int) -> int:
        card_id = _card(store, cid, stats=_LEECH)
        _age(store, card_id, 5)
        return card_id

    def test_young_or_healthy_cards_untouched(self, store: Any) -> None:
        cid = _concept(store)
        young = _card(store, cid, stats=_LEECH)  # leech but < min_age_days old
        healthy = _card(store, cid, stats=_STRONG)
        _age(store, healthy, 10)
        report = rework_stale_cards(store, client=_FakeClient({}), act=False)
        decided = {d.card_id for d in report.decisions}
        assert young not in decided and healthy not in decided

    def test_report_mode_decides_but_writes_nothing(self, store: Any) -> None:
        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        client = _FakeClient({"text": "New {{c1::angle}}."})
        report = rework_stale_cards(store, client=client, act=False)
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "rewrite" and not d.applied
        assert client.calls == []  # report mode never spends a model call
        assert store.get_ref(kind="anki", id=card_id) is not None  # still live

    def test_act_rewrite_replaces_card_and_bumps_streak(self, store: Any) -> None:
        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        client = _FakeClient({"text": "A fresh {{c1::breakdown}}."})
        report = rework_stale_cards(store, client=client, act=True)
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "rewrite" and d.applied and d.new_card_id
        assert store.get_ref(kind="anki", id=card_id) is None  # old card retired
        new = store.get_ref(kind="anki", id=d.new_card_id)
        assert new.meta["rework_of"] == card_id
        assert store.get_ref(kind="concept", id=cid).meta["remunge_streak"] == 1

    def test_mastered_concept_retires_the_leech(self, store: Any) -> None:
        cid = _concept(store, state="mastered")
        card_id = self._stale_leech(store, cid)
        report = rework_stale_cards(store, client=_FakeClient({}), act=True)
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "retire" and d.applied
        assert store.get_ref(kind="anki", id=card_id) is None

    def test_weak_prereq_diverts_to_teaching_it(self, store: Any) -> None:
        prereq = _concept(store, mastery=0.1)
        cid = _concept(store)
        store.add_link(src_ref_id=cid, dst_ref_id=prereq, relation="has-prerequisite")
        card_id = self._stale_leech(store, cid)
        report = rework_stale_cards(store, client=_FakeClient({}), act=True)
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "teach-prereq" and d.applied
        assert store.get_ref(kind="anki", id=card_id) is not None  # card kept
        assert store.get_ref(kind="concept", id=prereq).meta["state"] == "active"

    def test_streak_cap_escalates_to_human(self, store: Any) -> None:
        cid = _concept(store)
        store.update_ref(cid, meta_patch={"remunge_streak": 3})
        card_id = self._stale_leech(store, cid)
        report = rework_stale_cards(
            store, client=_FakeClient({}), act=True, streak_cap=3
        )
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "escalate" and d.applied
        assert store.get_ref(kind="concept", id=cid).meta["escalated_at"]
        assert store.get_ref(kind="anki", id=card_id) is not None


class TestReportLines:
    def test_lines_read_as_audit_log(self) -> None:
        from precis.reading.cards import CardDecision

        r = ForgeReport(
            minted=[(5, [9])],
            decisions=[
                CardDecision(9, 5, "rewrite", "why", applied=True, new_card_id=10)
            ],
            skipped=1,
        )
        text = "\n".join(r.lines())
        assert "minted 1 card(s) for concept cn5" in text
        assert "did rewrite ak9" in text and "→ ak10" in text
        assert "skipped 1" in text
