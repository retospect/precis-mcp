"""A create-time ``put(link=, rel=)`` runs the link verb's checks (gr753894).

The link verb refuses an edge that breaks the relation's constraint row
(``check_relation_constraints``), the taxon hierarchy rules
(``guard_taxon_hierarchy``), and mints a lazy target on first link
(``mint_lazy_link_target``). Every create door writes its own edge, so each
must run the same checks before its insert: a refused put writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._finding_common import check_put_extras
from precis.handlers.citation import CitationHandler
from precis.handlers.concept import ConceptHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.message import MessageHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import ChunkInsert

#: ``contradicts`` is memory -> memory only (migration 0180), so every put
#: below names an end the link verb refuses.
_REFUSAL = r"'(contradicts|selects)' (domain|range)_kinds"


def _count(store: Store, kind: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = %s", (kind,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _memory(store: Store) -> int:
    return store.insert_ref(kind="memory", slug=None, title="a claim").id


def _paper(store: Store, slug: str = "smith2020") -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title="a paper")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="body", meta={})])
    return ref.id


def _put_concept(store: Store, hub: Hub) -> None:
    ConceptHandler(hub=hub).put(
        text="a concept", link=f"memory:{_memory(store)}", rel="contradicts"
    )


def _put_memory(store: Store, hub: Hub) -> None:
    _paper(store)
    MemoryHandler(hub=hub).put(
        text="a claim", link="paper:smith2020", rel="contradicts"
    )


def _put_memory_dry_run(store: Store, hub: Hub) -> None:
    _paper(store)
    MemoryHandler(hub=hub).put(
        text="a claim", link="paper:smith2020", rel="contradicts", dry_run=True
    )


def _put_todo(store: Store, hub: Hub) -> None:
    TodoHandler(hub=hub).put(
        text="do a thing", link=f"memory:{_memory(store)}", rel="contradicts"
    )


def _put_gripe(store: Store, hub: Hub) -> None:
    GripeHandler(hub=hub).put(
        text="a gripe", link=f"memory:{_memory(store)}", rel="contradicts"
    )


def _put_message(store: Store, hub: Hub) -> None:
    MessageHandler(hub=hub).put(
        text="hello",
        target="discord/1/2/3",
        link=f"memory:{_memory(store)}",
        rel="contradicts",
    )


def _put_job(store: Store, hub: Hub) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="parent").id
    JobHandler(hub=hub).put(
        job_type="graph_garden",
        parent_id=parent,
        link=f"memory:{_memory(store)}",
        rel="contradicts",
    )


def _put_finding_chase(store: Store, hub: Hub) -> None:
    _paper(store, "miller23a")
    FindingHandler(hub=hub).put(
        title="t",
        text="a finding body",
        cited_in="miller23a",
        link=f"memory:{_memory(store)}",
        rel="contradicts",
    )


def _put_finding_acquiring(store: Store, hub: Hub) -> None:
    mem = _memory(store)
    FindingHandler(hub=hub).put(
        title="claim awaiting a paper",
        body="claim body text that needs grounding",
        wants=[{"doi": "10.1234/create-link-checks"}],
        provenance=f"memory:{mem}",
        link=f"memory:{mem}",
        rel="contradicts",
    )


def _put_finding_hub_extras(store: Store, hub: Hub) -> None:
    # ``contradicts`` is an evidence role this door refuses on its own;
    # ``selects`` (memory -> part/component) reaches the constraint row.
    check_put_extras(store, tags=None, link=f"memory:{_memory(store)}", rel="selects")


def _put_citation(store: Store, hub: Hub) -> None:
    _paper(store, "collins06")
    CitationHandler(hub=hub).put(
        text="a cited claim",
        source_handle="collins06~0",
        source_quote="body",
        link=f"memory:{_memory(store)}",
        rel="contradicts",
    )


_DOORS: list[tuple[str, Callable[[Store, Hub], None]]] = [
    ("concept", _put_concept),
    ("memory", _put_memory),
    ("memory", _put_memory_dry_run),
    ("todo", _put_todo),
    ("gripe", _put_gripe),
    ("message", _put_message),
    ("job", _put_job),
    ("finding", _put_finding_chase),
    ("finding", _put_finding_acquiring),
    ("finding", _put_finding_hub_extras),
    ("citation", _put_citation),
]


@pytest.mark.parametrize(
    ("kind", "door"),
    _DOORS,
    ids=[door.__name__.removeprefix("_put_") for _, door in _DOORS],
)
def test_create_door_refuses_what_the_link_verb_refuses(
    kind: str, door: Callable[[Store, Hub], None], store: Store, hub: Hub
) -> None:
    with pytest.raises(BadInput, match=_REFUSAL):
        door(store, hub)
    # each door's target is of another kind, so any row of the put's own
    # kind is a ref the refused put left behind without its link
    assert _count(store, kind) == 0


def test_create_time_functional_slot_is_checked(store: Store) -> None:
    """The gripe's command: a second ``draft-of`` on a project that has a
    draft is refused at create, naming the draft that holds the slot."""
    proj = store.insert_ref(kind="todo", slug=None, title="project").id
    store.drafts.create_draft(name="first", title="First", project_ref_id=proj)
    with pytest.raises(BadInput, match="functional"):
        check_put_extras(store, tags=None, link=f"todo:{proj}", rel="draft-of")


def test_create_time_functional_slot_through_a_handler(store: Store, hub: Hub) -> None:
    proj = store.insert_ref(kind="todo", slug=None, title="project").id
    store.drafts.create_draft(name="first", title="First", project_ref_id=proj)
    with pytest.raises(BadInput, match="functional"):
        ConceptHandler(hub=hub).put(
            text="a concept", link=f"todo:{proj}", rel="draft-of"
        )
    assert _count(store, "concept") == 0


def test_finding_hub_extras_run_the_taxon_hierarchy_guard(store: Store) -> None:
    taxon = store.insert_ref(kind="taxon", slug=None, title="a taxon").id
    with pytest.raises(BadInput, match="needs a taxon at both ends"):
        check_put_extras(store, tags=None, link=f"taxon:{taxon}", rel="specialises")


def test_create_time_link_mints_a_lazy_target(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    """A ``markdown:<slug>`` target resolves on first link through the link
    verb (gr516697); the create door mints it the same way."""
    from precis.handlers.markdown import MarkdownHandler

    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    hub.register_handler("markdown", MarkdownHandler(hub=hub, root=tmp_path))
    ConceptHandler(hub=hub).put(text="a concept", link="markdown:notes")
    md_ref = store.get_ref(kind="markdown", id="notes")
    assert md_ref is not None
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.relation FROM links l JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE r.kind = 'concept' AND l.dst_ref_id = %s",
            (md_ref.id,),
        ).fetchall()
    assert [r[0] for r in rows] == ["related-to"]


def test_an_allowed_create_time_link_still_lands(store: Store, hub: Hub) -> None:
    other = _memory(store)
    MemoryHandler(hub=hub).put(
        text="a second claim", link=f"memory:{other}", rel="contradicts"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT relation FROM links WHERE dst_ref_id = %s", (other,)
        ).fetchall()
    assert [r[0] for r in rows] == ["contradicts"]


# ── review round: dry run, refusal after a lazy mint, converging re-put ──

#: A shipped skill no test links, so its anchor ref is minted on first link.
_SKILL = "precis-relations"


def test_memory_dry_run_accepts_a_target_the_real_put_would_mint(
    store: Store, hub: Hub
) -> None:
    """The dry run never mints, so it must not read a mintable anchor as
    missing: the preview names the anchor the real put creates."""
    handler = MemoryHandler(hub=hub)
    out = handler.put(text="a thought", link=f"skill:{_SKILL}", dry_run=True).body
    assert f"skill:{_SKILL}" in out and "creates its anchor" in out
    assert _count(store, "skill") == 0
    assert _count(store, "memory") == 0

    handler.put(text="a thought", link=f"skill:{_SKILL}")
    assert _count(store, "skill") == 1


def test_memory_dry_run_previews_a_markdown_file_without_ingesting_it(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    from precis.handlers.markdown import MarkdownHandler

    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    hub.register_handler("markdown", MarkdownHandler(hub=hub, root=tmp_path))
    out = MemoryHandler(hub=hub).put(
        text="a thought", link="markdown:notes", dry_run=True
    )
    assert "creates its anchor" in out.body
    assert _count(store, "markdown") == 0


def test_memory_dry_run_still_refuses_an_unknown_lazy_target(hub: Hub) -> None:
    with pytest.raises(NotFound, match="no skill"):
        MemoryHandler(hub=hub).put(
            text="a thought", link="skill:no-such-skill-xyz", dry_run=True
        )


def test_memory_dry_run_checks_the_kind_of_a_target_not_minted_yet(
    store: Store, hub: Hub
) -> None:
    """``selects`` is memory -> part/component: the real put refuses a skill
    target after minting its anchor, so the dry run refuses it too."""
    with pytest.raises(BadInput, match=r"'selects' range_kinds"):
        MemoryHandler(hub=hub).put(
            text="a thought", link=f"skill:{_SKILL}", rel="selects", dry_run=True
        )
    assert _count(store, "skill") == 0


def test_put_refused_after_a_lazy_mint_leaves_no_ref_of_its_own_kind(
    store: Store, hub: Hub
) -> None:
    """The anchor is minted before the relation check; the refused put
    leaves that anchor (as the link verb does) and nothing of its own."""
    with pytest.raises(BadInput, match=_REFUSAL):
        ConceptHandler(hub=hub).put(
            text="a concept", link=f"skill:{_SKILL}", rel="contradicts"
        )
    assert _count(store, "concept") == 0
    assert _count(store, "skill") == 1


def _hub_holding_draft_of(store: Store) -> tuple[int, int]:
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    proj = store.insert_ref(kind="todo", slug=None, title="project").id
    existing = mint_hub(
        store, CanonicalClaim(sentence="Pd/C catalyzes Suzuki coupling.", scope={})
    )
    store.add_link(src_ref_id=existing, dst_ref_id=proj, relation="draft-of")
    return proj, existing


def _draft_of_sources(store: Store, proj: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id FROM links WHERE dst_ref_id = %s "
            "AND relation = 'draft-of' ORDER BY src_ref_id",
            (proj,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_converging_hub_put_may_repeat_the_functional_edge_it_holds(
    store: Store, hub: Hub
) -> None:
    """A put that converges onto a hub already holding the ``draft-of`` slot
    re-adds that hub's own edge: the link verb passes it, so the put does."""
    _paper(store, "miller23a")
    proj, existing = _hub_holding_draft_of(store)
    resp = FindingHandler(hub=hub).put(
        title="Pd on carbon catalyzes the Suzuki reaction.",
        supporters=[{"paper": "miller23a"}],
        dedup=f"attach:fi{existing}",
        link=f"todo:{proj}",
        rel="draft-of",
    )
    assert "converged onto" in resp.body
    assert _draft_of_sources(store, proj) == [existing]


def test_a_new_hub_cannot_take_a_functional_slot_another_hub_holds(
    store: Store, hub: Hub
) -> None:
    _paper(store, "miller23a")
    proj, existing = _hub_holding_draft_of(store)
    before = _count(store, "finding")
    with pytest.raises(BadInput, match="functional"):
        FindingHandler(hub=hub).put(
            title="A wholly novel claim with no neighbours.",
            supporters=[{"paper": "miller23a"}],
            dedup=False,
            link=f"todo:{proj}",
            rel="draft-of",
        )
    assert _count(store, "finding") == before
    assert _draft_of_sources(store, proj) == [existing]
