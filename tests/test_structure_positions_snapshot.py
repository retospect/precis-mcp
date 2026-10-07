"""Canonical DB seam: one statement keeps same-version cell/atoms coherent."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from precis.store import Store
from precis.store._structure_ops import StructureMixin
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene


def scene(scale: float, frac_z: float) -> Scene:
    sc = Scene(cell=Cell(np.eye(3) * scale))
    sc.atoms["aC1"] = Atom(label="aC1", element="C", frac=np.array([0, 0, frac_z]))
    return sc


def save(store: Store, sc: Scene, generated: dict[str, Any] | None = None) -> Any:
    return store.structure_save(
        slug="s1-snapshot",
        title="Snapshot fixture",
        scene=sc,
        version=4,
        card_text="local deterministic test",
        meta_extra={"generated": generated} if generated is not None else None,
    )[0]


def test_same_version_rewrite_after_query_before_fetch(store: Store) -> None:
    ref = save(store, scene(10, 0.04), {"receipt": "old"})
    executions = []

    class Cursor:
        def __init__(self, cur: Any) -> None:
            self.cur = cur

        def execute(self, sql: str, params: Any) -> None:
            self.cur.execute(sql, params)
            executions.append(sql)
            # SQL snapshot A has been evaluated; commit B at the SAME
            # caller version before the helper retrieves the result.
            save(store, scene(20, 0.035), {"receipt": "new"})

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

    # Exercise the real owning helper with only a per-test connection
    # adapter. No shared Store pool/writer implementation is modified.
    reader = cast(StructureMixin, SimpleNamespace(pool=ReaderPool()))
    old = StructureMixin.structure_positions_snapshot(reader, ref.id)
    new = store.structure_positions_snapshot(ref.id)
    assert len(executions) == 1
    assert old is not None and new is not None
    assert old["ref_id"] == new["ref_id"] == ref.id
    assert old["version"] == new["version"] == 4
    assert old["generated"] == {"receipt": "old"}
    assert new["generated"] == {"receipt": "new"}
    assert old["atom_ids"] != new["atom_ids"]
    assert (np.asarray(old["fractional"]) @ np.asarray(old["lattice"]))[
        0, 2
    ] == pytest.approx(0.4)
    assert (np.asarray(new["fractional"]) @ np.asarray(new["lattice"]))[
        0, 2
    ] == pytest.approx(0.7)


def test_snapshot_empty_and_missing_structure(store: Store) -> None:
    ref = save(store, Scene(cell=Cell(np.eye(3) * 10)))
    snapshot = store.structure_positions_snapshot(ref.id)
    assert snapshot is not None and snapshot["fractional"] == []
    assert store.structure_positions_snapshot(-1) is None
