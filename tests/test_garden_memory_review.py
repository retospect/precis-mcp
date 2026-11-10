"""Proposal-only memory review detectors on dev-DB fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.errors import QueryCanceled
from psycopg.types.json import Jsonb

from precis.graph.garden import candidates as cand
from precis.graph.garden.consumer import _reviewable
from precis.graph.garden.proposals import GardenProposal, key_block, record_rejection
from precis.graph.garden.proposer import (
    MEMORY_ACTIONS,
    MEMORY_PROMPT_VERSION,
    _prompt_for,
)
from precis.store import Store
from precis.store.revision_context import TrustAct, revision_context
from precis.store.types import ChunkInsert, Tag

NOW = datetime(2026, 11, 4, tzinfo=UTC)
DETECTORS = ("review_due", "title_shape", "negative_undated")


def _memory(
    store: Store,
    title: str = "A standing fact remains useful",
    body: str = "The verified route reaches the evidence.",
    *,
    section: str = "reference",
    age_days: int = 0,
    meta: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title=title, meta=meta or {})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    store.add_tag(ref.id, Tag.open(f"section:{section}"), set_by="system")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s, updated_at = %s WHERE ref_id = %s",
            (NOW - timedelta(days=age_days), NOW - timedelta(days=age_days), ref.id),
        )
    return int(ref.id)


def _review(
    store: Store,
    rid: int,
    *,
    age_days: int = 0,
    version: str | None = MEMORY_PROMPT_VERSION,
) -> None:
    state = store.trust_state("ref", rid)
    assert state is not None
    act = TrustAct(
        action="consider",
        target_kind="ref",
        target_id=rid,
        expected_sha=state.sha,
        reason=f"kg-review:fixture:{(NOW - timedelta(days=age_days)).date()}:ok",
    )
    store.record_trust_act(act, actor="reviewer", actor_type="agent")
    if version is not None:
        # The current trust writer lacks a checklist-version argument. Seed
        # that future stamp field in the dev fixture, preserving all acts.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE revisions r SET trust_acts = ("
                "SELECT jsonb_agg(CASE WHEN a->>'op_id' = %s THEN "
                "jsonb_set(a, '{payload,prompt_version}', %s::jsonb, true) ELSE a END "
                "ORDER BY ord) FROM jsonb_array_elements(r.trust_acts) "
                "WITH ORDINALITY AS x(a, ord)) "
                "WHERE r.target_kind = 'ref' AND r.target_id = %s "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(r.trust_acts) a "
                "WHERE a->>'op_id' = %s)",
                (act.op_id, Jsonb(version), rid, act.op_id),
            )


@pytest.mark.parametrize(
    ("title", "sections", "expected"),
    [
        ("Remaining work", frozenset({"threads"}), ("thread", 14)),
        ("Release handoff", frozenset({"reference"}), ("snapshot", 14)),
        ("Resume the allocator work", frozenset({"workflow"}), ("snapshot", 14)),
        ("Worker state on 2026-10-01", frozenset({"gotchas"}), ("snapshot", 14)),
        ("Worker state as of 2026-10-01", frozenset(), ("snapshot", 14)),
        ("Queue incident report", frozenset({"reference"}), ("incident", 90)),
        ("Queue postmortem", frozenset({"gotchas"}), ("incident", 90)),
        ("Allocator campaign", frozenset({"workflow"}), ("campaign", 90)),
        ("Allocator waves 2-4", frozenset({"reference"}), ("campaign", 90)),
        ("Allocator 2026-09-01–2026-10-01", frozenset(), ("campaign", 90)),
        ("A standing reference", frozenset({"reference"}), ("standing", None)),
        ("A standing gotcha", frozenset({"gotchas"}), ("standing", None)),
        ("A standing runbook", frozenset({"runbooks"}), ("standing", None)),
    ],
)
def test_expiry_shape_table(
    title: str, sections: frozenset[str], expected: tuple[str, int | None]
) -> None:
    assert cand.memory_expiry_shape(title, sections) == expected


def test_memory_review_requires_explicit_opt_in() -> None:
    from precis.workers.job_types.graph_garden import PARAMS_SCHEMA

    assert "memory-review" in PARAMS_SCHEMA["properties"]["pass"]["anyOf"][0]["enum"]
    assert cand.detectors_for(("memory-review",)) == [
        "entity_claim_stale",
        "negative_undated",
        "restated_claim",
        "review_due",
        "source_drift",
        "title_shape",
    ]
    for detector in DETECTORS:
        assert detector in cand.detectors_for(("memory-review",))
        assert detector not in cand.detectors_for(("all",))
        assert detector not in cand.detectors_for(("memory",))
        assert detector not in cand.detectors_for(("all", "memory"))


@pytest.mark.parametrize("limit", [1, 50])
def test_review_due_priority_does_not_skip_lower_ids(store: Store, limit: int) -> None:
    aged = _memory(store, age_days=40)
    _review(store, aged, age_days=31)
    version = _memory(store)
    _review(store, version, version="garden-propose.memory.old")
    changed = _memory(store)
    _review(store, changed)
    with revision_context("fixture content edit", trust_origin="generated"):
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET title = 'The route now names another source' "
                "WHERE ref_id = %s",
                (changed,),
            )
    never = _memory(store)
    expired = _memory(store, "Allocator handoff", age_days=15)
    cursor: dict[str, int] = {}
    seen: list[cand.GardenCandidate] = []
    for _ in range(5 if limit == 1 else 1):
        seen.extend(
            cand.review_due_candidates(
                store, now=NOW, limit=limit, cursors=cursor, next_cursors=cursor
            )
        )
    assert [c.target_id for c in seen] == [expired, never, changed, version, aged]
    assert [c.evidence["reason"] for c in seen] == [
        "expired",
        "never_reviewed",
        "sha_changed",
        "version_bumped",
        "aged",
    ]
    shas = cand._ref_shas(store, [c.target_id for c in seen])
    for candidate in seen:
        assert candidate.evidence["content_sha"] == candidate.payload["base_sha"]
        assert candidate.payload["base_sha"] == shas[candidate.target_id]
        assert store.reviews_for("ref", candidate.target_id) == []
    assert all(
        r.retired_at is None for r in store.fetch_refs_by_ids(list(shas)).values()
    )
    assert "expires" not in store.fetch_refs_by_ids([expired])[expired].meta


@pytest.mark.parametrize(
    ("section", "age", "due"),
    [
        ("reference", 29, False),
        ("reference", 30, True),
        ("gotchas", 29, False),
        ("gotchas", 30, True),
        ("runbooks", 59, False),
        ("runbooks", 60, True),
        ("workflow", 90, False),
        ("rulings", 90, False),
    ],
)
def test_review_age_thresholds(store: Store, section: str, age: int, due: bool) -> None:
    rid = _memory(store, section=section, age_days=age)
    _review(store, rid, age_days=age)
    found = cand.review_due_candidates(store, now=NOW)
    assert [c.target_id for c in found] == ([rid] if due else [])
    if due:
        assert found[0].evidence["reason"] == "aged"


@pytest.mark.parametrize("section", ["workflow", "rulings"])
def test_rulings_follow_review_by(store: Store, section: str) -> None:
    due = _memory(store, section=section, meta={"review_by": "2026-11-04"})
    later = _memory(store, section=section, meta={"review_by": "2026-11-05"})
    _review(store, due, age_days=1)
    _review(store, later)
    assert [c.target_id for c in cand.review_due_candidates(store, now=NOW)] == [due]


def test_current_review_suppresses_due_but_versionless_stamp_does_not(
    store: Store,
) -> None:
    current = _memory(store)
    _review(store, current)
    old_format = _memory(store)
    _review(store, old_format, version=None)
    (candidate,) = cand.review_due_candidates(store, now=NOW)
    assert candidate.target_id == old_format
    assert candidate.evidence["reason"] == "version_bumped"


def test_explicit_expiry_overrides_shape_and_incident_advice_is_conditional(
    store: Store,
) -> None:
    extended = _memory(
        store, "Allocator handoff", age_days=20, meta={"expires": "2026-12-01"}
    )
    _review(store, extended)
    expired = _memory(store, meta={"expires": "2026-11-03"})
    _review(store, expired)
    incident = _memory(store, "Allocator incident report", age_days=91)
    _review(store, incident)
    found = {c.target_id: c for c in cand.review_due_candidates(store, now=NOW)}
    assert set(found) == {expired, incident}
    assert all(c.evidence["reason"] == "expired" for c in found.values())
    advice = found[incident].payload["suggested_action"].lower()
    assert all(word in advice for word in ("dated", "cause", "fix", "uncertainty"))
    assert "verif" in advice
    assert store.fetch_refs_by_ids([incident])[incident].retired_at is None


@pytest.mark.parametrize(
    ("title", "section", "reason"),
    [
        ("ship_trap", "reference", "slug"),
        ("docs/conventions/testing.md", "reference", "path_like"),
        (
            "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen",
            "reference",
            "too_many_words",
        ),
        ("Allocator snapshot 2026-11-03", "threads", "dated_snapshot"),
    ],
)
def test_title_shape_fixture(
    store: Store, title: str, section: str, reason: str
) -> None:
    rid = _memory(store, title, section=section)
    (candidate,) = cand.title_shape_candidates(store)
    assert candidate.target_id == rid
    assert reason in candidate.evidence["reasons"]
    assert candidate.payload["new_title"] is None
    assert candidate.payload["base_sha"] == cand._ref_shas(store, [rid])[rid]
    assert store.fetch_refs_by_ids([rid])[rid].title == title


def test_title_shape_keeps_prose_and_dated_non_thread_titles(store: Store) -> None:
    _memory(store, "The route reaches the current evidence")
    _memory(store, "A measured result from 2026-11-03")
    assert cand.title_shape_candidates(store) == []


def test_negative_undated_uses_line_local_dates_and_grep_brief(store: Store) -> None:
    body = (
        "2026-11-03: coverage does not exist.\n"
        "NO door clears the DOI.\n"
        "The allocator is not yet implemented.\n"
        "No tests exercise this route.\n"
        "The recovery path does not exist.\n"
        "Remaining checks: none.\n"
        "Ordinary affirmative statement."
    )
    rid = _memory(store, body=body)
    (candidate,) = cand.negative_undated_candidates(store)
    assert candidate.target_id == rid
    lines = candidate.evidence["lines"]
    assert len(lines) == 5
    for line in lines:
        assert body[slice(*line["span"])] == line["line"]
        assert "2026-11-03" not in line["line"]
    assert {"door", "doi"} <= set(lines[0]["grep_brief"])
    assert "allocator" in lines[1]["grep_brief"]
    assert candidate.payload["not_checked"] == ["absence_claims"]
    assert candidate.payload["base_sha"] == cand._ref_shas(store, [rid])[rid]
    assert cand._memory_bodies(store, [rid])[0][1] == body
    assert store.reviews_for("ref", rid) == []


def test_negative_reads_code_spans_and_iso_timestamps(store: Store) -> None:
    body = "No `recovery_door` exists.\n2026-11-03T12:00:00Z: No recovery door exists."
    _memory(store, body=body)
    (candidate,) = cand.negative_undated_candidates(store)
    assert [line["line"] for line in candidate.evidence["lines"]] == [
        "No `recovery_door` exists."
    ]


def test_negative_matches_are_capped(store: Store) -> None:
    _memory(store, body="\n".join(f"No route checks item {i}." for i in range(100)))
    (candidate,) = cand.negative_undated_candidates(store)
    assert candidate.payload["matches_capped"] is True
    assert len(candidate.evidence["lines"]) == candidate.payload["match_cap"]
    assert candidate.payload["match_cap"] <= 100


def test_dreams_and_retired_memories_never_enter_review_detectors(store: Store) -> None:
    dream = _memory(
        store,
        "old_snapshot",
        "NO door exists.",
        section="threads",
        age_days=400,
        meta={"expires": "2020-01-01"},
    )
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    retired = _memory(store, "old_snapshot", "No route exists.", age_days=400)
    store.retire_ref(retired)
    assert cand.review_due_candidates(store, now=NOW) == []
    assert cand.title_shape_candidates(store) == []
    assert cand.negative_undated_candidates(store) == []


@pytest.mark.parametrize("detector", DETECTORS)
def test_review_detector_timeout_keeps_cursor_unsaved(
    store: Store, monkeypatch: pytest.MonkeyPatch, detector: str
) -> None:
    def cancelled(*args: Any, **kwargs: Any) -> list[cand.GardenCandidate]:
        kwargs["next_cursors"][f"{detector}:memory"] = 99
        raise QueryCanceled("fixture timeout")

    monkeypatch.setattr(cand, f"{detector}_candidates", cancelled)
    run = cand.run_detectors(store, passes=("memory-review",), detector_deadline_s=0)
    assert detector in run.skipped
    assert f"{detector}:memory" not in run.cursors


@pytest.mark.parametrize("detector", ["title_shape", "negative_undated"])
def test_review_text_detectors_resume_after_limit(store: Store, detector: str) -> None:
    targets = [_memory(store, f"slug_{i}", "NO door exists.") for i in range(3)]
    detect = getattr(cand, f"{detector}_candidates")
    cursor: dict[str, int] = {}
    seen: list[int] = []
    for _ in targets:
        found = detect(store, limit=1, scan_cap=2, cursors=cursor, next_cursors=cursor)
        seen.extend(c.target_id for c in found)
    assert seen == targets
    assert detect(store, cursors=cursor, next_cursors=cursor) == []
    assert cursor[f"{detector}:memory"] == 0


def test_detector_keys_are_distinct_on_the_same_target() -> None:
    candidates = [
        cand.GardenCandidate(
            detector=name, action="rewrite", target_kind="ref", target_id=1
        )
        for name in (*DETECTORS, "dangling_mention", "anchor_rot")
    ]
    assert len({candidate.key for candidate in candidates}) == len(candidates)


@pytest.mark.parametrize("detector", DETECTORS)
def test_memory_review_prompts_and_actions_stay_proposal_only(detector: str) -> None:
    candidate = cand.GardenCandidate(
        detector=detector, action="rewrite", target_kind="ref", target_id=1
    )
    prompt = _prompt_for(candidate)
    assert f'the payload actions are "{"|".join(MEMORY_ACTIONS[detector])}"' in prompt
    assert "All three detectors are proposal-only" in prompt
    assert "no apply or auto-apply path" in prompt
    assert "never invent a fix" in prompt
    assert "not_checked" in prompt
    for action in MEMORY_ACTIONS[detector]:
        proposal = GardenProposal(
            review_id=1,
            target_kind="ref",
            target_id=1,
            action=action,
            key=candidate.key,
            detector=detector,
            title="Fixture proposal",
            payload={"base_sha": "fixture-sha"},
            evidence={"content_sha": "fixture-sha"},
            rationale="Fixture",
            model=None,
            at=NOW,
            current=True,
        )
        assert not _reviewable(proposal)


def test_review_due_survives_the_generic_fresh_considered_skip(store: Store) -> None:
    expired = _memory(store, meta={"expires": "2000-01-01"})
    version = _memory(store)
    # The generic freshness SQL uses the DB clock, whereas other review
    # tests use a fixed detector clock. Stamp these fixtures on today's UTC date.
    stamp_age = (NOW.date() - datetime.now(UTC).date()).days
    _review(store, expired, age_days=stamp_age)
    _review(store, version, age_days=stamp_age, version="garden-propose.memory.old")
    assert cand.freshly_considered(store, [expired, version], []) == {
        ("ref", expired),
        ("ref", version),
    }
    run = cand.run_detectors(store, passes=("memory-review",), detector_deadline_s=0)
    due = [
        candidate for candidate in run.candidates if candidate.detector == "review_due"
    ]
    assert [
        (candidate.target_id, candidate.evidence["reason"]) for candidate in due
    ] == [(expired, "expired"), (version, "version_bumped")]
    assert run.skipped_considered == 0


def test_review_due_single_ref_windows_finish_one_priority_sweep(store: Store) -> None:
    never = _memory(store)
    version = _memory(store)
    _review(store, version, version="garden-propose.memory.old")
    expired = _memory(store, "Allocator handoff", age_days=15)
    cursor: dict[str, int] = {}
    seen: list[int] = []
    for _ in range(50):
        found = cand.review_due_candidates(
            store, now=NOW, scan_cap=1, limit=1, cursors=cursor, next_cursors=cursor
        )
        assert len(found) <= 1
        seen.extend(candidate.target_id for candidate in found)
        if cursor["review_due:memory"] == cursor["review_due:priority"] == 0:
            break
    else:
        pytest.fail("review_due did not finish its bounded priority sweep")
    assert seen == [expired, never, version]


def test_null_expiry_disables_default_and_invalid_expiry_is_unchecked(
    store: Store,
) -> None:
    disabled = _memory(
        store,
        "Allocator handoff",
        section="threads",
        age_days=400,
        meta={"expires": None},
    )
    _review(store, disabled)
    invalid = _memory(
        store,
        "Allocator handoff",
        section="threads",
        age_days=400,
        meta={"expires": "not-a-date"},
    )
    (candidate,) = cand.review_due_candidates(store, now=NOW)
    assert candidate.target_id == invalid
    assert candidate.evidence["reason"] == "never_reviewed"
    assert candidate.evidence["expires"] is None
    assert "meta.expires" in candidate.payload["not_checked"]


def test_review_completed_on_review_by_satisfies_the_ruling(store: Store) -> None:
    rid = _memory(store, section="rulings", meta={"review_by": "2026-11-04"})
    _review(store, rid)
    assert cand.review_due_candidates(store, now=NOW) == []


@pytest.mark.parametrize(
    "date", ["2026/11/03", "03/11/2026", "November 3, 2026", "3 Nov 2026"]
)
def test_negative_claim_date_formats_are_recognized(store: Store, date: str) -> None:
    _memory(store, body=f"No recovery door exists as of {date}.")
    assert cand.negative_undated_candidates(store) == []


def test_unchanged_review_rejection_does_not_hide_later_expiry(store: Store) -> None:
    rid = _memory(store, "Allocator handoff")
    (unreviewed,) = cand.review_due_candidates(store, now=NOW)
    assert unreviewed.evidence["reason"] == "never_reviewed"
    record_rejection(
        store,
        unreviewed,
        rationale="Still live state",
        prompt_version=MEMORY_PROMPT_VERSION,
    )
    (expired,) = cand.review_due_candidates(store, now=NOW + timedelta(days=15))
    assert expired.target_id == rid
    assert expired.evidence["reason"] == "expired"
    assert expired.payload["base_sha"] == unreviewed.payload["base_sha"]
    assert expired.key != unreviewed.key
    assert (
        key_block(
            store, "ref", rid, unreviewed.key, prompt_version=MEMORY_PROMPT_VERSION
        )
        == "rejected"
    )
    assert (
        key_block(store, "ref", rid, expired.key, prompt_version=MEMORY_PROMPT_VERSION)
        is None
    )


def test_new_review_generation_reopens_aged_review_key(store: Store) -> None:
    rid = _memory(store, age_days=40)
    _review(store, rid, age_days=31)
    (old,) = cand.review_due_candidates(store, now=NOW)
    assert old.evidence["reason"] == "aged"
    _review(store, rid)
    (new,) = cand.review_due_candidates(store, now=NOW + timedelta(days=30))
    assert new.evidence["reason"] == "aged"
    assert new.payload["base_sha"] == old.payload["base_sha"]
    assert new.key != old.key


def test_demoted_review_stamp_does_not_suppress_review_due(store: Store) -> None:
    rid = _memory(store)
    _review(store, rid)
    assert cand.review_due_candidates(store, now=NOW) == []
    state = store.trust_state("ref", rid)
    assert state is not None
    store.record_trust_act(
        TrustAct(
            action="demote",
            target_kind="ref",
            target_id=rid,
            expected_sha=state.sha,
            confidence="hunch",
            reason="Fixture doubt",
        ),
        actor="reviewer",
        actor_type="agent",
    )
    (candidate,) = cand.review_due_candidates(store, now=NOW)
    assert candidate.target_id == rid
    assert candidate.evidence["reason"] == "never_reviewed"
    assert candidate.evidence["review_state"] == "invalidated"


def test_undated_thread_campaign_title_is_not_a_dated_snapshot(store: Store) -> None:
    _memory(store, "Allocator campaign", section="threads")
    assert cand.title_shape_candidates(store) == []


def test_thread_wave_range_title_is_a_dated_snapshot(store: Store) -> None:
    rid = _memory(store, "Allocator waves 2-4", section="threads")
    (candidate,) = cand.title_shape_candidates(store)
    assert candidate.target_id == rid
    assert "dated_snapshot" in candidate.evidence["reasons"]


def test_undated_resume_and_prose_containing_path_are_not_title_smells(
    store: Store,
) -> None:
    _memory(store, "Resume allocation after dependencies resolve", section="threads")
    _memory(store, "Run scripts/test before committing changes")
    assert cand.title_shape_candidates(store) == []
