"""Ontology-v2 Q8 measure re-point + merge/retire (scripts/onto-repoint).

The repoint helper (``Store.repoint_measurand_measures``) is the
canonical_unit-safe door opened by migration 0234's
``precis.allow_measurand_repoint`` flag in ``precis_measures_frozen``: it
moves ``measurand_ref_id`` — live and superseded rows alike — only onto a
live taxon with the same unit contract, and refuses everything else.
The script drives it per Q8 pair, then merges through the garden apply
path or retires. Real PG (the ``store`` fixture).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.errors import BadInput, NotFound
from precis.store import Store
from precis.store._measures_ops import MeasureSpec
from tests.workers._helpers import seed_chunk, seed_ref

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _onto_repoint as repoint_script

_SI_LEN = "1,0,0,0,0,0,0"


def _taxon(
    store: Store,
    name: str,
    *,
    parent: int | None = None,
    canonical: str | None = "m",
    dimension_kind: str | None = "si",
    si_vector: str | None = _SI_LEN,
) -> int:
    meta = {
        "name": name,
        "norm_name": name.lower(),
        "slug": name.lower().replace(" ", "-"),
        "status": "proposed",
    }
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


def _measure(
    store: Store, measurand: int, subject: int, literal: str, unit: str | None = "mm"
) -> int:
    run = store.insert_measure(
        MeasureSpec(
            measurand_ref_id=measurand,
            literal=literal,
            subject_ref_id=subject,
            reported_unit=unit,
        ),
        actor="reader",
    )
    return run.output_id


@pytest.fixture
def world(store: Store) -> dict[str, Any]:
    """A Length/Overall-length pair under a measurand root, plus a time
    taxon (the incompatible target) and a dimensionless one."""
    root = _taxon(
        store, "measurand", canonical=None, dimension_kind=None, si_vector=None
    )
    paper = seed_ref(store, title="a paper", kind="paper")
    seed_chunk(store, ref_id=paper, text="lengths")
    w = {
        "root": root,
        "paper": paper,
        "length": _taxon(store, "Length", parent=root),
        "overall": _taxon(store, "Overall length", parent=root),
        "time": _taxon(
            store, "thermal half life", canonical="s", si_vector="0,0,1,0,0,0,0"
        ),
        "free": _taxon(
            store,
            "pss short fraction",
            parent=root,
            canonical=None,
            dimension_kind="dimensionless",
            si_vector=None,
        ),
    }
    return w


def _rows(store: Store, measurand: int) -> list[int]:
    with store.pool.connection() as conn:
        return [
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM measures WHERE measurand_ref_id = %s ORDER BY id",
                (measurand,),
            ).fetchall()
        ]


def _live(store: Store, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row is not None and row[0] is None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0]) if row else {}


class TestRepointHelper:
    def test_moves_live_and_superseded_and_stamps(
        self, store: Store, world: dict
    ) -> None:
        mid = _measure(store, world["length"], world["paper"], "12")
        old = _measure(store, world["length"], world["paper"], "8")
        # Supersede `old` onto a same-measurand row: prod's legacy rows sit
        # under their 0188 re-base exactly like this.
        store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["length"],
                literal="8",
                subject_ref_id=world["paper"],
                reported_unit="mm",
                supersedes=old,
            ),
            actor="reader",
        )
        with store.tx() as conn:
            rep = store.repoint_measurand_measures(
                world["length"], world["overall"], conn=conn, actor="tester"
            )
        assert rep["moved"] == 3
        assert {mid, old} <= set(rep["ids"])
        assert _rows(store, world["length"]) == []
        assert _rows(store, world["overall"]) == sorted(rep["ids"])
        stamp = _meta(store, world["length"])["measure_repoint"]
        assert (stamp["dst"], stamp["moved"], stamp["actor"]) == (
            world["overall"],
            3,
            "tester",
        )

    def test_no_rows_still_stamps(self, store: Store, world: dict) -> None:
        with store.tx() as conn:
            rep = store.repoint_measurand_measures(
                world["length"], world["overall"], conn=conn, actor="tester"
            )
        assert rep["moved"] == 0
        assert (
            _meta(store, world["length"])["measure_repoint"]["dst"] == world["overall"]
        )

    def test_refusals(self, store: Store, world: dict) -> None:
        with store.tx() as conn:
            with pytest.raises(BadInput, match="distinct"):
                store.repoint_measurand_measures(
                    world["length"], world["length"], conn=conn, actor="t"
                )
            with pytest.raises(NotFound, match="not found"):
                store.repoint_measurand_measures(
                    world["length"], 999999999, conn=conn, actor="t"
                )
            with pytest.raises(BadInput, match="canonical_unit-safe") as ei:
                store.repoint_measurand_measures(
                    world["length"], world["time"], conn=conn, actor="t"
                )
            assert "canonical_unit" in str(ei.value)
            with pytest.raises(BadInput, match="not a taxon"):
                store.repoint_measurand_measures(
                    world["length"], world["paper"], conn=conn, actor="t"
                )
        store.retire_ref(world["overall"])
        with store.tx() as conn:
            with pytest.raises(BadInput, match="target taxon .* retired"):
                store.repoint_measurand_measures(
                    world["length"], world["overall"], conn=conn, actor="t"
                )

    def test_stray_subject_refuses(self, store: Store, world: dict) -> None:
        # A measure whose SUBJECT is the loser: not a measurand_ref_id row.
        run = store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["free"],
                literal="0.8",
                subject_ref_id=world["length"],
            ),
            actor="reader",
        )
        assert run.output_id
        with store.tx() as conn:
            with pytest.raises(BadInput, match="subject or through"):
                store.repoint_measurand_measures(
                    world["length"], world["overall"], conn=conn, actor="t"
                )

    def test_current_review_refuses(self, store: Store, world: dict) -> None:
        mid = _measure(store, world["length"], world["paper"], "12")
        store.record_target_review("measure", mid, actor="checker", verdict="approved")
        with store.tx() as conn:
            with pytest.raises(BadInput, match="review"):
                store.repoint_measurand_measures(
                    world["length"], world["overall"], conn=conn, actor="t"
                )

    def test_door_stays_shut_without_flag(self, store: Store, world: dict) -> None:
        mid = _measure(store, world["length"], world["paper"], "12")
        with store.pool.connection() as conn, conn.transaction():
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                conn.execute(
                    "UPDATE measures SET measurand_ref_id = %s WHERE id = %s",
                    (world["overall"], mid),
                )

    def test_flag_still_refuses_incompatible(self, store: Store, world: dict) -> None:
        """The unit contract lives in the trigger, not just the helper:
        a caller holding the flag still cannot re-label onto an
        incompatible measurand."""
        mid = _measure(store, world["length"], world["paper"], "12")
        with store.pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT set_config('precis.allow_measurand_repoint', 'on', true)"
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                conn.execute(
                    "UPDATE measures SET measurand_ref_id = %s WHERE id = %s",
                    (world["time"], mid),
                )

    def test_flag_refuses_other_columns(self, store: Store, world: dict) -> None:
        mid = _measure(store, world["length"], world["paper"], "12")
        with store.pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT set_config('precis.allow_measurand_repoint', 'on', true)"
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                conn.execute("UPDATE measures SET literal = '13' WHERE id = %s", (mid,))


class TestScript:
    def test_plan_table_matches_ruling(self) -> None:
        """The module PLAN pins the 2026-10-22 ruling: one merge, the five
        legacy terms retired — never re-parented under a group."""
        by_loser = {p.loser: p for p in repoint_script.PLAN}
        assert by_loser[460115] == repoint_script.Plan(460115, 460116, "merge")
        for loser in (460131, 460151, 460140, 460150, 460142):
            plan = by_loser[loser]
            assert plan.action == "retire"
            assert plan.target is None
        assert all(p.action in ("merge", "retire") for p in repoint_script.PLAN)

    def test_dry_run_reports_and_writes_nothing(
        self, store: Store, world: dict, capsys: Any
    ) -> None:
        _measure(store, world["length"], world["paper"], "12")
        _measure(store, world["free"], world["paper"], "0.8", unit=None)
        plans = (
            repoint_script.Plan(world["length"], world["overall"], "merge"),
            repoint_script.Plan(world["time"], None, "retire"),
            repoint_script.Plan(world["free"], None, "retire"),
        )
        out: list[str] = []
        for plan in plans:
            out += repoint_script._plan_lines(store, plan)
        text = "\n".join(out)
        assert "re-point 1 measure row(s)" in text
        assert "-> retire: 0 attached measure row(s)" in text
        assert "-> retire: 1 attached measure row(s)" in text
        assert "refuses while measures are attached" in text
        # Nothing written: all taxa live, measures unmoved.
        assert _rows(store, world["length"]) != []
        assert _live(store, world["length"]) and _live(store, world["overall"])
        assert _live(store, world["free"])

    def test_apply_merges_after_repoint(self, store: Store, world: dict) -> None:
        mid = _measure(store, world["length"], world["paper"], "12")
        plan = repoint_script.Plan(world["length"], world["overall"], "merge")
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        text = "\n".join(lines)
        assert "re-pointed 1 measure row(s)" in text
        assert "garden-merge:" in text
        assert _rows(store, world["length"]) == []
        assert _rows(store, world["overall"]) == [mid]
        loser = _meta(store, world["length"])
        assert loser["superseded_by"] == world["overall"]
        assert loser["garden_merge"]["winner"] == world["overall"]
        assert loser["measure_repoint"]["moved"] == 1
        assert "Length" in (_meta(store, world["overall"]).get("aliases") or [])
        assert not _live(store, world["length"])

    def test_apply_is_idempotent(self, store: Store, world: dict) -> None:
        plan = repoint_script.Plan(world["length"], world["overall"], "merge")
        repoint_script._apply_plan(store, plan, actor="tester")
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        assert any("skipping" in line for line in lines)

    def test_retire_soft_deletes_without_reparenting(
        self, store: Store, world: dict
    ) -> None:
        plan = repoint_script.Plan(world["free"], None, "retire")
        lines = repoint_script._plan_lines(store, plan)
        assert any("-> retire: 0 attached measure row(s)" in l for l in lines)
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        assert any("retired" in line for line in lines)
        assert not _live(store, world["free"])
        event = store.events_for(world["free"], event="deleted", limit=1)
        assert event and event[0].payload["actor"] == "tester"
        # Retired, NOT re-parented: the only specialises edge is the
        # original one to the measurand root.
        with store.pool.connection() as conn:
            edges = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
                "AND relation = 'specialises'",
                (world["free"],),
            ).fetchall()
        assert [int(r[0]) for r in edges] == [world["root"]]
        # Re-run: already retired — done, skipped.
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        assert any("skipping" in line for line in lines)

    def test_retire_refuses_while_measures_attached(
        self, store: Store, world: dict
    ) -> None:
        _measure(store, world["time"], world["paper"], "40", unit="s")
        plan = repoint_script.Plan(world["time"], None, "retire")
        lines = repoint_script._plan_lines(store, plan)
        text = "\n".join(lines)
        assert "-> retire: 1 attached measure row(s)" in text
        assert "refuses while measures are attached" in text
        # --apply writes nothing while the refusal stands.
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        assert any("refuses" in line for line in lines)
        assert _live(store, world["time"])
        assert _rows(store, world["time"]) != []

    def test_stray_plan_refuses_before_writes(self, store: Store, world: dict) -> None:
        store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["free"],
                literal="0.8",
                subject_ref_id=world["length"],
            ),
            actor="reader",
        )
        plan = repoint_script.Plan(world["length"], world["overall"], "merge")
        lines = repoint_script._plan_lines(store, plan)
        assert any("resolve by hand" in line for line in lines)
        lines = repoint_script._apply_plan(store, plan, actor="tester")
        assert any("resolve by hand" in line for line in lines)
        assert _live(store, world["length"])
        assert _rows(store, world["overall"]) == []
