"""source-backfill slice 8b.1 — the heading-intent substrate.

A durable teleological note per heading: a ``memory`` ref anchored to the heading
by ``meta.anchor`` with its strength in ``meta.heading_intent``. Verifies
set/read/upsert/retire, the prune-dangling hygiene heal, and the non-export
guarantee. Uses ``plan`` docs (a DraftMixin tree kind) like ``test_backfill``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.backfill.heading_intent import (
    HARD,
    SOFT,
    Intent,
    intents_for,
    intents_for_draft,
    prune_dangling,
    retire_intent,
    set_intent,
)
from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


def _doc_with_heading(
    store: Any, plan: PlanHandler, title: str = "A section"
) -> tuple[int, str]:
    """A plan doc with one heading. Returns ``(draft_ref_id, heading_handle)``."""
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    h = _pe(plan.put(id="p", chunk_kind="heading", text=title, at={"last": True}).body)
    draft_ref_id = store.get_draft_chunk(h, kind="plan").ref_id
    return draft_ref_id, h


def test_set_read_and_upsert(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    draft, h = _doc_with_heading(store, plan)

    rid = set_intent(store, h, "This section exists to motivate the problem.")
    got = intents_for(store, [h])
    assert h in got
    i = got[h]
    assert isinstance(i, Intent)
    assert i.ref_id == rid
    assert i.hard is False and i.strength == SOFT
    assert "motivate the problem" in i.text

    # upsert: the same heading updates in place (one intent per heading, no dup),
    # flips soft→hard, and rewrites the body.
    rid2 = set_intent(store, h, "This section MUST establish the baseline.", hard=True)
    assert rid2 == rid
    i2 = intents_for(store, [h])[h]
    assert i2.hard is True and i2.strength == HARD
    assert "establish the baseline" in i2.text
    assert "motivate the problem" not in i2.text  # body replaced, not appended

    # draft-scoped read keys intents by heading handle
    by_draft = intents_for_draft(store, draft, kind="plan")
    assert h in by_draft and by_draft[h].ref_id == rid


def test_body_is_recallable_chunk(hub: Hub, plan: PlanHandler) -> None:
    """The intent prose lands in a ``memory_body`` chunk (the embed source), so it
    is searchable/recallable — not stranded in the title."""
    store = hub.store
    _draft, h = _doc_with_heading(store, plan)
    rid = set_intent(store, h, "grain-boundary blocking dominates below 30C")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'memory_body'",
            (rid,),
        ).fetchone()
    assert row is not None and "grain-boundary blocking" in row[0]


def test_retire(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    _draft, h = _doc_with_heading(store, plan)
    rid = set_intent(store, h, "some intent")
    assert intents_for(store, [h]) != {}
    retire_intent(store, rid)
    assert intents_for(store, [h]) == {}


def test_prune_dangling_reaps_orphans_only(hub: Hub, plan: PlanHandler) -> None:
    """An intent whose heading still resolves survives; one anchored to a vanished
    heading (the DELETE+INSERT-orphan case) is retired by the heal."""
    store = hub.store
    _draft, h = _doc_with_heading(store, plan)
    live = set_intent(store, h, "live intent")
    dead = set_intent(store, "pe999999", "orphan intent")  # anchor never resolves

    retired = prune_dangling(store)
    assert dead in retired
    assert live not in retired
    assert intents_for(store, [h]) != {}  # the live one is untouched


def test_intent_note_is_not_exportable(hub: Hub, plan: PlanHandler) -> None:
    """Belt to the anchor-not-a-chunk suspenders: a heading-intent memory is
    rejected by the export guard, so it can never leave as a document."""
    from precis.errors import BadInput
    from precis.export import guard_exportable

    store = hub.store
    _draft, h = _doc_with_heading(store, plan)
    rid = set_intent(store, h, "never exported")
    ref = store.fetch_refs_by_ids([rid])[rid]
    with pytest.raises(BadInput):
        guard_exportable(ref)
