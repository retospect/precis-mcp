"""gr613470: ``get(id=N, view='meta'|'tags')`` — the per-ref reads
agents kept guessing (~70 refused calls / 30d on todo, paper, job,
quest, gripe; a PreToolUse hint advertised the spelling verbatim).

``meta``/``tags`` are base views on every numeric-ref kind now —
``meta`` renders the ref's meta dict as JSON, ``tags`` its tag set —
and paper's ``tags`` lists its ref_tags. Kinds that already gave the
spelling an own reading keep it (todo/quest/taxon 'meta', job's
'meta'→'raw', todo/gripe 'tags'→card, paper's 'meta'→overview).
The kind-level inventories stay on
``search(kind=K, view='meta'|'tags')``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.quest import QuestHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store, Tag


def _id_of(body: str) -> int:
    from tests.conftest import id_of

    return id_of(body)


# ── gripe: 'meta' refused before, 'tags' folded to the card ───────


def test_gripe_meta_renders_the_meta_dict(hub: Hub, store: Store) -> None:
    handler = GripeHandler(hub=hub)
    r = handler.put(text="meta fixture gripe")
    rid = _id_of(r.body)
    store.stamp_ref_meta(rid, {"lane": "devin"})
    body = handler.get(id=rid, view="meta").body
    assert f"# gripe {rid} — meta" in body
    assert '"lane": "devin"' in body


def test_gripe_tags_still_reads_as_the_card(hub: Hub) -> None:
    """'tags' was already a gripe alias for the title card (gr503549)
    — the card carries the ``tags:`` line, so that reading stays."""
    handler = GripeHandler(hub=hub)
    r = handler.put(text="tagged fixture gripe", tags=["area-meta-fix"])
    rid = _id_of(r.body)
    body = handler.get(id=rid, view="tags").body
    assert "interpreted: view='tags' read as the default view" in body
    assert "area-meta-fix" in body


def test_gripe_meta_batch_view(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    a = _id_of(handler.put(text="meta a", meta={"k": 1}).body)
    b = _id_of(handler.put(text="meta b", meta={"k": 2}).body)
    body = handler.get(id=[a, b], view="meta").body
    assert f"# gripe {a} — meta" in body and f"# gripe {b} — meta" in body


# ── job / quest / memory: 'tags' was refused on the base gate ─────


def test_job_tags_lists_the_ref_tags(hub: Hub, store: Store) -> None:
    ref = store.insert_ref(kind="job", slug=None, title="fixture job", meta={})
    store.add_tag(ref.id, Tag.open("host-hp99"))
    body = JobHandler(hub=hub).get(id=ref.id, view="tags").body
    assert f"# job {ref.id} — tags" in body
    assert "host-hp99" in body


def test_tags_view_names_no_tags(store: Store, hub: Hub) -> None:
    ref = store.insert_ref(kind="job", slug=None, title="untagged fixture", meta={})
    body = JobHandler(hub=hub).get(id=ref.id, view="tags").body
    assert "(no tags)" in body


def test_quest_tags_lists_the_ref_tags(hub: Hub) -> None:
    import re

    handler = QuestHandler(hub=hub)
    r = handler.put(text="fixture quest for tags view")
    m = re.search(r"\bqu(\d+)\b", r.body)
    assert m is not None
    qid = int(m.group(1))
    handler.store.add_tag(qid, Tag.open("topic-x"))
    body = handler.get(id=qid, view="tags").body
    assert f"# quest {qid} — tags" in body
    assert "topic-x" in body


def test_memory_tags_lists_the_ref_tags(hub: Hub) -> None:
    handler = MemoryHandler(hub=hub)
    r = handler.put(text="fixture memory", tags=["fixture-tag"])
    mid = _id_of(r.body)
    body = handler.get(id=mid, view="tags").body
    assert f"# memory {mid} — tags" in body
    assert "fixture-tag" in body


# ── todo: 'meta' stays byte-identical on the shared render ───────


def test_todo_meta_uses_the_shared_render(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    r = handler.put(text="todo meta fixture", meta={"llm_tier": "sonnet"})
    rid = _id_of(r.body)
    body = handler.get(id=rid, view="meta").body
    assert f"# todo {rid} — meta" in body
    assert '"llm_tier": "sonnet"' in body


def test_meta_needs_an_id(hub: Hub) -> None:
    """An id-less ``get(view='meta')`` direct call still refuses — via
    dispatch it is the kind-level inventory, which lives on search."""
    handler = TodoHandler(hub=hub)
    with pytest.raises(BadInput, match="require"):
        handler.get(view="meta")


# ── paper: 'meta' is the overview no-op (gr551843); 'tags' is new ──


def _seed_paper(store: Store, slug: str) -> int:
    return store.insert_ref(
        kind="paper", slug=slug, title=f"Test paper {slug}", provider="manual", meta={}
    ).id


def test_paper_tags_lists_the_ref_tags(hub: Hub, store: Store) -> None:
    rid = _seed_paper(store, "tagsview2024")
    store.add_tag(rid, Tag.open("topic-co2"))
    body = PaperHandler(hub=hub).get(id=f"pa{rid}", view="tags").body
    assert f"# pa{rid} — tags" in body
    assert "topic-co2" in body


def test_paper_meta_still_reads_as_the_overview(hub: Hub, store: Store) -> None:
    """gr551843: 'meta' aliases the overview — the biblio card IS the
    meta card on a paper."""
    rid = _seed_paper(store, "metaalias2024")
    body = PaperHandler(hub=hub).get(id=f"pa{rid}", view="meta").body
    assert f"# pa{rid}" in body
