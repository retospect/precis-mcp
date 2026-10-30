"""``search(kind='paper', args={'linked_from', 'lacks'})`` — gr538073.

The link-cohort filter: papers with an inbound edge from a live ref of
one kind, and/or with no live edge to or from a ref of another kind —
"papers linked from memories that have no finding linked". ``tags=``
filters the ``linked_from`` (source) side; ``view='count'`` returns the
filtered total.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.utils import handle_registry
from tests.conftest import id_of

pytestmark = pytest.mark.db


@pytest.fixture
def papers(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


@pytest.fixture
def memories(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _paper(store: Store, slug: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=slug, year=2024).id


def _handle(ref_id: int) -> str:
    return handle_registry.format_handle("paper", ref_id)


def _link(store: Store, src: int, dst: int, rel: str = "related-to") -> None:
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation=rel, set_by="agent")


class TestLinkedFrom:
    def test_lists_only_inbound_linked(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "cited-alpha")
        b = _paper(store, "uncited-beta")
        mem = id_of(memories.put(text="note about alpha").body)
        _link(store, mem, a)
        body = papers.search(linked_from="memory").body
        assert _handle(a) in body and _handle(b) not in body, body

    def test_tags_filter_the_source_side(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "campaign-cited")
        b = _paper(store, "elsewhere-cited")
        # SPACE:research is a default-on-create memory tag, so a
        # distinguishing open tag is what separates the two sources.
        m1 = id_of(memories.put(text="campaign note", tags=["mycampaign"]).body)
        m2 = id_of(memories.put(text="other note").body)
        _link(store, m1, a)
        _link(store, m2, b)
        body = papers.search(linked_from="memory", tags=["mycampaign"]).body
        assert _handle(a) in body and _handle(b) not in body, body

    def test_retired_source_and_target_drop_out(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "dead-source")
        b = _paper(store, "live-source-dead-target")
        c = _paper(store, "live")
        m_dead = id_of(memories.put(text="gone soon").body)
        m_live = id_of(memories.put(text="staying").body)
        _link(store, m_dead, a)
        _link(store, m_live, b)
        _link(store, m_live, c)
        memories.delete(id=m_dead)
        store.retire_ref(b)
        body = papers.search(linked_from="memory").body
        assert _handle(c) in body
        assert _handle(a) not in body and _handle(b) not in body, body

    def test_unknown_kind_refused(self, papers: PaperHandler) -> None:
        with pytest.raises(BadInput, match="unknown kind") as exc:
            papers.search(linked_from="bogus-kind")
        assert exc.value.options and "memory" in exc.value.options
        with pytest.raises(BadInput):
            papers.search(linked_from="  ")


class TestLacks:
    def test_lacks_excludes_either_direction(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "uncovered")
        b = _paper(store, "finding-inbound")
        c = _paper(store, "finding-outbound")
        mem = id_of(memories.put(text="cites three").body)
        for p in (a, b, c):
            _link(store, mem, p)
        f1 = store.insert_ref(kind="finding", slug=None, title="covers b").id
        f2 = store.insert_ref(kind="finding", slug=None, title="cited by c").id
        _link(store, f1, b)  # finding → paper
        _link(store, c, f2)  # paper → finding
        body = papers.search(linked_from="memory", lacks="finding").body
        assert _handle(a) in body
        assert _handle(b) not in body and _handle(c) not in body, body

    def test_lacks_alone(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "no-finding")
        b = _paper(store, "has-finding")
        f = store.insert_ref(kind="finding", slug=None, title="f").id
        _link(store, f, b)
        body = papers.search(lacks="finding").body
        assert _handle(a) in body and _handle(b) not in body, body

    def test_empty_cohort(self, papers: PaperHandler, store: Store) -> None:
        _paper(store, "lonely")
        body = papers.search(linked_from="memory").body
        assert "no live paper entries" in body


class TestCountAndPaging:
    def test_count_view_total(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        a = _paper(store, "count-a")
        b = _paper(store, "count-b")
        _paper(store, "count-c")
        mem = id_of(memories.put(text="cites two").body)
        _link(store, mem, a)
        _link(store, mem, b)
        f = store.insert_ref(kind="finding", slug=None, title="f").id
        _link(store, f, b)
        body = papers.search(view="count", linked_from="memory", lacks="finding").body
        assert body.startswith("# 1 paper entry"), body

    def test_page_bound(
        self, papers: PaperHandler, memories: MemoryHandler, store: Store
    ) -> None:
        mem = id_of(memories.put(text="cites many").body)
        ids = [_paper(store, f"paged-{i}") for i in range(3)]
        for p in ids:
            _link(store, mem, p)
        body = papers.search(linked_from="memory", page_size=2).body
        assert " of 3" in body and "page=2" in body, body


def test_args_dict_reaches_the_handler(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The MCP ``args={'linked_from','lacks'}`` shape flattens through
    the verb layer."""
    mem = id_of(
        runtime_with_store.dispatch("put", {"kind": "memory", "text": "cites a paper"})
    )
    a = _paper(store, "verb-cited")
    _link(store, mem, a)
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "paper",
            "__extras__": {"linked_from": "memory", "lacks": "finding"},
        },
    )
    assert "[error" not in out, out
    assert _handle(a) in out
