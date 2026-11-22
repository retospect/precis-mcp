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
    unqueue_asked_term,
)
from precis.reading.flashcards import (
    _cardless_concepts,
    author_card,
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

    def test_source_and_asked_mint_stamp_on_a_fresh_concept(self, store: Any) -> None:
        """The highlight queue's provenance: meta.source names the capture
        door and meta.asked_mint marks the mint as the queue's own, so
        Undo can retire it (gr743234)."""
        term = f"term-{uuid.uuid4().hex[:8]}"
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        out = queue_asked_term(
            store,
            term,
            owner_login="reto",
            definition="highlighted text",
            source="highlight",
        )
        ref = store.get_ref(kind="concept", id=out["ref_id"])
        assert ref.meta["source"] == "highlight"
        assert ref.meta["asked_mint"] is True

    def test_every_queue_files_an_undoable_submission(self, store: Any) -> None:
        """``out["submission"]`` is the undo handle: the ledger entry
        records who asked, through which door, with what context and
        from which page (gr743234)."""
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        page = store.insert_ref(
            kind="conv", slug=f"page/{uuid.uuid4().hex[:8]}", title="p", meta={}
        )
        term = f"term-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(
            store,
            term,
            owner_login="reto",
            conv_ref_id=page.id,
            definition="the highlighted sentence",
            source="highlight",
        )
        ref = store.get_ref(kind="concept", id=out["ref_id"])
        entry = ref.meta["asked_submissions"][out["submission"]]
        assert entry["owner"] == "reto"
        assert entry["source"] == "highlight"
        assert entry["ref"] == page.id
        assert "the highlighted sentence" not in json.dumps(ref.meta)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT context FROM anki_highlight_context "
                "WHERE concept_id = %s AND owner_login = %s AND submission = %s",
                (ref.id, "reto", out["submission"]),
            ).fetchone()
            linked = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'related-to'",
                (ref.id, page.id),
            ).fetchone()
        assert row == ("the highlighted sentence",)
        assert linked is not None

    def test_a_capture_on_an_existing_node_keeps_its_context(self, store: Any) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        name = f"ctx-{uuid.uuid4().hex[:8]}"
        shared_id = _concept(store, name)
        out = queue_asked_term(
            store,
            name,
            owner_login="reto",
            definition="what the page said about it",
            source="highlight",
        )
        assert out["created"] is True
        assert out["ref_id"] != shared_id
        assert store.get_ref(kind="concept", id=out["ref_id"]).owner_login == "reto"
        shared = store.get_ref(kind="concept", id=shared_id)
        assert "asked_submissions" not in shared.meta
        assert shared.meta["definition"] == "a seeded test idea"

    def test_a_self_link_is_skipped_not_fatal(self, store: Any) -> None:
        """Highlighting a concept's own name on its own page must not
        self-link: the links CHECK would roll the whole queue write
        back (gr743234)."""
        name = f"self-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        out = queue_asked_term(store, name, conv_ref_id=cid)
        assert out["created"] is False
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["asked_count"] == 1
        assert ASKED_TAG in _tags(store, cid)
        with store.pool.connection() as conn:
            link = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (cid, cid),
            ).fetchone()
        assert link is None

    def test_the_related_link_lands_on_an_existing_node_too(self, store: Any) -> None:
        """A highlight queue that matches a pre-existing concept still
        records which page it came from — the related-to edge is not a
        mint-only privilege."""
        name = f"linked-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        page = store.insert_ref(
            kind="conv", slug=f"page/{uuid.uuid4().hex[:8]}", title="p", meta={}
        )
        out = queue_asked_term(store, name, conv_ref_id=page.id)
        assert out["created"] is False
        with store.pool.connection() as conn:
            link = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (cid, page.id),
            ).fetchone()
        assert link is not None

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


class TestForgeRefusesWorkTracking:
    def test_gap_concept_refused_without_model_call(self, store: Any) -> None:
        co = f"wt-{uuid.uuid4().hex[:8]}"
        cid = create_concept(
            store,
            name="Catpath gap C1",
            definition="our spec has no observables",
            cohort=co,
        )
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = run_card_forge(store, client=client, cohort=co, act=False)
        assert report.minted == []
        assert [c for c, _ in report.refused] == [cid]
        assert client.calls == []

    def test_generated_gap_card_is_dropped(self, store: Any) -> None:
        co = f"wt-{uuid.uuid4().hex[:8]}"
        create_concept(store, name=f"c-{uuid.uuid4().hex[:8]}", cohort=co)
        client = _FakeClient(
            {"cards": [{"text": "Catpath gap C1: we lack {{c1::observables}}."}]}
        )
        report = run_card_forge(store, client=client, cohort=co, act=False)
        assert report.minted == []

    def test_mint_prompt_names_work_tracking(self) -> None:
        from precis.reading.flashcards import _MINT_SYS

        assert "to-do or gap about OUR OWN system" in _MINT_SYS


def _open_tag() -> Any:
    from precis.store import Tag

    return Tag.open(ASKED_TAG)


class TestUnqueueAskedTerm:
    def test_retires_a_concept_the_queue_minted_for_this_owner(
        self, store: Any
    ) -> None:
        """Undo on a highlight queue's own mint (asked_mint + matching
        owner + a single ask) retires the concept outright (gr743234)."""
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        term = f"term-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(store, term, owner_login="reto", source="highlight")
        assert unqueue_asked_term(
            store,
            out["ref_id"],
            submission=out["submission"],
            owner_login="reto",
        ) == {"undone": True, "retired": True}
        assert store.get_ref(kind="concept", id=out["ref_id"]) is None
        assert unqueue_asked_term(
            store, out["ref_id"], submission=out["submission"], owner_login="reto"
        ) == {"undone": False}

    def test_another_login_cannot_clear_the_owners_ask(self, store: Any) -> None:
        """Undo binds the token to its recorded owner: a different login
        holding it — or guessing the ref — undoes nothing; the owner's
        ask stands untouched (gr743234 review)."""
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        term = f"term-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(store, term, owner_login="reto", source="highlight")
        res = unqueue_asked_term(
            store,
            out["ref_id"],
            submission=out["submission"],
            owner_login="alice",
        )
        assert res == {"undone": False}
        ref = store.get_ref(kind="concept", id=out["ref_id"])
        assert ref is not None
        assert ref.meta["asked_count"] == 1
        assert out["submission"] in ref.meta["asked_submissions"]
        assert ASKED_TAG in _tags(store, ref.id)

    def test_a_consumed_token_cannot_eat_another_ask(self, store: Any) -> None:
        """One submission, one reversal: replaying the same token after a
        successful Undo cannot consume a second, independent ask — the
        bug a ref-id-only Undo had (gr743234 review)."""
        name = f"replay-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        first = queue_asked_term(store, name, owner_login="reto")
        second = queue_asked_term(store, name, owner_login="reto")
        assert first["submission"] != second["submission"]
        res = unqueue_asked_term(
            store, cid, submission=first["submission"], owner_login="reto"
        )
        assert res == {"undone": True, "retired": False, "asked_count": 1}
        # The replay undoes nothing — the second ask survives intact.
        assert unqueue_asked_term(
            store, cid, submission=first["submission"], owner_login="reto"
        ) == {"undone": False}
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["asked_count"] == 1
        assert ASKED_TAG in _tags(store, cid)

    def test_decrements_and_lifts_the_tag_at_zero(self, store: Any) -> None:
        """Undo on a pre-existing concept the queue merely tagged drops
        the ask count; at zero the twin:asked tag lifts but the node
        stays live."""
        name = f"undo-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        out = queue_asked_term(store, name, owner_login="reto")
        res = unqueue_asked_term(
            store, cid, submission=out["submission"], owner_login="reto"
        )
        assert res == {"undone": True, "retired": False, "asked_count": 0}
        assert ASKED_TAG not in _tags(store, cid)
        meta = store.get_ref(kind="concept", id=cid).meta
        assert meta["asked_count"] == 0
        assert meta["asked_submissions"] == {}

    def test_keeps_the_tag_while_an_ask_remains(self, store: Any) -> None:
        """One Undo undoes one ask — a concept asked twice keeps the tag."""
        name = f"undo2-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        queue_asked_term(store, name, owner_login="reto")
        out = queue_asked_term(store, name, owner_login="reto")
        res = unqueue_asked_term(
            store, cid, submission=out["submission"], owner_login="reto"
        )
        assert res == {"undone": True, "retired": False, "asked_count": 1}
        assert ASKED_TAG in _tags(store, cid)

    def test_a_wrong_token_is_not_an_undo(self, store: Any) -> None:
        """Ref id plus a token nobody issued undoes nothing."""
        name = f"tok-{uuid.uuid4().hex[:8]}"
        cid = _concept(store, name)
        queue_asked_term(store, name, owner_login="reto")
        res = unqueue_asked_term(store, cid, submission="0" * 32, owner_login="reto")
        assert res == {"undone": False}
        assert store.get_ref(kind="concept", id=cid).meta["asked_count"] == 1

    def test_a_manual_mint_survives_a_later_highlight_undo(self, store: Any) -> None:
        """The manual queue's mint is out of Undo reach: a highlight ask
        on the same term can undo itself (count drops, tag survives on
        the manual ask) but never retires a mint it didn't create
        (gr743234 review)."""
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        term = f"term-{uuid.uuid4().hex[:8]}"
        mint = queue_asked_term(store, term, owner_login="reto")  # manual door
        assert mint["created"] is True
        cid = mint["ref_id"]
        hl = queue_asked_term(store, term, owner_login="reto", source="highlight")
        res = unqueue_asked_term(
            store, cid, submission=hl["submission"], owner_login="reto"
        )
        assert res == {"undone": True, "retired": False, "asked_count": 1}
        ref = store.get_ref(kind="concept", id=cid)
        assert ref is not None
        assert ASKED_TAG in _tags(store, cid)
        # The manual submission's token is still the only live one — and
        # it is unissued, so nothing on the page can retire this mint.
        assert list(ref.meta["asked_submissions"]) == [mint["submission"]]

    def test_none_for_a_missing_or_wrong_kind(self, store: Any) -> None:
        conv = store.insert_ref(
            kind="conv", slug=f"c/{uuid.uuid4().hex[:8]}", title="c", meta={}
        )
        assert (
            unqueue_asked_term(store, conv.id, submission="x", owner_login="reto")
            is None
        )
        assert (
            unqueue_asked_term(store, 999999999, submission="x", owner_login="reto")
            is None
        )


class TestForgeCardOwnership:
    """gr743234 review: the forge must carry the requesting login onto
    the minted card — cards are per-login rows, so an ownerless card is
    outside that login's review list and sync queries."""

    def test_existing_card_does_not_block_forge_or_undo(self, store: Any) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        term = f"recapture-{uuid.uuid4().hex}"
        original = queue_asked_term(store, term, owner_login="reto")
        cid = original["ref_id"]
        store.update_ref(cid, meta_patch={"cohorts": [term]})
        existing = author_card(
            store,
            concept_id=cid,
            text="An {{c1::existing card}}.",
            deck="Older",
            owner_login="reto",
        )
        page = store.insert_ref(kind="conv", slug=term, title="Source page")

        def capture() -> dict[str, Any]:
            return queue_asked_term(
                store,
                term,
                owner_login="reto",
                conv_ref_id=page.id,
                definition="new context",
                source="highlight",
            )

        def undo(out: dict[str, Any]) -> dict[str, Any] | None:
            return unqueue_asked_term(
                store, cid, submission=out["submission"], owner_login="reto"
            )

        def linked() -> bool:
            with store.pool.connection() as conn:
                return (
                    conn.execute(
                        "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                        "AND relation = 'related-to'",
                        (cid, page.id),
                    ).fetchone()
                    is not None
                )

        first, second = capture(), capture()
        assert undo(first) == {"undone": True, "retired": False, "asked_count": 2}
        assert linked()
        assert undo(second) == {"undone": True, "retired": False, "asked_count": 1}
        assert not linked()
        store.add_link(src_ref_id=cid, dst_ref_id=page.id, relation="related-to")
        assert undo(capture()) == {"undone": True, "retired": False, "asked_count": 1}
        assert linked()

        pending, published = capture(), capture()
        client = _FakeClient({"cards": [{"text": "A {{c1::new card}}."}]})
        report = mint_daily_cards(store, client=client, per_day=2, cohort=term)
        assert [concept for concept, _ in report.minted] == [cid]
        assert "HIGHLIGHT CONTEXT: new context" in client.calls[0][-1]["content"]
        assert undo(published) == {"undone": False}
        assert undo(pending) == {"undone": True, "retired": False, "asked_count": 2}
        assert linked()
        assert store.get_ref(kind="anki", id=existing) is not None

        queued, refused = capture(), capture()
        declining = _FakeClient({"skip_reason": "not suitable"})
        report = mint_daily_cards(store, client=declining, per_day=3, cohort=term)
        assert report.refused == [(cid, "the forge declined the captured term")]
        report = mint_daily_cards(store, client=client, per_day=3, cohort=term)
        assert [concept for concept, _ in report.minted] == [cid]
        assert undo(queued) == {"undone": False}
        assert undo(refused) == {"undone": True, "retired": False, "asked_count": 3}

    def test_the_card_belongs_to_the_queue_mints_owner(self, store: Any) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        co = f"own-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(
            store, f"term-{uuid.uuid4().hex[:8]}", owner_login="reto"
        )
        store.update_ref(out["ref_id"], meta_patch={"cohorts": [co]})
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=5, cohort=co)
        card_id = report.minted[0][1][0]
        assert store.get_ref(kind="anki", id=card_id).owner_login == "reto"

    @pytest.mark.parametrize("kind", ["concept", "taxon"])
    def test_each_requester_gets_their_own_context_and_card(
        self, store: Any, kind: str
    ) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
        co = f"scoped-{uuid.uuid4().hex[:8]}"
        name = f"shared-{uuid.uuid4().hex[:8]}"
        shared = store.insert_ref(
            kind=kind,
            slug=None,
            title=name,
            meta={"name": name, "cohorts": [co], "state": "mastered"},
        )
        reto = queue_asked_term(
            store,
            name,
            owner_login="reto",
            definition="context for Reto",
            source="highlight",
        )
        alice = queue_asked_term(
            store,
            name,
            owner_login="alice",
            definition="context for Alice",
            source="highlight",
        )
        assert len({shared.id, reto["ref_id"], alice["ref_id"]}) == 3
        # Even a legacy link from the queue's concept to another login's card
        # must leave the requester's own candidate eligible.
        author_card(
            store,
            concept_id=reto["ref_id"],
            text="The {{c1::older card}}.",
            deck="Older",
            owner_login="alice",
        )
        client = _FakeClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=3, cohort=co)
        for (concept_id, cards), call in zip(report.minted, client.calls, strict=True):
            owner = "reto" if concept_id == reto["ref_id"] else "alice"
            assert (
                "HIGHLIGHT CONTEXT: context for " + owner.title() in call[-1]["content"]
            )
            assert (
                "context for Alice" if owner == "reto" else "context for Reto"
            ) not in call[-1]["content"]
            assert store.get_ref(kind="anki", id=cards[0]).owner_login == owner
        assert {cid for cid, _ in report.minted} == {reto["ref_id"], alice["ref_id"]}
        assert unqueue_asked_term(
            store, reto["ref_id"], submission=reto["submission"], owner_login="reto"
        ) == {"undone": False}
        assert store.get_ref(kind="concept", id=reto["ref_id"]) is not None

    def test_undo_during_authoring_prevents_publish(self, store: Any) -> None:
        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        co = f"undo-race-{uuid.uuid4().hex[:8]}"
        out = queue_asked_term(
            store,
            co,
            owner_login="reto",
            definition="context to withdraw",
            source="highlight",
        )
        store.update_ref(out["ref_id"], meta_patch={"cohorts": [co]})

        class UndoingClient(_FakeClient):
            def complete(self, messages: list[dict[str, str]]) -> Any:
                assert unqueue_asked_term(
                    store,
                    out["ref_id"],
                    submission=out["submission"],
                    owner_login="reto",
                ) == {"undone": True, "retired": True}
                return super().complete(messages)

        client = UndoingClient({"cards": [{"text": "A {{c1::b}}."}]})
        report = mint_daily_cards(store, client=client, per_day=1, cohort=co)
        assert client.calls
        assert report.minted == []
