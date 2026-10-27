"""Handler-level formula/footer/stats coverage (spreadsheet-tables slice 2):
put columns=/rows=/footer= split args, recompute on every write, footer=
edit, view='stats'. Pure-evaluator + escape coverage lives in
tests/test_table_formula.py."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _table_chunk(hub: Hub, slug: str) -> Any:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    order = hub.live_store.drafts.reading_order(ref.id)
    return next(c for c in order if c.chunk_kind == "table")


def _meta(hub: Hub, handle: str) -> dict:
    return hub.live_store.drafts.draft_chunk_meta(handle)


def test_put_columns_formula_and_footer(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[
            {"name": "qty"},
            {"name": "unit_price"},
            {"name": "line_cost", "formula": "qty * unit_price"},
        ],
        rows=[[2, 0.5, None], [10, 0.1, None]],
        footer={"line_cost": "sum"},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    tbl = _meta(hub, tc.handle)["table"]
    assert [r[2] for r in tbl["rows"]] == [1.0, 1.0]
    cost = tbl["columns"][2]
    assert cost["formula"].startswith(cost["id"]) or "qty" not in cost["formula"]
    assert list(tbl["footer"].values()) == ["sum"]
    assert list(tbl["footer_values"].values())[0]["value"] == 2.0


def test_cell_edit_recomputes_formula_and_footer(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[
            {"name": "qty"},
            {"name": "price"},
            {"name": "cost", "formula": "qty*price"},
        ],
        rows=[[2, 0.5, None]],
        footer={"cost": "sum"},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    tbl = _meta(hub, tc.handle)["table"]
    row_id = tbl["rowmeta"][0]["id"]
    qty_id = tbl["columns"][0]["id"]
    draft.edit(id=tc.dc, cell=f"{row_id}.{qty_id}", text="4")
    tbl = _meta(hub, tc.handle)["table"]
    assert tbl["rows"][0][2] == 2.0  # cost recomputed in the same edit
    cost_id = tbl["columns"][2]["id"]
    assert tbl["footer_values"][cost_id]["value"] == 2.0


def test_put_escape_formula_refused(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    with pytest.raises(BadInput):
        draft.put(
            id="d",
            chunk_kind="table",
            columns=[{"name": "x"}, {"name": "y", "formula": "x.__class__"}],
            rows=[[1, None]],
            at={"last": True},
        )


def test_table_edit_dropping_referenced_column_lands_with_ref(
    draft: DraftHandler, hub: Hub
) -> None:
    """Spec §4 / GRIPE_FIX: dropping a column a STORED formula names is
    not refused — the dependent cells store #REF and the reply warns
    (the formula was declared earlier; the column was dropped later)."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[{"name": "qty"}, {"name": "cost", "formula": "qty * 2"}],
        rows=[[2, None]],
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    resp = draft.edit(id=tc.dc, drop_column="qty")
    tbl = _meta(hub, tc.handle)["table"]
    assert tbl["rows"][0][0] == "#REF"
    assert "#REF" in resp.body


def test_new_formula_naming_unknown_column_refused(
    draft: DraftHandler, hub: Hub
) -> None:
    """GRIPE_FIX: a NEW or CHANGED formula naming no column refuses —
    a typo must not land silently (#REF is for a stored formula whose
    column was dropped by a later edit)."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    with pytest.raises(BadInput, match="unknown name 'qyt'"):
        draft.put(
            id="d",
            chunk_kind="table",
            columns=[{"name": "qty"}, {"name": "cost", "formula": "qyt * 2"}],
            rows=[[2, None]],
            at={"last": True},
        )
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[{"name": "qty"}, {"name": "cost", "formula": "qty * 2"}],
        rows=[[2, None]],
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    # re-declaring the formula via table= IS a new declaration — naming
    # a column the new grid doesn't have refuses the same way
    with pytest.raises(BadInput, match="unknown name"):
        draft.edit(
            id=tc.dc,
            table={
                "columns": [{"name": "cost", "formula": "qty * 2"}],
                "rows": [[None]],
            },
        )
    # a changed formula that names a typo refuses too
    with pytest.raises(BadInput, match="unknown name"):
        draft.edit(id=tc.dc, add_column={"name": "tax", "formula": "qtyy * 0.1"})


def test_formula_cycle_lands_with_cycle_cells_and_warning(
    draft: DraftHandler, hub: Hub
) -> None:
    """A dependency cycle stores #CYCLE in every column on the loop, the
    edit lands, and the ack names the columns."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    resp = draft.put(
        id="d",
        chunk_kind="table",
        columns=[
            {"name": "a", "formula": "b + 1"},
            {"name": "b", "formula": "a + 1"},
        ],
        rows=[[None, None]],
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    tbl = _meta(hub, tc.handle)["table"]
    assert tbl["rows"][0] == ["#CYCLE", "#CYCLE"]
    assert "cycle" in resp.body and "a" in resp.body and "b" in resp.body
    # the stored text carries the markers — no refusal, nothing dropped
    assert "#CYCLE" in tc.text


def test_table_replace_restating_ref_formula_names_dropped_column(
    draft: DraftHandler, hub: Hub
) -> None:
    """DECIDED: re-submitting the stored grid (a formula decayed to #REF
    after drop_column) still refuses — restated = declared — but the
    refusal says the formula names a dropped column and how to fix it,
    not a bare 'unknown name'."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[{"name": "qty"}, {"name": "cost", "formula": "qty * 2"}],
        rows=[[2, None]],
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    draft.edit(id=tc.dc, drop_column="qty")
    tbl = _meta(hub, tc.handle)["table"]
    assert tbl["rows"][0][0] == "#REF"
    with pytest.raises(BadInput, match="dropped column") as e:
        draft.edit(id=tc.dc, table=tbl)
    assert "remove" in str(e.value.next)


def test_footer_edit_sets_and_clears(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["n"], "rows": [[1], [2], [3]]},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    draft.edit(id=tc.dc, footer={"n": "mean"})
    tbl = _meta(hub, tc.handle)["table"]
    assert list(tbl["footer_values"].values())[0]["value"] == 2.0
    # footer surfaces on the read-time trailer
    body = draft.get(id=tc.dc).body
    assert "footer:" in body and "mean(n)=2.0" in body
    draft.edit(id=tc.dc, footer={})
    tbl = _meta(hub, tc.handle)["table"]
    assert "footer" not in tbl


def test_stats_view(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={
            "header": ["part", "qty"],
            "rows": [["a", 2], ["b", 10], ["c", None]],
        },
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    body = draft.get(id=tc.dc, view="stats").body
    assert "stats (3 rows)" in body
    assert "qty" in body and "mean=6" in body and "range=[2, 10]" in body
    # stats targets a chunk — a whole-draft id refuses
    with pytest.raises(BadInput, match="targets a chunk"):
        draft.get(id="d", view="stats")


def test_footer_unknown_column_refused(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    with pytest.raises(BadInput, match="no column"):
        draft.put(
            id="d",
            chunk_kind="table",
            table={"header": ["n"], "rows": [[1]]},
            footer={"nope": "sum"},
            at={"last": True},
        )


def test_non_table_footer_refused(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(id="d", chunk_kind="paragraph", text="prose", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="d")
    assert ref is not None
    para = hub.live_store.drafts.reading_order(ref.id)[-1]
    with pytest.raises(BadInput, match="chunk_kind='table'"):
        draft.edit(id=para.dc, footer={"x": "sum"})
