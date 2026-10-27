"""Data/table chunks (build step 1) — canonical ``meta.table``
JSON + derived markdown ``text``, inert ``meta.regen``. No execution."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.export import latex
from precis.handlers.draft import DraftHandler
from precis.utils.table_data import (
    _CAPTION_CMD_RE,
    _blank_balanced_command,
    _skip_colspec,
    _tabular_env_span,
    find_replace_cells,
    fresh_table_id,
    infer_scalar,
    is_table_id,
    mint_table_id,
    normalize_table,
    parse_markdown_table,
    resolve_cell,
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
    # numbers stay numbers (numerics-indexable), header coerced to str;
    # columns/rowmeta are minted with ids + pos
    norm = normalize_table({"header": [1, "gap"], "rows": [["Si", 1.12]]})
    assert norm["header"] == ["1", "gap"]
    assert norm["rows"] == [["Si", 1.12]]
    assert [c["name"] for c in norm["columns"]] == ["1", "gap"]
    assert all(is_table_id(c["id"]) for c in norm["columns"])
    assert all(is_table_id(r["id"]) for r in norm["rowmeta"])
    assert [c["pos"] for c in norm["columns"]] == sorted(
        c["pos"] for c in norm["columns"]
    )
    assert [r["pos"] for r in norm["rowmeta"]] == sorted(
        r["pos"] for r in norm["rowmeta"]
    )


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


def test_mint_table_id_letter_first_property() -> None:
    """10k mints: 4 chars, Crockford base-32, first char a letter other
    than ``p`` (the ``~pN`` selector owns ``p``; ``~N``/``~N..M`` own
    digits) — the spec's property test."""
    for _ in range(10_000):
        i = mint_table_id()
        assert is_table_id(i)
        assert len(i) == 4
        assert i[0].isalpha() and i[0] != "p"


def test_fresh_table_id_never_reuses() -> None:
    """A retired id can never be re-minted: ``used`` (live ∪ retired) is
    excluded — a monkeypatched forced draw still skips it."""
    import precis.utils.table_data as td

    assert fresh_table_id({"aaaa"}) != "aaaa"
    draws = iter(["zzzz", "zzzz", "yyyy"])
    old = td.mint_table_id
    td.mint_table_id = lambda: next(draws)
    try:
        assert td.fresh_table_id({"zzzz"}) == "yyyy"
    finally:
        td.mint_table_id = old


def test_keyword_column_id_refused() -> None:
    """A caller-supplied column id that is a Python keyword would land in
    stored formulas verbatim and blow up ast.parse — refused at
    normalize."""
    for kid in ("break", "assert"):
        with pytest.raises(BadInput, match="keyword"):
            normalize_table(
                {
                    "columns": [{"name": "x", "id": kid}],
                    "rows": [[1]],
                }
            )


def test_footer_non_string_func_refused() -> None:
    """footer={'a': ['sum']} must be a BadInput with next=, not a bare
    TypeError on the unhashable list."""
    with pytest.raises(BadInput, match="must be one of") as e:
        normalize_table({"header": ["a"], "rows": [[1]], "footer": {"a": ["sum"]}})
    assert e.value.next


def test_footer_key_of_retired_column_is_pruned() -> None:
    """A stored footer spec keyed by a dropped (retired) column id is
    pruned on re-normalise — the aggregate can't compute, and one stale
    key must not sink an unrelated later write. A key that was never a
    column still refuses."""
    t = normalize_table(
        {
            "columns": [{"name": "a"}, {"name": "b"}],
            "rows": [[1, 2]],
            "footer": {"b": "sum"},
        }
    )
    b_id = t["columns"][1]["id"]
    again = normalize_table(
        {
            "columns": t["columns"][:1],
            "rows": [[1]],
            "retired_ids": [b_id],
            "footer": t["footer"],
        }
    )
    assert "footer" not in again
    with pytest.raises(BadInput, match="no column id/name"):
        normalize_table({"header": ["a"], "rows": [[1]], "footer": {"nope": "sum"}})


def test_minted_ids_avoid_column_names() -> None:
    """A minted id equal to another column's *name* would bind formulas to
    the wrong column (shared resolver namespace) — the mint pool includes
    names."""
    import precis.utils.table_data as td

    cols = [{"name": "zzzz"}, {"name": "b"}]
    draws = iter(["zzzz", "yyyy", "wwww", "xxxx"])
    old = td.mint_table_id
    td.mint_table_id = lambda: next(draws)
    try:
        t = normalize_table({"columns": cols, "rows": [[1, 2]]})
    finally:
        td.mint_table_id = old
    # zzzz is taken as a NAME — the mint skips it for the first column
    assert t["columns"][0]["id"] == "yyyy"
    assert t["columns"][1]["id"] == "wwww"


def test_normalize_20k_rows_is_linear_enough() -> None:
    """The used-id set is kept, not copied per mint (the old per-mint copy
    made this quadratic). Guarded by a 20k/2k time ratio, not wall clock:
    linear is ~10, quadratic ~100, and load scales both sides alike. GC is
    off and each side takes its best of three, so a collection or a noisy
    2k sample can't push a linear run past the cap (36 seen under xdist)."""
    import gc
    import time

    def timed(n: int) -> tuple[float, dict]:
        rows = [[i] for i in range(n)]
        best, out = float("inf"), {}
        for _ in range(3):
            t0 = time.process_time()
            out = normalize_table({"header": ["a"], "rows": rows})
            best = min(best, time.process_time() - t0)
        return best, out

    gc.disable()
    try:
        t_small, _ = timed(2_000)
        t_big, t = timed(20_000)
    finally:
        gc.enable()
    assert len(t["rowmeta"]) == 20_000
    assert t_big / max(t_small, 1e-3) < 50


def test_resolve_cell_id_address() -> None:
    table = normalize_table({"header": ["a", "b"], "rows": [[1, 2], [3, 4], [5, 6]]})
    rid = table["rowmeta"][1]["id"]
    cid = table["columns"][0]["id"]
    assert resolve_cell(table, f"{rid}.{cid}") == (1, 0)
    # column side may also be named
    assert resolve_cell(table, f"{rid}.b") == (1, 1)


def test_resolve_cell_refuses_a1_and_dict_and_unknown_ids() -> None:
    table = normalize_table({"header": ["a", "b"], "rows": [[1, 2]]})
    with pytest.raises(BadInput, match="not a cell address"):
        resolve_cell(table, "B2")  # A1 gone — refused with the id form
    with pytest.raises(BadInput, match="must be a"):
        resolve_cell(table, {"row": 2, "col": 2})
    with pytest.raises(BadInput, match="no row id"):
        resolve_cell(table, "zzzz.a")
    with pytest.raises(BadInput, match="no column id/name"):
        resolve_cell(table, f"{table['rowmeta'][0]['id']}.zzzz")


def test_set_cell_by_id() -> None:
    table = normalize_table(
        {"header": ["el", "gap_eV"], "rows": [["Si", 1.12], ["Ge", 0.67]]}
    )
    rid, cid = table["rowmeta"][0]["id"], table["columns"][1]["id"]
    norm = set_cell(table, f"{rid}.{cid}", "1.523")
    assert norm["rows"][0] == ["Si", 1.523]
    assert isinstance(norm["rows"][0][1], float)
    # ids survive the write — an edit rewrites cells, not identities
    assert norm["rowmeta"][0]["id"] == rid
    assert norm["columns"][1]["id"] == cid
    assert norm["rows"][1] == ["Ge", 0.67]  # unrelated data untouched


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


def _table_ids(hub: Hub, tc: Any) -> tuple[list[str], list[str]]:
    """``(row_ids, col_ids)`` of the chunk's canonical table, in order —
    the stable ids ``cell='<row-id>.<col-id>'`` addresses by."""
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    t = meta["table"]
    return [r["id"] for r in t["rowmeta"]], [c["id"] for c in t["columns"]]


def test_edit_table_cell_id_address(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="1.523")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"][0] == ["Si", 1.523]
    assert isinstance(meta["table"]["rows"][0][1], float)  # numerics-indexable

    # the column side may also be named; ids survive the write either way
    draft.edit(id=tc.dc, cell=f"{rows[1]}.gap_eV", text="0.7")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta2["table"]["rows"][1] == ["Ge", 0.7]
    assert meta2["table"]["rowmeta"][1]["id"] == rows[1]


def test_edit_table_cell_a1_and_dict_forms_refused(
    draft: DraftHandler, hub: Hub
) -> None:
    """A1 (``'B2'``) and ``{'row','col'}`` addressing are gone — both
    refuse with a ``next=`` showing the ``<row-id>.<col-id>`` form, and
    neither touches the chunk."""
    tc = _seed_table(draft, hub)
    before = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    with pytest.raises(BadInput, match="not a cell address") as exc:
        draft.edit(id=tc.dc, cell="B2", text="x")
    assert "row-id" in str(exc.value.next)
    with pytest.raises(BadInput, match="must be a"):
        draft.edit(id=tc.dc, cell=cast(Any, {"row": 2, "col": 2}), text="x")
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle) == before


def test_edit_table_cell_requires_text(draft: DraftHandler, hub: Hub) -> None:
    tc = _seed_table(draft, hub)
    with pytest.raises(BadInput, match="needs text="):
        draft.edit(id=tc.dc, cell="r2k7.k5f9")


def test_edit_table_cell_nan_stays_string_no_crash(
    draft: DraftHandler, hub: Hub
) -> None:
    # 'NaN' is a legit "not measured" placeholder — must store as the
    # string "NaN", not float('nan') (which jsonb would reject outright).
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="NaN")
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
            cell="r2k7.k5f9",
            text="x",
        )
    after = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert after == before  # nothing applied — neither selector silently won


def test_edit_table_cell_header_rename_is_not_a_cell_address(
    draft: DraftHandler, hub: Hub
) -> None:
    """The header row carries no row id — renaming a column is a
    column-schema op (``rename_column=``, slice 1b), never a cell edit.
    The old ``cell='B1'`` route is refused and touches nothing."""
    tc = _seed_table(draft, hub)
    before = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    with pytest.raises(BadInput, match="not a cell address"):
        draft.edit(id=tc.dc, cell="B1", text="gap (eV)")
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle) == before


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
    rows, cols = _table_ids(hub, tc)
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text=r"$\sim$3 zJ")
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


def test_edit_table_cell_on_latex_source_converts_then_refuses_with_ids(
    draft: DraftHandler, hub: Hub
) -> None:
    """The read path (``table_payload``) always recovered these chunks, so
    ``get()`` rendered them while every write door refused — the read/write
    divergence of gripe 263197. ``cell=`` is id-addressed now
    (spreadsheet-tables 1a), and a LaTeX-sourced chunk has no ids: the
    first cell edit CONVERTS it to the canonical id-addressed grid (spec
    §13 — raw LaTeX layout is dropped, the flag cleared) and then refuses,
    naming the just-minted ids the caller couldn't know. The retry with a
    real address lands."""
    tc = _flagged_latex_chunk(draft, hub, _RAW_TABULAR)
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # precondition: no canonical data
    assert meta["flag"] == "needs-table-review"

    with pytest.raises(BadInput, match="DID convert") as exc:
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="order")
    assert "cell='" in str(exc.value.next)  # offers a real address

    # The conversion landed despite the refusal — canonical grid with ids,
    # derived markdown text, review flag cleared.
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    tbl = meta["table"]
    assert tbl["header"] == ["$k$", "Simple", "Combinatorial"]
    assert all(is_table_id(c["id"]) for c in tbl["columns"])
    assert all(is_table_id(r["id"]) for r in tbl["rowmeta"])
    assert not meta.get("flag")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "| $k$ | Simple | Combinatorial |" in chunk.text
    assert "\\toprule" not in chunk.text  # raw LaTeX layout dropped

    # Retry with the real ids — the cell edit now applies.
    rows, cols = _table_ids(hub, tc)
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="order")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta2["table"]["rows"][0][0] == "order"


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
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="nope")


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
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="nope")
    # and the chunk is untouched — no partial grid persisted, flag intact
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")
    assert meta["flag"] == "needs-table-review"


def test_edit_table_recovers_caption_from_raw_latex(
    draft: DraftHandler, hub: Hub
) -> None:
    """A caption living only in the raw ``\\caption{}`` survives the
    cell-edit conversion (spec §13): the raw ``\\caption{}`` command is
    dropped with the rest of the LaTeX layout, but the recovered legend is
    carried into the derived markdown's ``**…**`` lead line."""
    raw = "{cc}\n\\caption{Yield thresholds}\n\\toprule\na & b \\\\\n1 & 2 \\\\\n"
    tc = _flagged_latex_chunk(draft, hub, raw)
    with pytest.raises(BadInput, match="DID convert"):
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="alpha")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "**Yield thresholds**" in chunk.text
    assert "\\caption" not in chunk.text  # raw LaTeX dropped by conversion
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["header"] == ["a", "b"]


def test_edit_table_cell_edit_persists_label_through_conversion(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``\\label{...}`` recovered alongside the grid lands in
    ``meta.label`` on the cell-edit conversion path (gripe 271129 + spec
    §13) — the raw ``\\label{}`` command is dropped with the rest of the
    LaTeX layout, so ``meta.label`` is the anchor's only durable home."""
    raw = "{cc}\n\\label{tab:x}\n\\toprule\na & b \\\\\n1 & 2 \\\\\n"
    tc = _flagged_latex_chunk(draft, hub, raw)
    with pytest.raises(BadInput, match="DID convert"):
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="alpha")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\label" not in chunk.text  # raw LaTeX dropped
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta.get("label") == "tab:x"


def test_edit_table_caption_only_edit_on_latex_source_persists_label(
    draft: DraftHandler, hub: Hub
) -> None:
    """``caption=``/``regen=`` alone (no cell=/find=/sub=) on a LaTeX-sourced
    chunk falls through to the markdown-re-derivation path, not the
    in-place patcher — the exact write that used to drop ``\\label``
    silently (gripe 271129). ``meta.label`` now survives even though the
    raw LaTeX text itself is replaced by derived markdown."""
    raw = (
        "{cc}\n\\caption{Yield thresholds}\n\\label{tab:yield}\n"
        "\\toprule\na & b \\\\\n1 & 2 \\\\\n"
    )
    tc = _flagged_latex_chunk(draft, hub, raw)
    draft.edit(id=tc.dc, caption="New legend")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta.get("label") == "tab:yield"
    assert meta.get("caption") == "New legend"


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
    — which patches the raw text and never promotes ``meta.table``."""
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

    # cell= is id-addressed now — A1 refuses and touches nothing (the
    # recovered grid's just-minted ids aren't caller-knowable).
    with pytest.raises(BadInput, match="not a cell address"):
        draft.edit(id=tc.dc, cell="A1", text="one")
    assert not hub.live_store.drafts.draft_chunk_meta(tc.handle).get("table")

    draft.edit(id=tc.dc, find="a", text="one")

    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    # Promoted to canonical — proves the ordinary re-derive path fired
    # (is_latex_source correctly False), not the LaTeX in-place patcher,
    # which never writes meta.table.
    assert meta["table"]["header"] == ["one", "b"]
    assert meta["table"]["rows"] == [["1", "2"]]
    assert all(is_table_id(c["id"]) for c in meta["table"]["columns"])


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


def test_edit_table_latex_cell_converts_full_wrapped_chunk(
    draft: DraftHandler, hub: Hub
) -> None:
    """A well-formed id address on the full float-wrapped LaTeX chunk
    takes the convert-then-refuse path: the parsed grid is promoted to
    canonical (``meta.label`` carried along), the refusal names fresh
    ids, and a retry lands. Raw LaTeX layout — ``\\label``, rules,
    ``\\,`` thin spaces — is dropped by design (spec §13;
    ``find=``/``sub=`` remain for in-place edits)."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    with pytest.raises(BadInput, match="DID convert") as exc:
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="BEACON2")
    assert "cell='" in str(exc.value.next)
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    tbl = meta["table"]
    assert tbl["header"] == ["Role", "$\\lambda$", "Cassette", "Count"]
    assert meta.get("label") == "tab:poc-roles"
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\label" not in chunk.text and "\\begin{tabular}" not in chunk.text

    rows, cols = _table_ids(hub, tc)
    draft.edit(id=tc.dc, cell=f"{rows[4]}.{cols[0]}", text="BEACON2")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta2["table"]["rows"][4][0] == "BEACON2"


def test_edit_table_latex_cell_conversion_flattens_multicolumn_row(
    draft: DraftHandler, hub: Hub
) -> None:
    """The ``\\multicolumn`` summary row has no span in the canonical
    grid — conversion lands it as an ordinary row, short cells padded
    with ``""`` to the table width. That's the documented lossy edge of
    spec §13: the cell content is preserved verbatim, the span is not."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    with pytest.raises(BadInput, match="DID convert"):
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="x")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    rows = meta["table"]["rows"]
    total = next(r for r in rows if r[0] == "Total")
    assert total == ["Total", "5 boxels, 4 roles", "", ""]


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
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="oops", caption="New caption")
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
            cell="r2k7.k5f9",
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
        draft.edit(id=tc.dc, cell="r2k7.k5f9")
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
    ``sub=``-only selector on a LaTeX-sourced chunk), vs.
    ``find_replace_cells`` on the legacy re-derive path (wrong route,
    reached if the ``sub is not None`` routing guard mis-fires) — so
    asserting the ``next=`` text pins the route, not just the exception
    type/message (both routes raise the identically-worded "invalid
    regex ..." BadInput; since gr494831 M3 both carry a ``next=``, and
    they differ: ``_edit_latex_table_in_place`` offers the literal-find
    alternative, the legacy route's says "check the pattern")."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="invalid regex") as exc:
        draft.edit(id=tc.dc, sub={"find": "(unclosed", "replace": "x"})
    assert "no escaping needed" in str(exc.value.next)
    assert "check the pattern" not in str(exc.value.next)
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
    (body, caption, or any trailing note) refuses (zero replacements) —
    the LaTeX-path sibling of the canonical-table ``find=`` no-match
    refusal."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="no cell, caption or note matches"):
        draft.edit(id=tc.dc, sub={"find": "no-such-substring-zzz", "replace": "y"})
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


# ── LaTeX in-place patching: caption/note widening (gr344147) ──────────
#
# The patcher used to scope find=/sub= to the tabular row/cell area only,
# so a \cite/\ref living in the caption or in footnote text trailing
# \end{tabular} could never be reached, even though the renderer
# linkifies that text the same as a cell (docs/backlog/boxel-42995-
# citation-residue.md's three hand edits were the cost).

_POC_ROLES_LATEX_WITH_NOTE = _POC_ROLES_LATEX + (
    "\n\\vspace{2mm}\n"
    "\\footnotesize Values from the 2019 pilot batch\\cite{legacy2019}.\n"
)

_DUAL_MATCH_LATEX = (
    "\\caption{Summary table (see the alpha row).}\n"
    "\\begin{tabular}{ll}\n"
    "\\toprule\n"
    "Label & Value \\\\\n"
    "alpha & 1 \\\\\n"
    "\\bottomrule\n"
    "\\end{tabular}\n"
    "\\footnotesize Note: the alpha row is the baseline.\n"
)


def test_edit_table_latex_find_matches_caption_when_body_has_no_match(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``find=`` that matches nothing in the row/cell body but does
    match the caption (here, the ``\\ref`` target inside
    ``\\caption{}``) is patched in place there instead of refusing."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    draft.edit(id=tc.dc, find="por-summary", text="poc-notes")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\ref{sec:poc-notes}" in chunk.text
    # body/label untouched — the row/cell area never saw this edit
    assert "\\label{tab:poc-roles}" in chunk.text
    assert "XOR & --- &" in chunk.text


def test_edit_table_latex_find_matches_post_tabular_note(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``find=`` that matches nothing in the body or caption but does
    match trailing footnote text after ``\\end{tabular}`` is patched
    there."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX_WITH_NOTE)
    draft.edit(id=tc.dc, find="legacy2019", text="legacy2020")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\cite{legacy2020}" in chunk.text
    # body/caption untouched
    assert "\\label{tab:poc-roles}" in chunk.text
    assert "XOR & --- &" in chunk.text
    assert "PoC circuit roles" in chunk.text


def test_edit_table_latex_find_body_match_wins_over_caption_and_note(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``find=`` present in the body AND the caption AND a trailing
    note replaces only the body occurrence(s) — the row/cell area is
    tried first and, on any match there, wins outright without even
    looking at the caption/note."""
    tc = _flagged_latex_chunk(draft, hub, _DUAL_MATCH_LATEX)
    draft.edit(id=tc.dc, find="alpha", text="beta")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "beta & 1" in chunk.text  # body cell replaced
    assert "see the alpha row" in chunk.text  # caption untouched
    assert "the alpha row is the baseline" in chunk.text  # note untouched


def test_edit_table_latex_find_matches_both_caption_and_note_is_ambiguous(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``find=`` absent from the body but present in BOTH the caption
    and a trailing note refuses as ambiguous rather than guessing which
    one the caller meant — chunk left untouched."""
    tc = _flagged_latex_chunk(draft, hub, _DUAL_MATCH_LATEX)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="ambiguous"):
        draft.edit(id=tc.dc, find="alpha row", text="beta row")
    after = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert after is not None
    assert after.text == before.text


def test_edit_table_latex_find_no_match_anywhere_refuses(
    draft: DraftHandler, hub: Hub
) -> None:
    """A ``find=`` absent from the body, caption, AND any trailing note
    refuses with the widened error message naming all three."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX_WITH_NOTE)
    before = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert before is not None
    with pytest.raises(BadInput, match="no cell, caption or note matches"):
        draft.edit(id=tc.dc, find="no-such-substring-zzz", text="y")
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

    rows, cols = _table_ids(hub, tc)
    r = draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[0]}", text="Ge", dry_run=True)
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


# ── stable ids in get + ~sel reads (spreadsheet-tables 1a rework) ──────


def _preschema_table_chunk(draft: DraftHandler, hub: Hub, slug: str) -> Any:
    """A table chunk stripped to the pre-schema shape — legacy
    ``meta.table = {header, rows}`` AND plain-GFM text with no ``ids:``
    trailer, the shape every table written before the column schema
    still has. BOTH halves are stripped: an ``ids:`` line left in the
    stored text would make a "get shows the ids" check pass before any
    mint (the vacuous-fixture hole must-fix 1 slipped through)."""
    proj = _proj(hub)
    draft.put(id=slug, title="T", project=proj)
    draft.put(
        id=slug,
        chunk_kind="table",
        table={
            "header": ["element", "gap_eV"],
            "rows": [["Si", 1.12], ["Ge", 0.67]],
        },
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    tc = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "table"
    )
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


def test_get_table_chunk_shows_ids_line_then_cell_edit_lands(
    draft: DraftHandler, hub: Hub
) -> None:
    """The skill/next= promise made true: ``get`` renders the row/col ids
    on an ``ids:`` line under the grid, and an id taken straight from
    that output addresses ``cell=``."""
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)
    body = draft.get(id=tc.dc).body
    assert f"ids: rows {' '.join(rows)}; cols {' '.join(cols)}" in body

    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="1.523")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta["table"]["rows"][0][1] == 1.523


def test_table_to_markdown_bare_dict_stays_plain() -> None:
    """A ``{header, rows}`` table with no ids renders plain GFM — no
    ``ids:`` trailer (pre-schema read unchanged)."""
    md = table_to_markdown({"header": ["a"], "rows": [[1]]})
    assert "ids:" not in md
    assert md == "| a |\n| --- |\n| 1 |"


def test_edit_table_cell_on_preschema_mints_then_refuses_with_ids(
    draft: DraftHandler, hub: Hub
) -> None:
    """A pre-schema ``{header, rows}`` table mints ids on its first write
    touch — even a *refusing* ``cell=`` leaves them stored (and shown by
    get), so the retry with a real address lands instead of dead-ending."""
    tc = _preschema_table_chunk(draft, hub, "d")
    assert "columns" not in hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    # pre-mint the read has no ids line at all — the check below fails
    # here, so it can only pass because the mint really landed
    assert "ids: rows" not in draft.get(id=tc.dc).body

    with pytest.raises(BadInput, match="no row id") as exc:
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="x")
    assert "just minted" in str(exc.value)

    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert all(is_table_id(c["id"]) for c in meta["table"]["columns"])
    assert all(is_table_id(r["id"]) for r in meta["table"]["rowmeta"])
    # get shows the minted ids — the SAME ids meta stores, rendered from
    # meta.table at read time (the refusal's next= is true)
    rows, cols = _table_ids(hub, tc)
    assert (
        f"ids: rows {' '.join(rows)}; cols {' '.join(cols)}" in draft.get(id=tc.dc).body
    )

    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="1.523")
    meta2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert meta2["table"]["rows"][0][1] == 1.523
    assert meta2["table"]["rowmeta"][0]["id"] == rows[0]  # ids stable


def test_edit_table_caption_only_mints_preschema_table(
    draft: DraftHandler, hub: Hub
) -> None:
    """``caption=`` alone on a pre-schema table persists the normalized
    table — ids land on the metadata-only edit too (no "edit it once"
    dead end)."""
    tc = _preschema_table_chunk(draft, hub, "d")
    assert "ids: rows" not in draft.get(id=tc.dc).body  # nothing minted yet
    draft.edit(id=tc.dc, caption="New legend")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert all(is_table_id(c["id"]) for c in meta["table"]["columns"])
    assert all(is_table_id(r["id"]) for r in meta["table"]["rowmeta"])
    assert meta.get("caption") == "New legend"
    rows, cols = _table_ids(hub, tc)
    assert (
        f"ids: rows {' '.join(rows)}; cols {' '.join(cols)}" in draft.get(id=tc.dc).body
    )


def test_edit_table_cell_a1_on_preschema_mints_and_refuses(
    draft: DraftHandler, hub: Hub
) -> None:
    """An A1 address on a pre-schema table still refuses the id-form —
    but the mint lands anyway, so get/cell= work from there."""
    tc = _preschema_table_chunk(draft, hub, "d")
    with pytest.raises(BadInput, match="not a cell address"):
        draft.edit(id=tc.dc, cell="B2", text="x")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert all(is_table_id(c["id"]) for c in meta["table"]["columns"])


def test_table_select_reads_row_col_cell_and_range(
    draft: DraftHandler, hub: Hub
) -> None:
    """``dc<N>~<sel>`` end-to-end: one cell, one row, one column, a row
    range, and a dangling id — all resolved against the canonical ids."""
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)

    cell = draft.get(id=f"{tc.dc}~{rows[0]}.{cols[1]}")
    assert "1.12" in cell.body
    row = draft.get(id=f"{tc.dc}~{rows[1]}")
    assert "gap_eV=0.67" in row.body and "element=Ge" in row.body
    col = draft.get(id=f"{tc.dc}~{cols[0]}")
    assert f"{rows[0]}\tSi" in col.body and f"{rows[1]}\tGe" in col.body
    rng = draft.get(id=f"{tc.dc}~{rows[0]}..{rows[1]}")
    assert f"{rows[0]}\tSi\t1.12" in rng.body
    with pytest.raises(NotFound, match="dangling id"):
        draft.get(id=f"{tc.dc}~zzzz")
    with pytest.raises(NotFound, match="dangling cell id"):
        draft.get(id=f"{tc.dc}~{rows[0]}.zzzz")


def test_table_select_on_preschema_names_a_minting_edit(
    draft: DraftHandler, hub: Hub
) -> None:
    """``~sel`` on a table with no ids points at an edit that mints —
    and minting actually makes the same read work."""
    tc = _preschema_table_chunk(draft, hub, "d")
    with pytest.raises(BadInput, match="no stable row/column ids") as exc:
        draft.get(id=f"{tc.dc}~r2k7")
    assert "mints" in str(exc.value.next)
    draft.edit(id=tc.dc, caption="mints me")
    rows, _ = _table_ids(hub, tc)
    assert rows[0] in draft.get(id=f"{tc.dc}~{rows[0]}").body


def test_edit_table_latex_a1_refuses_before_converting(
    draft: DraftHandler, hub: Hub
) -> None:
    """An A1/dict ``cell=`` on a LaTeX-sourced chunk refuses on the cheap
    syntax gate — BEFORE the lossy conversion writes, so the raw LaTeX
    (layout, ``\\label``, flag) is fully intact."""
    tc = _flagged_latex_chunk(draft, hub, _POC_ROLES_LATEX)
    with pytest.raises(BadInput, match="not a cell address"):
        draft.edit(id=tc.dc, cell="B2", text="x")
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    assert not meta.get("table")  # nothing was converted
    assert meta["flag"] == "needs-table-review"
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None
    assert "\\label{tab:poc-roles}" in chunk.text  # raw LaTeX intact

    with pytest.raises(BadInput, match="must be a"):
        draft.edit(id=tc.dc, cell=cast(Any, {"row": 6, "col": 1}), text="x")
    assert not hub.live_store.drafts.draft_chunk_meta(tc.handle).get("table")


def test_edit_table_whole_replace_retires_old_ids(
    draft: DraftHandler, hub: Hub
) -> None:
    """``table=`` re-mints the grid — the replaced table's live ids (and
    its retired pool) merge into the new ``retired_ids`` so a deleted id
    can never resurface."""
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)
    draft.edit(
        id=tc.dc,
        table={"header": ["a", "b"], "rows": [[1, 2], [3, 4], [5, 6]]},
    )
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)
    tbl = meta["table"]
    new_ids = {e["id"] for e in [*tbl["columns"], *tbl["rowmeta"]]}
    assert new_ids.isdisjoint({*rows, *cols})
    assert set(tbl["retired_ids"]) >= {*rows, *cols}
    # a retired id reports dangling, not a re-purposed row
    with pytest.raises(NotFound, match="dangling id"):
        draft.get(id=f"{tc.dc}~{rows[0]}")


def test_edit_table_replace_keeps_supplied_ids(draft: DraftHandler, hub: Hub) -> None:
    """``table={**meta.table, rows: …}`` round-trip: re-supplying the
    existing columns/rowmeta keeps every id live (an edit rewrites
    cells, not identities) — only ids the caller dropped retire. The
    retired pool itself is never subtracted: re-supplying a retired id
    still refuses."""
    tc = _seed_table(draft, hub)
    meta = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    rows, cols = _table_ids(hub, tc)
    new_rows = [list(r) for r in meta["rows"]]
    new_rows[0][1] = 9.99
    draft.edit(id=tc.dc, table={**meta, "rows": new_rows})
    tbl = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    assert [r["id"] for r in tbl["rowmeta"]] == rows
    assert [c["id"] for c in tbl["columns"]] == cols
    assert tbl["rows"][0][1] == 9.99
    # no live id landed in the retired pool
    assert not ({*rows, *cols} & set(tbl.get("retired_ids") or []))

    # a dropped row id retires; re-supplying it refuses — the retired
    # pool is subtracted by supplied LIVE ids, never resurrected
    smaller = {**meta, "rows": new_rows[:1], "rowmeta": meta["rowmeta"][:1]}
    draft.edit(id=tc.dc, table=smaller)
    tbl2 = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    assert rows[1] in tbl2["retired_ids"]
    resurrect = {
        **tbl2,
        "rows": [*tbl2["rows"], ["Ge", 0.67]],
        "rowmeta": [*tbl2["rowmeta"], {"id": rows[1]}],
    }
    with pytest.raises(BadInput, match="already taken"):
        draft.edit(id=tc.dc, table=resurrect)


def test_delete_with_table_selector_refuses_naming_selector(
    draft: DraftHandler, hub: Hub
) -> None:
    """``delete(id='dc<N>~<rowid>')`` must not read the selector as a
    draft slug — it refuses naming the selector form."""
    tc = _seed_table(draft, hub)
    rows, _ = _table_ids(hub, tc)
    with pytest.raises(BadInput, match="row/column selector"):
        draft.delete(id=f"{tc.dc}~{rows[0]}")
    chunk = hub.live_store.drafts.get_draft_chunk(tc.dc)
    assert chunk is not None and not chunk.retired


def test_edit_table_replace_repairs_malformed_legacy_table(
    draft: DraftHandler, hub: Hub
) -> None:
    """A malformed legacy ``meta.table`` (``rows=[[1]]`` under a 2-col
    header — the shape the id backfill counts as ``errors``) must not
    block ``table=``, the only repair path: the lazy mint is skipped
    when the whole grid is replaced. Every other edit still refuses
    honestly on the broken shape."""
    tc = _preschema_table_chunk(draft, hub, "d")
    hub.live_store.drafts.patch_chunk_meta(
        tc.handle, {"table": {"header": ["a", "b"], "rows": [[1]]}}
    )
    # non-replace edits keep refusing honestly on the ragged grid
    with pytest.raises(BadInput, match="has 1 cells, header has 2"):
        draft.edit(id=tc.dc, caption="x")
    with pytest.raises(BadInput, match="has 1 cells, header has 2"):
        draft.edit(id=tc.dc, cell="r2k7.k5f9", text="x")
    # table= replaces the grid outright — never reads the old shape
    draft.edit(id=tc.dc, table={"header": ["a", "b"], "rows": [[1, 2]]})
    tbl = hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]
    assert tbl["rows"] == [[1, 2]]
    assert [c["name"] for c in tbl["columns"]] == ["a", "b"]
    assert all(is_table_id(c["id"]) for c in tbl["columns"])
    assert all(is_table_id(r["id"]) for r in tbl["rowmeta"])
    # and the repaired table reads/edits like any canonical one
    rows, cols = _table_ids(hub, tc)
    assert "ids: rows" in draft.get(id=tc.dc).body
    draft.edit(id=tc.dc, cell=f"{rows[0]}.{cols[1]}", text="9")
    assert hub.live_store.drafts.draft_chunk_meta(tc.handle)["table"]["rows"][0][1] == 9


def test_dispatch_routes_table_selector_to_draft_handler(
    draft: DraftHandler, hub: Hub, runtime_with_store: Any
) -> None:
    """``get(id='dc<N>~<sel>')`` with no kind= routes through
    ``runtime/dispatch._maybe_route_draft_chunk`` to the draft handler —
    a bare row id, a cell address and a range all reach
    ``_render_table_select``, not a kind-missing error."""
    tc = _seed_table(draft, hub)
    rows, cols = _table_ids(hub, tc)
    out = runtime_with_store.dispatch("get", {"id": f"{tc.dc}~{rows[0]}.{cols[1]}"})
    assert "[error:" not in out
    assert "1.12" in out
    out = runtime_with_store.dispatch("get", {"id": f"{tc.dc}~{rows[1]}"})
    assert "[error:" not in out
    assert "gap_eV=0.67" in out and "element=Ge" in out
    out = runtime_with_store.dispatch("get", {"id": f"{tc.dc}~{rows[0]}..{rows[1]}"})
    assert "[error:" not in out
    assert f"{rows[0]}\tSi\t1.12" in out


def test_normalize_table_retired_ids_must_be_a_list() -> None:
    with pytest.raises(BadInput, match="retired_ids must be a list"):
        normalize_table({"header": ["a"], "rows": [[1]], "retired_ids": "abcd"})


# ── selector grammar acceptance (spec §2: ~N/~pN stay unparsed) ────────


def test_handle_parse_table_selector_and_legacy_shapes() -> None:
    from precis.utils import handle_registry as hr

    assert hr.parse("dc5~r2k7") == ("draft", True, 5)
    assert hr.parse("dc5~r2k7.k5f9") == ("draft", True, 5)
    assert hr.parse("dc5~r2k7..s3ab") == ("draft", True, 5)
    # digit-first and p-first "ids" are NOT table ids — the grammar owns
    # ~N and ~pN already; both fail to parse, exactly as before 1a.
    assert hr.parse("dc5~4821") is None
    assert hr.parse("dc5~p12") is None
    # a record handle takes no selector; a word-shaped sel on a chunk
    # handle parses (see the persist-site guards for why it must not be
    # stored)
    assert hr.parse("pa5~r2k7") is None
    assert hr.split_selector("dc5~r2k7") == ("dc5", "r2k7")
    assert hr.split_selector("dc5") == ("dc5", None)


def test_bracket_ref_pattern_sel_group() -> None:
    from precis.utils import mentions

    m = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[dc5~r2k7.k5f9]")
    assert m is not None and m.group("sel") == "~r2k7.k5f9"
    m2 = mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[dc5]")
    assert m2 is not None and m2.group("sel") is None
    # a non-id suffix never binds the sel group
    assert mentions.BARE_BRACKET_REF_PATTERN.fullmatch("[dc5~xy]") is None


def test_rung_and_supply_evidence_reject_selector_handles() -> None:
    """``is_well_formed`` now accepts ``dc5~data`` — evidence lists that
    persist the raw string must refuse the selector form instead of
    storing it."""
    from precis.handlers.quest import _check_supply_meta
    from precis.todo.guards import check_rung_meta

    rung: dict[str, Any] = {
        "pathway": "qu1",
        "consumes": [],
        "produces": [
            {
                "capability": "qu1",
                "key": "k",
                "value": 1,
                "evidence": ["dc5~data"],
            }
        ],
    }
    with pytest.raises(BadInput, match="evidence"):
        check_rung_meta({"rung": rung})
    with pytest.raises(BadInput, match="evidence"):
        _check_supply_meta({"supply": {"k": {"value": 1, "evidence": ["dc5~data"]}}})
    # plain handles still pass
    rung["produces"][0]["evidence"] = ["fi1"]
    check_rung_meta({"rung": rung})
    _check_supply_meta({"supply": {"k": {"value": 1, "evidence": ["fi1"]}}})
