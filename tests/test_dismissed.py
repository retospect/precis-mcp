"""source-backfill slice 4 (leading edge) — the dismissed-source ledger.

A dismissed paper must (a) read back from the ledger and (b) join the Tier-0
exclude set that ``assemble`` hands recall, so a rejected hit never resurfaces —
while still not seeding a citation-graph neighbourhood (dismissed ≠ cited).
"""

from __future__ import annotations

import re

import pytest

from precis.backfill import workspace as wsmod
from precis.backfill.dismissed import dismiss_source, dismissed_ref_ids
from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler


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
