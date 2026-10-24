"""onto-groups — ontology-v2 slice 1: measurand quantity-class groups.

Carries out Reto's Q8 ruling (``docs/backlog/ontology-v2.md``, decisions
log, 2026-10-22): **13** quantity-class groups under ``measurand``, with
the member mapping table exactly as ruled — no regrouping. For every live
direct leaf the driver adds a ``specialises -> group`` edge with
``meta.axis='quantity-class'``; the leaf's direct edge to the root stays
(the spec's bridge until the ancestor-path lookup — landed in slice 0 —
proves out). Group nodes are minted as ``dimension_kind='categorical'``
navigation nodes (the start contract holds; a group is not a measurand).

The mapping table is the contract — it is read, not guessed:

* a member title that resolves to no live leaf, or to several, is
  reported and ``--apply`` refuses outright (no partial groupings);
* a live leaf no member resolves lands in ``measurand/other`` (the
  spec's catch-all), minted only when such leaves exist — leaves whose
  meta marks them as proposed pending slice-1 placement (the
  climate-orch batch) are flagged in the report, not guessed into a
  named group;
* the six live taxa owned by ``scripts/onto-repoint`` / its in-flight
  fix (the ``length -> overall length`` merge pair's loser and the five
  legacy terms tn460131/460151/460140/460150/460142) are skipped — this
  driver neither re-parents nor retires them.

Dry run by default; ``--apply`` writes. Re-runnable: a leaf already
edged to its group is reported and skipped, an existing group node under
``measurand`` is reused (one with the same slug elsewhere is refused —
it is not this tree's group). Every edge the driver writes carries
``meta={'axis': 'quantity-class', 'migration': 'onto-groups'}`` so a
rollback pass can find exactly these rows — the spec's rollback shape.

Usage (dev DB): ``scripts/onto-groups [--apply]``
Prod:            ``scripts/_prod-run scripts/onto-groups [--apply]``
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _onto_repoint import (
    _has_specialises_edge,
    _measurand_root,
    _taxon_by_slug,
)

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

#: The axis every edge this driver writes carries (ontology-v2 slice 1).
_AXIS = "quantity-class"

#: ``meta.migration`` stamp on every written edge: the rollback hook —
#: a rollback pass deletes these links ``WHERE meta->>'migration' = ...``
#: and retires the group nodes (found by slug under measurand). It is an
#: edge key only: putting it in a *node's* meta would break
#: ``edit(kind='taxon')``'s fixed-key validation on the group nodes.
_MARK = "onto-groups"

_EDGE_META = {"axis": _AXIS, "migration": _MARK}

#: The 13 ruled quantity-class groups and their member taxon titles,
#: verbatim from the Q8 member-mapping table in
#: ``docs/backlog/ontology-v2.md`` (Reto 2026-10-22). ``length -> overall
#: length`` and ``price per gram -> cost per unit mass`` name the merge
#: winners, so the winners are the members. The table's ``new:`` entries
#: (BET surface area, ECSA, band gap, overpotential, Tafel slope, onset
#: potential, TOF) are slice-2 mints — noted per group, not resolved here.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "geometry",
        (
            "across flats",
            "bore diameter",
            "drive size",
            "head diameter",
            "head height",
            "height",
            "inner diameter",
            "overall length",
            "outer diameter",
            "minimum bend radius",
            "thickness",
            "wall thickness",
            "width",
            "thread pitch",
            "head angle",
        ),
    ),
    (
        "component form",
        (
            "drive code",
            "drive type",
            "head form",
            "point type",
            "thread size",
            "grade",
            "surface finish",
            "is reinforced",
        ),
    ),
    (
        "mechanical",
        (
            "Young's modulus",
            "shear modulus",
            "Poisson's ratio",
            "ultimate tensile strength",
            "tensile yield strength",
            "elongation at break",
            "Vickers hardness",
            "dynamic load rating",
            "maximum working pressure",
        ),
    ),
    (
        "thermal",
        (
            "melting point",
            "maximum service temperature",
            "specific heat capacity",
            "thermal conductivity",
            "coefficient of thermal expansion",
        ),
    ),
    (
        "electrical and dielectric",
        (
            "electrical resistivity",
            "dielectric strength",
            "relative permittivity",
            "is magnetic",
            "electric field magnitude",
            "electrostatic potential",
        ),
    ),
    # new in slice 2: BET surface area, ECSA
    ("bulk and structure", ("density", "mass", "crystal structure")),
    (
        "molecular and surface",
        (
            "dipole moment",
            "net partial charge",
            "surface charge density",
            "H-bond donor count",
            "H-bond acceptor count",
            "hydrophobicity index",
            "contact angle",
            "absorption maximum wavelength",
        ),
    ),
    # new in slice 2: band gap
    (
        "energetics",
        (
            "adsorption energy",
            "binding energy",
            "reaction step free energy",
            "activation barrier",
        ),
    ),
    # new in slice 2: overpotential, Tafel slope, onset potential, TOF
    (
        "electrochemical performance",
        (
            "faradaic efficiency",
            "partial current density",
            "total current density",
            "product yield rate per geometric area",
            "product yield rate per catalyst mass",
            "limiting potential",
            "stability duration",
        ),
    ),
    (
        "reaction conditions",
        (
            "temperature",
            "pressure",
            "reaction time",
            "solvent",
            "scale",
            "catalyst loading",
            "applied electrode potential",
            "NO partial pressure",
            "NO feed fraction",
            "electrolyte concentration",
        ),
    ),
    (
        "reaction outcome",
        (
            "yield",
            "product selectivity",
            "reactant conversion",
            "reaction product",
            "atom economy",
            "enantiomeric excess",
        ),
    ),
    ("cost", ("unit cost", "cost per unit mass")),
    ("generic", ("count", "ratio", "angle", "PCB routed-net count")),
)

#: Catch-all group for live leaves no member resolves (spec: "a leaf with
#: no group lands in measurand/other"). Minted only when such leaves exist.
OTHER_GROUP = "other"

#: Live leaves owned by scripts/onto-repoint and its in-flight fix
#: (tn460115 — the ``length -> overall length`` merge loser — and the
#: five legacy terms). This driver skips them: it neither re-parents nor
#: retires them (the brief's boundary).
EXCLUDED: frozenset[int] = frozenset({460115, 460131, 460140, 460142, 460150, 460151})


@dataclass(slots=True)
class _Leaf:
    ref_id: int
    title: str
    meta: dict[str, Any]

    @property
    def label(self) -> str:
        return f"tn{self.ref_id} {self.title}"


@dataclass(slots=True)
class _State:
    """Read-only assessment: what the run would do."""

    root_id: int
    leaves: dict[int, _Leaf]  # every live direct child of measurand
    excluded: list[_Leaf] = field(default_factory=list)
    groups_live: dict[str, _Leaf] = field(default_factory=dict)
    groups_mint: list[str] = field(default_factory=list)
    groups_offtree: list[tuple[str, _Leaf]] = field(default_factory=list)
    assign: dict[int, str] = field(default_factory=dict)  # leaf -> group
    member_leaf: dict[tuple[str, str], _Leaf] = field(default_factory=dict)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # member, group
    ambiguous: list[tuple[str, str, list[_Leaf]]] = field(default_factory=list)
    other: list[_Leaf] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.unresolved or self.ambiguous or self.groups_offtree)


def _group_slug(name: str) -> str:
    from precis.taxonomy.nodes import slugify

    return slugify(name)


def _hierarchy_parents(conn: Connection, ref_id: int) -> set[int]:
    """Ref ids a node already specialises (either stored form, ref-level)."""
    rows = conn.execute(
        "SELECT l.dst_ref_id FROM links l WHERE l.relation = 'specialises' "
        "AND l.src_ref_id = %(r)s "
        "AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL "
        "UNION "
        "SELECT l.src_ref_id FROM links l WHERE l.relation = 'generalises' "
        "AND l.dst_ref_id = %(r)s "
        "AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL",
        {"r": ref_id},
    ).fetchall()
    return {int(r[0]) for r in rows}


def _measurand_leaves(conn: Connection, root_id: int) -> dict[int, _Leaf]:
    """Live taxa with a direct hierarchy edge to ``root_id`` — the slice's
    "leaves" (the stored ``specialises`` child -> root row or its
    ``generalises`` inverse, both ref-level)."""
    rows = conn.execute(
        "SELECT r.ref_id, r.title, r.meta FROM refs r "
        "WHERE r.kind = 'taxon' AND r.retired_at IS NULL AND EXISTS ("
        "  SELECT 1 FROM links l "
        "  WHERE l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL AND ("
        "    (l.relation = 'specialises' AND l.src_ref_id = r.ref_id "
        "     AND l.dst_ref_id = %(r)s) OR"
        "    (l.relation = 'generalises' AND l.dst_ref_id = r.ref_id "
        "     AND l.src_ref_id = %(r)s))) ORDER BY r.ref_id",
        {"r": root_id},
    ).fetchall()
    return {
        int(row[0]): _Leaf(int(row[0]), str(row[1] or ""), dict(row[2] or {}))
        for row in rows
    }


def _leaf_terms(leaf: _Leaf) -> set[str]:
    """Matchable terms of a leaf: slug plus normalised name and aliases —
    the same surface ``taxon_find_by_term`` resolves."""
    from precis.reading.concepts import normalize_name
    from precis.utils.text import slugify

    terms = {str(leaf.meta.get("slug") or slugify(leaf.title))}
    terms.add(normalize_name(str(leaf.meta.get("name") or leaf.title)))
    for alias in leaf.meta.get("aliases") or []:
        terms.add(normalize_name(str(alias)))
    return {t for t in terms if t}


def _member_candidates(title: str, pool: dict[int, _Leaf]) -> list[_Leaf]:
    """Live leaves a member title resolves to (slug, name or alias)."""
    from precis.reading.concepts import normalize_name
    from precis.utils.text import slugify

    slug, norm = slugify(title), normalize_name(title)
    return [
        leaf
        for leaf in pool.values()
        if slug in _leaf_terms(leaf) or norm in _leaf_terms(leaf)
    ]


def _collect(store: Store) -> _State:
    """One read pass: leaves, existing group nodes, the resolution."""
    with store.tx() as conn:
        root_id = _measurand_root(conn)
        leaves = _measurand_leaves(conn, root_id)
        st = _State(root_id=root_id, leaves=leaves)
        for name in [n for n, _m in GROUPS] + [OTHER_GROUP]:
            row = _taxon_by_slug(conn, _group_slug(name))
            if row is None:
                st.groups_mint.append(name)
            elif _has_specialises_edge(conn, row["id"], root_id):
                st.groups_live[name] = _Leaf(row["id"], row["title"], row["meta"])
            else:
                st.groups_offtree.append(
                    (name, _Leaf(row["id"], row["title"], row["meta"]))
                )
        pool = dict(leaves)
        for ref_id in sorted(EXCLUDED):
            if ref_id in pool:
                st.excluded.append(pool.pop(ref_id))
        # Group nodes are themselves live children of the root — on a
        # re-run they must not count as unmapped leaves.
        for leaf in st.groups_live.values():
            pool.pop(leaf.ref_id, None)
        limbo: set[int] = set()
        for group, members in GROUPS:
            for title in members:
                cands = _member_candidates(title, pool)
                if len(cands) == 1 and cands[0].ref_id not in st.assign:
                    st.assign[cands[0].ref_id] = group
                    st.member_leaf[(group, title)] = cands[0]
                elif len(cands) == 1:
                    # The leaf is already claimed by another member title —
                    # the table double-maps; refuse, never silently take it.
                    st.ambiguous.append((title, group, cands))
                    limbo.add(cands[0].ref_id)
                elif not cands:
                    st.unresolved.append((title, group))
                else:
                    st.ambiguous.append((title, group, cands))
                    limbo.update(c.ref_id for c in cands)
        st.other = [
            leaf
            for ref_id, leaf in sorted(pool.items())
            if ref_id not in st.assign and ref_id not in limbo
        ]
        return st


def _pending_note(leaf: _Leaf) -> str:
    """Marker for a leaf minted with a 'placement pending slice 1' note —
    it is reported by name, still lands in ``'other'`` (no guessing)."""
    blob = json.dumps(leaf.meta, default=str).lower()
    if "climate-orch" in blob:
        return "proposed by climate-orch"
    if "placement pending" in blob or "pending ontology-v2" in blob:
        return "placement pending"
    return ""


def _group_label(st: _State, name: str) -> str:
    if name in st.groups_live:
        return f"tn{st.groups_live[name].ref_id}"
    if name in dict(st.groups_offtree):
        return "OFF-TREE — refuses"
    return "would mint"


def assess(store: Store) -> tuple[_State, list[str]]:
    """The dry-run report plus the state it reads (shared with --apply)."""
    st = _collect(store)
    lines = [
        f"ontology-v2 slice 1: quantity-class groups under tn{st.root_id} measurand",
        f"{len(st.leaves)} live direct leaves; "
        f"{len(st.excluded)} owned by onto-repoint/-fix (skipped)",
    ]
    for group, members in GROUPS:
        lines.append(f"\n{group} ({len(members)}) -> {_group_label(st, group)}")
        for title in members:
            if (leaf := st.member_leaf.get((group, title))) is not None:
                lines.append(f"  {leaf.label}")
            elif (title, group) in st.unresolved:
                lines.append(f"  '{title}' — UNRESOLVED: no live leaf matches")
            else:
                hit = next(
                    (c for t, g, c in st.ambiguous if t == title and g == group),
                    None,
                )
                assert hit is not None  # resolved, missing or ambiguous
                ids = ", ".join(c.label for c in hit)
                lines.append(f"  '{title}' — AMBIGUOUS: {ids}")
    if st.other:
        pending = [l for l in st.other if _pending_note(l)]
        plain = [l for l in st.other if not _pending_note(l)]
        if pending:
            lines.append(
                f"\nunmapped, placement pending ({len(pending)}) — lands in "
                f"'{OTHER_GROUP}' but flagged for the review queue:"
            )
            lines += [f"  {leaf.label} — {_pending_note(leaf)}" for leaf in pending]
        if plain:
            lines.append(f"\nunmapped leaves -> '{OTHER_GROUP}' ({len(plain)}):")
            lines += [f"  {leaf.label}" for leaf in plain]
    if st.excluded:
        lines.append("\nexcluded — owned by onto-repoint/-fix, untouched:")
        lines += [f"  {leaf.label}" for leaf in st.excluded]
    if st.groups_offtree:
        lines.append("\nsame-slug taxa NOT under measurand — refuses to adopt:")
        lines += [f"  '{name}' -> {leaf.label}" for name, leaf in st.groups_offtree]
    n_edges = len(st.assign) + len(st.other)
    n_groups = len(GROUPS) + (1 if st.other else 0)
    mints = [n for n in st.groups_mint if n != OTHER_GROUP or st.other]
    lines.append(
        f"\nplan: mint {len(mints)} group node(s); write {n_edges} "
        f"leaf->group edge(s) + {n_groups} group->root edge(s), "
        "all meta axis='quantity-class'"
    )
    if st.refused:
        lines.append("PROBLEMS — --apply refuses until the table or the data is fixed:")
        lines += [f"  unresolved member '{t}' ({g})" for t, g in st.unresolved]
        lines += [
            f"  ambiguous member '{t}' ({len(c)} candidates)"
            for t, _g, c in st.ambiguous
        ]
        lines += [f"  off-tree group '{n}'" for n, _l in st.groups_offtree]
    return st, lines


def _mint_group(store: Store, conn: Connection, root_id: int, name: str) -> int:
    """Mint ``name`` under measurand as a ``dimension_kind='categorical'``
    navigation node (the Q8 shape, same as scripts/_onto_repoint.py mints)."""
    definition = f"measurand group '{name}' (ontology-v2 Q8)"
    meta = {
        "name": name,
        "norm_name": name.lower(),
        "slug": _group_slug(name),
        "definition": definition,
        "status": "proposed",
        "dimension_kind": "categorical",
    }
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta, conn=conn)
    store.add_link(
        src_ref_id=int(ref.id),
        dst_ref_id=root_id,
        relation="specialises",
        meta=_EDGE_META,
        merge_meta=True,
        set_by="agent",
        conn=conn,
    )
    store.chunks.upsert_card_combined(int(ref.id), f"{name} — {definition}", conn=conn)
    return int(ref.id)


def apply(store: Store) -> tuple[list[str], int]:
    """Assess fresh, refuse on problems, else write everything in one
    transaction (the run is atomic: it lands whole or not at all)."""
    st, lines = assess(store)
    if st.refused:
        return lines, 1
    out = list(lines)
    with store.tx() as conn:
        group_ids: dict[str, int] = {
            name: leaf.ref_id for name, leaf in st.groups_live.items()
        }
        for name in st.groups_mint:
            if name == OTHER_GROUP and not st.other:
                continue
            group_ids[name] = _mint_group(store, conn, st.root_id, name)
            out.append(f"minted group tn{group_ids[name]} '{name}'")
        # A group adopted live may carry a bare root edge (e.g. minted by
        # onto-repoint's reparent half): restamp axis + migration on it.
        for name, gid in sorted(group_ids.items()):
            store.add_link(
                src_ref_id=gid,
                dst_ref_id=st.root_id,
                relation="specialises",
                meta=_EDGE_META,
                merge_meta=True,
                set_by="agent",
                conn=conn,
            )
            if name in st.groups_live:
                out.append(f"restamped root edge on group tn{gid} '{name}'")
        for leaf, group in [
            *[(st.leaves[r], g) for r, g in sorted(st.assign.items())],
            *[(leaf, OTHER_GROUP) for leaf in st.other],
        ]:
            gid = group_ids[group]
            claimed = _hierarchy_parents(conn, leaf.ref_id) & set(group_ids.values())
            if claimed - {gid}:
                held = sorted(n for n, i in group_ids.items() if i in claimed - {gid})
                out.append(
                    f"{leaf.label} already grouped under "
                    f"{'/'.join(held)} — not adding '{group}'"
                )
                continue
            already = gid in claimed
            store.add_link(
                src_ref_id=leaf.ref_id,
                dst_ref_id=gid,
                relation="specialises",
                meta=_EDGE_META,
                merge_meta=True,
                set_by="agent",
                conn=conn,
            )
            out.append(
                f"{leaf.label} -> '{group}'"
                + (" (already grouped; axis/migration ensured)" if already else "")
            )
    out.append(
        f"applied: {len(st.assign) + len(st.other)} leaf edge(s), "
        f"{len([n for n in st.groups_mint if n != OTHER_GROUP or st.other])}"
        " group node(s) minted"
    )
    return out, 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="dry run unless --apply; prod via scripts/_prod-run",
    )
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = p.parse_args(argv)

    from _common import open_store

    store, _cfg = open_store()
    try:
        if args.apply:
            lines, rc = apply(store)
        else:
            st, lines = assess(store)
            rc = 1 if st.refused else 0
            lines.append("\ndry run — re-run with --apply to write")
        for line in lines:
            print(line)
        return rc
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
