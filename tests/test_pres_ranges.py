"""Presentation ranges must reach every slide exactly once (gr561484)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, ResultBudgetExceeded
from precis.handlers.paper import _parse_paper_id
from precis.handlers.presentation import PresentationHandler, _parse_pres_id
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils.pres_text import strip_latexit


def _register_budget_deck(runtime: PrecisRuntime, texts: list[str]) -> None:
    """Pin pr123 so the regression exercises the exact byte boundary."""
    ref = SimpleNamespace(id=123, slug="deck", title="Deck")
    blocks = [
        SimpleNamespace(ord=i, text=text, chunk_kind="pres_slide")
        for i, text in enumerate(texts)
    ]

    def get_ref(*, kind: str, id: str | int) -> SimpleNamespace:
        assert kind == "pres"
        assert id in (123, "deck")
        return ref

    def list_chunks_for_ref(
        ref_id: int, *, pos_range: tuple[int, int] | None = None
    ) -> list[SimpleNamespace]:
        assert ref_id == 123
        return [
            block
            for block in blocks
            if pos_range is None or pos_range[0] <= block.ord <= pos_range[1]
        ]

    store = cast(
        Store,
        SimpleNamespace(
            get_ref=get_ref,
            chunks=SimpleNamespace(list_chunks_for_ref=list_chunks_for_ref),
        ),
    )
    handler = PresentationHandler(hub=Hub(store=store))
    runtime.hub.register_handler("pres", handler)


@pytest.mark.parametrize("fold", [False, True])
def test_runtime_id_hint_preserves_whole_slide_and_continuation(
    runtime_stateless: PrecisRuntime, monkeypatch: pytest.MonkeyPatch, fold: bool
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "600")
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    _register_budget_deck(runtime_stateless, ["x" * 450] * 3)
    body, is_error = runtime_stateless.dispatch_with_status(
        "get", {"kind": "pres", "id": "pr123/full\t8kB"}, fold=fold
    )
    assert not is_error, body
    assert len(body.encode("utf-8")) <= 600
    assert re.findall(r"^## slide ~(\d+)$", body, re.M) == ["0"]
    assert "## slide ~0\n" + "x" * 450 + "\n\n" in body
    assert "next: get(kind='pres', id='pr123~1..2')" in body
    assert "[info] id 'pr123/full\\t8kB' read as 'pr123/full'" in body
    assert body.count("… truncated:") == 1
    assert "search(" not in body


def test_runtime_fold_false_ignores_optional_context_cap(
    runtime_stateless: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "400")
    monkeypatch.delenv("PRECIS_MAX_BODY_BYTES", raising=False)
    _register_budget_deck(runtime_stateless, ["x" * 500])
    args: dict[str, Any] = {"kind": "pres", "id": "deck/full"}
    body = runtime_stateless.dispatch("get", args, fold=False)
    assert body == "# pr123 - full\n_Deck_\n\n## slide ~0\n" + "x" * 500
    # The same runtime still enforces an opted-in context cap; the
    # preceding unfolded call must not leak its policy into this call.
    folded, is_error = runtime_stateless.dispatch_with_status(
        "get", {"kind": "pres", "id": "deck/full"}, fold=True
    )
    assert is_error, folded
    assert "ResultBudgetExceeded" in folded


def test_runtime_hint_repaginating_previously_complete_deck(
    runtime_stateless: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "600")
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    _register_budget_deck(runtime_stateless, ["x" * 260] * 2)
    body, is_error = runtime_stateless.dispatch_with_status(
        "get", {"kind": "pres", "id": "pr123/full\t8kB"}, fold=True
    )
    assert not is_error, body
    assert len(body.encode("utf-8")) <= 600
    assert re.findall(r"^## slide ~(\d+)$", body, re.M) == ["0"]
    assert "## slide ~0\n" + "x" * 260 + "\n\n" in body
    assert "next: get(kind='pres', id='pr123~1..1')" in body
    assert "read as 'pr123/full'" in body


@pytest.mark.parametrize("selector", ["0..3", "3-5", "0..0", "136..137", "-1", "4"])
def test_selector_matches_paper(selector: str) -> None:
    raw = f"deck~{selector}"
    slug, selected, view = _parse_pres_id(raw)
    if isinstance(selected, int):
        selected = (selected, selected)
    assert (slug, selected, view) == _parse_paper_id(raw)


@pytest.mark.parametrize("selector", ["5..3", "-1..2", "1..", "x", "1,2", ""])
def test_selector_errors_match_paper(selector: str) -> None:
    raw = f"deck~{selector}"
    with pytest.raises(BadInput) as paper_error:
        _parse_paper_id(raw)
    with pytest.raises(BadInput) as pres_error:
        _parse_pres_id(raw)
    assert str(pres_error.value) == str(paper_error.value)


@pytest.fixture
def fixture_deck(store: Store) -> tuple[str, list[str]]:
    ref = store.insert_ref(kind="pres", slug="range-deck", title="Range fixture")
    # Multibyte text, varying lengths and a blank slide exercise byte costs
    # and boundaries; reference-slide labels at the tail pin the original bug.
    texts = [f"BEGIN-{i}\n" + "é→ " * (45 + i % 7) + f"\nEND-{i}" for i in range(138)]
    texts[17] = ""
    texts[-1] = "BEGIN-137\nReferences at the tail.\nEND-137"
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=text, meta={"chunk_kind": "pres_slide"})
            for i, text in enumerate(texts)
        ],
    )
    return f"pr{ref.id}", texts


@pytest.mark.parametrize(
    ("suffix", "view", "start", "end", "frame_cap", "context_cap"),
    [
        ("/full", None, 0, 137, 24576, 0),
        ("/full", None, 0, 137, 1200, 0),
        ("", "full", 0, 137, 1200, 800),
        ("~12..130", None, 12, 130, 1200, 0),
        ("~12..999", None, 12, 137, 1200, 0),
        ("~0..137", None, 0, 137, 1200, 100),
    ],
)
def test_runtime_continuation_walk_has_every_slide_once(
    runtime_with_store: PrecisRuntime,
    fixture_deck: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    view: str | None,
    start: int,
    end: int,
    frame_cap: int,
    context_cap: int,
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", str(frame_cap))
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", str(context_cap))
    handle, texts = fixture_deck
    address = handle + suffix
    seen: list[int] = []
    page_count = 0
    while page_count <= len(texts):
        args = {"kind": "pres", "id": address}
        if view is not None:
            args["view"] = view
        body, is_error = runtime_with_store.dispatch_with_status("get", args, fold=True)
        assert not is_error, body
        cap = (
            min(frame_cap, context_cap)
            if context_cap and (view is not None or "~" not in address)
            else frame_cap
        )
        assert len(body.encode("utf-8")) <= cap
        headings = list(re.finditer(r"^## slide ~(\d+)\n", body, re.M))
        assert headings, body
        positions = [int(heading.group(1)) for heading in headings]
        assert positions == list(range(start + len(seen), positions[-1] + 1))
        # Check whole slide contents, not only headings: a generic byte cut
        # can retain a heading while losing its final reference sentence.
        for heading, pos in zip(headings, positions, strict=True):
            assert body[heading.end() :].startswith(texts[pos])
        seen.extend(positions)
        page_count += 1
        continuation = re.search(
            r"^next: get\(kind='pres', id='([^']+)~(\d+)\.\.(\d+)'\)$",
            body,
            re.M,
        )
        if continuation is None:
            assert "truncated" not in body
            break
        assert body.count("… truncated:") == 1
        assert continuation.group(1) == handle
        assert int(continuation.group(2)) == positions[-1] + 1
        assert int(continuation.group(3)) == end
        address = f"{handle}~{continuation.group(2)}..{continuation.group(3)}"
        view = None
    else:
        pytest.fail("continuation did not reach the tail")
    assert page_count > 1
    assert seen == list(range(start, end + 1))


def test_range_clamps_and_single_block_shape_is_preserved(
    store: Store, fixture_deck: tuple[str, list[str]]
) -> None:
    handle, texts = fixture_deck
    handler = PresentationHandler(hub=Hub(store=store))
    body = handler.get(id=f"{handle}~136..999").body
    assert re.findall(r"^## slide ~(\d+)$", body, re.M) == ["136", "137"]
    assert texts[-1] in body
    assert "next:" not in body
    single = handler.get(id=f"{handle}~137").body
    assert "(slide)" in single
    assert texts[-1] in single
    degenerate = handler.get(id=f"{handle}~137..137").body
    assert "## slide ~137\n" in degenerate


def test_missing_range_reports_valid_bounds(
    store: Store, fixture_deck: tuple[str, list[str]]
) -> None:
    handle, _ = fixture_deck
    handler = PresentationHandler(hub=Hub(store=store))
    with pytest.raises(NotFound, match=r"138 blocks, valid: ~0\.\.137"):
        handler.get(id=f"{handle}~138..150")
    ref = store.insert_ref(kind="pres", slug="empty-range-deck", title="Empty")
    assert handler.get(id=f"pr{ref.id}/full").body.endswith("no blocks")
    with pytest.raises(NotFound, match="has no body blocks"):
        handler.get(id=f"pr{ref.id}~0..5")


def test_continuation_uses_actual_sparse_positions(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "600")
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    ref = store.insert_ref(kind="pres", slug="sparse-deck", title="Sparse")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text="x" * 300, meta={"chunk_kind": "pres_slide"})
            for i in (2, 7, 11)
        ],
    )
    handler = PresentationHandler(hub=Hub(store=store))
    body = handler.get(id=f"pr{ref.id}/full").body
    assert f"next: get(kind='pres', id='pr{ref.id}~7..11')" in body
    body = handler.get(id=f"pr{ref.id}~7..11").body
    assert f"next: get(kind='pres', id='pr{ref.id}~11..11')" in body
    assert "truncated" not in handler.get(id=f"pr{ref.id}~11..11").body


def test_oversize_slide_refuses_instead_of_losing_content(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    handler = PresentationHandler(hub=Hub(store=store))
    handler.put(id="oversize-deck", text="x" * 800)
    with pytest.raises(ResultBudgetExceeded, match="increase PRECIS_MAX_BODY_BYTES"):
        handler.get(id="oversize-deck/full")
    with pytest.raises(ResultBudgetExceeded):
        handler.get(id="oversize-deck~0..0")


def test_latexit_filter_preserves_everything_outside_complete_elements() -> None:
    blob = '<latexit sha1_base64="YWJj">\nYWJjZA==\n</latexit>'
    assert strip_latexit(f"before{blob}after{blob}end") == "beforeafterend"
    untouched = "<latex>real formula</latex> <latexit-note>note</latexit-note>"
    untouched += ' <latexit sha1_base64="unfinished"> text'
    assert strip_latexit(untouched) == untouched
    assert strip_latexit(f"<latexit-note>keep</latexit-note>{blob}") == (
        "<latexit-note>keep</latexit-note>"
    )


@pytest.mark.parametrize("suffix", ["/full", "~0..0", "~0"])
def test_legacy_latexit_filtered_before_budget_without_rewriting_chunks(
    store: Store, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "600")
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    ref = store.insert_ref(kind="pres", slug="legacy-deck", title="Legacy")
    blob = '<latexit sha1_base64="YWJj">' + "YWJj" * 1000 + "</latexit>"
    text = f"The real sentence before.{blob}The real sentence after."
    chunks = store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "pres_slide"})]
    )
    handler = PresentationHandler(hub=Hub(store=store))
    body = handler.get(id=f"pr{ref.id}{suffix}").body
    assert "The real sentence before.The real sentence after." in body
    assert "latexit" not in body
    assert "YWJj" not in body
    assert "truncated" not in body
    assert store.chunks.list_chunks_for_ref(ref.id) == chunks
