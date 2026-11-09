"""gr572723 (C4): ``search(kind='memory')`` on a domain-bound connection
defaults to the domain's ``SPACE:`` set.

A call with no ``SPACE:`` tag in ``tags=`` filters to the domain's
spaces; a caller-supplied ``SPACE:`` tag wins (either polarity), and the
reads view's ``space=`` counts as one. The default never narrows by
``section:``/``topic:``, so rulings, gotchas and runbooks stay reachable
inside the space. The footer names the door when the default dropped
hits. Fisheye and get-by-handle are untouched; ``'all'`` is byte-
identical to the pre-domain surface.
"""

from __future__ import annotations

from typing import Any

import pytest

import precis.runtime.scope as scope_mod
from precis.dispatch import Hub
from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import emit_proposal
from precis.handlers.memory import MemoryHandler
from precis.runtime.scope import Domain, request_domain
from precis.store import Store
from tests.conftest import id_of

_TOKEN = "Domspx"


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _note(handler: MemoryHandler, text: str, space: str) -> int:
    return id_of(handler.put(text=text, tags=[f"SPACE:{space}"]).body)


def _ruling(handler: MemoryHandler, text: str, space: str, **meta: Any) -> int:
    return id_of(
        handler.put(
            text=text, tags=["ruling", f"SPACE:{space}"], meta=meta or None
        ).body
    )


def _reads_row(store: Store, ref_id: int, n_agent: int) -> None:
    """One ref_reads row — the reads view's min_reads predicate needs it."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_reads (ref_id, n_agent) VALUES (%s, %s)",
            (ref_id, n_agent),
        )


def _proposal(store: Store, ref_id: int) -> None:
    review_id = emit_proposal(
        store,
        GardenCandidate(
            detector="anchor_rot",
            action="rewrite",
            target_kind="ref",
            target_id=ref_id,
            title="Repair anchor_rot",
            evidence={"observed": "missing scripts/old-command"},
        ),
        rationale="Fixture evidence supports this repair",
    )
    assert review_id is not None


def test_domain_default_keeps_search_inside_the_space(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(q=_TOKEN).body
    assert f"memory {dev}" in body
    assert f"memory {prod}" not in body
    # The footer names the door — the dropped product-space hit is
    # announced with the way to reach it.
    assert "domain=repo-dev" in body and "the door" in body


def test_caller_space_tag_wins(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(q=_TOKEN, tags=["SPACE:research"]).body
    assert f"memory {prod}" in body
    assert f"memory {dev}" not in body
    # The caller named the axis — no default, no door footer.
    assert "the door" not in body


def test_negated_space_tag_is_a_caller_choice(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(q=_TOKEN, tags=["!SPACE:research"]).body
    assert f"memory {dev}" in body


def test_non_space_tag_still_defaults_the_space(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(q=_TOKEN, tags=["topic:gardening"]).body
    # The domain default narrows by SPACE: only — the caller's open tag
    # ANDs on top, so only in-space candidates carry it. Neither note is
    # tagged, but both stay reachable: the default is a filter, not a
    # stamp.
    assert f"memory {prod}" not in body
    body = handler.search(q=_TOKEN).body
    assert f"memory {dev}" in body


def test_domain_all_is_unchanged(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "all")
    body = handler.search(q=_TOKEN).body
    assert f"memory {dev}" in body and f"memory {prod}" in body
    assert "the door" not in body


def test_index_view_stays_in_space(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(q=_TOKEN, view="index").body
    assert f"me{dev}" in body and f"me{prod}" not in body
    # The queryless listing walks the same partition.
    body = handler.search(view="index").body
    assert f"me{dev}" in body and f"me{prod}" not in body


def test_reads_view_stays_in_space(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(view="reads", min_chars=1).body
    assert f"me{dev}" in body and f"me{prod}" not in body
    # The reads view's own space= is a caller choice — it wins.
    body = handler.search(view="reads", space="research", min_chars=1).body
    assert f"me{prod}" in body and f"me{dev}" not in body


def test_multi_space_domain_is_set_membership(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The domain's SPACE: *set* is an OR — a two-space domain keeps
    both spaces and still drops the third."""
    monkeypatch.delenv(scope_mod.DOMAIN_ENV, raising=False)
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    mine = _note(handler, f"{_TOKEN} personal note", "personal")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    two = Domain(name="two", spaces=frozenset({"repo-dev", "personal"}))
    with request_domain(two):
        body = handler.search(q=_TOKEN).body
    assert f"memory {dev}" in body and f"memory {mine}" in body
    assert f"memory {prod}" not in body
    assert "domain=two" in body


def test_unrelated_views_untouched(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Get by handle and the rulings- axis plumbing are out of scope:
    only ``search`` filters on the domain."""
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    assert f"{_TOKEN} product note" in handler.get(id=prod).body
    # rulings view: the domain filter applies to the search surface
    # (rulings inside the space stay reachable; foreign ones don't).
    handler.tag(id=dev, add=["ruling"])
    handler.tag(id=prod, add=["ruling"])
    body = handler.search(tags=["ruling"], view="rulings").body
    assert f"me{dev}" in body and f"me{prod}" not in body


def test_cross_kind_stream_stays_in_space(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    prod = _note(handler, f"{_TOKEN} product note", "research")
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    hits = handler.search_hits(q=_TOKEN, tags=None, page_size=10)
    ids = {h.ref_id for h in hits}
    assert dev in ids and prod not in ids


# ── the footer counts the view's corpus, not every memory (td611734) ────
# Each specialized view carries predicates the plain ref count ignores:
# a corpus entry the view never had is not a hidden hit.


def test_reads_footer_keeps_min_predicates(
    handler: MemoryHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.live_store
    dev = _note(handler, f"{_TOKEN} " + "d" * 300, "repo-dev")
    _reads_row(store, dev, 5)
    _note(handler, f"{_TOKEN} tiny", "research")  # below min_chars
    _note(handler, f"{_TOKEN} " + "p" * 300, "research")  # no read counters
    seen = _note(handler, f"{_TOKEN} " + "r" * 300, "research")
    _reads_row(store, seen, 7)
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(view="reads", min_chars=100).body
    # The corpus is dev + the two large research rows; tiny never had a hit.
    assert "2 memory entries outside SPACE:repo-dev" in body
    body = handler.search(view="reads", min_chars=100, min_reads=1).body
    # Only dev and seen carry read counters.
    assert "1 memory entry outside SPACE:repo-dev" in body
    body = handler.search(view="reads", min_chars=1000).body
    # An empty view has nothing to hide — no phantom door.
    assert "the door" not in body


def test_rulings_footer_keeps_ruling_and_status_predicates(
    handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    _note(handler, f"{_TOKEN} plain foreign note", "research")
    _ruling(handler, f"{_TOKEN} archived foreign ruling", "research", status="archived")
    handler.tag(id=dev, add=["ruling"])
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(view="rulings").body
    # The default view is in-force rulings only: the plain foreign note
    # is not a ruling, the archived foreign ruling is not shown — so
    # neither is a dropped hit.
    assert f"me{dev}" in body
    assert "the door" not in body
    _ruling(handler, f"{_TOKEN} live foreign ruling", "research", ruled_by="test")
    body = handler.search(view="rulings").body
    # One in-force foreign ruling now exists — it alone is hidden.
    assert "1 memory entry outside SPACE:repo-dev" in body
    body = handler.search(view="rulings", status="*").body
    # status='*' widens the corpus: the archived ruling joins the count.
    assert "2 memory entries outside SPACE:repo-dev" in body


def test_smells_footer_counts_only_proposal_nodes(
    handler: MemoryHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = hub.live_store
    dev = _note(handler, f"{_TOKEN} dev note", "repo-dev")
    _note(handler, f"{_TOKEN} clean foreign note", "research")
    flagged = _note(handler, f"{_TOKEN} flagged foreign note", "research")
    _proposal(store, dev)
    _proposal(store, flagged)
    monkeypatch.setenv(scope_mod.DOMAIN_ENV, "repo-dev")
    body = handler.search(view="smells").body
    # A memory with no proposals is no smells entry; only the flagged
    # foreign node is hidden.
    assert "1 memory entry outside SPACE:repo-dev" in body
