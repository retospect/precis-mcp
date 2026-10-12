"""Raw completion geometry reads stay coherent, complete and read-only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import numpy as np

from precis.store import Store
from precis.store._structure_ops import StructureMixin
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Bond, Scene


def scene(scale: float, frac: float) -> Scene:
    sc = Scene(cell=Cell(np.eye(3) * scale))
    sc.atoms["aC1"] = Atom(
        label="aC1",
        element="C",
        frac=np.array([frac, 0.125, 0.25]),
        fixed=3,
        magmom=0.12345678901234566,
        oxidation=1,
        charge=-1,
        hybridization="sp2",
    )
    sc.atoms["aO2"] = Atom(
        label="aO2",
        element="O",
        frac=np.array([0.5, 0.25, 0.125]),
    )
    sc.bonds.append(
        Bond(i="aC1", j="aO2", order=1.25, provenance="inferred", image=(1, 0, -1))
    )
    return sc


def save(store: Store, sc: Scene) -> Any:
    return store.structure_save(
        slug="completion-geometry-snapshot",
        title="Completion snapshot fixture",
        scene=sc,
        version=7,
        card_text="local deterministic test",
    )[0]


def test_snapshot_is_one_statement_and_same_version_graph_is_coherent(
    store: Store,
) -> None:
    ref = save(store, scene(10, 0.04))
    executions: list[str] = []

    class Cursor:
        def __init__(self, cur: Any) -> None:
            self.cur = cur

        def execute(self, sql: str, params: Any) -> None:
            self.cur.execute(sql, params)
            executions.append(sql)
            # Replace the complete graph at the same caller version after the
            # read statement's snapshot is fixed but before its result fetch.
            save(store, scene(20, 0.035))

        def fetchone(self) -> Any:
            return self.cur.fetchone()

    class Connection:
        def __init__(self, conn: Any) -> None:
            self.conn = conn

        @contextmanager
        def cursor(self, **kwargs: Any) -> Iterator[Cursor]:
            with self.conn.cursor(**kwargs) as cur:
                yield Cursor(cur)

    class ReaderPool:
        @contextmanager
        def connection(self) -> Iterator[Connection]:
            with store.pool.connection() as conn:
                yield Connection(conn)

    reader = cast(StructureMixin, SimpleNamespace(pool=ReaderPool()))
    old = StructureMixin.structure_completion_geometry_snapshot(reader, ref.id)
    new = store.structure_completion_geometry_snapshot(ref.id)

    assert len(executions) == 1
    assert "struct_atoms" in executions[0] and "struct_bonds" in executions[0]
    assert "jsonb_agg" in executions[0]
    assert old is not None and new is not None
    assert old["ref_id"] == new["ref_id"] == ref.id
    assert old["version"] == new["version"] == 7
    assert old["lattice"][0][0] == Decimal("10.0")
    assert new["lattice"][0][0] == Decimal("20.0")
    assert len(old["atoms"]) == len(new["atoms"]) == 2
    assert len(old["bonds"]) == len(new["bonds"]) == 1
    assert old["atoms"][0]["id"] != new["atoms"][0]["id"]
    assert old["bonds"][0]["id"] != new["bonds"][0]["id"]
    assert old["atoms"][0]["label"] == "aC1"
    assert old["atoms"][0]["element"] == "C"
    assert old["atoms"][0]["fixed"] == 3
    assert old["atoms"][0]["charge"] == -1
    assert old["atoms"][0]["oxidation"] == 1
    assert old["atoms"][0]["hybridization"] == "sp2"
    assert old["atoms"][0]["fa"].as_integer_ratio() == (0.04).as_integer_ratio()
    assert old["atoms"][0]["magmom"] == 0.12345678901234566
    assert old["bonds"][0]["i"] == old["atoms"][0]["id"]
    assert old["bonds"][0]["j"] == old["atoms"][1]["id"]
    assert old["bonds"][0]["bond_order"] == Decimal("1.25")
    assert old["bonds"][0]["provenance"] == "inferred"
    assert old["bonds"][0]["image"] == [1, 0, -1]


def test_snapshot_empty_missing_retired_and_wrong_kind(store: Store) -> None:
    empty = store.structure_save(
        slug="completion-geometry-empty",
        title="Empty snapshot fixture",
        scene=Scene(cell=Cell(np.eye(3) * 5)),
        version=2,
        card_text="empty",
    )[0]
    snapshot = store.structure_completion_geometry_snapshot(empty.id)
    assert snapshot is not None
    assert snapshot["atoms"] == []
    assert snapshot["bonds"] == []
    assert snapshot["pbc"] == [True, True, True]
    assert store.structure_completion_geometry_snapshot(-1) is None

    paper = store.insert_ref(
        kind="paper", slug="completion-snapshot-paper", title="Paper"
    )
    assert store.structure_completion_geometry_snapshot(paper.id) is None

    store.retire_ref(empty.id)
    assert store.structure_completion_geometry_snapshot(empty.id) is None


def test_snapshot_keeps_caller_connection_open_and_performs_no_writes(
    store: Store,
) -> None:
    ref = save(store, scene(11, 0.2))
    with store.pool.connection() as conn:
        conn.execute("BEGIN")
        before = conn.execute(
            "SELECT count(*) FROM struct_atoms "
            "WHERE ref_id = %s AND retired_version IS NULL",
            (ref.id,),
        ).fetchone()

        statements: list[str] = []

        class Cursor:
            def __init__(self, cur: Any) -> None:
                self.cur = cur

            def execute(self, sql: str, params: Any) -> None:
                statements.append(sql)
                self.cur.execute(sql, params)

            def fetchone(self) -> Any:
                return self.cur.fetchone()

        class Connection:
            def cursor(self, **kwargs: Any) -> Any:
                @contextmanager
                def cursor_context() -> Iterator[Cursor]:
                    with conn.cursor(**kwargs) as cur:
                        yield Cursor(cur)

                return cursor_context()

        snapshot = store.structure_completion_geometry_snapshot(
            ref.id, conn=cast(Any, Connection())
        )
        after = conn.execute(
            "SELECT count(*) FROM struct_atoms "
            "WHERE ref_id = %s AND retired_version IS NULL",
            (ref.id,),
        ).fetchone()
        assert snapshot is not None
        assert before is not None and after is not None
        assert before[0] == after[0] == len(snapshot["atoms"]) == 2
        assert len(statements) == 1 and statements[0].lstrip().startswith("SELECT")
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert not conn.closed
        conn.rollback()


def test_snapshot_float_binary_and_json_decimal_are_distinct(store: Store) -> None:
    from fractions import Fraction

    ref = save(store, scene(0.1, 0.1))
    with store.pool.connection() as conn:
        conn.execute("SET LOCAL extra_float_digits = -3")
        snapshot = store.structure_completion_geometry_snapshot(ref.id, conn=conn)
        assert snapshot is not None
        assert snapshot["lattice"][0][0] == Decimal("0.1")
        assert Fraction(snapshot["atoms"][0]["fa"]) == Fraction(0.1)
        assert Fraction(snapshot["atoms"][0]["fa"]) != Fraction(1, 10)
        conn.execute(
            "UPDATE refs SET meta = meta - 'lattice' - 'pbc' WHERE ref_id = %s",
            (ref.id,),
        )
        missing = store.structure_completion_geometry_snapshot(ref.id, conn=conn)
        assert missing is not None
        assert missing["lattice"] is None and missing["pbc"] is None
        conn.rollback()
