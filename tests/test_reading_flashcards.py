"""Tests for the morning card work (`reading/flashcards.py`) and
mastery-from-Anki (`reading/mastery.py`).

Pure strength/report helpers run everywhere; the mint/rework/mastery passes run
against real PG (the `store` fixture) with seeded concepts + a fake client — no
Anki dependency (stats are patched straight into ``meta.anki_stats``).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from precis.reading.flashcards import (
    _MINT_SYS,
    _REWORK_SYS,
    ForgeReport,
    _stale_leeches,
    author_card,
    cloze_skill_preamble,
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
        assert ref.meta["recoverability_check"] == "unchecked"
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

    def test_budget_counts_distinct_clozes_per_note_on_rerun(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        cid = _concept(store, cohort=co)
        client = _FakeClient(
            {
                "cards": [
                    {"text": "{{c1,3::A}} and {{c1::B}}."},
                    {"text": "{{c1::D}}."},
                ]
            }
        )
        report = mint_daily_cards(store, client=client, per_day=2, cohort=co)
        [(minted_concept, [note])] = report.minted
        assert minted_concept == cid
        assert store.get_ref(kind="anki", id=note).meta["minted_card_count"] == 2
        # The next run must count legacy notes that predate the metadata.
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta - 'minted_card_count' WHERE ref_id = %s",
                (note,),
            )
        assert not mint_daily_cards(store, client=client, per_day=2, cohort=co).minted
        assert len(client.calls) == 1

    def test_solo_account_backlog_pauses_then_ramps_intake(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.reading import flashcards
        from precis.users import hash_password

        store.create_web_user(
            login="forge-owner", abbrev="fg", password=hash_password("pw")
        )
        monkeypatch.setattr(flashcards, "anki_logins", lambda _store: ["forge-owner"])
        monkeypatch.setenv("PRECIS_READING_DUE_REVIEW_LIMIT", "10")
        review_note = author_card(
            store,
            text="Review {{c1::this}}.",
            concept_id=_concept(store),
            deck="Precis::other",
            owner_login="forge-owner",
        )
        co = f"mint-{uuid.uuid4().hex[:8]}"
        for _ in range(7):
            _concept(store, cohort=co)
        client = _FakeClient({"cards": [{"text": "{{c1::A}}."}]})

        store.update_ref(review_note, meta_patch={"anki_stats": {"due_reviews": 10}})
        paused = mint_daily_cards(store, client=client, per_day=5, cohort=co)
        assert paused.mint_throttled and not paused.minted and not client.calls

        store.update_ref(review_note, meta_patch={"anki_stats": {"due_reviews": 6}})
        partial = mint_daily_cards(store, client=client, per_day=5, cohort=co)
        assert partial.mint_throttled and len(partial.minted) == 2
        assert not mint_daily_cards(store, client=client, per_day=5, cohort=co).minted
        assert len(client.calls) == 2

        store.update_ref(review_note, meta_patch={"anki_stats": {"due_reviews": 0}})
        resumed = mint_daily_cards(store, client=client, per_day=7, cohort=co)
        assert not resumed.mint_throttled and len(resumed.minted) == 5

    def test_skips_concepts_that_already_have_cards(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, cohort=co)
        _card(store, cid)
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=50, cohort=co)
        assert cid not in [c for c, _ in report.minted]

    @pytest.mark.parametrize(
        "text", ["no cloze markup", "{{c1::RMSD::root-mean-square deviation}}"]
    )
    def test_invalid_model_output_is_skipped(self, store: Any, text: str) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        client = _FakeClient({"cards": [{"text": text}]})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted == [] and report.skipped == 1

    def test_self_check_failing_card_is_dropped_and_pass_is_stamped(
        self, store: Any
    ) -> None:
        # gr743086: the model returns a per-card recoverability verdict. A
        # failing card is not minted; a passing card carries the verdict in
        # meta so the mint stays auditable.
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        client = _FakeClient(
            {
                "cards": [
                    {"text": "A {{c1::b}}.", "check": "pass"},
                    {"text": "C {{c1::d}}.", "check": "fail: c1 has two answers"},
                ]
            }
        )
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert len(report.minted) == 1
        _cid, cards = report.minted[0]
        assert len(cards) == 1
        ref = store.get_ref(kind="anki", id=cards[0])
        assert ref.meta["recoverability_check"] == "pass"

    def test_all_cards_failing_self_check_counts_as_skipped(self, store: Any) -> None:
        co = f"mint-{uuid.uuid4().hex[:8]}"
        _concept(store, cohort=co)
        client = _FakeClient(
            {"cards": [{"text": "A {{c1::b}}.", "check": "fail: vague blank"}]}
        )
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted == [] and report.skipped == 1

    def test_refused_concept_is_stamped_and_not_retried(self, store: Any) -> None:
        # The gate: a model that judges a concept un-cardable returns empty
        # cards + a skip_reason. We record the refusal, stamp the concept, and
        # never offer it to the mint pass again (no daily re-spend).
        co = f"mint-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, cohort=co)
        client = _FakeClient({"cards": [], "skip_reason": "topic label, not a fact"})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted == [] and report.skipped == 0
        assert report.refused == [(cid, "topic label, not a fact")]
        meta = store.get_ref(kind="concept", id=cid).meta
        assert meta["card_forge_skip"] == "topic label, not a fact"
        # A re-run finds no cardless concept in the cohort — the stamped one is
        # excluded, so the model is never called again for it.
        again = _FakeClient({"cards": [{"text": "X is {{c1::Y}}."}]})
        report2 = mint_daily_cards(store, client=again, per_day=1, cohort=co)
        assert report2.minted == [] and again.calls == []

    def test_concept_retired_mid_mint_is_not_a_model_skip(self, store: Any) -> None:
        """gr742735: a concept that retires between the cardless scan and
        the write is not "the model gave no valid card" — it is dropped
        from the pass uncounted. The mint transaction row-locks the concept
        before it writes, so a retire can only land before that lock."""
        co = f"mint-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, cohort=co)

        class _RetiringClient(_FakeClient):
            def complete(self, messages: list[dict[str, str]]) -> Any:
                store.retire_ref(cid)
                return super().complete(messages)

        client = _RetiringClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert report.minted == [] and report.skipped == 0
        assert report.refused == []


class TestRework:
    def _stale_leech(self, store: Any, cid: int) -> int:
        card_id = _card(store, cid, stats=_LEECH)
        _age(store, card_id, 5)
        return card_id

    def test_float_min_age_days_runs_on_pg(self, store: Any) -> None:
        # Regression: prod's card_forge derives min_age_days via _env_float, so a
        # *float* (e.g. 4.0) reaches _stale_leeches. `make_interval(days => 4.0)`
        # has no PG overload (days is integer-only) → card_forge failed 100% with
        # "function make_interval(days => double precision) does not exist". The
        # int-defaulted tests never exercised the float path.
        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        found = _stale_leeches(store, min_age_days=4.0, now=datetime.now(UTC))
        assert card_id in {r[0] for r in found}

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
        assert new.meta["recoverability_check"] == "unchecked"
        assert store.get_ref(kind="concept", id=cid).meta["remunge_streak"] == 1

    def test_rewrite_with_acronym_hint_preserves_the_old_card(self, store: Any) -> None:
        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        client = _FakeClient({"text": "{{c1::RMSD::root-mean-square deviation}}"})
        report = rework_stale_cards(store, client=client, act=True)
        decision = next(d for d in report.decisions if d.card_id == card_id)
        assert decision.action == "rewrite" and not decision.applied
        assert store.get_ref(kind="anki", id=card_id) is not None
        assert store.get_ref(kind="concept", id=cid).meta.get("remunge_streak") is None

    def test_self_check_failure_leaves_card_in_place(self, store: Any) -> None:
        # gr743086: a replacement the model itself flags as unrecoverable is
        # not swapped in — the leech stays live for a future pass.
        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        client = _FakeClient(
            {"text": "New {{c1::angle}}.", "check": "fail: not recoverable"}
        )
        report = rework_stale_cards(store, client=client, act=True)
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "rewrite" and not d.applied
        assert "self-check failed" in d.reason
        assert store.get_ref(kind="anki", id=card_id) is not None  # kept

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

    @pytest.mark.parametrize("shared", [False, True])
    def test_concept_retired_mid_rework_reports_original_card_state(
        self, store: Any, monkeypatch: pytest.MonkeyPatch, shared: bool
    ) -> None:
        """A cascade retires a single-home leech and preserves a shared leech."""
        import precis.reading.flashcards as flashcards

        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        if shared:
            other = _concept(store)
            store.add_link(src_ref_id=other, dst_ref_id=card_id, relation="represents")
        real_author = flashcards.author_card

        def retire_then_author(st: Any, **kw: Any) -> int:
            st.retire_ref(cid)
            return real_author(st, **kw)

        monkeypatch.setattr(flashcards, "author_card", retire_then_author)
        report = rework_stale_cards(
            store, client=_FakeClient({"text": "A fresh {{c1::angle}}."}), act=True
        )
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.new_card_id is None
        assert d.applied is (not shared)
        assert d.action == ("rewrite" if shared else "retire")
        assert (
            "original card still live" if shared else "original card retired"
        ) in d.reason
        assert (store.get_ref(kind="anki", id=card_id) is not None) is shared

    @pytest.mark.parametrize("shared", [False, True])
    def test_concept_retired_after_rewrite_write_reports_the_retire(
        self, store: Any, monkeypatch: pytest.MonkeyPatch, shared: bool
    ) -> None:
        """gr742735: the concept can retire after author_card commits —
        the cascade retires the rewrite and, unless another live concept
        homes it, the leech. The follow-up then sees no live concept and
        leaves the original alone: a shared leech survives on its other
        home rather than being retired after the cascade spared it."""
        import precis.reading.flashcards as flashcards

        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        if shared:
            other = _concept(store)
            store.add_link(src_ref_id=other, dst_ref_id=card_id, relation="represents")
        real_author = flashcards.author_card

        def author_then_retire(st: Any, **kw: Any) -> int:
            new_id = real_author(st, **kw)
            st.retire_ref(cid)
            return new_id

        monkeypatch.setattr(flashcards, "author_card", author_then_retire)
        report = rework_stale_cards(
            store, client=_FakeClient({"text": "A fresh {{c1::angle}}."}), act=True
        )
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.new_card_id is None
        assert d.applied is (not shared)
        assert d.action == ("rewrite" if shared else "retire")
        assert (
            "original card still live" if shared else "original card retired"
        ) in d.reason
        assert (store.get_ref(kind="anki", id=card_id) is not None) is shared

    def test_original_dropped_mid_rework_still_records_the_rewrite(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr742735: an independent drop (not a concept retire) can take
        the leech after author_card commits while its concept stays
        live. That is not concept retirement: the streak still stamps
        and the live replacement still lands in the audit."""
        import precis.reading.flashcards as flashcards

        cid = _concept(store)
        card_id = self._stale_leech(store, cid)
        real_author = flashcards.author_card

        def author_then_drop(st: Any, **kw: Any) -> int:
            new_id = real_author(st, **kw)
            st.retire_ref(card_id)
            return new_id

        monkeypatch.setattr(flashcards, "author_card", author_then_drop)
        report = rework_stale_cards(
            store, client=_FakeClient({"text": "A fresh {{c1::angle}}."}), act=True
        )
        d = next(d for d in report.decisions if d.card_id == card_id)
        assert d.action == "rewrite" and d.applied and d.new_card_id
        assert "original already retired" in d.reason
        assert store.get_ref(kind="anki", id=d.new_card_id) is not None
        assert store.get_ref(kind="concept", id=cid).meta["remunge_streak"] == 1


class TestReportLines:
    def test_lines_read_as_audit_log(self) -> None:
        from precis.reading.flashcards import CardDecision

        r = ForgeReport(
            minted=[(5, [9])],
            decisions=[
                CardDecision(9, 5, "rewrite", "why", applied=True, new_card_id=10)
            ],
            skipped=1,
        )
        text = "\n".join(r.lines())
        assert "minted 1 note(s) for concept cn5" in text
        assert "did rewrite ak9" in text and "→ ak10" in text
        assert "skipped 1" in text


def _flat(text: str) -> str:
    """Whitespace-collapsed, so a pinned phrase matches across a line wrap."""
    return " ".join(text.split())


class TestSymbolGlossary:
    """gr745846: every symbol, variable and acronym on a card is defined in
    Back Extra as ``symbol = meaning (units)``, Greek letters with their spoken
    name, one symbol per line. Pinned in both authoring prompts and in the
    ``precis-cloze`` skill the forge prepends to them."""

    #: The rule's wording, matched case-insensitively.
    _RULE = (
        "symbol, variable and acronym",
        "symbol = meaning (units)",
        "spoken name",
        "one symbol per line",
    )
    #: The Back Extra shape: an HTML list, a rule, then context. Exact case —
    #: the spoken name is capitalised to match the letter.
    _LAYOUT = ("<ul><li>Γ (Gamma) = hop rate (1/s)</li>", "</li></ul><hr>")

    def _assert_states(self, text: str, needles: tuple[str, ...], where: str) -> None:
        flat = _flat(text).casefold()
        missing = [n for n in needles if n.casefold() not in flat]
        assert not missing, f"{where} does not say {missing}"

    def _skill_section(self, heading: str) -> str:
        """One section of the skill the forge prepends: from ``heading`` to the
        next heading of the same or a higher level."""
        body = cloze_skill_preamble()
        assert heading in body, f"precis-cloze has no section {heading!r}"
        rest = body[body.index(heading) + len(heading) :]
        level = len(heading.split(" ", 1)[0])
        end = re.search(rf"^#{{2,{level}}} ", rest, re.MULTILINE)
        return rest[: end.start()] if end else rest

    @pytest.mark.parametrize(
        ("name", "prompt"),
        [("mint", _MINT_SYS), ("rework", _REWORK_SYS)],
        ids=["mint", "rework"],
    )
    def test_prompt_asks_for_the_glossary(self, name: str, prompt: str) -> None:
        self._assert_states(prompt, self._RULE, f"the {name} prompt")
        for needle in self._LAYOUT:
            assert needle in prompt, f"the {name} prompt lacks the layout {needle!r}"

    def test_mint_prompt_no_longer_makes_back_extra_optional(self) -> None:
        assert "Optionally add a terse Back Extra" not in _MINT_SYS

    @pytest.mark.parametrize(
        "heading",
        [
            "### 7. Define every symbol in Back Extra",
            "## What goes in Back Extra",
            "### Before you save: check each cN in turn",
        ],
    )
    def test_skill_states_the_rule(self, heading: str) -> None:
        self._assert_states(
            self._skill_section(heading), self._RULE, f"precis-cloze {heading!r}"
        )

    def test_skill_back_extra_section_shows_the_html_layout(self) -> None:
        section = self._skill_section("## What goes in Back Extra")
        for needle in self._LAYOUT:
            assert needle in section, f"Back Extra section lacks {needle!r}"
