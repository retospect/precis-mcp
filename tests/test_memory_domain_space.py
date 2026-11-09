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

import pytest

import precis.runtime.scope as scope_mod
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.runtime.scope import Domain, request_domain
from tests.conftest import id_of

_TOKEN = "Domspx"


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _note(handler: MemoryHandler, text: str, space: str) -> int:
    return id_of(handler.put(text=text, tags=[f"SPACE:{space}"]).body)


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
