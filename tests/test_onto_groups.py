"""Ontology-v2 slice 1 measurand quantity-class groups (scripts/onto-groups).

The driver mints the 13 Q8-ruled group nodes under ``measurand`` and adds
each live leaf's ``specialises -> group`` edge with
``meta={'axis': 'quantity-class', 'migration': 'onto-groups'}`` — the
direct root edge stays. Members resolve to live leaves by slug/name/alias;
unresolved or ambiguous members refuse the whole apply, unmapped leaves
land in ``measurand/other``, and the six ids owned by onto-repoint/-fix
are skipped. Real PG (the ``store`` fixture).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from precis.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _onto_groups as groups_script
import _onto_repoint

_SI_LEN = "1,0,0,0,0,0,0"

#: A trimmed table covering the fixture world's leaves — the real one has
#: 87 members and would refuse on the missing ones (by design).
_SMALL = (
    ("geometry", ("across flats", "bore diameter")),
    ("bulk and structure", ("density",)),
)


def _taxon(
    store: Store,
    name: str,
    *,
    parent: int | None = None,
    canonical: str | None = "m",
    dimension_kind: str | None = "si",
    si_vector: str | None = _SI_LEN,
    slug: str | None = None,
    norm_name: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    meta = {
        "name": name,
        "norm_name": norm_name or name.lower(),
        "slug": slug or name.lower().replace(" ", "-"),
        "status": "proposed",
    }
    if extra_meta:
        meta.update(extra_meta)
    if canonical is not None:
        meta["canonical_unit"] = canonical
    if dimension_kind is not None:
        meta["dimension_kind"] = dimension_kind
    if si_vector is not None:
        meta["si_vector"] = si_vector
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta)
    if parent is not None:
        store.add_link(src_ref_id=ref.id, dst_ref_id=parent, relation="specialises")
    return int(ref.id)


@pytest.fixture
def world(store: Store) -> dict[str, Any]:
    """A measurand root with member leaves, an unmapped leaf and a retired
    leaf (never a leaf, never grouped)."""
    root = _taxon(
        store, "measurand", canonical=None, dimension_kind=None, si_vector=None
    )
    w = {
        "root": root,
        "flats": _taxon(store, "across flats", parent=root, slug="across-flats"),
        "bore": _taxon(store, "bore diameter", parent=root, slug="bore-diameter"),
        "density": _taxon(store, "density", parent=root),
        "damkohler": _taxon(store, "damkohler", parent=root),
        "stray": _taxon(store, "stray leaf", parent=root, slug="stray-leaf"),
    }
    store.retire_ref(w["stray"])
    return w


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(groups_script, "GROUPS", _SMALL)


def _edges(store: Store, src: int) -> list[tuple[int, str | None, str | None]]:
    """(dst_ref_id, axis, migration) of the leaf's specialises edges."""
    with store.pool.connection() as conn:
        return [
            (int(r[0]), r[1], r[2])
            for r in conn.execute(
                "SELECT dst_ref_id, meta->>'axis', meta->>'migration' "
                "FROM links WHERE src_ref_id = %s AND relation = 'specialises' "
                "ORDER BY dst_ref_id",
                (src,),
            ).fetchall()
        ]


def _group(store: Store, slug: str) -> dict[str, Any] | None:
    with store.pool.connection() as conn:
        return _onto_repoint._taxon_by_slug(conn, slug)


class TestTable:
    """The mapping table is the ruling, verbatim: 13 groups in the ruled
    order, each member a non-empty title, no title in two groups."""

    def test_thirteen_ruled_groups_in_order(self) -> None:
        assert [g for g, _m in groups_script.GROUPS] == [
            "geometry",
            "component form",
            "mechanical",
            "thermal",
            "electrical and dielectric",
            "bulk and structure",
            "molecular and surface",
            "energetics",
            "electrochemical performance",
            "reaction conditions",
            "reaction outcome",
            "cost",
            "generic",
        ]

    def test_members_nonempty_and_unique(self) -> None:
        members = [t.lower() for _g, ms in groups_script.GROUPS for t in ms]
        assert all(t.strip() for t in members)
        assert len(members) == len(set(members))
        # The ruled table names exactly 87 existing leaves — the slice-2
        # extension (BET, ECSA, band gap, overpotential, Tafel slope,
        # onset potential, TOF) must update this count when it lands.
        assert len(members) == 87, "Q8 table drifted; update for slice-2 members"

    def test_excluded_ids_are_the_repoint_six(self) -> None:
        assert (
            frozenset({460115, 460131, 460140, 460142, 460150, 460151})
            == groups_script.EXCLUDED
        )


@pytest.mark.usefixtures("small")
class TestAssess:
    def test_dry_run_reports_and_writes_nothing(
        self, store: Store, world: dict
    ) -> None:
        st, lines = groups_script.assess(store)
        text = "\n".join(lines)
        assert f"tn{world['flats']}" in text
        assert "unmapped leaves -> 'other' (1)" in text
        # Nothing written: no group node, no new edges.
        assert _group(store, "geometry") is None
        assert _edges(store, world["flats"]) == [(world["root"], None, None)]
        # The retired leaf is not even counted.
        assert world["stray"] not in st.leaves

    def test_assignments(self, store: Store, world: dict) -> None:
        st, _lines = groups_script.assess(store)
        assert st.assign[world["flats"]] == "geometry"
        assert st.assign[world["bore"]] == "geometry"
        assert st.assign[world["density"]] == "bulk and structure"
        assert {l.ref_id for l in st.other} == {world["damkohler"]}
        assert not st.refused

    def test_unresolved_member_refuses(
        self, store: Store, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            groups_script,
            "GROUPS",
            _SMALL + (("bogus-group", ("no such leaf",)),),
        )
        st, lines = groups_script.assess(store)
        assert st.refused
        assert any("no such leaf" in line for line in lines)
        out, rc = groups_script.apply(store)
        assert rc == 1
        assert _group(store, "bogus-group") is None
        assert _edges(store, world["flats"]) == [(world["root"], None, None)]

    def test_ambiguous_member_refuses(self, store: Store, world: dict) -> None:
        # A second leaf normalising to 'density' — two candidates, one member.
        _taxon(store, "density", parent=world["root"], slug="density-2")
        st, lines = groups_script.assess(store)
        assert st.refused
        assert any("density" in line and "AMBIGUOUS" in line for line in lines)
        _out, rc = groups_script.apply(store)
        assert rc == 1

    def test_offtree_group_slug_refuses(self, store: Store, world: dict) -> None:
        _taxon(store, "geometry", parent=None, slug="geometry")
        st, lines = groups_script.assess(store)
        assert st.refused
        assert any("NOT under measurand" in line for line in lines)
        _out, rc = groups_script.apply(store)
        assert rc == 1

    def test_pending_leaf_flagged_not_guessed(self, store: Store, world: dict) -> None:
        # A leaf minted with a 'placement pending' note (the climate-orch
        # batch): named in the report, still lands in 'other'.
        ghg = _taxon(
            store,
            "GHG removal rate",
            parent=world["root"],
            extra_meta={
                "note": "proposed 2026-10-24 by climate-orch — group "
                "placement pending ontology-v2 slice 1",
            },
        )
        st, lines = groups_script.assess(store)
        text = "\n".join(lines)
        assert "unmapped, placement pending (1)" in text
        assert "proposed by climate-orch" in text
        assert ghg in {l.ref_id for l in st.other}

    def test_excluded_leaf_skipped(
        self, store: Store, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(groups_script, "EXCLUDED", frozenset({world["density"]}))
        st, lines = groups_script.assess(store)
        text = "\n".join(lines)
        assert {l.ref_id for l in st.excluded} == {world["density"]}
        assert world["density"] not in st.assign
        # An excluded leaf is not catch-all'd into 'other' …
        assert world["density"] not in {l.ref_id for l in st.other}
        # … and its now-unresolvable member makes the apply refuse.
        assert st.refused
        assert "unresolved member 'density'" in text


@pytest.mark.usefixtures("small")
class TestApply:
    def test_mints_groups_and_edges(self, store: Store, world: dict) -> None:
        out, rc = groups_script.apply(store)
        assert rc == 0
        text = "\n".join(out)
        assert "minted group" in text and "applied:" in text
        geometry = _group(store, "geometry")
        other = _group(store, "other")
        assert geometry is not None and other is not None
        # Group meta: categorical navigation node; the start contract holds.
        assert geometry["meta"]["dimension_kind"] == "categorical"
        assert geometry["meta"]["status"] == "proposed"
        # Leaf -> group edge carries axis + migration; the root edge stays.
        edges = _edges(store, world["flats"])
        assert (geometry["id"], "quantity-class", "onto-groups") in edges
        assert (world["root"], None, None) in edges
        # Group -> root edge is labelled too.
        assert _edges(store, geometry["id"]) == [
            (world["root"], "quantity-class", "onto-groups")
        ]
        # The unmapped leaf landed in 'other'.
        d_edges = _edges(store, world["damkohler"])
        assert (other["id"], "quantity-class", "onto-groups") in d_edges
        # The retired leaf got nothing.
        assert _edges(store, world["stray"]) == [(world["root"], None, None)]

    def test_apply_is_idempotent(self, store: Store, world: dict) -> None:
        groups_script.apply(store)
        out, rc = groups_script.apply(store)
        assert rc == 0
        text = "\n".join(out)
        assert "already grouped" in text
        assert "minted group" not in text
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM links WHERE relation='specialises'"
            ).fetchone()
        assert row is not None
        # 5 root edges (incl. the retired leaf's) + 4 leaf->group +
        # 3 group->root (geometry, bulk and structure, other).
        assert int(row[0]) == 5 + 4 + 3
        # And the minted groups were not caught up as unmapped 'other' leaves.
        geometry = _group(store, "geometry")
        assert geometry is not None
        assert _edges(store, geometry["id"]) == [
            (world["root"], "quantity-class", "onto-groups")
        ]

    def test_different_group_edge_not_doubled(self, store: Store, world: dict) -> None:
        # density is assigned to 'bulk and structure' but already carries
        # an edge to 'geometry': reported, no second group edge written.
        geometry = _taxon(
            store,
            "geometry",
            parent=world["root"],
            canonical=None,
            dimension_kind="categorical",
            si_vector=None,
            slug="geometry",
        )
        store.add_link(
            src_ref_id=world["density"], dst_ref_id=geometry, relation="specialises"
        )
        out, rc = groups_script.apply(store)
        assert rc == 0
        assert any(
            "already grouped under" in line and "not adding" in line for line in out
        )
        dsts = {d for d, _ax, _m in _edges(store, world["density"])}
        assert geometry in dsts
        bs = _group(store, "bulk-and-structure")
        assert bs is not None
        assert bs["id"] not in dsts

    def test_apply_reuses_live_group_and_restamps(
        self, store: Store, world: dict
    ) -> None:
        # A pre-existing group node under measurand with a bare root edge
        # (the onto-repoint mint shape): reused, edge restamped.
        geometry = _taxon(
            store,
            "geometry",
            parent=world["root"],
            canonical=None,
            dimension_kind="categorical",
            si_vector=None,
            slug="geometry",
        )
        out, rc = groups_script.apply(store)
        assert rc == 0
        assert any("restamped root edge" in line for line in out)
        edges = _edges(store, world["flats"])
        assert (geometry, "quantity-class", "onto-groups") in edges
        assert _edges(store, geometry) == [
            (world["root"], "quantity-class", "onto-groups")
        ]
