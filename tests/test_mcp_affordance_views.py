"""gr503059 + gr503028 — the read affordances that used to need prod-psql:

- ``get(kind='taxon', view='meta')`` — slug / display_unit /
  required_conditions / axis rows over an id list or ``under=``.
- ``get(kind='paper', view='chunks')`` — counts by chunk_kind and
  section tail + totals; ``id=[…]`` batches.
- ``get(kind='quest', view='served', args={'has_fulltext': …})`` — the
  served-by listing with a fulltext column + count line, and
  ``search(kind='paper', args={'serves': …, 'has_fulltext': …})``.
- ``search(view='plan')`` — the route a kind-scoped search would take
  (index + why) without running the ranking.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput

pytestmark = pytest.mark.db

TOF = "turnover frequency — cycles per active site per unit time"


def _taxon(store: Any) -> Any:
    from precis.handlers.taxon import TaxonHandler

    return TaxonHandler(hub=Hub(store=store))


def _tn(resp: Any) -> int:
    m = re.search(r"\btn(\d+)\b", resp.body)
    assert m is not None
    return int(m.group(1))


class TestTaxonMetaView:
    def test_single_and_batch_rows_carry_the_keys(self, store: Any) -> None:
        h = _taxon(store)
        a = _tn(
            h.put(
                text=TOF,
                meta={
                    "dimension_kind": "dimensionless",
                    "display_unit": "%",
                    "required_conditions": ["product"],
                },
            )
        )
        b = _tn(h.put(text="site time yield — product per site per time"))
        single = h.get(id=f"tn{a}", view="meta").body
        assert "turnover frequency" in single
        assert "turnover-frequency" in single
        assert "%" in single
        assert "product" in single
        batch = h.get(id=[f"tn{a}", f"tn{b}"], view="meta").body
        assert "# 2 taxon meta" in batch
        assert "site time yield" in batch

    def test_under_lists_descendants_with_axis(self, store: Any) -> None:
        h = _taxon(store)
        parent = _tn(h.put(text="catalysis metric — a metric"))
        child = _tn(h.put(text="selectivity — fraction to the wanted product"))
        h.link(
            id=child,
            target=f"taxon:{parent}",
            rel="specialises",
            meta={"axis": "metric"},
        )
        body = h.get(under=f"tn{parent}", view="meta").body
        assert "under" in body
        assert "selectivity" in body and "selectivity" in body
        assert "metric" in body
        # the header names the parent; the parent itself is never a row
        rows = body.split("\n\n", 1)[1]
        assert f"tn{child}" in rows
        assert f"tn{parent}" not in rows

    def test_cap_and_under_gating(self, store: Any) -> None:
        h = _taxon(store)
        with pytest.raises(BadInput) as ei:
            h.get(id=list(range(60)), view="meta")
        assert "50" in str(ei.value)
        with pytest.raises(BadInput):
            h.get(under="tn1")
        with pytest.raises(BadInput):
            h.get(view="meta")


class TestPaperChunksView:
    def _mk(self, store: Any, slug: str) -> int:
        ref = store.insert_ref(kind="paper", slug=slug, title=slug, year=2024)
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
                "meta, section_path) VALUES "
                "(%s,'system',-1,'card_combined','card','{}','{}'),"
                "(%s,'system',0,'paragraph','p0','{}','{Intro,Scope}'),"
                "(%s,'system',1,'paragraph','p1','{}','{Intro,Scope}'),"
                "(%s,'system',2,'references','r0','{}','{References}')",
                (ref.id, ref.id, ref.id, ref.id),
            )
            conn.commit()
        return ref.id

    def test_counts_by_kind_and_section(self, store: Any) -> None:
        from precis.handlers.paper import PaperHandler

        rid = self._mk(store, "chunkstats")
        h = PaperHandler(hub=Hub(store=store))
        body = h.get(id="chunkstats", view="chunks").body
        assert "total: 4" in body and "body: 3" in body
        assert "paragraph: 2" in body
        assert "card_combined: 1" in body
        assert "references: 1" in body
        assert "Scope: 2" in body
        assert "References: 1" in body
        assert (
            sum(
                int(m)
                for m in re.findall(
                    r"^  \S.*: (\d+)$", body.split("by section tail:")[1], re.M
                )
            )
            == 3
        )

    def test_batch_and_cap(self, store: Any) -> None:
        from precis.handlers.paper import PaperHandler

        self._mk(store, "cb1")
        h = PaperHandler(hub=Hub(store=store))
        body = h.get(id=["cb1"], view="chunks").body
        assert "cb1" in body and "total:" in body
        with pytest.raises(BadInput) as ei:
            h.get(id=["cb1"] * 51, view="chunks")
        assert "50" in str(ei.value)


class TestQuestServed:
    def _mk(self, store: Any) -> tuple[Any, int, int, int]:
        from precis.handlers.quest import QuestHandler

        h = QuestHandler(hub=Hub(store=store))
        q = store.insert_ref(kind="quest", slug=None, title="the striving")
        p_ft = store.insert_ref(kind="paper", slug="ft", title="full paper")
        p_stub = store.insert_ref(kind="paper", slug="stub", title="abstract only")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta)"
                " VALUES (%s,'system',0,'paragraph','body','{}')",
                (p_ft.id,),
            )
            conn.execute(
                "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) VALUES"
                " (%s,%s,'serves','system'),(%s,%s,'serves','system')",
                (p_ft.id, q.id, p_stub.id, q.id),
            )
            conn.commit()
        return h, q.id, p_ft.id, p_stub.id

    def test_served_listing_and_filter(self, store: Any) -> None:
        h, qid, _ft, _stub = self._mk(store)
        body = h.get(id=qid, view="served").body
        assert "2 serving" in body and "1 with fulltext" in body
        assert "fulltext=yes" in body and "fulltext=no" in body
        only = h.get(id=qid, view="served", args={"has_fulltext": True}).body
        assert "1 serving" in only and "fulltext=yes" in only
        assert "fulltext=no" not in only
        none = h.get(id=qid, view="served", args={"has_fulltext": False}).body
        assert "1 serving" in none and "fulltext=no" in none
        assert "fulltext=yes" not in none

    def test_paper_search_serves_and_fulltext(self, store: Any) -> None:
        from precis.handlers.paper import PaperHandler

        h, qid, _ft, _stub = self._mk(store)
        ph = PaperHandler(hub=Hub(store=store))
        # `serves` alone restricts to the quest's servers
        # (a non-serving paper with a matching body chunk must not appear)
        other = store.insert_ref(kind="paper", slug="other", title="elsewhere")
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta)"
                " VALUES (%s,'system',0,'paragraph','body','{}')",
                (other.id,),
            )
            conn.commit()
        unscoped = ph.search(q="body", args=None).body
        assert "# 2 papers" in unscoped
        body = ph.search(q="body", args=None, serves=f"quest:{qid}").body
        assert "# 1 paper" in body
        # has_fulltext=True drops the bodiless server
        body2 = ph.search(q="body", serves=f"quest:{qid}", has_fulltext=True).body
        assert "# 1 paper" in body2
        with pytest.raises(BadInput):
            ph.search(q="x", serves="notaquest")


class TestSearchPlanView:
    def test_plan_names_index_or_fallback_and_never_ranks(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.config import PrecisConfig
        from precis.dispatch import boot
        from precis.runtime import PrecisRuntime

        rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))
        # The plan must not run any ranking — kill the ranking doors.
        monkeypatch.setattr(
            store.chunks,
            "search_chunks",
            lambda **_kw: pytest.fail("ranking ran on view='plan'"),
        )
        monkeypatch.setattr(
            store.chunks,
            "search_chunks_multi",
            lambda **_kw: pytest.fail("ranking ran on view='plan'"),
        )
        body = rt.dispatch("search", {"kind": "memory", "q": "x", "view": "plan"})
        assert "no ranking executed" in body
        assert "mode: hybrid" in body
        assert "kind=memory:" in body
        # memory is in _ANN_PARTIAL_KINDS: either its partial index or the
        # named global fallback, never silence.
        assert (
            "chunk_embeddings_vec_hnsw_memory_idx" in body
            or "chunk_embeddings_vec_hnsw_idx" in body
        )
        assert ("route:" in body and "reason" not in body.lower()) or "—" in body
        assert "ef_search" in body

    def test_plan_exact_kind_and_wildcard(self, store: Any) -> None:
        from precis.config import PrecisConfig
        from precis.dispatch import boot
        from precis.runtime import PrecisRuntime

        rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))
        # a kind not in _ANN_PARTIAL_KINDS/_ANN_GLOBAL_KINDS → exact scan
        body = rt.dispatch("search", {"kind": "taxon", "view": "plan"})
        assert "kind=taxon:" in body
        assert "exact" in body or "none (exact scan)" in body
        wide = rt.dispatch("search", {"view": "plan"})
        assert "kind=" in wide and "index:" in wide

    def test_plan_accepts_filters_and_names_the_route(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """The real search's filters are accepted, echoed, and used to
        pick the path the same call would take — not refused."""
        from precis.config import PrecisConfig
        from precis.dispatch import boot
        from precis.runtime import PrecisRuntime

        rt = PrecisRuntime(config=PrecisConfig(), hub=boot(store=store))
        body = rt.dispatch(
            "search",
            {"kind": "memory", "view": "plan", "tags": ["x"], "status": "open"},
        )
        assert "[error" not in body, body
        assert "path: single-kind search on kind='memory'" in body, body
        assert "filters:" in body and "status='open'" in body, body
        body = rt.dispatch(
            "search", {"kind": "paper", "view": "plan", "q": "x", "sort": "recency"}
        )
        assert "path: cross-kind source search" in body, body
        body = rt.dispatch("search", {"kind": "paper", "view": "plan", "folder": 5})
        assert "path: cross-kind fan-out" in body, body
        body = rt.dispatch("search", {"view": "plan"})
        assert "path: cross-kind fan-out" in body, body
