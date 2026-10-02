"""Relation constraints as data (docs/backlog/relation-constraints.md).

Migration 0180 puts ``domain_kinds`` / ``range_kinds`` / ``functional`` /
``transitive`` / ``acyclic`` on ``relations``; one validator
(``check_relation_constraints``) runs at both generic link doors
(``apply_link_ops`` and ``NumericRefHandler.link``). Pins: the seeded rows
(AC1), the ``contradicts`` rule through both doors (AC2), the 1:1 family
(AC3), cycle refusal incl. the inverse-stored direction (AC4), the
``precis-relations`` skill table (AC5) and that an unconstrained relation is
untouched (AC6).
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._link_tag_ops import apply_link_ops
from precis.handlers.concept import ConceptHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.quest import QuestHandler
from precis.store import Store
from precis.taproot.hub import EVIDENCE_SRC_KINDS, PATHWAY_EVIDENCE_KINDS
from precis.utils import handle_registry

#: The relations migration 0180 seeds a rule on — nothing else.
SEEDED = frozenset(
    {
        "contradicts",
        "draft-of",
        "plan-of",
        "dossier-of",
        "establishes",
        "corroborates",
        "specialises",
        "contains",
        "has-prerequisite",
        "serves",
    }
)


def _ref(store: Store, kind: str, title: str, slug: str | None = None) -> int:
    return store.insert_ref(kind=kind, slug=slug, title=title).id


def _links(store: Store, src: int) -> list[tuple[int, str]]:
    return [
        (link.dst_ref_id, link.relation)
        for link in store.links_for(src, direction="out")
    ]


# ── AC1: the seeded rows ────────────────────────────────────────────


def test_seeded_rows_are_exactly_the_documented_set(store: Store) -> None:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT slug FROM relations WHERE domain_kinds IS NOT NULL "
            "OR functional OR acyclic"
        ).fetchall()
        range_only = conn.execute(
            "SELECT slug FROM relations WHERE range_kinds IS NOT NULL "
            "AND domain_kinds IS NULL"
        ).fetchall()
    assert {r[0] for r in rows} == SEEDED
    # no row is range-only, and ``instance-of`` is the taxon item's to seed
    assert range_only == []


def test_seeded_values(store: Store) -> None:
    c = store.relation_constraints(refresh=True)
    assert c["contradicts"].domain_kinds == frozenset({"memory"})
    assert c["contradicts"].range_kinds == frozenset({"memory"})
    for slug in ("draft-of", "plan-of", "dossier-of"):
        assert c[slug].functional and not c[slug].acyclic
    evidence = EVIDENCE_SRC_KINDS | PATHWAY_EVIDENCE_KINDS
    assert c["establishes"].domain_kinds == evidence
    assert c["establishes"].range_kinds == frozenset({"finding"})
    assert c["corroborates"].domain_kinds == evidence
    # also the paper -> dossier-draft integration disposition (migration 0085)
    assert c["corroborates"].range_kinds == frozenset({"finding", "draft"})
    for slug in ("specialises", "contains", "has-prerequisite", "serves"):
        assert c[slug].transitive and c[slug].acyclic
    assert not c["cites"].constrained


# ── AC2: contradicts through both doors ─────────────────────────────


class TestContradicts:
    def test_apply_link_ops_door(self, store: Store) -> None:
        m = _ref(store, "memory", "a claim")
        _ref(store, "paper", "a paper", slug="smith2020")
        with pytest.raises(BadInput) as exc:
            apply_link_ops(
                store, m, link="paper:smith2020", unlink=None, rel="contradicts"
            )
        assert "range_kinds = memory" in exc.value.cause
        assert "'contradicts'" in exc.value.cause
        # the relation description (the rule's why) rides in the message
        assert "adjudication-derived" in exc.value.cause
        assert _links(store, m) == []

    def test_numeric_ref_link_door(self, store: Store, hub: Hub) -> None:
        h = MemoryHandler(hub=hub)
        m = _ref(store, "memory", "a claim")
        _ref(store, "paper", "a paper", slug="smith2020")
        with pytest.raises(BadInput, match=r"range_kinds = memory"):
            h.link(id=m, target="paper:smith2020", rel="contradicts")
        assert _links(store, m) == []

    def test_domain_end_is_named(self, store: Store) -> None:
        f = _ref(store, "finding", "a finding")
        m = _ref(store, "memory", "a claim")
        with pytest.raises(BadInput, match=r"domain_kinds = memory: source"):
            apply_link_ops(store, f, link=f"memory:{m}", unlink=None, rel="contradicts")

    def test_memory_to_memory_still_allowed(self, store: Store, hub: Hub) -> None:
        a = _ref(store, "memory", "a")
        b = _ref(store, "memory", "b")
        c = _ref(store, "memory", "c")
        assert apply_link_ops(
            store, a, link=f"memory:{b}", unlink=None, rel="contradicts"
        ) == (1, 0)
        MemoryHandler(hub=hub).link(id=a, target=f"memory:{c}", rel="contradicts")
        assert {d for d, _r in _links(store, a)} == {b, c}

    def test_inverse_slug_is_not_a_bypass(self, store: Store) -> None:
        m = _ref(store, "memory", "a claim")
        _ref(store, "paper", "a paper", slug="smith2020")
        with pytest.raises(BadInput, match=r"'contradicts' (domain|range)_kinds"):
            apply_link_ops(
                store, m, link="paper:smith2020", unlink=None, rel="contradicted-by"
            )


# ── AC3: the 1:1 family via the generic verb ────────────────────────


class TestFunctional:
    def _project_with_draft(self, store: Store) -> tuple[int, int]:
        proj = _ref(store, "todo", "project")
        ref, _chunk = store.drafts.create_draft(
            name="first", title="First", project_ref_id=proj
        )
        return proj, ref.id

    def test_second_draft_of_names_the_existing_draft(self, store: Store) -> None:
        proj, first = self._project_with_draft(store)
        second = _ref(store, "draft", "second", slug="second")
        with pytest.raises(BadInput) as exc:
            apply_link_ops(
                store, second, link=f"todo:{proj}", unlink=None, rel="draft-of"
            )
        assert "functional" in exc.value.cause
        assert f"already has {handle_registry.try_format('draft', first)}" in (
            exc.value.cause
        )
        assert (proj, "draft-of") not in _links(store, second)

    def test_inverse_slug_from_the_project_side(self, store: Store) -> None:
        proj, _first = self._project_with_draft(store)
        _ref(store, "draft", "second", slug="second")
        with pytest.raises(BadInput, match="functional"):
            apply_link_ops(
                store, proj, link="draft:second", unlink=None, rel="has-draft"
            )

    def test_relinking_the_same_edge_is_idempotent(self, store: Store) -> None:
        proj, first = self._project_with_draft(store)
        assert apply_link_ops(
            store, first, link=f"todo:{proj}", unlink=None, rel="draft-of"
        ) == (1, 0)

    def test_remove_then_add_swaps_the_draft(self, store: Store) -> None:
        proj, first = self._project_with_draft(store)
        second = _ref(store, "draft", "second", slug="second")
        apply_link_ops(store, first, link=None, unlink=f"todo:{proj}", rel="draft-of")
        apply_link_ops(store, second, link=f"todo:{proj}", unlink=None, rel="draft-of")
        assert (proj, "draft-of") in _links(store, second)

    def test_a_retired_draft_does_not_hold_the_slot(self, store: Store) -> None:
        proj, first = self._project_with_draft(store)
        store.retire_ref(first)
        second = _ref(store, "draft", "second", slug="second")
        apply_link_ops(store, second, link=f"todo:{proj}", unlink=None, rel="draft-of")
        assert (proj, "draft-of") in _links(store, second)

    def test_create_draft_valueerror_path_still_fires(self, store: Store) -> None:
        proj, _first = self._project_with_draft(store)
        with pytest.raises(ValueError, match="already has a draft"):
            store.drafts.create_draft(name="x", title="X", project_ref_id=proj)

    def test_plan_of_is_independent_of_draft_of(self, store: Store) -> None:
        proj, _first = self._project_with_draft(store)
        plan = _ref(store, "plan", "a plan", slug="aplan")
        apply_link_ops(store, plan, link=f"todo:{proj}", unlink=None, rel="plan-of")
        assert (proj, "plan-of") in _links(store, plan)


# ── AC4: cycles ─────────────────────────────────────────────────────


class TestAcyclic:
    def test_serves_cycle_through_the_link_verb(self, store: Store, hub: Hub) -> None:
        h = QuestHandler(hub=hub)
        a = _ref(store, "quest", "A")
        b = _ref(store, "quest", "B")
        c = _ref(store, "quest", "C")
        h.link(id=a, target=f"quest:{b}", rel="serves")
        h.link(id=b, target=f"quest:{c}", rel="serves")
        # c serves a would close a -> b -> c -> a (transitively)
        with pytest.raises(BadInput, match="cycle"):
            h.link(id=c, target=f"quest:{a}", rel="serves")
        assert _links(store, c) == []

    def test_serves_cycle_through_apply_link_ops(self, store: Store) -> None:
        a = _ref(store, "quest", "A")
        b = _ref(store, "quest", "B")
        apply_link_ops(store, a, link=f"quest:{b}", unlink=None, rel="serves")
        with pytest.raises(BadInput, match="cycle"):
            apply_link_ops(store, b, link=f"quest:{a}", unlink=None, rel="serves")

    def test_has_prerequisite_cycle(self, store: Store, hub: Hub) -> None:
        h = ConceptHandler(hub=hub)
        a = _ref(store, "concept", "A")
        b = _ref(store, "concept", "B")
        c = _ref(store, "concept", "C")
        h.link(id=a, target=f"concept:{b}", rel="has-prerequisite")
        h.link(id=b, target=f"concept:{c}", rel="has-prerequisite")
        with pytest.raises(BadInput, match="cycle"):
            h.link(id=c, target=f"concept:{a}", rel="has-prerequisite")

    def test_contains_cycle_between_components(self, store: Store) -> None:
        a = _ref(store, "component", "A", slug="comp-a")
        b = _ref(store, "component", "B", slug="comp-b")
        apply_link_ops(store, a, link="component:comp-b", unlink=None, rel="contains")
        with pytest.raises(BadInput, match="cycle"):
            apply_link_ops(
                store, b, link="component:comp-a", unlink=None, rel="contains"
            )

    def test_inverse_stored_edge_counts(self, store: Store) -> None:
        """A tree written as ``part-of`` (child -> parent) is the same tree:
        a ``contains`` that would close it is refused, and the inverse slug
        itself is checked as the constrained edge."""
        parent = _ref(store, "component", "P", slug="comp-p")
        child = _ref(store, "component", "C", slug="comp-c")
        store.add_link(src_ref_id=child, dst_ref_id=parent, relation="part-of")
        with pytest.raises(BadInput, match="cycle"):
            apply_link_ops(
                store, child, link="component:comp-p", unlink=None, rel="contains"
            )
        with pytest.raises(BadInput, match="cycle"):
            apply_link_ops(
                store, parent, link="component:comp-c", unlink=None, rel="part-of"
            )

    def test_a_dag_diamond_is_fine(self, store: Store) -> None:
        a, b, c, d = (_ref(store, "quest", n) for n in "ABCD")
        for s, t in ((a, b), (a, c), (b, d), (c, d)):
            apply_link_ops(store, s, link=f"quest:{t}", unlink=None, rel="serves")
        assert {x for x, _r in _links(store, a)} == {b, c}


class TestAncestors:
    def test_walks_either_stored_direction(self, store: Store) -> None:
        a = _ref(store, "component", "A", slug="anc-a")
        b = _ref(store, "component", "B", slug="anc-b")
        c = _ref(store, "component", "C", slug="anc-c")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contains")
        store.add_link(src_ref_id=c, dst_ref_id=b, relation="part-of")  # b contains c
        assert store.ancestors("contains", c) == {b, a}
        assert store.ancestors("contains", b) == {a}
        assert store.ancestors("contains", a) == set()

    def test_depth_cap_terminates_on_a_pre_existing_cycle(self, store: Store) -> None:
        a = _ref(store, "quest", "A")
        b = _ref(store, "quest", "B")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="serves")
        store.add_link(src_ref_id=b, dst_ref_id=a, relation="serves")  # past the door
        assert store.ancestors("serves", a, 5) == {a, b}

    def test_depth_cap_limits_reach(self, store: Store) -> None:
        ids = [_ref(store, "quest", f"q{i}") for i in range(5)]
        for s, t in pairwise(ids):
            store.add_link(src_ref_id=s, dst_ref_id=t, relation="serves")
        assert store.ancestors("serves", ids[-1], 2) == {ids[-2], ids[-3]}
        assert store.ancestors("serves", ids[-1]) == set(ids[:-1])


# ── AC5: the runtime skill table ────────────────────────────────────


def _cell(kinds: frozenset[str] | None) -> str:
    return ", ".join(sorted(kinds)) if kinds else "any"


def test_relations_skill_table_matches_the_seeded_rows(store: Store) -> None:
    """The skill's constraint table is the seeded rows rendered: every row
    in ``relations`` with a constraint appears with its domain, range,
    functional and acyclic cells equal to the DB's."""
    path = Path(__file__).parent.parent / "src/precis/data/skills/precis-relations.md"
    table: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| `([a-z-]+)` \|(.*)\|\s*$", line)
        if m and line.count("|") == 6:
            table[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    constraints = store.relation_constraints(refresh=True)
    constrained = {s for s, rc in constraints.items() if rc.constrained}
    assert constrained == SEEDED
    for slug in SEEDED:
        rc = constraints[slug]
        assert table.get(slug) == [
            _cell(rc.domain_kinds),
            _cell(rc.range_kinds),
            "yes" if rc.functional else "-",
            "yes" if rc.acyclic else "-",
        ], slug


# ── AC6: an unconstrained relation behaves exactly as before ────────


class TestUnconstrainedUnchanged:
    def test_apply_link_ops_door(self, store: Store) -> None:
        m = _ref(store, "memory", "m")
        p = _ref(store, "paper", "p", slug="jones2021")
        f = _ref(store, "finding", "f")
        for src, dst, rel in (
            (m, "paper:jones2021", "cites"),
            (p, f"finding:{f}", "related-to"),
            (f, f"memory:{m}", "cites"),
            (m, f"finding:{f}", "cites"),
        ):
            assert apply_link_ops(store, src, link=dst, unlink=None, rel=rel) == (1, 0)
        assert {d for d, _r in _links(store, m)} == {p, f}

    def test_numeric_ref_link_door(self, store: Store, hub: Hub) -> None:
        h = MemoryHandler(hub=hub)
        a = _ref(store, "memory", "a")
        b = _ref(store, "memory", "b")
        h.link(id=a, target=f"memory:{b}", rel="derived-from")
        h.link(id=b, target=f"memory:{a}", rel="derived-from")  # no acyclic row
        assert _links(store, a) == [(b, "derived-from")]
        assert _links(store, b) == [(a, "derived-from")]

    def test_unknown_slug_in_the_cache_is_unconstrained(self, store: Store) -> None:
        assert "no-such-relation" not in store.relation_constraints()
