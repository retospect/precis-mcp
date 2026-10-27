"""TOON view, patch/replace, base= guard and structure ops for
id-addressed draft tables (spreadsheet-tables slice 1b, spec §9)."""

from __future__ import annotations

import ast
import copy
import re
import threading
import time
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.utils.table_data import normalize_table
from precis.utils.table_formula import compile_formula
from precis.utils.table_toon import (
    apply_patch,
    check_table_base,
    compile_where,
    journal_append,
    op_drop_column,
    parse_header_cell,
    table_content_hash,
    table_diff,
    table_from_toon,
    table_toon,
)


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


def _meta(hub: Hub, tc: Any) -> dict[str, Any]:
    return hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]


def _seed(draft: DraftHandler, hub: Hub) -> Any:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={
            "header": ["el", "gap", "qty"],
            "rows": [["Si", 1.12, 10], ["Ge", 0.67, 4]],
        },
        at={"last": True},
    )
    return _table_chunk(hub, "d")


def _ids(hub: Hub, tc: Any) -> tuple[list[str], list[str]]:
    t = _meta(hub, tc)
    return [r["id"] for r in t["rowmeta"]], [c["id"] for c in t["columns"]]


# ── pure: header grammar ──────────────────────────────────────────────


def test_header_cell_roundtrip_and_escapes() -> None:
    for spec in (
        {"id": "k5f9", "name": "vendor"},
        {"id": "h2r8", "name": "unit_price", "unit": "$", "from": "vendor:price@qty"},
        {"id": "t6ne", "name": "line_cost", "formula": "unit_price*qty"},
        {"id": "a1b2", "name": "size [mm]"},
        {"id": "c3d4", "name": "weird = x <y>"},
        {"name": "bare name"},
    ):
        from precis.utils.table_toon import header_cell

        parsed = parse_header_cell(header_cell(spec))
        assert parsed["id"] == spec.get("id")
        assert parsed["name"] == spec["name"]
        assert parsed.get("unit") == spec.get("unit")
        assert parsed.get("from") == spec.get("from")
        assert parsed.get("formula") == spec.get("formula")


# ── pure: emit + filters ──────────────────────────────────────────────


def _table() -> dict[str, Any]:
    return normalize_table(
        {
            "header": ["el", "gap", "qty"],
            "rows": [["Si", 1.12, 10], ["Ge", 0.67, 4], ["GaN", 3.4, 800]],
        }
    )


def test_table_toon_emit() -> None:
    t = _table()
    out = table_toon(t)
    lines = out.splitlines()
    cols = [c["id"] for c in t["columns"]]
    rows = [r["id"] for r in t["rowmeta"]]
    assert (
        lines[0]
        == "{#\t" + "\t".join(f"{c['id']}:{c['name']}" for c in t["columns"]) + "}"
    )
    assert lines[1] == f"{rows[0]}\tSi\t1.12\t10"
    assert lines[3] == f"{rows[2]}\tGaN\t3.4\t800"
    assert cols[0] in lines[0]


def test_table_toon_filters() -> None:
    t = _table()
    rows = [r["id"] for r in t["rowmeta"]]
    cols = [c["id"] for c in t["columns"]]
    out = table_toon(t, cols=["gap"], rows=[rows[0]])
    assert out.splitlines()[0] == f"#\t{cols[1]}:gap".replace("#", "{#") + "}"
    assert len(out.splitlines()) == 2
    out = table_toon(t, rows=[f"{rows[0]}..{rows[1]}"])
    assert len(out.splitlines()) == 3
    out = table_toon(t, where="qty > 5 and gap < 2")
    assert len(out.splitlines()) == 2 and out.splitlines()[1].startswith(rows[0])
    with pytest.raises(BadInput, match="no such column"):
        table_toon(t, where="bogus < 1")
    with pytest.raises(BadInput, match="can't use"):
        table_toon(t, where="open('x')")


def test_content_hash_changes_on_write_not_journal() -> None:
    t = _table()
    h = table_content_hash(t)
    assert table_content_hash({**t, "recent": [{"h": "x"}]}) == h
    t2 = normalize_table({**t, "rows": [["Si", 1.5, 10], *t["rows"][1:]]})
    assert table_content_hash(t2) != h


# ── pure: patch ───────────────────────────────────────────────────────


def test_apply_patch_update_null_empty() -> None:
    t = _table()
    rid = t["rowmeta"][0]["id"]
    doc = "{#\tel\tqty}\n" + f"{rid}\t\tnull\n"
    norm, rep = apply_patch(t, doc)
    assert rep["changed_cells"] == 1
    assert norm["rows"][0] == ["Si", 1.12, None]
    assert norm["rows"][1] == t["rows"][1]


def test_apply_patch_key_and_upsert() -> None:
    t = normalize_table(
        {
            "header": ["pn", "qty"],
            "columns": [{"name": "pn", "key": True}, {"name": "qty"}],
            "rows": [["C1", 5]],
        }
    )
    norm, rep = apply_patch(t, "{#\tqty}\nC1\t9\n", mode="update")
    assert norm["rows"][0] == ["C1", 9]
    with pytest.raises(BadInput, match="match no row id"):
        apply_patch(t, "{#\tqty}\nC9\t1\n")
    norm, rep = apply_patch(t, "{#\tqty}\nC9\t1\n", mode="upsert")
    assert rep["appended_row_ids"] and norm["rows"][-1] == ["C9", 1]
    # upsert needs the key column
    t2 = _table()
    with pytest.raises(BadInput, match="key column"):
        apply_patch(t2, "{#\tqty}\nx\t1\n", mode="upsert")


def test_apply_patch_unknown_col_and_calc_refused() -> None:
    t = _table()
    rid = t["rowmeta"][0]["id"]
    with pytest.raises(BadInput, match="matches no column"):
        apply_patch(t, "{#\tbogus}\n" + rid + "\t1\n")
    t2 = normalize_table(
        {
            "header": ["a", "total"],
            "columns": [{"name": "a"}, {"name": "total", "formula": "a*2"}],
            "rows": [[1, 2]],
        }
    )
    with pytest.raises(BadInput, match="calculated/pulled"):
        apply_patch(t2, "{#\ttotal}\n" + t2["rowmeta"][0]["id"] + "\t5\n")


def test_table_from_toon_roundtrip_and_retire() -> None:
    t = _table()
    doc = table_toon(t)
    norm = table_from_toon(t, doc)
    assert norm["rows"] == t["rows"]
    assert [c["id"] for c in norm["columns"]] == [c["id"] for c in t["columns"]]
    assert [r["id"] for r in norm["rowmeta"]] == [r["id"] for r in t["rowmeta"]]
    # dropping a row retires its id
    lines = doc.splitlines()
    norm2 = table_from_toon(t, "\n".join(lines[:-1]))
    assert len(norm2["rows"]) == 2
    assert t["rowmeta"][2]["id"] in norm2.get("retired_ids", [])


def test_toon_formula_refusal_names_real_kwargs(draft: DraftHandler, hub: Hub) -> None:
    """A toon= header can't write a formula — and the refusal's next=
    must name kwargs edit actually has (add_column= / table=), not a
    phantom columns=."""
    tc = _seed(draft, hub)
    with pytest.raises(BadInput, match="can't write a column formula") as e:
        draft.edit(id=tc.dc, toon="{#\tqty =el*2}\nr\t1\n")
    nxt = str(e.value.next)
    assert "add_column" in nxt and "table=" in nxt


# ── handler: view='toon'/'table' ──────────────────────────────────────


def test_get_view_toon(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    r = draft.get(id=tc.dc, view="toon")
    assert f"{cols[0]}:el" in r.body and f"{rows[0]}\tSi\t1.12\t10" in r.body
    assert "\nhash: " in r.body
    r2 = draft.get(id=tc.dc, view="toon", args={"cols": ["gap"], "rows": [rows[1]]})
    assert f"{cols[1]}:gap" in r2.body and rows[1] in r2.body
    assert "Si" not in r2.body
    r3 = draft.get(id=tc.dc, view="toon", args={"where": "qty > 5"})
    assert "Si" in r3.body and "Ge" not in r3.body


def test_get_view_table_filtered_and_refusals(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, _cols = _ids(hub, tc)
    r = draft.get(id=tc.dc, view="table", args={"rows": [rows[0]]})
    assert "| Si | 1.12 | 10 |" in r.body and "Ge" not in r.body
    assert "hash: " in r.body
    proj = _proj(hub)
    draft.put(id="e", title="T", project=proj)
    draft.put(id="e", chunk_kind="paragraph", text="prose", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="e")
    assert ref is not None
    para = hub.live_store.drafts.reading_order(ref.id)[-1]
    with pytest.raises(BadInput, match="chunk_kind='table'"):
        draft.get(id=para.dc, view="toon")


# ── handler: patch/toon/base/dry_run ──────────────────────────────────


def test_edit_patch_reply_and_cells(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    r = draft.edit(
        id=tc.dc,
        patch="{#\tel\tgap}\n" + f"{rows[1]}\tGe2\t0.70\n",
    )
    assert "patched table" in r.body and "2 cell(s) in 1 row(s)" in r.body
    assert "hash: " in r.body
    t = _meta(hub, tc)
    assert t["rows"][1] == ["Ge2", 0.7, 4]
    assert t["rows"][0] == ["Si", 1.12, 10]
    assert t["recent"], "concurrency journal not written"


def test_edit_patch_dry_run_writes_nothing(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, _ = _ids(hub, tc)
    before = _meta(hub, tc)
    r = draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[0]}\t99\n", dry_run=True)
    assert "dry-run" in r.body and "99" in r.body
    assert _meta(hub, tc) == before


def test_edit_toon_replace(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    doc = draft.get(id=tc.dc, view="toon").body.split("\nhash:")[0].split("\n", 1)[1]
    r = draft.edit(id=tc.dc, toon=doc)
    assert "replaced table" in r.body and "hash: " in r.body
    old = _ids(hub, tc)
    assert (
        old
        in (
            [r["id"] for r in _meta(hub, tc)["rowmeta"]],
            [c["id"] for c in _meta(hub, tc)["columns"]],
        )
        or True
    )
    t = _meta(hub, tc)
    assert [r["id"] for r in t["rowmeta"]] == old[0]
    assert [c["id"] for c in t["columns"]] == old[1]


def test_edit_base_guard(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, _ = _ids(hub, tc)
    base = draft.get(id=tc.dc, view="toon").body.rsplit("hash: ", 1)[1].strip()
    # concurrent change elsewhere: patch a different cell
    draft.edit(id=tc.dc, patch="{#\tel}\n" + f"{rows[0]}\tSi2\n")
    # non-overlapping stale-base patch lands
    r = draft.edit(
        id=tc.dc,
        patch="{#\tqty}\n" + f"{rows[1]}\t7\n",
        base=base,
    )
    assert "patched" in r.body
    # overlapping stale-base patch refuses
    base2 = draft.get(id=tc.dc, view="toon").body.rsplit("hash: ", 1)[1].strip()
    draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[1]}\t8\n")
    with pytest.raises(BadInput, match="overlaps|changed since base"):
        draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[1]}\t9\n", base=base2)


# ── handler: structure ops ────────────────────────────────────────────


def test_edit_add_drop_rename_move_column(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    r = draft.edit(id=tc.dc, add_column={"name": "note", "type": "text"})
    assert "added column note" in r.body
    t = _meta(hub, tc)
    assert [c["name"] for c in t["columns"]] == ["el", "gap", "qty", "note"]
    new_id = t["columns"][3]["id"]
    draft.edit(id=tc.dc, rename_column={"from": new_id, "to": "memo"})
    assert _meta(hub, tc)["columns"][3]["name"] == "memo"
    draft.edit(id=tc.dc, move_column=new_id, at="start")
    assert _meta(hub, tc)["columns"][0]["id"] == new_id
    draft.edit(id=tc.dc, drop_column=new_id)
    t = _meta(hub, tc)
    assert [c["name"] for c in t["columns"]] == ["el", "gap", "qty"]
    assert new_id in t["retired_ids"]
    assert all(len(r_) == 3 for r_ in t["rows"])


def test_edit_rows_insert_move_drop_sort(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, _ = _ids(hub, tc)
    r = draft.edit(id=tc.dc, rows=[["GaN", 3.4, 2]], at={"after": rows[0]})
    assert "inserted 1 row(s)" in r.body
    t = _meta(hub, tc)
    assert t["rows"][1] == ["GaN", 3.4, 2]
    new_id = t["rowmeta"][1]["id"]
    draft.edit(id=tc.dc, move_row=new_id, at="end")
    assert _meta(hub, tc)["rows"][2] == ["GaN", 3.4, 2]
    draft.edit(id=tc.dc, sort="gap desc")
    assert _meta(hub, tc)["rows"][0] == ["GaN", 3.4, 2]
    draft.edit(id=tc.dc, drop_rows=[new_id])
    t = _meta(hub, tc)
    assert len(t["rows"]) == 2 and new_id in t["retired_ids"]
    with pytest.raises(BadInput, match="no row id"):
        draft.edit(id=tc.dc, drop_rows=[new_id])  # retired stays gone


def test_op_drop_column_prunes_its_footer() -> None:
    """op_drop_column drops the gone column's footer spec — an aggregate
    over a dead column can't recompute (normalize used to refuse the
    drop outright), while footers on surviving columns stay."""
    t = normalize_table(
        {
            "columns": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "rows": [[1, 2, 3]],
            "footer": {"b": "sum", "c": "mean"},
        }
    )
    t = op_drop_column(t, "b")
    assert t["footer"] == {t["columns"][1]["id"]: "mean"}
    t = op_drop_column(t, "c")
    assert "footer" not in t


def test_edit_drop_column_reports_dropped_footer(draft: DraftHandler, hub: Hub) -> None:
    """Handler level: the drop lands and the ack says the column's
    footer aggregate went with it (not silently lost)."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        columns=[{"name": "a"}, {"name": "b"}],
        rows=[[1, 2]],
        footer={"b": "sum"},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    r = draft.edit(id=tc.dc, drop_column="b")
    t = _meta(hub, tc)
    assert [c["name"] for c in t["columns"]] == ["a"]
    assert "footer" not in t and "footer_values" not in t
    assert "footer sum() went with it" in r.body


def test_edit_table_selector_conflicts_and_non_table(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    with pytest.raises(BadInput, match="conflicting table edit selectors"):
        draft.edit(
            id=tc.dc, table={"header": ["x"], "rows": [[1]]}, patch="{#\tx}\nr\t1"
        )
    proj = _proj(hub)
    draft.put(id="e", title="T", project=proj)
    draft.put(id="e", chunk_kind="paragraph", text="prose", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="e")
    assert ref is not None
    para = hub.live_store.drafts.reading_order(ref.id)[-1]
    with pytest.raises(BadInput, match="only to a chunk_kind='table'"):
        draft.edit(id=para.dc, patch="{#\tx}\nr\t1")


# ── base= guard: overlap rules (1b rework) ───────────────────────────


def _hash(draft: DraftHandler, dc: str) -> str:
    return draft.get(id=dc, view="toon").body.rsplit("hash: ", 1)[1].strip()


def test_base_toon_refuses_after_concurrent_insert(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    base = _hash(draft, tc.dc)
    doc = table_toon(_meta(hub, tc))
    draft.edit(id=tc.dc, rows=[["Sn", 0.0, 1]])  # concurrent insert
    with pytest.raises(BadInput, match="changed since base"):
        draft.edit(id=tc.dc, toon=doc, base=base)
    assert len(_meta(hub, tc)["rows"]) == 3  # the insert survived


def test_base_toon_refuses_after_concurrent_add_column(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    base = _hash(draft, tc.dc)
    doc = table_toon(_meta(hub, tc))
    draft.edit(id=tc.dc, add_column={"name": "note", "type": "text"})
    with pytest.raises(BadInput, match="changed since base"):
        draft.edit(id=tc.dc, toon=doc, base=base)
    assert "note" in [c["name"] for c in _meta(hub, tc)["columns"]]


def test_base_toon_refuses_after_concurrent_rename(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    base = _hash(draft, tc.dc)
    doc = table_toon(_meta(hub, tc))
    draft.edit(id=tc.dc, rename_column={"from": cols[0], "to": "element"})
    with pytest.raises(BadInput, match="changed since base"):
        draft.edit(id=tc.dc, toon=doc, base=base)
    assert _meta(hub, tc)["columns"][0]["name"] == "element"


def test_base_drop_column_refuses_after_concurrent_cell_patch(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    base = _hash(draft, tc.dc)
    draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[0]}\t99\n")
    with pytest.raises(BadInput, match="overlaps"):
        draft.edit(id=tc.dc, drop_column=cols[2], base=base)
    # a stale drop of an untouched column still lands
    r = draft.edit(id=tc.dc, drop_column=cols[1], base=base)
    assert "dropped column" in r.body


def test_base_drop_rows_refuses_after_concurrent_cell_patch(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    rows, _cols = _ids(hub, tc)
    base = _hash(draft, tc.dc)
    draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[0]}\t99\n")
    with pytest.raises(BadInput, match="overlaps"):
        draft.edit(id=tc.dc, drop_rows=[rows[0]], base=base)
    # a stale drop of an untouched row still lands
    r = draft.edit(id=tc.dc, drop_rows=[rows[1]], base=base)
    assert "dropped 1 row(s)" in r.body


def test_base_rename_refuses_after_concurrent_rename(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed(draft, hub)
    _rows, cols = _ids(hub, tc)
    base = _hash(draft, tc.dc)
    draft.edit(id=tc.dc, rename_column={"from": cols[0], "to": "element"})
    with pytest.raises(BadInput, match="overlaps"):
        draft.edit(id=tc.dc, rename_column={"from": cols[0], "to": "elem"}, base=base)
    assert _meta(hub, tc)["columns"][0]["name"] == "element"


# ── CAS: read→check→write is atomic ──────────────────────────────────


def test_edit_table_cas_refuses_stale_snapshot(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = _seed(draft, hub)
    rows, _cols = _ids(hub, tc)
    orig = draft.store.drafts.draft_chunk_meta
    fired = {"hit": False}

    def racing_read(handle: str) -> dict[str, Any]:
        meta = orig(handle)
        if not fired["hit"]:
            fired["hit"] = True
            # a concurrent writer lands between this edit's read and write
            draft.edit(id=tc.dc, patch="{#\tel}\n" + f"{rows[0]}\tSiX\n")
        return meta

    monkeypatch.setattr(draft.store.drafts, "draft_chunk_meta", racing_read)
    with pytest.raises(BadInput, match="clobber|changed since"):
        draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[0]}\t1\n")
    t = _meta(hub, tc)
    assert t["rows"][0][0] == "SiX"  # the winning write survived
    assert t["rows"][0][2] == 10  # the losing write did NOT land


def test_edit_table_cas_blocks_on_row_lock(draft: DraftHandler, hub: Hub) -> None:
    """A real two-connection interleaving — the sequential monkeypatch
    above passes even without the ``FOR UPDATE``. Connection A holds the
    chunk row lock; a second thread's edit blocks inside the CAS read;
    A changes ``meta.table`` and commits; the unblocked write sees the
    new baseline and must refuse rather than overwrite."""
    tc = _seed(draft, hub)
    rows, _cols = _ids(hub, tc)
    errors: list[BaseException] = []

    def loser() -> None:
        try:
            draft.edit(id=tc.dc, patch="{#\tqty}\n" + f"{rows[0]}\t1\n")
        except Exception as e:
            errors.append(e)

    with hub.live_store.pool.connection() as aconn:
        aconn.execute(
            "SELECT meta FROM chunks WHERE handle = %s FOR UPDATE",
            (tc.handle,),
        ).fetchone()
        t = threading.Thread(target=loser)
        t.start()
        # wait until the writer is actually blocked on the row lock —
        # an ungranted pg_locks entry, not a guess at timing
        n = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lock_row = aconn.execute(
                "SELECT count(*) FROM pg_locks WHERE NOT granted"
            ).fetchone()
            assert lock_row is not None
            n = lock_row[0]
            if n:
                break
            time.sleep(0.05)
        assert n, "writer never reached the FOR UPDATE"
        concurrent = copy.deepcopy(_meta(hub, tc))
        concurrent["rows"][0][0] = "SiX"
        aconn.execute(
            "UPDATE chunks SET meta = meta || %s::jsonb WHERE handle = %s",
            (Jsonb({"table": concurrent}), tc.handle),
        )
        aconn.commit()
        t.join(timeout=15)
    assert not t.is_alive(), "writer stayed blocked after the lock released"
    assert len(errors) == 1 and isinstance(errors[0], BadInput)
    t2 = _meta(hub, tc)
    assert t2["rows"][0][0] == "SiX"  # the winning write survived
    assert t2["rows"][0][2] == 10  # the losing write did NOT land


# ── where= caps ──────────────────────────────────────────────────────


def test_where_caps_and_parser_crashes() -> None:
    t = _table()
    # length cap — the review's MemoryError repro never reaches the parser
    with pytest.raises(BadInput, match="cap"):
        compile_where("not " * 100000 + "el", t["columns"])
    # AST-node cap — ~403 nodes in 802 chars
    with pytest.raises(BadInput, match="complex|nodes"):
        compile_where("not " * 200 + "el", t["columns"])
    with pytest.raises(BadInput, match="valid expression|complex"):
        compile_where("-" * 999, t["columns"])
    # a sane expression still compiles
    pred = compile_where("qty > 5 and not el == 'Ge'", t["columns"])
    assert pred(["Si", 1.12, 10]) and not pred(["Ge", 0.67, 4])


# ── lossless round-trip + control chars ──────────────────────────────


def test_table_from_toon_lossless_roundtrip() -> None:
    t = normalize_table(
        {
            "header": ["el", "code", "note", "qty"],
            "rows": [
                ["Si", "0603", "null", 10],
                ["Ge", None, "", 4],
                ["GaN", "0010", "true", None],
            ],
        }
    )
    rt = table_from_toon(t, table_toon(t))
    assert rt == t
    assert table_content_hash(rt) == table_content_hash(t)


def test_toon_refuses_control_chars() -> None:
    t = _table()
    rid = t["rowmeta"][0]["id"]
    # %-decoded NUL in a header cell (review repro: raw DataError)
    with pytest.raises(BadInput, match="control character"):
        table_from_toon(t, "{#\tbad%00name\tgap\tqty}\n" + f"{rid}\ta\t1\t2\n")
    # literal control char in a data cell
    with pytest.raises(BadInput, match="control character"):
        table_from_toon(t, "{#\tel\tgap\tqty}\n" + f"{rid}\ta\x01b\t1\t2\n")
    with pytest.raises(BadInput, match="control character"):
        apply_patch(t, "{#\tel}\n" + f"{rid}\ta\x00b\n")
    # tab/newline/CR in a cell are legit content and round-trip
    t2 = normalize_table(
        {"header": ["a"], "rows": [["x\ty"], ["line1\nline2"], ["c\rd"]]}
    )
    assert table_from_toon(t2, table_toon(t2))["rows"] == t2["rows"]


def test_toon_header_formula_refused() -> None:
    t = normalize_table(
        {
            "header": ["a", "total"],
            "columns": [{"name": "a"}, {"name": "total", "formula": "a*2"}],
            "rows": [[1, 2]],
        }
    )
    cid_a, cid_total = (c["id"] for c in t["columns"])
    rid = t["rowmeta"][0]["id"]
    # a real write stores the id form (evaluate_table compiles the
    # caller's name form); the toon view renders the name form back
    stored = compile_formula("a*2", t["columns"])
    t["columns"][1]["formula"] = stored
    # re-declaring the emitted ``=a * 2`` compiles to the stored id
    # form — a semantic echo, kept, so read-then-replace works on a
    # formula table
    norm = table_from_toon(t, table_toon(t))
    assert norm["columns"][1].get("formula") == stored
    # a changed formula (``=a*3`` ≠ stored) or a new formula on a
    # non-formula column refuses — toon= is not the formula channel
    with pytest.raises(BadInput, match="formula"):
        table_from_toon(
            t, "{#\ta\t" + f"{cid_total}:total =a*3" + "}\n" + f"{rid}\t1\t6\n"
        )
    with pytest.raises(BadInput, match="formula"):
        table_from_toon(t, "{#\ta\t" + f"{cid_a}:a =a*2" + "}\n" + f"{rid}\t1\t6\n")
    # omitting the formula column keeps it (preserved, not refused)
    norm = table_from_toon(t, "{#\ta}\n" + f"{rid}\t5\n")
    assert [c["name"] for c in norm["columns"]] == ["a", "total"]
    assert norm["rows"][0] == [5, 2]


def test_edit_refuses_control_chars_on_json_paths(
    draft: DraftHandler, hub: Hub
) -> None:
    """The same refusal as the TOON gate, on every JSON write path —
    ``rows=``/``table=``/``cell=``/``add_column=``/find all funnel
    through ``normalize_table``, which scans cells, the header and the
    column spec's string fields (an unscanned NUL hits Postgres as a
    raw DataError)."""
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, rows=[["a\x00b", 1, 2]])
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, table={"header": ["x"], "rows": [["a\x00b"]]})
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, table={"header": ["a\x00b"], "rows": [["x"]]})
    with pytest.raises(BadInput, match="control character"):
        draft.edit(
            id=tc.dc,
            table={
                "columns": [{"name": "x", "unit": "a\x02b"}],
                "rows": [["v"]],
            },
        )
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="a\x00b")
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, add_column={"name": "a\x01b"})
    with pytest.raises(BadInput, match="control character"):
        draft.edit(id=tc.dc, find="Si", text="a\x00b")
    # tab/newline/CR are legit cell content and still land
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="a\tb\nc\rd")
    assert _meta(hub, tc)["rows"][0][0] == "a\tb\nc\rd"


def test_edit_stored_ctrl_cell_names_the_cell_fix(
    draft: DraftHandler, hub: Hub
) -> None:
    """A control char that predates the write gate isn't in the incoming
    write to strip — the refusal must say the stored value predates the
    gate and name the exact ``cell=`` repair in ``next=``; running it
    then lets the refused op land."""
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    # slip a pre-gate ESC into one stored cell, behind the gate's back
    meta = _meta(hub, tc)
    meta["rows"][0][0] = "a\x1bb"
    with hub.live_store.pool.connection() as aconn:
        aconn.execute(
            "UPDATE chunks SET meta = meta || %s::jsonb WHERE handle = %s",
            (Jsonb({"table": meta}), tc.handle),
        )
        aconn.commit()
    # a clean append still refuses — the stored cell trips the gate —
    # and next= names the exact repair call
    with pytest.raises(BadInput, match="predates the write gate") as e:
        draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert e.value.next == (
        f"edit(kind='draft', id={tc.dc!r}, cell={f'{rows[0]}.{cols[0]}'!r}, text='ab')"
    )
    # the named repair lands; the append it unblocked then lands too
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="ab")
    assert _meta(hub, tc)["rows"][0][0] == "ab"
    r = draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert "inserted 1 row(s)" in r.body
    assert _meta(hub, tc)["rows"][-1] == ["GaN", 3.4, 800]


def _store_meta(hub: Hub, tc: Any, meta: dict[str, Any]) -> None:
    """Write ``meta`` straight to the chunk row — behind the write
    gate's back, the only way a pre-gate stored cell can exist."""
    with hub.live_store.pool.connection() as aconn:
        aconn.execute(
            "UPDATE chunks SET meta = meta || %s::jsonb WHERE handle = %s",
            (Jsonb({"table": meta}), tc.handle),
        )
        aconn.commit()


def _run_cell_repair(draft: DraftHandler, call: str) -> None:
    """Execute the ``cell=`` repair a stored-ctrl refusal's ``next=``
    names, parsing its id=/cell=/text= literals back out."""
    m = re.fullmatch(r"edit\(kind='draft', id=(.+), cell=(.+), text=(.+)\)", call)
    assert m, call
    draft.edit(
        id=ast.literal_eval(m.group(1)),
        cell=ast.literal_eval(m.group(2)),
        text=ast.literal_eval(m.group(3)),
    )


def test_edit_stored_ctrl_cells_lists_every_offender(
    draft: DraftHandler, hub: Hub
) -> None:
    """Two pre-gate cells: the refusal lists every offender and ``next=``
    names one ``cell=`` repair per cell. On the ``cell=`` path the gate
    ignores stored control characters outside the addressed cell — the
    repair lands and the reply carries a note naming each remaining
    offender with its own repair call."""
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    meta = _meta(hub, tc)
    meta["rows"][0][0] = "a\x1bb"
    meta["rows"][1][2] = "c\x07d"
    _store_meta(hub, tc, meta)
    with pytest.raises(BadInput, match="predate the write gate") as e:
        draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert f"{rows[0]}.{cols[0]}" in e.value.cause
    assert f"{rows[1]}.{cols[2]}" in e.value.cause
    assert e.value.next == [
        f"edit(kind='draft', id={tc.dc!r}, cell='{rows[0]}.{cols[0]}', text='ab')",
        f"edit(kind='draft', id={tc.dc!r}, cell='{rows[1]}.{cols[2]}', text='cd')",
    ]
    # the first named repair LANDS — the remaining stored offender is
    # not the caller's input — and the reply notes it with its repair
    r = draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="ab")
    assert "note:" in r.body
    assert f"{rows[1]}.{cols[2]}" in r.body
    assert f"{rows[0]}.{cols[0]}" not in r.body
    assert _meta(hub, tc)["rows"][0][0] == "ab"
    # the second repair lands with no note — nothing left to name
    r = draft.edit(id=tc.dc, cell=f"{rows[1]}.{cols[2]}", text="cd")
    assert "note:" not in r.body
    r = draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert "inserted 1 row(s)" in r.body
    assert _meta(hub, tc)["rows"][-1] == ["GaN", 3.4, 800]


def test_edit_stored_ctrl_cell_message_caps_value(
    draft: DraftHandler, hub: Hub
) -> None:
    """The refusal quotes the stored value capped at 80 chars + ellipsis
    — a 5000-char pre-gate cell can't flood the wire — while ``next=``
    still carries the full cleaned value so the named repair lands."""
    tc = _seed(draft, hub)
    rows, cols = _ids(hub, tc)
    meta = _meta(hub, tc)
    meta["rows"][0][0] = "x" * 4999 + "\x1b"
    _store_meta(hub, tc, meta)
    with pytest.raises(BadInput, match="predates the write gate") as e:
        draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert len(e.value.cause) < 400
    assert "…" in e.value.cause
    assert isinstance(e.value.next, str)
    _run_cell_repair(draft, e.value.next)
    assert _meta(hub, tc)["rows"][0][0] == "x" * 4999
    r = draft.edit(id=tc.dc, rows=[["GaN", 3.4, 800]])
    assert "inserted 1 row(s)" in r.body


# ── should-fix batch ─────────────────────────────────────────────────


def test_edit_drop_rows_counts_range(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed(draft, hub)
    rows, _cols = _ids(hub, tc)
    r = draft.edit(id=tc.dc, drop_rows=[f"{rows[0]}..{rows[1]}"])
    assert "dropped 2 row(s)" in r.body
    assert _meta(hub, tc)["rows"] == []


def test_apply_patch_upsert_dup_key_refused() -> None:
    t = normalize_table(
        {
            "header": ["pn", "qty"],
            "columns": [{"name": "pn", "key": True}, {"name": "qty"}],
            "rows": [["C1", 5]],
        }
    )
    with pytest.raises(BadInput, match="twice"):
        apply_patch(t, "{#\tqty}\nC9\t1\nC9\t2\n", mode="upsert")
    norm, rep = apply_patch(t, "{#\tqty}\nC9\t1\nC8\t2\n", mode="upsert")
    assert len(rep["appended_row_ids"]) == 2
    assert norm["rows"][-2] == ["C9", 1] and norm["rows"][-1] == ["C8", 2]


def test_apply_patch_refuses_cells_past_header() -> None:
    t = _table()
    rid = t["rowmeta"][0]["id"]
    with pytest.raises(BadInput, match="past the header|extra cells"):
        apply_patch(t, "{#\tel}\n" + f"{rid}\tx\tEXTRA\n")
    # a short row is fine — missing cells stay unchanged
    norm, _rep = apply_patch(t, "{#\tel\tgap}\n" + f"{rid}\tSi2\n")
    assert norm["rows"][0] == ["Si2", 1.12, 10]


def test_journal_caps_cells_to_coarse_marker() -> None:
    t = normalize_table(
        {
            "header": ["a", "b", "c"],
            "rows": [[i, i, i] for i in range(150)],
        }
    )
    changed = [[i + 1, i + 1, i] for i in range(150)]  # 300 cells in cols a,b
    t2 = normalize_table(
        {
            **t,
            "rows": changed,
            "columns": [dict(c) for c in t["columns"]],
            "rowmeta": [dict(r) for r in t["rowmeta"]],
        }
    )
    diff = table_diff(t, t2)
    assert len(diff["cells"]) == 300
    recent = journal_append(t, diff, "h1")
    cells = recent[-1]["cells"]
    assert cells[0] == "*all"
    assert set(cells[1:]) == {t["columns"][0]["id"], t["columns"][1]["id"]}

    cur = {**t2, "recent": recent}
    base = recent[-1]["p"]
    rid0 = t["rowmeta"][0]["id"]
    ca, _cb, cc = (c["id"] for c in t["columns"])
    # a stale cell write in a coarse-touched column refuses
    with pytest.raises(BadInput, match="overlaps"):
        check_table_base(
            cur, base, {"cells": [f"{rid0}.{ca}"], "rows": [], "cols": []}, dc="dc1"
        )
    # a stale row drop refuses — the coarse entry touched cells on it
    with pytest.raises(BadInput, match="overlaps"):
        check_table_base(
            cur, base, {"cells": [], "rows": [f"-{rid0}"], "cols": []}, dc="dc1"
        )
    # a stale drop of a coarse-listed column refuses; an untouched one lands
    with pytest.raises(BadInput, match="overlaps"):
        check_table_base(
            cur, base, {"cells": [], "rows": [], "cols": [f"-{ca}"]}, dc="dc1"
        )
    check_table_base(cur, base, {"cells": [], "rows": [], "cols": [f"-{cc}"]}, dc="dc1")
