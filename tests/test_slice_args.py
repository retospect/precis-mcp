"""The canonical slice pair ``page``/``page_size`` and its aliases (gr514469).

Args-facing slicing was spelled ``limit``/``offset``/``page``/``page_size``
across verbs; the canonical pair is now ``page`` (1-based) + ``page_size``
everywhere, with ``limit`` read as ``page_size`` and ``offset`` as an
absolute 0-based row index for one release. A call that uses an alias
still runs, but the reply leads with ``interpreted: …`` naming the
reading (:mod:`precis.handlers._interpret`), so the agent learns the new
spelling from the answer itself rather than from a refusal.

The unit half pins :func:`~precis.handlers._slice_args.slice_window`'s
contract — the resolution is shared, so a divergence between two views
would live in the helper. The handler half proves equivalence end to
end: the same window under both spellings returns the same rows, and
only the alias call carries the ``interpreted:`` line.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._slice_args import slice_window
from precis.handlers.draft import DraftHandler
from precis_se.handler import SeHandler


@pytest.fixture
def se(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


# ── slice_window: the four spellings resolve to one window ─────────────


def test_no_args_slices_from_the_top_with_no_cap() -> None:
    assert slice_window(None, where="t") == (0, None, [])
    assert slice_window({}, where="t") == (0, None, [])


def test_canonical_pair_needs_no_interpreted_line() -> None:
    assert slice_window({"page": 3, "page_size": 40}, where="t") == (80, 40, [])


def test_bare_page_uses_the_site_default_window() -> None:
    """``default_size`` sizes ``page=`` arithmetic only: a bare
    ``page=2`` on a view whose own cap is 50 asks for rows 50–99."""
    assert slice_window({"page": 2}, where="t", default_size=50) == (50, None, [])


def test_page_without_any_window_refuses() -> None:
    with pytest.raises(BadInput, match="no window"):
        slice_window({"page": 2}, where="t")


def test_limit_reads_as_page_size_and_says_so() -> None:
    offset, size, readings = slice_window({"limit": 40}, where="t")
    assert (offset, size) == (0, 40)
    assert readings == [
        "'limit' read as 'page_size' — the deprecated alias; 'page_size' is canonical"
    ]


def test_offset_reads_as_an_absolute_index_and_says_so() -> None:
    offset, size, readings = slice_window({"offset": 100}, where="t")
    assert (offset, size) == (100, None)
    assert readings == [
        "'offset' read as an absolute row index — the deprecated "
        "alias; 'page' is canonical"
    ]


def test_both_size_spellings_agreeing_is_one_window() -> None:
    """``page_size`` + ``limit`` with the same value is unambiguous —
    it runs, and the ``interpreted:`` line still names the alias."""
    offset, size, readings = slice_window(
        {"page_size": 40, "limit": 40, "page": 2}, where="t"
    )
    assert (offset, size) == (40, 40)
    assert len(readings) == 1 and "'limit'" in readings[0]


def test_conflicting_sizes_refuse() -> None:
    with pytest.raises(BadInput, match="conflicting slice sizes"):
        slice_window({"page_size": 40, "limit": 50}, where="t")


def test_page_and_offset_together_refuse() -> None:
    with pytest.raises(BadInput, match="both 'page' and 'offset'"):
        slice_window({"page": 2, "offset": 10, "page_size": 40}, where="t")


@pytest.mark.parametrize(
    "args",
    [
        {"offset": -1},
        {"page": 0},
        {"page_size": -1},
        {"limit": -2},
        {"offset": 1.5},
        {"offset": True},
        {"page_size": False},
        {"limit": "x"},
    ],
)
def test_non_whole_or_negative_slice_args_refuse(args: dict[str, Any]) -> None:
    with pytest.raises(BadInput) as exc:
        slice_window(args, where="t")
    assert exc.value.next


def test_coerce_accepts_digit_strings_for_query_string_sites() -> None:
    """Views reached through an ``id='/logs?…'`` query string get
    strings for every value; ``coerce`` reads ``"40"`` but still
    refuses anything that isn't a whole number."""
    assert slice_window({"limit": "40"}, where="t", coerce=True)[1] == 40
    with pytest.raises(BadInput):
        slice_window({"limit": "4.5"}, where="t", coerce=True)
    with pytest.raises(BadInput):
        slice_window({"limit": "40"}, where="t")


# ── handler level: both spellings return the same rows ─────────────────


_OPS = [
    {"op": "add_block", "name": f"b{i}", "desc": f"block {i} " + "x" * 40}
    for i in range(90)
]


def _ops_window(body: str) -> list[dict]:
    return list(json.loads(body.split("```json")[1].split("```")[0])["ops"])


def test_se_ops_aliases_return_the_same_window(se: SeHandler) -> None:
    """The gr514469 promise end to end: ``offset``/``limit`` and
    ``page``/``page_size`` slice the identical window; only the alias
    call announces itself."""
    se.put(id="sliced", text=json.dumps({"ops": _OPS}))
    aliased = se.get(id="sliced", view="ops", args={"offset": 40, "limit": 20}).body
    canonical = se.get(id="sliced", view="ops", args={"page": 3, "page_size": 20}).body
    assert _ops_window(aliased) == _ops_window(canonical)
    assert aliased.startswith("interpreted: 'limit' read as 'page_size'")
    assert "'offset' read as an absolute row index" in aliased.splitlines()[0]
    assert not canonical.startswith("interpreted:")


def test_draft_outline_aliases_return_the_same_rows(
    draft: DraftHandler, hub: Hub
) -> None:
    """Same contract on a second surface: ``args={'limit','offset'}``
    and ``args={'page_size','page'}`` page the outline identically."""
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    th = hub.live_store.drafts.reading_order(ref.id)[0].handle
    for i in range(6):
        draft.put(
            id="nt",
            chunk_kind="paragraph",
            text=f"para{i}",
            at={"after": "¶" + th},
        )
    aliased = draft.get(id="nt", args={"limit": 2, "offset": 2}).body
    canonical = draft.get(id="nt", args={"page_size": 2, "page": 2}).body
    assert aliased.startswith("interpreted:")
    assert not canonical.startswith("interpreted:")
    # The interpreted: note differs by spelling; the payload under it
    # must not.
    assert aliased.split("\n", 1)[1] == canonical
    assert "para4" in canonical and "para3" in canonical and "para5" not in canonical
