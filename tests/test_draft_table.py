"""Data/table chunks (ADR 0035 §1, build step 1) — canonical ``meta.table``
JSON + derived markdown ``text``, inert ``meta.regen``. No execution."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.utils.table_data import (
    normalize_table,
    parse_markdown_table,
    table_payload,
    table_to_markdown,
)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


def _table_chunk(hub: Hub, slug: str) -> Any:
    """The draft's table chunk — carries both ``.dc`` (the agent-facing
    ADR-0036 ``dc<id>`` address) and ``.handle`` (the legacy base-58 anchor
    the low-level store ops still key on)."""
    ref = hub.store.get_ref(kind="draft", id=slug)
    order = hub.store.reading_order(ref.id)
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
    chunk = hub.store.get_draft_chunk(tc.dc)  # dc<id> resolves
    # text is the derived markdown (caption + table), one block
    assert chunk.text.startswith("**Band gaps**\n| element | gap_eV |")
    assert "| Si | 1.12 |" in chunk.text and "\n\n" not in chunk.text
    # canonical data + provenance live in meta, numbers preserved
    meta = hub.store.draft_chunk_meta(tc.handle)
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
    chunk = hub.store.get_draft_chunk(tc.dc)
    assert chunk.text.startswith("**C**\n")  # caption preserved
    assert "| 3 |" in chunk.text
    assert hub.store.draft_chunk_meta(tc.handle)["table"]["rows"] == [[1], [2], [3]]

    # regen-only edit keeps the data, restamps provenance
    draft.edit(id=tc.dc, regen={"source": "manual"})
    assert hub.store.draft_chunk_meta(tc.handle)["regen"] == {"source": "manual"}
    assert hub.store.draft_chunk_meta(tc.handle)["table"]["rows"] == [[1], [2], [3]]


def test_edit_table_on_non_table_chunk_errors(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="d", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="d")
    draft.put(id="d", chunk_kind="paragraph", text="prose", at={"last": True})
    para = hub.store.reading_order(ref.id)[-1]  # the paragraph just added
    with pytest.raises(BadInput, match="only to a chunk_kind='table'"):
        draft.edit(id=para.dc, table={"header": ["x"], "rows": [[1]]})
