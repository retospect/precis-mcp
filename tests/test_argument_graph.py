"""Argument graph, v1 slice (ADR 0054).

Covers each of the five build-order steps:

1. `entails`/`entailed-by` + `qualifies`/`qualified-by` relations, and the
   system-set `STALE:` tag axis.
2. `meta.rule` / `meta.warrant` on `put`/`edit` (kind='memory').
3. `view='argument'` on `get(kind='memory', ...)`.
4. The retraction push hook (`STALE:retracted-premise` ripple).
5. The `precis stats --argument` corpus report — see ``test_stats.py``
   (extended there, alongside the existing findings/stubs sections).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.memory import MemoryHandler
from precis.store.types import _INVERSE_RELATIONS, Tag
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _seed_paper(store, *, cite_key: str) -> int:
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"paper {cite_key}", meta={}
    )
    return ref.id


def _lemma(handler: MemoryHandler, store, *, text: str, cites: int) -> int:
    """A kind:lemma memory that cites ``cites`` (a paper ref_id)."""
    lid = id_of(handler.put(text=text, tags=["kind:lemma"]).body)
    store.add_link(src_ref_id=lid, dst_ref_id=cites, relation="cites")
    return lid


def _inference(
    handler: MemoryHandler,
    store,
    *,
    text: str,
    premises: list[int],
    rule: str | None = None,
    warrant: str | None = None,
) -> int:
    iid = id_of(
        handler.put(text=text, tags=["kind:inference"], rule=rule, warrant=warrant).body
    )
    for p in premises:
        store.add_link(src_ref_id=iid, dst_ref_id=p, relation="derived-from")
    return iid


# ---------------------------------------------------------------------------
# Step 1 — relations + STALE axis
# ---------------------------------------------------------------------------


class TestRelationsAndStaleAxis:
    def test_inverse_map_registered(self) -> None:
        assert _INVERSE_RELATIONS["entails"] == "entailed-by"
        assert _INVERSE_RELATIONS["entailed-by"] == "entails"
        assert _INVERSE_RELATIONS["qualifies"] == "qualified-by"
        assert _INVERSE_RELATIONS["qualified-by"] == "qualifies"

    def test_migration_seeded_relations_table(self, store) -> None:
        # Migration 0080 — the DB is the FK authority; store.valid_relations()
        # reads it directly.
        valid = store.valid_relations()
        assert {"entails", "entailed-by", "qualifies", "qualified-by"} <= valid

    def test_entails_auto_mirrors_at_read_time(self, store) -> None:
        a = store.insert_ref(kind="memory", slug=None, title="I", meta={}).id
        b = store.insert_ref(kind="memory", slug=None, title="Z", meta={}).id
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="entails")
        # Physical row is a->b relation='entails' — links_for returns the
        # row as stored (not rewritten), so the matched row's src_ref_id
        # is still `a`; the inverse rewrite is what let it match a
        # direction='out'/relation='entailed-by' query issued from b.
        inbound = store.links_for(b, direction="out", relation="entailed-by")
        assert {link.src_ref_id for link in inbound} == {a}

    def test_qualifies_auto_mirrors_at_read_time(self, store) -> None:
        caveat = store.insert_ref(kind="memory", slug=None, title="caveat", meta={}).id
        claim = store.insert_ref(kind="memory", slug=None, title="claim", meta={}).id
        store.add_link(src_ref_id=caveat, dst_ref_id=claim, relation="qualifies")
        inbound = store.links_for(claim, direction="out", relation="qualified-by")
        assert {link.src_ref_id for link in inbound} == {caveat}

    def test_stale_axis_accepted_on_memory(self) -> None:
        # Registered in _CLOSED_VOCAB + _KIND_ALLOWED_AXES['memory'] —
        # parse_strict must accept it (write-time enforcement is a
        # separate concern, see test_agent_cannot_tag_stale below).
        tag = Tag.parse_strict("STALE:retracted-premise", kind="memory")
        assert str(tag) == "STALE:retracted-premise"

    def test_stale_bad_value_rejected(self) -> None:
        with pytest.raises(BadInput):
            Tag.parse_strict("STALE:bogus", kind="memory")

    def test_stale_axis_rejected_on_todo(self) -> None:
        # STALE: is memory-only in v1 (the argument graph lives on memory).
        with pytest.raises(BadInput, match="axis not allowed"):
            Tag.parse_strict("STALE:retracted-premise", kind="todo")

    def test_agent_cannot_add_stale_via_tag_verb(self, handler: MemoryHandler) -> None:
        mid = id_of(handler.put(text="an inference", tags=["kind:inference"]).body)
        with pytest.raises(BadInput, match="system-set"):
            handler.tag(id=mid, add=["STALE:retracted-premise"])

    def test_agent_cannot_remove_stale_via_tag_verb(
        self, handler: MemoryHandler, store
    ) -> None:
        mid = id_of(handler.put(text="an inference", tags=["kind:inference"]).body)
        store.add_tag(mid, Tag.closed("STALE", "retracted-premise"), set_by="system")
        with pytest.raises(BadInput, match="system-set"):
            handler.tag(id=mid, remove=["STALE:retracted-premise"])

    def test_ordinary_tags_still_work_on_memory(self, handler: MemoryHandler) -> None:
        # The STALE: guard shouldn't swallow normal tag() calls.
        mid = id_of(handler.put(text="x").body)
        handler.tag(id=mid, add=["kind:lemma"])
        got = handler.get(id=mid)
        assert "kind:lemma" in got.body


# ---------------------------------------------------------------------------
# Step 2 — meta.rule / meta.warrant
# ---------------------------------------------------------------------------


class TestRuleAndWarrant:
    def test_put_accepts_rule_and_warrant(self, handler: MemoryHandler, store) -> None:
        mid = id_of(
            handler.put(
                text="from X and Y, Z",
                tags=["kind:inference"],
                rule="and-intro",
                warrant="both hold under the same ambient",
            ).body
        )
        ref = store.get_ref(kind="memory", id=mid)
        assert ref.meta["rule"] == "and-intro"
        assert ref.meta["warrant"] == "both hold under the same ambient"

    def test_rule_and_warrant_rendered_in_get(self, handler: MemoryHandler) -> None:
        mid = id_of(
            handler.put(
                text="from X and Y, Z",
                tags=["kind:inference"],
                rule="and-intro",
                warrant="why",
            ).body
        )
        got = handler.get(id=mid)
        assert "rule: and-intro" in got.body
        assert "warrant: why" in got.body

    def test_put_without_rule_warrant_omits_them(
        self, handler: MemoryHandler, store
    ) -> None:
        mid = id_of(handler.put(text="plain memory").body)
        ref = store.get_ref(kind="memory", id=mid)
        assert "rule" not in ref.meta
        assert "warrant" not in ref.meta

    def test_edit_sets_warrant_without_text(
        self, handler: MemoryHandler, store
    ) -> None:
        mid = id_of(handler.put(text="original body", tags=["kind:inference"]).body)
        handler.edit(id=mid, warrant="refined justification")
        ref = store.get_ref(kind="memory", id=mid)
        assert ref.meta["warrant"] == "refined justification"
        # Body untouched.
        assert handler._body_text(ref) == "original body"

    def test_edit_sets_rule_without_text(self, handler: MemoryHandler, store) -> None:
        mid = id_of(handler.put(text="original body", tags=["kind:inference"]).body)
        handler.edit(id=mid, rule="modus-ponens")
        ref = store.get_ref(kind="memory", id=mid)
        assert ref.meta["rule"] == "modus-ponens"

    def test_edit_with_text_also_updates_meta(
        self, handler: MemoryHandler, store
    ) -> None:
        mid = id_of(handler.put(text="v1", tags=["kind:inference"]).body)
        handler.edit(id=mid, text="v2", rule="abduction")
        ref = store.get_ref(kind="memory", id=mid)
        assert handler._body_text(ref) == "v2"
        assert ref.meta["rule"] == "abduction"

    def test_edit_requires_at_least_one_of_text_rule_warrant(
        self, handler: MemoryHandler
    ) -> None:
        mid = id_of(handler.put(text="x").body)
        with pytest.raises(BadInput, match="text=, rule=, or warrant="):
            handler.edit(id=mid)


# ---------------------------------------------------------------------------
# Step 3 — view='argument'
# ---------------------------------------------------------------------------


class TestArgumentView:
    def test_renders_premises_rule_warrant_conclusion(
        self, handler: MemoryHandler, store
    ) -> None:
        pa = _seed_paper(store, cite_key="paperA")
        pb = _seed_paper(store, cite_key="paperB")
        la = _lemma(handler, store, text="A claims X", cites=pa)
        lb = _lemma(handler, store, text="B claims Y", cites=pb)
        infer = _inference(
            handler,
            store,
            text="from X and Y, Z",
            premises=[la, lb],
            rule="and-intro",
            warrant="both hold under N2",
        )
        z = id_of(handler.put(text="Z", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=z, relation="entails")

        out = handler.get(id=infer, view="argument").body
        assert "and-intro" in out
        assert "both hold under N2" in out
        assert "A claims X" in out
        assert "B claims Y" in out
        assert "Z" in out

    def test_flags_premise_citing_retracted_paper(
        self, handler: MemoryHandler, store
    ) -> None:
        pa = _seed_paper(store, cite_key="retracted-src")
        la = _lemma(handler, store, text="A claims X", cites=pa)
        infer = _inference(handler, store, text="from X, Z", premises=[la])

        notice = store.insert_ref(
            kind="paper", slug="notice1", title="notice", meta={}
        ).id
        store.add_link(src_ref_id=notice, dst_ref_id=pa, relation="retracts")

        out = handler.get(id=infer, view="argument").body
        assert "STALE-SOURCE" in out

    def test_flags_inherited_caveat(self, handler: MemoryHandler, store) -> None:
        pa = _seed_paper(store, cite_key="paperA")
        la = _lemma(handler, store, text="A claims X", cites=pa)
        infer = _inference(handler, store, text="from X, Z", premises=[la])

        caveat = id_of(
            handler.put(text="only validated for n<100", tags=["kind:caveat"]).body
        )
        store.add_link(src_ref_id=caveat, dst_ref_id=la, relation="qualifies")

        out = handler.get(id=infer, view="argument").body
        assert "inherited" in out
        assert "only validated for n<100" in out

    def test_view_on_lemma_shows_upstream_inference(
        self, handler: MemoryHandler, store
    ) -> None:
        pa = _seed_paper(store, cite_key="paperA")
        la = _lemma(handler, store, text="A claims X", cites=pa)
        infer = _inference(
            handler, store, text="from X, Z", premises=[la], rule="modus-ponens"
        )
        z = id_of(handler.put(text="Z conclusion", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=infer, dst_ref_id=z, relation="entails")

        out = handler.get(id=z, view="argument").body
        assert "modus-ponens" in out
        assert "A claims X" in out

    def test_rejects_on_plain_memory(self, handler: MemoryHandler) -> None:
        mid = id_of(handler.put(text="just a note").body)
        with pytest.raises(BadInput, match="kind:lemma"):
            handler.get(id=mid, view="argument")

    def test_unknown_view_lists_argument_as_an_option(
        self, handler: MemoryHandler
    ) -> None:
        mid = id_of(handler.put(text="x").body)
        with pytest.raises(Unsupported) as exc:
            handler.get(id=mid, view="bogus")
        assert exc.value.options is not None
        assert "argument" in exc.value.options


# ---------------------------------------------------------------------------
# Step 4 — retraction push hook
# ---------------------------------------------------------------------------


class TestRetractionPushHook:
    def _graph(self, handler: MemoryHandler, store):
        """paper -> lemma -> inference -> conclusion lemma -> inference2."""
        paper = _seed_paper(store, cite_key="root-paper")
        lemma = _lemma(handler, store, text="claims X", cites=paper)
        infer1 = _inference(handler, store, text="from X, Y", premises=[lemma])
        concl = id_of(handler.put(text="Y", tags=["kind:lemma"]).body)
        store.add_link(src_ref_id=infer1, dst_ref_id=concl, relation="entails")
        infer2 = _inference(handler, store, text="from Y, W", premises=[concl])
        return paper, infer1, infer2

    def test_add_retracts_edge_tags_direct_inference(
        self, handler: MemoryHandler, store
    ) -> None:
        paper, infer1, infer2 = self._graph(handler, store)
        assert not any(
            str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1)
        )

        notice = store.insert_ref(
            kind="paper", slug="notice", title="notice", meta={}
        ).id
        store.add_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")

        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

    def test_ripple_is_transitive_to_downstream_inference(
        self, handler: MemoryHandler, store
    ) -> None:
        paper, infer1, infer2 = self._graph(handler, store)
        notice = store.insert_ref(
            kind="paper", slug="notice", title="notice", meta={}
        ).id
        store.add_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")

        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer2))

    def test_raises_concern_about_also_ripples(
        self, handler: MemoryHandler, store
    ) -> None:
        paper, infer1, _infer2 = self._graph(handler, store)
        notice = store.insert_ref(
            kind="paper", slug="notice", title="notice", meta={}
        ).id
        store.add_link(
            src_ref_id=notice, dst_ref_id=paper, relation="raises-concern-about"
        )
        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

    def test_paper_retracted_by_form_also_ripples(
        self, handler: MemoryHandler, store
    ) -> None:
        """Automated provenance write-through stores the OTHER physical
        direction (paper --retracted-by--> notice) — the hook must catch
        this form too."""
        paper, infer1, _infer2 = self._graph(handler, store)
        notice = store.insert_ref(
            kind="paper", slug="notice", title="notice", meta={}
        ).id
        store.add_link(src_ref_id=paper, dst_ref_id=notice, relation="retracted-by")
        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

    def test_removing_the_only_retraction_edge_clears_the_tag(
        self, handler: MemoryHandler, store
    ) -> None:
        paper, infer1, _infer2 = self._graph(handler, store)
        notice = store.insert_ref(
            kind="paper", slug="notice", title="notice", meta={}
        ).id
        store.add_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")
        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

        store.remove_link(src_ref_id=notice, dst_ref_id=paper, relation="retracts")
        assert not any(
            str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1)
        )

    def test_removing_one_of_two_retraction_edges_keeps_tag(
        self, handler: MemoryHandler, store
    ) -> None:
        paper, infer1, _infer2 = self._graph(handler, store)
        notice_a = store.insert_ref(
            kind="paper", slug="notice-a", title="a", meta={}
        ).id
        notice_b = store.insert_ref(
            kind="paper", slug="notice-b", title="b", meta={}
        ).id
        store.add_link(src_ref_id=notice_a, dst_ref_id=paper, relation="retracts")
        store.add_link(
            src_ref_id=notice_b, dst_ref_id=paper, relation="raises-concern-about"
        )
        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

        store.remove_link(src_ref_id=notice_a, dst_ref_id=paper, relation="retracts")
        # Second (concern) edge still reaches — tag must survive.
        assert any(str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1))

        store.remove_link(
            src_ref_id=notice_b, dst_ref_id=paper, relation="raises-concern-about"
        )
        assert not any(
            str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1)
        )

    def test_unrelated_paper_retraction_does_not_tag(
        self, handler: MemoryHandler, store
    ) -> None:
        _paper, infer1, _infer2 = self._graph(handler, store)
        other_paper = _seed_paper(store, cite_key="unrelated")
        notice = store.insert_ref(kind="paper", slug="notice2", title="n2", meta={}).id
        store.add_link(src_ref_id=notice, dst_ref_id=other_paper, relation="retracts")
        assert not any(
            str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1)
        )

    def test_ordinary_link_writes_are_not_affected(
        self, handler: MemoryHandler, store
    ) -> None:
        """The hook only fires for the 4 retraction/concern relations —
        a plain related-to/cites/derived-from write must stay a cheap
        no-op (no tag mutation)."""
        paper, infer1, _infer2 = self._graph(handler, store)
        other = _seed_paper(store, cite_key="other")
        store.add_link(src_ref_id=paper, dst_ref_id=other, relation="related-to")
        assert not any(
            str(t) == "STALE:retracted-premise" for t in store.tags_for(infer1)
        )
