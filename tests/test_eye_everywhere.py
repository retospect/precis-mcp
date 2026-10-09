"""``get(kind=K, id=…, extent=<rung>)`` on every kind — the one door of
``docs/backlog/fisheye-everywhere.md`` in-scope 2, through the runtime
(the dispatcher routes ``extent=`` and the ladder labels on ``view=`` to
``Handler.eye``). AC 1: every registered kind renders a neighbourhood or
raises ``Unsupported`` with a one-sentence reason; AC 2/3: the structural
relations land under their ring headings on taxon, concept, component,
todo and memory; the doc eye on a paper; the skill eye."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._eye import EYE_LADDER, eye_handle, is_eye_view
from precis.store import Tag
from precis.store.types import ChunkInsert
from precis.utils import handle_registry, kind_facts
from precis.utils.eye_render import _TREE_KINDS

pytestmark = pytest.mark.db


def _get(runtime: Any, **args: Any) -> tuple[str, bool]:
    return runtime.dispatch_with_status("get", args)


def _ref(store: Any, kind: str, title: str, *, slug: str | None = None) -> Any:
    return store.insert_ref(kind=kind, slug=slug, title=title)


class TestTheDoor:
    def test_is_eye_view_names_the_ladder_and_recall(self) -> None:
        assert EYE_LADDER == (
            "kwd",
            "summary",
            "verbatim",
            "fisheye",
            "fisheye+1hop",
            "fisheye+2hop",
        )
        assert all(is_eye_view(label) for label in EYE_LADDER)
        assert is_eye_view("fisheye+1hop+recall") and is_eye_view("+recall")
        assert not is_eye_view("links") and not is_eye_view(None)

    def test_extent_and_the_view_label_render_the_same(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Wire the eye")
        memo = _ref(store, "memory", "Why the eye is one door")
        store.add_link(src_ref_id=memo.id, dst_ref_id=todo.id, relation="related-to")
        via_extent, err1 = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+1hop"
        )
        via_view, err2 = _get(
            runtime_with_store, kind="todo", id=todo.id, view="fisheye+1hop"
        )
        assert not err1 and not err2
        assert via_extent == via_view
        assert "— linked (1 hop) —" in via_extent
        assert f"related-to: me{memo.id} — Why the eye is one door" in via_extent

    def test_extent_on_a_bare_handle_routes_by_the_handle(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Handle-routed")
        out, err = _get(runtime_with_store, id=f"td{todo.id}", extent="fisheye")
        assert not err and f"td{todo.id} [todo] Handle-routed" in out

    def test_extent_and_a_different_view_is_one_argument_too_many(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Two doors")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye", view="links"
        )
        assert err and "[error:BadInput]" in out and "one argument" in out

    def test_unknown_rung_names_the_ladder(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Bad rung")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+9hop"
        )
        assert err and "[error:BadInput]" in out
        assert "fisheye+2hop" in out  # the next= line lists the ladder

    def test_dead_ref_is_not_found(self, runtime_with_store: Any, store: Any) -> None:
        todo = _ref(store, "todo", "Gone soon")
        store.retire_ref(todo.id)
        out, err = _get(runtime_with_store, kind="todo", id=todo.id, extent="fisheye")
        assert err and ("[error:Gone]" in out or "[error:NotFound]" in out)


class TestPerKind:
    def test_taxon_eye_groups_its_hierarchy_under_taxonomy(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        genus = _ref(store, "taxon", "Faradaic efficiency")
        species = _ref(store, "taxon", "NH3 Faradaic efficiency")
        instance = _ref(store, "memory", "FE measured at -0.5 V")
        store.add_link(
            src_ref_id=species.id, dst_ref_id=genus.id, relation="specialises"
        )
        store.add_link(
            src_ref_id=instance.id, dst_ref_id=species.id, relation="instance-of"
        )
        out, err = _get(
            runtime_with_store, kind="taxon", id=species.id, extent="fisheye+1hop"
        )
        assert not err
        assert f"tn{species.id} [taxon] NH3 Faradaic efficiency" in out
        assert "Taxonomy:" in out
        assert f"  specialises: tn{genus.id} — Faradaic efficiency" in out
        assert f"  has-instance: me{instance.id} — FE measured at -0.5 V" in out
        assert "Notes & links:" not in out  # nothing fell into the bucket

    def test_concept_eye_groups_prerequisites_under_concepts(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        concept = _ref(store, "concept", "Rigidity percolation")
        prereq = _ref(store, "concept", "Maxwell counting")
        store.add_link(
            src_ref_id=concept.id, dst_ref_id=prereq.id, relation="has-prerequisite"
        )
        out, err = _get(
            runtime_with_store, kind="concept", id=concept.id, extent="fisheye+1hop"
        )
        assert not err and "Concepts:" in out
        assert f"  has-prerequisite: cn{prereq.id} — Maxwell counting" in out

    def test_component_eye_groups_parts_under_parts(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        assembly = _ref(store, "component", "Electrolyser stack", slug="stack")
        part = _ref(store, "component", "Bipolar plate", slug="plate")
        store.add_link(src_ref_id=assembly.id, dst_ref_id=part.id, relation="contains")
        out, err = _get(
            runtime_with_store, kind="component", id=assembly.id, extent="fisheye+1hop"
        )
        assert not err and "Parts:" in out
        assert f"  contains: cp{part.id} — Bipolar plate" in out
        # and from the part's side the same row reads as its inverse
        back, err = _get(
            runtime_with_store, kind="component", id=part.id, extent="fisheye+1hop"
        )
        assert not err and f"  part-of: cp{assembly.id} — Electrolyser stack" in back

    def test_memory_eye_renders_body_links_and_mirror_name(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        memo = store.insert_ref(
            kind="memory",
            slug=None,
            title="Worker busy vs starved",
            meta={"file_mirror": {"filename": "worker_busy_vs_starved.md"}},
        )
        store.chunks.insert_chunks(
            memo.id,
            [
                ChunkInsert(
                    ord=0, text="A busy worker has a queue; a starved one has none."
                )
            ],
        )
        section = _ref(store, "memory", "Diagnosis section")
        store.add_link(src_ref_id=section.id, dst_ref_id=memo.id, relation="part-of")
        out, err = _get(
            runtime_with_store, kind="memory", id=memo.id, extent="fisheye+1hop"
        )
        assert not err
        assert (
            f"me{memo.id} (worker_busy_vs_starved.md) [memory] Worker busy vs starved"
            in out
        )
        assert "A busy worker has a queue" in out
        assert (
            "Parts:" in out and f"  contains: me{section.id} — Diagnosis section" in out
        )

    def test_paper_eye_is_the_cluster_map_and_a_chunk_eye_the_split(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        paper = _ref(store, "paper", "Rigidity percolation", slug="mao18")
        store.chunks.insert_chunks(
            paper.id, [ChunkInsert(ord=i, text=f"body of chunk {i}") for i in range(4)]
        )
        note = _ref(store, "memory", "Read this for the floppy modes")
        store.add_link(src_ref_id=note.id, dst_ref_id=paper.id, relation="related-to")
        whole, err = _get(
            runtime_with_store, kind="paper", id="mao18", extent="fisheye+1hop"
        )
        assert not err
        assert f"pa{paper.id} [paper] Rigidity percolation" in whole
        assert "clusters" in whole and "▸" not in whole  # the map, no verbatim eye
        assert f"related-to: me{note.id} — Read this for the floppy modes" in whole
        # the chunk handle — rewritten by the dispatcher to slug~ord — is the split
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 2", (paper.id,)
            ).fetchone()[0]
        split, err = _get(runtime_with_store, id=f"pc{chunk_id}", extent="fisheye")
        assert not err and f"▸ pc{chunk_id}" in split and "body of chunk 2" in split

    def test_todo_eye_shows_its_quest_and_notes(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        todo = _ref(store, "todo", "Ship the ring")
        quest = _ref(store, "quest", "Grow the mesh")
        store.add_link(src_ref_id=todo.id, dst_ref_id=quest.id, relation="serves")
        out, err = _get(
            runtime_with_store, kind="todo", id=todo.id, extent="fisheye+1hop"
        )
        assert not err and "Roadmap:" in out
        assert f"  serves: qu{quest.id} — Grow the mesh" in out

    def test_skill_eye_is_the_verbatim_body_with_no_ring(
        self, runtime_with_store: Any
    ) -> None:
        out, err = _get(
            runtime_with_store,
            kind="skill",
            id="precis-fisheye-help",
            extent="fisheye+1hop",
        )
        assert not err
        assert out.startswith("sk:precis-fisheye-help [skill]")
        assert "— linked (1 hop) —" not in out
        kwd, err = _get(
            runtime_with_store, kind="skill", id="precis-fisheye-help", extent="kwd"
        )
        assert not err and kwd.startswith("· sk:precis-fisheye-help [skill]")

    def test_whole_draft_says_the_eye_is_a_section(
        self, runtime_with_store: Any, store: Any
    ) -> None:
        draft = _ref(store, "draft", "A draft", slug="a-draft")
        out, err = _get(
            runtime_with_store, kind="draft", id="a-draft", extent="fisheye"
        )
        assert err and "[error:Unsupported]" in out and "dc<id>" in out
        assert f"dr{draft.id}" not in out or "section" in out

    def test_codeless_and_file_backed_kinds_refuse_with_a_reason(
        self, runtime_with_store: Any
    ) -> None:
        for kind, ident in (("web", "https://example.org"), ("tag", "SPACE:repo-dev")):
            out, err = _get(runtime_with_store, kind=kind, id=ident, extent="fisheye")
            assert err and "[error:Unsupported]" in out, (kind, out)
            assert "graph node" in out, (kind, out)


class TestEyeHandle:
    def test_resolves_every_address_form(self, store: Any) -> None:
        paper = _ref(store, "paper", "P", slug="p-slug")
        store.chunks.insert_chunks(paper.id, [ChunkInsert(ord=0, text="t")])
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s", (paper.id,)
            ).fetchone()[0]
        assert eye_handle(store, kind="paper", id="p-slug") == f"pa{paper.id}"
        assert eye_handle(store, kind="paper", id=f"pa{paper.id}") == f"pa{paper.id}"
        assert eye_handle(store, kind="paper", id=str(paper.id)) == f"pa{paper.id}"
        assert (
            eye_handle(store, kind="paper", id=f"paper:{paper.id}") == f"pa{paper.id}"
        )
        assert eye_handle(store, kind="paper", id="p-slug~0") == f"pc{chunk_id}"
        assert eye_handle(store, kind="paper", id=f"pc{chunk_id}") == f"pc{chunk_id}"
        with pytest.raises(BadInput, match="one node"):
            eye_handle(store, kind="paper", id="p-slug~0..2")
        with pytest.raises(NotFound):
            eye_handle(store, kind="paper", id="p-slug~7")
        with pytest.raises(NotFound):
            eye_handle(store, kind="paper", id="nope")
        _ref(store, "draft", "D", slug="d-slug")
        with pytest.raises(Unsupported, match="section"):
            eye_handle(store, kind="draft", id="d-slug")
        for kind in ("web", "calc", "python", "md", "tag"):
            with pytest.raises(Unsupported):
                eye_handle(store, kind=kind, id="x")


_SPECS = {s.kind: s for s in kind_facts.all_declared_specs()}


@pytest.mark.parametrize("kind", sorted(_SPECS))
def test_every_kind_renders_or_refuses_with_a_reason(
    kind: str, runtime_with_store: Any, store: Any
) -> None:
    """AC 1: ``get(kind=K, id=<a live ref>, extent='fisheye')`` is a render
    or ``Unsupported`` for every declared kind — never a bare chunk, never
    a kwargs-gate rejection of ``extent=``, never an internal error."""
    if kind == "skill":
        ident: Any = "precis-fisheye-help"
    elif handle_registry.try_format(kind, 0) is None or kind in ("python", "md", "tag"):
        ident = "anything"
    else:
        slug = None if _SPECS[kind].is_numeric else f"{kind}-node"
        try:
            if kind == "gripe":  # migration 0176: a gripe needs its STATUS tag
                with store.tx() as conn:
                    ref = store.insert_ref(
                        kind=kind, slug=None, title="a gripe", conn=conn
                    )
                    store.add_tag(
                        ref.id, Tag.closed("STATUS", "open"), set_by="agent", conn=conn
                    )
                ident = ref.id
            else:
                ident = _ref(store, kind, f"a {kind} node", slug=slug).id
        except Exception:
            ident = 1
    out, err = _get(runtime_with_store, kind=kind, id=ident, extent="fisheye")
    if not err:
        assert out.strip(), kind
        assert "does not accept" not in out
        return
    assert "[error:Unsupported]" in out, (kind, out)
    cause = out.splitlines()[0]
    # one sentence: a reason, not a shrug
    assert len(cause) > 40 and ":" in cause, (kind, cause)
    if kind in _TREE_KINDS:
        assert "section" in cause
