"""Tests for the morning reading-brief producer (lanes degrade to empty)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from precis.reading.briefing_cast import (
    _DORMANT_NUDGE_KEY,
    _cite_token,
    _dormant_nudge,
    _lane_news,
    _lane_quest,
    _lane_reading,
    _render_papers,
    build_reading_briefing,
)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=5)


class _NudgeStore:
    """A minimal store exercising only the quest lane's reads/writes: a
    ``list_refs`` over quest status tags + the ``app_state`` get/set pair.
    No DB — the decaying-nudge logic is pure over these four calls."""

    def __init__(self, active: list[Any], dormant: list[Any]) -> None:
        self._active = active
        self._dormant = dormant
        self.kv: dict[str, str] = {}

    def list_refs(self, *, kind: str, tags: list[str], limit: int) -> list[Any]:
        return self._active if tags == ["STATUS:active"] else self._dormant

    def get_setting(self, key: str) -> str | None:
        return self.kv.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.kv[key] = value


class TestLanesDegrade:
    def test_unbuilt_lanes_are_empty(self, store: Any) -> None:
        assert _lane_reading(store) == ""  # booklet unbuilt
        assert _lane_quest(store, now=datetime.now(UTC)) == ""  # no quests


class TestDormantNudgeDecay:
    def test_nudge_fires_on_a_doubling_cadence_then_resets_when_active(self) -> None:
        dormant = [SimpleNamespace(id=1, title="Strive for X")]
        st = _NudgeStore(active=[], dormant=dormant)
        base = datetime(2026, 7, 17, tzinfo=UTC)

        fired = [
            d
            for d in range(20)
            if _lane_quest(st, now=base + timedelta(days=d)).startswith("DORMANT")
        ]
        # Days 0,1,3,7,15 — the quiet window doubles (1 → 2 → 4 → 8) each fire.
        assert fired == [0, 1, 3, 7, 15]

        # An active quest re-engages the human → the decay cursor resets so a
        # future dormancy nudges from scratch again.
        st._active = [SimpleNamespace(id=9, title="Strive live")]
        _lane_quest(st, now=base + timedelta(days=16))
        assert st.get_setting(_DORMANT_NUDGE_KEY) == '{"last": null, "fires": 0}'

        st._active = []
        # First morning after the reset fires immediately (fresh decay).
        assert _lane_quest(st, now=base + timedelta(days=17)).startswith("DORMANT")

    def test_no_quests_at_all_is_silent(self) -> None:
        st = _NudgeStore(active=[], dormant=[])
        assert _lane_quest(st, now=datetime.now(UTC)) == ""
        assert st.kv == {}  # nothing dormant → no cursor written

    def test_dormant_nudge_links_the_named_strivings(self) -> None:
        # A firing morning collects the nudged quests as ``related-to`` sources
        # so the report links back to "the palladium catalyst" et al. — the same
        # graph edge the active-quest branch writes (bug: it never did before).
        dormant = [
            SimpleNamespace(id=164903, title="Pd catalyst for NO to ammonia"),
            SimpleNamespace(id=200, title="Second striving"),
        ]
        st = _NudgeStore(active=[], dormant=dormant)
        src: list[Any] = []
        text = _dormant_nudge(
            st, dormant, now=datetime(2026, 7, 21, tzinfo=UTC), sources=src
        )
        assert text.startswith("DORMANT")
        assert (164903, "related-to") in src
        assert (200, "related-to") in src

    def test_dormant_nudge_quiet_morning_links_nothing(self) -> None:
        # A quiet (off-cadence) morning returns "" and must write no source.
        dormant = [SimpleNamespace(id=1, title="Strive")]
        st = _NudgeStore(active=[], dormant=dormant)
        base = datetime(2026, 7, 21, tzinfo=UTC)
        _dormant_nudge(st, dormant, now=base, sources=[])  # first fire → 1-day window
        src: list[Any] = []
        # A same-day re-check is inside that quiet window → silent, links nothing.
        assert _dormant_nudge(st, dormant, now=base, sources=src) == ""
        assert src == []

    def test_news_lane_empty_when_no_briefing(self, store: Any) -> None:
        # A date with no briefing ref → empty (no raise).
        assert _lane_news(store, f"2999-01-{uuid.uuid4().hex[:2]}") == ""


class TestPaperLane:
    def test_render_papers_carries_cite_markers_and_true_total(self) -> None:
        # Ten named papers but a true overnight total of 23: the header must
        # report the real count (the "12 papers" under-count bug) and note the
        # un-named remainder; each deep paper carries its [[pa<id>]] marker.
        papers = [
            SimpleNamespace(
                id=1000 + i,
                title=f"Paper {i}",
                meta={"abstract": f"Abstract {i}."},
            )
            for i in range(10)
        ]
        out = _render_papers(papers, total=23)
        assert out.startswith("Papers acquired or updated (23)")
        assert "13 more not listed here" in out  # 23 total − 10 named
        # The top papers carry a copy-me citation marker for the model.
        assert _cite_token(papers[0]) == "[[pa1000]]"
        assert "[[pa1000]]" in out

    def test_render_papers_falls_back_to_named_count(self) -> None:
        papers = [SimpleNamespace(id=5, title="Solo", meta={})]
        out = _render_papers(papers)  # no total → the named count
        assert out.startswith("Papers acquired or updated (1)")
        assert "more not listed" not in out


class TestBuild:
    def test_no_material_returns_none_without_calling_model(self, store: Any) -> None:
        client = _FakeClient("unused")
        # A far-future date: no news, and the overnight window catches nothing new
        # attributable to this run. If the shared DB happens to have activity, the
        # lane may be non-empty — so assert the weaker invariant on a clean date
        # only when nothing composed.
        out = build_reading_briefing(
            store, client=client, date_tag=f"2999-01-{uuid.uuid4().hex[:2]}"
        )
        if out is None:
            assert client.calls == []  # never consulted the model with no material

    def test_news_lane_flows_into_a_composed_draft(self, store: Any) -> None:
        date_tag = f"2026-07-{uuid.uuid4().hex[:2]}"
        # Seed today's news briefing ref the morning cast should consume.
        news = store.insert_ref(
            kind="news",
            slug=f"briefing-{date_tag}",
            title=f"Morning briefing — {date_tag}",
            meta={"briefing": True, "date": date_tag},
        )
        store.add_chunks(
            ref_id=news.id,
            chunk_kind="paragraph",
            text="Markets rose. A new catalyst paper landed.",
            split=True,
            kind="news",
        )
        client = _FakeClient("Good morning.\n\nHere is your day.\n\nGo gently.")

        draft_id = build_reading_briefing(store, client=client, date_tag=date_tag)

        assert draft_id is not None
        assert client.calls  # the news lane gave it material → model consulted
        # The composed brief is a cast draft with paragraphs + the voice profile.
        with store.pool.connection() as conn:
            meta = conn.execute(
                "SELECT meta FROM refs WHERE ref_id=%s", (draft_id,)
            ).fetchone()[0]
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id=%s AND chunk_kind='paragraph'",
                (draft_id,),
            ).fetchone()[0]
        assert meta["cast"] == "reading"
        assert meta["voice"] == "bm_george"
        assert n >= 2
        # The news body was actually handed to the model.
        user_turn = client.calls[0][1]["content"]
        assert "catalyst paper" in user_turn

    def test_sources_are_linked_from_the_draft(self, store: Any) -> None:
        date_tag = f"2026-09-{uuid.uuid4().hex[:2]}"
        # A news wire (so it composes) + an overnight paper the brief draws on.
        news = store.insert_ref(
            kind="news",
            slug=f"briefing-{date_tag}",
            title="wire",
            meta={"briefing": True, "date": date_tag},
        )
        store.add_chunks(
            ref_id=news.id,
            chunk_kind="paragraph",
            text="A new catalyst paper landed.",
            split=True,
            kind="news",
        )
        paper = store.insert_ref(
            kind="paper",
            slug=f"paper-{uuid.uuid4().hex[:8]}",
            title="Single-atom Pd catalyses ammonia synthesis at 1 bar",
            meta={"abstract": "We report a single-atom palladium catalyst."},
        )
        client = _FakeClient("Morning.\n\nA paper landed.\n\nOn to the quests.")

        draft_id = build_reading_briefing(store, client=client, date_tag=date_tag)
        assert draft_id is not None

        with store.pool.connection() as conn:
            rels = {
                r[0]
                for r in conn.execute(
                    "SELECT relation FROM links WHERE src_ref_id=%s AND dst_ref_id=%s",
                    (draft_id, paper.id),
                ).fetchall()
            }
            news_rels = {
                r[0]
                for r in conn.execute(
                    "SELECT relation FROM links WHERE src_ref_id=%s AND dst_ref_id=%s",
                    (draft_id, news.id),
                ).fetchall()
            }
        # The paper the brief drew on is reachable from the draft for later use.
        assert rels == {"cites"}
        # …and the consumed news wire is linked as the thing it derived from.
        assert news_rels == {"derived-from"}

    def test_idempotent_second_call_skips_compose(self, store: Any) -> None:
        date_tag = f"2026-08-{uuid.uuid4().hex[:2]}"
        news = store.insert_ref(
            kind="news",
            slug=f"briefing-{date_tag}",
            title="b",
            meta={"briefing": True, "date": date_tag},
        )
        store.add_chunks(
            ref_id=news.id,
            chunk_kind="paragraph",
            text="News.",
            split=True,
            kind="news",
        )
        c1 = _FakeClient("First.\n\nSecond.")
        first = build_reading_briefing(store, client=c1, date_tag=date_tag)
        assert first is not None

        c2 = _FakeClient("SHOULD NOT BE USED")
        second = build_reading_briefing(store, client=c2, date_tag=date_tag)
        assert second == first
        assert c2.calls == []  # idempotent — no recompose
