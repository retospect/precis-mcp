"""The scheduled paper-metadata enrichment pass — claim + cadence + retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.identity import PLACEHOLDER_TITLE
from precis.ingest import paper_meta_enrich
from precis.store import Store
from precis.store.types import Tag
from precis.workers.paper_meta_enrich import (
    _DEFAULT_BATCH_LIMIT,
    _DEFAULT_REFRESH_HOURS,
    _STATE_KEY,
    run_paper_meta_enrich_pass,
)


def _paper(
    store: Store,
    *,
    slug: str,
    doi: str | None = None,
    title: str = "X",
    meta: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta=meta or {})
    if doi:
        store.set_ref_identifier(ref.id, "doi", doi, source="manual")
    return ref.id


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids([ref_id])[ref_id]
    return ref.meta or {}


def _chunk(
    store: Store, ref_id: int, ord_: int, text: str, kind: str = "paragraph"
) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, %s, %s)",
                (ref_id, ord_, kind, text),
            )


class TestDefaults:
    def test_default_claim_is_400_per_hour(self) -> None:
        """precis.utils.authors module docstring — the 33.8K backlog
        drains in ~4 days at 400 refs/1 h (env overrides unchanged)."""
        assert _DEFAULT_BATCH_LIMIT == 400
        assert _DEFAULT_REFRESH_HOURS == 1.0


class TestBatchAndThrottle:
    def test_visits_and_stamps_doi_less_and_doi_papers(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        rid_no_doi = _paper(store, slug="w1")
        rid_doi = _paper(store, slug="w2", doi="10.1/w2")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )

        result = run_paper_meta_enrich_pass(store)

        assert result.claimed == 2
        assert result.ok == 2
        assert result.failed == 0
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_no_doi)
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_doi)

    def test_second_immediate_run_is_a_noop(self, store: Store) -> None:
        _paper(store, slug="w3")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        r1 = run_paper_meta_enrich_pass(store)
        assert r1.claimed == 1

        # Force the throttle open again — the claim predicate now selects
        # zero rows regardless, because the visited ref got stamped.
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        r2 = run_paper_meta_enrich_pass(store)
        assert r2.claimed == 0
        assert r2.ok == 0
        assert r2.failed == 0

    def test_throttled_second_call_without_marker_reset_is_idle(
        self, store: Store
    ) -> None:
        _paper(store, slug="w4")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        r1 = run_paper_meta_enrich_pass(store)
        assert r1.claimed == 1

        r2 = run_paper_meta_enrich_pass(store)  # not due yet
        assert r2.claimed == 0

    def test_junk_verdict_paper_is_not_claimed(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """gr520915: a JUNK-verdict paper gets no metadata sweep — the
        claim query drops it like the stub-chase paths do."""
        junk = _paper(store, slug="junkenrich")
        keep = _paper(store, slug="keepenrich")
        store.add_tag(junk, Tag.closed("JUNK", "junk"))
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        result = run_paper_meta_enrich_pass(store)

        assert result.claimed == 1
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, keep)
        assert paper_meta_enrich.RESOLVED_AT_KEY not in _meta(store, junk)

    def test_expired_junk_verdict_is_still_claimed(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """A lapsed JUNK verdict doesn't exclude — the claim leg mirrors
        ``build_tag_filter``'s TTL clause."""
        lapsed = _paper(store, slug="junklapsed")
        store.add_tag(
            lapsed,
            Tag.closed("JUNK", "junk"),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        result = run_paper_meta_enrich_pass(store)

        assert result.claimed == 1
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, lapsed)


class TestFailureIsolation:
    def test_one_ref_raising_does_not_sink_the_batch(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        rid_bad = _paper(store, slug="w5", doi="10.1/bad")
        rid_ok = _paper(store, slug="w6")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        real_enrich = paper_meta_enrich.enrich_paper

        def _flaky(store_: Store, ref_id: int, **kw: Any) -> Any:
            if ref_id == rid_bad:
                raise RuntimeError("boom")
            return real_enrich(store_, ref_id, **kw)

        monkeypatch.setattr(paper_meta_enrich, "enrich_paper", _flaky)

        result = run_paper_meta_enrich_pass(store)
        assert result.claimed == 2
        assert result.failed == 1
        assert result.ok == 1
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid_ok)
        assert paper_meta_enrich.RESOLVED_AT_KEY not in _meta(store, rid_bad)


class TestSharedLimiterRefusal:
    def test_crossref_refusal_stops_the_batch(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        from precis.utils.rate_limit import RateLimited

        rid_a = _paper(store, slug="rl-a", doi="10.1/rl-a")
        rid_b = _paper(store, slug="rl-b", doi="10.1/rl-b")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        attempted: list[int] = []

        def _refused(store_: Store, ref_id: int, **kw: Any) -> Any:
            attempted.append(ref_id)
            raise RateLimited("crossref")

        monkeypatch.setattr(paper_meta_enrich, "enrich_paper", _refused)

        result = run_paper_meta_enrich_pass(store)

        assert len(attempted) == 1  # second ref never attempted
        assert result.ok == 0
        for rid in (rid_a, rid_b):
            assert paper_meta_enrich.RESOLVED_AT_KEY not in _meta(store, rid)


class TestPlaceholderTitleRecovery:
    """gr658887: a paper stamped ``authors_resolved_at`` before the pass
    learned the front-matter title leg re-enters the claim exactly once —
    the ``title_recovery_v`` pin written on that visit bounds the retry so
    a still-blank dead end never spins."""

    def _stamped_placeholder(
        self, store: Store, *, slug: str, doi: str | None, **meta: Any
    ) -> int:
        return _paper(
            store,
            slug=slug,
            doi=doi,
            title=PLACEHOLDER_TITLE,
            meta={"authors_resolved_at": "2026-09-01T00:00:00+00:00", **meta},
        )

    def _due_pass(self, store: Store) -> Any:
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")
        return run_paper_meta_enrich_pass(store)

    def test_stamped_placeholder_is_reclaimed_once_and_filled(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """The class the cascade delegates to enrich: stamped, DOI'd,
        still at the sentinel — the Crossref miss falls through to the
        front-matter leg."""
        rid = self._stamped_placeholder(store, slug="rec1", doi="10.1/gone")
        _chunk(
            store,
            rid,
            0,
            "**Varieties of Helmholtz Machine**\n\nG. E. Hinton & P. Dayan",
        )
        _chunk(store, rid, 1, "The Helmholtz machine learns. " * 4)
        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )

        result = self._due_pass(store)

        assert result.claimed == 1
        assert result.ok == 1
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == "Varieties of Helmholtz Machine"
        meta = _meta(store, rid)
        assert meta[paper_meta_enrich.TITLE_RECOVERY_KEY] == (
            paper_meta_enrich._TITLE_RECOVERY_VERSION
        )
        assert len(store.events_for(rid, event="title_front_matter")) == 1

        # And the bound: the next due pass claims nothing for it.
        result2 = self._due_pass(store)
        assert result2.claimed == 0

    def test_pinned_placeholder_is_not_reclaimed(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """The version pin is the bound: a placeholder already swept at
        the current recovery version stays out of the claim."""
        rid = self._stamped_placeholder(
            store,
            slug="rec2",
            doi="10.1/gone2",
            title_recovery_v=paper_meta_enrich._TITLE_RECOVERY_VERSION,
        )
        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )

        assert self._due_pass(store).claimed == 0
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == PLACEHOLDER_TITLE  # untouched
        assert paper_meta_enrich.RESOLVED_AT_KEY in _meta(store, rid)

    def test_dead_end_placeholder_pins_out_after_one_retry(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """A placeholder the leg cannot fix (no byline signature) is
        claimed once, pins out, and is never reclaimed — the recovery is
        bounded, not hourly."""
        rid = self._stamped_placeholder(store, slug="rec3", doi=None)
        _chunk(
            store,
            rid,
            0,
            "An Obscure Machine Learning Result\n\n"
            + "The experiment proceeded as follows. " * 4,
        )
        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )

        assert self._due_pass(store).claimed == 1
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == PLACEHOLDER_TITLE  # still unresolved
        meta = _meta(store, rid)
        assert meta[paper_meta_enrich.TITLE_RECOVERY_KEY] == (
            paper_meta_enrich._TITLE_RECOVERY_VERSION
        )

        assert self._due_pass(store).claimed == 0

    def test_titled_stamped_paper_is_not_recovered(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """The recovery leg's title predicate: a stamped ref carrying a
        real title is as done as any visited row."""
        rid = _paper(
            store,
            slug="rec4",
            doi="10.1/real",
            title="A Real Title",
            meta={"authors_resolved_at": "2026-09-01T00:00:00+00:00"},
        )
        monkeypatch.setattr(
            paper_meta_enrich,
            "_fetch_crossref_message",
            lambda doi, mailto: pytest.fail("no fetch for a titled ref"),
        )

        assert self._due_pass(store).claimed == 0
        assert paper_meta_enrich.TITLE_RECOVERY_KEY not in _meta(store, rid)

    def test_failed_recovery_stays_claimable_until_side_effects_finish(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        """gr658887 review: the recovered title commits atomically with
        the ``title_recovery_v`` pin, after every side effect. A crash in
        the card rebuild must leave the sentinel title (the recovery
        leg's claim key) in place — a title committed earlier would
        falsify the leg's title predicate while the old
        ``authors_resolved_at`` stamp still stands, and the half-
        recovered row would never be claimed again."""
        rid = self._stamped_placeholder(store, slug="rec5", doi="10.1/gone5")
        _chunk(
            store,
            rid,
            0,
            "**Varieties of Helmholtz Machine**\n\nG. E. Hinton & P. Dayan",
        )
        _chunk(store, rid, 1, "The Helmholtz machine learns. " * 4)
        # A stale card carrying the sentinel — the rebuild must rewrite
        # it on the retry, proving the side effect actually finished.
        _chunk(store, rid, -1, PLACEHOLDER_TITLE, kind="card_title")
        monkeypatch.setattr(
            paper_meta_enrich, "_fetch_crossref_message", lambda doi, mailto: None
        )
        real_rebuild = paper_meta_enrich._rebuild_cards
        rebuild_calls = {"n": 0}

        def _flaky_rebuild(store_: Store, ref_id: int, **kw: Any) -> None:
            rebuild_calls["n"] += 1
            if rebuild_calls["n"] == 1:
                raise RuntimeError("cards boom")
            return real_rebuild(store_, ref_id, **kw)

        monkeypatch.setattr(paper_meta_enrich, "_rebuild_cards", _flaky_rebuild)

        first = self._due_pass(store)
        assert first.claimed == 1
        assert first.failed == 1

        # Still fully claimable: sentinel title kept, pin unstamped, the
        # pre-existing stamp intact — and no breadcrumb claims a title
        # write that rolled back.
        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == PLACEHOLDER_TITLE
        meta = _meta(store, rid)
        assert meta[paper_meta_enrich.RESOLVED_AT_KEY] == "2026-09-01T00:00:00+00:00"
        assert meta.get(paper_meta_enrich.TITLE_RECOVERY_KEY, 0) < (
            paper_meta_enrich._TITLE_RECOVERY_VERSION
        )
        assert store.events_for(rid, event="title_front_matter") == []

        second = self._due_pass(store)
        assert second.claimed == 1
        assert second.ok == 1
        assert rebuild_calls["n"] == 2

        ref = store.fetch_refs_by_ids([rid])[rid]
        assert ref.title == "Varieties of Helmholtz Machine"
        meta = _meta(store, rid)
        assert meta[paper_meta_enrich.TITLE_RECOVERY_KEY] == (
            paper_meta_enrich._TITLE_RECOVERY_VERSION
        )
        assert len(store.events_for(rid, event="title_front_matter")) == 1
        with store.pool.connection() as conn:
            card = conn.execute(
                "SELECT text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'card_title'",
                (rid,),
            ).fetchone()
        assert card is not None
        assert card[0] == "Varieties of Helmholtz Machine"

        # Recovery done: pinned out of the claim.
        assert self._due_pass(store).claimed == 0
