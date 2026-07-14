"""Tests for the `concept` kind + concept-graph relations (reading-prep loop,
slice 2). Grows with the handler + promotion sub-slices.

The relation tests run against real PG (the ``store`` fixture) so they exercise
the migration seed + the auto-mirror; they use plain seeded refs (relations are
generic ref→ref), so they don't depend on the concept handler yet.
"""

from __future__ import annotations

from typing import Any


class TestConceptRelations:
    def test_has_prerequisite_roundtrip_and_mirror(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        advanced = seed_ref(store, title="backpropagation")
        basic = seed_ref(store, title="the chain rule")
        # `advanced has-prerequisite basic`  ⇒  learn `basic` before `advanced`.
        store.add_link(
            src_ref_id=advanced, dst_ref_id=basic, relation="has-prerequisite"
        )

        out = store.links_for(advanced, direction="out", relation="has-prerequisite")
        assert any(link.dst_ref_id == basic for link in out)
        # The inverse is resolved at read time (one row per edge, no mirror row):
        # asking `prerequisite-of` from `basic` surfaces the same stored
        # `has-prerequisite` row, which touches `advanced`.
        inv = store.links_for(basic, direction="out", relation="prerequisite-of")
        assert any(advanced in (link.src_ref_id, link.dst_ref_id) for link in inv)

    def test_symmetric_analogy_edge(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        a = seed_ref(store, title="a spring")
        b = seed_ref(store, title="an LC circuit")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="analogy-of")

        # Symmetric (no inverse row) — surfaced from either end via direction=both.
        both = store.links_for(b, direction="both", relation="analogy-of")
        assert any(link.src_ref_id == a or link.dst_ref_id == a for link in both)

    def test_contrasts_with_is_registered(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        a = seed_ref(store, title="affect")
        b = seed_ref(store, title="effect")
        # Registration smoke: the relation resolves (no FK violation).
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contrasts-with")
        both = store.links_for(a, direction="both", relation="contrasts-with")
        assert any(link.src_ref_id == b or link.dst_ref_id == b for link in both)


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.concept import ConceptHandler

    return ConceptHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    import re

    m = re.search(r"\bcn(\d+)\b", resp.body)
    assert m is not None, f"no concept handle in ack: {resp.body!r}"
    return int(m.group(1))


class TestConceptHandler:
    def test_put_parses_name_def_and_emits_card(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(
            text="backpropagation — reverse-mode autodiff that computes gradients"
        )
        ref = store.get_ref(kind="concept", id=_created_id(resp))
        assert ref.meta["name"] == "backpropagation"
        assert "reverse-mode" in ref.meta["definition"]
        assert ref.meta["mastery"] == 0.0
        assert ref.meta["state"] == "candidate"
        # embeddable card_combined emitted (ord=-1), name+definition.
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (ref.id,)
            ).fetchone()
        assert card is not None
        assert "backpropagation" in card[0] and "gradients" in card[0]

    def test_name_only_leaves_definition_empty(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(text="renormalization")
        ref = store.get_ref(kind="concept", id=_created_id(resp))
        assert ref.meta["name"] == "renormalization"
        assert ref.meta["definition"] == ""

    def test_get_renders_concept(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(text="entropy — a measure of uncertainty")
        body = h.get(id=_created_id(resp)).body
        assert "entropy" in body
        assert "measure of uncertainty" in body
        assert "mastery:" in body

    def test_search_by_name(self, store: Any) -> None:
        h = _handler(store)
        h.put(text="mutual information — shared information between two variables")
        resp = h.search(q="mutual information")
        assert "mutual information" in resp.body.lower()


def _concept_id_by_name(store: Any, name: str) -> int | None:
    from precis.reading.concepts import normalize_name

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind='concept' AND deleted_at IS NULL "
            "AND meta->>'norm_name' = %s ORDER BY ref_id LIMIT 1",
            (normalize_name(name),),
        ).fetchone()
    return int(row[0]) if row else None


def _glossary_paper(
    store: Any, terms: list[tuple[str, str]], version: str = "1"
) -> int:
    """Seed a paper carrying a card_glossary chunk with the given (name, def) terms."""
    from psycopg.types.json import Jsonb

    from tests.workers._helpers import seed_ref

    pid = seed_ref(store, title="paper for promotion")
    clusters = [
        {
            "name": "Cluster",
            "terms": [{"term": n, "definition": d, "note": ""} for n, d in terms],
        }
    ]
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s, 'system', -1000, 'card_glossary', %s, %s)",
            (
                pid,
                "glossary",
                Jsonb({"glossary_version": version, "clusters": clusters}),
            ),
        )
        conn.commit()
    return pid


class TestPromotion:
    def test_promote_mints_concepts_with_provenance(self, store: Any) -> None:
        import uuid

        from precis.reading.promote import promote_paper

        u = uuid.uuid4().hex[:8]
        t1, t2 = f"alpha-{u}", f"beta-{u}"
        pid = _glossary_paper(store, [(t1, "def one"), (t2, "def two")])

        res = promote_paper(store, paper_id=pid, cohort=f"co-{u}")

        assert res == {"minted": 2, "linked": 0, "terms": 2}
        cid = _concept_id_by_name(store, t1)
        assert cid is not None
        ref = store.get_ref(kind="concept", id=cid)
        assert ref.meta["definition"] == "def one"
        assert f"co-{u}" in ref.meta["cohorts"]
        # provenance: concept --derived-from--> paper
        links = store.links_for(cid, direction="out", relation="derived-from")
        assert any(link.dst_ref_id == pid for link in links)
        # embeddable card emitted
        with store.pool.connection() as conn:
            card = conn.execute(
                "SELECT text FROM chunks WHERE ref_id=%s AND ord=-1", (cid,)
            ).fetchone()
        assert card is not None and t1 in card[0]

    def test_promote_dedups_corpus_wide(self, store: Any) -> None:
        import uuid

        from precis.reading.promote import promote_paper

        u = uuid.uuid4().hex[:8]
        shared = f"shared-{u}"
        p_a = _glossary_paper(store, [(shared, "def A")])
        p_b = _glossary_paper(store, [(shared, "def B")])

        r_a = promote_paper(store, paper_id=p_a, cohort=f"cA-{u}")
        r_b = promote_paper(store, paper_id=p_b, cohort=f"cB-{u}")

        assert r_a["minted"] == 1
        assert r_b["minted"] == 0 and r_b["linked"] == 1
        # one node, both papers as provenance, both cohorts, first definition kept
        cid = _concept_id_by_name(store, shared)
        assert cid is not None
        dsts = {
            link.dst_ref_id
            for link in store.links_for(cid, direction="out", relation="derived-from")
        }
        assert p_a in dsts and p_b in dsts
        ref = store.get_ref(kind="concept", id=cid)
        assert f"cA-{u}" in ref.meta["cohorts"] and f"cB-{u}" in ref.meta["cohorts"]
        assert ref.meta["definition"] == "def A"  # dedup never overwrites

    def test_promote_no_glossary_is_noop(self, store: Any) -> None:
        from precis.reading.promote import promote_paper
        from tests.workers._helpers import seed_ref

        pid = seed_ref(store, title="paper without a glossary")
        assert promote_paper(store, paper_id=pid) == {
            "minted": 0,
            "linked": 0,
            "terms": 0,
        }


class _FakeClient:
    """Records calls; returns a fixed completion text (like ``LlmClient``)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        from types import SimpleNamespace

        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=9)


class TestGraphEdges:
    def _seed_cohort(
        self, store: Any, cohort: str, names: list[tuple[str, str]]
    ) -> dict:
        from precis.reading.promote import create_concept

        return {
            name: create_concept(store, name=name, definition=defn, cohort=cohort)
            for name, defn in names
        }

    def test_infer_writes_typed_edges(self, store: Any) -> None:
        import json
        import uuid

        from precis.reading.graph import infer_edges

        u = uuid.uuid4().hex[:8]
        co = f"cohort-{u}"
        bp, cr, grad = f"backprop-{u}", f"chainrule-{u}", f"grad-{u}"
        ids = self._seed_cohort(
            store,
            co,
            [
                (bp, "reverse-mode autodiff"),
                (cr, "derivative of a composition"),
                (grad, "vector of partial derivatives"),
            ],
        )
        payload = json.dumps(
            {
                "prerequisites": [{"concept": bp, "requires": cr}],
                "analogies": [{"a": grad, "b": cr}],
                "contrasts": [{"a": bp, "b": grad}],
            }
        )
        client = _FakeClient(payload)

        counts = infer_edges(store, cohort=co, client=client)

        assert counts == {
            "prerequisite": 1,
            "analogy": 1,
            "contrast": 1,
            "skipped": 0,
        }
        # bp has-prerequisite cr
        out = store.links_for(ids[bp], direction="out", relation="has-prerequisite")
        assert any(link.dst_ref_id == ids[cr] for link in out)
        # analogy is symmetric — reachable from either end
        an = store.links_for(ids[grad], direction="both", relation="analogy-of")
        assert any(ids[cr] in (link.src_ref_id, link.dst_ref_id) for link in an)

    def test_unknown_and_self_edges_skipped(self, store: Any) -> None:
        import json
        import uuid

        from precis.reading.graph import infer_edges

        u = uuid.uuid4().hex[:8]
        co = f"cohort-{u}"
        a, b = f"aa-{u}", f"bb-{u}"
        self._seed_cohort(store, co, [(a, "def a"), (b, "def b")])
        payload = json.dumps(
            {
                "prerequisites": [
                    {"concept": a, "requires": f"ghost-{u}"},  # unknown → skip
                    {"concept": a, "requires": a},  # self-loop → skip
                    {"concept": a, "requires": b},  # valid
                ],
                "analogies": [],
                "contrasts": [],
            }
        )
        counts = infer_edges(store, cohort=co, client=_FakeClient(payload))
        assert counts["prerequisite"] == 1 and counts["skipped"] == 2

    def test_too_few_concepts_makes_no_call(self, store: Any) -> None:
        import uuid

        from precis.reading.graph import infer_edges

        u = uuid.uuid4().hex[:8]
        co = f"cohort-{u}"
        self._seed_cohort(store, co, [(f"solo-{u}", "only one")])
        client = _FakeClient("{}")
        counts = infer_edges(store, cohort=co, client=client)
        assert counts == {"prerequisite": 0, "analogy": 0, "contrast": 0, "skipped": 0}
        assert client.calls == []  # no LLM call for a <2-concept cohort
