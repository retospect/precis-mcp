"""Read-only drift proposals over dev-DB memories and committed source fixtures."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import QueryCanceled
from psycopg.types.json import Jsonb

from precis.graph.garden import candidates as cand
from precis.graph.garden import proposer
from precis.graph.garden._drift_refs import ref_change
from precis.graph.garden._drift_repo import RepoDriftResolver
from precis.store import Store
from precis.store.types import Tag
from tests import test_garden_drift_repo as repo_fixtures
from tests.test_garden_drift_repo import TOUCHED, _commit, _git

drift_repo = repo_fixtures.drift_repo

DETECTORS = ("source_drift", "entity_claim_stale")
AFTER = datetime(2026, 1, 3, tzinfo=UTC)


def _memory(store: Store, body: str, *, touched: datetime = TOUCHED) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="Drift memory", conn=conn
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, created_at) "
            "VALUES (%s, 0, 'memory_body', %s, %s)",
            (ref.id, body, touched),
        )
        conn.execute(
            "UPDATE refs SET updated_at = %s WHERE ref_id = %s", (touched, ref.id)
        )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE revisions SET at = %s WHERE target_kind = 'ref' AND target_id = %s",
            (touched, ref.id),
        )
    return int(ref.id)


@pytest.fixture
def changed_repo(drift_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = drift_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Old rule.", "New contract."),
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    monkeypatch.setattr(
        cand, "RepoDriftResolver", lambda: RepoDriftResolver(drift_repo)
    )
    return drift_repo


def test_source_section_change_pins_evidence_and_preserves_memory(
    store: Store, changed_repo: Path
) -> None:
    body = "The old contract requires one route.\nSource: docs/guide.md § Contract"
    target = _memory(store, body)
    (candidate,) = cand.source_drift_candidates(store)
    assert candidate.target_id == target
    assert candidate.action == "rewrite"
    assert candidate.payload["base_sha"] == store.target_sha("ref", target)
    assert candidate.evidence["content_sha"] == candidate.payload["base_sha"]
    (change,) = candidate.evidence["changes"]
    assert change["status"] == "changed"
    assert change["old_sha"] != change["new_sha"]
    assert "New contract." in change["excerpt"]
    assert change["main_sha"] == _git(changed_repo, "rev-parse", "main")
    (operation,) = candidate.payload["ops"]
    assert body[slice(*operation["span"])] == operation["find"]
    assert operation["text"] is None
    assert candidate.payload["not_checked"] == []
    assert store.reviews_for("ref", target) == []
    assert cand._memory_bodies(store, [target])[0][1] == body
    assert candidate.key != replace(candidate, detector="entity_claim_stale").key
    changed_evidence = {**candidate.payload, "evidence_sha": "different-source-sha"}
    assert candidate.key != replace(candidate, payload=changed_evidence).key


@pytest.mark.parametrize("detector", DETECTORS)
def test_unrelated_commit_preserves_drift_key(
    store: Store, changed_repo: Path, detector: str
) -> None:
    body = (
        "Source: docs/guide.md § Contract"
        if detector == "source_drift"
        else "docs/guide.md § Contract is live."
    )
    _memory(store, body)
    detect = getattr(cand, f"{detector}_candidates")
    (before,) = detect(store)
    path = changed_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Other rule.", "Unrelated edit."),
        encoding="utf-8",
    )
    current = _commit(changed_repo, "2026-01-04T00:00:00Z")
    (after,) = detect(store)
    assert before.evidence["changes"][0]["main_sha"] != current
    assert after.evidence["changes"][0]["main_sha"] == current
    assert after.key == before.key
    assert after.payload["evidence_sha"] == before.payload["evidence_sha"]

    path.write_text(
        path.read_text(encoding="utf-8").replace("New contract.", "Revised contract."),
        encoding="utf-8",
    )
    _commit(changed_repo, "2026-01-05T00:00:00Z")
    (revised,) = detect(store)
    assert revised.key != after.key


@pytest.mark.parametrize("gap", ["\n", "\n\n", "\n \t\n"])
def test_source_evidence_includes_claim_after_long_background(
    store: Store, changed_repo: Path, gap: str
) -> None:
    claim = "The old contract requires one route."
    body = "B" * 650 + "\n\n" + claim + gap + "Source: docs/guide.md § Contract"
    _memory(store, body)
    (candidate,) = cand.source_drift_candidates(store)
    (change,) = candidate.evidence["changes"]
    assert change["memory_excerpt"] == claim
    assert body[slice(*change["span"])] == change["anchor"]


def test_source_memory_excerpt_is_capped(store: Store, changed_repo: Path) -> None:
    claim = "The old contract requires one route."
    _memory(
        store,
        "B" * (cand.DRIFT_MEMORY_EXCERPT_CAP + 1)
        + claim
        + "\nSource: docs/guide.md § Contract",
    )
    (candidate,) = cand.source_drift_candidates(store)
    excerpt = candidate.evidence["changes"][0]["memory_excerpt"]
    assert excerpt.endswith(claim)
    assert len(excerpt) == cand.DRIFT_MEMORY_EXCERPT_CAP


def test_exact_source_heading_wins_over_prefix(
    store: Store, drift_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = drift_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## Contract details\nOld details.\n",
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-01T12:00:00Z")
    path.write_text(
        path.read_text(encoding="utf-8").replace("Old details.", "New details."),
        encoding="utf-8",
    )
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    monkeypatch.setattr(
        cand, "RepoDriftResolver", lambda: RepoDriftResolver(drift_repo)
    )
    _memory(
        store,
        "The details require one route.\nSource: docs/guide.md § Contract details",
    )
    (candidate,) = cand.source_drift_candidates(store)
    (change,) = candidate.evidence["changes"]
    assert change["heading"] == "Contract details"
    assert "New details." in change["excerpt"]
    assert "Old rule." not in change["excerpt"]
    assert candidate.payload["not_checked"] == []


def test_cites_edge_names_changed_section(store: Store, changed_repo: Path) -> None:
    target = _memory(store, "The source contract requires one route.")
    source = store.insert_ref(kind="markdown", slug="source-contract", title="Contract")
    link = store.add_link(
        src_ref_id=target,
        dst_ref_id=source.id,
        relation="cites",
        meta={"source_handle": "docs/guide.md § Contract"},
    )
    (candidate,) = cand.source_drift_candidates(store)
    assert candidate.target_id == target
    assert candidate.evidence["changes"][0]["link_id"] == link.id
    assert "New contract." in candidate.evidence["changes"][0]["excerpt"]
    assert candidate.payload["ops"] == []


@pytest.mark.parametrize("background_size", [650, 2000])
def test_cites_evidence_includes_claim_after_long_background(
    store: Store, changed_repo: Path, background_size: int
) -> None:
    claim = "The old contract requires one route."
    body = "B" * background_size + "\n\n" + claim
    target = _memory(store, body)
    source = store.insert_ref(kind="markdown", slug="source-contract", title="Contract")
    store.add_link(
        src_ref_id=target,
        dst_ref_id=source.id,
        relation="cites",
        meta={"source_handle": "docs/guide.md § Contract"},
    )
    (candidate,) = cand.source_drift_candidates(store)
    (change,) = candidate.evidence["changes"]
    assert claim in change["memory_excerpt"]
    assert change["memory_excerpt"] == body[600:][-cand.DRIFT_MEMORY_EXCERPT_CAP :]
    assert candidate.payload["ops"] == []


def test_unchanged_source_and_non_source_mention_give_no_candidate(
    store: Store, changed_repo: Path
) -> None:
    _memory(store, "Source: docs/guide.md § Other")
    _memory(store, "See docs/guide.md § Contract")
    assert cand.source_drift_candidates(store) == []


def test_missing_prior_source_is_not_checked_alongside_verified_change(
    store: Store, changed_repo: Path
) -> None:
    path = changed_repo / "docs/guide.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## New section\nNew.\n", encoding="utf-8"
    )
    _commit(changed_repo, "2026-01-04T00:00:00Z")
    _memory(store, "Source: docs/guide.md § Contract; docs/guide.md § New section")
    (candidate,) = cand.source_drift_candidates(store)
    assert len(candidate.evidence["changes"]) == 1
    (unchecked,) = candidate.payload["not_checked"]
    assert unchecked["status"] == "not_checked"
    assert unchecked["heading"] == "New section"
    assert "old_sha" not in unchecked


def test_only_missing_baseline_emits_no_finding(
    store: Store, changed_repo: Path
) -> None:
    _memory(
        store,
        "Source: docs/guide.md § Contract",
        touched=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert cand.source_drift_candidates(store) == []


def _changed_machine(store: Store) -> int:
    machine = store.insert_ref(
        kind="machine", slug="ma_fixture", title="Old role", meta={"role": "retired"}
    )
    old_sha = store.target_sha("ref", machine.id)
    assert old_sha is not None
    store.update_ref(machine.id, title="Current role", meta_patch={"role": "compute"})
    # Machine history is not enabled in the default registry. This fixture
    # supplies a recorded transition without changing that registry or triggers.
    with store.tx() as conn:
        conn.execute(
            "UPDATE revisions SET at = %s WHERE target_kind = 'ref' AND target_id = %s",
            (TOUCHED - timedelta(days=1), machine.id),
        )
        conn.execute(
            "INSERT INTO revisions (target_kind, target_id, at, event, actor, reason, "
            "prev_sha, prev_state) VALUES ('ref', %s, %s, 'edited', 'system', "
            "'recorded machine transition fixture', %s, %s)",
            (machine.id, AFTER, old_sha, Jsonb({"meta": {"role": "retired"}})),
        )
    return int(machine.id)


def test_machine_role_change_uses_recorded_prior_state(store: Store) -> None:
    machine = _changed_machine(store)
    body = f"mh{machine} is PERMANENTLY retired."
    target = _memory(store, body)
    result = ref_change(store, f"mh{machine}", TOUCHED)
    assert result["status"] == "changed"
    assert result["changes"]["role"] == {"old": "retired", "new": "compute"}
    (candidate,) = cand.entity_claim_stale_candidates(store)
    assert candidate.target_id == target
    assert candidate.payload["base_sha"] == store.target_sha("ref", target)
    assert (
        candidate.evidence["changes"][0]["changes"]["role"] == result["changes"]["role"]
    )
    assert cand._memory_bodies(store, [target])[0][1] == body
    assert store.reviews_for("ref", target) == []


def test_machine_without_history_is_not_checked(store: Store) -> None:
    machine = store.insert_ref(
        kind="machine", slug="ma_unrecorded", title="Machine", meta={"role": "compute"}
    )
    assert ref_change(store, f"mh{machine.id}", TOUCHED)["status"] == "not_checked"
    _memory(store, f"mh{machine.id} is retired.")
    assert cand.entity_claim_stale_candidates(store) == []


@pytest.mark.parametrize(
    "body", ["mh{machine} was retired.", "See mh{machine}. It is live."]
)
def test_entity_needs_state_verb_in_same_sentence(store: Store, body: str) -> None:
    machine = _changed_machine(store)
    _memory(store, body.format(machine=machine))
    assert cand.entity_claim_stale_candidates(store) == []


def test_entity_not_checked_is_separate_from_verified_role(store: Store) -> None:
    changed = _changed_machine(store)
    unknown = store.insert_ref(kind="todo", slug=None, title="Unrecorded task")
    _memory(store, f"mh{changed} is retired. td{unknown.id} is open.")
    (candidate,) = cand.entity_claim_stale_candidates(store)
    assert len(candidate.evidence["changes"]) == 1
    (unchecked,) = candidate.payload["not_checked"]
    assert unchecked["anchor"] == f"td{unknown.id}"
    assert "prior SHA" in unchecked["reason"]


@pytest.mark.parametrize("recorded", [False, True])
def test_status_change_requires_a_recorded_prior_status(
    store: Store, recorded: bool
) -> None:
    task = store.insert_ref(kind="todo", slug=None, title="Task")
    store.add_tag(task.id, Tag.closed("STATUS", "open"), set_by="system")
    if recorded:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_events (ref_id, source, event, ts) "
                "VALUES (%s, 'fixture', 'status:done', %s)",
                (task.id, AFTER),
            )
    result = ref_change(store, f"td{task.id}", TOUCHED)
    if recorded:
        assert result["status"] == "changed"
        assert result["changes"]["STATUS"]["old"] == "done"
        assert result["changes"]["STATUS"]["new"] == "open"
    else:
        assert result["status"] == "not_checked"
        assert "prior STATUS unavailable" in result["reason"]


def test_numbered_section_keeps_state_verb_in_sentence() -> None:
    body = "docs/guide.md § 1. Setup is live."
    (entity,) = list(cand._state_entities(body))
    assert entity["kind"] == "section"
    assert entity["target"] == "docs/guide.md"
    assert entity["detail"] == "1. Setup is live."
    assert entity["context"] == body


def test_state_sentence_joins_soft_line_break() -> None:
    body = "mh123 is\nretired."
    (entity,) = list(cand._state_entities(body))
    assert entity["target"] == "mh123"
    assert entity["context"] == "mh123 is retired."
    assert body[slice(*entity["span"])] == "mh123"


@pytest.mark.parametrize(
    "gap", ["\n\n", "\n \t\n", "\n- ", "\n* ", "\n+ ", "\n1. ", "\n1) "]
)
def test_state_sentences_keep_paragraphs_and_list_items_separate(gap: str) -> None:
    assert list(cand._state_entities(f"mh123{gap}is retired.")) == []
    body = f"Background{gap}mh123 is\nretired."
    (entity,) = list(cand._state_entities(body))
    assert body[slice(*entity["span"])] == "mh123"


def test_skill_slug_without_ref_uses_committed_file(
    store: Store, drift_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "precis-fixture-help"
    relative = f"src/precis/data/skills/{slug}.md"
    path = drift_repo / relative
    path.parent.mkdir(parents=True)
    path.write_text("# Contract\nRuns router A.\n", encoding="utf-8")
    _git(drift_repo, "add", relative)
    _commit(drift_repo, "2026-01-01T12:00:00Z")
    path.write_text("# Contract\nRuns router B.\n", encoding="utf-8")
    _git(drift_repo, "add", relative)
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    monkeypatch.setattr(
        cand, "RepoDriftResolver", lambda: RepoDriftResolver(drift_repo)
    )
    assert ref_change(store, slug, TOUCHED)["reason"] == "entity unavailable"
    target = _memory(store, "precis-fixture-help runs router A.")
    (candidate,) = cand.entity_claim_stale_candidates(store)
    assert candidate.target_id == target
    (change,) = candidate.evidence["changes"]
    assert change["status"] == "changed"
    assert change["kind"] == "skill"
    assert change["basis"] == "committed_skill_document"
    assert "Runs router B." in change["excerpt"]
    assert candidate.payload["not_checked"] == []


def test_skill_contract_uses_committed_file_fallback(
    store: Store, drift_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = drift_repo / "src/precis/data/skills"
    folder.mkdir(parents=True)
    path = folder / "precis-fixture-help.md"
    path.write_text("# Contract\nOld contract.\n", encoding="utf-8")
    _git(drift_repo, "add", "src/precis/data/skills/precis-fixture-help.md")
    _commit(drift_repo, "2026-01-01T12:00:00Z")
    path.write_text("# Contract\nNew contract.\n", encoding="utf-8")
    _git(drift_repo, "add", "src/precis/data/skills/precis-fixture-help.md")
    _commit(drift_repo, "2026-01-03T00:00:00Z")
    monkeypatch.setattr(
        cand, "RepoDriftResolver", lambda: RepoDriftResolver(drift_repo)
    )
    skill = store.insert_ref(kind="skill", slug="precis-fixture-help", title="Contract")
    target = _memory(store, f"sk{skill.id} is the only supported contract.")
    (candidate,) = cand.entity_claim_stale_candidates(store)
    assert candidate.target_id == target
    assert candidate.evidence["changes"][0]["status"] == "changed"
    assert "New contract." in candidate.evidence["changes"][0]["excerpt"]


def test_memory_review_is_opt_in_and_accepted_by_job_schema() -> None:
    from precis.workers.job_types.graph_garden import PARAMS_SCHEMA

    expected = set(DETECTORS)
    assert expected <= set(cand.detectors_for(("memory-review",)))
    assert expected.isdisjoint(cand.detectors_for(("all",)))
    assert expected.isdisjoint(cand.detectors_for(("memory",)))
    assert expected <= set(cand.detectors_for(("all", "memory-review")))
    options = PARAMS_SCHEMA["properties"]["pass"]["anyOf"]
    assert "memory-review" in options[0]["enum"]
    assert "memory-review" in options[1]["items"]["enum"]


@pytest.mark.parametrize("detector", DETECTORS)
def test_drift_cursor_limit_and_match_cap(
    store: Store, changed_repo: Path, detector: str
) -> None:
    source = "Source: docs/guide.md § Contract"
    claim = "docs/guide.md § Contract is live."
    body = source if detector == "source_drift" else claim
    targets = [_memory(store, body) for _ in range(3)]
    function = getattr(cand, f"{detector}_candidates")
    cursor: dict[str, int] = {}
    seen: list[int] = []
    for _ in targets:
        found = function(
            store, limit=1, scan_cap=2, cursors=cursor, next_cursors=cursor
        )
        seen.extend(candidate.target_id for candidate in found)
    assert seen == targets
    assert function(store, cursors=cursor, next_cursors=cursor) == []
    assert cursor[f"{detector}:memory"] == 0
    capped = _memory(store, "\n".join([body] * (cand.DRIFT_MATCH_CAP + 1)))
    (candidate,) = function(store, cursors={f"{detector}:memory": targets[-1]})
    assert candidate.target_id == capped
    assert len(candidate.evidence["changes"]) == cand.DRIFT_MATCH_CAP
    assert candidate.payload["matches_capped"] is True


@pytest.mark.parametrize("detector", DETECTORS)
def test_drift_excludes_dreams_retired_memories_and_empty_budgets(
    store: Store, changed_repo: Path, detector: str
) -> None:
    body = "Source: docs/guide.md § Contract\ndocs/guide.md § Contract is live."
    retired = _memory(store, body)
    store.retire_ref(retired)
    dream = _memory(store, body)
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    function = getattr(cand, f"{detector}_candidates")
    assert function(store) == []
    _memory(store, body)
    assert function(store, limit=0) == []
    assert function(store, scan_cap=0) == []


@pytest.mark.parametrize("detector", DETECTORS)
def test_drift_timeout_preserves_cursor(
    store: Store, monkeypatch: pytest.MonkeyPatch, detector: str
) -> None:
    def cancelled(*args: Any, **kwargs: Any) -> list[cand.GardenCandidate]:
        kwargs["next_cursors"][f"{detector}:memory"] = 99
        raise QueryCanceled("fixture timeout")

    monkeypatch.setattr(cand, f"{detector}_candidates", cancelled)
    run = cand.run_detectors(
        store, passes=("memory-review",), detector_deadline_s=0, considered_days=0
    )
    assert detector in run.skipped
    assert f"{detector}:memory" not in run.cursors


@pytest.mark.parametrize("detector", DETECTORS)
def test_drift_proposer_is_memory_only(detector: str) -> None:
    candidate = cand.GardenCandidate(
        detector=detector, action="rewrite", target_kind="ref", target_id=1
    )
    assert proposer.MEMORY_ACTIONS[detector] == ("rewrite",)
    prompt = proposer._prompt_for(candidate)
    assert "not_checked" in prompt
    assert '"action": "rewrite"' in prompt
    assert proposer.PROMPT_VERSION == "garden-propose.v2"
    assert proposer.MEMORY_PROMPT_VERSION != proposer.PROMPT_VERSION
