"""onto-repoint — re-point live measures, merge/re-parent the Q8 taxa.

Carries out Reto's ontology-v2 Q8 rulings (``docs/backlog/ontology-v2.md``,
decisions log): "Merged/retired before slice 1: duplicate bore diameter
and maximum service temperature, length→overall length, price per gram→
cost per unit mass" — and, superseding the log's retire list (Reto
2026-10-22): keep the five polymer taxa (Delta Length, Unit Length,
Persistence Length, Thermal Half Life, PSS short fraction) and re-parent
each under its matching Q8 measurand group.

For each entry the script either:

* ``merge`` — re-points the loser's measure rows onto the winner through
  the canonical_unit-safe path
  (:meth:`precis.store.Store.repoint_measurand_measures`, the 0234
  ``precis.allow_measurand_repoint`` door — same canonical_unit,
  dimension_kind and si_vector or it refuses), then merges through the
  garden apply/undo path (:func:`precis.graph.garden.apply.apply_merge`).
* ``reparent`` — resolves the named Q8 group node under ``measurand``
  (minting it as a ``dimension_kind='categorical'`` navigation node via
  the store taxon path when absent) and adds the leaf's
  ``specialises -> group`` edge with ``meta.axis='quantity-class'``
  through :meth:`Store.add_link`. The leaf keeps its direct
  ``specialises`` edge to the root — the spec's double-edge bridge until
  ancestor-path lookup lands.

Dry run by default; ``--apply`` writes. Re-runnable: an entry already in
its end state is reported and skipped. The merge half needs migration
0234 applied on the target DB (without it the frozen trigger refuses);
re-parenting needs nothing new.

Usage (dev DB): ``scripts/onto-repoint [--apply]``
Prod:            ``scripts/_prod-run scripts/onto-repoint [--apply]``
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store


@dataclass(frozen=True, slots=True)
class Plan:
    """One taxon and what Q8 rules for it. ``action='merge'``: ``target``
    is the winner ref id. ``action='reparent'``: ``target`` is the Q8
    group NAME — the node is resolved by slug under ``measurand`` and
    minted when absent."""

    loser: int
    target: int | str
    action: str  # "merge" | "reparent"


#: The Q8 pairs not yet applied (see the module docstring). Group names are
#: spelled as the decisions log spells them; the slug resolves the node.
PLAN: tuple[Plan, ...] = (
    Plan(460115, 460116, "merge"),  # Length -> Overall length (12 rows)
    Plan(460131, "geometry", "reparent"),  # Delta Length — a length
    Plan(460151, "geometry", "reparent"),  # Unit Length — a length
    Plan(460140, "molecular and surface", "reparent"),  # Persistence Length
    Plan(460150, "thermal", "reparent"),  # Thermal Half Life — stability
    Plan(460142, "molecular and surface", "reparent"),  # PSS short fraction
)

#: ``meta.axis`` the slice-1 spec stamps on leaf -> group edges.
_GROUP_AXIS = "quantity-class"


def _ref_row(conn: Connection, ref_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ref_id, kind, title, retired_at IS NOT NULL, meta "
        "FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "kind": row[1],
        "title": row[2],
        "retired": bool(row[3]),
        "meta": row[4] or {},
    }


def _taxon_by_slug(conn: Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ref_id, kind, title, retired_at IS NOT NULL, meta "
        "FROM refs WHERE kind = 'taxon' AND retired_at IS NULL "
        "  AND meta ->> 'slug' = %s ORDER BY ref_id LIMIT 1",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "kind": row[1],
        "title": row[2],
        "retired": bool(row[3]),
        "meta": row[4] or {},
    }


def _measurand_root(conn: Connection) -> int:
    row = conn.execute(
        "SELECT ref_id FROM refs WHERE kind = 'taxon' "
        "AND retired_at IS NULL AND meta ->> 'slug' = 'measurand' "
        "ORDER BY ref_id LIMIT 1"
    ).fetchone()
    assert row is not None, "no live 'measurand' root taxon"
    return int(row[0])


def _measure_counts(conn: Connection, ref_id: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE superseded_by IS NULL), count(*) "
        "FROM measures WHERE measurand_ref_id = %s",
        (ref_id,),
    ).fetchone() or (0, 0)
    stray = conn.execute(
        "SELECT count(*) FROM measures WHERE subject_ref_id = %(r)s "
        "   OR primary_link_id IN (SELECT link_id FROM links "
        "        WHERE src_ref_id = %(r)s OR dst_ref_id = %(r)s)",
        {"r": ref_id},
    ).fetchone() or (0,)
    return {
        "live": int(row[0]),
        "all": int(row[1]),
        "stray": int(stray[0]),
    }


def _unit_diff(s_meta: dict[str, Any], d_meta: dict[str, Any]) -> list[str]:
    return [
        key
        for key in ("canonical_unit", "dimension_kind", "si_vector")
        if s_meta.get(key) != d_meta.get(key)
    ]


def _has_specialises_edge(conn: Connection, child: int, parent: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
        "AND relation = 'specialises'",
        (child, parent),
    ).fetchone()
    return row is not None


def _pending_merge_review(store: Store, loser: int, winner: int) -> int | None:
    """The review_id of a pending garden merge proposal for this pair, if
    one is open — applying through it flips the proposal to approved (the
    second bar), the way the bore-diameter merge landed (review 8170)."""
    from precis.graph.garden.proposals import pending_proposals

    for prop in pending_proposals(store):
        if prop.action != "merge":
            continue
        pl, pw = prop.payload.get("loser"), prop.payload.get("winner")
        if pl == loser and pw == winner:
            return prop.review_id
    return None


def _group_slug(name: str) -> str:
    from precis.taxonomy.nodes import slugify

    return slugify(name)


def _plan_lines(store: Store, plan: Plan) -> list[str]:
    """Read-only assessment of one entry, for the dry run and for the
    pre-write check ``--apply`` repeats before writing."""
    with store.tx() as conn:
        loser = _ref_row(conn, plan.loser)
        counts = _measure_counts(conn, plan.loser)
        label = f"tn{plan.loser} {loser['title'] if loser else '(missing)'}"
        if loser is None:
            return [f"{label}: MISSING — nothing to do"]
        if loser["retired"]:
            done = loser["meta"].get("superseded_by")
            tail = f" (superseded_by tn{done})" if done else ""
            return [f"{label}: already retired{tail} — done, skipping"]

        if plan.action == "reparent":
            group_name = str(plan.target)
            group = _taxon_by_slug(conn, _group_slug(group_name))
            lines = [f"{label} -> group '{group_name}' (re-parent):"]
            if group is None:
                lines.append(
                    f"    group node absent — would mint '{group_name}' "
                    "under measurand (dimension_kind='categorical')"
                )
                group_id = -1
            else:
                group_id = group["id"]
                lines.append(f"    group = tn{group_id} ({group['title']})")
            if group_id != -1 and _has_specialises_edge(conn, plan.loser, group_id):
                lines.append("    specialises edge already present — done")
            else:
                lines.append(
                    f"    add specialises -> group "
                    f"(meta.axis='{_GROUP_AXIS}'); root edge stays"
                )
            if counts["live"]:
                lines.append(
                    f"    note: keeps {counts['live']} live measure row(s) "
                    "on the taxon — no re-point (Reto: keep)"
                )
            return lines

        # merge
        winner = (
            _ref_row(conn, int(plan.target)) if isinstance(plan.target, int) else None
        )
        lines = [f"{label} -> tn{plan.target} (merge):"]
        if winner is None:
            lines.append("    target MISSING — refuses")
        elif winner["retired"]:
            lines.append("    target is retired — refuses")
        else:
            diff = _unit_diff(loser["meta"], winner["meta"])
            if diff:
                lines.append(
                    f"    not canonical_unit-safe: {', '.join(diff)} differ "
                    f"({loser['meta'].get('canonical_unit')!r} vs "
                    f"{winner['meta'].get('canonical_unit')!r}) — refuses"
                )
            else:
                lines.append(
                    f"    re-point {counts['all']} measure row(s) "
                    f"({counts['live']} live) — unit contract matches"
                )
        if counts["stray"]:
            lines.append(
                f"    {counts['stray']} measure(s) also reference the loser "
                "as subject or via an anchored link — resolve by hand"
            )
        lines.append(
            "    then garden merge (apply_merge); loser keeps its name as "
            "an alias on the winner"
        )
        return lines


def _apply_plan(store: Store, plan: Plan, *, actor: str) -> list[str]:
    """Run one entry's writes. Guards re-run inside this function so an
    ``--apply`` never writes past a state the dry run did not show."""
    lines = _plan_lines(store, plan)
    for line in lines:
        if any(
            mark in line for mark in ("refuses", "resolve by hand", "skipping", "done")
        ):
            return lines

    if plan.action == "reparent":
        group_name = str(plan.target)
        with store.tx() as conn:
            group = _taxon_by_slug(conn, _group_slug(group_name))
            if group is None:
                root_id = _measurand_root(conn)
                meta = {
                    "name": group_name,
                    "norm_name": group_name.lower(),
                    "slug": _group_slug(group_name),
                    "definition": (f"measurand group '{group_name}' (ontology-v2 Q8)"),
                    "status": "proposed",
                    "dimension_kind": "categorical",
                }
                ref = store.insert_ref(
                    kind="taxon", slug=None, title=group_name, meta=meta, conn=conn
                )
                store.add_link(
                    src_ref_id=int(ref.id),
                    dst_ref_id=root_id,
                    relation="specialises",
                    set_by="agent",
                    conn=conn,
                )
                store.chunks.upsert_card_combined(
                    int(ref.id),
                    f"{group_name} — measurand group (ontology-v2 Q8)",
                    conn=conn,
                )
                group_id = int(ref.id)
                lines.append(
                    f"    minted group tn{group_id} '{group_name}' under measurand"
                )
            else:
                group_id = group["id"]
            store.add_link(
                src_ref_id=plan.loser,
                dst_ref_id=group_id,
                relation="specialises",
                meta={"axis": _GROUP_AXIS},
                set_by="agent",
                conn=conn,
            )
            lines.append(f"    added specialises -> tn{group_id}")
        return lines

    # merge
    assert isinstance(plan.target, int)  # unresolved entries bailed above
    with store.tx() as conn:
        rep = store.repoint_measurand_measures(
            plan.loser, plan.target, conn=conn, actor=actor
        )
    lines.append(f"    re-pointed {rep['moved']} measure row(s)")
    from precis.graph.garden.apply import apply_merge

    rec = apply_merge(
        store,
        plan.loser,
        plan.target,
        actor=actor,
        review_id=_pending_merge_review(store, plan.loser, plan.target),
    )
    lines.append(
        f"    merged ({rec.merge_id}) — undo: precis garden undo '{rec.merge_id}'"
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="dry run unless --apply; prod via scripts/_prod-run",
    )
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument(
        "--actor",
        default=os.environ.get("USER") or "onto-repoint",
        help="actor stamped on the re-point record, links and merge (default $USER)",
    )
    args = p.parse_args(argv)

    from _common import open_store

    store, _cfg = open_store()
    try:
        for plan in PLAN:
            lines = (
                _apply_plan(store, plan, actor=args.actor)
                if args.apply
                else _plan_lines(store, plan)
            )
            for line in lines:
                print(line)
        if not args.apply:
            print("\ndry run — re-run with --apply to write")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
