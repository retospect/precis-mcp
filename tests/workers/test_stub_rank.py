"""Pure-math + merge-logic tests for the ``stub_rank`` pass.

``compute_stub_prios`` / ``compute_stub_percentiles`` / ``_clamp_prio`` /
``_should_write_prio`` / ``_merge_enrich_meta`` / ``_build_stub_text`` are
all pure (no DB, no network) by design — see ``workers/stub_rank.py``'s
module docstring — so the ranking math and the enrich-merge rules are
exercised directly here, without a database. Step (d) (the Tier-2 LLM
band) needs a real DB for its candidate-claim + interest-profile queries,
so ``TestRunLlmBand`` and friends below use the ``store`` fixture with a
fake ``.complete``-shaped client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.ingest.semantic_scholar import s2_stub_meta
from precis.store import Store
from precis.store.types import Tag
from precis.workers.stub_rank import (
    _build_stub_text,
    _clamp_prio,
    _merge_enrich_meta,
    _prio_from_percentile,
    _should_write_prio,
    compute_stub_percentiles,
    compute_stub_prios,
)

#: A type-satisfying stand-in for the ``TestRunRankNoAnchorNoOp`` cases below
#: — those monkeypatch away every function that would actually dereference
#: a ``Store``, so the sentinel is never touched; ``cast`` keeps mypy happy
#: without a real DB connection.
_FAKE_STORE = cast(Store, object())


def _unit(*floats: float) -> list[float]:
    """A small helper vector; not normalized on purpose (compute_stub_prios
    normalizes internally)."""
    return list(floats)


# ── compute_stub_prios ──────────────────────────────────────────────


class TestComputeStubPrios:
    def test_no_anchors_returns_empty(self) -> None:
        assert compute_stub_prios({1: _unit(1.0, 0.0)}, [], {}) == {}

    def test_no_stubs_returns_empty(self) -> None:
        assert compute_stub_prios({}, [(_unit(1.0, 0.0), 1.0)], {}) == {}

    def test_canon_direction_best_stub_gets_lowest_prio(self) -> None:
        # Anchor points along +x. Stub 1 is a near-perfect match (best);
        # stub 2 is orthogonal (worst); stub 3 points the opposite way
        # (also worst-ish, but strictly worse than orthogonal).
        anchor = (_unit(1.0, 0.0, 0.0), 1.0)
        stubs = {
            1: _unit(1.0, 0.01, 0.0),  # best match
            2: _unit(0.0, 1.0, 0.0),  # orthogonal
            3: _unit(-1.0, 0.0, 0.0),  # opposite
        }
        out = compute_stub_prios(stubs, [anchor], {})
        assert out[1] < out[2] < out[3]
        assert out[1] == 1  # best stub -> hottest (CANON prio 1)
        assert out[3] == 9  # worst stub -> coldest (CANON prio 9)

    def test_percentile_prio_mapping_is_1_to_9_range(self) -> None:
        anchor = (_unit(1.0, 0.0), 1.0)
        # 5 stubs spread evenly from a perfect match to the opposite
        # direction, so their percentile ranks land at 0, .25, .5, .75, 1.
        stubs = {
            1: _unit(1.0, 0.0),
            2: _unit(0.5, 0.5),
            3: _unit(0.0, 1.0),
            4: _unit(-0.5, 0.5),
            5: _unit(-1.0, 0.0),
        }
        out = compute_stub_prios(stubs, [anchor], {})
        assert out == {1: 1, 2: 3, 3: 5, 4: 7, 5: 9}

    def test_single_stub_is_best_by_convention(self) -> None:
        anchor = (_unit(1.0, 0.0), 1.0)
        out = compute_stub_prios({1: _unit(0.0, 1.0)}, [anchor], {})
        assert out[1] == 1

    def test_takes_the_max_score_across_multiple_anchors_not_just_the_first(
        self,
    ) -> None:
        # A buggy "only consider anchors[0]" implementation would score
        # "best_via_anchor2" as 0 (its cosine against anchor1 alone) and
        # rank it WORST; the correct max-across-anchors score is 1.0
        # (its cosine against anchor2) and ranks it BEST.
        anchor1 = (_unit(1.0, 0.0), 1.0)
        anchor2 = (_unit(0.0, 1.0), 1.0)
        best_via_anchor2, mid, near_best_via_anchor1 = 1, 2, 3
        stubs = {
            best_via_anchor2: _unit(0.0, 1.0),  # cos1=0, cos2=1 -> max=1
            mid: _unit(0.6, 0.8),  # cos1=0.6, cos2=0.8 -> max=0.8
            near_best_via_anchor1: _unit(0.99, 0.01),  # cos1~1, cos2~0.01
        }
        out = compute_stub_prios(stubs, [anchor1, anchor2], {})
        assert out[best_via_anchor2] < out[near_best_via_anchor1] < out[mid]


# ── compute_stub_percentiles ─────────────────────────────────────────


class TestComputeStubPercentiles:
    def test_no_stubs_or_no_anchors_returns_empty(self) -> None:
        anchor = (_unit(1.0, 0.0), 1.0)
        assert compute_stub_percentiles({}, [anchor]) == {}
        assert compute_stub_percentiles({1: _unit(1.0, 0.0)}, []) == {}

    def test_matches_the_percentile_map_compute_stub_prios_derives_prio_from(
        self,
    ) -> None:
        # Same 5-stub spread as test_percentile_prio_mapping_is_1_to_9_range:
        # percentiles 0, .25, .5, .75, 1 map to prios 1, 3, 5, 7, 9.
        anchor = (_unit(1.0, 0.0), 1.0)
        stubs = {
            1: _unit(1.0, 0.0),
            2: _unit(0.5, 0.5),
            3: _unit(0.0, 1.0),
            4: _unit(-0.5, 0.5),
            5: _unit(-1.0, 0.0),
        }
        out = compute_stub_percentiles(stubs, [anchor])
        assert out == {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25, 5: 0.0}

    def test_single_stub_is_percentile_one_by_convention(self) -> None:
        anchor = (_unit(1.0, 0.0), 1.0)
        out = compute_stub_percentiles({1: _unit(0.0, 1.0)}, [anchor])
        assert out[1] == 1.0


# ── LLM label delta (applied before _clamp_prio) ────────────────────


class TestLlmLabelDelta:
    """``_prio_from_percentile`` applies the one-time LLM label as a fixed
    delta on the percentile-derived base prio, clamped to 1..9, with
    :func:`_clamp_prio`'s tag-driven overrides running last so they always
    win over the label."""

    def _flags(self, **kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "dream_acquire": False,
            "requested_by_quest": False,
            "discovered_via_cite": False,
            "llm_label": None,
        }
        base.update(kw)
        return base

    def test_core_label_pulls_prio_hotter(self) -> None:
        # p=0.5 -> base prio 5; core delta -2 -> 3.
        assert _prio_from_percentile(0.5, self._flags(llm_label="core")) == 3

    def test_off_label_pushes_prio_colder(self) -> None:
        # p=0.5 -> base prio 5; off delta +2 -> 7.
        assert _prio_from_percentile(0.5, self._flags(llm_label="off")) == 7

    def test_explore_label_nudges_slightly_colder(self) -> None:
        assert _prio_from_percentile(0.5, self._flags(llm_label="explore")) == 6

    def test_adjacent_label_is_a_no_op(self) -> None:
        assert _prio_from_percentile(0.5, self._flags(llm_label="adjacent")) == 5

    def test_unknown_or_absent_label_is_a_no_op(self) -> None:
        assert _prio_from_percentile(0.5, self._flags(llm_label=None)) == 5
        assert _prio_from_percentile(0.5, self._flags(llm_label="bogus")) == 5

    def test_core_at_base_prio_1_stays_1_after_clamp(self) -> None:
        # p=1.0 -> base prio 1; core delta -2 would go to -1, clamped to 1.
        assert _prio_from_percentile(1.0, self._flags(llm_label="core")) == 1

    def test_off_plus_dream_acquire_still_ends_at_most_3(self) -> None:
        # p=1.0 -> base prio 1; off delta +2 -> 3 (already clamped);
        # DREAM:acquire's explicit-tag clamp still applies on top (min(3,3)).
        out = _prio_from_percentile(
            1.0, self._flags(llm_label="off", dream_acquire=True)
        )
        assert out == 3

    def test_off_label_does_not_override_the_cite_cold_pin(self) -> None:
        # p=0.1 (< 0.3) -> base prio 8, off delta +2 -> 9 (clamped); the
        # discovered-via:cite pin (max(prio, 9)) is a no-op here since the
        # label already pushed it to 9 -- confirms the tag clamp still
        # WINS (runs last) rather than the label being silently dropped.
        out = _prio_from_percentile(
            0.1, self._flags(llm_label="off", discovered_via_cite=True)
        )
        assert out == 9

    def test_dream_acquire_wins_over_a_core_label_pulling_hotter(self) -> None:
        # DREAM:acquire clamps to <=3 regardless of the label already
        # having pulled the prio hot -- the tag clamp is the final word.
        out = _prio_from_percentile(
            0.5, self._flags(llm_label="core", dream_acquire=True)
        )
        assert out == 3


# ── _clamp_prio ──────────────────────────────────────────────────────


class TestClampPrio:
    def test_dream_acquire_clamps_down_to_at_most_3(self) -> None:
        assert (
            _clamp_prio(
                9,
                0.0,
                dream_acquire=True,
                requested_by_quest=False,
                discovered_via_cite=False,
            )
            == 3
        )

    def test_dream_acquire_does_not_raise_an_already_hot_prio(self) -> None:
        assert (
            _clamp_prio(
                1,
                1.0,
                dream_acquire=True,
                requested_by_quest=False,
                discovered_via_cite=False,
            )
            == 1
        )

    def test_requested_by_quest_clamps_down_to_at_most_3(self) -> None:
        assert (
            _clamp_prio(
                7,
                0.2,
                dream_acquire=False,
                requested_by_quest=True,
                discovered_via_cite=False,
            )
            == 3
        )

    def test_discovered_via_cite_low_score_clamps_up_to_9(self) -> None:
        assert (
            _clamp_prio(
                2,
                0.1,
                dream_acquire=False,
                requested_by_quest=False,
                discovered_via_cite=True,
            )
            == 9
        )

    def test_discovered_via_cite_above_threshold_is_not_clamped(self) -> None:
        assert (
            _clamp_prio(
                2,
                0.5,
                dream_acquire=False,
                requested_by_quest=False,
                discovered_via_cite=True,
            )
            == 2
        )

    def test_no_flags_is_a_no_op(self) -> None:
        assert (
            _clamp_prio(
                4,
                0.5,
                dream_acquire=False,
                requested_by_quest=False,
                discovered_via_cite=False,
            )
            == 4
        )

    def test_compute_stub_prios_applies_clamps_end_to_end(self) -> None:
        # Same 5-stub spread as test_percentile_prio_mapping_is_1_to_9_range
        # (base prios 1/3/5/7/9 for stubs 1..5). Stub 4 (base=7, cold-ish)
        # carries DREAM:acquire -- the clamp should visibly pull it down to
        # 3. Stub 5 (base=9, the single worst-scoring stub, p=0 < 0.3)
        # carries discovered-via:cite -- confirms the flag correctly
        # reaches the clamp for the coldest stub too.
        anchor = (_unit(1.0, 0.0), 1.0)
        stubs = {
            1: _unit(1.0, 0.0),
            2: _unit(0.5, 0.5),
            3: _unit(0.0, 1.0),
            4: _unit(-0.5, 0.5),
            5: _unit(-1.0, 0.0),
        }
        flags = {
            4: {"dream_acquire": True},
            5: {"discovered_via_cite": True},
        }
        out = compute_stub_prios(stubs, [anchor], flags)
        assert out[4] == 3  # 7, clamped down by DREAM:acquire
        assert out[5] == 9  # 9 already; discovered-via:cite confirms it stays


# ── _should_write_prio (the prio_by skip rule) ──────────────────────


class TestShouldWritePrio:
    def test_writes_when_prio_is_null(self) -> None:
        assert _should_write_prio(None, None, 5) is True

    def test_never_clobbers_a_human_or_quest_set_prio(self) -> None:
        assert _should_write_prio(2, "user", 9) is False
        assert _should_write_prio(2, "quest", 9) is False
        assert _should_write_prio(2, None, 9) is False

    def test_writes_when_value_changed_on_a_row_it_owns(self) -> None:
        assert _should_write_prio(5, "stub_rank", 3) is True

    def test_writes_first_time_it_stamps_prio_by_even_if_value_matches(self) -> None:
        # prio was NULL before -> current_prio_by is also None; the write
        # still needs to happen once to stamp meta.prio_by='stub_rank'.
        assert _should_write_prio(None, None, 5) is True

    def test_no_write_when_unchanged_and_already_owned(self) -> None:
        assert _should_write_prio(5, "stub_rank", 5) is False


# ── _merge_enrich_meta ───────────────────────────────────────────────


class TestMergeEnrichMeta:
    _NOW = datetime(2026, 8, 10, tzinfo=UTC)

    def test_failed_resolve_stamps_failure_marker_only(self) -> None:
        patch = _merge_enrich_meta({}, None, now=self._NOW)
        assert patch == {
            "s2_enriched_at": self._NOW.isoformat(),
            "s2_enrich_failed": True,
        }

    def test_fills_abstract_when_ref_has_none(self) -> None:
        resolved = {
            "abstract": "A new abstract.",
            "fields": ["CS"],
            "citation_count": 3,
        }
        patch = _merge_enrich_meta({}, resolved, now=self._NOW)
        assert patch["abstract"] == "A new abstract."
        assert patch["s2_fields"] == ["CS"]
        assert patch["s2_citation_count"] == 3
        assert patch["s2_enriched_at"] == self._NOW.isoformat()

    def test_does_not_clobber_an_existing_abstract(self) -> None:
        existing_meta = {"abstract": "The original, richer abstract."}
        resolved = {
            "abstract": "S2's thinner abstract.",
            "fields": [],
            "citation_count": 0,
        }
        patch = _merge_enrich_meta(existing_meta, resolved, now=self._NOW)
        assert "abstract" not in patch

    def test_blank_existing_abstract_is_treated_as_absent(self) -> None:
        existing_meta = {"abstract": "   "}
        resolved: dict[str, Any] = {
            "abstract": "S2's abstract.",
            "fields": [],
            "citation_count": None,
        }
        patch = _merge_enrich_meta(existing_meta, resolved, now=self._NOW)
        assert patch["abstract"] == "S2's abstract."

    def test_blank_resolved_abstract_never_overwrites(self) -> None:
        resolved: dict[str, Any] = {
            "abstract": "",
            "fields": [],
            "citation_count": None,
        }
        patch = _merge_enrich_meta({}, resolved, now=self._NOW)
        assert "abstract" not in patch

    def test_missing_fields_and_citation_count_default_sane(self) -> None:
        resolved = {"abstract": ""}
        patch = _merge_enrich_meta({}, resolved, now=self._NOW)
        assert patch["s2_fields"] == []
        assert patch["s2_citation_count"] is None


# ── _build_stub_text ─────────────────────────────────────────────────


class TestBuildStubText:
    def test_joins_title_and_abstract(self) -> None:
        assert _build_stub_text("Title", "Abstract.") == "Title\n\nAbstract."

    def test_title_only_when_no_abstract(self) -> None:
        assert _build_stub_text("Title", "") == "Title"
        assert _build_stub_text("Title", "   ") == "Title"

    def test_truncates_to_max_chars(self) -> None:
        text = _build_stub_text("T", "x" * 100, max_chars=10)
        assert len(text) == 10

    def test_strips_surrounding_whitespace(self) -> None:
        assert _build_stub_text("  Title  ", "  Abstract  ") == "Title\n\nAbstract"


# ── _run_rank no-anchor no-op (orchestration, DB reads monkeypatched) ──


class TestRunRankNoAnchorNoOp:
    def test_no_anchors_logs_warning_and_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from precis.workers import stub_rank

        monkeypatch.setattr(
            stub_rank,
            "_load_rank_candidates",
            lambda store: (
                {1: [1.0, 0.0]},
                {1: (None, None)},
                {1: {}},
            ),
        )
        monkeypatch.setattr(stub_rank, "_load_anchors", lambda store: [])

        with caplog.at_level("WARNING"):
            written, percentiles = stub_rank._run_rank(
                _FAKE_STORE
            )  # store is never touched
        assert written == 0
        assert percentiles == {}
        assert any("no anchors available" in r.message for r in caplog.records)

    def test_no_pending_stubs_is_a_silent_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import stub_rank

        monkeypatch.setattr(
            stub_rank, "_load_rank_candidates", lambda store: ({}, {}, {})
        )

        def _boom(store: object) -> list[object]:
            raise AssertionError("anchors should not be loaded with no candidates")

        monkeypatch.setattr(stub_rank, "_load_anchors", _boom)
        written, percentiles = stub_rank._run_rank(_FAKE_STORE)
        assert written == 0
        assert percentiles == {}


# ── mint-time s2_meta skips the enrich stage end-to-end (real DB) ──────


class TestMintTimeS2MetaSkipsEnrich:
    """A stub minted via ``upsert_stub_paper(s2_meta=...)`` already carries
    ``meta.s2_enriched_at`` — :func:`~precis.workers.stub_rank.
    _claim_enrich_candidates`'s ``meta->>'s2_enriched_at' IS NULL``
    predicate must skip it, and :func:`~precis.workers.stub_rank.
    _claim_embed_candidates` must pick it up (title + meta abstract +
    the enriched stamp is exactly what it wants)."""

    def test_freshly_minted_stub_skips_enrich_but_is_embed_eligible(
        self, store: Store
    ) -> None:
        from precis.workers.stub_rank import (
            _claim_embed_candidates,
            _claim_enrich_candidates,
        )

        now = datetime(2026, 1, 1, tzinfo=UTC)
        patch = s2_stub_meta(
            {"abstract": "a mint-time abstract", "fields": [], "citation_count": 0},
            now=now,
        )
        ref_id, created = store.upsert_stub_paper(
            identifiers=[("doi", "10.1/mint-time-skip-enrich")],
            title="Mint-Time Enriched Stub",
            set_by="system",
            s2_meta=patch,
        )
        assert created is True

        enrich_ids = [c[0] for c in _claim_enrich_candidates(store, limit=100)]
        assert ref_id not in enrich_ids

        embed_ids = [c[0] for c in _claim_embed_candidates(store, limit=100)]
        assert ref_id in embed_ids


# ── (d) LLM band (real DB) ───────────────────────────────────────────


class _FakeLlmResult:
    """Duck-types :class:`~precis.utils.llm.router.LlmResult` — the
    ``.text``/``.model``/``.cost_usd``/``.total_tokens`` fields
    :func:`~precis.workers.stub_rank._run_llm_band` reads."""

    def __init__(
        self,
        text: str,
        *,
        model: str = "fake-small",
        cost_usd: float | None = 0.001,
        total_tokens: int | None = 42,
    ) -> None:
        self.text = text
        self.model = model
        self.cost_usd = cost_usd
        self.total_tokens = total_tokens


class _FakeBandClient:
    """A ``.complete``-shaped fake — pops one canned reply (an
    ``_FakeLlmResult`` or an exception instance to raise) per call."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(messages)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _active_quest_with_card(store: Store, *, title: str, card_text: str) -> int:
    """An active quest whose mission ``card_combined`` chunk carries
    ``card_text`` — what :func:`~precis.workers.stub_rank.
    _load_interest_profile` reads."""
    ref = store.insert_ref(kind="quest", slug=None, title=title, meta={})
    store.add_tag(ref.id, Tag.closed("STATUS", "active"), set_by="system")
    store.blocks.upsert_card_combined(ref.id, card_text)
    return ref.id


def _band_stub(
    store: Store, *, cite_key: str, doi: str, title: str, abstract: str = ""
) -> int:
    """A paper stub (external id, no PDF) with a title + meta abstract —
    exactly the shape :func:`~precis.workers.stub_rank._claim_band_candidates`
    selects."""
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=title, meta={"abstract": abstract}
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
            "VALUES (%s, 'doi', %s, 'manual')",
            (ref.id, doi),
        )
    return ref.id


class TestRunLlmBand:
    def test_skips_with_no_client(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        out = _run_llm_band(store, client=None, percentiles={1: 0.5}, limit=25)
        assert out == (0, 0)

    def test_skips_with_zero_limit(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        client = _FakeBandClient([])
        out = _run_llm_band(store, client=client, percentiles={1: 0.5}, limit=0)
        assert out == (0, 0)
        assert client.calls == []

    def test_skips_with_no_percentiles(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        client = _FakeBandClient([])
        out = _run_llm_band(store, client=client, percentiles={}, limit=25)
        assert out == (0, 0)
        assert client.calls == []

    def test_band_bounds_respected_stub_above_hi_not_claimed(
        self, store: Store
    ) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(
            store, title="Nitrate reduction", card_text="Cu/Pd catalysts for NO3-"
        )
        rid = _band_stub(
            store, cite_key="hotstub2024", doi="10.1/hotstub", title="Hot stub"
        )
        client = _FakeBandClient([])
        # Default band is [0.30, 0.70]; 0.9 sits above the hi bound.
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.9}, limit=25
        )
        assert (attempted, labeled) == (0, 0)
        assert client.calls == []

    def test_no_active_quests_skips_the_whole_step(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        rid = _band_stub(
            store, cite_key="noquest2024", doi="10.1/noquest", title="No quest stub"
        )
        client = _FakeBandClient([])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (0, 0)
        assert client.calls == []

    def test_labels_and_writes_llm_band_meta_for_a_stub_in_band(
        self, store: Store
    ) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(
            store, title="Nitrate reduction", card_text="Cu/Pd catalysts for NO3-"
        )
        rid = _band_stub(
            store,
            cite_key="midstub2024",
            doi="10.1/midstub",
            title="A mid-band stub",
            abstract="About electrocatalytic nitrate reduction.",
        )
        client = _FakeBandClient(
            [_FakeLlmResult('{"label": "core", "reason": "fits the quest"}')]
        )
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (1, 1)
        assert len(client.calls) == 1

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        assert row is not None
        meta = row[0]
        assert meta["llm_label"] == "core"
        assert meta["llm_reason"] == "fits the quest"
        band = meta["llm_band"]
        assert band["p"] == 0.5
        assert band["model"] == "fake-small"
        assert band["cost_usd"] == 0.001
        assert band["total_tokens"] == 42
        assert "ts" in band

    def test_cap_respected_limit_1_labels_only_one(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        r1 = _band_stub(store, cite_key="cap2024a", doi="10.1/capa", title="A")
        r2 = _band_stub(store, cite_key="cap2024b", doi="10.1/capb", title="B")
        client = _FakeBandClient([_FakeLlmResult('{"label": "adjacent"}')])
        attempted, labeled = _run_llm_band(
            store,
            client=client,
            percentiles={r1: 0.5, r2: 0.6},
            limit=1,
        )
        assert (attempted, labeled) == (1, 1)
        assert len(client.calls) == 1

    def test_already_labeled_stubs_are_not_reclaimed(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(
            store, cite_key="already2024", doi="10.1/already", title="Already"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                ('{"llm_label": "core", "llm_reason": "prior"}', rid),
            )
        client = _FakeBandClient([])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (0, 0)
        assert client.calls == []

    def test_invalid_json_leaves_stub_unlabeled_but_counts_attempted(
        self, store: Store
    ) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(
            store, cite_key="badjson2024", doi="10.1/badjson", title="Bad JSON"
        )
        client = _FakeBandClient([_FakeLlmResult("not json at all")])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (1, 0)

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        assert row is not None
        assert row[0].get("llm_label") is None

    def test_unknown_label_value_leaves_stub_unlabeled(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(
            store, cite_key="unknownlbl2024", doi="10.1/unknownlbl", title="Unknown"
        )
        client = _FakeBandClient([_FakeLlmResult('{"label": "spicy"}')])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (1, 0)

    def test_raised_exception_leaves_stub_unlabeled(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(
            store, cite_key="raises2024", doi="10.1/raises", title="Raises"
        )
        client = _FakeBandClient([RuntimeError("transport down")])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (1, 0)


class TestClaimBandLease:
    """The atomic-claim lease (fix for cross-node double-claim on a paid
    LLM call) and the lifetime paid-retry cap — see ``stub_rank.
    _claim_band_candidates``'s docstring."""

    def test_fresh_claim_not_reclaimed(self, store: Store) -> None:
        from precis.workers.stub_rank import _claim_band_candidates

        rid = _band_stub(
            store, cite_key="freshclaim2024", doi="10.1/freshclaim", title="Fresh"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "'llm_band_claimed_at', now()::text) WHERE ref_id = %s",
                (rid,),
            )
        out = _claim_band_candidates(store, band_ids=[rid], limit=25)
        assert out == []

    def test_stale_claim_is_reclaimed(self, store: Store) -> None:
        from precis.workers.stub_rank import _claim_band_candidates

        rid = _band_stub(
            store, cite_key="staleclaim2024", doi="10.1/staleclaim", title="Stale"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "'llm_band_claimed_at', (now() - interval '20 minutes')::text) "
                "WHERE ref_id = %s",
                (rid,),
            )
        out = _claim_band_candidates(store, band_ids=[rid], limit=25)
        assert [c[0] for c in out] == [rid]

    def test_claim_stamps_llm_band_claimed_at(self, store: Store) -> None:
        from precis.workers.stub_rank import _claim_band_candidates

        rid = _band_stub(
            store, cite_key="stampclaim2024", doi="10.1/stampclaim", title="Stamp"
        )
        out = _claim_band_candidates(store, band_ids=[rid], limit=25)
        assert [c[0] for c in out] == [rid]

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'llm_band_claimed_at' FROM refs WHERE ref_id = %s",
                (rid,),
            ).fetchone()
        assert row is not None and row[0] is not None

    def test_failing_client_increments_failure_counter(self, store: Store) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(
            store, cite_key="failinc2024", doi="10.1/failinc", title="Fail"
        )
        client = _FakeBandClient([RuntimeError("boom")])
        _run_llm_band(store, client=client, percentiles={rid: 0.5}, limit=25)

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'llm_band_failures' FROM refs WHERE ref_id = %s",
                (rid,),
            ).fetchone()
        assert row is not None and row[0] == "1"

    def test_stub_below_failure_cap_still_claimed(self, store: Store) -> None:
        from precis.workers.stub_rank import _MAX_BAND_FAILURES, _claim_band_candidates

        rid = _band_stub(
            store, cite_key="belowcap2024", doi="10.1/belowcap", title="Below"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{llm_band_failures}', "
                "to_jsonb(%s::int)) WHERE ref_id = %s",
                (_MAX_BAND_FAILURES - 1, rid),
            )
        out = _claim_band_candidates(store, band_ids=[rid], limit=25)
        assert [c[0] for c in out] == [rid]

    def test_stub_at_failure_cap_no_longer_claimed(self, store: Store) -> None:
        from precis.workers.stub_rank import _MAX_BAND_FAILURES, _claim_band_candidates

        rid = _band_stub(store, cite_key="atcap2024", doi="10.1/atcap", title="At cap")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{llm_band_failures}', "
                "to_jsonb(%s::int)) WHERE ref_id = %s",
                (_MAX_BAND_FAILURES, rid),
            )
        out = _claim_band_candidates(store, band_ids=[rid], limit=25)
        assert out == []

    def test_successful_label_after_prior_failure_still_works(
        self, store: Store
    ) -> None:
        from precis.workers.stub_rank import _run_llm_band

        _active_quest_with_card(store, title="Q", card_text="card text")
        rid = _band_stub(store, cite_key="retry2024", doi="10.1/retry", title="Retry")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{llm_band_failures}', "
                "to_jsonb(1)) WHERE ref_id = %s",
                (rid,),
            )
        client = _FakeBandClient([_FakeLlmResult('{"label": "adjacent"}')])
        attempted, labeled = _run_llm_band(
            store, client=client, percentiles={rid: 0.5}, limit=25
        )
        assert (attempted, labeled) == (1, 1)

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta->>'llm_label' FROM refs WHERE ref_id = %s", (rid,)
            ).fetchone()
        assert row is not None and row[0] == "adjacent"


class TestLoadInterestProfile:
    def test_empty_with_no_active_quests(self, store: Store) -> None:
        from precis.workers.stub_rank import _load_interest_profile

        assert _load_interest_profile(store) == ""

    def test_formats_one_line_per_active_quest_card(self, store: Store) -> None:
        from precis.workers.stub_rank import _load_interest_profile

        _active_quest_with_card(
            store, title="Nitrate reduction", card_text="Cu/Pd catalysts for NO3-"
        )
        out = _load_interest_profile(store)
        assert out == "- Nitrate reduction: Cu/Pd catalysts for NO3-"

    def test_dormant_quest_is_excluded(self, store: Store) -> None:
        from precis.workers.stub_rank import _load_interest_profile

        ref = store.insert_ref(kind="quest", slug=None, title="Dormant", meta={})
        store.blocks.upsert_card_combined(ref.id, "no longer active")
        assert _load_interest_profile(store) == ""


class TestRunStubRankPassBandWiring:
    """``run_stub_rank_pass`` folds step (d)'s counts in and passes
    ``band_client=None`` straight through (mirrors ``embedder=None``)."""

    def test_band_client_none_calls_run_llm_band_with_none_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import stub_rank

        monkeypatch.setattr(stub_rank, "_run_enrich", lambda store, **kw: (0, 0))
        monkeypatch.setattr(stub_rank, "_run_embed", lambda store, **kw: (0, 0))
        monkeypatch.setattr(stub_rank, "_run_rank", lambda store: (0, {1: 0.5}))

        captured: dict[str, Any] = {}

        def _fake_band(
            store: object, *, client: object, percentiles: dict[int, float], limit: int
        ) -> tuple[int, int]:
            captured["client"] = client
            captured["percentiles"] = percentiles
            captured["limit"] = limit
            return 0, 0

        monkeypatch.setattr(stub_rank, "_run_llm_band", _fake_band)

        out = stub_rank.run_stub_rank_pass(
            _FAKE_STORE, api_key="", resolve_batch=lambda *a: []
        )
        assert captured["client"] is None
        assert captured["percentiles"] == {1: 0.5}
        assert out == {"claimed": 0, "ok": 0, "failed": 0}

    def test_band_counts_fold_into_claimed_and_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import stub_rank

        monkeypatch.setattr(stub_rank, "_run_enrich", lambda store, **kw: (0, 0))
        monkeypatch.setattr(stub_rank, "_run_embed", lambda store, **kw: (0, 0))
        monkeypatch.setattr(stub_rank, "_run_rank", lambda store: (0, {1: 0.5}))
        monkeypatch.setattr(stub_rank, "_run_llm_band", lambda *a, **kw: (5, 3))

        out = stub_rank.run_stub_rank_pass(
            _FAKE_STORE,
            api_key="",
            resolve_batch=lambda *a: [],
            band_client=_FakeBandClient([]),
        )
        assert out == {"claimed": 5, "ok": 3, "failed": 0}
