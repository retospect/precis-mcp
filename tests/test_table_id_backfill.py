"""``scripts/_table_id_backfill.py`` — one-time mint of stable row/column
ids + ``pos`` keys on pre-schema ``chunk_kind='table'`` chunks,
spreadsheet-tables slice 1a (spec §13). Draft refs only, dry-run by
default, idempotent. Dev-DB test — the script itself never runs against
prod without Reto's go.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.store import ChunkInsert, Store
from precis.utils.table_data import is_table_id

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _table_id_backfill as backfill


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _seed_preschema_table(draft: DraftHandler, hub: Hub, slug: str) -> Any:
    """A draft table chunk stripped back to the pre-schema shape — legacy
    ``meta.table = {header, rows}`` AND plain-GFM text with no ``ids:``
    trailer, what the 68 prod tables look like before the pass. Both
    halves are stripped so a "get shows the ids" check can't pass off a
    stale ``ids:`` line left in the stored text."""
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id=slug, title="T", project=proj)
    draft.put(
        id=slug,
        chunk_kind="table",
        table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]},
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    tc = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )
    # Drop the canonical schema AND re-write the text as plain GFM —
    # simulate the pre-1a store shape on both columns.
    hub.live_store.drafts.edit_text(
        tc.handle,
        "| element | gap_eV |\n| --- | --- |\n| Si | 1.12 |\n| Ge | 0.67 |",
    )
    hub.live_store.drafts.patch_chunk_meta(
        tc.handle,
        {
            "table": {
                "header": ["element", "gap_eV"],
                "rows": [["Si", 1.12], ["Ge", 0.67]],
            }
        },
    )
    return tc


def test_backfill_dry_run_reports_but_writes_nothing(
    draft: DraftHandler, hub: Hub, store: Store
) -> None:
    tc = _seed_preschema_table(draft, hub, "d1")
    counts = backfill.backfill_table_ids(store, apply=False, report=False)
    assert counts == {"scanned": 1, "changed": 1, "errors": 0}
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert "columns" not in meta["table"]  # dry run wrote nothing


def test_backfill_apply_mints_ids_and_is_idempotent(
    draft: DraftHandler, hub: Hub, store: Store
) -> None:
    tc = _seed_preschema_table(draft, hub, "d2")
    counts = backfill.backfill_table_ids(store, apply=True, report=False)
    assert counts == {"scanned": 1, "changed": 1, "errors": 0}
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    tbl = meta["table"]
    # data untouched; schema minted
    assert tbl["header"] == ["element", "gap_eV"]
    assert tbl["rows"] == [["Si", 1.12], ["Ge", 0.67]]
    assert [c["name"] for c in tbl["columns"]] == ["element", "gap_eV"]
    assert all(is_table_id(c["id"]) for c in tbl["columns"])
    assert all(is_table_id(r["id"]) for r in tbl["rowmeta"])
    assert len({e["id"] for e in [*tbl["columns"], *tbl["rowmeta"]]}) == 4
    assert all("pos" in e for e in [*tbl["columns"], *tbl["rowmeta"]])
    # dc<N> / handle identity kept — meta-only write, not a chunk replace
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None and chunk.handle == tc.handle

    # second run: the columns guard skips it — a no-op
    counts2 = backfill.backfill_table_ids(store, apply=True, report=False)
    assert counts2 == {"scanned": 0, "changed": 0, "errors": 0}
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle) == meta


def test_backfill_skips_canonical_and_non_draft_chunks(
    draft: DraftHandler, hub: Hub, store: Store
) -> None:
    # a table already carrying columns is not a candidate
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="d3", title="T", project=proj)
    draft.put(
        id="d3",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        at={"last": True},
    )
    # a paper-ref table chunk with a stored meta.table is out of scope —
    # ingested paper tables are not migrated (spec §13)
    paper = store.insert_ref(kind="paper", slug="p-backfill", title="p")
    store.chunks.insert_chunks(
        paper.id,
        [
            ChunkInsert(
                ord=0,
                text="| a |\n| --- |\n| 1 |",
                meta={"chunk_kind": "table", "table": {"header": ["a"], "rows": [[1]]}},
            )
        ],
    )
    counts = backfill.backfill_table_ids(store, apply=True, report=False)
    assert counts == {"scanned": 0, "changed": 0, "errors": 0}
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM chunks WHERE ref_id = %s", (paper.id,)
        ).fetchone()
    assert row is not None
    paper_meta = row[0]
    assert "columns" not in paper_meta["table"]  # untouched


def test_backfill_malformed_table_counts_error_and_skips(
    draft: DraftHandler, hub: Hub, store: Store
) -> None:
    """A legacy ``meta.table`` that fails ``normalize_table`` (ragged
    rows) is reported and skipped, not forced through — the pass is a
    mint, not a repair."""
    tc = _seed_preschema_table(draft, hub, "d4")
    hub.live_store.drafts.patch_chunk_meta(
        tc.handle, {"table": {"header": ["a", "b"], "rows": [[1]]}}
    )
    counts = backfill.backfill_table_ids(store, apply=True, report=False)
    assert counts == {"scanned": 1, "changed": 0, "errors": 1}
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"] == {"header": ["a", "b"], "rows": [[1]]}


def test_backfilled_table_get_shows_meta_ids_and_cell_edit_lands(
    draft: DraftHandler, hub: Hub, store: Store
) -> None:
    """The pass is meta-only (``patch_chunk_meta``, spec §13) — it never
    re-derives ``text``. The ``ids:`` line ``get`` prints therefore comes
    from ``meta.table`` at read time, and a ``cell=`` addressed by those
    ids lands."""
    tc = _seed_preschema_table(draft, hub, "d5")
    # plain-GFM legacy text — no ids anywhere before the pass
    assert "ids: rows" not in draft.get(id=tc.dc).body
    counts = backfill.backfill_table_ids(store, apply=True, report=False)
    assert counts == {"scanned": 1, "changed": 1, "errors": 0}

    tbl = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    rows = [r["id"] for r in tbl["rowmeta"]]
    cols = [c["id"] for c in tbl["columns"]]
    # stored text untouched by the meta-only pass; the line is rendered
    # from meta at read time
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "ids:" not in chunk.text
    assert (
        f"ids: rows {' '.join(rows)}; cols {' '.join(cols)}" in draft.get(id=tc.dc).body
    )

    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="1.523")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    assert meta2["rows"][0] == ["Si", 1.523]
    assert meta2["rowmeta"][0]["id"] == rows[0]
