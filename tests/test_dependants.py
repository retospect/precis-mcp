"""``Store.dependants`` (gr626866) — the bounded, paged rely-side walk.

Direction rule under test: a stored ``S -rel-> D`` edge contributes the
relying endpoint — default the src (``S cites D`` ⇒ S relies on D), the
exception families in ``_DEPENDANT_AT_DST`` the dst (``S corroborates
D`` ⇒ D relies on S). The spec chain exercises both in one walk:
``paper -corroborates-> hub`` plus ``draft -cites-> hub`` makes the hub
a depth-1 dependant of the paper and the draft a depth-2 one.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput, NotFound
from precis.store import Store


def _mk_ref(store: Store, kind: str, title: str) -> int:
    return store.insert_ref(kind=kind, slug=None, title=title).id


def _mk_paper(store: Store, slug: str, title: str = "test paper") -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title).id


def _spec_chain(store: Store) -> tuple[int, int, int]:
    """paper P --corroborates--> hub H, draft D --cites--> hub H."""
    p = _mk_paper(store, "spec2026chain")
    h = _mk_ref(store, "finding", "claim hub under test")
    d = store.insert_ref(
        kind="draft", slug="speccitingdraft", title="draft citing the claim"
    ).id
    store.add_link(src_ref_id=p, dst_ref_id=h, relation="corroborates")
    store.add_link(src_ref_id=d, dst_ref_id=h, relation="cites")
    return p, h, d


class TestDirection:
    def test_figure_in_reports_embedding_draft(self, store: Store) -> None:
        canvas = _mk_ref(store, "figure", "Editable canvas")
        draft = store.insert_ref(
            kind="draft", slug="embeddingdraft", title="Draft with canvas"
        ).id
        anchor = store.drafts.add_chunks(
            ref_id=draft, chunk_kind="figure", text="Caption", split=False
        )[0]
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ord FROM chunks WHERE chunk_id = %s", (anchor.chunk_id,)
            ).fetchone()
        assert row is not None
        store.add_link(
            src_ref_id=canvas,
            dst_ref_id=draft,
            dst_pos=int(row[0]),
            relation="figure-in",
        )
        page = store.dependants(canvas, depth=1)
        assert [(row.ref_id, row.relation) for row in page.rows] == [
            (draft, "figure-in")
        ]
        assert store.dependants(draft).rows == ()

        store.drafts.link_figure_canvas(anchor.chunk_id, canvas)
        assert [row.ref_id for row in store.dependants(canvas).rows] == [draft]
        assert store.dependants(draft).rows == ()

    def test_spec_chain_depth_two(self, store: Store) -> None:
        """dependants(P, depth=2) → H at depth 1, D at depth 2."""
        p, h, d = _spec_chain(store)
        page = store.dependants(p, depth=2)
        by_id = {r.ref_id: r for r in page.rows}
        assert by_id[h].depth == 1
        assert by_id[h].relation == "corroborates"
        assert by_id[h].via_ref_id == p
        assert by_id[d].depth == 2
        # As seen from the relied-upon node (H), the draft's edge reads
        # as the inverse of cites.
        assert by_id[d].relation == "cited-by"
        assert by_id[d].via_ref_id == h
        assert page.complete is True
        assert page.uninspected == 0
        assert page.next_cursor is None
        assert page.total_seen == 2

    def test_depth_one_hides_transitive(self, store: Store) -> None:
        p, h, d = _spec_chain(store)
        page = store.dependants(p, depth=1)
        assert [r.ref_id for r in page.rows] == [h]
        # D was never discovered at depth=1 — not "uninspected", simply
        # outside the walk's bound.
        assert page.total_seen == 1
        assert page.complete is True

    def test_passive_spelling_stores_active(self, store: Store) -> None:
        """``add_link(rel='cited-by')`` canonicalises to a swapped
        ``cites`` row — the walk still reports the citing ref as the
        dependant."""
        p = _mk_paper(store, "passive2026test")
        m = _mk_ref(store, "memory", "the citing memory")
        store.add_link(src_ref_id=p, dst_ref_id=m, relation="cited-by")
        page = store.dependants(p, depth=1)
        assert [(r.ref_id, r.relation) for r in page.rows] == [(m, "cited-by")]

    def test_ordinary_pointing_edge_is_a_dependant(self, store: Store) -> None:
        """Default direction: ``S -rel-> T`` ⇒ S relies on T — a plain
        related-to/part-of-ish edge's src is the dependant."""
        t = _mk_ref(store, "memory", "the pointed-at note")
        s = _mk_ref(store, "todo", "the pointing todo")
        store.add_link(src_ref_id=s, dst_ref_id=t, relation="part-of")
        page = store.dependants(t, depth=1)
        assert [(r.ref_id, r.relation) for r in page.rows] == [(s, "contains")]

    def test_outbound_only_is_no_dependant(self, store: Store) -> None:
        """The focus relying on others is not a dependant of itself —
        an edge the focus *points* with does not bounce back."""
        p = _mk_paper(store, "outbound2026only")
        m = _mk_ref(store, "memory", "unrelated note")
        store.add_link(src_ref_id=p, dst_ref_id=m, relation="cites")
        page = store.dependants(p, depth=2)
        assert page.rows == ()
        assert page.complete is True

    def test_retired_dependant_excluded(self, store: Store) -> None:
        p = _mk_paper(store, "retiree2026deps")
        live = _mk_ref(store, "memory", "live citer")
        dead = _mk_ref(store, "memory", "retired citer")
        store.add_link(src_ref_id=live, dst_ref_id=p, relation="cites")
        store.add_link(src_ref_id=dead, dst_ref_id=p, relation="cites")
        store.retire_ref(dead)
        page = store.dependants(p)
        assert [r.ref_id for r in page.rows] == [live]

    def test_symmetric_related_to_both_sides(self, store: Store) -> None:
        a = _mk_ref(store, "memory", "first cousin")
        b = _mk_ref(store, "memory", "second cousin")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        assert [r.ref_id for r in store.dependants(a).rows] == [b]
        assert [r.ref_id for r in store.dependants(b).rows] == [a]


class TestInverseSpellingDirections:
    """Review td681243: inverse/passive spellings stored as written
    (outside ``_CANONICAL_FLIP``) were walked the wrong way. Each pair's
    invariant: exactly one spelling lists its dependant at ``dst`` —
    the one whose stored dst is the rely party."""

    def test_used_in_reports_artifact_dependant(self, store: Store) -> None:
        """``material -used-in-> component``: the component leans on the
        material — walking the material surfaces the component, not the
        other way round."""
        material = _mk_ref(store, "memory", "the material record")
        component = _mk_ref(store, "todo", "the using component")
        store.add_link(src_ref_id=material, dst_ref_id=component, relation="used-in")
        assert [r.ref_id for r in store.dependants(material).rows] == [component]
        assert store.dependants(component).rows == ()

    def test_depicted_in_reports_figure_dependant(self, store: Store) -> None:
        """``thing -depicted-in-> figure`` ≡ ``figure -depicts-> thing``:
        the figure leans on what it depicts."""
        thing = _mk_ref(store, "memory", "the depicted subject")
        figure = store.insert_ref(
            kind="draft", slug="depicting2026fig", title="the depicting figure"
        ).id
        store.add_link(src_ref_id=thing, dst_ref_id=figure, relation="depicted-in")
        assert [r.ref_id for r in store.dependants(thing).rows] == [figure]
        assert store.dependants(figure).rows == ()

    def test_authored_pair_reports_artifact_dependant(self, store: Store) -> None:
        """``orcid -authored-> paper`` and ``paper -authored-by-> orcid``
        are the same fact: the paper leans on its author. Either stored
        spelling surfaces the paper on the author's walk — and never the
        author on the paper's."""
        author = store.insert_ref(
            kind="orcid", slug="orcid-dep-2026", title="the author node"
        ).id
        p1 = _mk_paper(store, "auth2026a")
        p2 = _mk_paper(store, "auth2026b")
        store.add_link(src_ref_id=author, dst_ref_id=p1, relation="authored")
        store.add_link(src_ref_id=p2, dst_ref_id=author, relation="authored-by")
        assert sorted(r.ref_id for r in store.dependants(author).rows) == [
            p1,
            p2,
        ]
        assert store.dependants(p1).rows == ()
        assert store.dependants(p2).rows == ()

    def test_contradicted_by_reports_claim_dependant(self, store: Store) -> None:
        """``claim -contradicted-by-> evidence`` ≡ ``evidence
        -contradicts-> claim``: ``contradicts`` is src-depends (the
        contradicting record points at its target), so the passive
        stores the rely party at dst — the evidence is the claim's
        dependant."""
        claim = _mk_ref(store, "memory", "the contradicted claim")
        evidence = _mk_ref(store, "memory", "the counter-evidence")
        store.add_link(
            src_ref_id=claim, dst_ref_id=evidence, relation="contradicted-by"
        )
        assert [r.ref_id for r in store.dependants(claim).rows] == [evidence]
        assert store.dependants(evidence).rows == ()

    def test_fixed_by_reports_fix_dependant(self, store: Store) -> None:
        """``gripe -fixed-by-> job`` ≡ ``job -fixes-> gripe``: the fix
        relies on the gripe it resolves."""
        gripe = _mk_ref(store, "memory", "the complaint record")
        job = _mk_ref(store, "job", "the fixing job")
        store.add_link(src_ref_id=gripe, dst_ref_id=job, relation="fixed-by")
        assert [r.ref_id for r in store.dependants(gripe).rows] == [job]

    def test_quantified_by_reports_paper_dependant(self, store: Store) -> None:
        """``measurand -quantified-by-> paper`` ≡ ``paper -quantifies->
        measurand``: the paper's record points at the measurand — the
        paper is the measurand's dependant."""
        measurand = _mk_ref(store, "taxon", "the measurand term")
        p = _mk_paper(store, "quant2026dep")
        store.add_link(src_ref_id=measurand, dst_ref_id=p, relation="quantified-by")
        assert [r.ref_id for r in store.dependants(measurand).rows] == [p]

    def test_realizes_reports_design_dependant(self, store: Store) -> None:
        """``component -realizes-> design``: a producer→artifact active
        like ``makes``/``authored`` — the design leans on the component
        that makes it real."""
        component = _mk_ref(store, "memory", "the procurable component")
        design = _mk_ref(store, "todo", "the calling design")
        store.add_link(src_ref_id=component, dst_ref_id=design, relation="realizes")
        assert [r.ref_id for r in store.dependants(component).rows] == [design]

    def test_cited_in_reports_dossier_dependant(self, store: Store) -> None:
        """``paper -cited-in-> dossier`` (src=paper, dst=dossier per the
        0085 comment): the dossier's record leans on the paper."""
        p = _mk_paper(store, "citedin2026dep")
        dossier = store.insert_ref(
            kind="draft", slug="dossier2026dep", title="the citing dossier"
        ).id
        store.add_link(src_ref_id=p, dst_ref_id=dossier, relation="cited-in")
        assert [r.ref_id for r in store.dependants(p).rows] == [dossier]
        assert store.dependants(dossier).rows == ()


class TestPaging:
    def _fan_out(self, store: Store, n: int = 60) -> int:
        focus = _mk_paper(store, "fan2026out")
        for i in range(n):
            dep = _mk_ref(store, "memory", f"citing note {i:02d}")
            store.add_link(src_ref_id=dep, dst_ref_id=focus, relation="cites")
        return focus

    def test_limit_pages_through_fanout(self, store: Store) -> None:
        """60 direct dependants, limit=25 → 25 + 25 + 10, complete on
        the third page."""
        focus = self._fan_out(store)
        p1 = store.dependants(focus, limit=25)
        assert p1.shown == 25
        assert p1.total_seen == 60
        assert p1.complete is False
        assert p1.next_cursor == "dep:25"
        p2 = store.dependants(focus, limit=25, cursor=p1.next_cursor)
        assert p2.shown == 25
        assert p2.complete is False
        assert p2.next_cursor == "dep:50"
        p3 = store.dependants(focus, limit=25, cursor=p2.next_cursor)
        assert p3.shown == 10
        assert p3.complete is True
        assert p3.next_cursor is None
        ids = {r.ref_id for r in p1.rows + p2.rows + p3.rows}
        assert len(ids) == 60

    def test_empty_result_complete(self, store: Store) -> None:
        """``complete`` is True only when the frontier was exhausted —
        a truly dependant-free ref reports it honestly."""
        lonely = _mk_paper(store, "lonely2026ref")
        page = store.dependants(lonely)
        assert page.rows == ()
        assert page.total_seen == 0
        assert page.uninspected == 0
        assert page.complete is True

    def test_node_budget_marks_uninspected(self, store: Store) -> None:
        """budget=10 on a 60-node fan-out: discovered nodes the budget
        left unexpanded land in ``uninspected`` — never read as "no
        dependants"."""
        focus = self._fan_out(store)
        page = store.dependants(focus, depth=2, node_budget=10)
        assert page.uninspected > 0
        assert page.complete is False
        # Level-1 discovery itself isn't budget-capped: all 60 dependants
        # are *seen*; their own rely-side edges are what went unexpanded.
        assert page.total_seen == 60

    def test_bad_cursor_refused(self, store: Store) -> None:
        focus = self._fan_out(store, n=3)
        for bad in ("bogus", "dep:", "dep:-1", "dep:abc", "page:2"):
            with pytest.raises(BadInput, match="cursor"):
                store.dependants(focus, cursor=bad)

    def test_out_of_range_cursor_refused(self, store: Store) -> None:
        """A cursor at or past the seen count was never minted —
        answering it with an empty page would read as "no dependants"
        (review td681243: ``dep:999999`` used to return one)."""
        focus = self._fan_out(store, n=5)
        with pytest.raises(BadInput, match="cursor"):
            store.dependants(focus, cursor="dep:5")
        with pytest.raises(BadInput, match="cursor"):
            store.dependants(focus, cursor="dep:999999")
        lonely = _mk_paper(store, "emptycursor2026")
        with pytest.raises(BadInput, match="cursor"):
            store.dependants(lonely, cursor="dep:0")

    def test_depth_and_limit_bounds(self, store: Store) -> None:
        focus = _mk_paper(store, "bounds2026ref")
        with pytest.raises(BadInput, match="depth"):
            store.dependants(focus, depth=0)
        with pytest.raises(BadInput, match="depth"):
            store.dependants(focus, depth=99)
        with pytest.raises(BadInput, match="limit"):
            store.dependants(focus, limit=0)
        with pytest.raises(BadInput, match="limit"):
            store.dependants(focus, limit=9999)
        with pytest.raises(BadInput, match="node_budget"):
            store.dependants(focus, node_budget=0)

    def test_unknown_ref_is_notfound(self, store: Store) -> None:
        with pytest.raises(NotFound):
            store.dependants(999_999_999)

    def test_retired_target_still_walks(self, store: Store) -> None:
        """The mutation-ack path calls the walk on the tombstone —
        dependants of a retired ref still resolve."""
        p = _mk_paper(store, "tomb2026walk")
        m = _mk_ref(store, "memory", "still-citing note")
        store.add_link(src_ref_id=m, dst_ref_id=p, relation="cites")
        store.retire_ref(p)
        page = store.dependants(p)
        assert [r.ref_id for r in page.rows] == [m]
