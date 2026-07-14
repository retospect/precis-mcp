"""source-backfill slice 4 (leading edge) — the dismissed-source ledger.

A dismissed paper must (a) read back from the ledger and (b) join the Tier-0
exclude set that ``assemble`` hands recall, so a rejected hit never resurfaces —
while still not seeding a citation-graph neighbourhood (dismissed ≠ cited).
"""

from __future__ import annotations

import re

import pytest

from precis.backfill import workspace as wsmod
from precis.backfill.dismissed import (
    DISMISS_NS,
    dismiss_source,
    dismissed_ref_ids,
    resolve_source_ref_id,
)
from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.store.types import Tag


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


def test_dismiss_and_read_back(hub: Hub) -> None:
    store = hub.store
    draft = store.insert_ref(kind="todo", slug=None, title="proj").id
    paper = store.insert_ref(kind="paper", slug="kumar", title="Kumar 2021").id

    assert dismissed_ref_ids(store, draft) == set()
    dismiss_source(store, draft, paper, reason="off-topic")
    assert dismissed_ref_ids(store, draft) == {paper}
    # idempotent — a repeat dismissal folds
    dismiss_source(store, draft, paper)
    assert dismissed_ref_ids(store, draft) == {paper}
    # the reason is kept as an audit event
    assert store.events_for(draft, source="backfill", event="dismissed") != []


def test_assemble_excludes_dismissed_from_recall(
    hub: Hub, plan: PlanHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.store
    cited = store.insert_ref(kind="paper", slug="wang", title="Wang 2020")
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    sec = _pe(plan.put(id="p", text="Ionic transport", at={"last": True}).body)
    plan.put(id="p", text=f"a claim paper:{cited.id}", at={"into": sec})

    draft_ref_id = store.get_draft_chunk(sec, kind="plan").ref_id
    dropped = store.insert_ref(kind="paper", slug="kumar", title="Kumar 2021").id
    dismiss_source(store, draft_ref_id, dropped)

    captured: dict[str, object] = {}

    def fake_find(*a: object, **k: object) -> list[object]:
        captured["exclude"] = k.get("exclude_ref_ids")
        captured["seed"] = k.get("citation_seed_ref_ids")
        return []

    monkeypatch.setattr(wsmod, "find_candidates", fake_find)
    wsmod.assemble(store, hub.embedder, [sec], kind="plan")

    exclude = captured["exclude"]
    seed = captured["seed"]
    assert isinstance(exclude, set) and isinstance(seed, set)
    assert dropped in exclude  # dismissed → excluded from results
    assert cited.id in exclude  # cited → also excluded
    assert dropped not in seed  # dismissed does NOT seed the citation graph
    assert cited.id in seed  # cited seeds the neighbourhood


def test_dismissal_by_handle_form_still_sticks(hub: Hub) -> None:
    """A model that dismisses by pasting the whole ``pa<id>`` handle (or dismisses
    via ``dismiss_source`` with a handle argument) rather than the bare number must
    still suppress the candidate. The old number-only ledger silently dropped a
    handle-form value, so the dismissal never stuck and the candidate resurfaced
    every run — the non-convergence the ledger exists to prevent."""
    store = hub.store
    draft = store.insert_ref(kind="todo", slug=None, title="proj").id
    paper = store.insert_ref(kind="paper", slug="kumar", title="Kumar 2021").id

    # (a) a record handle written straight to the tag — the raw `tag` verb path the
    #     planner instruction exercises — reads back as the paper's ref_id.
    store.add_tag(draft, Tag.closed(DISMISS_NS, f"pa{paper}"))
    assert dismissed_ref_ids(store, draft) == {paper}

    # (b) dismiss_source normalises a handle argument to the canonical ref-id, and
    #     the stored tag is the bare number (uniform ledger regardless of input).
    other = store.insert_ref(kind="paper", slug="roht", title="Roht 2022").id
    dismiss_source(store, draft, f"pa{other}")
    assert dismissed_ref_ids(store, draft) == {paper, other}
    # dismiss_source stores the canonical bare number, not the handle it was given
    # (the raw-tag path in (a) keeps whatever the model wrote — readback resolves it).
    stored = {t.value for t in store.tags_for(draft) if t.prefix == DISMISS_NS}
    assert str(other) in stored and f"pa{other}" not in stored


def test_dismissal_by_chunk_handle_resolves_to_owning_ref(
    hub: Hub, plan: PlanHandler
) -> None:
    """A ``pc<id>`` chunk-handle paste (both handles ride the candidate line)
    resolves to the chunk's owning ref, so dismissing by the chunk still suppresses
    the whole source."""
    store = hub.store
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    sec = _pe(plan.put(id="p", text="Ionic transport", at={"last": True}).body)
    draft_ref_id = store.get_draft_chunk(sec, kind="plan").ref_id
    # ``sec`` is a chunk handle (``pe<id>`` here; ``pc<id>`` in prod) — it resolves
    # to the ref that owns the chunk, not the chunk id.
    assert resolve_source_ref_id(store, sec) == draft_ref_id
    assert resolve_source_ref_id(store, "definitely not a handle") is None


def test_resolve_recovers_kind_id_and_slug_forms(hub: Hub) -> None:
    """The recovery ladder is exhaustive: bare int / handle / ``kind:id`` canonical
    link-target form / ``cite_key`` slug all resolve to the same source ref, so a
    dismissal sticks no matter which form the model reached for."""
    store = hub.store
    paper = store.insert_ref(kind="paper", slug="wang2020", title="Wang 2020").id
    assert resolve_source_ref_id(store, paper) == paper  # bare int
    assert resolve_source_ref_id(store, f"pa{paper}") == paper  # record handle
    assert resolve_source_ref_id(store, f"paper:{paper}") == paper  # kind:id form
    assert resolve_source_ref_id(store, "wang2020") == paper  # cite_key slug


def test_unresolvable_dismissal_is_loud_and_dropped(
    hub: Hub, caplog: pytest.LogCaptureFixture
) -> None:
    """A ledger value that survives *every* recovery path is a real problem (the
    dismissal silently never happens, resurfacing the candidate forever), so the
    drop is loud — a warning naming the ref + the offending value — not silent. It
    is not raised: one bad tag must not blow up the workspace-render read path."""
    import logging

    store = hub.store
    draft = store.insert_ref(kind="todo", slug=None, title="proj").id
    good = store.insert_ref(kind="paper", slug="kumar", title="Kumar 2021").id
    store.add_tag(draft, Tag.closed(DISMISS_NS, str(good)))
    store.add_tag(draft, Tag.closed(DISMISS_NS, "garbage-99"))  # resolves to nothing

    with caplog.at_level(logging.WARNING, logger="precis.backfill.dismissed"):
        got = dismissed_ref_ids(store, draft)

    assert got == {good}  # the good one still lands; the junk one is dropped
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("garbage-99" in r.getMessage() for r in warnings)
    assert any(str(draft) in r.getMessage() for r in warnings)
