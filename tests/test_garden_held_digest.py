"""The held-proposal digest — judge-surviving holds reach Reto (gr739890).

Ruled (Reto, Claude voice, 2026-11-20): the judge reviews held proposals
like any other; the survivors reach Reto as a single linked digest, not
one ask per proposal; he accepts or rejects per item, an accept counts
as a ruling and is what lets the apply step run on a human-origin node.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.cli import garden
from precis.graph.garden import apply as doors
from precis.graph.garden import consumer
from precis.graph.garden.consumer import ConsumerVerdict, consume_proposals
from precis.graph.garden.digest import (
    DECISION_HOLDS,
    DIGEST_TAG,
    held_decision_items,
    maybe_mint_digest,
)
from precis.graph.garden.proposals import (
    GardenProposal,
    pending_proposals,
    proposal_holds,
)
from precis.store import Store
from precis.store.revision_context import revision_context
from precis.store.types import Tag
from tests.test_garden_memory_apply import (
    _approve,
    _body,
    _proposal,
    _reviews,
)


def _reject(_store: Store, _prop: GardenProposal) -> ConsumerVerdict:
    return ConsumerVerdict(
        verdict="reject",
        reason="insufficient_evidence",
        why="the evidence does not support the action",
        model="fixture-reviewer",
    )


def _todo_body(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s ORDER BY ord, chunk_id",
            (ref_id,),
        ).fetchall()
    return "\n".join(str(row[0]) for row in rows)


def _digest_ids(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "WHERE r.kind = 'todo' AND r.retired_at IS NULL "
            "  AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
            "              ON t.tag_id = rt.tag_id "
            "              WHERE rt.ref_id = r.ref_id "
            "                AND t.namespace = 'OPEN' AND t.value = %s) "
            "ORDER BY r.ref_id",
            (DIGEST_TAG,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def _close_todo(store: Store, ref_id: int) -> None:
    store.add_tag(
        ref_id, Tag.parse("STATUS:done"), set_by="system", replace_prefix=True
    )


def _hold_one(store: Store, origin: str = "human") -> Any:
    """A protected-memory rewrite proposal the consumer reviews and holds."""
    prop = _proposal(store, "rewrite", origin=origin)
    report = consume_proposals(store, review=_approve)
    assert (report.held, report.applied) == (1, 0)
    return prop


def _pending(store: Store, review_id: int) -> GardenProposal:
    return next(p for p in pending_proposals(store) if p.review_id == review_id)


def _hold_directly(store: Store, review_id: int, reason: str) -> None:
    consumer._hold_for_human(
        store, _pending(store, review_id), reason=reason, why=reason, identity=None
    )


def test_decision_holds_cover_human_and_paper_gate() -> None:
    assert "human" in DECISION_HOLDS
    assert "title_author_only" in DECISION_HOLDS
    assert "apply_error" not in DECISION_HOLDS
    assert "unresolved" not in DECISION_HOLDS
    assert "explicit_apply" not in DECISION_HOLDS


def test_consume_holds_survivor_and_mints_walkable_digest(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _hold_one(store)
    report = consume_proposals(store, review=_approve)
    # Nothing new to hold: the pass is empty and no second digest mints.
    assert (report.held, report.attempts, report.digest_todo) == (0, 0, None)

    (todo_id,) = _digest_ids(store)
    body = _todo_body(store, todo_id)
    node = f"me{prop.target}"
    assert "1 held garden proposal(s) need your ruling" in body
    assert f"## 1. rewrite {node}" in body
    assert f"get(kind='memory', id='{node}')" in body
    assert f"proposal: review {prop.review_id}" in body
    assert "judge: approve —" in body
    assert "recommend: accept" in body
    assert "diff:" in body
    assert "evidence:" in body
    assert f"precis garden accept {prop.review_id}" in body
    assert f"precis garden reject {prop.review_id}" in body

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (todo_id,)
        ).fetchone()
    meta = row[0] if row is not None else None
    assert isinstance(meta, dict)
    (item,) = meta["items"]
    assert item["review_id"] == prop.review_id
    assert item["node"] == node
    assert item["verdict"] == "approve"
    # One parked todo, not an ask-user flood item (gr738860).
    assert "llm_tier" not in meta
    tags = {t.value for t in store.tags_for(todo_id)}
    assert DIGEST_TAG in tags
    assert not any(t == "ask-user" or t.startswith("ask-user:") for t in tags)


def test_digest_not_reminted_while_open_digest_lists_live_item(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    _hold_one(store)
    assert len(_digest_ids(store)) == 1
    assert maybe_mint_digest(store) is None
    later = datetime.now(UTC) + timedelta(hours=48)
    assert maybe_mint_digest(store, now=later) is None


def test_digest_rate_capped_to_one_per_day(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _hold_one(store)
    (first,) = _digest_ids(store)
    _close_todo(store, first)

    soon = datetime.now(UTC) + timedelta(hours=1)
    assert maybe_mint_digest(store, now=soon) is None
    # The item is still held (Reto never answered) — a later day re-lists it.
    later = datetime.now(UTC) + timedelta(hours=25)
    second = maybe_mint_digest(store, now=later)
    assert second is not None and second != first
    body = _todo_body(store, second)
    assert f"proposal: review {prop.review_id}" in body


def test_spent_open_digest_does_not_block_the_next_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    first_prop = _hold_one(store)
    second_prop = _hold_one(store)
    assert len(_digest_ids(store)) == 1

    # Walk the open digest: answer both items (reject is enough).
    garden._reject(
        argparse.Namespace(review_id=str(first_prop.review_id), by="reto"),
        store,
    )
    garden._reject(
        argparse.Namespace(review_id=str(second_prop.review_id), by="reto"),
        store,
    )
    assert held_decision_items(store) == []

    # A fresh hold after the cap: a new digest mints even though the old
    # one was never closed — it lists no live items any more.
    third_prop = _proposal(store, "rewrite", origin="human")
    report = consume_proposals(store, review=_approve)
    assert (report.held, report.digest_todo) == (1, None)  # same-day cap
    later = datetime.now(UTC) + timedelta(hours=25)
    third = maybe_mint_digest(store, now=later)
    assert third is not None
    body = _todo_body(store, third)
    assert f"proposal: review {third_prop.review_id}" in body
    assert f"proposal: review {first_prop.review_id}" not in body


def test_legacy_hold_without_judge_verdict_names_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store, "rewrite", origin="human")
    _hold_directly(store, prop.review_id, "human")

    todo_id = maybe_mint_digest(store)
    assert todo_id is not None
    body = _todo_body(store, todo_id)
    assert "no judge review on file" in body
    assert "recommend: none" in body

    (item_prop, hold) = held_decision_items(store)[0]
    assert item_prop.review_id == prop.review_id
    assert hold["held"] == "human"
    assert "verdict" not in hold


def test_consume_rejected_hold_never_digests(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    _proposal(store, "rewrite", origin="human")
    report = consume_proposals(store, review=_reject)
    assert (report.held, report.rejected) == (0, 1)
    assert report.digest_todo is None
    assert _digest_ids(store) == []


def test_garden_accept_rules_and_applies_held_human_origin_rewrite(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _hold_one(store)

    garden._accept(
        argparse.Namespace(
            review_id=str(prop.review_id), by="reto", why="checked the source"
        ),
        store,
    )
    out = capsys.readouterr().out
    assert "applied rewrite" in out

    # The ruling is recorded and names exactly this proposal.
    ruling = [
        json.loads(row[3])["garden_ruling"]
        for row in _reviews(store, prop.target)
        if row[3] and "garden_ruling" in row[3]
    ]
    assert ruling and ruling[0]["proposal"] == prop.review_id
    assert ruling[0]["accepted_by"] == "reto"
    assert pending_proposals(store) == []
    assert _body(store, prop.target) != prop.body

    # The ruling only admitted this proposal's apply — undo restores.
    doors.undo_rewrite(
        store, f"garden-rewrite:{prop.target}:{prop.review_id}", actor="reto"
    )
    assert _body(store, prop.target) == prop.body


def test_garden_accept_refuses_non_decision_holds(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _proposal(store, "rewrite")
    _hold_directly(store, prop.review_id, "apply_error")
    with pytest.raises(SystemExit) as exc:
        garden._accept(
            argparse.Namespace(review_id=str(prop.review_id), by="reto", why="n/a"),
            store,
        )
    assert exc.value.code == 2
    # The ruling must not be recorded for a non-decision hold.
    assert all(
        not row[3] or "garden_ruling" not in row[3]
        for row in _reviews(store, prop.target)
    )


def test_garden_accept_ruling_survives_apply_refusal(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ruling recorded before an apply-time refusal (e.g. drift) stays
    on the ledger but does not apply anything."""
    monkeypatch.setenv("PRECIS_GARDEN_MEMORY_APPLY", "1")
    prop = _hold_one(store)
    # Drift the target past the proposal's sha pin.
    with revision_context(
        "drift", actor="fixture", trust_origin="human", trust_actor_type="operator"
    ):
        store.update_ref(prop.target, title="Changed after proposal")

    with pytest.raises(SystemExit) as exc:
        garden._accept(
            argparse.Namespace(
                review_id=str(prop.review_id), by="reto", why="too late"
            ),
            store,
        )
    assert exc.value.code == 2
    assert "apply refused" in capsys.readouterr().err
    assert any(
        row[3] and "garden_ruling" in row[3] for row in _reviews(store, prop.target)
    )
    # The proposal is still pending for a fresh review.
    assert any(p.review_id == prop.review_id for p in pending_proposals(store))
    assert proposal_holds(store, [prop.review_id])
