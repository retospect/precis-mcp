"""``get``/``tag`` with ``id=[...]`` on gripe, alert and todo.

Batch ``get`` is summary-shaped (one block per id, order kept, missing
ids are a one-line block); batch ``tag`` is one transaction (one bad id
rolls every id back). Both share the ``_coerce_ids`` normaliser and its
cap; every other verb and every other kind still refuses a list with a
``BadInput`` naming the batch form, never an ``Internal``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.alerts import STATE_OPEN, STATE_RESOLVED, raise_alert
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import BATCH_ID_CAP
from precis.handlers.alert import AlertHandler
from precis.handlers.gripe import GripeHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.todo import TodoHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag
from tests.conftest import id_of


def _gripes(hub: Hub, n: int) -> tuple[GripeHandler, list[int]]:
    handler = GripeHandler(hub=hub)
    return handler, [
        id_of(handler.put(text=f"batch gripe {i}\nmore body").body) for i in range(n)
    ]


def _batch(*ids: int | str | None) -> list[str | int]:
    """The ``id=[...]`` argument, typed as the verbs declare it."""
    return [i for i in ids if i is not None]


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


# ── get ────────────────────────────────────────────────────────────


def test_get_batch_gripe_is_summary_shaped_in_order(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    handler.tag(id=ids[1], add=["STATUS:triaged", "PRIO:high"])
    handler.link(id=ids[0], target=f"gripe:{ids[2]}", rel="related-to")
    missing = max(ids) + 100_000
    body = handler.get(id=_batch(ids[2], missing, ids[0], ids[1])).body
    assert body.startswith("# 4 gripe (batch)")
    blocks = body.split("\n\n")[1:5]
    assert blocks[0].startswith(f"gr{ids[2]}  ")
    assert blocks[1] == f"gr{missing}  not found"
    assert blocks[2].startswith(f"gr{ids[0]}  ")
    assert "links=1" in blocks[2]
    assert "STATUS:triaged" in blocks[3] and "prio=" in blocks[3]
    # first line only — never the full render
    assert "batch gripe 1" in blocks[3] and "more body" not in body
    assert "## comment" not in body


def test_get_batch_marks_deleted_and_accepts_handles(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    handler.delete(id=ids[1])
    body = handler.get(id=_batch(f"gr{ids[0]}", f"gripe:{ids[1]}", str(ids[0]))).body
    assert "# 2 gripe (batch)" in body  # duplicate collapsed
    assert f"gr{ids[1]}  deleted" in body


def test_get_batch_alert_shows_state(store: Store, hub: Hub) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp-a", title="alert A\ndetail")
    b, _ = raise_alert(store, source="s", fingerprint="fp-b", title="alert B")
    body = AlertHandler(hub=hub).get(id=_batch(a, b)).body
    assert f"al{a}  {STATE_OPEN}" in body
    assert "alert B" in body and "detail" not in body


def test_get_batch_todo_skips_ancestry(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    root = handler.put(text="root todo").ref_id
    leaf = handler.put(text="leaf todo", parent_id=root).ref_id
    body = handler.get(id=_batch(leaf, root)).body
    assert body.index(f"td{leaf}") < body.index(f"td{root}")
    assert "STATUS:open" in body
    assert "ancestry" not in body.lower()


def test_get_batch_cap_empty_view_and_foreign_handle(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match=f"at most {BATCH_ID_CAP}"):
        handler.get(id=_batch(*range(1, BATCH_ID_CAP + 2)))
    with pytest.raises(BadInput, match="empty"):
        handler.get(id=[])
    with pytest.raises(BadInput, match="does not combine"):
        handler.get(id=_batch(1, 2), view="links")
    with pytest.raises(BadInput, match="todo handle, not gripe"):
        handler.get(id=_batch("td5"))
    mixed: list[Any] = [1, 2.5]
    with pytest.raises(BadInput, match=r"id\[1\] must be an integer"):
        handler.get(id=mixed)


def test_list_id_refused_cleanly_elsewhere(hub: Hub) -> None:
    gripe = GripeHandler(hub=hub)
    two: Any = [1, 2]
    with pytest.raises(BadInput, match="got list") as err:
        gripe.link(id=two, target="gripe:3", rel="related-to")
    assert "accepted only by get and tag on gripe" in str(err.value.next)
    with pytest.raises(BadInput, match="does not combine with id="):
        gripe.get(id=_batch(1), view="comments")
    with pytest.raises(BadInput, match="does not take id="):
        MemoryHandler(hub=hub).get(id=_batch(1, 2))
    with pytest.raises(BadInput, match="does not take id=|got list"):
        MemoryHandler(hub=hub).tag(id=two, add=["SPACE:repo-dev"])


def test_get_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    _handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch("get", {"kind": "gripe", "id": ids})
    assert "# 2 gripe (batch)" in body
    assert "[error" not in body


# ── tag ────────────────────────────────────────────────────────────


def test_tag_batch_gripe_applies_to_all_with_per_id_lines(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    body = handler.tag(id=_batch(*ids), add=["STATUS:triaged", "PRIO:high"]).body
    assert body.startswith("tagged 3 gripe in one transaction")
    lines = body.splitlines()[1:]
    assert [ln.split(":")[0] for ln in lines] == [f"gr{i}" for i in ids]
    for rid in ids:
        assert "STATUS:triaged" in _tags(handler.store, rid)
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert ref is not None and ref.prio is not None


def test_tag_batch_one_bad_id_rolls_back_all(hub: Hub) -> None:
    handler, ids = _gripes(hub, 3)
    missing = max(ids) + 100_000
    with pytest.raises(NotFound, match=f"gr{missing}: .*rolled back, nothing applied"):
        handler.tag(id=_batch(*ids, missing), add=["STATUS:triaged", "PRIO:high"])
    for rid in ids:
        tags = _tags(handler.store, rid)
        assert "STATUS:open" in tags and "STATUS:triaged" not in tags
        ref = handler.store.get_ref(kind="gripe", id=rid)
        assert (
            ref is not None and ref.prio is None
        )  # set_prio ran outside tx(), still undone


def test_tag_batch_todo_prio_and_open_tag(hub: Hub) -> None:
    handler = TodoHandler(hub=hub)
    ids = [rid for i in range(2) if (rid := handler.put(text=f"batch todo {i}").ref_id)]
    body = handler.tag(id=_batch(*ids), add=["context:batch"], prio=2).body
    assert "tagged 2 todo" in body
    for rid in ids:
        assert "context:batch" in _tags(handler.store, rid)
        ref = handler.store.get_ref(kind="todo", id=rid)
        assert ref is not None and ref.prio == 2


def test_tag_batch_alert_resolve_syncs_column(store: Store, hub: Hub) -> None:
    a, _ = raise_alert(store, source="s", fingerprint="fp-1", title="one")
    b, _ = raise_alert(store, source="s", fingerprint="fp-2", title="two")
    AlertHandler(hub=hub).tag(
        id=_batch(a, b), add=[STATE_RESOLVED], remove=[STATE_OPEN]
    )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT resolved_at FROM refs WHERE ref_id = ANY(%s)", ([a, b],)
        ).fetchall()
    assert len(rows) == 2 and all(r[0] is not None for r in rows)
    assert STATE_RESOLVED in _tags(store, a) and STATE_OPEN not in _tags(store, b)


def test_tag_batch_rejects_bad_tag_before_any_write(hub: Hub) -> None:
    handler, ids = _gripes(hub, 2)
    with pytest.raises(BadInput):
        handler.tag(id=_batch(*ids), add=["STATUS:bogus"])
    for rid in ids:
        assert "STATUS:open" in _tags(handler.store, rid)


def test_tag_batch_through_dispatch(
    runtime_with_store: PrecisRuntime, hub: Hub
) -> None:
    handler, ids = _gripes(hub, 2)
    body = runtime_with_store.dispatch(
        "tag", {"kind": "gripe", "id": ids, "add": ["STATUS:triaged"]}
    )
    assert "tagged 2 gripe" in body and "[error" not in body
    assert all("STATUS:triaged" in _tags(handler.store, rid) for rid in ids)


# ── store.atomic ───────────────────────────────────────────────────


def test_store_atomic_joins_self_connected_ops(store: Store) -> None:
    """Ops that open their own pooled connection run inside the scope's
    transaction: an exception after them undoes their writes."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="atomic probe", conn=conn
        )
    with pytest.raises(RuntimeError, match="boom"), store.atomic():
        store.set_prio(ref.id, 3)
        store.add_tag(ref.id, Tag.open("atomic-probe"), set_by="agent")
        raise RuntimeError("boom")
    fetched = store.get_ref(kind="memory", id=ref.id)
    assert fetched is not None and fetched.prio is None
    assert "atomic-probe" not in _tags(store, ref.id)
    with store.atomic():
        store.set_prio(ref.id, 4)
    fetched = store.get_ref(kind="memory", id=ref.id)
    assert fetched is not None and fetched.prio == 4
