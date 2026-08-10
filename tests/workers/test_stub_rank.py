"""Pure-math + merge-logic tests for the ``stub_rank`` pass.

``compute_stub_prios`` / ``_clamp_prio`` / ``_should_write_prio`` /
``_merge_enrich_meta`` / ``_build_stub_text`` are all pure (no DB, no
network) by design — see ``workers/stub_rank.py``'s module docstring —
so the ranking math and the enrich-merge rules are exercised directly
here, without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.store import Store
from precis.workers.stub_rank import (
    _build_stub_text,
    _clamp_prio,
    _merge_enrich_meta,
    _should_write_prio,
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
            written = stub_rank._run_rank(_FAKE_STORE)  # store is never touched
        assert written == 0
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
        assert stub_rank._run_rank(_FAKE_STORE) == 0
