"""Bounded memory consumer retries and stale-proposal recovery on PostgreSQL."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import OperationalError
from psycopg.errors import QueryCanceled

from precis.cli import garden as garden_cli
from precis.graph.garden import apply as doors
from precis.graph.garden import candidates, consumer
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    key_block,
    pending_proposals,
)
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import Tag
from tests.test_garden_memory_apply import _approve, _body, _memory, _proposal, _reviews


def _never_review(_store: Store, _prop: GardenProposal) -> consumer.ConsumerVerdict:
    pytest.fail("This proposal must not consume a model review")


@pytest.mark.parametrize("unresolved", [False, True])
def test_edited_target_closes_pending_key_retryably(
    store: Store, monkeypatch: pytest.MonkeyPatch, unresolved: bool
) -> None:
    def unresolved_op(payload: dict[str, Any]) -> None:
        if unresolved:
            payload["ops"][0]["text"] = None

    prop = _proposal(store, change_payload=unresolved_op)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(
            prop.target, prop.body + "New context.\n", chunk_kind="memory_body"
        )
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "0")
    off = consumer.consume_proposals(store, review=_never_review)
    assert off.rejected == 0
    assert {p.review_id for p in pending_proposals(store)} == {prop.review_id}

    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    report = consumer.consume_proposals(store, review=_never_review)
    assert (report.attempts, report.rejected) == (0, 1)
    assert report.reasons == {"stale": 1}
    assert key_block(store, "ref", prop.target, prop.candidate.key) is None
    assert pending_proposals(store) == []
    fresh = next(
        c
        for c in candidates.dangling_mention_candidates(store)
        if c.target_id == prop.target
    )
    assert emit_proposal(store, fresh, rationale="Review the edited target") is not None


@pytest.mark.parametrize("unresolved", ["one", "all"])
def test_unresolved_rewrite_releases_resolved_ops_or_holds_after_age_bound(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unresolved: str,
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")

    def unresolved_ops(payload: dict[str, Any]) -> None:
        for op in payload["ops"][: 1 if unresolved == "one" else None]:
            op["text"] = None

    prop = _proposal(store, change_payload=unresolved_ops)
    young = consumer.consume_proposals(store, review=_never_review)
    assert (young.held, young.attempts) == (0, 0)
    pending = pending_proposals(store)[0]
    old = replace(
        pending,
        at=datetime.now(UTC) - consumer.MAX_UNRESOLVED_AGE - timedelta(seconds=1),
    )
    report = consumer.ConsumeReport()
    consumer.review_pending(store, [old], report, review=_never_review, limit=10)
    if unresolved == "one":
        assert (report.rejected, report.held, report.attempts, report.applied) == (
            1,
            0,
            0,
            0,
        )
        assert report.reasons == {"unresolved": 1}
        replacement = pending_proposals(store)[0]
        assert replacement.review_id != prop.review_id
        assert replacement.key == old.key
        assert replacement.evidence == old.evidence
        assert replacement.payload == {
            **old.payload,
            "ops": old.payload["ops"][1:],
        }
        assert _body(store, prop.target) == prop.body
        applied = consumer.consume_proposals(store, review=_approve)
        assert (applied.attempts, applied.reviewed, applied.applied) == (1, 1, 1)
        op = replacement.payload["ops"][0]
        start, end = op["span"]
        assert (
            _body(store, prop.target)
            == prop.body[:start] + op["text"] + prop.body[end:]
        )
        unresolved_op = old.payload["ops"][0]
        start, end = unresolved_op["span"]
        assert _body(store, prop.target)[start:end] == unresolved_op["find"]
        assert pending_proposals(store) == []
        return
    assert (report.held, report.attempts, report.applied) == (1, 0, 0)
    consumer.review_pending(store, [old], report, review=_never_review, limit=10)
    assert report.held == 1
    assert _body(store, prop.target) == prop.body
    garden_cli._proposals(argparse.Namespace(action=None, limit=20), store)
    output = capsys.readouterr().out
    assert "held: unresolved" in output
    assert "replacement text remains unresolved" in output
    assert "reject" in output


def test_splitting_unresolved_proposal_rolls_back_if_replacement_write_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")

    def unresolved_op(payload: dict[str, Any]) -> None:
        payload["ops"][0]["text"] = None

    prop = _proposal(store, change_payload=unresolved_op)
    old = replace(
        pending_proposals(store)[0],
        at=datetime.now(UTC) - consumer.MAX_UNRESOLVED_AGE - timedelta(seconds=1),
    )
    before = _reviews(store, prop.target)

    def fail_emit(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("Replacement persistence unavailable")

    monkeypatch.setattr(consumer, "emit_proposal", fail_emit)
    report = consumer.ConsumeReport()
    consumer.review_pending(store, [old], report, review=_never_review, limit=10)
    assert (report.errors, report.rejected, report.attempts) == (1, 0, 0)
    assert _reviews(store, prop.target) == before
    assert {p.review_id for p in pending_proposals(store)} == {prop.review_id}


def test_splitting_unresolved_proposal_rechecks_target_after_pending_read(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")

    def unresolved_op(payload: dict[str, Any]) -> None:
        payload["ops"][0]["text"] = None

    prop = _proposal(store, change_payload=unresolved_op)
    old = replace(
        pending_proposals(store)[0],
        at=datetime.now(UTC) - consumer.MAX_UNRESOLVED_AGE - timedelta(seconds=1),
    )
    assert old.current
    changed = prop.body + "New context.\n"
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        store.chunks.replace_body_chunk(prop.target, changed, chunk_kind="memory_body")
    report = consumer.ConsumeReport()
    consumer.review_pending(store, [old], report, review=_never_review, limit=10)
    assert report.reasons == {"stale": 1}
    assert report.errors == report.attempts == 0
    assert pending_proposals(store) == []
    assert key_block(store, "ref", prop.target, prop.candidate.key) is None
    assert _body(store, prop.target) == changed


def test_query_canceled_is_held_at_durable_failure_cap(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store)
    attempts = 0
    calls: list[int] = []

    def fail_apply(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise QueryCanceled("statement timeout while saving the rewrite")

    def approve(
        review_store: Store, proposal: GardenProposal
    ) -> consumer.ConsumerVerdict:
        calls.append(proposal.review_id)
        return _approve(review_store, proposal)

    monkeypatch.setattr(doors, "apply_rewrite", fail_apply)
    reports = [
        consumer.consume_proposals(store, review=approve)
        for _ in range(consumer.MAX_MEMORY_APPLY_FAILURES + 2)
    ]
    assert attempts == consumer.MAX_MEMORY_APPLY_FAILURES
    assert calls == [prop.review_id]
    assert sum(r.held for r in reports) == 1
    assert sum(r.errors for r in reports) == consumer.MAX_MEMORY_APPLY_FAILURES
    assert reports[-1].as_meta() == consumer.ConsumeReport().as_meta()
    attempts_recorded = [
        json.loads(row[3])["garden_memory_attempt"]
        for row in _reviews(store, prop.target)
        if "garden_memory_attempt" in json.loads(row[3])
    ]
    assert len(attempts_recorded) == consumer.MAX_MEMORY_APPLY_FAILURES
    assert all(a["proposal"] == prop.review_id for a in attempts_recorded)
    assert _body(store, prop.target) == prop.body
    garden_cli._proposals(argparse.Namespace(action=None, limit=20), store)
    output = capsys.readouterr().out
    assert "held: apply_error" in output
    assert "QueryCanceled: statement timeout" in output
    assert f"{consumer.MAX_MEMORY_APPLY_FAILURES} failed apply attempts" in output


def test_approval_save_failures_stop_at_consecutive_error_cap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    props = [_proposal(store) for _ in range(consumer.MAX_CONSECUTIVE_ERRORS + 2)]

    def fail_save(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("Approval persistence unavailable")

    monkeypatch.setattr(consumer, "_note_approved_memory", fail_save)
    report = consumer.consume_proposals(store, review=_approve, review_limit=20)
    assert (report.attempts, report.reviewed, report.errors) == (
        consumer.MAX_CONSECUTIVE_ERRORS,
        consumer.MAX_CONSECUTIVE_ERRORS,
        consumer.MAX_CONSECUTIVE_ERRORS,
    )
    assert report.applied == 0
    assert {p.review_id for p in pending_proposals(store)} == {
        p.review_id for p in props
    }
    assert all(_body(store, p.target) == p.body for p in props)


def test_failed_cap_hold_never_retries_apply_past_bound(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store)
    attempts = 0
    hold = consumer._hold_for_human

    def fail_apply(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise QueryCanceled("Persistent apply timeout")

    def fail_hold(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("Hold persistence unavailable")

    monkeypatch.setattr(doors, "apply_rewrite", fail_apply)
    monkeypatch.setattr(consumer, "_hold_for_human", fail_hold)
    reports = [
        consumer.consume_proposals(store, review=_approve)
        for _ in range(consumer.MAX_MEMORY_APPLY_FAILURES + 2)
    ]
    assert attempts == consumer.MAX_MEMORY_APPLY_FAILURES
    assert sum(r.reviewed for r in reports) == 1
    assert sum(r.held for r in reports) == 0
    assert reports[-1].errors == 1
    monkeypatch.setattr(consumer, "_hold_for_human", hold)
    recovered = consumer.consume_proposals(store, review=_never_review)
    assert (recovered.held, recovered.applied, recovered.errors) == (1, 0, 0)
    assert attempts == consumer.MAX_MEMORY_APPLY_FAILURES
    assert _body(store, prop.target) == prop.body
    idle = consumer.consume_proposals(store, review=_never_review)
    assert idle.as_meta() == consumer.ConsumeReport().as_meta()


def test_detector_action_drift_reopens_retire_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store, "retire")
    successor = _memory(store, "Left: finish campaign\nEarlier: campaign started.")
    store.add_tag(successor, Tag.open("section:threads"), set_by="system")
    link = store.add_link(
        src_ref_id=prop.target, dst_ref_id=successor, relation="part-of"
    )
    pending = pending_proposals(store)[0]
    assert consumer._memory_action_mismatch(store, pending) == "stale"
    report = consumer.consume_proposals(store, review=_never_review)
    assert report.reasons == {"stale": 1}
    assert key_block(store, "ref", prop.target, prop.candidate.key) is None
    with store.tx() as conn:
        store.delete_links([int(link.id)], conn=conn)
    fresh = next(
        c
        for c in candidates.thread_shape_candidates(store)
        if c.target_id == prop.target
    )
    assert fresh.action == "retire"
    assert fresh.key == prop.candidate.key
    assert (
        emit_proposal(store, fresh, rationale="Ancestor no longer exists") is not None
    )
    assert _body(store, prop.target) == prop.body


def test_exact_evidence_action_downgrade_is_terminal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store, "supersede")
    pending = pending_proposals(store)[0]
    altered = replace(pending, action="retire")
    assert consumer._memory_action_mismatch(store, altered) == "apply_refused"
    report = consumer.ConsumeReport()
    consumer.review_pending(store, [altered], report, review=_never_review, limit=10)
    assert report.reasons == {"apply_refused": 1}
    assert key_block(store, "ref", prop.target, prop.candidate.key) == "rejected"
    assert _body(store, prop.target) == prop.body
