"""onto-repoint — re-point live measures, merge/retire the Q8 taxa.

Carries out Reto's ontology-v2 Q8 rulings (``docs/backlog/ontology-v2.md``,
decisions log): "Merged/retired before slice 1: duplicate bore diameter
and maximum service temperature, length→overall length, price per gram→
cost per unit mass; retired delta length, unit length, persistence
length, thermal half life, PSS short fraction."

For each entry the script either:

* ``merge`` — re-points the loser's measure rows onto the winner through
  the canonical_unit-safe path
  (:meth:`precis.store.Store.repoint_measurand_measures`, the 0234
  ``precis.allow_measurand_repoint`` door — same canonical_unit,
  dimension_kind and si_vector or it refuses), then merges through the
  garden apply/undo path (:func:`precis.graph.garden.apply.apply_merge`).
* ``retire`` — soft-deletes the taxon through the door
  ``delete(kind='taxon')`` uses (:meth:`Store.retire_ref`, plus the
  ``deleted`` audit event with the reason). The ruling names no re-point
  target for the five, so while any ``measures`` row still references
  the node the entry reports the attached-measure count and refuses —
  the same guard shape ``apply_merge`` runs. Attached rows resolve by
  hand (measures are append-only: supersede-per-row).

Dry run by default; ``--apply`` writes. Re-runnable: an entry already in
its end state is reported and skipped. The merge half needs migration
0234 applied on the target DB (without it the frozen trigger refuses);
retiring needs nothing new.

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
    is the winner ref id. ``action='retire'``: ``target`` is ``None`` —
    the five have no ruled re-point target, so the entry refuses while
    ``measures`` rows are attached."""

    loser: int
    target: int | None
    action: str  # "merge" | "retire"


#: The Q8 pairs not yet applied (see the module docstring).
PLAN: tuple[Plan, ...] = (
    Plan(460115, 460116, "merge"),  # Length -> Overall length (12 rows)
    Plan(460131, None, "retire"),  # Delta Length
    Plan(460151, None, "retire"),  # Unit Length
    Plan(460140, None, "retire"),  # Persistence Length
    Plan(460150, None, "retire"),  # Thermal Half Life
    Plan(460142, None, "retire"),  # PSS short fraction
)


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

        if plan.action == "retire":
            attached = counts["all"] + counts["stray"]
            lines = [f"{label} -> retire: {attached} attached measure row(s)"]
            if counts["all"]:
                lines.append(
                    f"    {counts['all']} on measurand_ref_id ({counts['live']} live)"
                )
            if counts["stray"]:
                lines.append(
                    f"    {counts['stray']} via subject_ref_id or an anchored link"
                )
            if attached:
                lines.append(
                    "    refuses while measures are attached — no ruled "
                    "re-point target; resolve by hand (measures are "
                    "append-only: supersede-per-row)"
                )
            else:
                lines.append(
                    "    soft-delete via store.retire_ref (the delete verb's door)"
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

    if plan.action == "retire":
        with store.tx() as conn:
            store.append_event(
                plan.loser,
                source="agent",
                event="deleted",
                payload={
                    "reason": "ontology-v2 Q8 ruling — retired (onto-repoint)",
                    "actor": actor,
                },
                conn=conn,
            )
            store.retire_ref(plan.loser, conn=conn)
        lines.append("    retired")
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
