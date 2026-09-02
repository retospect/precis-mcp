"""Data/table chunks (build step 1) — canonical ``meta.table``
JSON + derived markdown ``text``, inert ``meta.regen``. No execution."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.export import latex
from precis.handlers.draft import DraftHandler
from precis.utils.table_data import (
    _CAPTION_CMD_RE,
    _blank_balanced_command,
    _skip_colspec,
    _tabular_env_span,
    col_letters_to_index,
    find_replace_cells,
    index_to_col_letters,
    infer_scalar,
    locate_latex_cell,
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


# ── needs-table-review recovery (gripe 263197) ────────────────────────


def _flagged_latex_chunk(draft: DraftHandler, hub: Hub, raw: str) -> Any:
    """A LaTeX-imported table chunk as the importer leaves it when
    ``parse_latex_table`` failed at import time: raw ``tabular`` body in
    ``text``, no canonical ``meta.table``, ``meta.flag`` set."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["placeholder"], "rows": [["x"]]},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    hub.live_store.drafts.edit_text(
        tc.handle, raw, meta_patch={"table": None, "flag": "needs-table-review"}
    )
    return tc


_RAW_TABULAR = (
    "{rcc}\n"
    "\\toprule\n"
    "$k$ & \\textbf{Simple} & \\textbf{Combinatorial} \\\\\n"
    "\\midrule\n"
    "2 & 2 & 1 \\\\\n"
    "3 & 3 & 4 \\\\\n"
    "\\bottomrule"
)


def test_edit_table_recovers_grid_from_raw_latex_and_patches_in_place(
    draft: DraftHandler, hub: Hub
) -> None:
    """The read path (``table_payload``) always recovered these chunks, so
    ``get()`` rendered them while every write door refused — the read/write
    divergence of gripe 263197. The write path now recovers through the same
    function for ADDRESSING, so a flagged chunk is editable without
    hand-reconstructing its grid — but (gripe 271129) the raw LaTeX is
    patched IN PLACE rather than replaced with markdown re-derived from the
    (lossy) parsed grid: ``meta.table`` is never written, and the flag is
    left exactly as-is, because no canonical grid was actually persisted."""
    tc = _flagged_latex_chunk(draft, hub, _RAW_TABULAR)
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # precondition: no canonical data
    assert meta["flag"] == "needs-table-review"

    # A cell edit used to raise "this table chunk has no stored data".
    draft.edit(id=tc.dc, cell="A1", text="order")

    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "order & \\textbf{Simple} & \\textbf{Combinatorial}" in chunk.text
    assert "\\toprule" in chunk.text and "\\bottomrule" in chunk.text
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # never promoted to canonical
    assert meta["flag"] == "needs-table-review"  # honest: still not canonical


def test_edit_table_find_replace_patches_flagged_latex_chunk_in_place(
    draft: DraftHandler, hub: Hub
) -> None:
    """Citation backfill reaches table chunks via ``find=`` — the door the
    conversion waves actually need. Patches the raw LaTeX in place (gripe
    271129), not the parsed-then-re-derived markdown."""
    tc = _flagged_latex_chunk(draft, hub, _RAW_TABULAR)
    draft.edit(id=tc.dc, find="Combinatorial", text="Combinatorial [fi99]")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\textbf{Combinatorial [fi99]}" in chunk.text
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")


def test_edit_table_unrecoverable_chunk_still_refuses(
    draft: DraftHandler, hub: Hub
) -> None:
    """Recovery must not paper over a genuinely dataless chunk: a float
    wrapper whose ``tabular`` never made it into this chunk has no grid to
    find, and the honest refusal has to survive."""
    tc = _flagged_latex_chunk(draft, hub, "[ht]\n\\centering")
    with pytest.raises(BadInput, match="no stored data"):
        draft.edit(id=tc.dc, cell="A1", text="nope")


def test_edit_table_ragged_parse_falls_through_to_refusal(
    draft: DraftHandler, hub: Hub
) -> None:
    """Recovery must not half-succeed. A ragged GFM table parses into a
    header and rows, but ``normalize_table`` rejects the width mismatch —
    and a partially-recovered grid is worse than none, because the edit
    would silently persist a table the source text never contained. The
    parse failure has to land on the same honest refusal as no data at
    all."""
    tc = _flagged_latex_chunk(draft, hub, "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |")
    with pytest.raises(BadInput, match="no stored data"):
        draft.edit(id=tc.dc, cell="A1", text="nope")
    # and the chunk is untouched — no partial grid persisted, flag intact
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")
    assert meta["flag"] == "needs-table-review"


def test_edit_table_recovers_caption_from_raw_latex(
    draft: DraftHandler, hub: Hub
) -> None:
    """A caption living only in the raw ``\\caption{}`` survives a cell
    edit — the in-place LaTeX patch (gripe 271129) never re-derives
    markdown, so the caption's own ``\\caption{}`` command stays put in the
    text verbatim (stronger than the old contract, which required a
    separate recovery step and lost the raw command either way)."""
    raw = "{cc}\n\\caption{Yield thresholds}\n\\toprule\na & b \\\\\n1 & 2 \\\\\n"
    tc = _flagged_latex_chunk(draft, hub, raw)
    draft.edit(id=tc.dc, cell="A1", text="alpha")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\caption{Yield thresholds}" in chunk.text
    assert "alpha & b" in chunk.text


def test_edit_table_markdown_fallback_recovery_promotes_to_canonical(
    draft: DraftHandler, hub: Hub
) -> None:
    """The SIBLING ``table_payload`` fallback — plain GFM markdown text
    with no canonical ``meta.table`` (a pre-canonical-store chunk, not a
    LaTeX import) — edits through the ORDINARY re-derive path and
    promotes ``meta.table`` to canonical. ``is_latex_source``
    (``parse_markdown_table(chunk.text or "") is None``) has to see the
    REAL text to tell the two recovery shapes apart: fed an unconditional
    ``""`` (the ``or`` → ``and`` mutant, since `chunk.text` is truthy
    here) it would misclassify this markdown-recovered chunk as
    LaTeX-sourced and divert the edit into the raw-LaTeX in-place patcher
    — which can't find any ``&``/``\\\\``-delimited LaTeX structure in
    GFM pipe syntax (refusing or mangling the whole chunk), and never
    promotes ``meta.table`` either way."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["placeholder"], "rows": [["x"]]},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    md_text = table_to_markdown({"header": ["a", "b"], "rows": [["1", "2"]]})
    # Simulate a pre-canonical-store chunk: raw GFM text, no meta.table
    # (no needs-table-review flag either — that flag is a LaTeX-import
    # marker, not part of this fallback shape).
    hub.live_store.drafts.edit_text(tc.handle, md_text, meta_patch={"table": None})
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # precondition: recoverable only via the fallback

    draft.edit(id=tc.dc, cell="A1", text="one")

    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    # Promoted to canonical — proves the ordinary re-derive path fired
    # (is_latex_source correctly False), not the LaTeX in-place patcher,
    # which never writes meta.table.
    assert meta.get("table") == {"header": ["one", "b"], "rows": [["1", "2"]]}


# ── LaTeX in-place patching (gripe 271129) ─────────────────────────────
#
# The exact chunk that exposed the bug (dr42995 dc1506560): a full
# \begin{tabular} wrapper with \label, booktabs rules, a \multicolumn
# summary row, and \, thin spaces — every one of those used to vanish
# (or, for \multicolumn, silently migrate to the wrong column) the moment
# any cell/find/sub edit re-derived markdown from the parsed grid.

_POC_ROLES_LATEX = (
    "\\centering\n"
    "\\caption{PoC circuit roles and their cassette chemistry (see "
    "Section~\\ref{sec:por-summary} for the experimental protocol).}\n"
    "\\label{tab:poc-roles}\n"
    "\\small\n"
    "\\begin{tabular}{llll}\n"
    "\\toprule\n"
    "\\textbf{Role} & \\textbf{$\\lambda$} & \\textbf{Cassette} & "
    "\\textbf{Count} \\\\\n"
    "\\midrule\n"
    "$\\text{INPUT}_{\\lambda_A}$ & 365\\,nm (UV) & Diarylethene "
    "photoswitch & 1 \\\\\n"
    "$\\text{INPUT}_{\\lambda_B}$ & 450\\,nm (blue) & Azobenzene "
    "photoswitch & 1 \\\\\n"
    "XOR & --- & SWITCH (DAE + rotaxane + BODIPY) & 1 \\\\\n"
    "OUTPUT & 520\\,nm (emission) & BODIPY reporter & 1 \\\\\n"
    "BEACON & 650\\,nm (emission) & Porphyrin marker (always on) & 1 \\\\\n"
    "\\midrule\n"
    "\\multicolumn{3}{l}{\\textit{Total}} & \\textbf{5 boxels, 4 roles} \\\\\n"
    "\\bottomrule\n"
    "\\end{tabular}"
)


def test_edit_table_latex_label_survives_cell_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """The exact edit from gripe 271129 (XOR -> NOR) leaves \\label{} —
    and hence every \\ref{tab:poc-roles} elsewhere — intact. Before the
    fix, any edit re-derived markdown from the parsed grid and
    \\label{} was simply never carried through, dangling every cross-ref
    (surfacing only as "Table ??" at LaTeX build time)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, find="XOR", text="NOR")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\label{tab:poc-roles}" in chunk.text
    assert "NOR & --- &" in chunk.text
    assert "XOR" not in chunk.text


def test_edit_table_latex_multicolumn_row_survives_unrelated_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """The other half of the gripe's exact edit (SWITCH cassette rename)
    leaves the untouched \\multicolumn summary row byte-for-byte intact.
    Before the fix, the parsed grid flattened that row's value from
    column 4 to column 2 the moment ANY cell in the table was edited."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(
        id=tc.dc,
        find="SWITCH (DAE + rotaxane + BODIPY)",
        text="SWITCH (DAE + azobenzene + rotaxane + BODIPY)",
    )
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert (
        "\\multicolumn{3}{l}{\\textit{Total}} & \\textbf{5 boxels, 4 roles}"
        in chunk.text
    )
    assert "SWITCH (DAE + azobenzene + rotaxane + BODIPY)" in chunk.text


def test_edit_table_latex_thin_space_and_rules_survive_unrelated_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """\\, thin spaces (on cells the edit never touches) and the booktabs
    rules survive an edit elsewhere in the table — both used to be
    silently normalised away by the markdown re-derivation."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, find="XOR", text="NOR")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "365\\,nm (UV)" in chunk.text
    assert "450\\,nm (blue)" in chunk.text
    assert "\\toprule" in chunk.text
    assert "\\midrule" in chunk.text
    assert "\\bottomrule" in chunk.text


def test_edit_table_latex_full_gripe_edit_end_to_end(
    draft: DraftHandler, hub: Hub
) -> None:
    """Both halves of the gripe's exact edit applied in sequence: the
    chunk stays LaTeX-sourced throughout (meta.table never gets written —
    promoting the lossy parsed grid to canonical would freeze its
    multicolumn mis-mapping in as the new truth)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, find="XOR", text="NOR")
    draft.edit(
        id=tc.dc,
        find="SWITCH (DAE + rotaxane + BODIPY)",
        text="SWITCH (DAE + azobenzene + rotaxane + BODIPY)",
    )
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    text = chunk.text
    assert "\\label{tab:poc-roles}" in text
    assert "\\toprule" in text and "\\midrule" in text and "\\bottomrule" in text
    assert "NOR & --- & SWITCH (DAE + azobenzene + rotaxane + BODIPY) & 1" in text
    assert "\\multicolumn{3}{l}{\\textit{Total}} & \\textbf{5 boxels, 4 roles}" in text
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # never promoted — stays LaTeX-sourced


def test_edit_table_latex_cell_address_patches_in_place(
    draft: DraftHandler, hub: Hub
) -> None:
    """``cell=`` addressing (not just ``find=``) also patches the raw LaTeX
    in place — row 6 (1-based, header=row1) is the BEACON data row, col 1
    is Role."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, cell={"row": 6, "col": 1}, text="BEACON2")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "BEACON2 & 650\\,nm (emission)" in chunk.text
    assert "\\label{tab:poc-roles}" in chunk.text


def test_edit_table_latex_cell_refuses_when_unlocatable_in_multicolumn_row(
    draft: DraftHandler, hub: Hub
) -> None:
    """The fallback guarantee this fix leans on: when the parsed-grid
    address can't be mapped back to a real raw cell (the \\multicolumn
    row only has 2 raw ``&``-delimited fields, not the parsed grid's 4),
    the edit REFUSES — chunk left byte-for-byte untouched — rather than
    silently patching the wrong text. Loud refusal, never silent damage."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="can't be safely located"):
        draft.edit(id=tc.dc, cell={"row": 7, "col": 4}, text="oops")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


# ── position-shadow addressing machinery (fallbacks, pure functions) ──


def test_blank_balanced_command_unbalanced_brace_stops_blanking() -> None:
    """When a \\caption{'s opening brace never finds a matching close
    (``_find_balanced`` returns ``None``), the blanking loop bails out
    rather than guessing a wrong span — the text comes back byte-for-byte
    unchanged (nothing blanked), not partially/incorrectly blanked."""
    text = "\\caption{never closes and the rest of the text is untouched"
    out = _blank_balanced_command(text, _CAPTION_CMD_RE)
    assert out == text


def test_blank_balanced_command_matches_at_text_start() -> None:
    """The scan has to start at offset 0 — a ``\\caption{}`` opening the
    text (no preceding wrapper, the "captured tabular inner" shape this
    function's own docstring calls out) gets found and blanked, not
    skipped over. Distinct from the unbalanced-brace case above, which
    returns the text unchanged regardless of the starting offset (no
    match found vs. no matching close found look the same) — this one's
    caption DOES close, so only a genuine from-0 scan blanks it."""
    text = "\\caption{Foo}\\label{tab:x}\nbody"
    out = _blank_balanced_command(text, _CAPTION_CMD_RE)
    assert "Foo" not in out
    assert "\\label{tab:x}" in out
    assert len(out) == len(text)


def test_blank_balanced_command_offset_targets_the_open_brace() -> None:
    """``_find_balanced`` is handed ``m.end() - 1`` — the index of the
    matched command's own opening ``{`` — not one character earlier. A
    synthetic pattern whose match ends in a literal ``\\{`` (a backslash
    immediately followed by the brace) makes the distinction sharp:
    starting one index too early (``- 2``) lands ON that backslash, whose
    escape-handling then skips clean over the real opening brace — the
    depth count never opens, so the scan runs off the end of the string
    unbalanced and nothing gets blanked at all."""
    pattern = re.compile(r"Q\\\{")
    text = "Q\\{Foo}rest"
    out = _blank_balanced_command(text, pattern)
    assert out == " " * 7 + "rest"


def test_tabular_env_span_unmatched_begin_returns_rest_of_text() -> None:
    """No matching ``\\end{tabular}`` anywhere after an inner
    ``\\begin{tabular}`` — the span falls back to "everything after the
    begin tag", not a crash or a truncated/incorrect span."""
    text = "prefix\n\\begin{tabular}{ll}\nrest of body"
    start, end = _tabular_env_span(text)
    assert (start, end) == (text.index("{ll}"), len(text))


def test_tabular_env_span_nested_env_depth_counted() -> None:
    """A ``tabular`` nested inside another ``tabular`` of the same name is
    depth-counted rather than matched to the FIRST ``\\end{tabular}`` seen
    (which would truncate the span at the inner table's close) — the
    returned span extends to the outer, matching close."""
    text = (
        "\\begin{tabular}{ll}\n"
        "\\begin{tabular}{ll}\n"
        "nested & cell \\\\\n"
        "\\end{tabular}\n"
        "outer1 & outer2 \\\\\n"
        "\\end{tabular}"
    )
    start, end = _tabular_env_span(text)
    assert start == text.index("{ll}")
    # the OUTER close, not the inner one the depth counter skips past
    assert end == text.rindex("\\end{tabular}")


def test_skip_colspec_handles_leading_space_and_optional_pos_arg() -> None:
    """Leading whitespace, then an optional ``[pos]`` alignment arg, then
    whitespace again, then the ``{cols}`` group(s) — all skipped, landing
    exactly at the first row content."""
    text = "  [t]  {lcc}rows-start"
    end = _skip_colspec(text, 0)
    assert text[end:] == "rows-start"


def test_skip_colspec_unbalanced_group_stops_at_open_brace() -> None:
    """A colspec ``{`` that never closes (``_find_balanced`` returns
    ``None``) can't be confirmed to end anywhere — the scan stops before
    consuming it rather than guessing, leaving the index at the ``{``."""
    text = "{lcc"  # no closing brace anywhere
    end = _skip_colspec(text, 0)
    assert end == 0


def test_locate_latex_cell_row_out_of_range_returns_none() -> None:
    tc_text = _POC_ROLES_LATEX
    assert locate_latex_cell(tc_text, 999, 0) is None  # way past the last row
    assert locate_latex_cell(tc_text, 0, 0) is None  # row1=0 -> negative index


def test_locate_latex_cell_defensive_exception_guard_returns_none() -> None:
    """A malformed ``row1`` blows up inside the addressing arithmetic
    (``row1 - 1`` on a non-int) — the broad ``except Exception`` guard
    still returns the same honest "can't locate" ``None`` rather than
    letting an internal error escape to the caller."""
    assert locate_latex_cell(_POC_ROLES_LATEX, cast(Any, "oops"), 0) is None


def test_edit_table_latex_caption_regen_rejected_alongside_data_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """caption=/regen= riding along a cell=/find=/sub= edit on a
    LaTeX-sourced chunk is refused rather than silently no-op'd — patching
    meta.caption wouldn't be read back (the read path re-derives the
    caption from \\caption{} in the text itself, not from meta)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    with pytest.raises(BadInput, match="don't apply together") as exc:
        draft.edit(id=tc.dc, find="XOR", text="NOR", caption="New caption")
    # The next= hint names the selector actually in play (find=) so the
    # agent retries with the right verb — a wrong name here (the
    # cell/find ternary picking the wrong branch) would send it to retry
    # with cell= or sub= instead of find=.
    assert ", find=…" in (exc.value.next or "")


def test_edit_table_latex_caption_alone_rejected_alongside_cell_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """caption= alone (regen= absent) riding along a cell= edit is also
    refused — one half of the `caption is not None or regen is not None`
    guard; if it were `and` instead of `or`, this would fall through
    silently to the data-edit path since regen= is unset."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="don't apply together"):
        draft.edit(
            id=tc.dc, cell={"row": 6, "col": 1}, text="oops", caption="New caption"
        )
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_regen_alone_rejected_alongside_cell_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """regen= alone (caption= absent) riding along a cell= edit is also
    refused — the sibling half of the `or` guard; `and` would let this
    slip through to the data-edit path silently instead of refusing."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="don't apply together"):
        draft.edit(
            id=tc.dc,
            cell={"row": 6, "col": 1},
            text="oops",
            regen={"source": "manual"},
        )
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


# ── LaTeX in-place patching: refuse paths (_edit_latex_table_in_place) ──


def test_edit_table_latex_cell_requires_text(draft: DraftHandler, hub: Hub) -> None:
    """``cell=`` without ``text=`` on a LaTeX-sourced table chunk refuses —
    same guard as the canonical-table path, but exercised through the
    LaTeX in-place branch (``_edit_latex_table_in_place``, not
    ``_edit_table``)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="needs text="):
        draft.edit(id=tc.dc, cell="A1")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_find_empty_string_rejected(
    draft: DraftHandler, hub: Hub
) -> None:
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="non-empty string"):
        draft.edit(id=tc.dc, find="")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_find_requires_text(draft: DraftHandler, hub: Hub) -> None:
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="requires text="):
        draft.edit(id=tc.dc, find="XOR")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_sub_invalid_regex_rejected(
    draft: DraftHandler, hub: Hub
) -> None:
    """An unparseable regex in ``sub=`` (unbalanced group) is re-raised as
    ``BadInput`` rather than propagating the raw ``re.error`` — chunk left
    untouched. This exact refusal is raised from TWO call sites depending
    on routing — ``_edit_latex_table_in_place`` (correct, for a
    ``sub=``-only selector on a LaTeX-sourced chunk) with no ``next=``,
    vs. ``find_replace_cells`` on the legacy re-derive path (wrong route,
    reached if the ``sub is not None`` routing guard mis-fires) which DOES
    set one ("check the pattern — …") — so asserting ``next is None``
    pins the route, not just the exception type/message (both routes
    raise the identically-worded "invalid regex ..." BadInput)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="invalid regex") as exc:
        draft.edit(id=tc.dc, sub={"find": "(unclosed", "replace": "x"})
    assert exc.value.next is None
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_find_replacement_may_contain_latex_commands(
    draft: DraftHandler, hub: Hub
) -> None:
    """A literal ``find=`` replacement carrying backslash commands lands
    VERBATIM. Regression for gripe 273955: the replacement was handed to
    ``Pattern.subn`` as a *template*, so ``\\cite{…}`` / ``$\\sim$`` died on
    ``re.error: bad escape \\c`` and surfaced as an opaque ``[error:Internal]``
    — meaning no citation or inline math could ever be introduced into a
    table cell, which is exactly what citation-accuracy work needs."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(
        id=tc.dc,
        find="SWITCH (DAE + rotaxane + BODIPY)",
        text="500--2000 (best $\\sim$10$^3$)\\cite{collier2001}",
    )
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert "500--2000 (best $\\sim$10$^3$)\\cite{collier2001}" in after.text
    # in-place patch: surrounding LaTeX untouched
    assert "\\label{tab:poc-roles}" in after.text
    assert "\\multicolumn{3}{l}{\\textit{Total}}" in after.text


def test_edit_table_latex_find_replacement_backreference_stays_literal(
    draft: DraftHandler, hub: Hub
) -> None:
    """``find=`` is literal on BOTH sides: a ``\\1`` in the replacement is
    emitted as the two characters, never interpolated as group 1. Without
    the escaping this silently substituted captured text instead of raising
    — the quiet half of gripe 273955."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, find="BEACON", text="\\1 marker")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert "\\1 marker" in after.text


def test_edit_table_latex_sub_keeps_backreference_interpolation(
    draft: DraftHandler, hub: Hub
) -> None:
    """The 273955 fix escapes the replacement for literal ``find=`` ONLY —
    ``sub=`` keeps its documented regex-template semantics, so a
    backreference still resolves. Pins that the fix did not over-escape and
    quietly break the regex path."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, sub={"find": r"(BEACON)", "replace": r"\1-PRIMARY"})
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert "BEACON-PRIMARY" in after.text
    assert "\\1-PRIMARY" not in after.text


def test_edit_table_latex_sub_bad_replacement_template_is_badinput(
    draft: DraftHandler, hub: Hub
) -> None:
    """A malformed replacement *template* in ``sub=`` (unescaped ``\\cite``)
    is refused as ``BadInput`` naming the escaping rule, not raised as a
    bare ``re.error`` that the handler reports as ``[error:Internal]`` with
    nothing actionable. Chunk left untouched. The ``next=`` distinguishes
    this from the find-side "invalid regex" refusal."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="invalid replacement template") as exc:
        draft.edit(id=tc.dc, sub={"find": "BEACON", "replace": "\\cite{x}"})
    assert exc.value.next is not None
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_sub_no_match_refuses(draft: DraftHandler, hub: Hub) -> None:
    """``sub=`` with a valid regex that matches nothing in the raw LaTeX
    refuses (zero replacements) — the LaTeX-path sibling of the
    canonical-table ``find=`` no-match refusal."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="no cell matches"):
        draft.edit(id=tc.dc, sub={"find": "no-such-substring-zzz", "replace": "y"})
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


# ── dry_run on table chunks (gr273955 residual) ────────────────────────
# A table chunk used to blanket-reject dry_run for EVERY op, so a table
# edit could never be previewed at all — and the rejection's own next=
# hint recommended the exact call (text=, dry_run=True) that had just
# failed on this chunk kind. Every path that computes a would-be new
# chunk text now renders the same diff/full preview as the plain
# text-mutation paths and writes NOTHING.


def test_edit_table_latex_find_replace_dry_run_diff_leaves_chunk_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    """``find=``+``text=`` with a backslash-carrying replacement, previewed
    on a LaTeX-in-place table chunk: the diff shows the replacement
    (backslashes un-doubled back to literal, per the find= escaping rule)
    and nothing is written."""
    tc = _flagged_latex_chunk(draft, hub, _RAW_TABULAR)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None

    r = draft.edit(
        id=tc.dc,
        find="Combinatorial",
        text="Combinatorial \\cite{fi99}",
        dry_run=True,
    )
    assert "[dry-run]" in r.body
    assert "Combinatorial \\cite{fi99}" in r.body

    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text
    assert "\\cite{fi99}" not in after.text
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # still never promoted to canonical


def test_edit_table_latex_find_replace_dry_run_full_leaves_chunk_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    """``dry_run='full'`` on the same LaTeX-in-place path shows the whole
    post-edit body instead of a diff — still writes nothing."""
    tc = _flagged_latex_chunk(draft, hub, _RAW_TABULAR)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None

    r = draft.edit(
        id=tc.dc,
        find="Combinatorial",
        text="Combinatorial \\cite{fi99}",
        dry_run="full",
    )
    assert "[dry-run]" in r.body
    assert "\\textbf{Combinatorial \\cite{fi99}}" in r.body
    assert "\\toprule" in r.body and "\\bottomrule" in r.body

    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_cell_dry_run_on_markdown_canonical_table_leaves_untouched(
    draft: DraftHandler, hub: Hub
) -> None:
    """``cell=`` dry_run on an ordinary (markdown-canonical, not
    LaTeX-recovered) table chunk previews the re-derived markdown and
    writes neither the text nor ``meta.table``."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["element", "gap_eV"], "rows": [["Si", 1.12]]},
        caption="Band gaps",
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None

    r = draft.edit(id=tc.dc, cell="A2", text="Ge", dry_run=True)
    assert "[dry-run]" in r.body
    assert "Ge" in r.body

    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"] == [["Si", 1.12]]  # unchanged


def test_edit_table_structural_op_dry_run_still_rejects_with_table_aware_hint(
    draft: DraftHandler, hub: Hub
) -> None:
    """A genuinely non-previewable op (``move=``) on a table chunk still
    rejects dry_run — but the ``next=`` hint must name a call that
    actually works for THIS chunk (a find=/text= preview), not the
    generic text= rewrite claim that used to be a self-recommending dead
    end on a table chunk (gr273955 residual)."""
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    draft.put(
        id="d",
        chunk_kind="table",
        table={"header": ["x"], "rows": [[1]]},
        at={"last": True},
    )
    tc = _table_chunk(hub, "d")
    order = hub.live_store.drafts.reading_order(
        hub.live_store.get_ref(kind="draft", id="d").id  # type: ignore[union-attr]
    )
    title_h = order[0].handle

    with pytest.raises(BadInput, match="dry_run has no preview") as exc:
        draft.edit(id=tc.dc, move={"before": "¶" + title_h}, dry_run=True)
    assert exc.value.next is not None
    assert "find=" in exc.value.next and "text=" in exc.value.next
    assert tc.dc in exc.value.next
