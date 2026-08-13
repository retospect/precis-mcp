"""Data/table chunks (build step 1) — canonical ``meta.table``
JSON + derived markdown ``text``, inert ``meta.regen``. No execution."""

from __future__ import annotations

from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.export import latex
from precis.handlers.draft import DraftHandler
from precis.utils.table_data import (
    col_letters_to_index,
    find_replace_cells,
    index_to_col_letters,
    infer_scalar,
    normalize_table,
    parse_cell_address,
    parse_markdown_table,
    set_cell,
    table_payload,
    table_to_markdown,
)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _table_chunk(hub: Hub, slug: str) -> Any:
    """The draft's table chunk — carries both ``.dc`` (the agent-facing
    Universal handles ``dc<id>`` address) and ``.handle`` (the legacy base-58 anchor
    the low-level store ops still key on)."""
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    order = hub.live_store.drafts.reading_order(ref.id)
    return next(c for c in order if c.chunk_kind == "table")


# ── pure util ─────────────────────────────────────────────────────────


def test_normalize_rejects_ragged_and_nonscalar() -> None:
    with pytest.raises(BadInput, match="non-empty list"):
        normalize_table({"rows": [[1]]})
    with pytest.raises(BadInput, match="align to header|has 1 cells"):
        normalize_table({"header": ["a", "b"], "rows": [[1]]})
    with pytest.raises(BadInput, match="JSON scalar"):
        normalize_table({"header": ["a"], "rows": [[{"x": 1}]]})
    # numbers stay numbers (numerics-indexable), header coerced to str
    norm = normalize_table({"header": [1, "gap"], "rows": [["Si", 1.12]]})
    assert norm == {"header": ["1", "gap"], "rows": [["Si", 1.12]]}


def test_normalize_table_string_channel_backslash_survives_single() -> None:
    """table= accepted as a top-level JSON string (item 2b, gr178512) — the
    same reliable string channel caption= already has. A single logical
    backslash in the JSON payload (doubled to ``\\\\`` per JSON escaping)
    decodes to exactly one backslash in the stored cell — the round-trip
    the nested-dict MCP wire path corrupts client-side."""
    norm = normalize_table('{"header": ["x"], "rows": [["$\\\\sim$3 aJ"]]}')
    assert norm["rows"][0][0] == "$\\sim$3 aJ"
    assert norm["rows"][0][0].count("\\") == 1  # single backslash, not doubled


def test_normalize_table_rejects_malformed_json_string() -> None:
    with pytest.raises(BadInput, match="not valid JSON"):
        normalize_table("{not json")


def test_normalize_table_dict_form_never_doubles_backslash() -> None:
    # A real Python dict (not the JSON-string channel) with a single
    # backslash cell comes back UNCHANGED — proves the server path never
    # touches backslashes; the doubling gr178512 reports is purely the MCP
    # client transport serializing a nested dict arg, not this function.
    norm = normalize_table({"header": ["x"], "rows": [["$\\sim$3 aJ"]]})
    assert norm["rows"][0][0] == "$\\sim$3 aJ"
    assert norm["rows"][0][0].count("\\") == 1


def test_markdown_is_single_block_and_escapes() -> None:
    md = table_to_markdown(
        {"header": ["a|b", "n"], "rows": [["x\ny", 2], [None, True]]},
        caption="Cap",
    )
    assert "\n\n" not in md  # one block — survives the add_chunks splitter
    assert md.startswith("**Cap**\n")
    assert r"a\|b" in md and "x<br>y" in md
    assert "| --- | --- |" in md
    assert md.strip().endswith("|  | true |")  # None→"", True→"true"


# ── render-side recovery (table_payload) ──────────────────────────────


def test_payload_prefers_canonical_meta() -> None:
    # cells stringified (None→"", numbers→str, bools→true/false); caption kept
    payload = table_payload(
        {
            "table": {"header": ["el", "gap"], "rows": [["Si", 1.12], [None, True]]},
            "caption": "Band gaps",
        },
        "ignored derived text",
    )
    assert payload == {
        "header": ["el", "gap"],
        "rows": [["Si", "1.12"], ["", "true"]],
        "caption": "Band gaps",
    }


def test_payload_falls_back_to_markdown_roundtrip() -> None:
    # No meta.table → parse the derived GFM text back to structure.
    md = table_to_markdown(
        {"header": ["a|b", "n"], "rows": [["x\ny", 2]]}, caption="Cap"
    )
    payload = table_payload({}, md)
    assert payload == {
        "header": ["a|b", "n"],  # \| unescaped
        "rows": [["x\ny", "2"]],  # <br> → newline
        "caption": "Cap",
    }


def test_payload_none_when_not_a_table() -> None:
    assert table_payload({}, "just prose, no pipes") is None
    assert parse_markdown_table("| a | b |") is None  # header but no separator


# ── put ───────────────────────────────────────────────────────────────


def test_put_table_derives_markdown_and_stores_canonical(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    r = draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]},
        caption="Band gaps",
        regen={"source": "dft", "cmd": "vasp relax"},
        at={"last": True},
    )
    assert "added table dc" in r.body and "2 rows × 2 cols" in r.body
    tc = _table_chunk(hub, "d")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)  # dc<id> resolves
    assert chunk is not None
    # text is the derived markdown (caption + table), one block
    assert chunk.text.startswith("**Band gaps**\n| element | gap_eV |")
    assert "| Si | 1.12 |" in chunk.text and "\n\n" not in chunk.text
    # canonical data + provenance live in meta, numbers preserved
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"] == [["Si", 1.12], ["Ge", 0.67]]
    assert meta["regen"] == {"source": "dft", "cmd": "vasp relax"}
    assert meta["caption"] == "Band gaps"


def test_put_table_requires_data(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    with pytest.raises(BadInput, match="requires table="):
        draft.put(id="d", chunk_kind="table", at={"last": True})


# ── edit ──────────────────────────────────────────────────────────────


def test_edit_table_rederives_and_rejects_text(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        caption="C",
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")

    # text= is rejected — the markdown is derived, not hand-edited
    with pytest.raises(BadInput, match="derived from its data"):
        draft.edit(id=tc.dc, text="| hand | edited |")

    # new data re-derives the markdown; caption persists from meta
    draft.edit(id=tc.dc, table={"header": ["x"], "rows": [[1], [2], [3]]})
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert chunk.text.startswith("**C**\n")  # caption preserved
    assert "| 3 |" in chunk.text
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]["rows"] == [
        [1],
        [2],
        [3],
    ]

    # regen-only edit keeps the data, restamps provenance
    draft.edit(id=tc.dc, regen={"source": "manual"})
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle)["regen"] == {
        "source": "manual"
    }
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]["rows"] == [
        [1],
        [2],
        [3],
    ]


def test_edit_table_clears_caption_from_data_and_markdown(
    draft: DraftHandler, hub: Hub
) -> None:
    # An explicit empty caption clears the legend from BOTH meta and the
    # derived markdown — no stranded ``**…**`` lead line (one-source, no drift).
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        caption="Legend",
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    seeded = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert seeded is not None
    assert seeded.text.startswith("**Legend**\n")

    draft.edit(id=tc.dc, table={"header": ["x"], "rows": [[1]]}, caption="")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert not chunk.text.startswith("**")  # legend line dropped
    assert chunk.text.startswith("| x |")
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle)["caption"] == ""


def test_edit_table_on_non_table_chunk_errors(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="d")
    assert ref is not None
    draft.put(id="d", chunk_kind="paragraph", text="prose", at={"last": True})
    para = hub.live_store.drafts.reading_order(ref.id)[-1]  # the paragraph just added
    with pytest.raises(BadInput, match="only to a chunk_kind='table'"):
        draft.edit(id=para.dc, table={"header": ["x"], "rows": [[1]]})


# ── field-level table editing (docs/backlog/draft-table-editing.md #1) ──
# ── pure helpers ─────────────────────────────────────────────────────────


def test_infer_scalar_excel_style() -> None:
    assert infer_scalar("42") == 42 and isinstance(infer_scalar("42"), int)
    assert infer_scalar("1.523") == 1.523 and isinstance(infer_scalar("1.523"), float)
    assert infer_scalar("true") is True
    assert infer_scalar("FALSE") is False
    assert infer_scalar("Si") == "Si"
    assert infer_scalar("") == ""  # empty string stays a string, not None


def test_infer_scalar_non_finite_stays_string() -> None:
    # float('nan'/'inf'/...) parses fine in Python but is not valid RFC-8259
    # JSON (jsonb rejects it) and 'NaN' is a legit "not measured" placeholder
    # — both must stay the original string, never a non-finite float.
    for raw in ("NaN", "nan", "inf", "-inf", "Infinity", "-Infinity"):
        v = infer_scalar(raw)
        assert v == raw and isinstance(v, str), f"{raw!r} -> {v!r}"
    # a normal finite float still infers correctly
    assert infer_scalar("1.5") == 1.5 and isinstance(infer_scalar("1.5"), float)


def test_col_letter_roundtrip() -> None:
    assert col_letters_to_index("A") == 0
    assert col_letters_to_index("Z") == 25
    assert col_letters_to_index("AA") == 26
    for i in (0, 1, 25, 26, 27, 51, 52, 701, 702):
        assert col_letters_to_index(index_to_col_letters(i)) == i


def test_parse_cell_address_a1_and_dict() -> None:
    # row 1 = header; data rows are 2..1+n_rows
    assert parse_cell_address("B2", n_rows=3, n_cols=3) == (2, 1)
    assert parse_cell_address("A1", n_rows=3, n_cols=3) == (1, 0)
    assert parse_cell_address({"row": 2, "col": 2}, n_rows=3, n_cols=3) == (2, 1)


def test_parse_cell_address_out_of_range_and_malformed() -> None:
    with pytest.raises(BadInput, match="out of range"):
        parse_cell_address("D2", n_rows=2, n_cols=2)  # only 2 cols (A, B)
    with pytest.raises(BadInput, match="out of range"):
        parse_cell_address("A9", n_rows=2, n_cols=2)  # only 1(header)+2 rows
    with pytest.raises(BadInput, match="not valid A1 notation"):
        parse_cell_address("2B", n_rows=2, n_cols=2)
    with pytest.raises(BadInput, match="must have both"):
        parse_cell_address({"row": 1}, n_rows=2, n_cols=2)
    with pytest.raises(BadInput, match="A1 string or"):
        parse_cell_address(cast(Any, 1.5), n_rows=2, n_cols=2)


def test_set_cell_header_and_data_cell() -> None:
    table = {"header": ["el", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]}
    # data cell: numeric edit stays a number
    norm = set_cell(table, "B2", "1.523")
    assert norm["rows"][0] == ["Si", 1.523]
    assert isinstance(norm["rows"][0][1], float)
    # header rename (row 1)
    norm2 = set_cell(table, "B1", "gap (eV)")
    assert norm2["header"] == ["el", "gap (eV)"]
    assert norm2["rows"] == table["rows"]  # unrelated data untouched
    # dict address form (row 3 = 2nd data row, col 2 = gap_eV)
    norm3 = set_cell(table, {"row": 3, "col": 2}, "0.7")
    assert norm3["rows"][1] == ["Ge", 0.7]


def test_find_replace_cells_string_only_and_count() -> None:
    table = {
        "header": ["element", "note"],
        "rows": [["Si", "band gap aJ"], ["Ge", "band gap aJ"], [1, "aJ scale"]],
    }
    norm, n = find_replace_cells(table, "aJ", "zJ", regex=False)
    assert n == 3
    assert norm["rows"][0][1] == "band gap zJ"
    assert norm["rows"][1][1] == "band gap zJ"
    assert norm["rows"][2][1] == "zJ scale"
    assert norm["rows"][2][0] == 1  # non-string cell untouched, stays an int
    assert norm["header"] == ["element", "note"]  # no match in header


def test_find_replace_cells_regex_backreference() -> None:
    table = {"header": ["x"], "rows": [["value: 12"]]}
    norm, n = find_replace_cells(table, r"(\d+)", r"[\1]", regex=True)
    assert n == 1
    assert norm["rows"][0][0] == "value: [12]"


# ── handler-level ─────────────────────────────────────────────────────


def _seed_table(draft: DraftHandler, hub: Hub, *, caption: str = "Cap") -> Any:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={
            "header": ["element", "gap_eV"],
            "rows": [["Si", 1.12], ["Ge", 0.67]],
        },
        caption=caption,
        at={"last": True},
    )
    return _table_chunk(hub, "d")


def test_edit_table_find_replace_only_matching_cells_and_caption_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed_table(draft, hub, caption="Band gaps (Si)")
    draft.edit(id=tc.dc, find="Si", text="silicon")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"] == [["silicon", 1.12], ["Ge", 0.67]]
    # non-target cell (Ge, both numbers) untouched, caption untouched
    assert meta["caption"] == "Band gaps (Si)"
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "**Band gaps (Si)**" in chunk.text  # markdown re-derived, caption kept


def test_edit_table_find_no_match_refuses_and_leaves_chunk_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed_table(draft, hub)
    before = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    with pytest.raises(BadInput, match="no cell matches"):
        draft.edit(id=tc.dc, find="xenon", text="Xe")
    after = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert after == before


def test_edit_table_cell_a1_string_and_dict_address(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed_table(draft, hub)
    draft.edit(id=tc.dc, cell="B2", text="1.523")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"][0] == ["Si", 1.523]
    assert isinstance(meta["table"]["rows"][0][1], float)  # numerics-indexable

    draft.edit(id=tc.dc, cell={"row": 3, "col": 2}, text="0.7")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta2["table"]["rows"][1] == ["Ge", 0.7]


def test_edit_table_cell_requires_text(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed_table(draft, hub)
    with pytest.raises(BadInput, match="needs text="):
        draft.edit(id=tc.dc, cell="B2")


def test_edit_table_cell_nan_stays_string_no_crash(
    draft: DraftHandler, hub: Hub
) -> None:
    # 'NaN' is a legit "not measured" placeholder — must store as the
    # string "NaN", not float('nan') (which jsonb would reject outright).
    tc = _seed_table(draft, hub)
    draft.edit(id=tc.dc, cell="B2", text="NaN")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    val = meta["table"]["rows"][0][1]
    assert val == "NaN" and isinstance(val, str)


def test_edit_table_conflicting_selectors_rejected(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _seed_table(draft, hub)
    before = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    with pytest.raises(BadInput, match="only one of table=/cell=/find=/sub="):
        draft.edit(
            id=tc.dc,
            table={"header": ["x"], "rows": [[1]]},
            cell="B2",
            text="x",
        )
    after = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert after == before  # nothing applied — neither selector silently won


def test_edit_table_cell_header_rename(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed_table(draft, hub)
    draft.edit(id=tc.dc, cell="B1", text="gap (eV)")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["header"] == ["element", "gap (eV)"]
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "| gap (eV) |" in chunk.text


def test_edit_table_sub_regex_find_replace(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed_table(draft, hub)
    draft.edit(id=tc.dc, sub={"find": r"^Si$", "replace": "silicon"})
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"][0][0] == "silicon"


def test_edit_table_backslash_roundtrip_latex_export(
    draft: DraftHandler, hub: Hub
) -> None:
    """The item-1 backslash-safe channel: a single-backslash LaTeX cell set
    via cell=/text= stores a single backslash in meta.table and LaTeX export
    emits it unescaped (proves gr178512's fix for the supported edit path)."""
    tc = _seed_table(draft, hub)
    draft.edit(id=tc.dc, cell="B2", text=r"$\sim$3 zJ")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    cell_val = meta["table"]["rows"][0][1]
    assert cell_val == r"$\sim$3 zJ"
    assert cell_val.count("\\") == 1  # single backslash, not doubled

    ref = hub.live_store.get_ref(kind="draft", id="d")
    body = latex.render_body(hub.live_store, ref).body
    assert r"$\sim$3 zJ" in body
    assert r"$\\sim$" not in body
    assert "textbackslash" not in body


def test_edit_table_string_channel_roundtrip_latex_export(
    draft: DraftHandler, hub: Hub
) -> None:
    """table= accepted as a JSON string (item 2b, gr178512), verified
    end-to-end: edit via the string channel stores a single backslash and
    LaTeX export emits it unescaped — mirrors the cell=/text= round-trip
    above, now proving the table= string path too."""
    tc = _seed_table(draft, hub)
    draft.edit(
        id=tc.dc,
        table='{"header": ["gap"], "rows": [["$\\\\sim$3 aJ"]]}',
    )
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    cell_val = meta["table"]["rows"][0][0]
    assert cell_val == "$\\sim$3 aJ"
    assert cell_val.count("\\") == 1  # single backslash, not doubled

    ref = hub.live_store.get_ref(kind="draft", id="d")
    body = latex.render_body(hub.live_store, ref).body
    assert r"$\sim$3 aJ" in body
    assert r"$\\sim$" not in body
    assert "textbackslash" not in body
