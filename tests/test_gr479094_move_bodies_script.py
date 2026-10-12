"""scripts/_gr479094_move_bodies.py — guards and body move on a retired twin."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from precis.store import Store

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_gr479094_move_bodies.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_gr479094_move_bodies", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pair(
    store: Store, tag: str, *, n_body: int = 3, retire_twin: bool = True
) -> tuple[int, int]:
    stub = int(store.insert_ref(kind="paper", slug=f"stub-{tag}", title="Stub").id)
    twin = int(store.insert_ref(kind="paper", slug=f"twin-{tag}", title="Twin").id)
    with store.pool.connection() as conn:
        for i in range(n_body):
            conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, 'paragraph', %s)",
                (twin, i, f"body {tag} {i}"),
            )
        conn.commit()
    if retire_twin:
        store.stamp_ref_meta(twin, {"superseded_by": stub})
        store.retire_ref(twin)
    return stub, twin


def _count(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id=%s AND ord>=0", (ref_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_dry_run_changes_nothing(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "dry")
    out = mod.run(store, ((stub, twin),), apply=False)
    assert "dry run" in out[0]
    assert (_count(store, stub), _count(store, twin)) == (0, 3)


def test_apply_moves_body_to_stub(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "apply")
    out = mod.run(store, ((stub, twin),), apply=True)
    assert "body" in out[0]
    assert (_count(store, stub), _count(store, twin)) == (3, 0)


def test_live_twin_is_refused(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "live", retire_twin=False)
    with pytest.raises(AssertionError, match="not retired"):
        mod.run(store, ((stub, twin),), apply=True)
    assert _count(store, stub) == 0


def test_bodied_survivor_is_refused(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "bodied")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'x')",
            (stub,),
        )
        conn.commit()
    with pytest.raises(AssertionError, match="already has"):
        mod.run(store, ((stub, twin),), apply=False)


def test_retired_body_chunks_do_not_break_the_move(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "retchunk", n_body=4)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET retired_at = now() WHERE ref_id = %s AND ord = 3",
            (twin,),
        )
        conn.commit()
    mod.run(store, ((stub, twin),), apply=True)
    assert _count(store, stub) == 3


def test_twin_superseded_by_other_ref_is_refused(store: Store) -> None:
    mod = _load()
    stub, twin = _pair(store, "other")
    other = int(store.insert_ref(kind="paper", slug="other-sv", title="O").id)
    store.stamp_ref_meta(twin, {"superseded_by": other})
    with pytest.raises(AssertionError, match="superseded by"):
        mod.run(store, ((stub, twin),), apply=True)
