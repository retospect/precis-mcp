"""Tests for the `taxon` kind, stage A (docs/backlog/term-taxonomy.md AC 1):
put/get on the concept pattern, the fixed meta key set, earned status.

Handler tests run against real PG (the ``store`` fixture) so they exercise the
0173 migration seed (kind row + instance-of relation).
"""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

from precis.errors import BadInput, NotFound
from precis.taxonomy.nodes import slugify, validate_taxon_meta

TOF = (
    "turnover frequency — the number of catalytic cycles per active site per unit time"
)


def _handler(store: Any) -> Any:
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler

    return TaxonHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\btn(\d+)\b", resp.body)
    assert m is not None, f"no taxon handle in ack: {resp.body!r}"
    return int(m.group(1))


class TestTaxonPut:
    def test_put_mints_proposed_node_with_card_and_no_body(self, store: Any) -> None:
        h = _handler(store)
        ref = store.get_ref(kind="taxon", id=_created_id(h.put(text=TOF)))
        assert ref.title == "turnover frequency"
        assert ref.meta["status"] == "proposed"
        assert ref.meta["name"] == "turnover frequency"
        assert ref.meta["slug"] == "turnover-frequency"
        assert ref.meta["definition"].startswith("the number of catalytic cycles")
        with store.pool.connection() as conn:
            ords = [
                r[0]
                for r in conn.execute(
                    "select ord from chunks where ref_id=%s order by ord", (ref.id,)
                ).fetchall()
            ]
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (ref.id,)
            ).fetchone()
        assert ords == [-1]  # the card only: no ord 0 body chunk
        assert "turnover frequency" in card[0] and "active site" in card[0]

    def test_put_meta_lands_and_aliases_enter_the_card(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(
            text=TOF,
            meta={
                "dimension_kind": "si",
                "si_vector": "0, 0, -1, 0, 0, 0, 0",
                "canonical_unit": "1/s",
                "aliases": ["TOF"],
            },
        )
        ref = store.get_ref(kind="taxon", id=_created_id(resp))
        assert ref.meta["si_vector"] == "0,0,-1,0,0,0,0"  # canonicalised
        assert ref.meta["canonical_unit"] == "1/s"
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (ref.id,)
            ).fetchone()
        assert "aka TOF" in card[0]

    def test_unknown_meta_key_refused_naming_it(self, store: Any) -> None:
        with pytest.raises(BadInput) as ei:
            _handler(store).put(text=TOF, meta={"mastery": 0.5})
        assert "mastery" in str(ei.value)
        assert "dimension_kind" in (ei.value.next or "")

    def test_si_without_vector_refused(self, store: Any) -> None:
        with pytest.raises(BadInput, match="si_vector"):
            _handler(store).put(text=TOF, meta={"dimension_kind": "si"})

    def test_malformed_si_vector_refused(self, store: Any) -> None:
        with pytest.raises(BadInput, match="seven"):
            _handler(store).put(
                text=TOF, meta={"dimension_kind": "si", "si_vector": "0,0,-1"}
            )

    def test_status_systematic_refused(self, store: Any) -> None:
        with pytest.raises(BadInput, match="earned"):
            _handler(store).put(text=TOF, meta={"status": "systematic"})

    def test_contract_without_start_refused(self, store: Any) -> None:
        with pytest.raises(BadInput, match="start"):
            _handler(store).put(
                text="measurand — a quantity",
                meta={"contract": {"required_keys": ["dimension_kind"]}},
            )

    def test_refused_put_writes_nothing(self, store: Any) -> None:
        name = "ghost-term-zq9"
        with pytest.raises(BadInput):
            _handler(store).put(text=f"{name} — x", meta={"bogus": 1})
        with store.pool.connection() as conn:
            n = conn.execute(
                "select count(*) from refs where kind='taxon' and title=%s", (name,)
            ).fetchone()[0]
        assert n == 0

    def test_start_node_with_contract_accepted(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(
            text="measurand — a quantity that can be measured",
            meta={"start": True, "contract": {"required_keys": ["dimension_kind"]}},
        )
        ref = store.get_ref(kind="taxon", id=_created_id(resp))
        assert ref.meta["contract"] == {"required_keys": ["dimension_kind"]}
        assert "start node (requires: dimension_kind)" in h.get(id=ref.id).body

    def test_other_kind_still_ignores_meta(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.handlers.concept import ConceptHandler

        h = ConceptHandler(hub=Hub(store=store))
        resp = h.put(text="eigenvalue — a scalar", meta={"bogus": 1})
        m = re.search(r"cn(\d+)", resp.body)
        assert m is not None
        ref = store.get_ref(kind="concept", id=int(m[1]))
        assert "bogus" not in ref.meta


class TestTaxonRead:
    def test_get_renders_node(self, store: Any) -> None:
        h = _handler(store)
        resp = h.put(
            text=TOF,
            meta={
                "dimension_kind": "si",
                "si_vector": "0,0,-1,0,0,0,0",
                "aliases": ["TOF"],
            },
        )
        body = h.get(id=_created_id(resp)).body
        assert body.startswith(f"# taxon {_created_id(resp)}: turnover frequency")
        assert "catalytic cycles" in body
        assert "aka: TOF" in body
        assert "status: proposed" in body
        assert "dimension_kind: si" in body
        assert "si_vector: 0,0,-1,0,0,0,0" in body

    def test_boundary_examples_render_on_get_and_stay_out_of_the_card(
        self, store: Any
    ) -> None:
        h = _handler(store)
        rid = _created_id(
            h.put(
                text=TOF,
                meta={
                    "includes": ["CO oxidation cycles per Pt site per second"],
                    "excludes": ["moles of product per gram per hour → zorbspecific"],
                },
            )
        )
        body = h.get(id=rid).body
        assert "includes:\n- CO oxidation cycles" in body
        assert "excludes:\n- moles of product" in body
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (rid,)
            ).fetchone()
        assert "zorbspecific" not in card[0]
        assert "CO oxidation" not in card[0]

    def test_boundary_examples_must_be_nonempty_strings(self, store: Any) -> None:
        with pytest.raises(BadInput, match="excludes must be a list"):
            _handler(store).put(text=TOF, meta={"excludes": "one string"})
        with pytest.raises(BadInput, match="includes must be a list"):
            validate_taxon_meta({"includes": ["ok", " "]})

    def test_boundary_recovery_hint_puts_near_miss_under_excludes(self) -> None:
        with pytest.raises(BadInput) as ei:
            validate_taxon_meta({"includes": "scalar"})
        hint = ei.value.next or ""
        assert "includes=[" in hint
        assert "near-miss" not in hint
        with pytest.raises(BadInput) as ei:
            validate_taxon_meta({"excludes": "scalar"})
        hint = ei.value.next or ""
        assert "excludes=[" in hint
        assert "near-miss" in hint

    def test_definition_is_in_the_searchable_card_and_name_search_hits(
        self, store: Any
    ) -> None:
        # The harness fills no embeddings (a worker does), so AC 1's "found on
        # definition similarity alone" is covered up to the card: a word that
        # appears only in the definition is in the ord -1 card's tsvector (the
        # text the embed worker vectorizes), and the node is found by name.
        h = _handler(store)
        rid = _created_id(
            h.put(text="zorbulation rate — frequency of quuxified catalytic events")
        )
        with store.pool.connection() as conn:
            hit = conn.execute(
                "select count(*) from chunks where ref_id=%s and ord=-1 "
                "and tsv @@ websearch_to_tsquery('english', 'quuxified')",
                (rid,),
            ).fetchone()[0]
        assert hit == 1
        assert "zorbulation rate" in h.search(q="zorbulation rate").body


class TestPureHelpers:
    def test_slugify(self) -> None:
        assert slugify("Turnover Frequency (TOF)") == "turnover-frequency-tof"

    def test_validate_rejects_si_vector_without_si(self) -> None:
        with pytest.raises(BadInput, match="only allowed"):
            validate_taxon_meta(
                {"dimension_kind": "count", "si_vector": "0,0,0,0,0,0,0"}
            )

    def test_validate_rejects_bad_dimension_kind(self) -> None:
        with pytest.raises(BadInput, match="dimension_kind"):
            validate_taxon_meta({"dimension_kind": "volume"})

    def test_instance_of_relation_registered_with_inverse(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        a = seed_ref(store, title="a catalyst")
        b = seed_ref(store, title="a term")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="instance-of")
        inv = store.links_for(b, direction="out", relation="has-instance")
        assert any(a in (lk.src_ref_id, lk.dst_ref_id) for lk in inv)


# ── stage B: hierarchy (docs/backlog/term-taxonomy.md AC 3-5) ──────────────


def _mk(
    store: Any,
    name: str,
    *,
    meta: dict[str, Any] | None = None,
    under: int | None = None,
    rel: str = "specialises",
    definition: str = "a thing. It has a second sentence",
) -> int:
    """Put a taxon (optionally under a parent via put(link=)); return its id."""
    kw: dict[str, Any] = {}
    if under is not None:
        kw["link"] = f"taxon:{under}"
        kw["rel"] = rel
        kw["dedup"] = False  # fixtures share boilerplate definitions
    return _created_id(
        _handler(store).put(text=f"{name} — {definition}", meta=meta, **kw)
    )


def _start(store: Any, name: str, required: list[str] | None = None) -> int:
    meta: dict[str, Any] = {"start": True}
    if required is not None:
        meta["contract"] = {"required_keys": required}
    return _mk(store, name, meta=meta)


def _link(
    store: Any,
    child: int,
    parent: int,
    *,
    axis: str | None = None,
    rel: str = "specialises",
) -> Any:
    return _handler(store).link(
        id=child,
        target=f"taxon:{parent}",
        rel=rel,
        meta={"axis": axis} if axis else None,
    )


def _count_titled(store: Any, title: str) -> int:
    with store.pool.connection() as conn:
        return int(
            conn.execute(
                "select count(*) from refs where kind='taxon' and title=%s", (title,)
            ).fetchone()[0]
        )


def _ids(rows: Any) -> set[int]:
    return {r[0] for r in rows}


def _created_memory(store: Any, text: str) -> int:
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler

    resp = MemoryHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bme(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


@pytest.fixture
def mounted_runtime(runtime_with_store: Any) -> Any:
    """Mount a store-backed runtime on ``tools.core`` so the real ``link``
    verb function (the MCP schema) runs end to end."""
    from precis.tools import core

    core._runtime = runtime_with_store
    try:
        yield runtime_with_store
    finally:
        core._runtime = None


def _verb_text(out: Any) -> str:
    content = getattr(out, "content", None)
    return content[0].text if content else str(out)


class TestHierarchyGuard:
    def test_two_node_cycle_refused_naming_both(self, store: Any) -> None:
        a = _mk(store, "cyc2 alpha")
        b = _mk(store, "cyc2 beta", under=a)
        with pytest.raises(BadInput) as ei:
            _link(store, a, b)
        msg = str(ei.value)
        assert f"tn{a}" in msg and f"tn{b}" in msg
        assert "cyc2 alpha" in msg and "cyc2 beta" in msg
        assert "cycle" in msg

    def test_three_node_cycle_refused(self, store: Any) -> None:
        a = _mk(store, "cyc3 a")
        b = _mk(store, "cyc3 b", under=a)
        c = _mk(store, "cyc3 c", under=b)
        with pytest.raises(BadInput, match="cycle") as ei:
            _link(store, a, c)
        assert f"tn{a}" in str(ei.value) and f"tn{c}" in str(ei.value)
        # the inverse stored form is the same edge: c --generalises--> a
        # means a specialises c.
        with pytest.raises(BadInput, match="cycle"):
            _link(store, c, a, rel="generalises")

    def test_self_link_refused(self, store: Any) -> None:
        a = _mk(store, "self linker")
        with pytest.raises(BadInput) as ei:
            _link(store, a, a)
        assert f"tn{a}" in str(ei.value) and "self linker" in str(ei.value)

    def test_non_taxon_endpoint_refused_both_directions_both_doors(
        self, store: Any
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers._link_tag_ops import apply_link_ops
        from precis.handlers.memory import MemoryHandler

        t = _mk(store, "door taxon")
        mem = _created_memory(store, "a memory about doors")
        # link verb, taxon source -> memory target
        with pytest.raises(BadInput) as ei:
            _handler(store).link(id=t, target=f"memory:{mem}", rel="specialises")
        assert "memory" in str(ei.value) and f"me{mem}" in str(ei.value)
        # link verb, memory source -> taxon target (both stored forms)
        for rel in ("specialises", "generalises"):
            with pytest.raises(BadInput) as ei:
                MemoryHandler(hub=Hub(store=store)).link(
                    id=mem, target=f"taxon:{t}", rel=rel
                )
            assert "memory" in str(ei.value) and f"me{mem}" in str(ei.value)
        # put(link=) door, create-time taxon under a memory: nothing written
        with pytest.raises(BadInput, match="memory"):
            _handler(store).put(
                text="door orphan — x", link=f"memory:{mem}", rel="specialises"
            )
        assert _count_titled(store, "door orphan") == 0
        # put(link=) door, create-time memory over a taxon
        with pytest.raises(BadInput, match="memory"):
            MemoryHandler(hub=Hub(store=store)).put(
                text="a second memory about doors",
                link=f"taxon:{t}",
                rel="specialises",
            )
        # apply_link_ops (the shared put(link=) helper)
        for rel in ("specialises", "generalises"):
            with pytest.raises(BadInput, match="memory"):
                apply_link_ops(store, mem, link=f"taxon:{t}", unlink=None, rel=rel)
        assert store.links_for(mem, direction="out", relation="specialises") == []

    @pytest.mark.parametrize(
        ("kind", "extra"),
        [
            ("gripe", {}),
            ("job", {"job_type": "fix_gripe"}),
            ("message", {"target": "discord/1/2/3"}),
            ("todo", {}),
        ],
    )
    def test_create_time_put_link_door_guarded_per_kind(
        self, store: Any, kind: str, extra: dict[str, Any]
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.gripe import GripeHandler
        from precis.handlers.job import JobHandler
        from precis.handlers.message import MessageHandler
        from precis.handlers.todo import TodoHandler

        handlers = {
            "gripe": GripeHandler,
            "job": JobHandler,
            "message": MessageHandler,
            "todo": TodoHandler,
        }
        t = _mk(store, f"{kind} door taxon")

        def _count() -> int:
            with store.pool.connection() as conn:
                return int(
                    conn.execute(
                        "select count(*) from refs where kind=%s", (kind,)
                    ).fetchone()[0]
                )

        before = _count()
        for rel in ("specialises", "generalises"):
            with pytest.raises(BadInput, match=kind):
                handlers[kind](hub=Hub(store=store)).put(
                    text=f"a {kind} over a taxon",
                    link=f"taxon:{t}",
                    rel=rel,
                    **extra,
                )
        assert _count() == before

    def test_chunk_level_target_refused(self, store: Any) -> None:
        from precis.handlers._link_tag_ops import guard_taxon_hierarchy
        from precis.handlers._link_target import LinkTarget

        a = _mk(store, "chunky a")
        b = _mk(store, "chunky b")
        with pytest.raises(BadInput, match="whole nodes"):
            guard_taxon_hierarchy(
                store,
                a,
                LinkTarget(ref_id=b, pos=3, kind="taxon", raw="taxon:b~3"),
                "specialises",
            )

    def test_specialises_between_non_taxon_refs_unchanged(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.handlers.memory import MemoryHandler

        m1 = _created_memory(store, "general memory idea")
        m2 = _created_memory(store, "specific memory idea")
        MemoryHandler(hub=Hub(store=store)).link(
            id=m2, target=f"memory:{m1}", rel="specialises"
        )
        links = store.links_for(m2, direction="out", relation="specialises")
        assert [lk.dst_ref_id for lk in links] == [m1]


class TestContract:
    def test_put_under_parent_requires_key_names_start_node(self, store: Any) -> None:
        root = _start(store, "ctr measurand", ["dimension_kind"])
        with pytest.raises(BadInput) as ei:
            _mk(store, "ctr keyless", under=root)
        msg = str(ei.value)
        assert "dimension_kind" in msg
        assert f"tn{root}" in msg and "ctr measurand" in msg
        assert _count_titled(store, "ctr keyless") == 0  # nothing written
        ok = _mk(store, "ctr keyed", under=root, meta={"dimension_kind": "count"})
        assert store.taxon_parents(ok) == [(root, None)]

    def test_link_of_existing_keyless_node_refused(self, store: Any) -> None:
        root = _start(store, "lnk measurand", ["dimension_kind"])
        loose = _mk(store, "lnk loose")
        with pytest.raises(BadInput, match="dimension_kind") as ei:
            _link(store, loose, root)
        assert f"tn{root}" in str(ei.value)
        assert store.taxon_parents(loose) == []
        keyed = _mk(store, "lnk keyed", meta={"dimension_kind": "scale"})
        _link(store, keyed, root)
        assert store.taxon_parents(keyed) == [(root, None)]

    def test_contract_inherited_through_intermediate(self, store: Any) -> None:
        root = _start(store, "inh measurand", ["dimension_kind"])
        mid = _mk(store, "inh mid", under=root, meta={"dimension_kind": "count"})
        with pytest.raises(BadInput) as ei:
            _mk(store, "inh grandchild", under=mid)
        assert "dimension_kind" in str(ei.value) and "inh measurand" in str(ei.value)
        assert _count_titled(store, "inh grandchild") == 0
        _mk(store, "inh grandchild", under=mid, meta={"dimension_kind": "count"})

    def test_generalises_form_checks_the_same_contract(self, store: Any) -> None:
        root = _start(store, "gen measurand", ["dimension_kind"])
        loose = _mk(store, "gen loose")
        # root --generalises--> loose  ==  loose specialises root
        with pytest.raises(BadInput, match="dimension_kind"):
            _link(store, root, loose, rel="generalises")

    def test_new_start_node_generalising_an_existing_child_binds_it(
        self, store: Any
    ) -> None:
        loose = _mk(store, "newstart loose")
        with pytest.raises(BadInput, match="dimension_kind"):
            _mk(
                store,
                "newstart root",
                meta={"start": True, "contract": {"required_keys": ["dimension_kind"]}},
                under=loose,
                rel="generalises",
            )
        assert _count_titled(store, "newstart root") == 0
        _mk(
            store,
            "newstart root",
            meta={"start": True, "contract": {"required_keys": []}},
            under=loose,
            rel="generalises",
        )


class TestPathAndIntegrity:
    def test_two_parents_two_axes_two_chains_with_ledes(self, store: Any) -> None:
        root = _start(store, "pth root", [])
        a = _mk(
            store, "pth alpha", under=root, definition="Alpha lede. Alpha extra detail"
        )
        b = _mk(store, "pth beta", under=root, definition="Beta lede. Beta extra")
        c = _mk(store, "pth child", definition="Child lede. Child extra")
        _link(store, c, a, axis="composition")
        _link(store, c, b, axis="method")
        h = _handler(store)
        body = h.get(id=c, view="path").body
        assert body.count("chain ") == 2
        assert body.count(f"tn{root} pth root — ") == 2
        assert "↳ [composition]" in body and "↳ [method]" in body
        for lede in ("Alpha lede.", "Beta lede.", "Child lede."):
            assert lede in body
        assert "extra" not in body  # the lede is the first sentence only
        # chains are start-node first, child last
        first = body.split("chain 1")[1].split("chain 2")[0]
        assert first.index(f"tn{root}") < first.index(f"tn{c}")
        # default get: direct parents with their axis; parent shows a count
        node = h.get(id=c).body
        assert (
            f"specialises: tn{a} pth alpha [composition], tn{b} pth beta [method]"
            in node
        )
        assert "children: 2" in h.get(id=root).body

    def test_start_node_path_is_itself_and_no_start_is_said(self, store: Any) -> None:
        root = _start(store, "one root")
        h = _handler(store)
        assert f"tn{root} one root" in h.get(id=root, view="path").body
        top = _mk(store, "dangling top")
        low = _mk(store, "dangling low", under=top)
        body = h.get(id=low, view="path").body
        assert "reaches no start node" in body
        assert "partial chains" in body and f"tn{top} dangling top" in body

    def test_chain_cut_is_reported(self, store: Any) -> None:
        root = _start(store, "cut root")
        a = _mk(store, "cut a", under=root)
        b = _mk(store, "cut b", under=root)
        c = _mk(store, "cut c", under=a)
        _link(store, c, b)
        h = _handler(store)
        ref = store.get_ref(kind="taxon", id=c)
        assert "1 more chain(s) cut" in h._render_path_view(ref, limit=1).body

    def test_unknown_view_lists_path(self, store: Any) -> None:
        from precis.errors import Unsupported

        a = _mk(store, "view probe")
        with pytest.raises(Unsupported) as ei:
            _handler(store).get(id=a, view="bogus")
        assert "path" in (ei.value.options or [])

    def test_unrooted_lists_orphans_not_rooted(self, store: Any) -> None:
        root = _start(store, "unr root")
        rooted = _mk(store, "unr rooted", under=root)
        orphan = _mk(store, "unr orphan")
        body = _handler(store).get(id="/unrooted").body
        assert f"tn{orphan} unr orphan" in body
        assert f"tn{rooted} " not in body and f"tn{root} " not in body


class TestTaxonOps:
    def _tree(self, store: Any) -> dict[str, int]:
        root = _start(store, "ops root")
        a = _mk(store, "ops a", under=root)
        _link(store, a, root, axis="composition")  # re-link sets the axis
        b = _mk(store, "ops b", under=a)
        _link(store, b, a, axis="composition")
        c = _mk(store, "ops c", under=a)
        _link(store, c, a, axis="method")
        d = _mk(store, "ops d")
        # inverse stored form: root --generalises--> d  (d specialises root)
        store.add_link(src_ref_id=root, dst_ref_id=d, relation="generalises")
        return {"root": root, "a": a, "b": b, "c": c, "d": d}

    def test_descendants_axis_depth_both_forms(self, store: Any) -> None:
        t = self._tree(store)
        r = t["root"]
        assert _ids(store.taxon_descendants(r)) == {t["a"], t["b"], t["c"], t["d"]}
        # axis restricts EVERY hop: a and b are reached by composition hops
        # only; c needs a method hop -> excluded; d has no axis -> excluded.
        assert _ids(store.taxon_descendants(r, axis="composition")) == {t["a"], t["b"]}
        assert _ids(store.taxon_descendants(r, max_depth=1)) == {t["a"], t["d"]}
        got = store.taxon_descendants(r, max_depth=2)
        assert _ids(got) == {t["a"], t["b"], t["c"], t["d"]}
        assert max(row[1] for row in got) == 2
        assert _ids(store.taxon_descendants(r, axis="composition", max_depth=1)) == {
            t["a"]
        }
        assert store.taxon_descendants(r, max_depth=0) == []

    def test_soft_deleted_node_excluded(self, store: Any) -> None:
        t = self._tree(store)
        store.retire_ref(t["a"])
        assert _ids(store.taxon_descendants(t["root"])) == {t["d"]}
        assert store.taxon_parents(t["b"]) == []
        assert t["b"] in store.taxon_unrooted()

    def test_ancestors_paths_start_nodes_would_cycle(self, store: Any) -> None:
        t = self._tree(store)
        assert set(store.taxon_ancestors(t["b"])) == {
            (t["a"], 1, "composition"),
            (t["root"], 2, "composition"),
        }
        assert store.taxon_start_nodes_reached(t["b"]) == [t["root"]]
        assert store.taxon_start_nodes_reached(t["root"]) == [t["root"]]
        assert store.taxon_paths(t["b"]) == [
            [(t["b"], "composition"), (t["a"], "composition"), (t["root"], None)]
        ]
        assert store.taxon_paths(t["root"]) == [[(t["root"], None)]]
        assert store.taxon_would_cycle(t["root"], t["b"])
        assert store.taxon_would_cycle(t["a"], t["a"])
        assert not store.taxon_would_cycle(t["b"], t["root"])

    def test_corrupted_cycle_does_not_hang(self, store: Any) -> None:
        x = _mk(store, "loop x")
        y = _mk(store, "loop y")
        store.add_link(src_ref_id=x, dst_ref_id=y, relation="specialises")
        store.add_link(src_ref_id=y, dst_ref_id=x, relation="specialises")
        # each walk stops at a node already on its own path: terminates, and
        # x is not reported as its own ancestor.
        assert _ids(store.taxon_ancestors(x)) == {y}
        assert _ids(store.taxon_descendants(x)) == {y}
        assert store.taxon_paths(x) == []
        assert {x, y} <= set(store.taxon_unrooted())
        assert store.taxon_would_cycle(x, y)


class TestLinkMeta:
    def test_axis_round_trips_onto_the_edge(self, store: Any) -> None:
        p = _mk(store, "meta parent")
        c = _mk(store, "meta child")
        _link(store, c, p, axis="composition")
        (lk,) = store.links_for(c, direction="out", relation="specialises")
        assert lk.meta["axis"] == "composition"
        _handler(store).link(
            id=c,
            target=f"taxon:{p}",
            rel="specialises",
            meta={"axis": "method", "note": "free"},
        )
        (lk,) = store.links_for(c, direction="out", relation="specialises")
        assert lk.meta == {"axis": "method", "note": "free"}

    @pytest.mark.parametrize("bad", ["", "  ", 3])
    def test_bad_axis_refused(self, store: Any, bad: Any) -> None:
        p = _mk(store, "badaxis parent")
        c = _mk(store, "badaxis child")
        with pytest.raises(BadInput, match="axis"):
            _handler(store).link(
                id=c, target=f"taxon:{p}", rel="specialises", meta={"axis": bad}
            )
        assert store.taxon_parents(c) == []

    def test_meta_on_other_kind_is_refused_not_dropped(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        m1 = _created_memory(store, "meta probe one")
        m2 = _created_memory(store, "meta probe two")
        out = _verb_text(
            core.link(
                kind="memory",
                id=m1,
                target=f"memory:{m2}",
                rel="related-to",
                meta={"axis": "composition"},
            )
        )
        assert "error" in out.lower() and "meta" in out
        assert store.links_for(m1, direction="out", relation="related-to") == []

    def test_link_verb_signature_declares_meta(self) -> None:
        import inspect

        from precis.tools import core

        assert "meta" in inspect.signature(core.link).parameters

    def test_meta_flows_through_the_verb_dispatch(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        p = _mk(store, "verb parent")
        c = _mk(store, "verb child")
        out = _verb_text(
            core.link(
                kind="taxon",
                id=c,
                target=f"taxon:{p}",
                rel="specialises",
                meta={"axis": "regime"},
            )
        )
        assert "error" not in out.lower(), out
        assert store.taxon_parents(c) == [(p, "regime")]

    def test_put_meta_flows_through_the_verb_dispatch(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        """``put(kind='taxon', meta={...}, link=, rel=)`` through the MCP verb
        layer: ``meta=`` must pass the dispatch strictness gate (it was
        refused as an unaccepted kwarg while read only from ``**_kw``)."""
        from precis.tools import core

        root = _start(store, "put-meta measurand", ["dimension_kind"])
        meta = {
            "dimension_kind": "si",
            "si_vector": "0,0,-1,0,0,0,0",
            "canonical_unit": "1/s",
        }
        out = _verb_text(
            core.put(
                kind="taxon",
                text="turnover frequency — catalytic cycles per unit time",
                meta=meta,
                link=f"taxon:{root}",
                rel="specialises",
            )
        )
        assert "error" not in out.lower(), out
        m = re.search(r"\btn(\d+)\b", out)
        assert m is not None, out
        ref = store.get_ref(kind="taxon", id=int(m.group(1)))
        assert ref is not None
        for k, v in meta.items():
            assert ref.meta[k] == v
        assert store.taxon_parents(ref.id) == [(root, None)]


# ── stage C: search facets, lexical name+definition, dedup, path ids ────


def _names(body: str) -> list[str]:
    """Node names from a listing, in order (``tn<id> <name> — ...`` rows)."""
    out = []
    for line in body.splitlines():
        m = re.match(r"tn\d+ (.+?)(?: — .*)?  \[depth \d+\]$", line)
        if m:
            out.append(m.group(1))
    return out


class TestSearchFacets:
    def _tree(self, store: Any) -> dict[str, int]:
        r = _mk(store, "facet root")
        a = _mk(store, "facet alpha")
        b = _mk(store, "facet bravo")
        a1 = _mk(store, "facet alpha child", definition="zyxomorph. extra")
        a2 = _mk(store, "facet alpha grandchild")
        out = _mk(store, "facet outsider", definition="zyxomorph. extra")
        _link(store, a, r, axis="method")
        _link(store, b, r, axis="composition")
        _link(store, a1, a, axis="method")
        _link(store, a2, a1, axis="method")
        return {"r": r, "a": a, "b": b, "a1": a1, "a2": a2, "out": out}

    def test_under_lists_descendants_excluding_under(self, store: Any) -> None:
        t = self._tree(store)
        body = _handler(store).search(under=f"taxon:{t['r']}").body
        # depth, then name; the root and the outsider are absent
        assert _names(body) == [
            "facet alpha",
            "facet bravo",
            "facet alpha child",
            "facet alpha grandchild",
        ]

    def test_axis_and_depth_narrow(self, store: Any) -> None:
        t = self._tree(store)
        h = _handler(store)
        u = f"tn{t['r']}"
        assert _names(h.search(under=u, axis="method").body) == [
            "facet alpha",
            "facet alpha child",
            "facet alpha grandchild",
        ]
        assert _names(h.search(under=u, depth=1).body) == ["facet alpha", "facet bravo"]
        assert _names(h.search(under=u, axis="method", depth=2).body) == [
            "facet alpha",
            "facet alpha child",
        ]
        assert "no taxa" in h.search(under=u, axis="nosuchaxis").body

    def test_q_intersects_with_the_under_set(self, store: Any) -> None:
        t = self._tree(store)
        h = _handler(store)
        # "zyxomorph" is in both a descendant's and the outsider's definition.
        assert "facet outsider" in h.search(q="zyxomorph").body
        body = h.search(q="zyxomorph", under=f"taxon:{t['r']}").body
        assert "facet alpha child" in body
        assert "facet outsider" not in body

    def test_axis_depth_need_under_and_depth_validated(self, store: Any) -> None:
        h = _handler(store)
        with pytest.raises(BadInput, match="under="):
            h.search(q="x", axis="method")
        with pytest.raises(BadInput, match="under="):
            h.search(q="x", depth=2)
        r = _mk(store, "facetval root")
        for bad in (0, -1, True, "2"):
            with pytest.raises(BadInput, match="depth"):
                h.search(under=r, depth=bad)

    def test_verb_signature_and_dispatch(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        import inspect

        from precis.tools import core

        params = inspect.signature(core.search).parameters
        assert {"under", "axis", "depth"} <= set(params)
        t = self._tree(store)
        out = _verb_text(
            core.search(kind="taxon", under=f"taxon:{t['r']}", axis="method", depth=2)
        )
        assert "error" not in out.lower(), out
        assert _names(out) == ["facet alpha", "facet alpha child"]

    def test_axis_depth_without_under_refused_on_other_kinds(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        _created_memory(store, "facet refusal probe")
        for kw in ({"axis": "method"}, {"depth": 2}):
            out = _verb_text(core.search(kind="memory", q="facet", **kw))
            name = next(iter(kw))
            assert "error" in out.lower() and name in out, (kw, out)


class TestLexicalNameAndDefinition:
    def test_definition_only_and_name_only_words_via_search(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        h = _handler(store)
        rid = _created_id(
            h.put(text="blorptastic ratio — frequency of wibblexified catalytic events")
        )
        # Test DB has no embeddings: this is the lexical leg alone.
        with store.pool.connection() as conn:
            n_emb = conn.execute(
                "select count(*) from chunk_embeddings e join chunks c "
                "on c.chunk_id=e.chunk_id where c.ref_id=%s",
                (rid,),
            ).fetchone()[0]
        assert n_emb == 0
        for word in ("blorptastic", "wibblexified"):  # name-only, definition-only
            assert f"tn{rid}" in h.search(q=word, mode="lexical").body, word
            assert f"tn{rid}" in h.search(q=word).body, word
            out = _verb_text(core.search(kind="taxon", q=word))
            assert f"tn{rid}" in out, (word, out)

    def test_exact_name_leads_page_one(self, store: Any) -> None:
        # gr460338: prod ranked 'Tensile yield strength' above 'Yield'
        h = _handler(store)
        root = _mk(store, "exq root")
        long_ = _mk(store, "exq quuxle strength", under=root)
        exact = _mk(store, "exq quuxle", under=root)
        body = h.search(q="exq quuxle").body
        assert body.splitlines()[0].startswith(f"exact: tn{exact} ")
        assert f"tn{long_}" not in body.splitlines()[0]
        faceted = h.search(q="exq quuxle", under=f"taxon:{root}").body
        assert f"exact: tn{exact} " in faceted
        assert "exact:" not in h.search(q="exq quuxle", page=2).body
        assert "exact:" not in h.search(q="exq quux").body
        # a match outside under= is not promoted into the facet
        other = _mk(store, "exq other root")
        assert "exact:" not in h.search(q="exq quuxle", under=f"taxon:{other}").body

    def test_card_scan_is_taxon_only(self) -> None:
        from precis.handlers.concept import ConceptHandler
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.taxon import TaxonHandler

        assert TaxonHandler.search_card_kinds == ("card_combined",)
        assert ConceptHandler.search_card_kinds is None
        assert MemoryHandler.search_card_kinds is None


class TestDedup:
    SI_T = {"dimension_kind": "si", "si_vector": "0,0,0,0,1,0,0"}
    SI_L = {"dimension_kind": "si", "si_vector": "1,0,0,0,0,0,0"}

    def test_same_name_same_dimension_refused_naming_node(self, store: Any) -> None:
        h = _handler(store)
        first = _created_id(h.put(text="dedupa temperature — hotness", meta=self.SI_T))
        with pytest.raises(BadInput) as ei:
            h.put(text="Dedupa  Temperature — a different wording", meta=self.SI_T)
        msg = str(ei.value)
        assert f"tn{first}" in msg and "dedupa temperature" in msg
        assert "dedup=False" in (ei.value.next or "")

    def test_dedup_false_mints(self, store: Any) -> None:
        h = _handler(store)
        _created_id(h.put(text="dedupb temperature — hotness", meta=self.SI_T))
        second = _created_id(
            h.put(text="dedupb temperature — again", meta=self.SI_T, dedup=False)
        )
        assert store.get_ref(kind="taxon", id=second) is not None

    def test_different_si_vector_mints_without_bypass(self, store: Any) -> None:
        h = _handler(store)
        _created_id(h.put(text="dedupc length — extent", meta=self.SI_L))
        other = _created_id(h.put(text="dedupc length — time", meta=self.SI_T))
        assert store.get_ref(kind="taxon", id=other) is not None

    def test_missing_dimension_side_is_compatible(self, store: Any) -> None:
        h = _handler(store)
        _created_id(
            h.put(text="dedupd count — a tally", meta={"dimension_kind": "count"})
        )
        _created_id(
            h.put(text="dedupd count — money", meta={"dimension_kind": "currency"})
        )
        with pytest.raises(BadInput, match="dedupd count"):
            h.put(text="dedupd count — no dimension given")

    def test_alias_and_slug_matches(self, store: Any) -> None:
        h = _handler(store)
        first = _created_id(
            h.put(text="dedupe turnover — rate", meta={"aliases": ["TOFE"]})
        )
        with pytest.raises(BadInput, match=f"tn{first}"):
            h.put(text="tofe — an alias spelled as a name")
        with pytest.raises(BadInput, match=f"tn{first}"):
            h.put(text="dedupe-turnover — the slug collides")

    def test_embedder_down_still_refuses_lexical_duplicate(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.handlers.taxon import TaxonHandler

        class Boom:
            def embed_one(self, _q: str) -> list[float]:
                raise RuntimeError("embedder down")

        h = TaxonHandler(hub=Hub(store=store, embedder=cast(Any, Boom())))
        first = _created_id(h.put(text="dedupf entropy — disorder"))
        with pytest.raises(BadInput, match=f"tn{first}"):
            h.put(text="dedupf entropy — again")
        # A fresh name still mints: the dead embedder is not a 500.
        _created_id(h.put(text="dedupf enthalpy — heat content"))

    def test_embedding_neighbour_offered_unless_dimension_clashes(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.dispatch import Hub
        from precis.embedder import MockEmbedder
        from precis.handlers.taxon import TaxonHandler

        emb = MockEmbedder(dim=store.embedding_dim())
        h = TaxonHandler(hub=Hub(store=store, embedder=emb))
        near = _created_id(
            h.put(text="dedupg heat flow — energy per time", meta=self.SI_T)
        )
        near_ref = store.get_ref(kind="taxon", id=near)
        calls: list[dict[str, Any]] = []

        def fake_semantic(**kw: Any) -> list[Any]:
            calls.append(kw)
            return [(None, near_ref, 0.1)]

        monkeypatch.setattr(store.chunks, "search_chunks_semantic", fake_semantic)
        with pytest.raises(BadInput, match=f"tn{near}"):
            h.put(text="dedupg thermal power — a different name", meta=self.SI_T)
        assert calls and calls[0]["kind"] == "taxon"
        assert calls[0]["max_distance"] == 0.25
        # same neighbour, explicitly different dimension: never offered
        minted = _created_id(
            h.put(text="dedupg thermal power — a different name", meta=self.SI_L)
        )
        assert store.get_ref(kind="taxon", id=minted) is not None

    def test_dimension_clash_rule(self) -> None:
        from precis.handlers.taxon import _dimension_clash

        si = {"dimension_kind": "si", "si_vector": "1,0,0,0,0,0,0"}
        assert not _dimension_clash({}, si)
        assert not _dimension_clash(si, {})
        assert not _dimension_clash(si, dict(si))
        assert _dimension_clash(si, {**si, "si_vector": "0,1,0,0,0,0,0"})
        assert _dimension_clash(si, {"dimension_kind": "count"})


class TestSiblingRefusal:
    """Slice 1 of taxon-facet-navigation: nearest-sibling refusal at mint."""

    PARA = (
        "an instrument that senses surface forces with a sharp tip on a flexible beam"
    )

    def _parent(self, store: Any) -> tuple[int, int]:
        root = _mk(store, "sibref technique")
        afm = _mk(
            store,
            "sibref probe microscope",
            definition=self.PARA,
            under=root,
        )
        return root, afm

    def test_paraphrase_under_same_parent_refused_naming_sibling(
        self, store: Any
    ) -> None:
        root, afm = self._parent(store)
        with pytest.raises(BadInput) as ei:
            _handler(store).put(
                text="sibref scanning tip device — an instrument that senses "
                "surface forces with a sharp tip on a flexible beam",
                link=f"taxon:{root}",
                rel="specialises",
            )
        msg = str(ei.value)
        assert f"tn{afm}" in msg and "sibref-technique/sibref-probe-microscope" in msg
        assert "dedup=False" in (ei.value.next or "")

    def test_unrelated_sibling_mints_and_dedup_false_overrides(
        self, store: Any
    ) -> None:
        root, _afm = self._parent(store)
        h = _handler(store)
        ok = _created_id(
            h.put(
                text="sibref electron beam lithography — writes patterns by "
                "scanning a focused electron beam over resist",
                link=f"taxon:{root}",
                rel="specialises",
            )
        )
        assert store.get_ref(kind="taxon", id=ok) is not None
        forced = _created_id(
            h.put(
                text="sibref scanning tip device — an instrument that senses "
                "surface forces with a sharp tip on a flexible beam",
                link=f"taxon:{root}",
                rel="specialises",
                dedup=False,
            )
        )
        assert store.get_ref(kind="taxon", id=forced) is not None

    def test_other_parent_is_not_compared(self, store: Any) -> None:
        _root, _afm = self._parent(store)
        elsewhere = _mk(store, "sibref elsewhere")
        _created_id(
            _handler(store).put(
                text="sibref scanning tip device — an instrument that senses "
                "surface forces with a sharp tip on a flexible beam",
                link=f"taxon:{elsewhere}",
                rel="specialises",
            )
        )

    def test_vector_leg_refuses_with_distance_and_scopes_to_children(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.dispatch import Hub
        from precis.embedder import MockEmbedder
        from precis.handlers import taxon as taxon_mod

        root, afm = self._parent(store)
        h = taxon_mod.TaxonHandler(
            hub=Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
        )
        afm_ref = store.get_ref(kind="taxon", id=afm)
        calls: list[dict[str, Any]] = []

        def fake_semantic(**kw: Any) -> list[Any]:
            calls.append(kw)
            # only the sibling leg (scoped by include_ref_ids) finds it
            return [(None, afm_ref, 0.1)] if "include_ref_ids" in kw else []

        monkeypatch.setattr(store.chunks, "search_chunks_semantic", fake_semantic)
        with pytest.raises(
            BadInput,
            match=r"reads like an existing sibling[\s\S]*tn\d+[\s\S]*embedding similarity, cosine distance 0.10",
        ):
            h.put(
                text="sibref unrelated words — nothing alike here whatsoever",
                link=f"taxon:{root}",
                rel="specialises",
            )
        scoped = [c for c in calls if "include_ref_ids" in c]  # not the dedup call
        assert scoped and scoped[0]["include_ref_ids"] == [afm]
        assert scoped[0]["max_distance"] == taxon_mod.SIBLING_MAX_DISTANCE

    def test_lexical_overlap_helper(self) -> None:
        from precis.handlers.taxon import lexical_overlap

        assert lexical_overlap("alpha beta gamma", "alpha beta gamma") == 1.0
        assert lexical_overlap("alpha beta gamma", "delta epsilon zeta") == 0.0
        assert lexical_overlap("", "alpha") == 0.0


class TestTaxonEdit:
    """Slice 1b: sharpen an existing node's descriptive keys."""

    def test_edit_updates_meta_and_recards(self, store: Any) -> None:
        h = _handler(store)
        n = _mk(store, "edit target", definition="old words here")
        resp = h.edit(
            id=f"tn{n}",
            meta={
                "definition": "genus plus differentia naming the sibling",
                "includes": ["a", "b"],
                "excludes": ["c (see tn1)"],
                "aliases": ["ET"],
            },
        )
        assert f"tn{n}" in resp.body
        ref = store.get_ref(kind="taxon", id=n)
        assert ref.meta["definition"].startswith("genus plus")
        assert ref.meta["excludes"] == ["c (see tn1)"]
        assert ref.meta["name"] == "edit target"
        with store.pool.connection() as conn:
            rows = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (n,)
            ).fetchall()
        assert len(rows) == 1
        assert "genus plus" in rows[0][0] and "aka ET" in rows[0][0]
        assert "- c (see tn1)" in h.get(id=n).body

    def test_edit_alias_onto_another_nodes_name_is_refused(self, store: Any) -> None:
        h = _handler(store)
        _mk(store, "alias owner")
        n = _mk(store, "alias taker")
        with pytest.raises(BadInput, match="alias owner"):
            h.edit(id=n, meta={"aliases": ["alias owner"]})

    def test_edit_refuses_non_descriptive_keys_and_bad_values(self, store: Any) -> None:
        h = _handler(store)
        n = _mk(store, "edit refused")
        with pytest.raises(BadInput, match="status"):
            h.edit(id=n, meta={"status": "systematic"})
        with pytest.raises(BadInput, match="includes"):
            h.edit(id=n, meta={"includes": "not a list"})
        with pytest.raises(BadInput, match="meta"):
            h.edit(id=n)
        with pytest.raises(BadInput, match="meta="):
            h.edit(id=n, text="x")

    def test_edit_through_the_verb(self, store: Any, mounted_runtime: Any) -> None:
        from precis.tools import core

        n = _mk(store, "edit verb node")
        out = _verb_text(core.edit(kind="taxon", id=f"tn{n}", meta={"includes": ["z"]}))
        assert "error" not in out.lower(), out
        assert store.get_ref(kind="taxon", id=n).meta["includes"] == ["z"]


class TestPathIds:
    def test_path_resolves_get_under_and_link(self, store: Any) -> None:
        h = _handler(store)
        meas = _mk(store, "pathx measurand")
        temp = _mk(store, "pathx temperature")
        flow = _mk(store, "pathx flowrate")
        _link(store, temp, meas)
        body = h.get(id="pathx-measurand/pathx-temperature").body
        assert body.startswith(f"# taxon {temp}:")
        assert h.get(id="pathx measurand/pathx temperature").body == body  # names
        assert h.get(id="pathx-temperature").body == body  # single segment
        assert h.get(id=f"tn{temp}").body == body  # handle still works
        assert h.get(id=str(temp)).body == body  # numeric string
        assert h.get(id=temp).body == body
        assert _names(h.search(under="pathx-measurand").body) == ["pathx temperature"]
        h.link(id=flow, target="taxon:pathx-measurand", rel="specialises")
        assert (meas, None) in store.taxon_parents(flow)

    def test_ambiguous_path_refused_with_candidates(self, store: Any) -> None:
        h = _handler(store)
        a = _mk(store, "pathy root a")
        b = _mk(store, "pathy root b")
        t1 = _mk(store, "pathy shared")
        # same slug, different parent: only mintable past the dedup refusal
        t2 = _created_id(h.put(text="pathy shared — a thing", dedup=False))
        _link(store, t1, a)
        _link(store, t2, b)
        with pytest.raises(BadInput, match="ambiguous") as ei:
            h.get(id="pathy-shared")
        assert f"tn{t1}" in str(ei.value) and f"tn{t2}" in str(ei.value)
        assert "pathy-root-a/pathy-shared" in str(ei.value)
        assert "longer path" in (ei.value.next or "")
        # a longer path disambiguates
        assert h.get(id="pathy-root-b/pathy-shared").body.startswith(f"# taxon {t2}:")

    def test_same_path_siblings_listed_by_definition(self, store: Any) -> None:
        # prod: two seeded "Bore diameter" nodes under measurand share a path
        h = _handler(store)
        root = _mk(store, "pathq root")
        t1 = _created_id(h.put(text="pathq twin — inner-ring bore of a bearing"))
        t2 = _created_id(h.put(text="pathq twin — bore of a hose fitting", dedup=False))
        _link(store, t1, root)
        _link(store, t2, root)
        with pytest.raises(BadInput, match="ambiguous") as ei:
            h.get(id="pathq-root/pathq-twin")
        assert "inner-ring bore of a bearing" in str(ei.value)
        assert "bore of a hose fitting" in str(ei.value)
        assert "same path" in (ei.value.next or "")
        assert "longer path" not in (ei.value.next or "")

    def test_unmatched_path_not_found_with_near_candidates(self, store: Any) -> None:
        h = _handler(store)
        a = _mk(store, "pathz root")
        t = _mk(store, "pathz leaf")
        _link(store, t, a)
        with pytest.raises(NotFound, match=f"tn{t}") as ei:
            h.get(id="wrongparent/pathz-leaf")
        assert "pathz-root/pathz-leaf" in str(ei.value)
        with pytest.raises(NotFound):
            h.get(id="pathz-nonexistent-term")

    def test_leading_slash_is_a_list_view_and_bad_paths_refused(
        self, store: Any
    ) -> None:
        h = _handler(store)
        assert h.get(id="/unrooted").body  # still the list view, not a path
        with pytest.raises(BadInput, match="empty segment"):
            h.get(id="a//b")
        with pytest.raises(BadInput, match="empty segment"):
            h.get(id="a/")

    def test_path_through_the_get_verb(self, store: Any, mounted_runtime: Any) -> None:
        from precis.tools import core

        meas = _mk(store, "pathv measurand")
        temp = _mk(store, "pathv temperature")
        _link(store, temp, meas)
        out = _verb_text(core.get(kind="taxon", id="pathv-measurand/pathv-temperature"))
        assert f"# taxon {temp}:" in out, out


# ── stage D: the seed (migration 0174) ──────────────────────────────

_SEED_TABLES = (
    "material_properties",
    "component_specs",
    "rxn_properties",
    "component_categories",
)
_AWKWARD = "  ZZseed Thermal Cond. (W/m·K)\t Ünïcode  Å  "


def _run_seed_sql() -> None:
    """Execute the whole 0174 file against the test DB (as the migrator does:
    autocommit connection, the file brings its own BEGIN/COMMIT)."""
    import psycopg

    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / "0174_taxon_seed.sql").read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


@pytest.fixture
def seeded(store: Any) -> Any:
    """Legacy rows with awkward names and every dimension case, then the seed.

    The registries are preserved across tests (they carry the migrations' own
    rows), so the extras are added here and removed in teardown."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO component_categories (category_id, name, status, description) "
            "VALUES ('zz_cat', 'ZZseed Category', 'core', 'A category for the seed test.'),"
            "       ('zz_cat_nodesc', 'ZZseed Bare', 'proposed', NULL)"
        )
        conn.execute(
            "INSERT INTO material_properties "
            "(prop_id, name, canonical_unit, dimension, value_type, status, description)"
            " VALUES "
            "('zz_awkward', %s, 'W/m/K', 'power/(length*temperature)', 'quantity',"
            "   'core', 'Zzseedword conduction of heat. Second sentence.'),"
            "('zz_unmapped', 'ZZseed Unmapped', 'foo', 'zz mystery label', 'quantity',"
            "   'proposed', 'A quantity whose label is not in the lookup.'),"
            "('zz_nodimlabel', 'ZZseed No Label', 'm', NULL, 'quantity', 'proposed', NULL),"
            "('zz_null_cat', 'ZZseed Null Cat', NULL, NULL, 'categorical', 'proposed', 'x.'),"
            "('zz_null_bool', 'ZZseed Null Bool', NULL, NULL, 'boolean', 'proposed', 'y.'),"
            "('zz_null_ratio', 'ZZseed Null Ratio', NULL, 'dimensionless', 'ratio',"
            "   'proposed', 'z.')",
            (_AWKWARD,),
        )
        conn.execute(
            "INSERT INTO component_specs "
            "(spec_id, name, canonical_unit, dimension, value_type, status,"
            " description, category_id) VALUES "
            "('zz_spec_scoped', 'ZZseed Scoped Length', 'mm', 'length', 'quantity',"
            "   'proposed', 'A length scoped to a category.', 'zz_cat'),"
            "('zz_spec_universal', 'ZZseed Universal Mass', 'g', 'mass', 'quantity',"
            "   'core', 'A universal mass.', NULL)"
        )
        conn.execute(
            "INSERT INTO rxn_properties "
            "(prop_id, name, canonical_unit, dimension, value_type, status, description)"
            " VALUES ('zz_rxn', 'ZZseed Yield', NULL, 'dimensionless', 'ratio',"
            "   'proposed', 'Fraction of product formed.')"
        )
    try:
        _run_seed_sql()
        yield store
    finally:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM rxn_properties WHERE prop_id LIKE 'zz\\_%'")
            conn.execute("DELETE FROM component_specs WHERE spec_id LIKE 'zz\\_%'")
            conn.execute("DELETE FROM material_properties WHERE prop_id LIKE 'zz\\_%'")
            conn.execute(
                "DELETE FROM component_categories WHERE category_id LIKE 'zz\\_%'"
            )


def _seed_rows(store: Any) -> list[Any]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT ref_id, title, meta FROM refs WHERE kind='taxon' "
            "AND meta ? 'legacy_source' ORDER BY ref_id"
        ).fetchall()


def _seed_node(store: Any, table: str, key: str) -> Any:
    for rid, title, meta in _seed_rows(store):
        if meta["legacy_source"] == {"table": table, "key": key}:
            return rid, title, meta
    raise AssertionError(f"no seeded node for {table}/{key}")


def _count(store: Any, sql: str, *params: Any) -> int:
    with store.pool.connection() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _parent_slugs(store: Any, rid: int) -> list[str]:
    return [
        store.get_ref(kind="taxon", id=p).meta["slug"]
        for p, _ax in store.taxon_parents(rid)
    ]


class TestSeed:
    def test_one_node_per_legacy_row_plus_two_start_nodes(self, seeded: Any) -> None:
        store = seeded
        for tbl in _SEED_TABLES:
            n_legacy = _count(store, f"SELECT count(*) FROM {tbl}")
            n_nodes = _count(
                store,
                "SELECT count(*) FROM refs WHERE kind='taxon' "
                "AND meta->'legacy_source'->>'table' = %s",
                tbl,
            )
            assert n_nodes == n_legacy > 0, tbl
        total = sum(_count(store, f"SELECT count(*) FROM {t}") for t in _SEED_TABLES)
        assert (
            _count(store, "SELECT count(*) FROM refs WHERE kind='taxon'") == total + 2
        )
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT meta FROM refs WHERE kind='taxon' AND meta->>'start'='true'"
            ).fetchall()
        starts = {r[0]["slug"]: r[0] for r in rows}
        assert set(starts) == {"measurand", "subject"}
        assert starts["measurand"]["contract"] == {"required_keys": ["dimension_kind"]}
        assert starts["subject"]["contract"] == {"required_keys": []}
        for m in starts.values():
            assert m["status"] == "proposed"
            assert validate_taxon_meta(m) == m
            assert m["definition"].endswith(".") and m["definition"].count(". ") == 0

    def test_every_node_reaches_a_start_and_is_proposed(self, seeded: Any) -> None:
        store = seeded
        assert store.taxon_unrooted() == []
        body = _handler(store).get(id="/unrooted").body
        assert "every taxon reaches a start node" in body
        statuses = {m["status"] for _r, _t, m in _seed_rows(store)}
        assert statuses == {"proposed"}  # core never carries over
        rid, _, _ = _seed_node(store, "component_categories", "zz_cat")
        assert _parent_slugs(store, rid) == ["subject"]
        rid, _, _ = _seed_node(store, "material_properties", "zz_awkward")
        assert _parent_slugs(store, rid) == ["measurand"]

    def test_null_unit_rows_are_categorical_or_dimensionless(self, seeded: Any) -> None:
        store = seeded
        for key, want in (
            ("zz_null_cat", "categorical"),
            ("zz_null_bool", "categorical"),
            ("zz_null_ratio", "dimensionless"),
        ):
            _, _, m = _seed_node(store, "material_properties", key)
            assert m["dimension_kind"] == want, key
            assert "si_vector" not in m
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT l.key, r.meta->>'dimension_kind' FROM ("
                " SELECT 'material_properties' t, prop_id key FROM material_properties"
                "  WHERE canonical_unit IS NULL"
                " UNION ALL SELECT 'component_specs', spec_id FROM component_specs"
                "  WHERE canonical_unit IS NULL"
                " UNION ALL SELECT 'rxn_properties', prop_id FROM rxn_properties"
                "  WHERE canonical_unit IS NULL) l"
                " JOIN refs r ON r.kind='taxon' AND r.meta->'legacy_source' ="
                "  jsonb_build_object('table', l.t, 'key', l.key)"
            ).fetchall()
        assert len(rows) >= 4
        assert {k for _key, k in rows} <= {"categorical", "dimensionless"}

    def test_si_nodes_have_seven_slot_vectors(self, seeded: Any) -> None:
        store = seeded
        si = [m for _r, _t, m in _seed_rows(store) if m.get("dimension_kind") == "si"]
        assert si
        for m in si:
            assert re.fullmatch(r"-?\d+(,-?\d+){6}", m["si_vector"]), m
        for _r, _t, m in _seed_rows(store):
            if m.get("dimension_kind") != "si":
                assert "si_vector" not in m
        _, _, m = _seed_node(store, "material_properties", "zz_awkward")
        assert m["si_vector"] == "1,1,-3,0,-1,0,0"  # W/(m K) = kg m s^-3 K^-1
        _, _, m = _seed_node(store, "component_specs", "zz_spec_universal")
        assert m["si_vector"] == "0,1,0,0,0,0,0"

    def test_unmapped_label_is_null_and_listed(self, seeded: Any) -> None:
        store = seeded
        _, _, m = _seed_node(store, "material_properties", "zz_unmapped")
        assert "dimension_kind" not in m and "si_vector" not in m
        _, _, m = _seed_node(store, "material_properties", "zz_nodimlabel")
        assert "dimension_kind" not in m
        body = _handler(store).get(id="/unmapped").body
        assert "ZZseed Unmapped" in body and "ZZseed No Label" in body
        assert "ZZseed Thermal" not in body  # mapped rows are not listed
        assert "ZZseed Category" not in body  # categories carry no dimension
        with store.pool.connection() as conn:
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"dimension_kind": "scale"}\' '
                "WHERE kind='taxon' AND meta->'legacy_source'->>'key' IN "
                "('zz_unmapped', 'zz_nodimlabel')"
            )
        assert "ZZseed Unmapped" not in _handler(store).get(id="/unmapped").body

    def test_specs_point_at_their_category_node_by_meta(self, seeded: Any) -> None:
        store = seeded
        cat_id, _, cat = _seed_node(store, "component_categories", "zz_cat")
        assert "dimension_kind" not in cat
        # no description -> a synthesised one-sentence definition naming the term
        _, _, bare = _seed_node(store, "component_categories", "zz_cat_nodesc")
        assert bare["definition"].startswith("ZZseed Bare: a component category")
        sid, _, scoped = _seed_node(store, "component_specs", "zz_spec_scoped")
        assert scoped["applies_to_ref"] == f"tn{cat_id}"
        assert store.get_ref(kind="taxon", id=cat_id).meta["name"] == "ZZseed Category"
        _, _, universal = _seed_node(store, "component_specs", "zz_spec_universal")
        assert "applies_to_ref" not in universal
        # a meta pointer, not an edge: the spec's only parent is the measurand
        assert _parent_slugs(store, sid) == ["measurand"]

    def test_replay_inserts_nothing_new(self, seeded: Any) -> None:
        store = seeded

        def snapshot() -> tuple[int, int, int]:
            return (
                _count(store, "SELECT count(*) FROM refs WHERE kind='taxon'"),
                _count(
                    store,
                    "SELECT count(*) FROM chunks c JOIN refs r USING (ref_id) "
                    "WHERE r.kind='taxon'",
                ),
                _count(
                    store, "SELECT count(*) FROM links WHERE relation='specialises'"
                ),
            )

        before = snapshot()
        assert before[0] > 2
        _run_seed_sql()
        assert snapshot() == before
        # a legacy row added later is picked up alone
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO rxn_properties (prop_id, name, canonical_unit, dimension,"
                " value_type, status, description) VALUES ('zz_late', 'ZZseed Late',"
                " 's', 'time', 'quantity', 'proposed', 'Added after the first seed.')"
            )
        _run_seed_sql()
        after = snapshot()
        assert after == (before[0] + 1, before[1] + 1, before[2] + 1)
        _run_seed_sql()
        assert snapshot() == after

    def test_sql_slug_norm_name_and_card_match_the_python(self, seeded: Any) -> None:
        from precis.reading.concepts import normalize_name
        from precis.taxonomy.nodes import initial_taxon_meta, taxon_card_text

        store = seeded
        rows = _seed_rows(store)
        assert len(rows) >= 20
        _, title, awk = _seed_node(store, "material_properties", "zz_awkward")
        assert title == _AWKWARD.strip() and awk["name"] == title
        assert awk["norm_name"] == "zzseed thermal cond. (w/m·k) ünïcode å"
        assert awk["slug"] == slugify(_AWKWARD)
        for rid, title, meta in rows:
            name = meta["name"]
            assert title == name == name.strip()
            assert meta["norm_name"] == normalize_name(name), name
            assert meta["slug"] == slugify(name), name
            extra = {
                k: v
                for k, v in meta.items()
                if k not in ("name", "norm_name", "slug", "definition", "aliases")
            }
            # byte-identical to a hand-built node of the same content
            rebuilt = initial_taxon_meta(name, meta["definition"], extra)
            rebuilt["aliases"] = meta["aliases"]
            assert rebuilt == meta, name
            assert validate_taxon_meta(meta) == meta, name
            with store.pool.connection() as conn:
                card = conn.execute(
                    "SELECT text, chunk_kind FROM chunks WHERE ref_id=%s AND ord=-1",
                    (rid,),
                ).fetchall()
            want = taxon_card_text(name, meta["definition"], meta["aliases"])
            assert card == [(want, "card_combined")], name

    def test_cards_are_unembedded_lexical_and_the_only_chunk(self, seeded: Any) -> None:
        store = seeded
        with store.pool.connection() as conn:
            ords = conn.execute(
                "SELECT c.ord, count(*) FROM chunks c JOIN refs r USING (ref_id) "
                "WHERE r.kind='taxon' GROUP BY c.ord"
            ).fetchall()
            n_emb = conn.execute(
                "SELECT count(*) FROM chunk_embeddings e JOIN chunks c USING (chunk_id)"
                " JOIN refs r USING (ref_id) WHERE r.kind='taxon'"
            ).fetchone()[0]
        n_nodes = _count(store, "SELECT count(*) FROM refs WHERE kind='taxon'")
        assert ords == [(-1, n_nodes)]
        assert n_emb == 0
        rid, _, _ = _seed_node(store, "material_properties", "zz_awkward")
        h = _handler(store)
        body = h.search(q="zzseedword", mode="lexical").body  # definition-only word
        assert f"tn{rid}" in body, body
        assert f"tn{rid}" in h.search(q="zzseedword").body


# ── slice 2: search(kind=<any>, under=<taxon>) ──────────────────────────────


def _instance_of(store: Any, ref_id: int, taxon: int) -> None:
    store.add_link(src_ref_id=ref_id, dst_ref_id=taxon, relation="instance-of")


class TestUnderOnEveryKind:
    def _world(self, store: Any) -> dict[str, int]:
        afm = _mk(store, "uo afm")
        tip = _mk(store, "uo afm tip")
        dft = _mk(store, "uo dft")
        other = _mk(store, "uo other")
        _link(store, tip, afm, axis="object")
        m_in = _created_memory(store, "uoquark cantilever resonance shift")
        m_tip = _created_memory(store, "uoquark tip radius effect")
        m_dft = _created_memory(store, "uoquark dft of tip")
        m_out = _created_memory(store, "uoquark unrelated outsider")
        _instance_of(store, m_in, afm)
        _instance_of(store, m_tip, tip)  # only via the descendant
        _instance_of(store, m_dft, tip)
        _instance_of(store, m_dft, dft)
        _instance_of(store, m_out, other)
        return {
            "afm": afm, "tip": tip, "dft": dft, "m_in": m_in,
            "m_tip": m_tip, "m_dft": m_dft, "m_out": m_out,
        }  # fmt: skip

    def test_closure_includes_node_and_descendants_and_q_ranks(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(core.search(kind="memory", q="uoquark", under=f"tn{w['afm']}"))
        assert "error" not in out.lower(), out
        assert f"me{w['m_in']}" in out or str(w["m_in"]) in out
        assert str(w["m_tip"]) in out and str(w["m_dft"]) in out
        assert "unrelated outsider" not in out

    def test_list_under_is_intersection(self, store: Any, mounted_runtime: Any) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(
            core.search(
                kind="memory",
                q="uoquark",
                under=[f"tn{w['afm']}", f"tn{w['dft']}"],
            )
        )
        assert "dft of tip" in out
        assert "cantilever resonance" not in out and "tip radius" not in out

    def test_no_q_lists_newest_first_with_total(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(core.search(kind="memory", under=f"tn{w['afm']}"))
        assert "3 instances, newest first" in out
        # newest first: m_dft was linked/created after m_tip after m_in
        body = [ln for ln in out.splitlines() if ln.startswith("me")]
        assert [int(ln.split()[0][2:]) for ln in body] == [
            w["m_dft"], w["m_tip"], w["m_in"],
        ]  # fmt: skip

    def test_axis_restricts_the_closure_walk(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(
            core.search(
                kind="memory", q="uoquark", under=f"tn{w['afm']}", axis="nosuchaxis"
            )
        )
        assert "tip radius" not in out and "cantilever resonance" in out

    def test_non_taxon_target_walks_part_of_instead(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        # One under=: the target picks the tree. A non-taxon handle walks
        # part-of; only a taxon (or a list of them) selects instance-of
        # members, and axis= is refused off the taxon tree.
        from precis.tools import core

        w = self._world(store)
        for other in (f"me{w['m_in']}", f"memory:{w['m_in']}"):
            out = _verb_text(core.search(kind="memory", q="uoquark", under=other))
            assert "on part-of" in out and "error" not in out.lower(), (other, out)
        out = _verb_text(core.search(kind="memory", q="x", under=w["m_in"]))
        assert "resolves to no live ref" in out and "under= takes a taxon" in out
        out = _verb_text(
            core.search(kind="memory", q="x", under=[f"tn{w['afm']}", f"me{w['m_in']}"])
        )
        assert "not a taxon" in out, out
        out = _verb_text(
            core.search(kind="memory", q="x", under=f"me{w['m_in']}", axis="method")
        )
        assert "axis= applies only when under= is a taxon" in out, out

    def test_unappliable_shapes_refused_on_both_trees(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        w = self._world(store)
        for target in (f"tn{w['afm']}", f"me{w['m_in']}"):
            out = _verb_text(core.search(kind="memory", view="stubs", under=target))
            assert "not supported with view=" in out, (target, out)
        out = _verb_text(
            core.search(kind="memory", under=f"tn{w['afm']}", args={"folder": "x"})
        )
        assert "folder= is not applied to an under= listing" in out, out
        out = _verb_text(core.search(kind="memory,todo", under=f"tn{w['afm']}"))
        assert "kind='todo' has its own under=" in out, out

    def test_taxon_kind_keeps_its_own_under(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(core.search(kind="taxon", under=f"tn{w['afm']}"))
        assert "uo afm tip" in out


# ── slice 3: get(kind='taxon', view='facets') ───────────────────────────────


def _closed_tag(store: Any, ref_id: int, ns: str, value: str) -> None:
    with store.pool.connection() as conn:
        tid = conn.execute(
            "INSERT INTO tags (namespace, value) VALUES (%s, %s) "
            "ON CONFLICT (namespace, value) DO UPDATE SET value = EXCLUDED.value "
            "RETURNING tag_id",
            (ns, value),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (%s, %s, 'system') "
            "ON CONFLICT DO NOTHING",
            (ref_id, tid),
        )
        conn.commit()


class TestFacetsView:
    def _world(self, store: Any) -> dict[str, Any]:
        from tests.workers._helpers import seed_ref

        afm = _mk(store, "fv afm")
        tip = _mk(store, "fv afm tip")
        _link(store, tip, afm, axis="object")
        meth = _mk(store, "fv method")
        dft = _mk(store, "fv dft")
        md = _mk(store, "fv md")
        _link(store, dft, meth, axis="method")
        _link(store, md, meth, axis="method")
        scl = _mk(store, "fv scale")
        nano = _mk(store, "fv nano")
        micro = _mk(store, "fv micro")
        _link(store, nano, scl, axis="scale")
        _link(store, micro, scl, axis="scale")
        papers, findings = [], []
        for i in range(6):
            r = seed_ref(store, title=f"fv paper {i}", kind="paper")
            papers.append(r)
            _instance_of(store, r, afm if i % 2 else tip)
            _instance_of(store, r, dft if i < 3 else md)
            _instance_of(store, r, nano)
        for i in range(2):
            r = seed_ref(store, title=f"fv finding {i}", kind="finding")
            findings.append(r)
            _instance_of(store, r, tip)
            _instance_of(store, r, md)
            _instance_of(store, r, micro)
        for i, r in enumerate(papers):
            if i < 4:
                _closed_tag(store, r, "DOMAIN", "physics" if i < 3 else "materials")
            if i < 5:
                _closed_tag(store, r, "DOMAINCASCADE", "1")
        return {
            "afm": afm, "papers": papers, "findings": findings,
            "dft": dft, "micro": micro,
        }  # fmt: skip

    def _facets(self, store: Any, node: int, **kw: Any) -> str:
        return _handler(store).get(id=f"tn{node}", view="facets", **kw).body

    def test_split_order_totals_and_machine_labelling(self, store: Any) -> None:
        w = self._world(store)
        body = self._facets(store, w["afm"])
        assert "instances: 8" in body and "sort=split" in body
        # method splits 3/5 evenly, scale 6/2 unevenly: method first
        assert body.index("- method") < body.index("- scale")
        assert "fv dft 3" in body and "fv md 5" in body
        # categorizer facet: machine-written, pass version, unclassified row
        assert "machine-written categorizer facets" in body
        assert "domain [pass v1]" in body
        assert "physics 3" in body and "materials 1" in body
        # 6 papers apply: 4 valued, 1 processed without value, 1 never processed
        assert "unclassified 2 (no value 1, not processed 1)" in body
        # footer: other sorts and the narrowing steer
        for s in ("recent", "evidence", "gap", "name"):
            assert f"  {s}:" in body
        assert "search(kind=<any>, under=<value handle>)" in body

    def test_evidence_sort_counts_findings_only(self, store: Any) -> None:
        w = self._world(store)
        body = self._facets(store, w["afm"], sort="evidence")
        assert "instances: 2 of 8" in body
        assert "fv micro 2" in body and "fv nano" not in body

    def test_recent_sort_uses_last_90_days(self, store: Any) -> None:
        w = self._world(store)
        with store.pool.connection() as conn:
            conn.execute(
                "update refs set created_at = now() - interval '200 days' "
                "where ref_id = any(%s)",
                (w["papers"][:4],),
            )
            conn.commit()
        body = self._facets(store, w["afm"], sort="recent")
        assert "instances: 4 of 8" in body

    def test_gap_lists_empty_cell_first(self, store: Any) -> None:
        w = self._world(store)
        body = self._facets(store, w["afm"], sort="gap", cross=["method", "scale"])
        cells = [ln.strip() for ln in body.splitlines() if re.search(r" x .*: ", ln)]
        assert "1 empty" in cells[0]  # the header line
        assert cells[1] == f"tn{w['dft']} fv dft x tn{w['micro']} fv micro: 0"
        with pytest.raises(BadInput, match="cross"):
            self._facets(store, w["afm"], sort="gap")
        with pytest.raises(BadInput, match="nosuch"):
            self._facets(store, w["afm"], sort="gap", cross=["method", "nosuch"])

    def test_bad_sort_and_stray_cross_refused(self, store: Any) -> None:
        w = self._world(store)
        with pytest.raises(BadInput, match="sort"):
            self._facets(store, w["afm"], sort="bogus")
        with pytest.raises(BadInput, match="cross"):
            self._facets(store, w["afm"], cross=["a", "b"])
        with pytest.raises(BadInput, match="view='facets'"):
            _handler(store).get(id=w["afm"], sort="name")

    def test_name_sort_and_empty_node(self, store: Any) -> None:
        w = self._world(store)
        body = self._facets(store, w["afm"], sort="name")
        assert body.index("- method") < body.index("- scale")
        lonely = _mk(store, "fv lonely")
        assert "no instances under this node" in self._facets(store, lonely)

    def test_caps_hidden_axes_values_and_char_budget(self, store: Any) -> None:
        from tests.workers._helpers import seed_ref

        root = _mk(store, "cap root")
        insts = [seed_ref(store, title=f"cap item {i}") for i in range(25)]
        for r in insts:
            _instance_of(store, r, root)
        for a in range(10):  # ten axes, one value each
            top = _mk(store, f"cap axis{a} top")
            v = _mk(store, f"cap axis{a} value")
            _link(store, v, top, axis=f"capaxis{a}")
            for r in insts[: 5 + a]:
                _instance_of(store, r, v)
        big = _mk(store, "cap big top")
        for i in range(20):  # one axis, twenty values, one item each
            v = _mk(store, f"cap big value{i}")
            _link(store, v, big, axis="capaaa")
            _instance_of(store, insts[i], v)
        body = self._facets(store, root)
        assert "instances: 25" in body
        assert re.search(r"\+\d+ axes not shown: ", body)
        assert len(re.findall(r"^- ", body, flags=re.M)) <= 8
        assert len(body) <= 8000  # ~2k tokens
        named = self._facets(store, root, sort="name")  # capaaa sorts first
        assert "+5 values (5 items)" in named and len(named) <= 8000

    def test_through_the_verb_with_args(self, store: Any, mounted_runtime: Any) -> None:
        from precis.tools import core

        w = self._world(store)
        out = _verb_text(
            core.get(
                kind="taxon",
                id=f"tn{w['afm']}",
                view="facets",
                args={"sort": "gap", "cross": ["method", "scale"]},
            )
        )
        assert "error" not in out.lower(), out
        assert "fv dft x" in out and "fv micro: 0" in out


class TestReviewFixes:
    def test_sibling_lexical_leg_gated_on_embedder_down(self, store: Any) -> None:
        from precis.dispatch import Hub
        from precis.embedder import MockEmbedder
        from precis.handlers.taxon import TaxonHandler

        root = _mk(store, "rf root")
        _mk(store, "rf alpha", definition="measures heat flow in a sample", under=root)
        # embedder present, sibling not embedded: no vector hit, must mint
        h = TaxonHandler(
            hub=Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
        )
        ok = _created_id(
            h.put(
                text="rf beta — measures heat flow in a sample holder",
                link=f"taxon:{root}",
                rel="specialises",
            )
        )
        assert store.get_ref(kind="taxon", id=ok) is not None
        # embedder down: near-identical definitions are still refused
        with pytest.raises(BadInput, match="word overlap"):
            _handler(store).put(
                text="rf gamma — measures heat flow in a sample",
                link=f"taxon:{root}",
                rel="specialises",
            )

    def test_edit_refuses_mode_and_unknown_kwargs(self, store: Any) -> None:
        n = _mk(store, "rf edit kw")
        h = _handler(store)
        with pytest.raises(BadInput, match="mode"):
            h.edit(id=n, meta={"includes": ["a"]}, mode="replace")
        with pytest.raises(BadInput, match="bogus"):
            h.edit(id=n, meta={"includes": ["a"]}, bogus=1)

    def test_edit_cannot_make_a_duplicate_sibling(self, store: Any) -> None:
        root = _mk(store, "rf dup root")
        _mk(store, "rf dup a", definition="measures heat flow in a sample", under=root)
        b = _mk(store, "rf dup b", definition="writes patterns with beams", under=root)
        h = _handler(store)
        with pytest.raises(BadInput, match="existing sibling"):
            h.edit(id=b, meta={"definition": "measures heat flow in a sample"})
        h.edit(id=b, meta={"definition": "measures heat flow in a sample"}, dedup=False)
        assert "heat flow" in store.get_ref(kind="taxon", id=b).meta["definition"]
        # editing a node against itself is not a duplicate
        c = _mk(store, "rf dup c", definition="writes patterns with beams")
        h.edit(id=c, meta={"definition": "writes patterns with electron beams"})

    def test_under_listing_applies_or_refuses_narrowing(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        afm = _mk(store, "rf afm")
        m1 = _created_memory(store, "rfq one")
        m2 = _created_memory(store, "rfq two")
        for m in (m1, m2):
            _instance_of(store, m, afm)
        out = _verb_text(core.search(under=f"tn{afm}"))  # kind=None
        assert "2 instances" in out and f"me{m1}" in out
        out = _verb_text(core.search(kind="memory,paper", under=f"tn{afm}"))
        assert "2 instances" in out
        out = _verb_text(
            core.search(kind="memory", under=f"tn{afm}", since="2000-01-01")
        )
        assert "error" in out.lower() and "since" in out and "q=" in out
        out = _verb_text(core.search(kind="memory", under=f"tn{afm}", tag="x"))
        assert "error" in out.lower() and "tag" in out
        out = _verb_text(
            core.search(kind="memory", under=f"tn{afm}", uncited="nodraft")
        )
        assert "error" in out.lower()  # an unresolvable uncited is not dropped

    def test_under_with_uncited_excludes(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.runtime import dispatch as dmod

        afm = _mk(store, "rf unc afm")
        m1 = _created_memory(store, "rfu one")
        m2 = _created_memory(store, "rfu two")
        for m in (m1, m2):
            _instance_of(store, m, afm)
        rt = mounted_runtime
        args = {"kind": "memory", "under": f"tn{afm}", "exclude_ref_ids": [m1]}
        resp = rt._dispatch_inner("search", args)
        assert "1 instance," in resp.body and f"me{m2}" in resp.body
        assert f"me{m1}" not in resp.body
        assert dmod is not None

    def test_under_refuses_kinds_without_include_wiring(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        afm = _mk(store, "rf unsup afm")
        out = _verb_text(core.search(kind="patent", q="x", under=f"tn{afm}"))
        assert "error" in out.lower() and "under=" in out and "cited" not in out

    def test_retired_instances_excluded_from_under_and_facets(
        self, store: Any, mounted_runtime: Any
    ) -> None:
        from precis.tools import core

        afm = _mk(store, "rf ret afm")
        other = _mk(store, "rf ret other")
        live = _created_memory(store, "rfr live")
        dead = _created_memory(store, "rfr dead")
        for m in (live, dead):
            _instance_of(store, m, afm)
            _instance_of(store, m, other)
        with store.pool.connection() as conn:
            conn.execute(
                "update refs set retired_at = now() where ref_id = %s", (dead,)
            )
            conn.commit()
        out = _verb_text(core.search(kind="memory", under=f"tn{afm}"))
        assert "1 instance," in out and f"me{dead}" not in out
        body = _handler(store).get(id=f"tn{afm}", view="facets").body
        assert "instances: 1" in body
