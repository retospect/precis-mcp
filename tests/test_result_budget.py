"""Result budgeting: fitted head + outline + targeted footer, never a cursor."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis._result_budget import fit_result_body, row_size_cell, size_cell
from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.store.store import Store
from precis.store.types import ChunkInsert


def test_small_result_passes() -> None:
    body = "small"
    assert (
        fit_result_body(
            body,
            verb="get",
            kind="skill",
            context_limited=True,
            targeted_hint=None,
            read="single",
        )
        == body
    )


def test_unset_context_cap_returns_13kb_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr540851: a ~13 kB body is under the transport cap — returned whole,
    no 4 kB context cut, even on a folded agent-facing read."""
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    monkeypatch.delenv("PRECIS_MAX_BODY_BYTES", raising=False)
    body = "\n".join(f"line {i:05d} of prose, padded out" for i in range(400))
    assert 12_000 < len(body.encode("utf-8")) < 14_000
    for read in ("single", "list", "extent"):
        out = fit_result_body(
            body,
            verb="get",
            kind="memory",
            context_limited=True,
            targeted_hint=None,
            read=read,
        )
        assert out == body


def test_unset_context_cap_over_transport_cap_returns_head_outline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr540851: above DEFAULT_MAX_BODY_BYTES (24576) the read returns the
    head + outline + next:, never an error."""
    from precis._result_budget import DEFAULT_MAX_BODY_BYTES

    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    monkeypatch.delenv("PRECIS_MAX_BODY_BYTES", raising=False)
    sections = [
        f"## part {n}\n" + "\n".join(f"row {n}.{i} text" for i in range(150))
        for n in range(12)
    ]
    body = "\n\n".join(sections)
    assert len(body.encode("utf-8")) >= DEFAULT_MAX_BODY_BYTES + 1
    out = fit_result_body(
        body,
        verb="get",
        kind="memory",
        context_limited=True,
        targeted_hint="get(kind='memory', id='me1~part-11')",
        read="single",
    )
    assert len(out.encode("utf-8")) <= DEFAULT_MAX_BODY_BYTES
    assert out.startswith("## part 0\nrow 0.0 text")
    assert "… truncated: showing" in out
    assert "outline:" in out
    assert "part 11" in out
    assert "next: get(kind='memory', id='me1~part-11')" in out


def test_single_read_returns_head_outline_and_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    body = "\n".join(f"line {i} of prose" for i in range(1, 400))
    out = fit_result_body(
        body,
        verb="get",
        kind="skill",
        context_limited=True,
        targeted_hint="get(kind='skill', id='precis-search-help/toc')",
        read="single",
        outline="precis-search-help~0\t3kB\nprecis-search-help~1",
    )
    assert is_error_free_truncation(out, 4096)
    assert "… truncated: showing" in out
    assert "outline:" in out
    assert "precis-search-help~0\t3kB" in out
    assert "next: get(kind='skill', id='precis-search-help/toc')" in out
    assert "more(cursor=" not in out


def is_error_free_truncation(out: str, cap: int) -> bool:
    return len(out.encode("utf-8")) <= cap + 200  # trailer overhead


def test_explicit_read_skips_context_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `~` selector over the 4 KB context cap returns in full."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "100")
    body = "\n".join(f"chunk line {i}" for i in range(1, 400))
    out = fit_result_body(
        body,
        verb="get",
        kind="paper",
        context_limited=True,
        targeted_hint=None,
        read="explicit",
    )
    assert out == body


def test_explicit_read_over_transport_cap_names_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    body = "\n".join(f"chunk line {i} " + "x" * 50 for i in range(1, 40))
    out = fit_result_body(
        body,
        verb="get",
        kind="paper",
        context_limited=True,
        targeted_hint="get(id='pa5~2..3')",
        read="explicit",
    )
    assert "chunk line 1" in out
    assert "rest: get(id='pa5~2..3')" in out
    assert len(out.encode("utf-8")) <= 500


def test_search_keeps_top_rows_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "400")
    body = "# 40 matches\n" + "\n".join(f"row {i}\tdata" for i in range(1, 41))
    out = fit_result_body(
        body,
        verb="search",
        kind="memory",
        context_limited=True,
        targeted_hint=None,
        read="list",
    )
    assert "row 1" in out
    assert "more lines not shown; narrow q or lower page_size" in out


def test_extent_drops_neighbours_and_names_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "300")
    body = "# me1 — the node\n\nnode body\n\n" + "\n".join(
        f"related-to: me10{i} — a note" for i in range(20)
    )
    out = fit_result_body(
        body,
        verb="get",
        kind="memory",
        context_limited=True,
        targeted_hint=None,
        read="extent",
    )
    assert "# me1 — the node" in out
    assert "dropped" in out
    assert "me10" in out  # dropped neighbour named by handle


def test_transport_cap_refusal_only_when_no_head_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "20")
    with pytest.raises(BadInput):
        fit_result_body(
            "x" * 5000,
            verb="get",
            kind="skill",
            context_limited=False,
            targeted_hint=None,
            read="explicit",
        )
    # A single huge line still yields a byte-cut head, not a refusal.
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "500")
    out = fit_result_body(
        "x" * 5000,
        verb="get",
        kind="skill",
        context_limited=False,
        targeted_hint=None,
        read="explicit",
    )
    assert out.startswith("x")
    assert "…" in out


def test_size_cell_threshold() -> None:
    assert size_cell(500) == ""
    assert size_cell(1023) == ""
    assert size_cell(1024) == "1kB"
    assert size_cell(8192) == "8kB"
    assert size_cell(8700) == "9kB" or size_cell(8700) == "8kB"


def test_row_size_cell_reports_decimal_kb() -> None:
    """Fetchable rows name a size at every scale — decimal kB, not blank
    and not raw bytes (gr568219, gr573411: ``353B`` → ``0.4kB``,
    ``1006B`` → ``1kB``)."""
    assert row_size_cell(0) == "0kB"
    assert row_size_cell(353) == "0.4kB"
    assert row_size_cell(500) == "0.5kB"
    assert row_size_cell(830) == "0.8kB"
    assert row_size_cell(949) == "0.9kB"
    # The ~1kB default is the one blank cell — the ``size [kB]
    # (default 1kB)`` column header says what a blank means.
    assert row_size_cell(1006) == ""
    assert row_size_cell(1024) == ""
    assert row_size_cell(1500) == "1.5kB"
    assert row_size_cell(2000) == "2kB"
    assert row_size_cell(8192) == "8.2kB"


# ── dispatch-level: memory section reads + search/list fitting ──────


def test_big_memory_get_truncates_with_working_section_read(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ~6 KB memory node reads as head + outline, and the outline's
    ``~<section>`` selector reads that part in full."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    ref = store.insert_ref(kind="memory", slug=None, title="big node")
    part = "word " * 400
    text = f"## Background\n\n{part}\n\n## Details\n\n{part}\n\n## Verdict\n\nshort"
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "memory_body"})],
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": ref.id}, fold=True
    )
    assert is_error is False
    assert "… truncated: showing" in body
    assert f"me{ref.id}~background" in body
    assert f"me{ref.id}~details" in body
    assert body.startswith("# memory")

    # The advertised section read returns that part in full (explicit).
    sec, sec_err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": f"{ref.id}~details"}, fold=True
    )
    assert sec_err is False
    assert "## Details" in sec
    assert "… truncated" not in sec


def test_memory_section_selector_direct(runtime_with_store, store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="n")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="## Alpha\n\nfirst\n\n## Beta\n\nsecond",
                meta={"chunk_kind": "memory_body"},
            )
        ],
    )
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": f"{ref.id}~beta"})
    assert "second" in out
    assert "first" not in out


def test_skill_card_over_cap_returns_head_and_outline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "2000")
    from precis.handlers.skill import _load_skill

    text = _load_skill("precis-search-help")
    assert text is not None
    out = fit_result_body(
        text,
        verb="get",
        kind="skill",
        context_limited=True,
        targeted_hint="get(kind='skill', id='precis-search-help/toc')",
        read="single",
    )
    assert "… truncated: showing" in out


def test_range_read_over_context_cap_returns_whole_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ~A..B read over 4 KB but under the transport cap is
    returned whole (amendment A)."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "100")
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "24576")
    body = "\n".join(f"chunk {i} " + "text " * 200 for i in range(8))
    out = fit_result_body(
        body,
        verb="get",
        kind="paper",
        context_limited=True,
        targeted_hint=None,
        read="explicit",
    )
    assert out == body


def test_outline_line_carries_size() -> None:
    from precis._result_budget import _heading_outline

    body = "## Big\n\n" + ("x" * 3000) + "\n\n## Small\n\ntiny"
    outline = _heading_outline(body)
    assert "Big\t3kB" in outline
    assert "Small" in outline
    assert "Small\t" not in outline  # under 1 kB: no size


def test_url_id_with_tilde_is_not_a_selector() -> None:
    """``https://host/~user/page`` must not skip the context cap (fix 1)."""
    from precis._result_budget import read_shape

    assert (
        read_shape("get", {"kind": "web", "id": "https://host/~user/page"}) == "single"
    )
    assert read_shape("get", {"kind": "paper", "id": "pa5~4..11"}) == "explicit"
    assert read_shape("get", {"kind": "memory", "id": "me9~body"}) == "explicit"
    assert read_shape("get", {"kind": "memory", "id": "me9~setup"}) == "explicit"
    assert read_shape("get", {"kind": "web", "id": "https://host/~user"}) == "single"


def test_first_line_cut_respects_cap_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A byte-cut first line + ellipsis stays under the cap, multibyte
    boundary included (fix 2)."""
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "400")
    for body in ("x" * 5000, "é" * 2500):
        out = fit_result_body(
            body,
            verb="get",
            kind="skill",
            context_limited=False,
            targeted_hint=None,
            read="explicit",
        )
        assert len(out.encode("utf-8")) <= 400
        assert "…" in out


def test_no_head_refusal_carries_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "20")
    with pytest.raises(BadInput) as caught:
        fit_result_body(
            "x" * 5000,
            verb="get",
            kind="skill",
            context_limited=False,
            targeted_hint=None,
            read="explicit",
        )
    assert caught.value.next


def test_more_and_cursor_state_are_gone(runtime_with_store: PrecisRuntime) -> None:
    from precis import server
    from precis.tools import TOOL_REGISTRY
    from precis.utils.llm.precis_tools import precis_tool_specs

    assert "more" not in TOOL_REGISTRY
    assert "more" not in server.READ_ONLY_VERBS
    assert "more" not in {spec.name for spec in precis_tool_specs()}
    assert not hasattr(runtime_with_store, "fetch_more")
    assert not hasattr(runtime_with_store, "pagination")


# ── fix-1 round: dispatch-level coverage, the same path an MCP call ──


def _paper_with_blocks(store: Store, slug: str, block_texts: list[str]) -> Any:
    ref = store.insert_ref(kind="paper", slug=slug, title="T paper")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=i, text=t) for i, t in enumerate(block_texts)]
    )
    return ref


def test_paper_range_under_cap_returns_in_full(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _paper_with_blocks(store, "rangep1", [f"block {i} text" for i in range(6)])
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{ref.id}~0..5"}, fold=True
    )
    assert is_error is False
    assert "block 5 text" in body
    assert "rest:" not in body
    assert "… truncated" not in body


def test_paper_range_over_transport_names_rest_once(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "3000")
    ref = _paper_with_blocks(
        store, "rangep2", [f"block {i}\n" + "word " * 200 for i in range(8)]
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{ref.id}~0..7"}, fold=True
    )
    assert is_error is False
    assert body.count("rest:") == 1
    assert re.search(rf"rest: get\(id='pa{ref.id}~\d+(?:\.\.7)?'\)", body), body
    assert len(body.encode("utf-8")) <= 3400
    assert "rest: search(" not in body  # no doubled generic rest line


def test_draft_get_over_cap_returns_head_not_error(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "400")
    todo = store.insert_ref(kind="todo", slug=None, title="P")
    runtime_with_store.dispatch(
        "put", {"kind": "draft", "id": "d1", "title": "Dossier", "project": todo.id}
    )
    dref = store.get_ref(kind="draft", id="d1")
    assert dref is not None
    tail_dc = store.drafts.reading_order(dref.id)[-1].dc
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "d1",
            "chunk_kind": "paragraph",
            "text": "draft body " + "word " * 200,
            "at": {"after": tail_dc},
        },
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": "d1"}, fold=True
    )
    assert is_error is False
    # The structured level map stands in for the truncated head: the rows
    # that fit plus an overflow pointer — never over the cap.
    assert "top-level map" in body
    assert len(body.encode("utf-8")) <= 400


def _nested_draft(runtime: PrecisRuntime, store: Store) -> tuple[Any, list[Any]]:
    """A ~50 kB two-level draft: title heading at root, six section
    headings under it, twelve ~110 B paragraphs per section."""
    todo = store.insert_ref(kind="todo", slug=None, title="P")
    runtime.dispatch(
        "put", {"kind": "draft", "id": "d1", "title": "Dossier", "project": todo.id}
    )
    dref = store.get_ref(kind="draft", id="d1")
    assert dref is not None
    title_dc = store.drafts.reading_order(dref.id)[0].dc
    sections = []
    for i in range(6):
        runtime.dispatch(
            "put",
            {
                "kind": "draft",
                "id": "d1",
                "chunk_kind": "heading",
                "text": f"Section {i}",
                "at": {"into": title_dc},
            },
        )
        sec = next(
            c for c in store.drafts.reading_order(dref.id) if c.text == f"Section {i}"
        )
        sections.append(sec)
        for j in range(12):
            runtime.dispatch(
                "put",
                {
                    "kind": "draft",
                    "id": "d1",
                    "chunk_kind": "paragraph",
                    "text": f"sec {i} para {j} " + "word " * 18,
                    "at": {"into": sec.dc},
                },
            )
    return dref, sections


def test_draft_outline_over_cap_fits_hierarchically(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr537833: ``view='outline'`` on a draft whose flat outline exceeds
    the 4 KB context cap fits *by structure* — the top-level map: each
    row a ``~dc<N>`` section selector carrying its subtree size and
    nested-heading count, small enough to fit whole. A single root
    heading (the common title-only case) auto-descends: the first call
    already shows the sections, not one row re-naming the title."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    dref, sections = _nested_draft(runtime_with_store, store)
    flat = runtime_with_store.hub.handlers["draft"]._render_outline("d1", dref).body
    assert len(flat.encode("utf-8")) <= 4096  # the level map already fits
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": "d1", "view": "outline"}, fold=True
    )
    assert is_error is False, body
    # The whole top-level map fits the cap — no truncation marker, no
    # refusal — and rows carry ~dc<N> selectors + sizes (R3/R6).
    assert len(body.encode("utf-8")) <= 4096, body
    assert "… truncated" not in body
    assert "top-level map" in body
    assert re.search(r"~dc\d+\t\d+kB", body), body
    assert "view='toc'" in body  # footer names the full skeleton
    title_dc = store.drafts.reading_order(dref.id)[0].dc
    # The lone title heading auto-descends: its sections ARE the map.
    assert f"{title_dc} is the single root" in body
    for sec in sections:
        assert f"~{sec.dc}\t" in body
    # Scoping to the title still lands on the same level.
    sec = sections[0]
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {"kind": "draft", "id": f"d1~{title_dc}", "view": "outline"},
        fold=True,
    )
    assert is_error is False, body
    assert f"~{sec.dc}" in body


def test_draft_outline_expands_a_section_by_selector(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get(id='<slug>~dc<N>', view='outline')`` — and the bare
    ``id='dc<N>'`` form — descend one level: the subtree's own flat
    outline when that fits, never a BadInput on ``view='outline'``."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    _dref, sections = _nested_draft(runtime_with_store, store)
    sec = sections[0]
    # A section subtree's flat outline fits — expand renders it whole.
    for ident in (f"d1~{sec.dc}", sec.dc):
        body, is_error = runtime_with_store.dispatch_with_status(
            "get", {"kind": "draft", "id": ident, "view": "outline"}, fold=True
        )
        assert is_error is False, (ident, body)
        assert "sec 0 para 0" in body
        assert "sec 0 para 11" in body
    # A relative/window handle is not an outline scope.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {"kind": "draft", "id": f"{sec.dc}-0..3", "view": "outline"},
        fold=True,
    )
    assert is_error is True
    assert "not a window" in body


def test_draft_level_map_coalesces_with_long_footer(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60 top-level headings plus a long work/hygiene footer: the map
    coalesces sibling runs down to ``N headings, X kB, expand ~dc<N>``
    rows and caps the footer at ~6 lines + ``+N more``, so the whole
    body stays under 4 KB — and every selector a row names resolves."""
    from precis.store.types import Tag
    from precis.utils.job_bubble import bubble_job_failure

    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    proj = store.insert_ref(kind="todo", slug=None, title="P")
    runtime_with_store.dispatch(
        "put", {"kind": "draft", "id": "d1", "title": "Wide", "project": proj.id}
    )
    dref = store.get_ref(kind="draft", id="d1")
    assert dref is not None
    for i in range(59):
        runtime_with_store.dispatch(
            "put",
            {
                "kind": "draft",
                "id": "d1",
                "chunk_kind": "heading",
                "text": f"Section {i:02d} " + "of the plan " * 10,
                "at": {"last": True},
            },
        )
        sec = next(
            c
            for c in store.drafts.reading_order(dref.id)
            if c.text.startswith(f"Section {i:02d}")
        )
        for j in range(2):
            runtime_with_store.dispatch(
                "put",
                {
                    "kind": "draft",
                    "id": "d1",
                    "chunk_kind": "paragraph",
                    "text": f"sec {i} para {j} " + "word " * 30,
                    "at": {"into": sec.dc},
                },
            )
    # A footer longer than the map's ~6-line budget: eight blocked work
    # items plus a hygiene finding would render ~15 lines uncapped.
    for w in range(8):
        child = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Work item {w} " + "long-title " * 8,
            parent_id=proj.id,
        )
        store.add_tag(
            child.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
        )
        job = store.insert_ref(
            kind="job", slug=None, title="tick", parent_id=child.id, meta={}
        )
        store.add_tag(
            job.id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
        )
        bubble_job_failure(store, job.id)

    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": "d1", "view": "outline"}, fold=True
    )
    assert is_error is False, body
    assert len(body.encode("utf-8")) <= 4096, body
    assert "… truncated" not in body
    assert "top-level map" in body
    # Footer was truncated, not dropped wholesale.
    assert re.search(r"\+1?\d+ more", body), body
    # Sibling runs merged: the map names the expansion entry point.
    assert "headings" in body and "expand ~dc" in body
    # Every dc<N> handle a row names resolves to a live chunk here.
    dcs = set(re.findall(r"\bdc\d+\b", body))
    assert dcs
    for tok in dcs:
        chunk = store.drafts.get_draft_chunk(tok)
        assert chunk is not None and int(chunk.ref_id) == int(dref.id), tok
    # …and every ~dc<N> expand selector answers without an error.
    for tok in set(re.findall(r"~dc\d+\b", body)):
        _b, err = runtime_with_store.dispatch_with_status(
            "get",
            {"kind": "draft", "id": f"d1~{tok[1:]}", "view": "outline"},
            fold=True,
        )
        assert err is False, (tok, _b)


def test_draft_level_map_windows_fit_the_cap(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaf-run coalesced to ``dc<A>-0..K`` rows splits at the point
    the window's own read would cross the cap — no hint in the map names
    a window that is itself over budget. ``args={'offset': 0}`` keeps
    its deprecated-alias note on this path."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    todo = store.insert_ref(kind="todo", slug=None, title="P")
    runtime_with_store.dispatch(
        "put", {"kind": "draft", "id": "d1", "title": "Leaves", "project": todo.id}
    )
    dref = store.get_ref(kind="draft", id="d1")
    assert dref is not None
    title_dc = store.drafts.reading_order(dref.id)[0].dc
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "d1",
            "chunk_kind": "heading",
            "text": "Big list",
            "at": {"into": title_dc},
        },
    )
    sec = next(c for c in store.drafts.reading_order(dref.id) if c.text == "Big list")
    for j in range(120):
        runtime_with_store.dispatch(
            "put",
            {
                "kind": "draft",
                "id": "d1",
                "chunk_kind": "paragraph",
                "text": f"para {j:03d} " + "word " * 36,
                "at": {"into": sec.dc},
            },
        )
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": "d1", "view": "outline"}, fold=True
    )
    assert is_error is False, body
    assert len(body.encode("utf-8")) <= 4096, body
    windows = set(re.findall(r"\bdc\d+-0\.\.\d+\b", body))
    assert windows, body
    handler = runtime_with_store.hub.handlers["draft"]
    for win in windows:
        inline = handler._render_chunk(win).body
        assert len(inline.encode("utf-8")) <= 4096, (win, len(inline))
    # The deprecated offset alias still gets its interpreted: line on the
    # level-map path (offset=0 reads as "from the top").
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {"kind": "draft", "id": "d1", "view": "outline", "args": {"offset": 0}},
        fold=True,
    )
    assert is_error is False, body
    assert body.splitlines()[0].startswith("interpreted:")
    assert "offset" in body.splitlines()[0]
    # The chunk-level unknown-view error names 'outline' as a valid view.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "draft", "id": sec.dc, "view": "bogus"}, fold=True
    )
    assert is_error is True
    assert "outline" in body


def test_cross_kind_search_includes_draft(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr537833: the wildcard fan-out skipped kind='draft' entirely, so a
    draft an agent names other than by its title had no cross-kind reach.
    Draft now opts into ``supports_search_hits`` — ``search(kind='*')``
    surfaces ``dc<N>`` chunk hits."""
    todo = store.insert_ref(kind="todo", slug=None, title="P")
    runtime_with_store.dispatch(
        "put", {"kind": "draft", "id": "d1", "title": "tech-plan", "project": todo.id}
    )
    dref = store.get_ref(kind="draft", id="d1")
    assert dref is not None
    tail_dc = store.drafts.reading_order(dref.id)[-1].dc
    runtime_with_store.dispatch(
        "put",
        {
            "kind": "draft",
            "id": "d1",
            "chunk_kind": "paragraph",
            "text": "the quixotic-zeppelin migration plan",
            "at": {"after": tail_dc},
        },
    )
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "*", "q": "quixotic-zeppelin"}, fold=True
    )
    assert is_error is False, body
    assert "tech-plan" in body
    para = store.drafts.reading_order(dref.id)[-1]
    assert para.dc in body


def test_quest_get_over_cap_returns_head_not_error(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "400")
    ack = runtime_with_store.dispatch(
        "put",
        {
            "kind": "quest",
            "text": "a catalyst question worth chasing " + "why " * 300,
        },
    )
    m = re.search(r"\bqu(\d+)\b", ack)
    assert m is not None
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "quest", "id": int(m.group(1))}, fold=True
    )
    assert is_error is False, body
    assert "… truncated: showing" in body


def test_youtube_get_over_cap_returns_head_not_error(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    mod = types.ModuleType("youtube_transcript_api")

    class _Snippet:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Api:
        def fetch(self, video_id: str, languages: list[str]) -> Any:
            return [_Snippet("hello world " * 40)]

    mod.YouTubeTranscriptApi = _Api  # type: ignore[attr-defined]
    errs = types.ModuleType("youtube_transcript_api._errors")
    for name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable"):
        setattr(errs, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errs)

    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "300")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": "dQw4w9WgXcQ"}, fold=True
    )
    assert is_error is False
    assert "… truncated: showing" in body


def test_perplexity_research_get_over_cap_returns_head_not_error(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv("PERPLEXITY_API_KEY", "TEST-KEY")

    class _Resp:
        status_code = 200
        text = "stub"
        headers: dict[str, str] = {}

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": "An answer that goes on. " * 20}}],
                "citations": [],
            }

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_e: Any) -> None:
            pass

        def post(self, url: str, *, headers: Any = None, json: Any = None) -> Any:
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    from precis.utils.http import reset_circuit

    reset_circuit()
    monkeypatch.setattr("precis.utils.http._default_sleep", lambda _s: None)

    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "300")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {"kind": "perplexity-research", "id": "landscape of test answers"},
        fold=True,
    )
    assert is_error is False
    assert "… truncated: showing" in body


def test_real_search_over_cap_truncates_with_line_count(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(15):
        ref = store.insert_ref(kind="memory", slug=None, title=f"plaza note {i}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"plaza fact {i} " + "word " * 30,
                    meta={"chunk_kind": "memory_body"},
                )
            ],
        )
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "800")
    body, is_error = runtime_with_store.dispatch_with_status(
        "search", {"kind": "memory", "q": "plaza", "page_size": 15}, fold=True
    )
    assert is_error is False
    assert "… truncated: showing" in body
    assert "more lines not shown; narrow q or lower page_size" in body


def test_skill_get_over_cap_returns_head_and_sized_outline(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "2000")
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "skill", "id": "precis-overview"}, fold=True
    )
    assert is_error is False
    assert "… truncated: showing" in body
    assert "outline:" in body
    assert "precis-overview~0" in body
    assert "kB" in body


# ── gr676312: batch id-list reads are explicit, with a stated-bound dial ──


def _two_body_nodes(store: Store, words: int = 400) -> tuple[int, int]:
    """Two memory nodes whose ~2 kB+ bodies are the gripe's repro shape."""
    ids = []
    for label in ("alpha", "beta"):
        ref = store.insert_ref(kind="memory", slug=None, title=f"batch {label}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"## {label}\n\n" + f"{label} prose " * words,
                    meta={"chunk_kind": "memory_body"},
                )
            ],
        )
        ids.append(ref.id)
    return ids[0], ids[1]


def test_batch_id_list_classifies_explicit_before_view_and_extent() -> None:
    """gr676312 (review td704220): the id-list check must run before the
    view=/extent= checks, or a batch body/extent read falls back to the
    single/extent shapes and an enabled context cap can cut the joined
    blocks mid-page."""
    from precis._result_budget import read_shape

    assert read_shape("get", {"id": [1, 2], "view": "body"}) == "explicit"
    assert read_shape("get", {"id": [1, 2], "view": "full"}) == "explicit"
    assert read_shape("get", {"id": [1, 2], "extent": "fisheye"}) == "explicit"
    assert read_shape("get", {"id": [1, 2], "view": "fisheye+1hop"}) == "explicit"
    assert (
        read_shape(
            "get",
            {"id": [1, 2], "extent": "fisheye", "__extras__": {"max_bytes": 9}},
        )
        == "explicit"
    )
    # Untouched neighbours of the reorder.
    assert read_shape("get", {"id": 5, "view": "body"}) == "single"
    assert read_shape("get", {"id": 5, "extent": "fisheye"}) == "extent"
    assert read_shape("get", {"id": [1, 2]}) == "explicit"


def test_batch_body_read_serves_both_bodies_one_call(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The gripe's call: get(kind='memory', id=[A, B], view='body') — both
    ~2 kB+ bodies in one result, no 'more — next:' cursor."""
    first, second = _two_body_nodes(store)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": [first, second], "view": "body"}
    )
    assert is_error is False
    assert "alpha prose" in body and "beta prose" in body
    assert "more — next:" not in body
    assert "… truncated" not in body


def test_batch_body_read_skips_enabled_context_cap(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr676312 runtime path: with the agent-context cap enabled and
    fold on, a batch body read over that cap still returns whole — the
    explicit classification skips the context cap that a 'single'-shape
    batch read used to hit, losing whole blocks and their cursor."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    first, second = _two_body_nodes(store, words=400)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": [first, second], "view": "body"}, fold=True
    )
    assert is_error is False
    assert "alpha prose" in body and "beta prose" in body
    assert "more — next:" not in body
    assert "… truncated" not in body
    assert len(body.encode("utf-8")) > 4096


def test_batch_body_read_stated_max_bytes_past_frame_cap(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr676312 brief step 2: a stated args={'max_bytes': N} is the
    caller's acknowledgement past the transport cap — the batch read
    serves as much as was asked for, and the continuation re-states the
    bound."""
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "4096")
    ids = []
    for i in range(4):
        ref = store.insert_ref(kind="memory", slug=None, title=f"stated node {i}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"## node {i}\n\n" + f"stated prose {i} " * 250,
                    meta={"chunk_kind": "memory_body"},
                )
            ],
        )
        ids.append(ref.id)
    # Frame cap alone: the batch pre-page emits one block and names the
    # rest on the cursor, under the chokepoint's fit.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": ids, "view": "body"}, fold=True
    )
    assert is_error is False
    assert "… truncated" not in body
    assert "3 more — next:" in body
    assert "stated prose 0" in body
    assert "stated prose 3" not in body
    # Stated bound: the pre-page and the explicit fit both take it.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get",
        {
            "kind": "memory",
            "id": ids,
            "view": "body",
            "__extras__": {"max_bytes": 24576},
        },
        fold=True,
    )
    assert is_error is False
    assert "… truncated" not in body
    assert "more — next:" not in body
    for i in range(4):
        assert f"stated prose {i}" in body


def test_stated_max_bytes_validation_refuses_at_chokepoint(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Garbage and over-ceiling stated bounds refuse before dispatch,
    with a next step — never clamped silently."""
    from precis._result_budget import MAX_STATED_BODY_BYTES

    first, second = _two_body_nodes(store)
    for bad in ("lots", 0, True, MAX_STATED_BODY_BYTES + 1):
        body, is_error = runtime_with_store.dispatch_with_status(
            "get",
            {
                "kind": "memory",
                "id": [first, second],
                "view": "body",
                "__extras__": {"max_bytes": bad},
            },
        )
        assert is_error is True, bad
        if bad == MAX_STATED_BODY_BYTES + 1:
            assert "exceeds the stated-read ceiling" in body
        else:
            assert "must be a positive integer" in body


def test_fit_result_body_honours_stated_bound_for_explicit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stated dial replaces the frame cap for explicit reads; other
    shapes keep the frame cap (open-ended protection is the point)."""
    monkeypatch.delenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", raising=False)
    monkeypatch.delenv("PRECIS_MAX_BODY_BYTES", raising=False)
    from precis._result_budget import DEFAULT_MAX_BODY_BYTES

    body = "\n".join(f"row {i}" for i in range(3000))
    assert len(body.encode("utf-8")) > DEFAULT_MAX_BODY_BYTES

    def _fit(read: str, stated: int | None) -> str:
        return fit_result_body(
            body,
            verb="get",
            kind="memory",
            context_limited=False,
            targeted_hint=None,
            read=read,
            stated_max_bytes=stated,
        )

    sized = len(body.encode("utf-8"))
    out = _fit("explicit", sized)
    assert out == body  # the stated bound admits the whole read
    out = _fit("explicit", 5000)
    assert "cap stated max_bytes (5000 bytes)" in out
    assert len(out.encode("utf-8")) <= 5000 + 200
    out = _fit("single", sized)
    assert "… truncated: showing" in out  # single shape ignores the dial
